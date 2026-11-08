"""Chat identity, multiple addresses and privacy isolation tests."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from universal.onboarding import format_subscription_dashboard

from universal.reminder_core import (
    connect, delete_subscription, list_accessible_subscriptions,
    migrate_chat_id, publish_confirmed_starts, register, selected_leads,
    set_leads, update_chat_title, lease_due,
)

NOW = datetime(2026, 11, 8, 11, 0, tzinfo=ZoneInfo("Europe/Kyiv"))


def add(db, chat_id, kind, owner, house, label, street="Вулиця", group="6.2"):
    return register(
        db, chat_id=chat_id, chat_kind=kind, actor_id=owner,
        operator="dnem", oblast="Область", settlement="Місто",
        street=street, building=house, queue_code=group, label=label,
    )


def test_same_person_many_addresses_same_personal_chat():
    db = connect()
    a = add(db, "101", "private", "101", "12", "Дім")
    b = add(db, "101", "private", "101", "18", "Робота")
    c = add(db, "101", "private", "101", "22", "Для бабусі")
    assert len({a,b,c}) == 3
    set_leads(db, subscription_id=a, chat_id="101",
              actor_is_authorized=True, minutes=(15,), now=NOW)
    set_leads(db, subscription_id=b, chat_id="101",
              actor_is_authorized=True, minutes=(), now=NOW)
    assert selected_leads(db, a) == (15,)
    assert selected_leads(db, b) == ()
    accessible = list_accessible_subscriptions(
        db, actor_id="101", is_admin_of_destination=lambda *_:False
    )
    assert {r["label"] for r in accessible} == {"Дім", "Для бабусі", "Робота"}
    assert len({r["subscription_id"] for r in accessible}) == 3
    assert all(r["chat_title"] == "Особистий чат" for r in accessible)


def test_same_person_many_chats_are_distinguished_by_chat_id_and_title():
    db = connect()
    private = add(db, "101", "private", "101", "12", "Дім")
    group = add(db, "-10022", "supergroup", "101", "12", "Дім")
    channel = add(db, "-10033", "channel", "101", "12", "Дім")
    update_chat_title(db, chat_id="-10022", title="Наш будинок",
                      actor_is_authorized=True)
    update_chat_title(db, chat_id="-10033", title="Оголошення ЖК",
                      actor_is_authorized=True)
    result = list_accessible_subscriptions(
        db, actor_id="101",
        manageable_chat_ids=("-10022", "-10033"),
        is_admin_of_destination=lambda chat, actor: actor == "101",
    )
    assert {r["destination_id"] for r in result} == {"101", "-10022", "-10033"}
    assert {r["subscription_id"] for r in result} == {private, group, channel}
    assert {r["chat_title"] for r in result} == {
        "Особистий чат", "Наш будинок", "Оголошення ЖК"
    }


def test_non_admin_cannot_see_other_chat_address_even_when_guessing_chat_id():
    db = connect()
    add(db, "101", "private", "101", "12", "Дім")
    add(db, "-10044", "channel", "101", "17", "Для мешканців")
    add(db, "-10055", "supergroup", "999", "28", "Квартира")
    actual = list_accessible_subscriptions(
        db, actor_id="888",
        manageable_chat_ids=("-10044", "-10055", "101"),
        is_admin_of_destination=lambda *_:False,
    )
    assert actual == []
    with pytest.raises(ValueError):
        list_accessible_subscriptions(
            db, actor_id="888", manageable_chat_ids=tuple(str(x) for x in range(200)),
            is_admin_of_destination=lambda *_:False,
        )


def test_deleting_one_subscription_does_not_affect_other_chats_or_addresses():
    db = connect()
    a = add(db, "101", "private", "101", "12", "Дім")
    b = add(db, "101", "private", "101", "18", "Робота")
    c = add(db, "202", "private", "202", "12", "Дім")
    set_leads(db,subscription_id=a,chat_id="101",
              actor_is_authorized=True,minutes=(5,15),now=NOW)
    start = NOW + timedelta(hours=2)
    publish_confirmed_starts(db,location_id=1,queue_code="6.2",
                             day=NOW.date(),starts=[start],now=NOW)
    assert db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 2
    with pytest.raises(PermissionError):
        delete_subscription(db, subscription_id=a, chat_id="202",
                            actor_is_authorized=True)
    delete_subscription(db, subscription_id=a, chat_id="101",
                        actor_is_authorized=True)
    assert db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 2
    # Location 12 survives because chat 202 still uses it.
    assert db.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 2
    delete_subscription(db, subscription_id=b, chat_id="101",
                        actor_is_authorized=True)
    assert db.execute("SELECT COUNT(*) FROM destinations").fetchone()[0] == 1
    assert list_accessible_subscriptions(
        db,actor_id="101",is_admin_of_destination=lambda *_:False)==[]


def test_group_to_supergroup_migration_preserves_subscriptions_and_settings():
    db=connect()
    sub=add(db,"-5","group","101","18","Дім")
    set_leads(db,subscription_id=sub,chat_id="-5",
              actor_is_authorized=True,minutes=(5,),now=NOW)
    with pytest.raises(PermissionError):
        migrate_chat_id(db,old_id="-5",new_id="-10055",
                        verified_migration=False)
    assert migrate_chat_id(db,old_id="-5",new_id="-10055",
                           verified_migration=True)
    r=list_accessible_subscriptions(
        db,actor_id="101",manageable_chat_ids=("-10055",),
        is_admin_of_destination=lambda *_:True,
    )
    assert len(r)==1 and r[0]["destination_id"]=="-10055"
    assert r[0]["reminder_minutes"]==(5,)


def test_private_dashboard_distinguishes_chats_and_labels_without_cross_leak():
    db=connect()
    a=add(db,"101","private","101","12","Дім")
    b=add(db,"101","private","101","18","Робота")
    c=add(db,"-10022","channel","101","12","Дім")
    update_chat_title(db,chat_id="-10022",title="Оголошення <Будинку>",
                      actor_is_authorized=True)
    items=list_accessible_subscriptions(
        db,actor_id="101",manageable_chat_ids=("-10022",),
        is_admin_of_destination=lambda chat,actor:actor=="101",
        include_address=True,
    )
    messages=format_subscription_dashboard(items)
    text="\n".join(messages)
    assert f"№{a}" in text and f"№{b}" in text and f"№{c}" in text
    assert "Особистий чат" in text
    assert "Оголошення &lt;Будинку&gt;" in text
    assert "Робота" in text and "Дім" in text
    assert "Вулиця" in text
    assert all(len(page)<=3500 for page in messages)
    # Safe-by-default: address fields do not leave backend unless explicitly requested.
    redacted=list_accessible_subscriptions(
        db,actor_id="101",manageable_chat_ids=("-10022",),
        is_admin_of_destination=lambda *_:True
    )
    assert all("street" not in item for item in redacted)
    assert "Вулиця" not in "\n".join(format_subscription_dashboard(redacted))


def test_messages_are_paginated_for_many_addresses():
    rows=[]
    for n in range(30):
        rows.append({
            "chat_kind":"private","chat_title":"Особистий чат",
            "label":"Адреса з довгою назвою "+str(n)+" X"*8,
            "subscription_id":n+1,"queue":"6.2","status":"active",
            "reminder_minutes":(15,5),
            "settlement":"Місто","street":"Вулиця "+str(n),"building":str(n+1),
        })
    pages=format_subscription_dashboard(rows,max_chars=400)
    assert len(pages)>1
    assert all(len(p)<=400 for p in pages)
    assert sum(p.count("Черга:") for p in pages)==30


def test_private_chat_id_does_not_allow_impersonation_by_other_user():
    from universal.onboarding import confirm_address
    db=connect()
    address={"operator":"dnem","oblast":"Область","settlement":"Місто",
             "street":"Вулиця","building":"12"}
    with pytest.raises(PermissionError):
        confirm_address(db,recipient_chat_id="101",recipient_kind="private",
                        actor_id="999",authorized=True,address=address,
                        queue_code="6.2")
    assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]==0
