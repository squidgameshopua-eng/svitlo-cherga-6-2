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
            owner_user_id TEXT NOT NULL,
            chat_title TEXT NOT NULL DEFAULT ''
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
            lead_minutes INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            UNIQUE(destination_id, location_id, queue_code)
        );
        CREATE TABLE IF NOT EXISTS reminder_leads (
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            minutes INTEGER NOT NULL CHECK(minutes IN (5,15,30,60)),
            PRIMARY KEY(subscription_id, minutes)
        );
        CREATE TABLE IF NOT EXISTS confirmed_starts (
            location_id INTEGER NOT NULL REFERENCES locations(id),
            queue_code TEXT NOT NULL,
            local_day TEXT NOT NULL,
            start_at INTEGER NOT NULL,
            PRIMARY KEY (location_id, queue_code, local_day, start_at)
        );
        CREATE TABLE IF NOT EXISTS reminder_drafts (
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            actor_id TEXT NOT NULL,
            choices TEXT NOT NULL DEFAULT '[]',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(subscription_id, actor_id)
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY,
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
            local_day TEXT NOT NULL,
            start_at INTEGER NOT NULL,
            due_at INTEGER NOT NULL,
            lead_minutes INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            lease_until INTEGER,
            retry_after INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            UNIQUE(subscription_id, local_day, start_at, lead_minutes)
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


def update_chat_title(
    db: sqlite3.Connection, *, chat_id: str,
    title: str, actor_is_authorized: bool
) -> None:
    """Keep chat title for human-friendly dashboards, not as a chat identifier."""
    if not actor_is_authorized:
        raise PermissionError("Недостатньо прав")
    if not isinstance(title, str) or len(title) > 120:
        raise ValueError("Некоректна назва чату")
    db.execute("UPDATE destinations SET chat_title=? WHERE chat_id=?",
               (title.strip(), chat_id))


def list_accessible_subscriptions(
    db: sqlite3.Connection, *, actor_id: str,
    is_admin_of_destination,
    manageable_chat_ids: tuple[str, ...] = (),
    include_address: bool = True
):
    """Query ONLY chats supplied by the authenticated Telegram session.

    Group/channel permissions must be rechecked with getChatMember.
    This query scales with the user's chats, not every chat in Ukraine.
    """
    # This is a lookup hint, not proof of authorization.
    chat_ids = tuple(dict.fromkeys((actor_id, *manageable_chat_ids)))
    if len(chat_ids) > 100:
        raise ValueError("Забагато чатів для однієї перевірки")
    placeholders = ",".join("?" for _ in chat_ids)
    rows = db.execute(f"""
        SELECT s.id AS subscription_id, s.label, s.queue_code, s.status,
               s.lead_minutes, d.chat_id, d.chat_kind, d.chat_title,
               d.owner_user_id, l.settlement, l.street, l.building
        FROM destinations d
        JOIN subscriptions s ON s.destination_id=d.id
        JOIN locations l ON l.id=s.location_id
        WHERE d.chat_id IN ({placeholders})
        ORDER BY d.chat_kind, d.chat_title, s.label, s.id
    """, chat_ids).fetchall()
    allowed = []
    for row in rows:
        if row["chat_kind"] == "private":
            visible = row["owner_user_id"] == actor_id and row["chat_id"] == actor_id
        else:
            visible = bool(is_admin_of_destination(row["chat_id"], actor_id))
        if not visible:
            continue
        chat_caption = ("Особистий чат" if row["chat_kind"] == "private"
                        else row["chat_title"] or "Група або канал")
        result = {
            "subscription_id": row["subscription_id"],
            "destination_id": row["chat_id"],
            "chat_kind": row["chat_kind"],
            "chat_title": chat_caption,
            "label": row["label"],
            "queue": row["queue_code"],
            "status": row["status"],
            "reminder_minutes": selected_leads(db, row["subscription_id"]),
        }
        if include_address:
            result.update({
                "settlement": row["settlement"],
                "street": row["street"],
                "building": row["building"],
            })
        allowed.append(result)
    return allowed


def delete_subscription(
    db: sqlite3.Connection, *, subscription_id: int,
    chat_id: str, actor_is_authorized: bool
):
    """Delete address+queue membership and its private notification history."""
    if not actor_is_authorized:
        raise PermissionError("Адресу може видалити лише власник або адміністратор")
    db.execute("BEGIN IMMEDIATE")
    try:
        sub = subscription_for_chat(db, subscription_id, chat_id)
        if sub is None:
            raise PermissionError("Підписка не належить цьому чату")
        location_id, destination_id = sub["location_id"], sub["destination_id"]
        db.execute("DELETE FROM reminders WHERE subscription_id=?", (subscription_id,))
        db.execute("DELETE FROM reminder_drafts WHERE subscription_id=?", (subscription_id,))
        db.execute("DELETE FROM reminder_leads WHERE subscription_id=?", (subscription_id,))
        db.execute("DELETE FROM subscriptions WHERE id=?", (subscription_id,))
        remain = db.execute(
            "SELECT 1 FROM subscriptions WHERE location_id=? LIMIT 1",
            (location_id,)
        ).fetchone()
        if not remain:
            db.execute("DELETE FROM confirmed_starts WHERE location_id=?", (location_id,))
            db.execute("DELETE FROM locations WHERE id=?", (location_id,))
        remain = db.execute(
            "SELECT 1 FROM subscriptions WHERE destination_id=? LIMIT 1",
            (destination_id,)
        ).fetchone()
        if not remain:
            db.execute("DELETE FROM destinations WHERE id=?", (destination_id,))
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def migrate_chat_id(
    db: sqlite3.Connection, *, old_id: str, new_id: str,
    verified_migration: bool
):
    """Handle Telegram group→supergroup migration without losing preferences."""
    if not verified_migration:
        raise PermissionError("Потрібна перевірена подія Telegram")
    if not old_id or not new_id or old_id == new_id:
        raise ValueError("Неправильна міграція")
    db.execute("BEGIN IMMEDIATE")
    try:
        record = db.execute("SELECT id FROM destinations WHERE chat_id=?",
                            (old_id,)).fetchone()
        if record is None:
            db.execute("COMMIT")
            return False
        if db.execute("SELECT id FROM destinations WHERE chat_id=?",
                      (new_id,)).fetchone():
            raise RuntimeError("Новий чат уже має окремі підписки")
        db.execute("""
            UPDATE destinations SET chat_id=?,chat_kind='supergroup'
            WHERE id=?
        """, (new_id, record["id"]))
        db.execute("COMMIT")
        return True
    except Exception:
        db.execute("ROLLBACK")
        raise


def subscription_for_chat(db, subscription_id: int, chat_id: str):
    return db.execute("""
        SELECT s.*, d.chat_id, d.chat_kind
        FROM subscriptions s JOIN destinations d ON d.id=s.destination_id
        WHERE s.id=? AND d.chat_id=?
    """, (subscription_id, chat_id)).fetchone()


def selected_leads(db: sqlite3.Connection, subscription_id: int) -> tuple[int, ...]:
    return tuple(int(r[0]) for r in db.execute("""
        SELECT minutes FROM reminder_leads
        WHERE subscription_id=? ORDER BY minutes DESC
    """, (subscription_id,)).fetchall())


def set_leads(
    db: sqlite3.Connection, *, subscription_id: int, chat_id: str,
    actor_is_authorized: bool, minutes: tuple[int, ...] | list[int],
    now: datetime
) -> None:
    """Save optional checkbox choices, empty = OFF, and rebuild pending jobs."""
    if not isinstance(minutes, (tuple, list)):
        raise ValueError("Список нагадувань має бути списком")
    selected = tuple(sorted(set(minutes), reverse=True))
    if any(type(n) is not int or n not in VALID_LEADS or n == 0 for n in selected):
        raise ValueError("Час нагадування не підтримується")
    if not actor_is_authorized:
        raise PermissionError("Налаштування може змінювати лише адміністратор")
    now_ts = unix_utc(now)
    db.execute("BEGIN IMMEDIATE")
    try:
        sub = subscription_for_chat(db, subscription_id, chat_id)
        if sub is None:
            raise PermissionError("Підписка не належить цьому чату")
        # lead_minutes is a compatibility summary; reminder_leads is canonical.
        db.execute("UPDATE subscriptions SET lead_minutes=? WHERE id=?",
                   (max(selected) if selected else 0, subscription_id))
        db.execute("DELETE FROM reminder_leads WHERE subscription_id=?",
                   (subscription_id,))
        db.executemany("""
            INSERT INTO reminder_leads(subscription_id, minutes) VALUES (?,?)
        """, [(subscription_id, n) for n in selected])
        db.execute("""
            UPDATE reminders SET status='cancelled',lease_until=NULL
            WHERE subscription_id=? AND status IN ('pending','leased')
        """, (subscription_id,))
        if selected and sub["status"] == "active":
            starts = db.execute("""
                SELECT local_day,start_at FROM confirmed_starts
                WHERE location_id=? AND queue_code=? AND start_at>?
            """, (sub["location_id"], sub["queue_code"], now_ts)).fetchall()
            for item in starts:
                for minutes_before in selected:
                    _upsert_job(db, subscription_id, item["local_day"],
                                item["start_at"], minutes_before)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def choose_lead(
    db: sqlite3.Connection, *, subscription_id: int,
    chat_id: str, actor_is_authorized: bool, minutes: int,
    now: datetime
) -> None:
    """Backward-compatible choice of one reminder or none."""
    if type(minutes) is not int or minutes not in VALID_LEADS:
        raise ValueError("Час нагадування не підтримується")
    set_leads(db, subscription_id=subscription_id, chat_id=chat_id,
              actor_is_authorized=actor_is_authorized,
              minutes=() if minutes == 0 else (minutes,), now=now)


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
        INSERT INTO reminders(subscription_id,local_day,start_at,due_at,lead_minutes,status)
        VALUES (?,?,?,?,?,'pending')
        ON CONFLICT(subscription_id,local_day,start_at,lead_minutes)
        DO UPDATE SET due_at=excluded.due_at,
                      status=CASE WHEN reminders.status='sent' THEN 'sent'
                                  ELSE 'pending' END,
                      lease_until=NULL,retry_after=NULL
    """, (sub_id, local_day, start_at, due, minutes))


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
            SELECT id,status FROM subscriptions
            WHERE location_id=? AND queue_code=?
        """, (location_id, queue_code)).fetchall()
        for sub in subscribers:
            leads = selected_leads(db, sub["id"])
            jobs = db.execute("""
                SELECT id,start_at,lead_minutes,status FROM reminders
                WHERE subscription_id=? AND local_day=?
            """, (sub["id"], local_day)).fetchall()
            for job in jobs:
                if job["status"] in ("pending", "leased") and (
                    job["start_at"] not in new_starts
                    or job["lead_minutes"] not in leads
                    or sub["status"] != "active"
                ):
                    db.execute("""
                        UPDATE reminders SET status='cancelled',lease_until=NULL
                        WHERE id=?
                    """, (job["id"],))
            if leads and sub["status"] == "active":
                for start in sorted(new_starts):
                    if start > now_ts:
                        for minutes_before in leads:
                            _upsert_job(db, sub["id"], local_day, start,
                                        minutes_before)
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
                   s.label,r.lead_minutes,d.chat_id
            FROM reminders r
            JOIN subscriptions s ON s.id=r.subscription_id
            JOIN destinations d ON d.id=s.destination_id
            WHERE (r.status='pending'
                   OR (r.status='leased' AND r.lease_until<=?))
              AND (r.retry_after IS NULL OR r.retry_after<=?)
              AND r.due_at<=? AND r.start_at>?
              AND s.status='active' AND EXISTS (
                SELECT 1 FROM reminder_leads p
                WHERE p.subscription_id=s.id AND p.minutes=r.lead_minutes
              )
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


