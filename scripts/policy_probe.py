"""Read-only verification of notification decisions. No Telegram calls."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from bot import collect_notifications, get_schedule


async def main():
    source, groups = await get_schedule()
    today = datetime.now(ZoneInfo("Europe/Kyiv")).date()
    for group in groups:
        plans = collect_notifications(source, group, today)
        print("BEGIN_POLICY_RESULT")
        print(f"date={today.isoformat()} group={group} count={len(plans)}")
        for key, _payload, kind, parts in plans:
            print(f"kind={kind} key={key}")
            if kind == "day":
                day, spans, update = parts
                print(f"  confirmed_date={day} intervals={len(spans)} update={update}")
            elif kind == "week":
                monday, entries = parts
                print(f"  week_start={monday} days_with_possible_outages={len(entries)}")
        print("END_POLICY_RESULT")


if __name__ == "__main__":
    asyncio.run(main())
