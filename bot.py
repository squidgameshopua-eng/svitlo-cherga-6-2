"""Automatic DTEK outage schedule notifications to Telegram."""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

KYIV = ZoneInfo("Europe/Kyiv")
SOURCE = "https://www.dtek-dnem.com.ua/ua/shutdowns"
LOG = logging.getLogger("dtek-bot")


def time_range(label):
    """Parse DTEK hourly '00-01' and minute-precision '00:00–01:00' slots."""
    source = str(label).strip()
    source = re.sub(r"\s+", "", source).replace("–", "-").replace("—", "-")
    hourly = re.fullmatch(r"(\d{1,2})-(\d{1,2})", source)
    if hourly:
        start_hour, end_hour = map(int, hourly.groups())
        if 0 <= start_hour < 24 and start_hour < end_hour <= 24:
            return start_hour * 60, end_hour * 60
        raise ValueError(f"Неверный часовой интервал ДТЭК: {label}")
    minute = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", source)
    if not minute:
        raise ValueError(f"Неверная подпись времени: {label}")
    sh, sm, eh, em = map(int, minute.groups())
    if sh > 23 or eh > 24 or sm > 59 or em > 59 or (eh == 24 and em != 0):
        raise ValueError(f"Неверный интервал времени: {label}")
    a, b = sh * 60 + sm, eh * 60 + em
    if a >= b:
        raise ValueError(f"Неверный интервал времени: {label}")
    return a, b


def intervals(slots, labels):
    if not slots or not labels:
        raise RuntimeError("Нет достоверных интервалов ДТЭК")
    spans = []
    unknown = 0
    for key, status in slots.items():
        if str(key) not in labels:
            raise RuntimeError(f"Нет подписи времени для слота {key}")
        a, b = time_range(labels[str(key)])
        mid = (a + b) // 2
        val = str(getattr(status, "value", status)).lower()
        chunks = {
            "no": [(a, b, "off")],
            "first": [(a, mid, "off")],
            "second": [(mid, b, "off")],
            "maybe": [(a, b, "maybe")],
            "mfirst": [(a, mid, "maybe")],
            "msecond": [(mid, b, "maybe")],
            "yes": [],
        }.get(val)
        if chunks is None:
            unknown += 1
        else:
            spans.extend(chunks)
    spans.sort()
    joined = []
    for a, b, mode in spans:
        if joined and joined[-1][2] == mode and joined[-1][1] == a:
            joined[-1] = (joined[-1][0], b, mode)
        else:
            joined.append((a, b, mode))
    return joined, unknown


def clock(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def minutes_text(minutes):
    """Ukrainian duration; fractional hours stay precise to the minute."""
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} год {mins} хв"
    if hours:
        return f"{hours} год"
    return f"{mins} хв"


def duration(spans, kind):
    """Sum lengths without counting potential outages as confirmed."""
    parts = sorted((a, b) for a, b, status in spans if status == kind)
    total = 0
    if not parts:
        return 0
    start, end = parts[0]
    for a, b in parts[1:]:
        if a <= end:
            end = max(end, b)
        else:
            total += end - start
            start, end = a, b
    return total + end - start


def subtract_spans(before, after):
    """Return merged portions of intervals in before but not in after."""
    result = []
    for a, b in sorted(before):
        remaining = [(a, b)]
        for c, d in sorted(after):
            next_parts = []
            for start, end in remaining:
                if d <= start or c >= end:
                    next_parts.append((start, end))
                else:
                    if start < c:
                        next_parts.append((start, c))
                    if d < end:
                        next_parts.append((d, end))
            remaining = next_parts
        result.extend(remaining)
    merged = []
    for a, b in sorted(result):
        if merged and merged[-1][1] == a:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def describe_changes(previous, current):
    """Explain removed and added time ranges when prior intervals are available."""
    if previous is None:
        return []
    lines = []
    names = (
        ("off", "✅ Прибрано із запланованих:", "❗ Додано заплановані:"),
        ("maybe", "🟢 Прибрано із можливих:", "🟠 Додано можливі:"),
    )
    for kind, removed_title, added_title in names:
        old = [(a, b) for a, b, status in previous if status == kind]
        new = [(a, b) for a, b, status in current if status == kind]
        removed = subtract_spans(old, new)
        added = subtract_spans(new, old)
        if removed:
            lines.append(removed_title)
            lines.extend(f"• {clock(a)}–{clock(b)}" for a, b in removed)
        if added:
            lines.append(added_title)
            lines.extend(f"• {clock(a)}–{clock(b)}" for a, b in added)
    return lines


