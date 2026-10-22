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

import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import sleeper_client as sleeper
import scoring
import telegram_notifier as notifier
import health
from state_store import load, save, timestamp

ET_ZONE = ZoneInfo("America/New_York")  # NBA's "today" is a calendar day in Eastern
# time, not UTC -- using UTC date.today() here would query the wrong day's
# Sleeper slate during the evening/night UTC hours (roughly 8pm ET onward),
# once UTC has already rolled to the next calendar date but Eastern hasn't.


def today_et():
    return datetime.now(ET_ZONE).date()


def nba_game_day(now=None):
    """Keep late games and delayed checks on their starting slate until 6am ET."""
    now = now or datetime.now(ET_ZONE)
    return (now.astimezone(ET_ZONE) - timedelta(hours=6)).date()


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
    return load(path, default)


def save_json(path, data):
    save(path, data)


def get_players_cached(sport, cache_path):
    """
    Caches Sleeper's full player list for 24h. Freshness is tracked via a
    sidecar "<cache_path>.fetched_at" file holding an ISO timestamp, NOT
    filesystem mtime -- GitHub Actions does a fresh `git checkout` every
    run, which sets every file's mtime to "now" regardless of when it was
    actually committed, so an mtime-based check would always read as
    freshly-fetched and this cache would never actually refresh.
    """
    meta_path = cache_path + ".fetched_at"
    if os.path.exists(cache_path) and os.path.exists(meta_path):
        with open(meta_path) as f:
            fetched_at = timestamp(f.read().strip())
        if fetched_at and timedelta(0) <= datetime.now(timezone.utc) - fetched_at < timedelta(hours=24):
            return load_json(cache_path, {})
    players = sleeper.get_all_players(sport)
    if players:
        save_json(cache_path, players)
        with open(meta_path, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
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


def write_dashboard(history, pending, health_state, config, runtime=None):
    import delivery
    hc = config.get("health_check", {})
    runtime = runtime or load("runtime_state.json", {})
    data = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "recent_alerts": list(reversed(history))[:200],
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
        "stale_after_minutes": hc.get("stale_minutes", 15),
        "runtime": runtime,
        "delivery": delivery.summary(load(delivery.PATH, {})),
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
            health_state[component]["notified"] = True
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

    component = f"yahoo_{sport}"
    if health.yahoo_circuit_is_open(health_state, component):
        print(f"Yahoo circuit breaker is open (recent repeated failures) -- skipping {sport} this run.")
        return None

    try:
        import yahoo_client
        league_key, team_key = get_yahoo_keys(config, sport)
        if not league_key or not team_key:
            raise ValueError("Yahoo league/team keys are not configured")
        manager = yahoo_client.YahooRosterManager("oauth2.json", sport, league_key, team_key)
        manager.get_open_bench_or_ir_slots()  # validation call, also warms the cache
        health.record_success(health_state, component)
        return manager
    except Exception as e:
        print(f"Could not set up Yahoo roster manager for {sport}: {type(e).__name__}")
        failures = health.record_failure(health_state, component)
        maybe_send_failure_alert(config, health_state, component, f"Yahoo {sport.upper()}")
        hc = config.get("health_check", {})
        breaker_threshold = hc.get("yahoo_breaker_threshold", health.YAHOO_BREAKER_THRESHOLD)
        breaker_cooldown = hc.get("yahoo_breaker_cooldown_minutes", health.YAHOO_BREAKER_COOLDOWN_MINUTES)
        if failures >= breaker_threshold:
            health.open_yahoo_circuit(health_state, breaker_cooldown, component)
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
    my_depth = scoring.number(my_depth)

    for p in players.values():
        if p.get("team") != team or p.get("position") != position:
            continue
        other_depth = p.get("depth_chart_order")
        if other_depth is None or scoring.number(other_depth) >= my_depth:
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

        if is_rostered and status_label != "On your roster" and rm.get("quiet_rostered_players", True):
            return {"send": False, "text": breakout_msg, "addable": False,
                    "player_id": None, "player_name": player_name, "sport": sport}

        text = f"{breakout_msg}\n\n{status_label}\n{manager.roster_context(position)}"
        has_open_slot = manager.get_open_bench_or_ir_slots()
        addable = bool(has_open_slot and match and match.get("status") == "FA")
        if addable:
            text += f" -- add {player_name}?"

        return {"send": True, "text": text, "addable": addable,
                "player_id": match["player_id"] if match else None,
                "player_name": player_name, "sport": sport, "availability": status_label}

    except Exception as e:
        print(f"Roster context unavailable for {player_name}: {type(e).__name__}")
        # Deliberately record_failure only, not record_success anywhere in
        # this function -- a per-player failure here shouldn't be masked by
        # other players in the same run succeeding, and connection-level
        # success is already tracked once per run in build_yahoo_manager.
        health.record_failure(health_state, f"yahoo_{sport}")
        maybe_send_failure_alert(config, health_state, f"yahoo_{sport}", f"Yahoo {sport.upper()}")
        return {"send": True, "text": breakout_msg, "addable": False,
                "player_id": None, "player_name": player_name, "sport": sport}


def dispatch_alerts(config, pending, batch, health_state, alerted=None, history=None):
    import delivery
    return delivery.dispatch(config, pending, batch, health_state,
                             alerted if alerted is not None else {}, history if history is not None else [])


def process_confirmations(config, pending, managers, taps):
    """Handles Add/No-thanks button taps. Expiry of old pending suggestions
    also happens here, on every run, regardless of whether any taps came in."""
    rm = config.get("roster_management", {})

    for tap in taps:
        action, _, code = tap["callback_data"].partition(":")
        if action not in ("add", "decline"):
            notifier.acknowledge_tap(config, tap["callback_query_id"], "Unknown action.")
            continue
        item = pending.get(code)
        if not item:
            notifier.acknowledge_tap(config, tap["callback_query_id"], "This suggestion expired.")
            continue
        created = timestamp(item.get("created_at"))
        if not created or datetime.now(timezone.utc) - created > timedelta(minutes=rm.get("suggestion_expiry_minutes", DEFAULT_SUGGESTION_EXPIRY_MINUTES)):
            pending.pop(code, None)
            notifier.acknowledge_tap(config, tap["callback_query_id"], "This suggestion expired.")
            continue

        batched = item.get("batched", False)

        if action == "decline":
            pending.pop(code, None)
            save_json(PENDING_PATH, pending)
            if not batched:
                notifier.edit_message(config, item["message_id"], f"Skipped {item['player_name']}.")
            else:
                notifier.send_message(config, f"Skipped {item['player_name']}.")
            notifier.acknowledge_tap(config, tap["callback_query_id"])
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
            response = "Added!"
        except Exception:
            msg = f"Could not confirm the add for {item['player_name']}. Check Yahoo before trying again."
            response = "Check Yahoo -- see message."
        # Consume the action before editing Telegram: a failed edit must not
        # turn a completed transaction into a retryable Add button.
        pending.pop(code, None)
        save_json(PENDING_PATH, pending)
        try:
            if not batched:
                notifier.edit_message(config, item["message_id"], msg)
            else:
                notifier.send_message(config, msg)
            notifier.acknowledge_tap(config, tap["callback_query_id"], response)
        except Exception:
            print("Roster action consumed; Telegram confirmation could not be updated.")

    # Expire old pending suggestions
    expiry_minutes = rm.get("suggestion_expiry_minutes", DEFAULT_SUGGESTION_EXPIRY_MINUTES)
    now = datetime.now(timezone.utc)
    for code in list(pending.keys()):
        created = timestamp(pending[code].get("created_at"))
        if not created or (now - created) > timedelta(minutes=expiry_minutes):
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


def poll_and_handle_telegram(config, pending, managers, health_state, players_nfl, tier_threshold_state,
                             history=None, runtime=None, batch=None):
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

    rm = config.get("roster_management", {})
    if pending:
        process_confirmations(config, pending, managers, result["taps"] if rm.get("enabled") else [])

    for command in result["commands"]:
        text = command["text"]
        try:
            import bot_commands
            sports = bot_commands.scan_sports(text)
            if sports and runtime is not None:
                import delivery
                identity = f"scan:{command.get('update_id', text)}"
                receipt = delivery.event_id({"event_id": identity})
                journal = load_json(delivery.PATH, {})
                requests = runtime.setdefault("scan_requests", [])
                if receipt not in journal.get("items", {}) and not any(r["event_id"] == identity for r in requests):
                    requests.append({"event_id": identity, "sports": list(sports)})
                # Persist the request before acknowledging its Telegram update.
                from alert_engine import RUNTIME_PATH
                save_json(RUNTIME_PATH, runtime)
                continue
            response = bot_commands.reply(text, config, health_state, history or [], runtime or {})
            if response is not None:
                if batch is not None:
                    batch.append(bot_commands.command_item(f"command:{command.get('update_id', text)}", response))
                else:
                    notifier.send_message(config, response)
                continue
            if handle_tier_threshold_command(config, text, tier_threshold_state):
                continue  # was a /tier command, handled (or reported invalid) -- don't also try /check
            handle_check_command(config, text, managers.get("nfl"), players_nfl,
                                  tier_threshold_state.get("threshold", DEFAULT_TIER_THRESHOLD))
        except Exception as e:
            print(f"Error handling command: {type(e).__name__}")
            if command.get("update_id") is not None:
                offset_state["offset"] = min(offset_state.get("offset", command["update_id"]), command["update_id"])
            break
    if batch:
        import delivery
        delivery.dispatch(config, pending, batch, health_state, {}, [], send=False)
    save_json(TELEGRAM_OFFSET_PATH, offset_state)


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


def run_football(config, alerted, players, history, manager, batch, health_state, **kwargs):
    import alert_engine
    return alert_engine.run_football(config, alerted, players, history, manager, batch, health_state,
                                     bot=sys.modules[__name__], **kwargs)


def run_basketball(config, alerted, players, history, manager, batch, health_state, **kwargs):
    import alert_engine
    return alert_engine.run_basketball(config, alerted, players, history, manager, batch, health_state,
                                       bot=sys.modules[__name__], **kwargs)


def main():
    import alert_engine
    return alert_engine.run_cycle(sys.modules[__name__])


if __name__ == "__main__":
    main()
