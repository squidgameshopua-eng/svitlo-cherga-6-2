import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import collect_notifications, intervals, message, time_range, weekly_message

KYIV = ZoneInfo("Europe/Kyiv")
TODAY = date(2026, 11, 7)
GROUP = "GPV6.2"


def fake_source(actual=None, weekly=None, region_groups=None):
    """
    actual: mapping date -> {group: {slot: status}}
    weekly: mapping ISO weekday -> {slot: status}
    """
    actual = actual or {}
    weekly = weekly or {}
    facts = {}
    for day, groups in actual.items():
        stamp = int(datetime(day.year, day.month, day.day, tzinfo=KYIV).timestamp())
        facts[str(stamp)] = groups
    plan = SimpleNamespace(
        get_day=lambda weekday: (
            SimpleNamespace(slots=weekly[weekday]) if weekday in weekly else None
        )
    )
    preset = SimpleNamespace(
        time_zone={"1": "00-01", "2": "01-02"},
        is_active=True,
        groups={GROUP: plan},
    )
    fact = SimpleNamespace(days=facts, update="07.11.2026 20:00") if facts else None
    return SimpleNamespace(preset=preset, fact=fact)


def test_time_ranges():
    assert time_range("00-01") == (0, 60)
    assert time_range("23-24") == (1380, 1440)
    assert time_range("00:30–01:00") == (30, 60)
    try:
        time_range("23:30–25:00")
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid time was accepted")


def test_half_hour_intervals_and_merge():
    spans, unknown = intervals(
        {"1": "first", "2": "second"},
        {"1": "00-01", "2": "01-02"},
    )
    assert spans == [(0, 30, "off"), (90, 120, "off")]
    assert unknown == 0


def test_no_schedule_and_no_current_restrictions_are_silent():
    assert collect_notifications(fake_source(), GROUP, TODAY) == []
    # Weekly templates may exist even when no restrictions are active.
    assert collect_notifications(fake_source(weekly={6: {"1": "maybe"}}),
                                 GROUP, TODAY) == []
    assert collect_notifications(fake_source(
        actual={TODAY: {GROUP: {"1": "yes", "2": "yes"}}},
        weekly={6: {"1": "maybe"}},
    ), GROUP, TODAY) == []


def test_old_confirmed_day_does_not_activate_weekly():
    yesterday = TODAY - timedelta(days=1)
    source = fake_source(
        actual={yesterday: {GROUP: {"1": "no"}}},
        weekly={6: {"1": "maybe"}},
    )
    assert collect_notifications(source, GROUP, TODAY) == []


def test_tomorrow_is_not_confirmed_by_weekly_template():
    source = fake_source(
        actual={TODAY: {GROUP: {"1": "no"}}},
        weekly={6: {"1": "maybe"}, 7: {"1": "maybe"}},
    )
    result = collect_notifications(source, GROUP, TODAY)
    keys = [n[0] for n in result]
    assert f"{TODAY.isoformat()}:{GROUP}" in keys
    assert f"{(TODAY + timedelta(days=1)).isoformat()}:{GROUP}" not in keys
    assert any(key.startswith("week:") for key in keys)
    assert len(result) == 2


def test_confirmed_tomorrow_is_sent_when_published():
    tomorrow = TODAY + timedelta(days=1)
    source = fake_source(
        actual={
            TODAY: {GROUP: {"1": "no"}},
            tomorrow: {GROUP: {"2": "second"}},
        },
        weekly={6: {"1": "maybe"}},
    )
    result = collect_notifications(source, GROUP, TODAY)
    keys = [n[0] for n in result]
    assert f"{TODAY.isoformat()}:{GROUP}" in keys
    assert f"{tomorrow.isoformat()}:{GROUP}" in keys


def test_malformed_or_unrecognized_slots_do_not_generate_posts():
    source = fake_source(actual={TODAY: {GROUP: {"1": "unknown"}}},
                         weekly={6: {"1": "maybe"}})
    assert collect_notifications(source, GROUP, TODAY) == []
    assert message(TODAY, GROUP, True, [], 0, None) is None
    assert message(TODAY, GROUP, True, [(0, 60, "off")], 1, None) is None


def test_daily_and_weekly_changes_are_separately_detectable():
    a = fake_source(actual={TODAY: {GROUP: {"1": "no"}}},
                    weekly={6: {"1": "maybe"}})
    b = fake_source(actual={TODAY: {GROUP: {"1": "first"}}},
                    weekly={6: {"1": "maybe"}})
    c = fake_source(actual={TODAY: {GROUP: {"1": "no"}}},
                    weekly={6: {"1": "mfirst"}})
    ra = {i[0]: i[1] for i in collect_notifications(a, GROUP, TODAY)}
    rb = {i[0]: i[1] for i in collect_notifications(b, GROUP, TODAY)}
    rc = {i[0]: i[1] for i in collect_notifications(c, GROUP, TODAY)}
    day = f"{TODAY.isoformat()}:{GROUP}"
    week = f"week:2026-11-02:{GROUP}"
    assert ra[day] != rb[day]
    assert ra[week] == rb[week]
    assert ra[day] == rc[day]
    assert ra[week] != rc[week]
    assert ra == {i[0]: i[1] for i in collect_notifications(a, GROUP, TODAY)}


def test_no_weekly_if_no_positive_outage():
    source = fake_source(
        actual={TODAY: {"GPV1.1": {"1": "no"}}},
        weekly={6: {"1": "yes"}},
    )
    assert collect_notifications(source, GROUP, TODAY) == []


def test_message_copy_and_weekly_warning():
    daily = message(TODAY, GROUP, True, [(0, 60, "off")], 0, "07.11.2026 20:00")
    assert "Черга 6.2" in daily
    assert "00:00–01:00" in daily
    assert "Дніпровські електромережі" not in daily
    assert "отключений нет" not in daily
    changed = message(TODAY, GROUP, True, [(0, 60, "off")], 0, None, changed=True)
    assert "Изменение" in changed
    w = weekly_message(date(2026, 11, 2), GROUP,
                       [(TODAY, ((0, 120),))], changed=False)
    assert "Недельный прогноз" in w
    assert "Сб 07.11" in w
    assert "предварительный прогноз" in w
    assert "Суточный график" not in w


def test_no_address_in_source():
    source = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
    assert "DTEK_GROUP" in source
    assert "get_home_num(" not in source
