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

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from state_store import load, save

import telegram_notifier as notifier

CONFIG_PATH = "config.json"
DASHBOARD_HISTORY_PATH = "dashboard_history.json"
DIGEST_STATE_PATH = "digest_state.json"
ET_ZONE_NAME = "America/New_York"


def load_json(path, default):
    return load(path, default)


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
        key = (entry["sport"], entry["player_name"], entry.get("game_id") or entry.get("game_date", ""))
        level = _breakout_level(entry)
        current = collapsed.get(key)
        priority = {"breakout": 2, "trend": 1, "opportunity": 0}
        strength = (priority.get(entry.get("kind", "breakout"), 0), level, entry.get("score") or 0)
        old_strength = (priority.get(current["entry"].get("kind", "breakout"), 0), current["level"], current["entry"].get("score") or 0) if current else None
        if current is None or strength >= old_strength:
            collapsed[key] = {"level": level, "entry": entry}
    return [
        (sport, name, details["level"], details["entry"])
        for (sport, name, game), details in collapsed.items()
    ]


def build_digest(today_entries, target_day=None):
    """Format a digest with each player listed once at their highest level."""
    breakouts = collapse_breakouts(today_entries)
    title = f"Breakout digest for {target_day.isoformat()}" if target_day else "Today's breakout digest"
    lines = [f"\U0001F4CB {title} -- {len(breakouts)} total\n"]
    for sport, title in (("nfl", "🏈 Football"), ("nba", "🏀 Basketball")):
        rows = [(name, level, entry) for kind, name, level, entry in breakouts if kind == sport]
        if not rows:
            continue
        lines.append(f"{title} ({len(rows)}):")
        grouped = {}
        for row in rows:
            grouped.setdefault(row[2].get("game_label") or "Other performances", []).append(row)
        for label, group in grouped.items():
            lines.append(label)
            group.sort(key=lambda row: (row[1], row[2].get("score") or 0), reverse=True)
            for name, level, entry in group:
                kind = entry.get("kind", "breakout")
                detail = _breakout_label(level) if kind == "breakout" else kind.upper()
                projection = _projection_summary(entry) if sport == "nfl" else None
                if projection:
                    detail += f"; {projection}"
                stats = entry.get("actual_stats") or {}
                if sport == "nba" and stats:
                    detail += f"; {stats.get('pts', 0):g} PTS / {stats.get('reb', 0):g} REB / {stats.get('ast', 0):g} AST"
                    if entry.get("score") is not None:
                        detail += f"; score {entry['score']:g}"
                lines.append(f"  • {name} — {detail}")
        lines.append("")

    return "\n".join(lines).strip()


def digest_target_day(now=None):
    """Use a stable reporting date throughout the scheduled run's UTC day."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).date() - timedelta(days=1)


def send_daily_digest(config, history, state, now=None, persist=None):
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
        entry_day = entry.get("game_date") or datetime.fromisoformat(ts).astimezone(et_zone).date().isoformat()
        if entry_day == day_key:
            entries.append(entry)
    if not entries and day_key not in state.get("in_progress", {}):
        print(f"No breakouts for {day_key} -- not sending a digest.")
        return False

    # Only record delivery after Telegram confirms it. A failed send stays
    # retryable. Workflow concurrency serializes sends and state persistence.
    plan = state.setdefault("in_progress", {}).setdefault(day_key, {
        "parts": notifier.split_text(build_digest(entries, target_day)), "sent": {},
    })
    if persist:
        persist()
    for index, part in enumerate(plan["parts"]):
        if str(index) in plan["sent"]:
            continue
        plan["sent"][str(index)] = notifier.send_message(config, part)
        if persist:
            persist()
    state.setdefault("sent_days", {})[day_key] = {
        "sent_at": now.astimezone(timezone.utc).isoformat(),
        "message_id": plan["sent"][str(len(plan["parts"]) - 1)],
    }
    state["in_progress"].pop(day_key, None)
    if persist:
        persist()
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
        send_daily_digest(config, history, state, persist=lambda: save(DIGEST_STATE_PATH, state))
    except Exception as e:
        print(f"Failed to send digest: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
