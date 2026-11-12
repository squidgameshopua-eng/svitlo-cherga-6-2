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


def verified_house_queues(raw) -> tuple[str, ...]:
    """Accept only explicit, recognized DTEK queue identifiers.

    Empty/malformed data never imply an actual queue removal.
    """
    if not isinstance(raw, (list, tuple, set)) or not raw:
        raise ValueError("ДТЕК не підтвердив черги будинку")
    if any(not isinstance(group, str) or
           not re.fullmatch(r"GPV[1-6]\.[12]", group) for group in raw):
        raise ValueError("ДТЕК повернув непідтверджену чергу")
    return tuple(sorted(set(raw)))


def house_queue_transition(state: dict, groups: tuple[str, ...],
                           default_group: str) -> tuple[list[str], str | None]:
    """Compare verified queue sets and construct one Ukrainian change notice.

    Do NOT update state before successful sending. The caller persists
    'house:groups' after delivery, so a failed notification is retried.
    Old blocking house:queue_hold state is migrated on successful lookup.
    """
    current = verified_house_queues(groups)
    previous = state.get("house:groups")
    has_previous = isinstance(previous, list) and bool(previous)
    if has_previous:
        previous = verified_house_queues(previous)
    else:
        # The previous fixed queue was a monitoring preference, not proof of
        # the actual electrical wiring of this building.
        previous = verified_house_queues([default_group])

    prior = set(previous)
    live = set(current)
    added = sorted(live - prior)
    removed = sorted(prior - live)
    if not added and not removed:
        return list(current), None

    title = ("🔄 <b>ДТЕК оновив черги будинку</b>" if has_previous
             else "⚡️ <b>Уточнено черги будинку</b>")
    lines = [title]
    if added:
        lines.append("➕ Додано: " + ", ".join(
            html.escape(x.removeprefix("GPV")) for x in added))
    if removed:
        label = "Вилучено" if has_previous else "Раніше відстежували"
        lines.append("➖ " + label + ": " + ", ".join(
            html.escape(x.removeprefix("GPV")) for x in removed))
    lines.extend((
        "Актуальні черги: " + ", ".join(
            html.escape(x.removeprefix("GPV")) for x in current),
        "✅ Бот продовжує надсилати графіки всіх черг за цією адресою.",
        "Відключення можуть відрізнятися для частин будинку.",
        '<a href="https://www.dtek-dnem.com.ua/ua/shutdowns">Джерело: ДТЕК</a>',
    ))
    return list(current), "\n".join(lines)


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
