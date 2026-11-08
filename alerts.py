"""Official DTEK event notices. No address, token or private chat IDs are stored here.

The public DTEK announcement stream is only a REGIONAL indicator.
House-specific outages require an optional encrypted DTEK_ADDRESS_JSON secret.
"""
import hashlib
import html
import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger("dtek-alerts")
KYIV = ZoneInfo("Europe/Kyiv")
OFFICIAL_URL = "https://t.me/s/dtek_ua"
DTEK_AJAX = "https://www.dtek-dnem.com.ua/ua/ajax"
DTEK_SOURCE = "https://www.dtek-dnem.com.ua/ua/shutdowns"


def official_posts(page):
    soup = BeautifulSoup(page, "html.parser")
    entries = []
    for post in soup.select(".tgme_widget_message[data-post]"):
        identity = post.get("data-post", "")
        match = re.fullmatch(r"dtek_ua/(\d+)", identity)
        if not match:
            continue
        content = post.select_one(".tgme_widget_message_text")
        clock = post.select_one("time[datetime]")
        if content is None or clock is None:
            continue
        try:
            stamp = datetime.fromisoformat(clock["datetime"])
        except (ValueError, KeyError):
            continue
        if stamp.tzinfo is None:
            continue
        entries.append({
            "id": int(match.group(1)),
            "date": stamp.astimezone(KYIV),
            "text": content.get_text("\n", strip=True),
            "url": f"https://t.me/dtek_ua/{match.group(1)}",
        })
    return sorted(entries, key=lambda p: p["id"])


def regional_event(post):
    """Strictly classify explicit region-wide or Kryvyi Rih headline events.

    Dnipro CITY reports and news about general attacks are not local events.
    """
    lines = post["text"].strip().splitlines()
    if not lines:
        return None
    title = re.sub(r"^[^А-Яа-яІіЇїЄєҐґ]+", "", lines[0]).strip().casefold()
    target = re.match(
        r"^(дніпропетровщина|дніпропетровська область|кривий ріг)\s*:",
        title,
    )
    if not target:
        return None
    area = target.group(1)
    if re.search(r"\b(гав|графік(?:и|ів)?\s+аварійн\w*\s+відключен\w*)\b", title):
        kind = "gav"
    elif "екстрен" in title and "відключен" in title:
        kind = "emergency"
    else:
        return None
    if re.search(r"скасован|припинен|завершен|відмінено", title):
        status = "ended"
    elif re.search(r"запроваджен|застосован|введен|діють|тривають|продовжен", title + " " + post["text"].casefold()):
        status = "active"
    else:
        # Ambiguous headlines must never generate residents' alerts.
        return None
    return {"kind": kind, "status": status,
            "area": "Кривий Ріг" if area == "кривий ріг" else "Дніпропетровська область",
            "post": post}


def regional_message(event):
    kind = event["kind"]
    active = event["status"] == "active"
    area = event["area"]
    if kind == "emergency":
        title = ("‼️ <b>Запроваджено екстрені відключення</b>" if active else
                 "✅ <b>Екстрені відключення скасовано</b>")
        body = ("Під час екстрених відключень погодинний графік може не діяти." if active
                else "Скасовано саме екстрений режим. Актуальний погодинний графік перевіряється окремо.")
    else:
        title = ("‼️ <b>Оголошено графіки аварійних відключень</b>" if active else
                 "✅ <b>Графіки аварійних відключень скасовано</b>")
        body = ("Погодинний графік може бути неактуальним." if active else
                "Оновлення стосується аварійного режиму, а не гарантії наявності світла.")
    return "\n".join((
        title, f"📍 {html.escape(area)}", "",
        body, "Це повідомлення про режим у регіоні, а не підтвердження відключення конкретного будинку.",
        f'<a href="{html.escape(event["post"]["url"], quote=True)}">Офіційне повідомлення ДТЕК</a>',
    ))