def day_message(day, group, spans, updated=None, event="new", previous=None):
    """Ukrainian daily message, only when there are scheduled/possible outages."""
    if not spans:
        return None
    titles = {
        "new": "⚡️ <b>Графік відключень</b>",
        "changed": "🔄 <b>Графік відключень змінено</b>",
        "resumed": "⚠️ <b>Відключення знову заплановано</b>",
    }
    lines = [
        titles[event],
        f"📅 {day:%d.%m.%Y} · Черга {html.escape(group.removeprefix('GPV'))}",
    ]
    if event == "changed":
        details = describe_changes(previous, spans)
        if details:
            lines.extend(["", "<b>Що змінилося:</b>", *details])
    for kind, title in (("off", "🔴 Заплановані відключення:"),
                        ("maybe", "🟠 Можливі відключення:")):
        selected = [(a, b) for a, b, status in spans if status == kind]
        if selected:
            lines.extend(["", f"<b>{title}</b>"])
            lines.extend(f"• {clock(a)}–{clock(b)}" for a, b in selected)
    confirmed_minutes = duration(spans, "off")
    possible_minutes = duration(spans, "maybe")
    if confirmed_minutes:
        lines.append(f"\n⏱ Загалом без світла за графіком: <b>{minutes_text(confirmed_minutes)}</b>")
    if possible_minutes:
        lines.append(f"⏱ Можливі відключення: <b>{minutes_text(possible_minutes)}</b>")
    if updated:
        lines.append("\nОновлено: " + html.escape(str(updated)))
    lines.append(f'<a href="{SOURCE}">Джерело: ДТЕК</a>')
    return "\n".join(lines)


def cancellation_message(day, group, updated=None):
    """Cancellation is a meaningful change only after a prior outage notice."""
    lines = [
        "✅ <b>Планові відключення скасовано</b>",
        f"📅 {day:%d.%m.%Y} · Черга {html.escape(group.removeprefix('GPV'))}",
        "",
        "За оновленим графіком планових відключень немає.",
    ]
    if updated:
        lines.append("\nОновлено: " + html.escape(str(updated)))
    lines.append(f'<a href="{SOURCE}">Джерело: ДТЕК</a>')
    return "\n".join(lines)


def weekly_message(monday, group, entries, changed=False):
    """Separate, clearly preliminary Ukrainian weekly forecast."""
    if not entries:
        return None
    sunday = monday + timedelta(days=6)
    lines = [
        "🔄 <b>Тижневий прогноз змінено</b>" if changed else
        "📆 <b>Тижневий прогноз можливих відключень</b>",
        f"Черга {html.escape(group.removeprefix('GPV'))} · {monday:%d.%m}–{sunday:%d.%m.%Y}",
    ]
    weekdays = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")
    for day, spans in entries:
        lines.append(f"\n<b>{weekdays[day.weekday()]} {day:%d.%m}</b>")
        lines.extend(f"• {clock(a)}–{clock(b)}" for a, b in spans)
    lines.append("\n⚠️ Попередній прогноз. Точний графік на день публікується окремо.")
    lines.append(f'<a href="{SOURCE}">Джерело: ДТЕК</a>')
    return "\n".join(lines)


def weekly_cancellation_message(monday, group):
    """Only for complete, explicit all-white weekly forecast after a prior notice."""
    sunday = monday + timedelta(days=6)
    return "\n".join([
        "✅ <b>Тижневий прогноз відключень скасовано</b>",
        f"Черга {html.escape(group.removeprefix('GPV'))} · {monday:%d.%m}–{sunday:%d.%m.%Y}",
        "",
        "У оновленій тижневій таблиці планових відключень немає.",
        f'<a href="{SOURCE}">Джерело: ДТЕК</a>',
    ])


def state_digest(record):
    """Read legacy SHA strings and the new structured state records."""
    if isinstance(record, str):
        return record
    if isinstance(record, dict):
        return record.get("digest")
    return None


def state_status(record):
    """Historical SHA entries were only created for positive outage notices."""
    if isinstance(record, str):
        return "outages"
    if isinstance(record, dict):
        return record.get("status")
    return None


def fingerprint(payload):
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


def parse_verified(slots, labels):
    """Refuse incomplete/unknown tables, especially for all-clear decisions."""
    if not isinstance(slots, dict) or not slots:
        return None
    if set(slots) != set(labels):
        return None
    try:
        spans, unknown = intervals(slots, labels)
    except (ValueError, RuntimeError):
        return None
    if unknown:
        return None
    return spans


