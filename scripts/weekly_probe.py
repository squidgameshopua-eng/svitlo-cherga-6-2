"""One-off read-only weekly forecast fetch. Does not use Telegram or bot.py."""
import asyncio
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dtek_client import DtekClient
from dtek_client.models import PresetSchedule, FactSchedule
from dtek_client.browser_auth import get_cleared_cookies
from curl_cffi.requests import AsyncSession

URL="https://www.dtek-dnem.com.ua/ua/shutdowns"
AJAX="https://www.dtek-dnem.com.ua/ua/ajax"
KYIV=ZoneInfo("Europe/Kyiv")
GROUP="GPV6.2"


def minutes(s):
    s=str(s).replace("–","-").replace("—","-").replace(" ","")
    m=re.fullmatch(r"(\d{1,2})(?::(\d{2}))?-(\d{1,2})(?::(\d{2}))?",s)
    if not m: raise ValueError(f"unrecognized slot: {s!r}")
    h1,m1,h2,m2=m.groups()
    a=int(h1)*60+int(m1 or 0)
    b=int(h2)*60+int(m2 or 0)
    if a>=b or a<0 or b>1440: raise ValueError(f"invalid slot: {s!r}")
    return a,b


def collapse(slots, labels):
    seg=[]
    for key,status in slots.items():
        a,b=minutes(labels[str(key)])
        v=getattr(status,"value",str(status))
        mid=(a+b)//2
        parts={"no":[(a,b,"off")],"yes":[],
            "first":[(a,mid,"off")],"second":[(mid,b,"off")],
            "maybe":[(a,b,"possible")],"mfirst":[(a,mid,"possible")],
            "msecond":[(mid,b,"possible")]}
        if v not in parts: raise ValueError(f"unknown status {v} in slot {key}")
        seg.extend(parts[v])
    seg.sort(key=lambda t:(t[0],t[1],t[2]))
    out=[]
    for a,b,label in seg:
        if out and out[-1][2]==label and out[-1][1]==a:out[-1]=(out[-1][0],b,label)
        else:out.append((a,b,label))
    return [{"start":f"{a//60:02d}:{a%60:02d}", "end":f"{b//60:02d}:{b%60:02d}","type":t}
            for a,b,t in out]


async def query(c):
    raw=await c._post({"method":"checkDisconUpdate","update":"01.01.2000 00:00"})
    p=PresetSchedule.model_validate(raw.get("preset") or {})
    f=FactSchedule.model_validate(raw["fact"]) if isinstance(raw.get("fact"),dict) else None
    if not p.is_active or GROUP not in p.groups:raise RuntimeError("weekly forecast missing or disabled")
    return p,f


async def run():
    try:
        async with DtekClient("dnem",ajax_url=AJAX) as c:p,f=await query(c)
    except Exception as ex:
        print("Direct API rejected",type(ex).__name__)
        cookies,csrf=await get_cleared_cookies(URL)
        headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                 "Origin":"https://www.dtek-dnem.com.ua","Referer":URL,
                 "Accept":"application/json, text/javascript, */*; q=0.01",
                 "X-Requested-With":"XMLHttpRequest"}
        if csrf:headers["X-CSRF-Token"]=csrf
        async with DtekClient("dnem",ajax_url=AJAX,
            session=AsyncSession(timeout=20,cookies=cookies,headers=headers,
                                 impersonate="chrome120")) as c:
            p,f=await query(c)

    today=datetime.now(KYIV).date()
    monday=today-timedelta(days=today.weekday())
    res={"fetched_at":datetime.now(KYIV).isoformat(),"week_start":monday.isoformat(),
         "group":GROUP,"region":"dnem","forecast":[],"confirmed":[]}
    grp=p.groups[GROUP]
    for day_idx in range(1,8):
        dt=monday+timedelta(days=day_idx-1)
        w=grp.get_day(day_idx)
        res["forecast"].append({"date":dt.isoformat(),"weekday":day_idx,"slots":len(w.slots) if w else 0,
            "intervals":collapse(w.slots,p.time_zone) if w else None})
    if f:
        res["fact_updated"]=f.update
        for stamp,day_map in f.days.items():
            if GROUP not in day_map: continue
            dt=datetime.fromtimestamp(int(stamp),KYIV).date()
            res["confirmed"].append({"date":dt.isoformat(),
                "intervals":collapse(day_map[GROUP],p.time_zone)})
    print("BEGIN_DTEK_WEEKLY_JSON")
    print(json.dumps(res,ensure_ascii=False,indent=2))
    print("END_DTEK_WEEKLY_JSON")


if __name__=="__main__":
    asyncio.run(run())
