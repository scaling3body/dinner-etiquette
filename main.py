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
import re
import sys
from datetime import date, datetime, timedelta, timezone

import sleeper_client as sleeper
import scoring
import telegram_notifier as notifier
import health

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


def write_dashboard(history, pending, health_state, config):
    hc = config.get("health_check", {})
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
        "health": health.summary_for_dashboard(health_state),
        "health_failure_threshold": hc.get("failure_alert_threshold", health.FAILURE_ALERT_THRESHOLD),
    }
    os.makedirs(os.path.dirname(DASHBOARD_DATA_PATH), exist_ok=True)
    save_json(DASHBOARD_DATA_PATH, data)


def maybe_send_failure_alert(config, health_state, component, friendly_name):
    """
    Sends exactly one Telegram alert per ongoing outage once a component
    crosses the failure threshold -- see health.py for the one-per-outage
    logic. If the failing component IS Telegram, this attempt might not
    arrive either; that's an accepted limitation (see health.py's
    docstring) -- the dashboard's health section is the real fallback for
    a Telegram-specific outage.
    """
    threshold = config.get("health_check", {}).get("failure_alert_threshold", health.FAILURE_ALERT_THRESHOLD)
    if health.should_notify(health_state, component, threshold):
        c = health_state.get(component, {})
        msg = (f"\u26A0\uFE0F {friendly_name} has failed {c.get('consecutive_failures')} checks in a row "
               f"(last success: {c.get('last_success') or 'never'}). Check the Actions log.")
        try:
            notifier.send_message(config, msg)
        except Exception as e:
            print(f"Also failed to send the failure-alert itself: {e}")


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


def build_yahoo_manager(config, sport, health_state):
    """
    Builds one YahooRosterManager for the given sport, or returns None if
    roster_management is disabled, the circuit breaker is open (too many
    recent consecutive failures -- see health.py), or setup fails. Build
    this ONCE per sport per run and reuse it across every breakout -- see
    the performance note in yahoo_client.py.

    Does a cheap validation call (get_open_bench_or_ir_slots(), already
    cached) right after building so a broken OAuth token or bad league key
    is caught here as a real failure, not just "OAuth2() didn't throw."
    """
    rm = config.get("roster_management", {})
    if not rm.get("enabled"):
        return None

    if health.yahoo_circuit_is_open(health_state):
        print(f"Yahoo circuit breaker is open (recent repeated failures) -- skipping {sport} this run.")
        return None

    try:
        import yahoo_client
        league_key, team_key = get_yahoo_keys(config, sport)
        manager = yahoo_client.YahooRosterManager("oauth2.json", sport, league_key, team_key)
        manager.get_open_bench_or_ir_slots()  # validation call, also warms the cache
        health.record_success(health_state, "yahoo")
        return manager
    except Exception as e:
        print(f"Could not set up Yahoo roster manager for {sport}, alerts will be plain this run: {e}")
        failures = health.record_failure(health_state, "yahoo")
        maybe_send_failure_alert(config, health_state, "yahoo", "Yahoo")
        hc = config.get("health_check", {})
        breaker_threshold = hc.get("yahoo_breaker_threshold", health.YAHOO_BREAKER_THRESHOLD)
        breaker_cooldown = hc.get("yahoo_breaker_cooldown_minutes", health.YAHOO_BREAKER_COOLDOWN_MINUTES)
        if failures >= breaker_threshold:
            health.open_yahoo_circuit(health_state, breaker_cooldown)
            print(f"Yahoo has failed {failures} times in a row -- opening circuit breaker "
                  f"for {breaker_cooldown} minutes.")
        return None


def get_game_status_tag(schedule, team, week=None):
    if not schedule or not team:
        return None
    status = sleeper.get_team_game_status(schedule, team, week)
    return GAME_STATUS_DISPLAY.get(status)  # None for pre_game or unresolved -- nothing useful to show


