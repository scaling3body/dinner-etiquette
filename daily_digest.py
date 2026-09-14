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

CAVEAT: "end of day" is an approximation. This runs once at a fixed time
(see the workflow's cron comment for the exact UTC time and its DST
caveat) -- very late West Coast NBA games that are still in progress or
finish after that point won't be included. Check the dashboard for
anything from after the digest ran.
"""

import json
import os
import sys
from datetime import date, datetime, timezone

import telegram_notifier as notifier

CONFIG_PATH = "config.json"
DASHBOARD_HISTORY_PATH = "dashboard_history.json"


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

    today_str = date.today().isoformat()  # UTC date, approximate -- see module docstring
    today_entries = [
        h for h in history
        if h.get("timestamp", "").startswith(today_str) and h.get("notified", True)
    ]

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
