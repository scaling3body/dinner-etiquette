"""
main.py

Entry point run on a schedule (see .github/workflows/fantasy_alerts.yml).
Each run:
  1. Checks for football/basketball breakout performances, with escalating
     repeat-breakout detection (see scoring.py).
  2. For each breakout, if a Yahoo manager is available, looks up
     availability status and tags the message with game status (Live/Final)
     if resolvable. Players already rostered in your league are silently
     skipped from notification (but still logged) if quiet_rostered_players
     is on -- see maybe_suggest_add() below.
  3. Collects all of this run's alerts and sends them as ONE combined
     Telegram message if there's more than one, instead of a flood of
     separate pings -- see dispatch_alerts().
  4. Checks Telegram for any button taps since the last run and executes
     the matching pending add.
  5. Expires pending suggestions after suggestion_expiry_minutes.

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

GAME_STATUS_DISPLAY = {"in_game": "Live", "complete": "Final", "canceled": "Canceled"}
DEFAULT_SUGGESTION_EXPIRY_MINUTES = 1440  # ~24 hours -- you may not be watching live


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


def record_history(history, sport, player_name, message, notified=True):
    history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sport": sport,
        "player_name": player_name,
        "message": message,
        "notified": notified,  # False when quiet_rostered_players suppressed the Telegram push
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


def build_yahoo_manager(config, sport):
    """
    Builds one YahooRosterManager for the given sport, or returns None if
    roster_management is disabled or setup fails. Build this ONCE per sport
    per run and reuse it across every breakout -- see the performance note
    in yahoo_client.py.
    """
    rm = config.get("roster_management", {})
    if not rm.get("enabled"):
        return None
    try:
        import yahoo_client
        league_key, team_key = get_yahoo_keys(config, sport)
        return yahoo_client.YahooRosterManager("oauth2.json", sport, league_key, team_key)
    except Exception as e:
        print(f"Could not set up Yahoo roster manager for {sport}, alerts will be plain this run: {e}")
        return None


def get_game_status_tag(schedule, team):
    if not schedule or not team:
        return None
    status = sleeper.get_team_game_status(schedule, team)
    return GAME_STATUS_DISPLAY.get(status)  # None for pre_game or unresolved -- nothing useful to show


def evaluate_breakout_item(config, sport, player_name, team, breakout_msg, manager):
    """
    Decides what (if anything) should be sent for one breakout, WITHOUT
    sending it yet -- results are collected across a whole run and sent
    together by dispatch_alerts(), so a busy check doesn't fire off a flood
    of separate Telegram messages.

    Returns a dict: {"send": bool, "text": str, "addable": bool,
                      "player_id": str|None, "player_name": str, "sport": str}
    "send" is False when quiet_rostered_players suppressed this one.
    """
    rm = config.get("roster_management", {})

    if manager is None:
        return {"send": True, "text": breakout_msg, "addable": False,
                "player_id": None, "player_name": player_name, "sport": sport}

    try:
        status_label, match = manager.get_player_status(player_name, team)
        is_rostered = match is None

        if is_rostered and rm.get("quiet_rostered_players", True):
            return {"send": False, "text": breakout_msg, "addable": False,
                    "player_id": None, "player_name": player_name, "sport": sport}

        text = f"{breakout_msg}\n\n{status_label}"
        has_open_slot = manager.get_open_bench_or_ir_slots()
        addable = bool(has_open_slot and match)
        if addable:
            text += f" -- add {player_name}?"

        return {"send": True, "text": text, "addable": addable,
                "player_id": match["player_id"] if match else None,
                "player_name": player_name, "sport": sport}

    except Exception as e:
        print(f"Roster management check failed for {player_name}, sending plain alert instead: {e}")
        return {"send": True, "text": breakout_msg, "addable": False,
                "player_id": None, "player_name": player_name, "sport": sport}


def dispatch_alerts(config, pending, batch):
    """
    Sends everything collected this run as ONE message if there's more than
    one item, or a normal single message (with edit-on-resolve behavior) if
    there's exactly one. Batched messages get a "batched": True flag on
    their pending entries so process_confirmations() knows to send a fresh
    follow-up message on resolve instead of editing the shared message
    (editing would blow away the other players' info in it).
    """
    items = [b for b in batch if b["send"]]
    if not items:
        return

    if len(items) == 1:
        item = items[0]
        if item["addable"]:
            code = str(random.randint(1000, 9999))
            buttons = [[(f"Add {item['player_name']}", f"add:{code}"), ("No thanks", f"decline:{code}")]]
            try:
                message_id = notifier.send_message(config, item["text"], buttons=buttons)
                pending[code] = {
                    "sport": item["sport"], "player_id": item["player_id"],
                    "player_name": item["player_name"], "message_id": message_id,
                    "batched": False, "created_at": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as e:
                print(f"FAILED to send Telegram alert: {e}")
        else:
            try:
                notifier.send_message(config, item["text"])
            except Exception as e:
                print(f"FAILED to send Telegram alert: {e}")
        return

    header = f"\U0001F514 {len(items)} breakouts this check:\n\n"
    text = header + "\n\n".join(i["text"] for i in items)
    button_rows = []
    codes_for_items = []
    for i in items:
        if i["addable"]:
            code = str(random.randint(1000, 9999))
            button_rows.append([(f"Add {i['player_name']}", f"add:{code}"), ("No thanks", f"decline:{code}")])
            codes_for_items.append((code, i))

    try:
        message_id = notifier.send_message(config, text, buttons=button_rows or None)
        for code, i in codes_for_items:
            pending[code] = {
                "sport": i["sport"], "player_id": i["player_id"],
                "player_name": i["player_name"], "message_id": message_id,
                "batched": True, "created_at": datetime.now(timezone.utc).isoformat(),
            }
    except Exception as e:
        print(f"FAILED to send batched Telegram alert: {e}")


def process_confirmations(config, pending, managers):
    rm = config.get("roster_management", {})
    if not rm.get("enabled") or not pending:
        return

    offset_state = load_json(TELEGRAM_OFFSET_PATH, {})
    try:
        taps = notifier.get_new_button_taps(config, offset_state)
    except Exception as e:
        print(f"Could not check Telegram for button taps: {e}")
        taps = []
    save_json(TELEGRAM_OFFSET_PATH, offset_state)

    for tap in taps:
        action, _, code = tap["callback_data"].partition(":")
        item = pending.get(code)
        if not item:
            notifier.acknowledge_tap(config, tap["callback_query_id"], "This suggestion expired.")
            continue

        batched = item.get("batched", False)

        if action == "decline":
            if not batched:
                notifier.edit_message(config, item["message_id"], f"Skipped {item['player_name']}.")
            else:
                notifier.send_message(config, f"Skipped {item['player_name']}.")
            notifier.acknowledge_tap(config, tap["callback_query_id"])
            del pending[code]
            continue

        manager = managers.get(item["sport"])
        if manager is None:
            msg = f"Couldn't add {item['player_name']} -- Yahoo connection unavailable this run."
            if not batched:
                notifier.edit_message(config, item["message_id"], msg)
            else:
                notifier.send_message(config, msg)
            notifier.acknowledge_tap(config, tap["callback_query_id"], "Failed -- see message.")
            del pending[code]
            continue

        try:
            manager.add_player(item["player_id"])
            msg = f"\u2705 Added {item['player_name']}!"
            if not batched:
                notifier.edit_message(config, item["message_id"], msg)
            else:
                notifier.send_message(config, msg)
            notifier.acknowledge_tap(config, tap["callback_query_id"], "Added!")
        except Exception as e:
            msg = f"\u274c Failed to add {item['player_name']}: {e}"
            if not batched:
                notifier.edit_message(config, item["message_id"], msg)
            else:
                notifier.send_message(config, msg)
            notifier.acknowledge_tap(config, tap["callback_query_id"], "Failed -- see message.")

        del pending[code]

    # Expire old pending suggestions
    expiry_minutes = rm.get("suggestion_expiry_minutes", DEFAULT_SUGGESTION_EXPIRY_MINUTES)
    now = datetime.now(timezone.utc)
    for code in list(pending.keys()):
        created = datetime.fromisoformat(pending[code]["created_at"])
        if (now - created) > timedelta(minutes=expiry_minutes):
            item = pending.pop(code)
            if not item.get("batched", False):
                try:
                    notifier.edit_message(config, item["message_id"],
                                           f"Suggestion to add {item['player_name']} expired.")
                except Exception:
                    pass
            # Batched expiries aren't announced individually -- the shared
            # message already carries other players' info and shouldn't be
            # overwritten just because one of several suggestions expired.


def _ensure_new_alerted_format(value):
    """
    Old format: alerted[key] was a flat list of player_ids (fully blocked
    after one alert). New format: a dict of player_id -> {"count", "baseline"}
    supporting repeat/escalating breakouts. Migrates old entries in place --
    their baseline is unknown, so it gets silently backfilled on the next
    check for that player rather than guessed (see run_football/run_basketball).
    """
    if isinstance(value, list):
        return {pid: {"count": 1, "baseline": None} for pid in value}
    return value


def run_football(config, alerted, players, history, manager, batch):
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

    schedule = sleeper.get_schedule("nfl", season, state.get("season_type", "regular"))

    extreme_multiplier = config["football"].get("extreme_multiplier", scoring.DEFAULT_EXTREME_MULTIPLIER)
    key = f"nfl-{season}-{week}"
    alerted[key] = _ensure_new_alerted_format(alerted.get(key, {}))

    for player_id, actual in stats.items():
        proj = projections.get(player_id)
        prior = alerted[key].get(player_id)
        prior_count = prior["count"] if prior else 0
        prior_baseline = prior["baseline"] if prior else None

        if prior_count > 0 and prior_baseline is None:
            # Migrated from the old format -- we don't know their actual
            # baseline, so backfill it silently this run without alerting,
            # then resume normal escalation checks from here on.
            actual_pts = scoring.calc_fantasy_points(actual, config["football"]["scoring_settings"])
            alerted[key][player_id] = {"count": prior_count, "baseline": actual_pts}
            continue

        result = scoring.football_breakout_level(
            actual, proj,
            config["football"]["scoring_settings"],
            config["football"]["pct_threshold"],
            config["football"]["point_floor"],
            prior_count, prior_baseline,
            extreme_multiplier,
        )

        if result["is_breakout"]:
            try:
                p = players.get(player_id, {})
                name = p.get("full_name") or f"Player {player_id}"
                team = p.get("team", "FA")
                label = scoring.breakout_label(result["count"], result["is_extreme"])
                status_tag = get_game_status_tag(schedule, team)
                msg = notifier.format_football_alert(name, team, result, label=label, status_tag=status_tag)
                print("ALERT:", msg)
                item = evaluate_breakout_item(config, "nfl", name, team, msg, manager)
                record_history(history, "nfl", name, msg, notified=item["send"])
                batch.append(item)
            except Exception as e:
                print(f"Error processing breakout for player {player_id}, continuing with the rest: {e}")
            finally:
                alerted[key][player_id] = {"count": result["count"], "baseline": result["baseline"]}


def run_basketball(config, alerted, players, history, manager, batch):
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

    schedule = sleeper.get_schedule("nba", season, "regular")  # NBA schedule shape is unverified -- may be None

    extreme_multiplier = config["basketball"].get("extreme_multiplier", scoring.DEFAULT_EXTREME_MULTIPLIER)
    key = f"nba-{today_str}"
    alerted[key] = _ensure_new_alerted_format(alerted.get(key, {}))

    cats = config["basketball"]["categories"]
    invert = config["basketball"]["invert_categories"]
    threshold = config["basketball"]["zscore_alert_threshold"]

    for player_id, actual in stats.items():
        proj = projections.get(player_id)
        prior = alerted[key].get(player_id)
        prior_count = prior["count"] if prior else 0
        prior_baseline = prior["baseline"] if prior else None

        if prior_count > 0 and prior_baseline is None:
            # Migrated from the old format -- backfill silently this run,
            # no alert, then resume normal escalation checks from here.
            total_z, _ = scoring.compute_zscore(actual or {}, proj or {}, projections, cats, invert)
            alerted[key][player_id] = {"count": prior_count, "baseline": round(total_z, 2)}
            continue

        result = scoring.basketball_breakout_level(
            actual, proj, projections, cats, invert, threshold, prior_count, prior_baseline,
            extreme_multiplier,
        )

        if result["is_breakout"]:
            try:
                p = players.get(player_id, {})
                name = p.get("full_name") or f"Player {player_id}"
                team = p.get("team", "FA")
                label = scoring.breakout_label(result["count"], result["is_extreme"])
                status_tag = get_game_status_tag(schedule, team)
                msg = notifier.format_basketball_alert(name, team, result, label=label, status_tag=status_tag)
                print("ALERT:", msg)
                item = evaluate_breakout_item(config, "nba", name, team, msg, manager)
                record_history(history, "nba", name, msg, notified=item["send"])
                batch.append(item)
            except Exception as e:
                print(f"Error processing breakout for player {player_id}, continuing with the rest: {e}")
            finally:
                alerted[key][player_id] = {"count": result["count"], "baseline": result["baseline"]}


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

    # Build one Yahoo manager per sport for this whole run (not one per
    # breakout) -- each caches its own free-agent scan and open-slot check,
    # so reusing them across every alert and every confirmation avoids
    # re-hitting Yahoo's API repeatedly. None if roster_management is off
    # or setup fails.
    managers = {
        "nfl": build_yahoo_manager(config, "nfl"),
        "nba": build_yahoo_manager(config, "nba"),
    }

    try:
        process_confirmations(config, pending, managers)
    except Exception as e:
        print(f"process_confirmations failed, continuing: {e}")

    # Skip a sport's check entirely if today's schedule (written once a day
    # by game_schedule_checker.py) says there's nothing happening. If the
    # file doesn't exist yet (e.g. the daily job hasn't run), default to
    # checking both -- fail safe rather than fail silent.
    schedule_windows = load_json(SCHEDULE_WINDOWS_PATH, {})
    nfl_today = schedule_windows.get("nfl_active_today", True)
    nba_today = schedule_windows.get("nba_active_today", True)

    batch = []  # collected across both sports, sent as one message by dispatch_alerts()

    if nfl_today:
        try:
            run_football(config, alerted, players_nfl, history, managers["nfl"], batch)
        except Exception as e:
            print(f"run_football failed, continuing to basketball: {e}")
    else:
        print("No NFL games scheduled today -- skipping football check.")

    if nba_today:
        try:
            run_basketball(config, alerted, players_nba, history, managers["nba"], batch)
        except Exception as e:
            print(f"run_basketball failed: {e}")
    else:
        print("No NBA games scheduled today -- skipping basketball check.")

    try:
        dispatch_alerts(config, pending, batch)
    except Exception as e:
        print(f"dispatch_alerts failed: {e}")

    save_json(ALERTED_PATH, alerted)
    save_json(PENDING_PATH, pending)
    save_json(DASHBOARD_HISTORY_PATH, history)
    write_dashboard(history, pending)


if __name__ == "__main__":
    main()
