"""Reliability guards for ONE configured house and ONE Telegram destination.

No address or Telegram credentials are kept in this module.
Daily confirmations come only from a fully parsed DTEK fact table.
GitHub Actions cannot guarantee a five-minute warning deadline.
"""
from __future__ import annotations

import hashlib
import html
import re
import secrets
from datetime import datetime, timedelta, time as dt_time
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
ALLOWED = (5, 15, 30, 60)
GRACE_SECONDS = 60


def parse_reminder_minutes(raw: str | None) -> tuple[int, ...]:
    """Default OFF; permit multiple opt-in lead times only when explicit."""
    if not raw or not str(raw).strip() or str(raw).strip() == "0":
        return ()
    parts = [p.strip() for p in str(raw).split(",")]
    if not parts or any(not re.fullmatch(r"\d{1,2}", p) for p in parts):
        raise ValueError("REMINDER_MINUTES: дозволено лише 0 або 5,15,30,60")
    values = [int(p) for p in parts]
    if any(x not in ALLOWED for x in values):
        raise ValueError("REMINDER_MINUTES: дозволено лише 0 або 5,15,30,60")
    return tuple(sorted(set(values), reverse=True))


def due_reminders(source, group: str, now: datetime, choices: tuple[int, ...],
                  state: dict, parse_verified) -> list[tuple[str, str, datetime]]:
    """Messages only inside strict one-minute grace after desired due time.

    No weekly forecasts, unknown slots or possible ('maybe') outages.
    The source must contain complete current daily GPV facts.
    """
    if not choices or not source or not source.fact or not source.preset:
        return []
    labels = source.preset.time_zone
    today = now.astimezone(KYIV).date()
    emitted = []
    for timestamp, groups in source.fact.days.items():
        day = datetime.fromtimestamp(int(timestamp), KYIV).date()
        if day not in (today, today + timedelta(days=1)):
            continue
        spans = parse_verified(groups.get(group), labels)
        if spans is None:
            continue
        for start_minutes, _end_minutes, mode in spans:
            if mode != "off" or not 0 <= start_minutes < 1440:
                continue
            outage_start = datetime.combine(day, dt_time(0, 0), tzinfo=KYIV)
            outage_start += timedelta(minutes=start_minutes)
            for lead in choices:
                due = outage_start - timedelta(minutes=lead)
                deadline = min(due + timedelta(seconds=GRACE_SECONDS),
                               outage_start)
                if not due <= now < deadline:
                    continue
                key = (f"reminder:{day.isoformat()}:{group}:"
                       f"{start_minutes}:{lead}")
                if key in state:
                    continue
                remaining = max(1, int((outage_start - now).total_seconds() // 60))
                text = "\n".join((
                    "⏰ <b>Нагадування про планове відключення</b>",
                    f"📅 {day:%d.%m.%Y} · Черга {html.escape(group.removeprefix('GPV'))}",
                    f"Початок: <b>{start_minutes // 60:02d}:{start_minutes % 60:02d}</b>",
                    f"До відключення — приблизно {remaining} хв.",
                    "Час може змінитися. Актуальні зміни графіка публікуються окремо.",
                ))
                emitted.append((key, text, deadline))
    return sorted(emitted, key=lambda x: x[2])


def queue_hold(state: dict, *, verified_groups: list[str] | tuple[str, ...],
               selected_group: str, approval: str = "",
               new_code: str | None = None) -> tuple[bool, str | None]:
    """Pause ALL alerts when the operator no longer verifies selected queue.

    A single notification contains a one-time code. Owner must set
    GitHub Action vars QUEUE_APPROVAL=<code> and DTEK_GROUP=<new queue>.
    No public home address is ever included in the message or state.
    """
    actual = sorted(set(verified_groups))
    old = state.get("house:queue_hold")
    if old:
        # A second, verified change during a hold requires a NEW acknowledgment.
        if actual and actual != old.get("groups"):
            old["groups"] = actual
            old["code"] = new_code or secrets.token_hex(4)
            old["notified"] = False
            return True, queue_hold_message(old)
        if (approval and approval == old.get("code")
                and selected_group in actual):
            state.pop("house:queue_hold", None)
            return False, None
        if not old.get("notified"):
            return True, queue_hold_message(old)
        return True, None

    if selected_group in actual:
        return False, None

    # Empty or unverified queue assignment is NOT a reliable new queue.
    if not actual:
        return True, None

    code = new_code or secrets.token_hex(4)
    hold = {"code": code, "groups": actual, "notified": False}
    state["house:queue_hold"] = hold
    return True, queue_hold_message(hold)


def queue_hold_message(hold: dict) -> str:
    groups = ", ".join(html.escape(g.removeprefix("GPV"))
                       for g in hold["groups"])
    code = html.escape(hold["code"])
    return "\n".join((
        "⚠️ <b>Черга будинку змінилася</b>",
        f"ДТЕК указує: {groups}.",
        "Публікацію графіків і нагадувань призупинено.",
        "Адміністратору: перевірте чергу будинку.",
        "Щоб відновити повідомлення, змініть у GitHub змінну DTEK_GROUP",
        "і підтвердьте перехід змінною QUEUE_APPROVAL:",
        f"<code>{code}</code>",
        "До підтвердження бот не публікуватиме графіки.",
    ))


def trim_state(state: dict, today) -> int:
    """Keep existing deduplication state bounded without deleting live keys."""
    before = len(state)
    for key in list(state):
        date_text = None
        if key.startswith(("reminder:", "week:")):
            parts = key.split(":")
            date_text = parts[1] if len(parts) > 1 else None
        elif re.match(r"^\d{4}-\d{2}-\d{2}:", key):
            date_text = key.split(":")[0]
        if not date_text:
            continue
        try:
            day = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < today - timedelta(days=21):
            state.pop(key, None)
    return before - len(state)
