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

The 04:45 UTC schedule reports the previous UTC date, selecting alert
timestamps by their Eastern calendar date. This remains the same reporting
day if Actions delays the morning run past 6am Eastern. Subtracting six
hours from the actual Eastern run time could select the same day on two
consecutive delayed runs. Successful reporting days are also persisted in
digest_state.json so reruns cannot send that day's digest again.

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
DIGEST_STATE_PATH = "digest_state.json"
ET_ZONE_NAME = "America/New_York"


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


def build_digest(today_entries, target_day=None):
    """Format a digest with each player listed once at their highest level."""
    breakouts = collapse_breakouts(today_entries)
    nfl_entries = [(name, level, _projection_summary(entry))
                   for sport, name, level, entry in breakouts if sport == "nfl"]
    nba_entries = [(name, level) for sport, name, level, entry in breakouts if sport == "nba"]

    title = f"Breakout digest for {target_day.isoformat()}" if target_day else "Today's breakout digest"
    lines = [f"\U0001F4CB {title} -- {len(breakouts)} total\n"]

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


def digest_target_day(now=None):
    """Use a stable reporting date throughout the scheduled run's UTC day."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).date() - timedelta(days=1)


def send_daily_digest(config, history, state, now=None):
    now = now or datetime.now(timezone.utc)
    target_day = digest_target_day(now)
    day_key = target_day.isoformat()
    if day_key in state.get("sent_days", {}):
        print(f"Digest for {day_key} already sent -- skipping.")
        return False

    et_zone = ZoneInfo(ET_ZONE_NAME)
    entries = []
    for entry in history:
        ts = entry.get("timestamp")
        if not ts or not entry.get("notified", True):
            continue
        if datetime.fromisoformat(ts).astimezone(et_zone).date() == target_day:
            entries.append(entry)
    if not entries:
        print(f"No breakouts for {day_key} -- not sending a digest.")
        return False

    # Only record delivery after Telegram confirms it. A failed send stays
    # retryable. Workflow concurrency serializes sends and state persistence.
    message_id = notifier.send_message(config, build_digest(entries, target_day))
    state.setdefault("sent_days", {})[day_key] = {
        "sent_at": now.astimezone(timezone.utc).isoformat(), "message_id": message_id,
    }
    print(f"Digest for {day_key} sent.")
    return True


def main():
    if not os.path.exists(CONFIG_PATH):
        print(f"Missing {CONFIG_PATH}. Copy config.example.json to config.json and fill it in.")
        sys.exit(1)

    config = load_json(CONFIG_PATH, {})
    history = load_json(DASHBOARD_HISTORY_PATH, [])
    state = load_json(DIGEST_STATE_PATH, {})

    try:
        if send_daily_digest(config, history, state):
            temp_path = DIGEST_STATE_PATH + ".tmp"
            with open(temp_path, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(temp_path, DIGEST_STATE_PATH)
    except Exception as e:
        print(f"Failed to send digest: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
