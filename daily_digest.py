"""
daily_digest.py

Run once near the end of the typical game day (see
.github/workflows/daily_digest.yml) to send ONE summary message of
everything that broke out today, for a single catch-up read instead of
piecing it together from scattered pings throughout the day.

Only includes entries that were actually sent to you (notified=True) --
players quiet_rostered_players suppressed don't clutter the digest either,
same as they didn't clutter your day.

Sends nothing if there were no breakouts today, rather than a "nothing
happened" message every single day.

"Today" is matched against each entry's Eastern calendar day (converted
from its stored UTC timestamp), not a raw UTC date string -- most of a
game day's activity happens in UTC hours that have already rolled past
midnight relative to the US, so a naive UTC-date comparison would miss
most of a real day's entries.

WHICH DAY COUNTS AS "TODAY": the workflow's cron is a fixed UTC time that
lands at very different ET clock times depending on DST -- roughly 11:45pm
ET during EST, but roughly 00:45am ET (already past ET midnight) during
EDT. A naive "today in ET, right now" would therefore summarize the wrong
day for half the year: during EDT the run has already rolled into the
NEXT ET calendar day before it fires, so "today" would mean tomorrow, not
the game day just finished. To dodge that without a DST-dependent branch,
the target day is the ET calendar date as of 6 hours before this runs --
a buffer comfortably inside the actual game day under both EST's ~11:45pm
firing and EDT's ~00:45am firing, but small enough it can't reach back
into a THIRD, older uninvolved day.

CAVEAT: "end of day" is still an approximation in the other direction --
this runs once at a fixed time (see the workflow's cron comment), so very
late West Coast NBA games that are still in progress or finish after that
point won't be included. Check the dashboard for anything from after the
digest ran.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import telegram_notifier as notifier

CONFIG_PATH = "config.json"
DASHBOARD_HISTORY_PATH = "dashboard_history.json"
ET_ZONE = ZoneInfo("America/New_York")
DAY_BOUNDARY_BUFFER_HOURS = 6


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def main():
    if not os.path.exists(CONFIG_PATH):
        print(f"Missing {CONFIG_PATH}. Copy config.example.json to config.json and fill it in.")
        sys.exit(1)

    config = load_json(CONFIG_PATH, {})
    history = load_json(DASHBOARD_HISTORY_PATH, [])

    # See module docstring for why this subtracts a buffer rather than just
    # taking "today in ET right now" -- the run itself can already be past
    # ET midnight (during EDT), which would otherwise make it summarize the
    # day about to start instead of the one that just ended.
    target_day = (datetime.now(ET_ZONE) - timedelta(hours=DAY_BOUNDARY_BUFFER_HOURS)).date()

    def _is_target_day(entry):
        ts = entry.get("timestamp")
        if not ts:
            return False
        return datetime.fromisoformat(ts).astimezone(ET_ZONE).date() == target_day

    today_entries = [h for h in history if _is_target_day(h) and h.get("notified", True)]

    if not today_entries:
        print("No breakouts today -- not sending a digest.")
        return

    nfl_entries = [h for h in today_entries if h["sport"] == "nfl"]
    nba_entries = [h for h in today_entries if h["sport"] == "nba"]

    lines = [f"\U0001F4CB Today's breakout digest -- {len(today_entries)} total\n"]

    if nfl_entries:
        lines.append(f"\U0001F3C8 Football ({len(nfl_entries)}):")
        for h in nfl_entries:
            lines.append(f"  \u2022 {h['player_name']}")
        lines.append("")

    if nba_entries:
        lines.append(f"\U0001F3C0 Basketball ({len(nba_entries)}):")
        for h in nba_entries:
            lines.append(f"  \u2022 {h['player_name']}")

    text = "\n".join(lines).strip()

    try:
        notifier.send_message(config, text)
        print("Digest sent.")
    except Exception as e:
        print(f"Failed to send digest: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
