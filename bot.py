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


def message(day, group, confirmed, spans, unknown, updated, changed=False):
    """A confirmed daily schedule with actual outages. Never announce missing data."""
    if not confirmed or unknown or not spans:
        return None
    lines = [
        "🔄 <b>Изменение суточного графика</b>" if changed else
        "⚡️ <b>Суточный график</b>",
        f"📆 {day:%d.%m.%Y} — черга {html.escape(group.removeprefix('GPV'))}",
    ]
    for mode, heading in (("off", "🔴 Плановые отключения:"),
                          ("maybe", "🟠 Возможные отключения:")):
        selected = [(a, b) for a, b, value in spans if value == mode]
        if selected:
            lines.extend(["", f"<b>{heading}</b>"])
            lines.extend(f"• {clock(a)}–{clock(b)}" for a, b in selected)
    if updated:
        lines.append("\nОбновлено ДТЭК: " + html.escape(str(updated)))
    lines.append(f'\n<a href="{SOURCE}">Источник: ДТЭК</a>')
    return "\n".join(lines)


def weekly_message(week_start, group, entries, changed=False):
    """Clearly label separate seven-day forecast as tentative."""
    if not entries:
        return None
    week_end = week_start + timedelta(days=6)
    lines = [
        "🔄 <b>Обновление недельного прогноза</b>" if changed else
        "📅 <b>Недельный прогноз возможных отключений</b>",
        f"Черга {html.escape(group.removeprefix('GPV'))} · "
        f"{week_start:%d.%m}–{week_end:%d.%m.%Y}",
    ]
    for day, spans in entries:
        lines.append(f"\n<b>{day:%a %d.%m}</b>")
        lines.extend(f"• {clock(a)}–{clock(b)}" for a, b in spans)
    lines.append("\n⚠️ Это только предварительный прогноз, не суточный график.")
    lines.append(f'<a href="{SOURCE}">Источник: ДТЭК</a>')
    return "\n".join(lines)


def collect_notifications(source, group, today):
    """Create zero or more items to publish; never create no-data notices."""
    labels = source.preset.time_zone if source.preset else None
    if not labels:
        LOG.info("ДТЭК не предоставил временные интервалы; публикаций нет")
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
                if not slots:
                    continue
                try:
                    spans, unknown = intervals(slots, labels)
                except (ValueError, RuntimeError):
                    continue
                if not unknown and spans:
                    active_region = True

    out = []
    for day in (today, today + timedelta(days=1)):
        slots = actual.get(day, {}).get(group)
        if not slots:
            continue
        try:
            spans, unknown = intervals(slots, labels)
        except (ValueError, RuntimeError) as ex:
            LOG.warning("Некорректный суточный график %s: %s", day, type(ex).__name__)
            continue
        # A table made entirely of white cells means no planned outage.
        if unknown or not spans:
            continue
        key = f"day:{day.isoformat()}:{group}"
        payload = (day.isoformat(), group, True, spans, unknown)
        out.append((key, payload, "day", (day, spans, source.fact.update)))

    # The preset table can exist even when no restrictions apply (e.g. in summer).
    # Require a current regional daily outage before broadcasting a weekly forecast.
    if active_region and source.preset.is_active:
        monday = today - timedelta(days=today.weekday())
        plan = source.preset.groups.get(group)
        entries = []
        valid = True
        for weekday in range(1, 8):
            dayplan = plan.get_day(weekday) if plan else None
            if not dayplan or not dayplan.slots:
                continue
            try:
                spans, unknown = intervals(dayplan.slots, labels)
            except (ValueError, RuntimeError):
                valid = False
                break
            if unknown:
                valid = False
                break
            possible = tuple((a, b) for a, b, mode in spans)
            if possible:
                entries.append((monday + timedelta(days=weekday - 1), possible))
        if valid and entries:
            payload = (monday.isoformat(), group,
                       tuple((d.isoformat(), spans) for d, spans in entries))
            out.append((f"week:{monday.isoformat()}:{group}", payload,
                        "week", (monday, entries)))
        elif not valid:
            LOG.warning("Недельная таблица ДТЭК не распознана; публикаций нет")
    else:
        LOG.info("Недельный прогноз не объявляется: нет актуальных региональных отключений")
    return out


def state_read(path):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def state_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    temp.replace(path)


def telegram_send(token, chat, text):
    try:
        result = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"},
            timeout=25,
        )
    except requests.RequestException:
        raise RuntimeError("Ошибка подключения к Telegram; токен не записан в журнал.") from None
    if result.status_code != 200 or not result.json().get("ok"):
        raise RuntimeError(f"Telegram отклонил сообщение: HTTP {result.status_code}")


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
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise RuntimeError("Нужно указать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в Secrets.")
    path = Path(os.getenv("STATE_PATH", "data/state.json"))
    state = state_read(path)
    source, groups = await get_schedule()
    today = datetime.now(KYIV).date()

    for group in groups:
        for key, payload, kind, parts in collect_notifications(source, group, today):
            digest = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
            if state.get(key) == digest:
                LOG.info("Нет изменений: %s", key)
                continue
            changed = bool(state.get(key))
            if kind == "day":
                day, spans, updated = parts
                text = message(day, group, True, spans, 0, updated, changed=changed)
            else:
                monday, entries = parts
                text = weekly_message(monday, group, entries, changed=changed)
            if not text:
                continue
            telegram_send(token, chat, text)
            state[key] = digest
            state_write(path, state)
            LOG.info("Опубликован %s: %s", "обновлённый" if changed else "новый", key)


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
