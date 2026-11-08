"""Experimental multi-recipient reminder engine. Not connected to production.

This module never stores addresses in source files or GitHub Actions artifacts.
Use a protected database volume. SQLite is only a single-worker prototype;
use a transactional hosted database and queue for nationwide deployments.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
VALID_LEADS = frozenset((0, 5, 15, 30, 60))
VALID_KINDS = frozenset(("private", "group", "supergroup", "channel"))


def unix_utc(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Потрібна дата з часовим поясом")
    return int(value.astimezone(timezone.utc).timestamp())


def connect(filename=":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(filename, timeout=20, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 20000")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS destinations (
            id INTEGER PRIMARY KEY,
            chat_id TEXT NOT NULL UNIQUE,
            chat_kind TEXT NOT NULL,
            owner_user_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY,
            operator TEXT NOT NULL,
            oblast TEXT NOT NULL,
            settlement TEXT NOT NULL,
            street TEXT NOT NULL,
            building TEXT NOT NULL,
            UNIQUE(operator, oblast, settlement, street, building)
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY,
            destination_id INTEGER NOT NULL REFERENCES destinations(id),
            location_id INTEGER NOT NULL REFERENCES locations(id),
            queue_code TEXT NOT NULL,
            label TEXT NOT NULL,
            lead_minutes INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            UNIQUE(destination_id, location_id, queue_code)
        );
        CREATE TABLE IF NOT EXISTS confirmed_starts (
            location_id INTEGER NOT NULL REFERENCES locations(id),
            queue_code TEXT NOT NULL,
            local_day TEXT NOT NULL,
            start_at INTEGER NOT NULL,
            PRIMARY KEY (location_id, queue_code, local_day, start_at)
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY,
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
            local_day TEXT NOT NULL,
            start_at INTEGER NOT NULL,
            due_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            lease_until INTEGER,
            retry_after INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            UNIQUE(subscription_id, local_day, start_at)
        );
        CREATE INDEX IF NOT EXISTS reminders_pending
            ON reminders(status, due_at, retry_after);
        CREATE INDEX IF NOT EXISTS subs_by_location
            ON subscriptions(location_id, queue_code, status);
    """)
    return db


