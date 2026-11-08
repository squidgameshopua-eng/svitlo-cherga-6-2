from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from universal.onboarding import (
    PROMPT, callback_choice, confirm_address, send_due_batch,
)
from universal.reminder_core import (
    connect, publish_confirmed_starts,
)

K=ZoneInfo("Europe/Kyiv")
NOW=datetime(2026,11,8,10,0,tzinfo=K)
ADDRESS={
    "operator":"dnem", "oblast":"Область",
    "settlement":"Місто","street":"Вулиця","building":"12",
}


def test_first_address_wizard_shows_five_buttons():
    db=connect()
    prompt=confirm_address(
        db,recipient_chat_id="123",recipient_kind="private",
        actor_id="123",authorized=True,address=ADDRESS,
        queue_code="6.2",label="Дім",
    )
    assert prompt["chat_id"]=="123"
    assert "Адресу збережено" in prompt["text"]
    assert len(prompt["reply_markup"]["inline_keyboard"])==5
    assert [x[0]["callback_data"].split(":")[-1]
            for x in prompt["reply_markup"]["inline_keyboard"]]==[
        "0","5","15","30","60",
    ]
    result=callback_choice(
        db,callback_data=f"lead:{prompt['subscription_id']}:5",
        acting_user_id="123",
        is_admin_of_destination=lambda *_:False,now=NOW,
    )
    assert result["chosen_minutes"]==5
    assert "за 5 хвилин" in result["confirmation"]


def test_private_chat_prevents_other_user_changing_alerts():
    db=connect()
    prompt=confirm_address(
        db,recipient_chat_id="123",recipient_kind="private",
        actor_id="123",authorized=True,address=ADDRESS,
        queue_code="6.2",
    )
    with pytest.raises(PermissionError):
        callback_choice(db,callback_data=f"lead:{prompt['subscription_id']}:0",
                        acting_user_id="456",
                        is_admin_of_destination=lambda *_:True,
                        now=NOW)


def test_group_or_channel_configuration_by_admin_in_private():
    db=connect()
    prompt=confirm_address(
        db,recipient_chat_id="-100000",recipient_kind="channel",
        actor_id="123",authorized=True,address=ADDRESS,
        queue_code="6.2",configuration_chat_id="123",
    )
    assert prompt["chat_id"]=="123"
    with pytest.raises(PermissionError):
        callback_choice(
            db,callback_data=f"lead:{prompt['subscription_id']}:30",
            acting_user_id="456",
            is_admin_of_destination=lambda target,actor:False,now=NOW,
        )
    output=callback_choice(
        db,callback_data=f"lead:{prompt['subscription_id']}:30",
        acting_user_id="123",
        is_admin_of_destination=lambda target,actor:(target=="-100000" and actor=="123"),
        now=NOW,
    )
    assert output["chat_id"]=="-100000"
    assert output["chosen_minutes"]==30


def test_disabled_reminder_does_not_disable_schedule_news():
    db=connect()
    prompt=confirm_address(
        db,recipient_chat_id="123",recipient_kind="private",
        actor_id="123",authorized=True,address=ADDRESS,queue_code="6.2",
    )
    output=callback_choice(
        db,callback_data=f"lead:{prompt['subscription_id']}:0",
        acting_user_id="123",
        is_admin_of_destination=lambda *_:False,now=NOW,
    )
    assert "нагадування вимкнено" in output["confirmation"].lower()
    assert "Сповіщення про графік" in output["confirmation"]


def test_worker_only_marks_sent_after_success():
    db=connect()
    prompt=confirm_address(
        db,recipient_chat_id="123",recipient_kind="private",
        actor_id="123",authorized=True,address=ADDRESS,queue_code="6.2",
    )
    callback_choice(
        db,callback_data=f"lead:{prompt['subscription_id']}:5",
        acting_user_id="123",is_admin_of_destination=lambda *_:False,now=NOW)
    start=NOW+timedelta(hours=1)
    publish_confirmed_starts(
        db,location_id=1,queue_code="6.2",day=NOW.date(),
        starts=[start],now=NOW,
    )
    due=start-timedelta(minutes=5)
    calls=[]
    def fail(chat,text):
        calls.append((chat,text))
        raise RuntimeError("Telegram rate limit")
    assert send_due_batch(db,fail,due)==0
    assert len(calls)==1
    assert db.execute("SELECT status FROM reminders").fetchone()[0]=="pending"
    def success(chat,text):
        calls.append((chat,text))
        assert chat=="123"
        assert "До відключення приблизно" in text
    assert send_due_batch(db,success,due+timedelta(seconds=46))==1
    assert send_due_batch(db,success,due+timedelta(seconds=47))==0
    assert db.execute("SELECT status FROM reminders").fetchone()[0]=="sent"
