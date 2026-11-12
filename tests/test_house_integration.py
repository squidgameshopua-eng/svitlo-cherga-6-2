"""Integration tests: one house, one recipient, persistent on-disk state."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import bot

KYIV=ZoneInfo("Europe/Kyiv")


def fake_schedule(group="GPV6.2", days=2, has_week=False):
    today=datetime.now(KYIV).date()
    raw={}
    for i in range(days):
        day=today+timedelta(days=i)
        stamp=str(int(datetime(day.year,day.month,day.day,tzinfo=KYIV).timestamp()))
        raw[stamp]={group: {"1":"no","2":"yes"}}
    return SimpleNamespace(
        fact=SimpleNamespace(days=raw,update="12.11.2026 17:00"),
        preset=SimpleNamespace(time_zone={"1":"00-12","2":"12-24"},
                               is_active=has_week,groups={}),
    ),[group]


def setup(monkeypatch,path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN","token-for-test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID","-100-test")
    monkeypatch.setenv("STATE_PATH",str(path))
    monkeypatch.setenv("DTEK_GROUP","6.2")
    monkeypatch.setenv("ANNOUNCEMENTS_ENABLED","false")
    monkeypatch.setenv("REMINDER_MINUTES","0")
    monkeypatch.delenv("DTEK_ADDRESS_JSON",raising=False)
    monkeypatch.delenv("QUEUE_APPROVAL",raising=False)


def test_successful_notification_persisted_when_next_one_fails(tmp_path,monkeypatch):
    state_file=tmp_path/"state.json"
    setup(monkeypatch,state_file)
    async def load():return fake_schedule()
    monkeypatch.setattr(bot,"get_schedule",load)
    calls=[]
    def send(_token,_chat,text,deadline=None):
        calls.append(text)
        if len(calls)==2:
            raise RuntimeError("Network fails after first successful delivery")
        return 1
    monkeypatch.setattr(bot,"telegram_send",send)
    with pytest.raises(RuntimeError,match="Помилки Telegram"):
        asyncio.run(bot.once())
    stored=json.loads(state_file.read_text())
    day=datetime.now(KYIV).date()
    assert f"{day.isoformat()}:GPV6.2" in stored
    assert f"{(day+timedelta(days=1)).isoformat()}:GPV6.2" not in stored
    assert len(calls)==2


def test_unchanged_data_is_silent_across_restarts(tmp_path,monkeypatch):
    state_file=tmp_path/"state.json"
    setup(monkeypatch,state_file)
    async def load():return fake_schedule(days=1)
    monkeypatch.setattr(bot,"get_schedule",load)
    calls=[]
    monkeypatch.setattr(bot,"telegram_send",
                        lambda _token,_chat,text,deadline=None:calls.append(text))
    asyncio.run(bot.once())
    assert len(calls)==1
    asyncio.run(bot.once())
    assert len(calls)==1


def address_entry(groups):
    return SimpleNamespace(
        group_ids=groups, outage_type="", start_date=None,
        end_date=None, sub_type=None,
    )


def setup_address(monkeypatch):
    monkeypatch.setenv(
        "DTEK_ADDRESS_JSON",
        '{"city":"місто","street":"вулиця","house":"12"}',
    )
    import alerts
    current = {"queues": ["GPV6.2"]}

    async def lookup(_secret):
        return address_entry(current["queues"])

    monkeypatch.setattr(alerts, "fetch_address_status", lookup)
    return current


def test_added_queue_produces_notice_and_both_schedules(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    setup(monkeypatch, state_file)
    current = setup_address(monkeypatch)
    current["queues"] = ["GPV6.2", "GPV5.1"]
    requested = []

    async def load(group_ids):
        requested.append(tuple(group_ids))
        today = datetime.now(KYIV).date()
        stamp = str(int(datetime(today.year, today.month, today.day, tzinfo=KYIV).timestamp()))
        source = SimpleNamespace(
            preset=SimpleNamespace(time_zone={"1":"00-12","2":"12-24"},
                                   is_active=False, groups={}),
            fact=SimpleNamespace(
                days={stamp: {
                    "GPV6.2": {"1":"no", "2":"yes"},
                    "GPV5.1": {"1":"yes", "2":"no"},
                }},
                update="12.11.2026 21:00"
            ),
        )
        return source, list(group_ids)

    monkeypatch.setattr(bot, "get_schedule", load)
    calls=[]
    monkeypatch.setattr(
        bot, "telegram_send",
        lambda _t, _c, text, deadline=None: calls.append(text),
    )

    asyncio.run(bot.once())
    assert requested == [("GPV5.1", "GPV6.2")]
    assert len(calls) == 3
    assert sum("ДТЕК оновив черги" in m or "Уточнено черги" in m for m in calls) == 1
    assert any("Черга 5.1" in m and "Графік відключень" in m for m in calls)
    assert any("Черга 6.2" in m and "Графік відключень" in m for m in calls)
    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert stored["house:groups"] == ["GPV5.1","GPV6.2"]
    assert not stored.get("house:queue_hold")

    asyncio.run(bot.once())
    assert len(calls) == 3


def test_removed_queue_keeps_remaining_schedule_and_one_notice(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    setup(monkeypatch, state_file)
    current = setup_address(monkeypatch)
    current["queues"] = ["GPV5.1"]
    state_file.write_text(json.dumps({
        "house:groups": ["GPV5.1", "GPV6.2"],
        "2026-01-01:GPV6.2": {"status":"outages","digest":"old"},
    }), encoding="utf-8")

    async def load(group_ids):
        return fake_schedule(group="GPV5.1", days=1)
    monkeypatch.setattr(bot, "get_schedule", load)
    calls=[]
    monkeypatch.setattr(
        bot, "telegram_send",
        lambda _t,_c,text,deadline=None: calls.append(text)
    )
    asyncio.run(bot.once())
    assert len(calls) == 2
    assert "Вилучено: 6.2" in calls[0]
    assert any("Черга 5.1" in m for m in calls)
    asyncio.run(bot.once())
    assert len(calls) == 2


def test_missing_address_data_never_sends_false_queue_change(tmp_path, monkeypatch):
    state_file=tmp_path/"state.json"
    setup(monkeypatch, state_file)
    current=setup_address(monkeypatch)
    current["queues"]=[]
    calls=[]
    monkeypatch.setattr(
        bot,"telegram_send",
        lambda _t,_c,text,deadline=None: calls.append(text)
    )
    async def should_not_run(*args):
        raise AssertionError("No queue schedule should be sent without confirmation")
    monkeypatch.setattr(bot,"get_schedule",should_not_run)
    asyncio.run(bot.once())
    assert calls == []
    assert "house:groups" not in json.loads(state_file.read_text())


def test_failed_queue_notice_retries_but_schedules_continue(tmp_path,monkeypatch):
    state_file=tmp_path/"state.json"
    setup(monkeypatch,state_file)
    current=setup_address(monkeypatch)
    current["queues"]=["GPV5.1"]
    calls=[]
    async def load(groups):
        return fake_schedule(group="GPV5.1",days=1)
    monkeypatch.setattr(bot,"get_schedule",load)

    def send(_t,_c,text,deadline=None):
        calls.append(text)
        if "Уточнено черги" in text:
            raise RuntimeError("Telegram unavailable for queue-change notice")
    monkeypatch.setattr(bot,"telegram_send",send)
    with pytest.raises(RuntimeError,match="Помилки Telegram"):
        asyncio.run(bot.once())
    assert any("Графік відключень" in x for x in calls)
    assert not json.loads(state_file.read_text()).get("house:groups")
    # The schedule was delivered once; the one-time queue notice is retried.
    monkeypatch.setattr(bot,"telegram_send",
                        lambda _t,_c,text,deadline=None: calls.append(text))
    asyncio.run(bot.once())
    assert sum("Уточнено черги" in x for x in calls) == 2
    assert sum("Графік відключень" in x for x in calls) == 1


def test_migrating_old_queue_hold_does_not_pause_anymore(tmp_path,monkeypatch):
    state_file=tmp_path/"state.json"
    setup(monkeypatch,state_file)
    current=setup_address(monkeypatch)
    current["queues"]=["GPV5.1","GPV6.2"]
    state_file.write_text(json.dumps({
        "house:queue_hold": {
            "groups":["GPV5.1"], "notified":True,"code":"old-code"
        }
    }),encoding="utf-8")
    async def load(groups):
        return fake_schedule(group="GPV6.2",days=1)
    monkeypatch.setattr(bot,"get_schedule",load)
    calls=[]
    monkeypatch.setattr(bot,"telegram_send",
                        lambda _t,_c,text,deadline=None:calls.append(text))
    asyncio.run(bot.once())
    assert "house:queue_hold" not in json.loads(state_file.read_text())
    assert any("5.1" in m and "Додано" in m for m in calls)
    assert any("Графік відключень" in m for m in calls)
