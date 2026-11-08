from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from alerts import (
    address_clear_message,
    address_decision,
    address_event,
    address_fields,
    address_message,
    get_house_entry,
    normalized_house,
    official_posts,
    regional_event,
    region_candidates,
)

KYIV=ZoneInfo("Europe/Kyiv")
NOW=datetime(2026,11,8,16,0,tzinfo=KYIV)


def fake_post(id, title, date=NOW):
    return {"id":id,"text":title,"date":date,"url":f"https://t.me/dtek_ua/{id}"}


def html_page(posts):
    nodes=[]
    for p in posts:
        nodes.append(
            f'<div class="tgme_widget_message" data-post="dtek_ua/{p["id"]}">'
            f'<div class="tgme_widget_message_text">{p["text"]}</div>'
            f'<time datetime="{p["date"].isoformat()}">x</time>'
            "</div>"
        )
    return "<html><body>"+"".join(nodes)+"</body></html>"


def house(reason="",kind="",start="",end="",groups=("GPV6.2",)):
    return SimpleNamespace(
        sub_type=reason,outage_type=kind,start_date=start,end_date=end,
        group_ids=list(groups),
    )


def test_official_html_posts_are_parsed_by_id_and_time():
    posts=[fake_post(15,"Дніпропетровщина: графіки"),
           fake_post(16,"Дніпропетровщина: екстрені відключення запроваджено")]
    result=official_posts(html_page(posts))
    assert len(result)==2
    assert result[1]["id"]==16
    assert result[1]["url"]=="https://t.me/dtek_ua/16"


def test_kyiv_dnipro_city_and_general_news_not_transmitted_to_kryvyi_rih():
    samples=[
        "‼️ Київ, Київська область та місто Дніпро: тривають екстрені відключення",
        "‼️ Дніпро: екстрені відключення",
        "‼️ Київ: екстрені відключення запроваджено",
        "Дніпропетровщина: ворог атакує енергооб’єкти ДТЕК",
        "Дніпропетровщина: графіки відключень на 8 листопада",
        "Дніпропетровщина: відновлення електропостачання у місті Дніпро",
    ]
    assert all(regional_event(fake_post(21, t)) is None for t in samples)


def test_emergency_started_and_cancelled():
    active=regional_event(fake_post(31,
        "‼️ Дніпропетровщина: екстрені відключення\n"
        "За командою Укренерго застосовані екстрені відключення."))
    ended=regional_event(fake_post(32,
        "Дніпропетровщина: екстрені відключення скасовано"))
    assert active["kind"]=="emergency" and active["status"]=="active"
    assert ended["status"]=="ended"
    assert active["area"]=="Дніпропетровська область"


def test_local_city_emergency_is_applicable():
    e=regional_event(fake_post(45,"Кривий Ріг: запроваджені екстрені відключення"))
    assert e["status"]=="active"
    assert e["area"]=="Кривий Ріг"


def test_gav_is_not_confused_with_gpv():
    e=regional_event(fake_post(33,
        "‼️ Дніпропетровщина: запроваджені графіки аварійних відключень"))
    assert e is not None and e["kind"]=="gav"
    assert regional_event(fake_post(34,
        "Дніпропетровщина: графіки погодинних відключень діють")) is None


def test_first_feed_run_is_silent_and_seeds_latest():
    state={}
    page=html_page([fake_post(10,"Дніпропетровщина: запроваджені екстрені відключення")])
    assert region_candidates(page,state,NOW)==[]
    assert state["official:last_id"]==10


def test_new_status_once_and_repeated_status_silent():
    state={"official:last_id":10}
    first=html_page([fake_post(11,"Дніпропетровщина: запроваджені екстрені відключення")])
    result=region_candidates(first,state,NOW)
    assert len([r for r in result if r[3]])==1
    assert state["official:last_id"]==10  # checkpoint must follow a successful send
    for post_id,key,value,text in result:
        state[key]=value
    repeat=html_page([fake_post(12,"Дніпропетровщина: екстрені відключення тривають")])
    result=region_candidates(repeat,state,NOW)
    assert not [r for r in result if r[3]]


def test_start_and_end_in_same_batch_remain_two_transitions():
    state={"official:last_id":10}
    page=html_page([
        fake_post(11,"Дніпропетровщина: запроваджені екстрені відключення"),
        fake_post(12,"Дніпропетровщина: екстрені відключення скасовано"),
    ])
    output=region_candidates(page,state,NOW)
    assert [r[2] for r in output if r[3]]==["active","ended"]


def test_cancel_without_prior_announcement_is_silent():
    state={"official:last_id":10}
    page=html_page([fake_post(11,"Дніпропетровщина: екстрені відключення скасовано")])
    output=region_candidates(page,state,NOW)
    assert not [r for r in output if r[3]]


