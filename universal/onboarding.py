"""Pure Telegram onboarding and reminder delivery adapters.

This is a reusable backend component, NOT a deployed webhook receiver.
The caller must verify group/channel administrator rights using Telegram
getChatMember before calling confirm_address or callback_choice.
"""
from __future__ import annotations

import json
import html
from datetime import datetime
from typing import Callable

from universal.reminder_core import (
    choose_lead, inline_choice_keyboard, lease_due, mark_failed,
    mark_sent, parse_preference_action, register, reminder_text,
    selected_leads, set_leads, subscription_for_chat, unix_utc,
    reserve_free_delivery_slot,
)


PROMPT = (
    "✅ Адресу збережено.\n"
    "⏰ Чи нагадувати перед плановим відключенням електроенергії?\n"
    "За замовчуванням нагадування ВИМКНЕНО.\n"
    "Позначте потрібний час або кілька варіантів. "
    "Натисніть «Зберегти вибір».\n"
    "Кожна позначка — окреме нагадування.\n"
    "Повідомлення про графік, зміни та скасування працюють незалежно."
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
    if recipient_kind == "private" and recipient_chat_id != actor_id:
        raise PermissionError("Не можна налаштовувати чужий особистий чат")
    if recipient_kind in ("group", "supergroup", "channel"):
        if configuration_chat_id is not None and configuration_chat_id != actor_id:
            raise PermissionError("Налаштування іншого чату доступні лише особисто адміністратору")
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
        "reply_markup": inline_choice_keyboard(sub_id, selected_leads(db, sub_id)),
        "subscription_id": sub_id,
    }


def _draft_choices(db, subscription_id: int, actor_id: str):
    draft = db.execute("""
        SELECT choices FROM reminder_drafts
        WHERE subscription_id=? AND actor_id=?
    """, (subscription_id, actor_id)).fetchone()
    return (tuple(json.loads(draft["choices"])) if draft else
            selected_leads(db, subscription_id))


def callback_choice(
    db, *,
    callback_data: str,
    acting_user_id: str,
    is_admin_of_destination: Callable[[str, str], bool],
    now: datetime,
):
    """Process checkbox choices securely; default reminders OFF.

    A channel's subscriber chat_id comes from the DB. Never trust callback
    origin or callback content as authorization to edit a channel/group.
    """
    action, sub_id, lead = parse_preference_action(callback_data)
    row = db.execute("""
        SELECT d.chat_id,d.chat_kind,d.owner_user_id
        FROM subscriptions s JOIN destinations d ON d.id=s.destination_id
        WHERE s.id=?
    """, (sub_id,)).fetchone()
    if row is None:
        raise PermissionError("Підписку не знайдено")
    chat = row["chat_id"]
    if row["chat_kind"] == "private":
        authorized = (row["owner_user_id"] == acting_user_id
                      and chat == acting_user_id)
    else:
        authorized = bool(is_admin_of_destination(chat, acting_user_id))
    if not authorized:
        raise PermissionError("Налаштування може змінювати лише адміністратор")

    choices = _draft_choices(db, sub_id, acting_user_id)
    if action == "toggle":
        selection = set(choices)
        if lead in selection:
            selection.remove(lead)
        else:
            selection.add(lead)
        choices = tuple(sorted(selection, reverse=True))
        db.execute("""
            INSERT INTO reminder_drafts(subscription_id,actor_id,choices,updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(subscription_id,actor_id)
            DO UPDATE SET choices=excluded.choices,updated_at=excluded.updated_at
        """, (sub_id, acting_user_id, json.dumps(choices), unix_utc(now)))
        return {
            "chat_id": chat, "subscription_id": sub_id,
            "chosen_minutes": None, "pending_minutes": choices,
            "reply_markup": inline_choice_keyboard(sub_id, choices),
            "confirmation": "Позначки оновлено. Натисніть «Зберегти вибір».",
        }
    if action == "off":
        choices = ()
    elif action == "legacy":
        choices = () if lead == 0 else (lead,)
    elif action != "save":
        raise ValueError("Невідома дія")

    # Saving is explicit. An untouched or empty draft keeps reminders OFF.
    set_leads(db, subscription_id=sub_id, chat_id=chat,
              actor_is_authorized=True, minutes=choices, now=now)
    db.execute(
        "DELETE FROM reminder_drafts WHERE subscription_id=? AND actor_id=?",
        (sub_id, acting_user_id),
    )
    if not choices:
        confirmation = (
            "✅ Нагадування вимкнено.\n"
            "Повідомлення про графік, зміни та скасування залишаються."
        )
    else:
        formats = ["1 годину" if minute == 60 else f"{minute} хвилин"
                   for minute in sorted(choices, reverse=True)]
        confirmation = (
            "✅ Нагадування налаштовано: " +
            ", ".join(f"за {value}" for value in formats) + ".\n"
            "Це додаткові нагадування до підтверджених відключень."
        )
    return {
        "chat_id": chat, "subscription_id": sub_id,
        "chosen_minutes": choices[0] if len(choices) == 1 else 0 if not choices else None,
        "pending_minutes": choices,
        "reply_markup": inline_choice_keyboard(sub_id, choices),
        "confirmation": confirmation,
    }


