import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import (
    cancellation_message, collect_notifications, day_message, duration,
    fingerprint, intervals, minutes_text, parse_verified, state_digest,
    state_status, time_range, weekly_cancellation_message, weekly_message,
)

KYIV = ZoneInfo("Europe/Kyiv")
TODAY = date(2026, 11, 8)
TOMORROW = TODAY + timedelta(days=1)
GROUP = "GPV6.2"
LABELS = {"1": "00-01", "2": "01-02", "3": "02-03"}
CLEAR = {"1": "yes", "2": "yes", "3": "yes"}


def full(**updates):
    value = dict(CLEAR)
    value.update(updates)
    return value


def fake_source(actual=None, weekly=None, active=True):
    facts = {}
    for day, groups in (actual or {}).items():
        stamp = int(datetime(day.year, day.month, day.day, tzinfo=KYIV).timestamp())
        facts[str(stamp)] = groups

    # Empty/missing weekly days can be simulated with explicit None.
    weekly = weekly or {}
    plans = {
        k: full(**v) if v is not None else None
        for k, v in weekly.items()
    }
    plan = SimpleNamespace(get_day=lambda i: (
        SimpleNamespace(slots=plans.get(i, dict(CLEAR)))
        if plans.get(i, dict(CLEAR)) is not None else None
    ))
    preset = SimpleNamespace(time_zone=LABELS, is_active=active,
                             groups={GROUP: plan})
    fact = (SimpleNamespace(days=facts, update="08.11.2026 12:00")
            if facts else None)
    return SimpleNamespace(preset=preset, fact=fact)


def candidates(source, state=None):
    return collect_notifications(source, GROUP, TODAY, state or {})


def posted_state(kind="outages", digest="old-digest"):
    return {f"{TODAY.isoformat()}:{GROUP}": {"digest": digest, "status": kind}}


def test_hourly_and_minute_slots():
    assert time_range("00-01") == (0, 60)
    assert time_range("23-24") == (1380, 1440)
    assert time_range("00:30–01:00") == (30, 60)
    try:
        time_range("23:30–25:00")
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid slot accepted")


def test_dtek_slot_parsing_and_lengths():
    spans, unknown = intervals(
        {"1": "first", "2": "second", "3": "maybe"}, LABELS)
    assert spans == [(0, 30, "off"), (90, 120, "off"), (120, 180, "maybe")]
    assert unknown == 0
    assert duration(spans, "off") == 60
    assert duration(spans, "maybe") == 60
    assert minutes_text(30) == "30 хв"
    assert minutes_text(90) == "1 год 30 хв"
    assert minutes_text(120) == "2 год"


def test_missing_or_incomplete_is_never_cancellation():
    s = fake_source()
    assert candidates(s, posted_state()) == []
    s = fake_source(actual={TODAY: {GROUP: {"1": "yes"}}})
    assert candidates(s, posted_state()) == []
    assert parse_verified({"1": "yes"}, LABELS) is None
    s = fake_source(actual={TODAY: {GROUP: full(**{"1": "unknown"})}})
    assert candidates(s, posted_state()) == []


def test_initial_no_outages_is_silent():
    source = fake_source(actual={TODAY: {GROUP: dict(CLEAR)}})
    assert candidates(source) == []
    assert candidates(fake_source()) == []


def test_initial_new_daily_outage_produces_one_event():
    s = fake_source(actual={TODAY: {GROUP: full(**{"2": "no"})}})
    result = candidates(s)
    assert [row[2] for row in result] == ["day"]
    assert result[0][0] == f"{TODAY.isoformat()}:{GROUP}"
    key, payload, _kind, _parts = result[0]
    assert fingerprint(payload) != ""
    assert candidates(s, {key: {"digest": fingerprint(payload),
                                "status": "outages"}}) == result


def test_previously_planned_then_all_white_sends_one_cancellation():
    s = fake_source(actual={TODAY: {GROUP: dict(CLEAR)}})
    state = posted_state()
    event = candidates(s, state)
    assert len(event) == 1 and event[0][2] == "day_cancel"
    key, payload, _, _ = event[0]
    state[key] = {"digest": fingerprint(payload), "status": "cancelled"}
    assert candidates(s, state) == []


def test_legacy_state_works_for_cancellation():
    old = {f"{TODAY.isoformat()}:{GROUP}": "legacy-sha"}
    s = fake_source(actual={TODAY: {GROUP: dict(CLEAR)}})
    assert candidates(s, old)[0][2] == "day_cancel"
    assert state_status("legacy-sha") == "outages"
    assert state_digest({"digest": "abc", "status": "cancelled"}) == "abc"


