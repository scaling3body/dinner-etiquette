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
"""

import json
import os
from datetime import date, timezone, timedelta

import sleeper_client as sleeper

CONFIG_PATH = "config.json"
OUTPUT_PATH = "schedule_windows.json"

# Rough Eastern Time "today" -- good enough for a once-a-day check where being
# off by an hour around midnight doesn't matter much. Avoids adding a
# timezone library dependency.
ET_OFFSET_HOURS = -5  # EST; during EDT this is off by an hour, which is fine here


def _today_et():
    return (date.today())  # date.today() is server-local (UTC on GH Actions);
    # close enough for a day-level check -- see note above.


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
    season = nba_state.get("season") if nba_state else str(date.today().year)
    today_str = _today_et().isoformat()

    projections = sleeper.get_nba_day_projections(today_str, season)
    return bool(projections)


def main():
    config = load_json(CONFIG_PATH, {})

    nfl_active = check_nfl_active_today(config)
    nba_active = check_nba_active_today(config)

    result = {
        "date": _today_et().isoformat(),
        "nfl_active_today": nfl_active,
        "nba_active_today": nba_active,
    }
    with open(OUTPUT_PATH, "w") as f:
        json.dump(result, f, indent=2)

    print(f"Schedule check for {result['date']}: NFL={nfl_active}, NBA={nba_active}")


if __name__ == "__main__":
    main()
