"""
main.py

Entry point run on a schedule (see .github/workflows/fantasy_alerts.yml).
Each run:
  1. Checks for football/basketball breakout performances.
  2. For each new breakout, if roster_management is enabled and there's an
     open bench/IR spot, sends a Telegram message with tappable "Add" /
     "No thanks" buttons instead of a plain alert.
  3. Checks Telegram for any button taps since the last run and executes
     the matching pending add.
  4. Expires pending suggestions after suggestion_expiry_minutes.

State files (alerted.json, pending.json, *_cache.json, telegram_offset.json)
are committed back to the repo by the GitHub Actions workflow so they
persist between runs.
"""

import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone

import sleeper_client as sleeper
import scoring
import telegram_notifier as notifier

CONFIG_PATH = "config.json"
ALERTED_PATH = "alerted.json"
PENDING_PATH = "pending.json"
TELEGRAM_OFFSET_PATH = "telegram_offset.json"
PLAYERS_CACHE_NFL = "players_nfl_cache.json"
PLAYERS_CACHE_NBA = "players_nba_cache.json"
DASHBOARD_HISTORY_PATH = "dashboard_history.json"
DASHBOARD_DATA_PATH = "docs/data.json"
SCHEDULE_WINDOWS_PATH = "schedule_windows.json"
MAX_HISTORY = 50


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def get_players_cached(sport, cache_path):
    if os.path.exists(cache_path):
        age_seconds = os.path.getmtime(cache_path)
        import time
        if time.time() - age_seconds < 24 * 3600:
            return load_json(cache_path, {})
    players = sleeper.get_all_players(sport)
    if players:
        save_json(cache_path, players)
        return players
    return load_json(cache_path, {})


def record_history(history, sport, player_name, message):
    history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sport": sport,
        "player_name": player_name,
        "message": message,
    })
    del history[:-MAX_HISTORY]  # keep only the most recent MAX_HISTORY entries


def write_dashboard(history, pending):
    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "recent_alerts": list(reversed(history)),  # newest first
        "pending_suggestions": [
            {
                "code": code,
                "player_name": item["player_name"],
                "sport": item["sport"],
                "created_at": item["created_at"],
            }
            for code, item in pending.items()
        ],
    }
    os.makedirs(os.path.dirname(DASHBOARD_DATA_PATH), exist_ok=True)
    save_json(DASHBOARD_DATA_PATH, data)


def get_yahoo_keys(config, sport):
    """
    League/team keys aren't secret credentials (they can't be used to act on
    your account), but they do identify which specific league/team you're
    in, so they're read from GitHub Secrets first -- same pattern as the
    real credentials -- falling back to config.json only for local testing.
    """
    rm = config.get("roster_management", {})
    if sport == "nfl":
        league_key = os.environ.get("YAHOO_NFL_LEAGUE_KEY") or rm.get("football_league_key")
        team_key = os.environ.get("YAHOO_NFL_TEAM_KEY") or rm.get("football_team_key")
    else:
        league_key = os.environ.get("YAHOO_NBA_LEAGUE_KEY") or rm.get("basketball_league_key")
        team_key = os.environ.get("YAHOO_NBA_TEAM_KEY") or rm.get("basketball_team_key")
    return league_key, team_key


