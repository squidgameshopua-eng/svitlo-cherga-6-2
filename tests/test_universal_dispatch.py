"""Free Telegram rate limits and non-spam dispatch tests."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from universal.onboarding import send_due_batch
from universal.reminder_core import (
    connect, register, set_leads, publish_confirmed_starts,
    reserve_free_delivery_slot, prune_delivery_buckets,
)
K=ZoneInfo("Europe/Kyiv")
NOW=datetime(2026,11,8,11,0,tzinfo=K)


def subscribe(db, chat, house, label, kind="private"):
    sub=register(
        db,chat_id=chat,chat_kind=kind,actor_id=chat if kind=="private" else "99",
        operator="dnem",oblast="Область",settlement="Місто",
        street="Вулиця",building=house,queue_code="6.2",label=label,
    )
    set_leads(db,subscription_id=sub,chat_id=chat,
              actor_is_authorized=True,minutes=(5,),now=NOW)
    return sub


def test_many_users_receive_no_more_than_free_global_rate_per_second():
    db=connect()
    for n in range(30):
        subscribe(db,str(n+100), "12", f"Дім {n}")
    start=NOW+timedelta(hours=1)
    publish_confirmed_starts(db,location_id=1,queue_code="6.2",
                             day=NOW.date(),starts=[start],now=NOW)
    messages=[]
    def send(chat,text):
        messages.append((chat,text))
    due=start-timedelta(minutes=5)
    assert send_due_batch(db,send,due,batch=40)==25
    assert len(messages)==25
    assert send_due_batch(db,send,due,batch=40)==0
    assert send_due_batch(db,send,due+timedelta(seconds=3),batch=40)==5
    assert len(messages)==30
    assert len({chat for chat,text in messages})==30
    assert "№" in messages[0][1]


def test_same_chat_two_addresses_one_combined_message_and_unique_labels():
    db=connect()
    subscribe(db,"100","12","Дім")
    subscribe(db,"100","13","Дім")
    start=NOW+timedelta(hours=1)
    for location_id in (1,2):
        publish_confirmed_starts(db,location_id=location_id,
                                 queue_code="6.2",day=NOW.date(),
                                 starts=[start],now=NOW)
    delivered=[]
    assert send_due_batch(db,lambda chat,text:delivered.append(text),
                          start-timedelta(minutes=5),batch=20)==1
    assert len(delivered)==1
    assert "Адреси:" in delivered[0]
    assert "Дім · №1" in delivered[0]
    assert "Дім · №2" in delivered[0]
    assert db.execute("SELECT COUNT(*) FROM reminders WHERE status='sent'").fetchone()[0]==2


def test_single_group_budget_one_per_second_and_eighteen_per_minute():
    db=connect()
    # Two calls inside the same second count as one chat's possible flood.
    assert reserve_free_delivery_slot(db,chat_id="-123",chat_kind="supergroup",now=NOW)
    assert not reserve_free_delivery_slot(db,chat_id="-123",chat_kind="supergroup",now=NOW)
    for sec in range(1,18):
        assert reserve_free_delivery_slot(db,chat_id="-123",chat_kind="supergroup",
                                          now=NOW+timedelta(seconds=sec))
    assert not reserve_free_delivery_slot(db,chat_id="-123",chat_kind="supergroup",
                                          now=NOW+timedelta(seconds=18))
    assert reserve_free_delivery_slot(db,chat_id="-123",chat_kind="supergroup",
                                      now=NOW+timedelta(minutes=1))
    prune_delivery_buckets(db,NOW+timedelta(hours=3))


def test_free_quota_prevents_unlimited_addresses_in_one_chat():
    db=connect()
    for n in range(30):
        register(db,chat_id="100",chat_kind="private",actor_id="100",
                 operator="dnem",oblast="Область",settlement="Місто",
                 street="Вулиця",building=str(n),queue_code="6.2",label=f"Адреса {n}")
    with pytest.raises(ValueError, match="30 адресних"):
        register(db,chat_id="100",chat_kind="private",actor_id="100",
                 operator="dnem",oblast="Область",settlement="Місто",
                 street="Вулиця",building="31",queue_code="6.2",label="Ще адреса")
    # Updating an existing address is still possible when the cap is reached.
    register(db,chat_id="100",chat_kind="private",actor_id="100",
             operator="dnem",oblast="Область",settlement="Місто",
             street="Вулиця",building="1",queue_code="6.2",label="Нова назва")
