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


def test_changed_queue_pauses_all_notifications_until_explicit_ack(tmp_path,monkeypatch):
    state_file=tmp_path/"state.json"
    setup(monkeypatch,state_file)
    monkeypatch.setenv("DTEK_ADDRESS_JSON",'{"city":"місто","street":"вулиця","house":"12"}')
    import alerts
    async def address():
        return SimpleNamespace(group_ids=["GPV5.1"],outage_type="",
                               start_date=None,end_date=None,sub_type=None)
    monkeypatch.setattr(alerts,"fetch_address_status",lambda secret:address())
    async def should_not_call():
        raise AssertionError("Must not read or send stale group during queue hold")
    monkeypatch.setattr(bot,"get_schedule",should_not_call)
    calls=[]
    monkeypatch.setattr(bot,"telegram_send",
                        lambda _token,_chat,text,deadline=None:calls.append(text))
    asyncio.run(bot.once())
    assert len(calls)==1
    assert "Черга будинку змінилася" in calls[0]
    stored=json.loads(state_file.read_text())
    assert stored["house:queue_hold"]["notified"] is True
    asyncio.run(bot.once())
    assert len(calls)==1
    monkeypatch.setenv("DTEK_GROUP","5.1")
    monkeypatch.setenv("QUEUE_APPROVAL",stored["house:queue_hold"]["code"])
    async def new_schedule():return fake_schedule("GPV5.1",days=1)
    monkeypatch.setattr(bot,"get_schedule",new_schedule)
    asyncio.run(bot.once())
    assert len(calls)==2
    assert "Черга 5.1" in calls[1]
    assert "house:queue_hold" not in json.loads(state_file.read_text())
