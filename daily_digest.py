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
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import telegram_notifier as notifier

CONFIG_PATH = "config.json"
DASHBOARD_HISTORY_PATH = "dashboard_history.json"
ET_ZONE_NAME = "America/New_York"
DAY_BOUNDARY_BUFFER_HOURS = 6


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def _breakout_level(entry):
    """Return the escalation level encoded in an alert-history message."""
    message = entry.get("message", "")
    if "DOUBLE BREAKOUT" in message:
        return 2
    match = re.search(r"\b(\d+)x BREAKOUT\b", message)
    if match:
        return int(match.group(1))
    return 1


def _breakout_label(level):
    if level == 1:
        return "BREAKOUT"
    if level == 2:
        return "DOUBLE BREAKOUT"
    return f"{level}x BREAKOUT"


def _projection_summary(entry):
    """Extract the football projection percentage recorded in an alert message."""
    match = re.search(r"\(([-\d.]+)% of projection\)", entry.get("message", ""))
    if not match:
        return None
    percentage = float(match.group(1))
    return f"{percentage:g}% of projection ({percentage - 100:+g}% over)"


def collapse_breakouts(entries):
    """Keep one entry per player/sport, retaining their highest escalation."""
    collapsed = {}
    for entry in entries:
        key = (entry["sport"], entry["player_name"])
        level = _breakout_level(entry)
        current = collapsed.get(key)
        if current is None or level >= current["level"]:
            collapsed[key] = {"level": level, "entry": entry}
    return [
        (sport, name, details["level"], details["entry"])
        for (sport, name), details in collapsed.items()
    ]


def build_digest(today_entries):
    """Format a digest with each player listed once at their highest level."""
    breakouts = collapse_breakouts(today_entries)
    nfl_entries = [(name, level, _projection_summary(entry))
                   for sport, name, level, entry in breakouts if sport == "nfl"]
    nba_entries = [(name, level) for sport, name, level, entry in breakouts if sport == "nba"]

    lines = [f"\U0001F4CB Today's breakout digest -- {len(breakouts)} total\n"]

    if nfl_entries:
        lines.append(f"\U0001F3C8 Football ({len(nfl_entries)}):")
        for name, level, projection in nfl_entries:
            suffix = f"; {projection}" if projection else ""
            lines.append(f"  \u2022 {name} — {_breakout_label(level)}{suffix}")
        lines.append("")

    if nba_entries:
        lines.append(f"\U0001F3C0 Basketball ({len(nba_entries)}):")
        for name, level in nba_entries:
            lines.append(f"  \u2022 {name} — {_breakout_label(level)}")

    return "\n".join(lines).strip()


def main():
    if not os.path.exists(CONFIG_PATH):
        print(f"Missing {CONFIG_PATH}. Copy config.example.json to config.json and fill it in.")
        sys.exit(1)

    config = load_json(CONFIG_PATH, {})
    history = load_json(DASHBOARD_HISTORY_PATH, [])
    et_zone = ZoneInfo(ET_ZONE_NAME)

    # See module docstring for why this subtracts a buffer rather than just
    # taking "today in ET right now" -- the run itself can already be past
    # ET midnight (during EDT), which would otherwise make it summarize the
    # day about to start instead of the one that just ended.
    target_day = (datetime.now(et_zone) - timedelta(hours=DAY_BOUNDARY_BUFFER_HOURS)).date()

    def _is_target_day(entry):
        ts = entry.get("timestamp")
        if not ts:
            return False
        return datetime.fromisoformat(ts).astimezone(et_zone).date() == target_day

    today_entries = [h for h in history if _is_target_day(h) and h.get("notified", True)]

    if not today_entries:
        print("No breakouts today -- not sending a digest.")
        return

    text = build_digest(today_entries)

    try:
        notifier.send_message(config, text)
        print("Digest sent.")
    except Exception as e:
        print(f"Failed to send digest: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