def collect_notifications(source, group, today, state=None):
    """
    Return notification candidates only for actual changes.

    Missing/unreadable data are not equivalent to cancellation.
    Complete all-white tables are cancellation evidence only if state
    proves that a positive notice was sent previously.
    """
    state = state or {}
    labels = source.preset.time_zone if source.preset else None
    if not labels:
        LOG.info("Немає часових інтервалів ДТЕК; публікацій немає")
        return []

    actual = {}
    active_region = False
    if source.fact:
        for stamp, group_map in source.fact.days.items():
            day = datetime.fromtimestamp(int(stamp), KYIV).date()
            if day not in (today, today + timedelta(days=1)):
                continue
            actual[day] = group_map
            for slots in group_map.values():
                spans = parse_verified(slots, labels)
                if spans:
                    active_region = True

    result = []
    for day in (today, today + timedelta(days=1)):
        key = f"{day.isoformat()}:{group}"
        slots = actual.get(day, {}).get(group)
        spans = parse_verified(slots, labels)
        if spans is None:
            continue
        if spans:
            # Maintain the legacy hash to avoid duplicate announcements
            # when deploying the new state-aware bot.
            payload = (day.isoformat(), group, True, spans, 0)
            result.append((key, payload, "day", (day, spans, source.fact.update)))
        elif state_status(state.get(key)) == "outages":
            payload = (day.isoformat(), group, "cancelled")
            result.append((key, payload, "day_cancel", (day, source.fact.update)))

    monday = today - timedelta(days=today.weekday())
    week_key = f"week:{monday.isoformat()}:{group}"
    plan = source.preset.groups.get(group) if source.preset.is_active else None
    entries = []
    all_seven_verified = bool(plan)
    if plan:
        for weekday in range(1, 8):
            item = plan.get_day(weekday)
            spans = parse_verified(item.slots, labels) if item else None
            if spans is None:
                all_seven_verified = False
                break
            if spans:
                entries.append((
                    monday + timedelta(days=weekday - 1),
                    tuple((a, b) for a, b, _mode in spans),
                ))

    if all_seven_verified and entries and active_region:
        # Maintain prior weekly hash for previously sent weekly forecasts.
        payload = (monday.isoformat(), group,
                   tuple((day.isoformat(), spans) for day, spans in entries))
        result.append((week_key, payload, "week", (monday, entries)))
    elif all_seven_verified and not entries and state_status(state.get(week_key)) == "outages":
        payload = (monday.isoformat(), group, "cancelled")
        result.append((week_key, payload, "week_cancel", (monday,)))

    return result


def state_read(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def state_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    temp.replace(path)


def telegram_send(token, chat, text, deadline=None):
    """Respect Telegram 429 RetryAfter; never send after reminder deadline.

    Telegram sendMessage has no idempotency key. A network timeout after
    delivery can cause a duplicate on retry; we do not claim exactly once.
    """
    for attempt in range(3):
        if deadline is not None and datetime.now(KYIV) >= deadline:
            raise RuntimeError("Термін нагадування минув; застаріле повідомлення не надсилається")
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                      "disable_web_page_preview": "true"},
                timeout=20,
            )
        except requests.RequestException:
            raise RuntimeError("Немає зв'язку з Telegram; доставка невідома") from None
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError(f"Некоректна відповідь Telegram HTTP {response.status_code}") from None
        if response.status_code == 200 and body.get("ok") is True:
            return body.get("result", {}).get("message_id")
        if response.status_code == 429:
            delay = (body.get("parameters") or {}).get("retry_after", 1)
            try:
                delay = max(1, int(delay))
            except (TypeError, ValueError):
                delay = 1
            if attempt < 2 and delay <= 12 and (
                deadline is None or datetime.now(KYIV) + timedelta(seconds=delay + 1) < deadline
            ):
                time.sleep(delay)
                continue
            raise RuntimeError(f"Telegram обмежив швидкість, повтор через {delay} с")
        raise RuntimeError(f"Telegram відхилив повідомлення: HTTP {response.status_code}")
    raise RuntimeError("Telegram: кількість повторів вичерпано")