def find_starter_injury_context(players, breakout_player):
    """
    If the breakout player has a teammate ranked ahead of them on the depth
    chart (same team, same position, lower depth_chart_order) who's
    currently listed with a non-null injury_status, returns a short note
    like "May be filling in for {name} (Questionable)". This is exactly
    the context that turns "random breakout" into "understand why, and
    whether it's likely to continue" -- often the single most useful piece
    of information for an add decision. Returns None if there's no clear
    signal (missing depth chart data, or the ranked-ahead teammate is
    healthy).
    """
    team = breakout_player.get("team")
    position = breakout_player.get("position")
    my_depth = breakout_player.get("depth_chart_order")
    if not team or not position or my_depth is None:
        return None

    for p in players.values():
        if p.get("team") != team or p.get("position") != position:
            continue
        other_depth = p.get("depth_chart_order")
        if other_depth is None or other_depth >= my_depth:
            continue  # not ranked ahead of the breakout player
        injury = p.get("injury_status")
        if injury:
            name = p.get("full_name") or "a teammate"
            return f"May be filling in for {name} ({injury})"
    return None


def evaluate_breakout_item(config, sport, player_name, team, breakout_msg, manager, health_state, position=None):
    """
    Decides what (if anything) should be sent for one breakout, WITHOUT
    sending it yet -- results are collected across a whole run and sent
    together by dispatch_alerts(), so a busy check doesn't fire off a flood
    of separate Telegram messages.

    Returns a dict: {"send": bool, "text": str, "addable": bool,
                      "player_id": str|None, "player_name": str, "sport": str}
    "send" is False when quiet_rostered_players suppressed this one.
    position, if given, only helps disambiguate a rare same-name collision
    on the Yahoo side (see find_player in yahoo_client.py) -- never
    required for a normal match.
    """
    rm = config.get("roster_management", {})

    if manager is None:
        return {"send": True, "text": breakout_msg, "addable": False,
                "player_id": None, "player_name": player_name, "sport": sport}

    try:
        status_label, match = manager.get_player_status(player_name, team, position)
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
        # Deliberately record_failure only, not record_success anywhere in
        # this function -- a per-player failure here shouldn't be masked by
        # other players in the same run succeeding, and connection-level
        # success is already tracked once per run in build_yahoo_manager.
        health.record_failure(health_state, "yahoo")
        maybe_send_failure_alert(config, health_state, "yahoo", "Yahoo")
        return {"send": True, "text": breakout_msg, "addable": False,
                "player_id": None, "player_name": player_name, "sport": sport}


def _send_single_alert(config, pending, item, health_state):
    """Sends one item as its own message, with edit-on-resolve buttons if addable."""
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
            health.record_success(health_state, "telegram")
        except Exception as e:
            print(f"FAILED to send Telegram alert: {e}")
            health.record_failure(health_state, "telegram")
            maybe_send_failure_alert(config, health_state, "telegram", "Telegram")
    else:
        try:
            notifier.send_message(config, item["text"])
            health.record_success(health_state, "telegram")
        except Exception as e:
            print(f"FAILED to send Telegram alert: {e}")
            health.record_failure(health_state, "telegram")
            maybe_send_failure_alert(config, health_state, "telegram", "Telegram")


def dispatch_alerts(config, pending, batch, health_state):
    """
    Sends each of this run's alerts as its own message (with edit-on-resolve
    buttons if addable) when there are fewer than notifications.batch_threshold
    of them (default 3) -- below that, separate pings feel more immediate
    than a combined message. At or above the threshold, everything gets
    combined into ONE message instead of a flood of separate pings, with one
    Add/No-thanks button row per addable player. Batched messages get a
    "batched": True flag on their pending entries so process_confirmations()
    knows to send a fresh follow-up message on resolve instead of editing
    the shared message (editing would blow away the other players' info in it).
    """
    items = [b for b in batch if b["send"]]
    if not items:
        return

    threshold = config.get("notifications", {}).get("batch_threshold", 3)

    if len(items) < threshold:
        for item in items:
            _send_single_alert(config, pending, item, health_state)
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
        health.record_success(health_state, "telegram")
    except Exception as e:
        print(f"FAILED to send batched Telegram alert: {e}")
        health.record_failure(health_state, "telegram")
        maybe_send_failure_alert(config, health_state, "telegram", "Telegram")