def inline_choice_keyboard(subscription_id: int, selected=()):
    """Four independent checkbox choices; none selected by default."""
    chosen = set(selected)
    if any(x not in (5, 15, 30, 60) for x in chosen):
        raise ValueError("Невідомий часовий інтервал")
    titles = ((5, "За 5 хвилин"), (15, "За 15 хвилин"),
              (30, "За 30 хвилин"), (60, "За 1 годину"))
    result = [
        [{"text": ("☑ " if n in chosen else "☐ ") + title,
          "callback_data": f"toggle:{subscription_id}:{n}"}]
        for n, title in titles
    ]
    result.append([{"text": "💾 Зберегти вибір",
                    "callback_data": f"save:{subscription_id}"}])
    result.append([{"text": "🚫 Не нагадувати (за замовчуванням)",
                    "callback_data": f"off:{subscription_id}"}])
    return {"inline_keyboard": result}


def parse_preference_action(data: str):
    """Return action, subscription ID, optional minute; no target ID from caller."""
    parts = data.split(":")
    if len(parts) == 3 and parts[0] == "toggle":
        action, subscription, minutes = parts
        try:
            sub_id, minute = int(subscription), int(minutes)
        except ValueError:
            raise ValueError("Некоректна кнопка") from None
        if sub_id <= 0 or minute not in (5, 15, 30, 60):
            raise ValueError("Некоректна кнопка")
        return action, sub_id, minute
    if len(parts) == 2 and parts[0] in ("save", "off"):
        try:
            sub_id = int(parts[1])
        except ValueError:
            raise ValueError("Некоректна кнопка") from None
        if sub_id <= 0:
            raise ValueError("Некоректна кнопка")
        return parts[0], sub_id, None
    if len(parts) == 3 and parts[0] == "lead":
        sub_id, minute = parse_choice(data)
        return "legacy", sub_id, minute
    raise ValueError("Некоректна кнопка")


def parse_choice(data: str):
    values = data.split(":")
    if len(values) != 3 or values[0] != "lead":
        raise ValueError("Некоректна кнопка")
    sub_id, lead = int(values[1]), int(values[2])
    if sub_id <= 0 or lead not in VALID_LEADS:
        raise ValueError("Некоректне налаштування")
    return sub_id, lead