def test_cancelled_then_outage_returns_as_newly_planned():
    source = fake_source(actual={TODAY: {GROUP: full(**{"2": "no"})}})
    event = candidates(source, posted_state(kind="cancelled"))
    assert event[0][2] == "day"
    text = day_message(TODAY, GROUP, event[0][3][1], event="resumed")
    assert "знову заплановано" in text


def test_changed_time_and_same_time():
    a = fake_source(actual={TODAY: {GROUP: full(**{"1": "no"})}})
    b = fake_source(actual={TODAY: {GROUP: full(**{"2": "no"})}})
    first = candidates(a)[0]
    again = candidates(b)[0]
    assert fingerprint(first[1]) != fingerprint(again[1])
    assert fingerprint(first[1]) == fingerprint(candidates(a)[0][1])


def test_no_tomorrow_daily_if_only_weekly():
    source = fake_source(
        actual={TODAY: {GROUP: full(**{"1": "no"})}},
        weekly={7: {"1": "maybe"}},
    )
    items = candidates(source)
    assert f"{TOMORROW.isoformat()}:{GROUP}" not in [row[0] for row in items]
    assert len(items) == 2
    assert any(row[2] == "week" for row in items)


def test_tomorrow_confirmed_schedule_is_separate():
    s = fake_source(actual={
        TODAY: {GROUP: full(**{"1": "no"})},
        TOMORROW: {GROUP: full(**{"2": "second"})},
    })
    assert {row[0] for row in candidates(s)} == {
        f"{TODAY.isoformat()}:{GROUP}",
        f"{TOMORROW.isoformat()}:{GROUP}",
    }


def test_initial_weekly_template_without_active_restriction_silent():
    source = fake_source(weekly={7: {"1": "maybe"}})
    assert candidates(source) == []
    source = fake_source(
        actual={TODAY: {GROUP: dict(CLEAR)}},
        weekly={7: {"1": "maybe"}},
    )
    assert candidates(source) == []


def test_weekly_complete_all_white_after_prior_post_cancels_once():
    monday = TODAY - timedelta(days=TODAY.weekday())
    key = f"week:{monday.isoformat()}:{GROUP}"
    state = {key: {"digest": "previous-week", "status": "outages"}}
    event = candidates(fake_source(), state)
    assert len(event) == 1 and event[0][2] == "week_cancel"
    state[key] = {"digest": fingerprint(event[0][1]), "status": "cancelled"}
    assert candidates(fake_source(), state) == []
    assert candidates(fake_source()) == []


def test_missing_weekly_day_not_interpreted_as_weekly_cancellation():
    monday = TODAY - timedelta(days=TODAY.weekday())
    state = {f"week:{monday.isoformat()}:{GROUP}":
             {"digest": "previous-week", "status": "outages"}}
    source = fake_source(weekly={4: None})
    assert candidates(source, state) == []


def test_weekly_active_update_detected():
    a = fake_source(
        actual={TODAY: {GROUP: full(**{"1": "no"})}},
        weekly={7: {"1": "maybe"}},
    )
    b = fake_source(
        actual={TODAY: {GROUP: full(**{"1": "no"})}},
        weekly={7: {"2": "maybe"}},
    )
    week_a = next(row for row in candidates(a) if row[2] == "week")
    week_b = next(row for row in candidates(b) if row[2] == "week")
    assert fingerprint(week_a[1]) != fingerprint(week_b[1])


def test_all_telegram_templates_are_ukrainian():
    spans = [(0, 90, "off"), (120, 150, "maybe")]
    for event, expected in (
        ("new", "Графік відключень"),
        ("changed", "Графік відключень змінено"),
        ("resumed", "Відключення знову заплановано"),
    ):
        t = day_message(TODAY, GROUP, spans, "08.11.2026 12:00", event=event)
        assert expected in t
        assert "1 год 30 хв" in t
        assert "30 хв" in t
        assert "Черга 6.2" in t
        assert "Джерело: ДТЕК" in t
    c = cancellation_message(TODAY, GROUP)
    assert "Планові відключення скасовано" in c
    assert "гарантовано" not in c
    monday = TODAY - timedelta(days=TODAY.weekday())
    w = weekly_message(monday, GROUP, [(TODAY, [(0, 60)])])
    assert "Тижневий прогноз" in w
    assert "Нд" in w
    assert "Попередній прогноз" in w
    wc = weekly_cancellation_message(monday, GROUP)
    assert "Тижневий прогноз відключень скасовано" in wc


def test_no_personal_address_in_bot_source():
    source = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
    assert "DTEK_GROUP" in source
    assert "get_home_num(" not in source