async def get_schedule():
    """Fetch a DTEK regional queue schedule without an address."""
    from types import SimpleNamespace
    from dtek_client import DtekClient
    from dtek_client.models import FactSchedule, PresetSchedule

    group = os.getenv("DTEK_GROUP", "6.2").strip()
    if not re.fullmatch(r"[1-6]\.[12]", group):
        raise RuntimeError("Некорректная очередь ДТЭК (пример: 6.2)")
    group_id = "GPV" + group
    api_url = "https://www.dtek-dnem.com.ua/ua/ajax"

    async def fetch(client):
        raw = await client._post({
            "method": "checkDisconUpdate",
            "update": "01.01.2000 00:00",
        })
        if not isinstance(raw, dict) or not isinstance(raw.get("preset"), dict):
            raise RuntimeError("ДТЭК не вернул общую таблицу графиков")
        preset = PresetSchedule.model_validate(raw["preset"])
        fact_raw = raw.get("fact")
        fact = FactSchedule.model_validate(fact_raw) if isinstance(fact_raw, dict) else None
        if not preset.time_zone:
            raise RuntimeError("ДТЭК не вернул временные интервалы")
        if group_id not in preset.groups and (
            not fact or not any(group_id in m for m in fact.days.values())
        ):
            raise RuntimeError("Очередь ДТЭК не найдена в данных региона")
        LOG.info("ДТЭК: Дніпровська область, черга %s", group)
        return SimpleNamespace(preset=preset, fact=fact), [group_id]

    try:
        async with DtekClient("dnem", ajax_url=api_url) as client:
            return await fetch(client)
    except Exception as ex:
        if os.getenv("DTEK_WAF_BROWSER", "0") != "1":
            raise
        LOG.warning("Прямой запрос ДТЭК не выполнен (%s). Пробую браузер.",
                    type(ex).__name__)
        from curl_cffi.requests import AsyncSession
        from dtek_client.browser_auth import get_cleared_cookies

        cookies, csrf = await get_cleared_cookies(SOURCE)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://www.dtek-dnem.com.ua",
            "Referer": SOURCE,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }
        if csrf:
            headers["X-CSRF-Token"] = csrf
        session = AsyncSession(timeout=20, cookies=cookies, headers=headers,
                               impersonate="chrome120")
        async with DtekClient("dnem", ajax_url=api_url, session=session) as client:
            return await fetch(client)