def region_candidates(page, state, now):
    posts = official_posts(page)
    if not posts:
        raise RuntimeError("Не вдалося прочитати офіційний канал ДТЕК")
    old_id = state.get("official:last_id")
    max_id = max(p["id"] for p in posts)
    if old_id is None:
        # Never flood the group with a backlog on first deployment.
        state["official:last_id"] = max_id
        LOG.info("Початкова синхронізація офіційного каналу: %d", max_id)
        return []
    try:
        old_id = int(old_id)
    except (ValueError, TypeError):
        raise RuntimeError("Некоректний стан останнього офіційного повідомлення")
    pending = []
    simulated = {}
    for post in posts:
        if post["id"] <= old_id:
            continue
        event = regional_event(post)
        if not event:
            continue
        if post["date"] > now + timedelta(minutes=10):
            continue
        if now - post["date"] > timedelta(hours=6):
            continue
        # Track transitions within a batch, not just the saved initial state.
        key = f"official:status:{event['kind']}"
        previous = simulated.get(key, state.get(key, "unknown"))
        if event["status"] == "active":
            if previous != "active":
                pending.append((post["id"], key, "active", regional_message(event)))
                simulated[key] = "active"
        elif previous == "active":
            pending.append((post["id"], key, "ended", regional_message(event)))
            simulated[key] = "ended"
    # A final checkpoint never triggers an outgoing message.
    # The caller saves it only after prior sends succeed.
    pending.append((max(old_id, max_id), "official:last_id",
                    max(old_id, max_id), None))
    return pending


def fetch_official_page():
    response = requests.get(
        OFFICIAL_URL, timeout=22,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SVITLO/1.0)"},
    )
    response.raise_for_status()
    return response.text


def normalized_house(value):
    text = re.sub(r"[\s/\\.-]", "", str(value)).casefold()
    return text.replace("a", "а").replace("b", "в")


def address_fields(secret):
    """Validate private JSON; never put its value into logs."""
    if not secret:
        return None
    try:
        raw = json.loads(secret)
    except (ValueError, TypeError):
        raise RuntimeError("Некоректний формат DTEK_ADDRESS_JSON")
    if not isinstance(raw, dict):
        raise RuntimeError("DTEK_ADDRESS_JSON повинен містити об'єкт")
    fields = [raw.get(k) for k in ("city", "street", "house")]
    if any(not isinstance(v, str) or not v.strip() or len(v) > 160 for v in fields):
        raise RuntimeError("DTEK_ADDRESS_JSON: обов'язкові поля city/street/house")
    return fields


def get_house_entry(response, house):
    matches = [(key, e) for key, e in response.houses.items()
               if normalized_house(key) == normalized_house(house)]
    if len(matches) != 1:
        raise RuntimeError("Будинок не знайдено однозначно в ДТЕК")
    entry = matches[0][1]
    # An empty record can mean none; but only in a successfully parsed response.
    return entry


async def fetch_address_status(secret):
    """Use private address only for DTEK address lookup, never regional schedule."""
    from curl_cffi.requests import AsyncSession
    from dtek_client import DtekClient
    from dtek_client.browser_auth import get_cleared_cookies

    city, street, house = address_fields(secret)

    async def read(client):
        response = await client.get_home_num(city, street)
        return get_house_entry(response, house)

    try:
        async with DtekClient("dnem", ajax_url=DTEK_AJAX) as client:
            return await read(client)
    except Exception as ex:
        LOG.warning("Прямий адресний запит відхилено (%s). Пробую браузер.",
                    type(ex).__name__)
        cookies, csrf = await get_cleared_cookies(DTEK_SOURCE)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Origin": "https://www.dtek-dnem.com.ua",
            "Referer": DTEK_SOURCE,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }
        if csrf:
            headers["X-CSRF-Token"] = csrf
        session = AsyncSession(timeout=20, cookies=cookies, headers=headers,
                               impersonate="chrome120")
        async with DtekClient("dnem", ajax_url=DTEK_AJAX, session=session) as client:
            return await read(client)


def parse_dtek_date(raw):
    if not raw:
        return None
    for pattern in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(raw), pattern).replace(tzinfo=KYIV)
        except ValueError:
            pass
    return None