def process_confirmations(config, pending, managers, taps):
    """Handles Add/No-thanks button taps. Expiry of old pending suggestions
    also happens here, on every run, regardless of whether any taps came in."""
    rm = config.get("roster_management", {})

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


CHECK_COMMAND_RE = re.compile(r"^/check\s+(.+)$", re.IGNORECASE)
TIER_COMMAND_RE = re.compile(r"^/tier\s+(\d+)\s*$", re.IGNORECASE)
VALID_CHECK_POSITIONS = {"QB", "RB", "WR", "TE", "FLEX", "K", "DST"}
POSITION_ALIASES = {
    "DEF": "DST", "DEFENSE": "DST",
    "PK": "K", "KICKER": "K",
    "FLX": "FLEX",
}
DEFAULT_TIER_THRESHOLD = 6
TIER_THRESHOLD_PATH = "tier_threshold_state.json"
DEFAULT_CHECK_COUNT = 3
MAX_CHECK_COUNT = 10


def _parse_check_args(arg_str):
    """
    Parses everything after "/check " -- position is required and first;
    an optional count and/or the literal word "refresh" can follow in
    either order, e.g. "/check RB", "/check RB 5", "/check DEF refresh",
    "/check FLX 5 refresh". Returns (position, count, refresh) or None if
    there's no position token at all.
    """
    tokens = arg_str.strip().split()
    if not tokens:
        return None
    position_token = tokens[0].upper()
    position = POSITION_ALIASES.get(position_token, position_token)
    count = DEFAULT_CHECK_COUNT
    refresh = False
    for tok in tokens[1:]:
        if tok.lower() == "refresh":
            refresh = True
        elif tok.isdigit():
            count = max(1, min(MAX_CHECK_COUNT, int(tok)))
    return position, count, refresh


def _find_sleeper_player_by_name(players, name):
    """Best-effort name match into the Sleeper player cache, for injury context only."""
    target = re.sub(r"[^a-z]", "", name.lower())
    for p in players.values():
        pname = p.get("full_name") or ""
        if re.sub(r"[^a-z]", "", pname.lower()) == target:
            return p
    return None


def handle_tier_threshold_command(config, text, tier_threshold_state):
    """
    Handles "/tier <N>": sets the minimum-quality bar for /check results --
    only players ranked Tier N or better (lower tier number) get shown.
    Persists across runs via tier_threshold_state.json. Returns True if
    this was actually a /tier command (handled or invalid), False if not,
    so the caller knows whether to also try /check parsing.
    """
    m = TIER_COMMAND_RE.match(text.strip())
    if not m:
        return False

    n = max(1, min(15, int(m.group(1))))
    tier_threshold_state["threshold"] = n
    notifier.send_message(config, f"Tier threshold set to {n} -- /check will now only show "
                                   f"players ranked Tier {n} or better.")
    return True