def test_stale_news_cannot_trigger_notifications():
    state={"official:last_id":10}
    page=html_page([fake_post(11,"Дніпропетровщина: запроваджені екстрені відключення",
                    NOW-timedelta(hours=9))])
    assert not [r for r in region_candidates(page,state,NOW) if r[3]]


def test_house_normalization_and_private_address_validation():
    assert normalized_house("67/А")==normalized_house("67A")
    assert address_fields('{"city":"місто", "street":"вулиця", "house":"67/А"}')==[
        "місто","вулиця","67/А"
    ]
    try:
        address_fields('{"city":"місто"}')
    except RuntimeError:
        pass
    else:
        raise AssertionError("Unusable secret should fail")


def test_exact_house_selection_without_guessing():
    response=SimpleNamespace(houses={"67/А":house(), "67/Б":house()})
    assert get_house_entry(response,"67A") is response.houses["67/А"]
    try:
        get_house_entry(response,"67/В")
    except RuntimeError:
        pass
    else:
        raise AssertionError("Unknown house must not be guessed")


def test_planned_repair_and_local_fault_distinguished():
    repair=address_event(house(reason="Планові роботи",kind="1",
          start="08.11.2026 16:00",end="08.11.2026 20:00"))
    fault=address_event(house(reason="Аварійне відключення",kind="2",
          start="08.11.2026 16:00",end="08.11.2026 20:00"))
    other=address_event(house(reason="Невідома причина",kind="2"))
    assert repair["category"]=="repair"
    assert fault["category"]=="fault"
    assert other["category"]=="unclassified"
    assert "Планові ремонтні роботи" in address_message(repair)
    assert "Аварійне відключення" in address_message(fault)


def test_no_initial_address_outage_is_silent():
    state={}
    changes=address_decision(house(),state,NOW)
    assert all(x[3] is None for x in changes)
    assert any(x[1]=="address:groups" for x in changes)


def test_new_local_outage_notified_once():
    event=house(reason="Планові роботи",kind="1",
                start="08.11.2026 17:00",end="08.11.2026 20:00")
    state={}
    first=address_decision(event,state,NOW)
    assert len([x for x in first if x[3]])==1
    for _,key,value,_ in first:state[key]=value
    assert not [x for x in address_decision(event,state,NOW) if x[3]]


def test_early_clear_vs_natural_end():
    old={"status":"active","event":{
        "category":"repair","reason":"Планові роботи",
        "start":"08.11.2026 17:00","end":"08.11.2026 20:00"}}
    msg=address_clear_message(old["event"],NOW)
    assert msg and "не відображається" in msg
    assert address_clear_message(old["event"],NOW+timedelta(hours=5)) is None
    state={"address:last_outage":old,"address:groups":["GPV6.2"]}
    changes=address_decision(house(),state,NOW)
    assert len([r for r in changes if r[3]])==1


def test_change_of_house_group_only_after_baseline():
    state={"address:groups":["GPV6.2"]}
    result=address_decision(house(groups=["GPV5.1"]),state,NOW)
    assert any("змінив чергу" in x[3] for x in result if x[3])
    assert all("Кропивницького" not in x[3] for x in result if x[3])


def test_public_state_never_contains_address_response_reason():
    item=house(reason="Планові роботи за приватною адресою 67/А",kind="1",
               start="08.11.2026 17:00",end="08.11.2026 20:00")
    state={}
    result=address_decision(item,state,NOW)
    alerts=[x for x in result if x[1]=="address:last_outage"]
    assert len(alerts)==1
    data=alerts[0][2]
    assert "event" not in data
    assert "reason" not in data
    assert "address" not in data
    assert "67/А" not in str(data)
    assert "67/А" not in str(alerts[0][3])
    assert "Причина: планові роботи" in alerts[0][3]


def test_group_mismatch_at_first_verified_read_gets_warning():
    state={}
    result=address_decision(house(groups=["GPV5.1"]),state,NOW)
    assert any(x[1]=="address:groups" and x[3] for x in result)


def test_reason_wording_only_does_not_alert_again():
    state={}
    a=house(reason="Планові роботи",kind="1",
            start="08.11.2026 17:00",end="08.11.2026 20:00")
    b=house(reason="Планові роботи з технічного обслуговування",kind="1",
            start="08.11.2026 17:00",end="08.11.2026 20:00")
    first=address_decision(a,state,NOW)
    for _,key,value,_ in first:
        state[key]=value
    follow=address_decision(b,state,NOW)
    assert not [item for item in follow if item[3]]
