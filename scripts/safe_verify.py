"""One-time safe live parser verification; never writes state or sends Telegram."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from bot import get_schedule, collect_notifications, parse_verified, KYIV

async def main():
    source, groups = await get_schedule()
    today = datetime.now(KYIV).date()
    labels = source.preset.time_zone if source.preset else {}
    print("BEGIN_SAFE_VERIFY")
    print("labels=",len(labels),"group=",groups[0])
    if source.fact:
        for ts, group_map in source.fact.days.items():
            d=datetime.fromtimestamp(int(ts), KYIV).date()
            for group in groups:
                slots=group_map.get(group)
                print("date=",d,"group=",group,
                      "fact_slots=",len(slots) if slots else 0,
                      "complete=",set(slots)==set(labels) if slots else False,
                      "parsed=",parse_verified(slots,labels) is not None)
    items=collect_notifications(source, groups[0], today)
    print("candidates=",[(k,kind) for k,_p,kind,_d in items])
    print("END_SAFE_VERIFY")

if __name__ == "__main__":
    asyncio.run(main())
