import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from universal.reminder_core import (
    choose_lead, connect, inline_choice_keyboard, lease_due, mark_failed,
    mark_sent, parse_choice, pause_subscription, publish_confirmed_starts,
    register, reminder_text, selected_leads, set_leads,
    list_accessible_subscriptions, update_chat_title, delete_subscription,
    migrate_chat_id,
)

K = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 11, 8, 9, 0, tzinfo=K)
DAY = NOW.date()


def register_one(db, chat="100", house="12", kind="private", actor="100", queue="6.2"):
    return register(
        db, chat_id=chat, chat_kind=kind, actor_id=actor,
        operator="dnem", oblast="Область", settlement="Місто",
        street="Вулиця", building=house, queue_code=queue, label="Дім",
    )


def test_first_address_setup_prompt_and_user_choice():
    db = connect()
    sub = register_one(db)
    assert db.execute(
        "SELECT lead_minutes FROM subscriptions WHERE id=?", (sub,)
    ).fetchone()[0] == 0
    kb = inline_choice_keyboard(sub)["inline_keyboard"]
    assert [item[0]["text"] for item in kb] == [
        "☐ За 5 хвилин", "☐ За 15 хвилин",
        "☐ За 30 хвилин", "☐ За 1 годину",
        "💾 Зберегти вибір", "🚫 Не нагадувати (за замовчуванням)",
    ]
    assert [i[0]["callback_data"] for i in kb] == [
        f"toggle:{sub}:5", f"toggle:{sub}:15",
        f"toggle:{sub}:30", f"toggle:{sub}:60",
        f"save:{sub}", f"off:{sub}",
    ]
    assert selected_leads(db, sub) == ()


def test_not_remind_disables_only_reminders_and_survives_restart(tmp_path):
    file = tmp_path / "secret.sqlite3"
    db = connect(file)
    sub = register_one(db)
    choose_lead(db, subscription_id=sub, chat_id="100",
                actor_is_authorized=True, minutes=0, now=NOW)
    start = NOW + timedelta(hours=1)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[start], now=NOW)
    assert lease_due(db, start - timedelta(minutes=5)) == []
    db.close()
    db = connect(file)
    assert db.execute("SELECT lead_minutes FROM subscriptions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0


@pytest.mark.parametrize("minutes", [5, 15, 30, 60])
def test_due_time_is_precise_for_each_preference(minutes):
    db = connect()
    sub = register_one(db)
    choose_lead(db, subscription_id=sub, chat_id="100",
                actor_is_authorized=True, minutes=minutes, now=NOW)
    start = NOW + timedelta(hours=2)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[start], now=NOW)
    assert lease_due(db, start - timedelta(minutes=minutes, seconds=1)) == []
    due = lease_due(db, start - timedelta(minutes=minutes))
    assert len(due) == 1
    assert due[0]["lead_minutes"] == minutes
    assert f"приблизно {minutes} хв" in reminder_text(
        due[0], start - timedelta(minutes=minutes)
    )


def test_two_addresses_two_chats_independent_preferences():
    db = connect()
    a = register_one(db, chat="100", house="12", actor="100")
    b = register_one(db, chat="200", house="13", actor="200")
    choose_lead(db, subscription_id=a, chat_id="100",
                actor_is_authorized=True, minutes=5, now=NOW)
    choose_lead(db, subscription_id=b, chat_id="200",
                actor_is_authorized=True, minutes=30, now=NOW)
    start = NOW + timedelta(hours=1)
    for location in (1, 2):
        publish_confirmed_starts(db, location_id=location, queue_code="6.2",
                                 day=DAY, starts=[start], now=NOW)
    jobs = lease_due(db, start - timedelta(minutes=30))
    assert len(jobs) == 1 and jobs[0]["chat_id"] == "200"
    mark_sent(db, jobs[0]["id"])
    jobs = lease_due(db, start - timedelta(minutes=5))
    assert len(jobs) == 1 and jobs[0]["chat_id"] == "100"


def test_many_chat_subscriptions_share_one_operator_schedule_update():
    db = connect()
    first = register_one(db, chat="100", house="12")
    second = register_one(db, chat="200", house="12", actor="200")
    for sub, chat, lead in ((first, "100", 5), (second, "200", 15)):
        choose_lead(db, subscription_id=sub, chat_id=chat,
                    actor_is_authorized=True, minutes=lead, now=NOW)
    assert db.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 1
    start = NOW + timedelta(hours=1)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[start], now=NOW)
    assert db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 2