async def once():
    """One-house notification run: urgent changes first, reminders only while timely."""
    from house_reliability import (
        due_reminders, parse_reminder_minutes, queue_hold, trim_state,
    )

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise RuntimeError("Відсутні TELEGRAM_BOT_TOKEN або TELEGRAM_CHAT_ID")

    path = Path(os.getenv("STATE_PATH", "data/state.json"))
    state = state_read(path)
    now = datetime.now(KYIV)
    today = now.date()
    selected = os.getenv("DTEK_GROUP", "6.2").strip()
    group_id = "GPV" + selected
    leads = parse_reminder_minutes(os.getenv("REMINDER_MINUTES", "0"))
    trim_state(state, today)
    state_write(path, state)
    problems = []
    sent = 0

    # A house is distinct from a generic regional queue. If an address secret
    # is provided, verify its operator assignment BEFORE any regional messages.
    house = None
    private_address = os.getenv("DTEK_ADDRESS_JSON", "").strip()
    if private_address:
        try:
            from alerts import fetch_address_status
            house = await fetch_address_status(private_address)
            if not house or not house.group_ids:
                LOG.warning("Адресна черга не підтверджена, повідомлення призупинено")
                return
            paused, notice = queue_hold(
                state, verified_groups=house.group_ids,
                selected_group=group_id,
                approval=os.getenv("QUEUE_APPROVAL", "").strip(),
            )
            state_write(path, state)
            if notice:
                try:
                    telegram_send(token, chat, notice)
                except Exception as ex:
                    LOG.error("Не вдалося попередити про зміну черги: %s",
                              type(ex).__name__)
                    raise
                state["house:queue_hold"]["notified"] = True
                state_write(path, state)
            if paused:
                LOG.info("Чергу будинку змінено; очікується підтвердження адміністратора")
                return
        except Exception as exc:
            LOG.error("Адресну перевірку не виконано: %s", type(exc).__name__)
            # Never publish a possibly wrong queue while address verification fails.
            return
    elif state.get("house:queue_hold"):
        LOG.warning("Чергу не підтверджено; адресний секрет відсутній")
        return

    pending = []
    seq = 0

    def enqueue(priority, key, value, message, origin, deadline=None, cursor=None):
        nonlocal seq
        pending.append((priority, seq, key, value, message, origin, deadline, cursor))
        seq += 1

    # Official emergency and GAV announcements are prioritized.
    if os.getenv("ANNOUNCEMENTS_ENABLED", "true").lower() == "true":
        try:
            from alerts import fetch_official_page, region_candidates
            events = region_candidates(fetch_official_page(), state, now)
            for post_id, key, value, message in events:
                enqueue(0, key, value, message, "official", cursor=post_id)
            if not events and "official:last_id" in state:
                state_write(path, state)
        except Exception as exc:
            LOG.warning("Офіційні оголошення недоступні: %s", type(exc).__name__)

    if house is not None:
        try:
            from alerts import address_decision
            for _channel, key, value, message in address_decision(
                    house, state, now, expected=group_id):
                if key == "address:groups":
                    # Queue change is handled by the blocking confirmation guard.
                    state[key] = value
                    state_write(path, state)
                    continue
                enqueue(1, key, value, message, "address")
        except Exception as exc:
            LOG.warning("Адресні події недоступні: %s", type(exc).__name__)

    try:
        source, groups = await get_schedule()
    except Exception as exc:
        LOG.error("Графік ДТЕК недоступний: %s", type(exc).__name__)
        source, groups = None, []

    if source:
        for group in groups:
            for key, payload, kind, parts in collect_notifications(source, group, today, state):
                digest = fingerprint(payload)
                old = state.get(key)
                if state_digest(old) == digest:
                    continue
                previous_status = state_status(old)
                next_state = {
                    "digest": digest,
                    "status": "cancelled" if kind.endswith("_cancel") else "outages",
                }
                if kind == "day":
                    day, spans, updated = parts
                    event = ("resumed" if previous_status == "cancelled" else
                             "changed" if previous_status == "outages" else "new")
                    old_spans = None
                    if isinstance(old, dict) and isinstance(old.get("spans"), list):
                        old_spans = [tuple(item) for item in old["spans"]]
                    message = day_message(day, group, spans, updated,
                                          event=event, previous=old_spans)
                    next_state["spans"] = [list(span) for span in spans]
                    priority = 2 if event == "changed" else 4
                elif kind == "day_cancel":
                    day, updated = parts
                    message = cancellation_message(day, group, updated)
                    priority = 2
                elif kind == "week":
                    monday, entries = parts
                    message = weekly_message(monday, group, entries,
                                             changed=previous_status == "outages")
                    priority = 7
                elif kind == "week_cancel":
                    message = weekly_cancellation_message(parts[0], group)
                    priority = 3
                else:
                    LOG.warning("Невідомий тип графіка: %s", kind)
                    continue
                if message:
                    enqueue(priority, key, next_state, message, "schedule")

            # Exact lead-time reminders are independent of schedule-change notices.
            for key, message, deadline in due_reminders(
                    source, group, now, leads, state, parse_verified):
                enqueue(5, key, {"sent": True}, message, "reminder", deadline)

    # Preserve order among official announcements (e.g. start then cancel).
    # A failed send must not advance the official feed checkpoint.
    pending.sort(key=lambda item: (item[0], item[1]))
    official_failed = False
    last_send = 0.0
    for _priority, _seq, key, value, message, origin, deadline, cursor in pending:
        if origin == "official" and official_failed:
            continue
        if key == "official:last_id" and origin == "official":
            state[key] = value
            state_write(path, state)
            continue
        if not message:
            state[key] = value
            state_write(path, state)
            continue
        if deadline is not None and datetime.now(KYIV) >= deadline:
            LOG.info("Пропущено прострочене нагадування: %s", key)
            continue
        # Limit to one outgoing Telegram message per second for this one chat.
        delay = 1.05 - (time.monotonic() - last_send) if last_send else 0
        if delay > 0:
            time.sleep(delay)
        if deadline is not None and datetime.now(KYIV) >= deadline:
            LOG.info("Пропущено прострочене нагадування: %s", key)
            continue
        try:
            telegram_send(token, chat, message, deadline=deadline)
        except Exception as exc:
            if origin == "official":
                official_failed = True
            LOG.error("Помилка доставки (%s): %s", origin, type(exc).__name__)
            problems.append(origin)
            continue
        last_send = time.monotonic()
        if origin == "official":
            state["official:last_id"] = cursor
        state[key] = value
        state_write(path, state)
        sent += 1
        LOG.info("Надіслано: %s", key)

    LOG.info("Нових сповіщень: %d; помилок доставки: %d", sent, len(problems))
    if problems:
        raise RuntimeError(f"Помилки Telegram: {len(problems)}; успішні події збережено")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    single = "--once" in sys.argv
    delay = max(60, int(os.getenv("POLL_INTERVAL_SECONDS", "300")))
    while True:
        try:
            asyncio.run(once())
        except Exception as ex:
            LOG.error("Проверка не выполнена: %s", type(ex).__name__)
            LOG.error("%s", str(ex))
            if single:
                sys.exit(1)
        if single:
            return
        time.sleep(delay)


if __name__ == "__main__":
    main()
