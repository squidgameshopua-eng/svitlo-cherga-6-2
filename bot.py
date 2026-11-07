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


def message(day, group, confirmed, spans, unknown, updated):
    kind = "Подтверждённый график" if confirmed else "Предварительный недельный прогноз"
    lines = [
        "⚡️ <b>SVITLO — Черга 6.2</b>",
        f"📆 {day:%d.%m.%Y} — {kind}",
        f"Группа ДТЭК: <b>{html.escape(group.removeprefix('GPV'))}</b>",
    ]
    for mode, heading in (("off", "🔴 Нет света:"), ("maybe", "🟠 Возможно отключение:")):
        selected = [(a, b) for a, b, value in spans if value == mode]
        if selected:
            lines.extend(["", f"<b>{heading}</b>"])
            lines.extend(f"• {clock(a)}–{clock(b)}" for a, b in selected)
    if not spans and not unknown:
        lines.append("\nПо опубликованным интервалам отключений нет.")
    if unknown:
        lines.append("\n⚠️ Некоторые интервалы нельзя достоверно расшифровать.")
    if not confirmed:
        lines.append("\n⚠️ Это прогноз. ДТЭК может изменить время.")
    if updated:
        lines.append("Обновлено ДТЭК: " + html.escape(str(updated)))
    lines.append(f'\n<a href="{SOURCE}">Источник: ДТЭК</a>')
    return "\n".join(lines)


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
    if not source.preset or not source.preset.time_zone:
        raise RuntimeError("ДТЭК не вернул таблицу часовых интервалов.")
    now = datetime.now(KYIV)
    for day in (now.date(), now.date() + timedelta(days=1)):
        for group in groups:
            slots = None
            confirmed = False
            update = None
            if source.fact:
                for stamp, group_map in source.fact.days.items():
                    if datetime.fromtimestamp(int(stamp), KYIV).date() == day:
                        slots = group_map.get(group)
                        confirmed = bool(slots)
                        update = source.fact.update if confirmed else None
                        break
            if not slots and source.preset.is_active:
                plan = source.preset.groups.get(group)
                wd = plan.get_day(day.isoweekday()) if plan else None
                slots = wd.slots if wd else None
            if not slots:
                LOG.info("Нет графика на %s для %s", day, group)
                continue
            spans, unknown = intervals(slots, source.preset.time_zone)
            payload = (day.isoformat(), group, confirmed, spans, unknown)
            digest = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
            key = f"{day.isoformat()}:{group}"
            if state.get(key) == digest:
                LOG.info("Нет изменений: %s", key)
                continue
            telegram_send(token, chat, message(day, group, confirmed, spans, unknown, update))
            state[key] = digest
            state = {k: v for k, v in state.items()
                     if k[:10] >= (now.date() - timedelta(days=1)).isoformat()}
            state_write(path, state)
            LOG.info("Сообщение отправлено: %s", key)


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