def maybe_suggest_add(config, pending, sport, player_name, team, breakout_msg):
    """
    If roster_management is enabled and there's an open spot, sends a
    message with tappable Add/No-thanks buttons and records it in `pending`.
    Otherwise just sends the plain breakout alert.
    """
    rm = config.get("roster_management", {})
    if not rm.get("enabled"):
        try:
            notifier.send_message(config, breakout_msg)
        except Exception as e:
            print(f"FAILED to send Telegram alert (check TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                  f"and that you've messaged your bot at least once): {e}")
        return

    try:
        import yahoo_client
        league_key, team_key = get_yahoo_keys(config, sport)
        manager = yahoo_client.YahooRosterManager("oauth2.json", sport, league_key, team_key)

        if not manager.get_open_bench_or_ir_slots():
            notifier.send_message(config, breakout_msg)
            return

        match = manager.find_player(player_name, team)
        if not match:
            # Probably already rostered by someone -- just send the FYI alert.
            notifier.send_message(config, breakout_msg)
            return

        code = str(random.randint(1000, 9999))
        text = f"{breakout_msg}\n\nOpen roster spot available -- add {player_name}?"
        buttons = [(f"Add {player_name}", f"add:{code}"), ("No thanks", f"decline:{code}")]
        message_id = notifier.send_message(config, text, buttons=buttons)

        pending[code] = {
            "sport": sport,
            "player_id": match["player_id"],
            "player_name": player_name,
            "message_id": message_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        print(f"Roster management check failed, sending plain alert instead: {e}")
        try:
            notifier.send_message(config, breakout_msg)
        except Exception as e2:
            print(f"FAILED to send Telegram alert (check TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                  f"and that you've messaged your bot at least once): {e2}")


def process_confirmations(config, pending):
    rm = config.get("roster_management", {})
    if not rm.get("enabled"):
        return

    offset_state = load_json(TELEGRAM_OFFSET_PATH, {})
    try:
        taps = notifier.get_new_button_taps(config, offset_state)
    except Exception as e:
        print(f"Could not check Telegram for button taps: {e}")
        taps = []
    save_json(TELEGRAM_OFFSET_PATH, offset_state)

    if taps:
        import yahoo_client

    for tap in taps:
        action, _, code = tap["callback_data"].partition(":")
        item = pending.get(code)
        if not item:
            notifier.acknowledge_tap(config, tap["callback_query_id"], "This suggestion expired.")
            continue

        if action == "decline":
            notifier.edit_message(config, item["message_id"], f"Skipped {item['player_name']}.")
            notifier.acknowledge_tap(config, tap["callback_query_id"])
            del pending[code]
            continue

        sport = item["sport"]
        league_key, team_key = get_yahoo_keys(config, sport)
        manager = yahoo_client.YahooRosterManager("oauth2.json", sport, league_key, team_key)

        try:
            manager.add_player(item["player_id"])
            notifier.edit_message(config, item["message_id"], f"\u2705 Added {item['player_name']}!")
            notifier.acknowledge_tap(config, tap["callback_query_id"], "Added!")
        except Exception as e:
            notifier.edit_message(config, item["message_id"], f"\u274c Failed to add {item['player_name']}: {e}")
            notifier.acknowledge_tap(config, tap["callback_query_id"], "Failed -- see message.")

        del pending[code]

    # Expire old pending suggestions
    expiry_minutes = rm.get("suggestion_expiry_minutes", 180)
    now = datetime.now(timezone.utc)
    for code in list(pending.keys()):
        created = datetime.fromisoformat(pending[code]["created_at"])
        if (now - created) > timedelta(minutes=expiry_minutes):
            item = pending.pop(code)
            try:
                notifier.edit_message(config, item["message_id"], f"Suggestion to add {item['player_name']} expired.")
            except Exception:
                pass


def run_football(config, alerted, pending, players, history):
    if not config["football"]["enabled"]:
        return
    state = sleeper.get_nfl_state()
    if not state:
        print("Could not fetch NFL state, skipping football check.")
        return
    season, week = state.get("season"), state.get("week")
    if not season or not week:
        return

    projections = sleeper.get_nfl_week_projections(season, week)
    stats = sleeper.get_nfl_week_stats(season, week)
    if not projections or not stats:
        print("No football projections/stats available yet this run.")
        return

    key = f"nfl-{season}-{week}"
    alerted.setdefault(key, [])

    for player_id, actual in stats.items():
        if player_id in alerted[key]:
            continue
        proj = projections.get(player_id)
        result = scoring.check_football_breakout(
            actual, proj,
            config["football"]["scoring_settings"],
            config["football"]["pct_threshold"],
            config["football"]["point_floor"],
        )
        if result:
            try:
                p = players.get(player_id, {})
                name = p.get("full_name") or f"Player {player_id}"
                team = p.get("team", "FA")
                msg = notifier.format_football_alert(name, team, result)
                print("ALERT:", msg)
                record_history(history, "nfl", name, msg)
                maybe_suggest_add(config, pending, "nfl", name, team, msg)
            except Exception as e:
                print(f"Error processing breakout for player {player_id}, continuing with the rest: {e}")
            finally:
                alerted[key].append(player_id)


def run_basketball(config, alerted, pending, players, history):
    if not config["basketball"]["enabled"]:
        return
    nba_state = sleeper.get_nba_state()
    season = nba_state.get("season") if nba_state else str(date.today().year)
    today_str = date.today().isoformat()

    projections = sleeper.get_nba_day_projections(today_str, season)
    stats = sleeper.get_nba_day_stats(today_str, season)
    if not projections or not stats:
        print("No basketball projections/stats available yet this run (maybe no games today).")
        return

    key = f"nba-{today_str}"
    alerted.setdefault(key, [])

    cats = config["basketball"]["categories"]
    invert = config["basketball"]["invert_categories"]
    threshold = config["basketball"]["zscore_alert_threshold"]

    for player_id, actual in stats.items():
        if player_id in alerted[key]:
            continue
        proj = projections.get(player_id)
        result = scoring.check_basketball_breakout(
            player_id, actual, proj, projections, cats, invert, threshold
        )
        if result:
            try:
                p = players.get(player_id, {})
                name = p.get("full_name") or f"Player {player_id}"
                team = p.get("team", "FA")
                msg = notifier.format_basketball_alert(name, team, result)
                print("ALERT:", msg)
                record_history(history, "nba", name, msg)
                maybe_suggest_add(config, pending, "nba", name, team, msg)
            except Exception as e:
                print(f"Error processing breakout for player {player_id}, continuing with the rest: {e}")
            finally:
                alerted[key].append(player_id)


def main():
    if not os.path.exists(CONFIG_PATH):
        print(f"Missing {CONFIG_PATH}. Copy config.example.json to config.json and fill it in.")
        sys.exit(1)

    config = load_json(CONFIG_PATH, {})
    alerted = load_json(ALERTED_PATH, {})
    pending = load_json(PENDING_PATH, {})
    history = load_json(DASHBOARD_HISTORY_PATH, [])

    players_nfl = get_players_cached("nfl", PLAYERS_CACHE_NFL)
    players_nba = get_players_cached("nba", PLAYERS_CACHE_NBA)

    try:
        process_confirmations(config, pending)
    except Exception as e:
        print(f"process_confirmations failed, continuing: {e}")

    # Skip a sport's check entirely if today's schedule (written once a day
    # by game_schedule_checker.py) says there's nothing happening. If the
    # file doesn't exist yet (e.g. the daily job hasn't run), default to
    # checking both -- fail safe rather than fail silent.
    schedule = load_json(SCHEDULE_WINDOWS_PATH, {})
    nfl_today = schedule.get("nfl_active_today", True)
    nba_today = schedule.get("nba_active_today", True)

    if nfl_today:
        try:
            run_football(config, alerted, pending, players_nfl, history)
        except Exception as e:
            print(f"run_football failed, continuing to basketball: {e}")
    else:
        print("No NFL games scheduled today -- skipping football check.")

    if nba_today:
        try:
            run_basketball(config, alerted, pending, players_nba, history)
        except Exception as e:
            print(f"run_basketball failed: {e}")
    else:
        print("No NBA games scheduled today -- skipping basketball check.")

    save_json(ALERTED_PATH, alerted)
    save_json(PENDING_PATH, pending)
    save_json(DASHBOARD_HISTORY_PATH, history)
    write_dashboard(history, pending)


if __name__ == "__main__":
    main()