def handle_check_command(config, text, nfl_manager, players_nfl, tier_threshold):
    """
    Handles "/check <position> [count] [refresh]": fetches that position's
    STD tier list from fantasyfootballtiers.com, walks it in tier order
    (best players first), and replies with the first `count` (default 3)
    players that are BOTH not currently rostered in the Yahoo league AND
    ranked Tier `tier_threshold` or better (see /tier to change that bar).
    NFL only (this data source doesn't cover basketball).
    """
    m = CHECK_COMMAND_RE.match(text.strip())
    if not m:
        return  # not a /check command -- ignore silently, could be anything

    parsed = _parse_check_args(m.group(1))
    if not parsed:
        return
    position, count, refresh = parsed

    if position not in VALID_CHECK_POSITIONS:
        notifier.send_message(config, f"Unknown position \"{position}\". Try one of: "
                                       f"{', '.join(sorted(VALID_CHECK_POSITIONS))} "
                                       f"(DEF, PK, FLX also work as aliases).")
        return

    if nfl_manager is None:
        notifier.send_message(config, "Can't check right now -- Yahoo roster management isn't "
                                       "set up or is temporarily unavailable (check the dashboard health tab).")
        return

    import tier_scraper
    configured_cache_hours = config.get("tier_check", {}).get("cache_hours", tier_scraper.CACHE_MAX_AGE_HOURS)
    cache_hours = 0 if refresh else configured_cache_hours
    result = tier_scraper.get_tier_list(position, max_age_hours=cache_hours)
    if result is None:
        notifier.send_message(config, f"Couldn't fetch tier data for {position} right now "
                                       f"(fantasyfootballtiers.com may be unreachable or its page format "
                                       f"changed). Check the Actions log for details.")
        return

    tiers = result["tiers"]
    fetch_time_note = tier_scraper.format_time_ago(result.get("fetched_at"))

    found = []
    seen_names = set()
    for tier_num, name in tiers:
        if tier_num > tier_threshold:
            break  # tiers are in order, so nothing past this point can qualify either
        if name in seen_names:
            continue
        seen_names.add(name)
        try:
            status_label, match = nfl_manager.get_player_status(name)
        except Exception as e:
            print(f"/check {position}: status lookup failed for {name}, skipping: {e}")
            continue
        if match is not None:  # actually available (FA or waivers), not rostered
            sleeper_p = _find_sleeper_player_by_name(players_nfl, name) if players_nfl else None
            injury_note = find_starter_injury_context(players_nfl, sleeper_p) if sleeper_p else None
            found.append((tier_num, name, status_label, injury_note))
            if len(found) == count:
                break

    if not found:
        notifier.send_message(config, f"No available {position}s found at Tier {tier_threshold} or better "
                                       f"right now.\n(Tier data pulled {fetch_time_note})")
        return

    lines = [f"🏈 Top available {position}s (STD tiers, Tier {tier_threshold} or better):"]
    for i, (tier_num, name, status_label, injury_note) in enumerate(found, 1):
        line = f"{i}. {name} (Tier {tier_num}) -- {status_label}"
        if injury_note:
            line += f"\n   {injury_note}"
        lines.append(line)
    lines.append(f"\n(Tier data pulled {fetch_time_note})")
    notifier.send_message(config, "\n".join(lines))


def poll_and_handle_telegram(config, pending, managers, health_state, players_nfl, tier_threshold_state):
    """
    Polls Telegram ONCE per run for anything new -- button taps, "/check
    <position>" commands, and "/tier <N>" commands all come from this
    single poll (see get_new_updates in telegram_notifier.py for why it
    must be one call, not two). Dispatches each to its handler.
    """
    offset_state = load_json(TELEGRAM_OFFSET_PATH, {})
    try:
        result = notifier.get_new_updates(config, offset_state)
    except Exception as e:
        print(f"Could not check Telegram for updates: {e}")
        result = {"taps": [], "commands": []}
    save_json(TELEGRAM_OFFSET_PATH, offset_state)

    rm = config.get("roster_management", {})
    if rm.get("enabled") and pending:
        process_confirmations(config, pending, managers, result["taps"])

    for command in result["commands"]:
        text = command["text"]
        try:
            if handle_tier_threshold_command(config, text, tier_threshold_state):
                continue  # was a /tier command, handled (or reported invalid) -- don't also try /check
            handle_check_command(config, text, managers.get("nfl"), players_nfl,
                                  tier_threshold_state.get("threshold", DEFAULT_TIER_THRESHOLD))
        except Exception as e:
            print(f"Error handling command {text!r}: {e}")


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