def test_two_intervals_produce_distinct_reminders_once():
    db = connect()
    sub = register_one(db)
    choose_lead(db, subscription_id=sub, chat_id="100",
                actor_is_authorized=True, minutes=15, now=NOW)
    a = NOW + timedelta(hours=1)
    b = NOW + timedelta(hours=2)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[a, b], now=NOW)
    assert len(lease_due(db, a - timedelta(minutes=15))) == 1
    assert len(lease_due(db, b - timedelta(minutes=15))) == 1


def test_confirmed_cancellation_never_reminds():
    db = connect()
    sub = register_one(db)
    choose_lead(db, subscription_id=sub, chat_id="100",
                actor_is_authorized=True, minutes=15, now=NOW)
    a = NOW + timedelta(hours=1)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[a], now=NOW)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[], now=NOW+timedelta(minutes=5))
    assert lease_due(db, a - timedelta(minutes=15)) == []


def test_unconfirmed_schedule_cannot_cancel_reminders():
    db = connect()
    sub = register_one(db)
    choose_lead(db, subscription_id=sub, chat_id="100",
                actor_is_authorized=True, minutes=15, now=NOW)
    a = NOW + timedelta(hours=1)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[a], now=NOW)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[], now=NOW, verified=False)
    assert len(lease_due(db, a - timedelta(minutes=15))) == 1


def test_changed_start_cancels_old_and_schedules_new():
    db = connect()
    sub = register_one(db)
    choose_lead(db, subscription_id=sub, chat_id="100",
                actor_is_authorized=True, minutes=15, now=NOW)
    old = NOW + timedelta(hours=1)
    new = NOW + timedelta(hours=2)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[old], now=NOW)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[new], now=NOW)
    assert lease_due(db, old - timedelta(minutes=15)) == []
    due = lease_due(db, new - timedelta(minutes=15))
    assert len(due) == 1 and due[0]["start_at"] == int(new.timestamp())


def test_job_sent_only_once_and_late_retry():
    db = connect()
    sub = register_one(db)
    choose_lead(db, subscription_id=sub, chat_id="100",
                actor_is_authorized=True, minutes=5, now=NOW)
    start = NOW + timedelta(hours=1)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[start], now=NOW)
    due = start - timedelta(minutes=5)
    job = lease_due(db, due)[0]
    assert lease_due(db, due+timedelta(seconds=30)) == []
    mark_failed(db, job["id"], due)
    assert lease_due(db, due+timedelta(seconds=30)) == []
    job = lease_due(db, due+timedelta(seconds=46))[0]
    mark_sent(db, job["id"])
    assert lease_due(db, due+timedelta(seconds=90)) == []


def test_lease_recovers_after_worker_crash():
    db = connect()
    sub = register_one(db)
    choose_lead(db, subscription_id=sub, chat_id="100",
                actor_is_authorized=True, minutes=5, now=NOW)
    start = NOW + timedelta(hours=1)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[start], now=NOW)
    due = start - timedelta(minutes=5)
    assert len(lease_due(db, due)) == 1
    assert lease_due(db, due+timedelta(seconds=60)) == []
    assert len(lease_due(db, due+timedelta(seconds=76))) == 1


def test_wrong_admin_or_foreign_chat_cannot_modify_settings():
    db = connect()
    sub = register_one(db, chat="-100001", kind="supergroup", actor="100")
    with pytest.raises(PermissionError):
        choose_lead(db, subscription_id=sub, chat_id="-100001",
                    actor_is_authorized=False, minutes=60, now=NOW)
    with pytest.raises(PermissionError):
        choose_lead(db, subscription_id=sub, chat_id="-999",
                    actor_is_authorized=True, minutes=60, now=NOW)
    assert db.execute(
        "SELECT lead_minutes FROM subscriptions WHERE id=?", (sub,)
    ).fetchone()[0] == 0


def test_pause_cancels_pending_reminders():
    db = connect()
    sub = register_one(db)
    choose_lead(db, subscription_id=sub, chat_id="100",
                actor_is_authorized=True, minutes=5, now=NOW)
    start = NOW + timedelta(hours=1)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[start], now=NOW)
    pause_subscription(db, sub, chat_id="100", authorized=True)
    assert lease_due(db, start-timedelta(minutes=5)) == []


def test_concurrent_schedule_refresh_does_not_release_leased_reminder():
    db = connect()
    sub = register_one(db)
    choose_lead(db, subscription_id=sub, chat_id="100",
                actor_is_authorized=True, minutes=5, now=NOW)
    start = NOW + timedelta(hours=1)
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[start], now=NOW)
    due = start - timedelta(minutes=5)
    first = lease_due(db, due)
    assert len(first) == 1
    publish_confirmed_starts(db, location_id=1, queue_code="6.2",
                             day=DAY, starts=[start], now=due)
    assert lease_due(db, due + timedelta(seconds=10)) == []
    assert len(lease_due(db, due + timedelta(seconds=76))) == 1
