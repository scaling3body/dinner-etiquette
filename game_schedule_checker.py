"""
game_schedule_checker.py

Run once a day (see .github/workflows/daily_schedule.yml, ~3am ET) to figure
out whether today is actually an NFL game day and/or NBA game day. The
result is written to schedule_windows.json, which main.py reads before
deciding whether to bother checking each sport at all on its 10-minute runs.

NFL: Sleeper doesn't expose a per-day schedule endpoint, so this uses a
day-of-week heuristic (Thu/Sun/Mon are game days; Saturday added from week 15
onward for the December Saturday slate). It's a heuristic, not a real
schedule lookup -- for one-off exceptions (e.g. a Christmas Day game that
doesn't fall on Thu/Sun/Mon/Sat), add the date to `extra_nfl_game_dates` in
config.json and this script will honor it.

NBA: this one IS a real check, not a heuristic -- Sleeper publishes daily
projections only for days that have games, so we just ask for today's
projections and see if anything comes back.

MANUAL RE-CHECK / OVERRIDE: this workflow can also be triggered manually
from the Actions tab (Daily Game Schedule Check -> Run workflow) at any
point, not just at 3am. Its "Force NFL active" / "Force NBA active" checkboxes
bypass the normal detection entirely for that run -- useful if you know
something changed after the 3am check ran (a weather-postponed game moved to
an unexpected day, for instance) and want to override the flag immediately
rather than waiting. A plain re-run with no overrides checked re-runs the
normal checks fresh, which is a real re-check for NBA (live data) but will
reproduce the same result for NFL (the heuristic is date-based, not
data-based) unless you also add the date to extra_nfl_game_dates in
config.json or use the force checkbox.
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import sleeper_client as sleeper

CONFIG_PATH = "config.json"
OUTPUT_PATH = "schedule_windows.json"

ET_ZONE = ZoneInfo("America/New_York")  # stdlib since Python 3.9 -- no extra dependency,
# and handles EST/EDT correctly rather than a fixed offset that drifts an hour
# half the year (and, at the day-boundary, could give the wrong CALENDAR DAY
# entirely -- not just an hour off -- if this were ever run outside the early
# UTC-morning window where the old fixed -5h heuristic happened to still agree
# with actual Eastern time).


def _today_et():
    return datetime.now(ET_ZONE).date()


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def check_nfl_active_today(config):
    state = sleeper.get_nfl_state()
    if not state or state.get("season_type") not in ("regular", "post"):
        return False

    today = _today_et()
    weekday = today.weekday()  # Mon=0 ... Sun=6

    extra_dates = set(config.get("scheduling", {}).get("extra_nfl_game_dates", []))
    if today.isoformat() in extra_dates:
        return True

    week = state.get("week", 0)
    game_weekdays = {3, 6, 0}  # Thursday=3, Sunday=6, Monday=0
    if week and week >= 15:
        game_weekdays.add(5)  # Saturday, common late-season slate

    return weekday in game_weekdays


def check_nba_active_today(config):
    nba_state = sleeper.get_nba_state()
    season = nba_state.get("season") if nba_state else str(_today_et().year)
    today_str = _today_et().isoformat()

    projections = sleeper.get_nba_day_projections(today_str, season)
    return bool(projections)


def main():
    config = load_json(CONFIG_PATH, {})

    force_nfl = os.environ.get("FORCE_NFL_ACTIVE") == "true"
    force_nba = os.environ.get("FORCE_NBA_ACTIVE") == "true"

    nfl_active = True if force_nfl else check_nfl_active_today(config)
    nba_active = True if force_nba else check_nba_active_today(config)

    result = {
        "date": _today_et().isoformat(),
        "nfl_active_today": nfl_active,
        "nba_active_today": nba_active,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2)

    forced_note = []
    if force_nfl:
        forced_note.append("NFL forced")
    if force_nba:
        forced_note.append("NBA forced")
    suffix = f" ({', '.join(forced_note)})" if forced_note else ""
    print(f"Schedule check for {result['date']}: NFL={nfl_active}, NBA={nba_active}{suffix}")


if __name__ == "__main__":
    main()
