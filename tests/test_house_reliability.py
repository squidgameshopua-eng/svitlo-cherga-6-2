"""Only one destination; alert delivery must remain timely or be suppressed."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import Mock
import pytest

from bot import parse_verified, telegram_send
from house_reliability import (
    parse_reminder_minutes, due_reminders, queue_hold, trim_state,
)

KYIV = ZoneInfo("Europe/Kyiv")
DAY = date(2026, 11, 12)
GROUP = "GPV6.2"
LABELS = {"1": "17-18", "2": "18-19", "3": "19-20"}


def provider(day=DAY, active=True, possible=False, missing=False):
    dt = datetime(day.year, day.month, day.day, tzinfo=KYIV)
    slots = {"1": "yes", "2": ("maybe" if possible else "no") if active else "yes",
             "3": "yes"}
    if missing:
        slots.pop("3")
    fact = SimpleNamespace(days={str(int(dt.timestamp())): {GROUP: slots}})
    preset = SimpleNamespace(time_zone=LABELS)
    return SimpleNamespace(preset=preset, fact=fact)


def on(hour, minute, second=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=KYIV)


def test_opt_in_default_off_and_multiple_choices():
    for value in ("", "0", None, "   "):
        assert parse_reminder_minutes(value) == ()
    assert parse_reminder_minutes("5") == (5,)
    assert parse_reminder_minutes(" 5,15,5,60 ") == (60, 15, 5)
    for value in ("1", "10", "70", "0,5", "five", "5,", "5;15", "-5"):
        with pytest.raises(ValueError):
            parse_reminder_minutes(value)


def test_one_minute_grace_and_skip_late_reminder():
    src = provider()
    assert due_reminders(src, GROUP, on(17,54,59), (5,), {}, parse_verified) == []
    items = due_reminders(src, GROUP, on(17,55,30), (5,), {}, parse_verified)
    assert len(items) == 1
    key, message, deadline = items[0]
    assert key == "reminder:2026-11-12:GPV6.2:1080:5"
    assert "Нагадування" in message
    assert "17:55" not in message and "18:00" in message
    assert deadline == on(17,56)
    assert due_reminders(src, GROUP, on(17,56), (5,), {}, parse_verified) == []
    assert due_reminders(src, GROUP, on(18,1), (5,), {}, parse_verified) == []


def test_no_repeat_after_a_successful_notice_and_multiple_leads():
    src = provider()
    earlier = due_reminders(src, GROUP, on(17,45,20), (15,5), {}, parse_verified)
    assert len(earlier) == 1
    state = {earlier[0][0]: {"sent": True}}
    assert due_reminders(src, GROUP, on(17,45,40), (15,5), state, parse_verified) == []
    later = due_reminders(src, GROUP, on(17,55,10), (15,5), state, parse_verified)
    assert len(later) == 1
    assert later[0][0].endswith(":5")


def test_no_reminder_on_missing_possible_or_cancelled_schedule():
    for src in (provider(active=False),provider(possible=True),provider(missing=True)):
        assert due_reminders(src, GROUP, on(17,55), (5,), {}, parse_verified) == []


def test_no_reminder_from_tentative_weekly_forecast():
    src = provider()
    src.fact = None
    assert due_reminders(src, GROUP, on(17,55), (5,), {}, parse_verified) == []


def test_tomorrow_midnight_reminder_can_be_due_today():
    day = DAY + timedelta(days=1)
    s = provider(day=day)
    s.preset.time_zone = {"1":"00-01","2":"01-02","3":"02-03"}
    stamp = next(iter(s.fact.days))
    s.fact.days[stamp][GROUP] = {"1":"no","2":"yes","3":"yes"}
    items = due_reminders(s,GROUP,on(23,55,10),(5,),{},parse_verified)
    assert len(items) == 1
    assert "00:00" in items[0][1]


def test_queue_mismatch_pauses_until_explicit_one_time_acknowledgment():
    state = {}
    paused, message = queue_hold(
        state,verified_groups=["GPV5.1"],selected_group=GROUP,new_code="1234abcd")
    assert paused and "1234abcd" in message
    assert state["house:queue_hold"]["groups"] == ["GPV5.1"]
    state["house:queue_hold"]["notified"] = True
    paused, message = queue_hold(
        state,verified_groups=["GPV5.1"],selected_group=GROUP)
    assert paused and message is None
    paused, message = queue_hold(
        state,verified_groups=["GPV5.1"],selected_group="GPV5.1",
        approval="")
    assert paused and message is None
    paused, message = queue_hold(
        state,verified_groups=["GPV5.1"],selected_group="GPV5.1",
        approval="1234abcd")
    assert not paused and message is None
    assert "house:queue_hold" not in state


def test_queue_cannot_resume_on_foreign_ack_or_missing_confirmation():
    state={}
    queue_hold(state,verified_groups=["GPV4.1"],selected_group=GROUP,new_code="code0001")
    state["house:queue_hold"]["notified"]=True
    assert queue_hold(state,verified_groups=["GPV4.1"],
                      selected_group="GPV4.1",approval="code0002")[0]
    assert queue_hold(state,verified_groups=["GPV4.1"],
                      selected_group=GROUP,approval="code0001")[0]
    assert queue_hold(state,verified_groups=[],selected_group=GROUP)[0]


def test_never_send_home_address_in_queue_change_alert():
    state={}
    _,text = queue_hold(state,verified_groups=["GPV5.1"],selected_group=GROUP,new_code="abc12345")
    assert "Кропивницького" not in text
    assert "67" not in text
    assert "5.1" in text


def test_old_state_pruned_without_touching_current_events():
    state = {"2026-10-01:GPV6.2":"old",
             "reminder:2026-10-01:GPV6.2:1080:5":{"sent":True},
             "2026-11-12:GPV6.2":{"digest":"x"},
             "house:queue_hold":{"code":"abc"},
             "official:last_id":4800}
    assert trim_state(state,DAY) == 2
    assert "2026-11-12:GPV6.2" in state
    assert "official:last_id" in state
    assert "house:queue_hold" in state


def test_telegram_429_retry_after_respected(monkeypatch):
    first = Mock(status_code=429)
    first.json.return_value={"ok":False,"parameters":{"retry_after":1}}
    second = Mock(status_code=200)
    second.json.return_value={"ok":True,"result":{"message_id":99}}
    post=Mock(side_effect=[first, second])
    sleep=Mock()
    monkeypatch.setattr("bot.requests.post",post)
    monkeypatch.setattr("bot.time.sleep",sleep)
    assert telegram_send("test-token","-123","<b>Тест</b>") == 99
    assert post.call_count == 2
    sleep.assert_called_once_with(1)


def test_telegram_429_very_long_wait_not_retried(monkeypatch):
    response=Mock(status_code=429)
    response.json.return_value={"parameters":{"retry_after":120}}
    post=Mock(return_value=response)
    monkeypatch.setattr("bot.requests.post",post)
    with pytest.raises(RuntimeError,match="120"):
        telegram_send("private-token","-123","Тест")
    assert post.call_count == 1


def test_reminder_never_sent_after_deadline(monkeypatch):
    post=Mock()
    monkeypatch.setattr("bot.requests.post",post)
    with pytest.raises(RuntimeError,match="минув"):
        telegram_send("private-token","-123","Тест",deadline=on(0,0))
    post.assert_not_called()