def register(
    db: sqlite3.Connection, *,
    chat_id: str, chat_kind: str, actor_id: str,
    operator: str, oblast: str, settlement: str, street: str,
    building: str, queue_code: str, label: str = "Адреса"
) -> int:
    """Register a chat+address+queue; lead remains NULL until wizard selection.

    Caller MUST check group/channel admin permission via Telegram getChatMember.
    """
    if chat_kind not in VALID_KINDS:
        raise ValueError("Невідомий тип Telegram-чату")
    fields = (chat_id, actor_id, operator, oblast, settlement,
              street, building, queue_code, label)
    if any(not isinstance(x, str) or not x.strip() or len(x) > 160 for x in fields):
        raise ValueError("Некоректні поля адреси або підписки")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""
            INSERT INTO destinations(chat_id,chat_kind,owner_user_id)
            VALUES (?,?,?) ON CONFLICT(chat_id) DO NOTHING
        """, (chat_id, chat_kind, actor_id))
        dest = db.execute(
            "SELECT id, chat_kind FROM destinations WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if dest["chat_kind"] != chat_kind:
            raise ValueError("Тип Telegram-чату змінився")
        db.execute("""
            INSERT INTO locations(operator,oblast,settlement,street,building)
            VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING
        """, (operator, oblast, settlement, street, building))
        loc = db.execute("""
            SELECT id FROM locations WHERE operator=? AND oblast=?
                AND settlement=? AND street=? AND building=?
        """, (operator, oblast, settlement, street, building)).fetchone()
        db.execute("""
            INSERT INTO subscriptions(destination_id,location_id,queue_code,label)
            VALUES (?,?,?,?)
            ON CONFLICT(destination_id,location_id,queue_code)
            DO UPDATE SET label=excluded.label
        """, (dest["id"], loc["id"], queue_code, label))
        sub = db.execute("""
            SELECT id FROM subscriptions
            WHERE destination_id=? AND location_id=? AND queue_code=?
        """, (dest["id"], loc["id"], queue_code)).fetchone()
        db.execute("COMMIT")
        return int(sub["id"])
    except Exception:
        db.execute("ROLLBACK")
        raise


def subscription_for_chat(db, subscription_id: int, chat_id: str):
    return db.execute("""
        SELECT s.*, d.chat_id, d.chat_kind
        FROM subscriptions s JOIN destinations d ON d.id=s.destination_id
        WHERE s.id=? AND d.chat_id=?
    """, (subscription_id, chat_id)).fetchone()


def choose_lead(
    db: sqlite3.Connection, *, subscription_id: int,
    chat_id: str, actor_is_authorized: bool, minutes: int,
    now: datetime
) -> None:
    """Apply an inline-button choice only after external authorization.

    Rebuild queued reminders from persisted confirmed daily starts.
    """
    if minutes not in VALID_LEADS:
        raise ValueError("Час нагадування не підтримується")
    if not actor_is_authorized:
        raise PermissionError("Налаштування може змінювати лише адміністратор")
    now_ts = unix_utc(now)
    db.execute("BEGIN IMMEDIATE")
    try:
        sub = subscription_for_chat(db, subscription_id, chat_id)
        if sub is None:
            raise PermissionError("Підписка не належить цьому чату")
        db.execute(
            "UPDATE subscriptions SET lead_minutes=? WHERE id=?",
            (minutes, subscription_id),
        )
        db.execute("""
            UPDATE reminders SET status='cancelled',lease_until=NULL
            WHERE subscription_id=? AND status IN ('pending','leased')
        """, (subscription_id,))
        if minutes and sub["status"] == "active":
            starts = db.execute("""
                SELECT local_day,start_at FROM confirmed_starts
                WHERE location_id=? AND queue_code=? AND start_at>?
            """, (sub["location_id"], sub["queue_code"], now_ts)).fetchall()
            for item in starts:
                _upsert_job(db, subscription_id, item["local_day"],
                            item["start_at"], minutes)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def pause_subscription(db, subscription_id: int, chat_id: str, authorized: bool):
    if not authorized:
        raise PermissionError("Потрібні права адміністратора")
    db.execute("BEGIN IMMEDIATE")
    try:
        sub = subscription_for_chat(db, subscription_id, chat_id)
        if sub is None:
            raise PermissionError("Невідома підписка")
        db.execute("UPDATE subscriptions SET status='paused' WHERE id=?",
                   (subscription_id,))
        db.execute("""
            UPDATE reminders SET status='cancelled',lease_until=NULL
            WHERE subscription_id=? AND status IN ('pending','leased')
        """, (subscription_id,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def _upsert_job(db, sub_id, local_day, start_at, minutes):
    due = start_at - 60 * minutes
    db.execute("""
        INSERT INTO reminders(subscription_id,local_day,start_at,due_at,status)
        VALUES (?,?,?,?,'pending')
        ON CONFLICT(subscription_id,local_day,start_at)
        DO UPDATE SET due_at=excluded.due_at,
                      status=CASE WHEN reminders.status='sent' THEN 'sent'
                                  ELSE 'pending' END,
                      lease_until=NULL,retry_after=NULL
    """, (sub_id, local_day, start_at, due))


def publish_confirmed_starts(
    db: sqlite3.Connection, *, location_id: int, queue_code: str,
    day: date, starts: list[datetime], now: datetime,
    verified: bool = True
) -> None:
    """Refresh one confirmed GPV day and cancel reminders for removed outages.

    Never call for tentative weekly templates or missing/incomplete DTEK data.
    An explicit verified empty list cancels previously queued reminders.
    """
    if not verified:
        return
    now_ts = unix_utc(now)
    local_day = day.isoformat()
    new_starts = set()
    for start in starts:
        if start.astimezone(KYIV).date() != day:
            raise ValueError("Початок відключення належить іншій даті")
        new_starts.add(unix_utc(start))
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""
            DELETE FROM confirmed_starts
            WHERE location_id=? AND queue_code=? AND local_day=?
        """, (location_id, queue_code, local_day))
        for start in sorted(new_starts):
            db.execute("""
                INSERT INTO confirmed_starts(location_id,queue_code,local_day,start_at)
                VALUES (?,?,?,?)
            """, (location_id, queue_code, local_day, start))
        subscribers = db.execute("""
            SELECT id,lead_minutes,status FROM subscriptions
            WHERE location_id=? AND queue_code=?
        """, (location_id, queue_code)).fetchall()
        for sub in subscribers:
            jobs = db.execute("""
                SELECT id,start_at,status FROM reminders
                WHERE subscription_id=? AND local_day=?
            """, (sub["id"], local_day)).fetchall()
            for job in jobs:
                if job["status"] in ("pending", "leased") and (
                    job["start_at"] not in new_starts
                    or not sub["lead_minutes"]
                    or sub["status"] != "active"
                ):
                    db.execute("""
                        UPDATE reminders SET status='cancelled',lease_until=NULL
                        WHERE id=?
                    """, (job["id"],))
            if sub["lead_minutes"] and sub["status"] == "active":
                for start in sorted(new_starts):
                    if start > now_ts:
                        _upsert_job(db, sub["id"], local_day, start,
                                    sub["lead_minutes"])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def lease_due(db: sqlite3.Connection, now: datetime, batch: int = 20):
    """Reserve due jobs atomically. Expired leases are retryable after restart."""
    ts = unix_utc(now)
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""
            UPDATE reminders SET status='cancelled'
            WHERE status IN ('pending','leased') AND start_at<=?
        """, (ts,))
        jobs = db.execute("""
            SELECT r.id,r.start_at,r.due_at,r.subscription_id,
                   s.label,s.lead_minutes,d.chat_id
            FROM reminders r
            JOIN subscriptions s ON s.id=r.subscription_id
            JOIN destinations d ON d.id=s.destination_id
            WHERE (r.status='pending'
                   OR (r.status='leased' AND r.lease_until<=?))
              AND (r.retry_after IS NULL OR r.retry_after<=?)
              AND r.due_at<=? AND r.start_at>?
              AND s.status='active' AND s.lead_minutes>0
            ORDER BY r.due_at,r.id LIMIT ?
        """, (ts, ts, ts, ts, batch)).fetchall()
        for job in jobs:
            db.execute("""
                UPDATE reminders SET status='leased',lease_until=?,attempts=attempts+1
                WHERE id=?
            """, (ts + 75, job["id"]))
        db.execute("COMMIT")
        return [dict(j) for j in jobs]
    except Exception:
        db.execute("ROLLBACK")
        raise


def mark_sent(db: sqlite3.Connection, job_id: int):
    db.execute("UPDATE reminders SET status='sent',lease_until=NULL WHERE id=? AND status='leased'",
               (job_id,))


def mark_failed(db: sqlite3.Connection, job_id: int, now: datetime, retry_seconds=45):
    db.execute("""
        UPDATE reminders SET status='pending',lease_until=NULL,retry_after=?
        WHERE id=? AND status='leased'
    """, (unix_utc(now) + retry_seconds, job_id))


def reminder_text(job, now: datetime) -> str:
    """Tell the real time remaining, not an inaccurate fixed interval."""
    until = max(1, (job["start_at"] - unix_utc(now) + 59) // 60)
    start = datetime.fromtimestamp(job["start_at"], KYIV).strftime("%H:%M")
    return (
        "⏰ Нагадування про планове відключення\n"
        f"📍 {job['label']}\n"
        f"Початок: {start}\n"
        f"До відключення приблизно {until} хв.\n"
        "Час може змінитися. Перевіряйте нові повідомлення про графік."
    )


def inline_choice_keyboard(subscription_id: int):
    """Callback IDs are short and scoped to an existing subscription."""
    options = (
        ("Не нагадувати", 0), ("За 5 хвилин", 5),
        ("За 15 хвилин", 15), ("За 30 хвилин", 30),
        ("За 1 годину", 60),
    )
    return {"inline_keyboard": [
        [{"text": label,
          "callback_data": f"lead:{subscription_id}:{minutes}"}]
        for label, minutes in options
    ]}


def parse_choice(data: str):
    values = data.split(":")
    if len(values) != 3 or values[0] != "lead":
        raise ValueError("Некоректна кнопка")
    sub_id, lead = int(values[1]), int(values[2])
    if sub_id <= 0 or lead not in VALID_LEADS:
        raise ValueError("Некоректне налаштування")
    return sub_id, lead
