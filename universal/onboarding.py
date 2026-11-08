"""Pure Telegram onboarding and reminder delivery adapters.

This is a reusable backend component, NOT a deployed webhook receiver.
The caller must verify group/channel administrator rights using Telegram
getChatMember before calling confirm_address or callback_choice.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from universal.reminder_core import (
    choose_lead, inline_choice_keyboard, lease_due, mark_failed,
    mark_sent, parse_choice, register, reminder_text, subscription_for_chat,
)


PROMPT = (
    "✅ Адресу збережено.\n"
    "⏰ Чи нагадувати перед плановим відключенням електроенергії?\n"
    "Це додаткове нагадування. Повідомлення про новий графік,"
    " зміни та скасування залишаються увімкненими."
)


def confirm_address(
    db, *,
    recipient_chat_id: str,
    recipient_kind: str,
    actor_id: str,
    authorized: bool,
    address: dict,
    queue_code: str,
    label: str = "Адреса",
    configuration_chat_id: str | None = None,
):
    """Send preference question immediately after provider validated address.

    For groups/channels, the application should normally send this question
    in the admin's PRIVATE chat, not into the public group/channel.
    """
    if not authorized:
        raise PermissionError("Адресу може налаштовувати лише власник або адміністратор")
    required = {"operator", "oblast", "settlement", "street", "building"}
    if not isinstance(address, dict) or not required.issubset(address):
        raise ValueError("Адреса не пройшла перевірку оператора")
    sub_id = register(
        db, chat_id=recipient_chat_id, chat_kind=recipient_kind,
        actor_id=actor_id, queue_code=queue_code, label=label,
        **{k: address[k] for k in required}
    )
    return {
        "method": "sendMessage",
        "chat_id": configuration_chat_id or recipient_chat_id,
        "text": PROMPT,
        "reply_markup": inline_choice_keyboard(sub_id),
        "subscription_id": sub_id,
    }


def callback_choice(
    db, *,
    callback_data: str,
    acting_user_id: str,
    is_admin_of_destination: Callable[[str, str], bool],
    now: datetime,
):
    """Validate inline choice against target chat and current administrator.

    A callback's origin may be the ADMIN'S private chat, while the subscription
    belongs to a CHANNEL. Always find the target from the DB, never trust a
    target_chat_id supplied in callback payload.
    """
    sub_id, lead = parse_choice(callback_data)
    row = db.execute("""
        SELECT d.chat_id,d.chat_kind,d.owner_user_id
        FROM subscriptions s
        JOIN destinations d ON d.id=s.destination_id
        WHERE s.id=?
    """, (sub_id,)).fetchone()
    if row is None:
        raise PermissionError("Підписку не знайдено")
    chat = row["chat_id"]
    if row["chat_kind"] == "private":
        authorized = row["owner_user_id"] == acting_user_id and chat == acting_user_id
    else:
        authorized = is_admin_of_destination(chat, acting_user_id)
    choose_lead(
        db, subscription_id=sub_id, chat_id=chat,
        actor_is_authorized=authorized, minutes=lead, now=now,
    )
    if lead == 0:
        text = ("✅ Нагадування вимкнено.\n"
                "Сповіщення про графік, його зміни та скасування залишаються.")
    else:
        title = "1 годину" if lead == 60 else f"{lead} хвилин"
        text = (
            f"✅ Нагадування налаштовано: за {title} до кожного "
            "підтвердженого планового відключення.\n"
            "Зміни графіка та скасування надходитимуть окремо."
        )
    return {"chat_id": chat, "subscription_id": sub_id,
            "chosen_minutes": lead, "confirmation": text}


def send_due_batch(db, send_message, now: datetime, batch=20):
    """Worker loop unit: send then mark delivered; failures remain retryable.

    send_message(chat_id, text) must raise on Telegram error or 429.
    Run a dispatcher often enough, with per-chat and global rate limiting.
    """
    delivered = 0
    for job in lease_due(db, now, batch=batch):
        try:
            text = reminder_text(job, now)
            send_message(job["chat_id"], text)
        except Exception:
            mark_failed(db, job["id"], now)
        else:
            mark_sent(db, job["id"])
            delivered += 1
    return delivered