def format_subscription_dashboard(subscriptions, max_chars=3500):
    """Split one user's private settings dashboard into safe Telegram messages.

    Caller must first authorize every subscription. Never use this renderer
    to display private addresses inside a group or channel.
    """
    if max_chars < 300 or max_chars > 4096:
        raise ValueError("Некоректна довжина повідомлення")
    header = "📋 <b>Мої адреси та отримувачі</b>"
    if not subscriptions:
        return [header + "\n\nНемає підключених адрес."]
    pages = []
    current = header
    for item in subscriptions:
        kind = item["chat_kind"]
        glyph = {"private": "👤", "group": "👥", "supergroup": "👥",
                 "channel": "📢"}.get(kind, "📍")
        dest = html.escape(item["chat_title"][:120])
        label = html.escape(item["label"][:160])
        status = "активна" if item["status"] == "active" else "призупинена"
        leads = item.get("reminder_minutes", ())
        title = f"{glyph} <b>{dest}</b> · №{item['subscription_id']}"
        lines = [title, f"Назва: <b>{label}</b>",
                 f"Черга: {html.escape(item['queue'])}",
                 f"Стан: {status}"]
        if "settlement" in item:
            # Private dashboard only, never a public group reply.
            lines.append("Адреса: " + html.escape(
                f"{item['settlement']}, {item['street']}, {item['building']}"
            ))
        lines.append("Нагадування: " + (
            ", ".join(f"за {x} хв" for x in sorted(leads, reverse=True))
            if leads else "вимкнено"
        ))
        entry = "\n\n" + "\n".join(lines)
        if len(current) + len(entry) > max_chars:
            pages.append(current)
            current = header + entry
        else:
            current += entry
    pages.append(current)
    return pages


def send_due_batch(db, send_message, now: datetime, batch=20):
    """No paid Telegram sending; aggregate labels and back off on free-rate limits.

    One verified due interval in one chat = at most one message, even if
    multiple subscriptions point to matching outages. Failed deliveries retry.
    For nationwide use a distributed rate limiter, not this SQLite prototype.
    """
    jobs = lease_due(db, now, batch=batch)
    buckets = {}
    for job in jobs:
        key = (job["chat_id"], job["due_at"], job["start_at"])
        buckets.setdefault(key, []).append(job)
    delivered = 0
    for (chat_id, _due_at, start_at), entries in buckets.items():
        if not reserve_free_delivery_slot(
            db, chat_id=chat_id, chat_kind=entries[0]["chat_kind"], now=now,
        ):
            for job in entries:
                mark_failed(db, job["id"], now, retry_seconds=2)
            continue
        labels = tuple(dict.fromkeys(item["label"] for item in entries))
        if len(labels) == 1:
            text = reminder_text(entries[0], now)
        else:
            lead = max(1, (start_at - unix_utc(now) + 59) // 60)
            start_time = datetime.fromtimestamp(
                start_at, tz=now.tzinfo
            ).strftime("%H:%M")
            text = (
                "⏰ Нагадування про планове відключення\n"
                f"Початок: {start_time} · приблизно за {lead} хв.\n"
                "Адреси:\n" + "\n".join(f"• {label}" for label in labels) +
                "\nЧас може змінитися. Перевіряйте оновлення графіка."
            )
        try:
            send_message(chat_id, text)
        except Exception:
            for job in entries:
                mark_failed(db, job["id"], now)
        else:
            for job in entries:
                mark_sent(db, job["id"])
            delivered += 1
    return delivered