def address_event(entry):
    reason = str(entry.sub_type or "").strip()
    beginning = str(entry.start_date or "").strip()
    ending = str(entry.end_date or "").strip()
    outage_type = str(entry.outage_type or "")
    if not (reason or beginning or ending):
        return None
    if outage_type == "1" or "планов" in reason.casefold() or "ремонт" in reason.casefold():
        category = "repair"
    elif "авар" in reason.casefold() or "пошкодж" in reason.casefold():
        category = "fault"
    else:
        category = "unclassified"
    return {
        "category": category,
        "reason": reason,
        "start": beginning,
        "end": ending,
    }


def address_message(event, changed=False):
    titles = {
        "repair": "🛠 <b>Планові ремонтні роботи</b>",
        "fault": "‼️ <b>Аварійне відключення</b>",
        "unclassified": "⚠️ <b>Повідомлення про відключення</b>",
    }
    title = ("🔄 <b>Оновлено дані щодо відключення</b>" if changed
             else titles[event["category"]])
    lines = [title]
    if event["start"]:
        lines.append("Початок: " + html.escape(event["start"]))
    if event["end"]:
        lines.append("Орієнтовне завершення: " + html.escape(event["end"]))
    # Keep the original address and free-form operational details private.
    if event["category"] == "repair":
        lines.append("Причина: планові роботи")
    elif event["category"] == "fault":
        lines.append("Причина: аварійне відключення")
    lines.append("Час відновлення може змінитися.")
    lines.append(f'<a href="{DTEK_SOURCE}">Джерело: ДТЕК</a>')
    return "\n".join(lines)


def address_clear_message(old, now):
    # Store only the end time publicly. Never persist the private reason
    # or the original address response in bot_state.json.
    end = parse_dtek_date(old.get("end"))
    if end is None or now >= end:
        return None  # Normal disappearance after predicted end: no extra noise.
    return "\n".join((
        "✅ <b>Інформацію про позаграфікове відключення знято</b>",
        "Раніше оголошене відключення більше не відображається за адресою.",
        "Це не підтверджує фактичну наявність світла.",
        f'<a href="{DTEK_SOURCE}">Джерело: ДТЕК</a>',
    ))


def address_decision(entry, state, now, expected="GPV6.2"):
    """One state transition per meaningful verified address change."""
    result = []
    if not entry:
        return result
    key = "address:last_outage"
    old = state.get(key)
    event = address_event(entry)
    if event:
        # Ignore stale past incidents that no longer affect today's plans.
        finish = parse_dtek_date(event["end"])
        if finish and finish < now and not isinstance(old, dict):
            event = None
    if event:
        digest = hashlib.sha256(json.dumps(
            event, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        if not isinstance(old, dict) or old.get("digest") != digest:
            result.append(("address", key, {
                "digest": digest,
                "end": event["end"],
                "status": "active",
            }, address_message(event, changed=isinstance(old, dict) and
                               old.get("status") == "active")))
    elif isinstance(old, dict) and old.get("status") == "active":
        msg = address_clear_message({"end": old.get("end") or (old.get("event") or {}).get("end", "")}, now)
        result.append(("address", key, {"status": "inactive"}, msg))
    # Group changes need human attention. Do not broadcast initial group assignment.
    previous = state.get("address:groups")
    current = sorted(set(entry.group_ids))
    if (previous is not None and previous != current) or (
        previous is None and current and expected not in current
    ):
        label = ", ".join(x.removeprefix("GPV") for x in current)
        text = (
            "⚠️ <b>ДТЕК змінив чергу за адресою</b>\n"
            f"Наразі вказані черги: {html.escape(label or 'не визначено')}.\n"
            "Перевірте актуальність автоматичних повідомлень за чергою 6.2.\n"
            f'<a href="{DTEK_SOURCE}">Джерело: ДТЕК</a>'
        )
        result.append(("address", "address:groups", current, text))
    if previous is None:
        result.append(("address", "address:groups", current, None))
    return result
