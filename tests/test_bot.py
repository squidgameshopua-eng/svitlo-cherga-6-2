import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import intervals, message, time_range


def test_half_hour_intervals():
    spans, unknown = intervals(
        {"1": "no", "2": "first", "3": "yes", "4": "msecond"},
        {"1": "00:00–00:30", "2": "00:30–01:00",
         "3": "01:00–01:30", "4": "01:30–02:00"},
    )
    assert spans == [(0, 45, "off"), (105, 120, "maybe")]
    assert unknown == 0


def test_unknown_status_is_not_all_clear():
    spans, unknown = intervals({"1": "unknown"}, {"1": "00:00–00:30"})
    text = message(date(2026, 11, 7), "GPV6.2", True, spans, unknown, None)
    assert unknown == 1
    assert "отключений нет" not in text


def test_invalid_time_rejected():
    try:
        time_range("23:30–25:00")
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid slot not rejected")


def test_dtek_hourly_intervals():
    assert time_range("00-01") == (0, 60)
    assert time_range("23-24") == (1380, 1440)
    spans, unknown = intervals({"1": "first", "2": "second"},
                               {"1": "00-01", "2": "01-02"})
    assert spans == [(0, 30, "off"), (90, 120, "off")]
    assert unknown == 0


def test_only_queue_in_message():
    text = message(date(2026, 11, 7), "GPV6.2", True, [(0, 60, "off")], 0, None)
    assert "Черга 6.2" in text
    assert "00:00" in text
    assert "01:00" in text
    assert "Дніпровські електромережі" not in text


def test_no_personal_address_in_source():
    source = (Path(__file__).resolve().parents[1] / "bot.py").read_text(encoding="utf-8")
    assert "DTEK_GROUP" in source
    assert "get_home_num(" not in source