def run_football(config, alerted, players, history, manager, batch, health_state):
    if not config["football"]["enabled"]:
        return
    state = sleeper.get_nfl_state()
    if not state:
        print("Could not fetch NFL state, skipping football check.")
        health.record_failure(health_state, "sleeper_nfl")
        maybe_send_failure_alert(config, health_state, "sleeper_nfl", "Sleeper (NFL)")
        return
    season, week = state.get("season"), state.get("week")
    if not season or not week:
        return

    projections = sleeper.get_nfl_week_projections(season, week)
    stats = sleeper.get_nfl_week_stats(season, week)
    if not projections or not stats:
        print("No football projections/stats available yet this run.")
        # Only a real anomaly if today is a confirmed NFL day (checked by the
        # caller before run_football is even invoked) -- so this counts.
        health.record_failure(health_state, "sleeper_nfl")
        maybe_send_failure_alert(config, health_state, "sleeper_nfl", "Sleeper (NFL)")
        return

    health.record_success(health_state, "sleeper_nfl")
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
                status_tag = get_game_status_tag(schedule, team, week)
                msg = notifier.format_football_alert(name, team, result, label=label, status_tag=status_tag)
                injury_note = find_starter_injury_context(players, p)
                if injury_note:
                    msg += f"\n{injury_note}"
                print("ALERT:", msg)
                item = evaluate_breakout_item(config, "nfl", name, team, msg, manager, health_state, position=p.get("position"))
                record_history(history, "nfl", name, msg, notified=item["send"])
                batch.append(item)
            except Exception as e:
                print(f"Error processing breakout for player {player_id}, continuing with the rest: {e}")
            finally:
                alerted[key][player_id] = {"count": result["count"], "baseline": result["baseline"]}


def run_basketball(config, alerted, players, history, manager, batch, health_state):
    if not config["basketball"]["enabled"]:
        return
    nba_state = sleeper.get_nba_state()
    season = nba_state.get("season") if nba_state else str(date.today().year)
    today_str = date.today().isoformat()

    projections = sleeper.get_nba_day_projections(today_str, season)
    stats = sleeper.get_nba_day_stats(today_str, season)
    if not projections or not stats:
        print("No basketball projections/stats available yet this run (maybe no games today).")
        # Only a real anomaly if today is a confirmed NBA day (checked by the
        # caller before run_basketball is even invoked) -- so this counts.
        health.record_failure(health_state, "sleeper_nba")
        maybe_send_failure_alert(config, health_state, "sleeper_nba", "Sleeper (NBA)")
        return

    health.record_success(health_state, "sleeper_nba")

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
                injury_note = find_starter_injury_context(players, p)
                if injury_note:
                    msg += f"\n{injury_note}"
                print("ALERT:", msg)
                item = evaluate_breakout_item(config, "nba", name, team, msg, manager, health_state, position=p.get("position"))
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
    health_state = health.load()
    tier_threshold_state = load_json(TIER_THRESHOLD_PATH, {"threshold": DEFAULT_TIER_THRESHOLD})

    players_nfl = get_players_cached("nfl", PLAYERS_CACHE_NFL)
    players_nba = get_players_cached("nba", PLAYERS_CACHE_NBA)

    # Build one Yahoo manager per sport for this whole run (not one per
    # breakout) -- each caches its own free-agent scan and open-slot check,
    # so reusing them across every alert and every confirmation avoids
    # re-hitting Yahoo's API repeatedly. None if roster_management is off,
    # the circuit breaker is open, or setup fails.
    managers = {
        "nfl": build_yahoo_manager(config, "nfl", health_state),
        "nba": build_yahoo_manager(config, "nba", health_state),
    }

    try:
        poll_and_handle_telegram(config, pending, managers, health_state, players_nfl, tier_threshold_state)
    except Exception as e:
        print(f"poll_and_handle_telegram failed, continuing: {e}")

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
            run_football(config, alerted, players_nfl, history, managers["nfl"], batch, health_state)
        except Exception as e:
            print(f"run_football failed, continuing to basketball: {e}")
    else:
        print("No NFL games scheduled today -- skipping football check.")

    if nba_today:
        try:
            run_basketball(config, alerted, players_nba, history, managers["nba"], batch, health_state)
        except Exception as e:
            print(f"run_basketball failed: {e}")
    else:
        print("No NBA games scheduled today -- skipping basketball check.")

    try:
        dispatch_alerts(config, pending, batch, health_state)
    except Exception as e:
        print(f"dispatch_alerts failed: {e}")

    save_json(ALERTED_PATH, alerted)
    save_json(PENDING_PATH, pending)
    save_json(DASHBOARD_HISTORY_PATH, history)
    save_json(TIER_THRESHOLD_PATH, tier_threshold_state)
    health.save(health_state)
    write_dashboard(history, pending, health_state, config)


if __name__ == "__main__":
    main()
