"""Collection, catch-up, structured alert metadata and durable delivery."""
import sys
from datetime import date, timedelta
import delivery
import game_context
import player_insights as insights
from state_store import load, save, timestamp, utcnow

RUNTIME_PATH = "runtime_state.json"
PERFORMANCE_PATH = "performance_state.json"


def _info(runtime, sport, day, status, **values):
    info = dict(day=str(day), status=status, checked_at=utcnow().isoformat(), **values)
    runtime.setdefault("sports", {})[sport] = info


def _candidate(bot, config, alerted, history, manager, batch, health_state,
               sport, player_id, player, result, period, day, game, kind="breakout", note=None, delayed=False):
    name = player.get("full_name") or f"Player {player_id}"
    team = player.get("team") or "FA"
    if kind == "breakout":
        label = bot.scoring.breakout_label(result["count"], result.get("is_extreme", False))
        tag = game_context.display(game)
        formatter = bot.notifier.format_basketball_alert if sport == "nba" else bot.notifier.format_football_alert
        text = formatter(name, team, result, label=label, status_tag=tag)
    else:
        text = f"{'🏀' if sport == 'nba' else '🏈'} {kind.upper()}: {name} ({team})\n{note}"
        if game_context.display(game):
            text += f"\n{game_context.display(game)}"
    injury = bot.find_starter_injury_context(player.get("_players", {}), player)
    if injury:
        text += "\n" + injury
    if delayed:
        text = f"⏱ Delayed alert · game date {day}\n" + text
    item = bot.evaluate_breakout_item(config, sport, name, team, text, manager, health_state,
                                      position=player.get("position"))
    count = result.get("count", 1) if kind == "breakout" else 1
    item.update({"alert_key": period, "sleeper_player_id": str(player_id), "kind": kind,
                 "count": count, "baseline": result.get("baseline"),
                 "event_id": f"{period}|{player_id}|{kind}|{count}"})
    game_label = game.get("matchup") if game else (player.get("_matchup") or f"{sport.upper()} · {day}")
    item["history"] = {"sport": sport, "player_name": name, "player_id": str(player_id),
                       "message": item["text"], "kind": kind, "level": count,
                       "game_date": str(day), "game_id": player.get("_game_id") or (game or {}).get("id"),
                       "game_label": game_label, "game_status": game_context.display(game),
                       "score": result.get("total_zscore", result.get("actual_points")),
                       "actual_stats": result.get("actual_stats"), "projected_stats": result.get("projected_stats"),
                       "projection_pct": result.get("pct_of_projection"), "delayed": delayed,
                       "availability": item.get("availability")}
    if not item["send"]:
        identity = delivery.event_id(item)
        if not any(h.get("event_id") == identity for h in history):
            history.append(dict(item["history"], event_id=identity, timestamp=utcnow().isoformat(), notified=False))
        if kind == "breakout":
            alerted.setdefault(period, {})[str(player_id)] = {"count": count, "baseline": result["baseline"], "score_version": 2}
    else:
        batch.append(item)


def run_basketball(config, alerted, players, history, manager, batch, health_state,
                   bot=None, game_day=None, performance=None, runtime=None, delayed=False):
    bot = bot or sys.modules["main"]
    if not config["basketball"]["enabled"]:
        return
    performance = performance if performance is not None else {}
    runtime = runtime if runtime is not None else {}
    day = game_day or bot.nba_game_day()
    component = "sleeper_nba" if day == bot.nba_game_day() else "sleeper_nba_catchup"
    day_string = str(day)
    state = bot.sleeper.get_nba_state() or {}
    season = state.get("season", str(day.year))
    week = bot.sleeper.nba_week_for_day(day_string, state)
    season_type = state.get("season_type", "regular")
    projections = bot.sleeper.get_nba_day_projections(day_string, season, season_type, week=week)
    stats = bot.sleeper.get_nba_day_stats(day_string, season, season_type, week=week)
    games = game_context.fetch_games("nba", day_string)
    if games is not None:
        runtime.setdefault("games", []).extend(dict(g, sport="nba") for g in games)
        bot.health.record_success(health_state, "scoreboard")
    else:
        bot.health.record_failure(health_state, "scoreboard")
    matched = {pid: actual for pid, actual in (stats or {}).items() if actual and (projections or {}).get(pid)}
    if not matched:
        # A valid empty slate or a pregame scoreboard is not an API outage.
        expected_empty = projections is not None and stats is not None and games is not None and not any(g.get("state") in ("in", "post") for g in games)
        if not expected_empty:
            bot.health.record_failure(health_state, component)
            if component == "sleeper_nba":
                bot.maybe_send_failure_alert(config, health_state, component, "Sleeper (NBA)")
        _info(runtime, "nba", day, "idle" if expected_empty else "data unavailable", matched=0)
        return
    bot.health.record_success(health_state, component)
    _info(runtime, "nba", day, "checked", projections=len(projections), stats=len(stats), matched=len(matched))
    source_times = [actual.get("_meta", {}).get("updated_at") for actual in matched.values()]
    source_times = [value for value in source_times if isinstance(value, (int, float))]
    if source_times:
        from datetime import datetime, timezone
        runtime["sports"]["nba"]["data_updated_at"] = datetime.fromtimestamp(max(source_times) / 1000, timezone.utc).isoformat()
    prior_check = runtime.setdefault("checked_days", {}).get(day_string, {})
    final_seen = prior_check.get("final_seen_at")
    all_final = bool(games) and all(g.get("final") for g in games)
    if all_final and not final_seen:
        final_seen = utcnow().isoformat()
    runtime["checked_days"][day_string] = {"checked_at": utcnow().isoformat(), "final_seen_at": final_seen,
            "complete": bool(all_final and timestamp(final_seen) and utcnow() - timestamp(final_seen) >= timedelta(minutes=30))}
    settings = config["basketball"]
    categories, invert, threshold = settings["categories"], settings["invert_categories"], settings["zscore_alert_threshold"]
    meaningful = {pid: p for pid, p in projections.items() if insights.minutes(p) >= 6} or projections
    deviations = bot.scoring._category_stdevs(meaningful, categories)
    period = f"nba-{day_string}"
    alerted[period] = bot._ensure_new_alerted_format(alerted.get(period, {}))
    trend_settings = dict(config.get("trends", {}), nba_threshold=threshold)
    print(f"NBA {day}: {len(projections)} projections, {len(stats)} stat lines, {len(matched)} matched players.")
    for player_id, actual in matched.items():
        try:
            projected = projections[player_id]
            meta = actual.get("_meta", {})
            player = dict(players.get(player_id, {}))
            player.update({k: v for k, v in {"full_name": meta.get("name"), "team": meta.get("team"), "position": meta.get("position")}.items() if v})
            player.update(_players=players, _game_id=meta.get("game_id"),
                          _matchup=f"{meta.get('team')} vs {meta.get('opponent')}" if meta.get("opponent") else None)
            game = game_context.for_team(games, player.get("team"), meta.get("opponent"))
            prior = alerted[period].get(player_id, {})
            prior_count, baseline = prior.get("count", 0), prior.get("baseline")
            if prior_count and (baseline is None or prior.get("score_version") != 2):
                # Old percentage-based scores cannot serve as new weighted baselines.
                score, _ = bot.scoring.compute_zscore(actual, projected, meaningful, categories, invert, deviations)
                alerted[period][player_id] = {"count": prior_count, "baseline": round(score, 2), "score_version": 2}
                baseline = round(score, 2)
            result = bot.scoring.basketball_breakout_level(actual, projected, meaningful, categories, invert, threshold,
                        prior_count, baseline, settings.get("extreme_multiplier", 1.5), stdevs=deviations)
            result.update(actual_stats={k: v for k, v in actual.items() if k != "_meta"},
                          projected_stats={k: v for k, v in projected.items() if k != "_meta"},
                          eligible=insights.qualifies(actual, settings))
            if result["is_breakout"] and result["eligible"]:
                _candidate(bot, config, alerted, history, manager, batch, health_state,
                           "nba", player_id, player, result, period, day, game, delayed=delayed)
            if result["eligible"]:
                note = insights.opportunity(actual, projected, game, config.get("opportunity", {}))
                if note and not result["is_breakout"]:
                    _candidate(bot, config, alerted, history, manager, batch, health_state,
                               "nba", player_id, player, result, period, day, game, kind="opportunity", note=note, delayed=delayed)
            note = insights.observe(performance, "nba", player_id, day, result, actual,
                                    bool(game and game.get("final")), trend_settings)
            if note:
                _candidate(bot, config, alerted, history, manager, batch, health_state,
                           "nba", player_id, player, result, period, day, game, kind="trend", note=note, delayed=delayed)
        except Exception as error:
            runtime["sports"]["nba"]["skipped_players"] = runtime["sports"]["nba"].get("skipped_players", 0) + 1
            runtime["sports"]["nba"]["status"] = "partial"
            print(f"Skipped NBA player {player_id}: {type(error).__name__}")


def run_football(config, alerted, players, history, manager, batch, health_state,
                 bot=None, performance=None, runtime=None, context_days=None, delayed=False):
    bot = bot or sys.modules["main"]
    if not config["football"]["enabled"]:
        return
    performance = performance if performance is not None else {}
    runtime = runtime if runtime is not None else {}
    settings = config["football"]
    state = bot.sleeper.get_nfl_state()
    if not state:
        bot.health.record_failure(health_state, "sleeper_nfl")
        return
    days = context_days or [bot.today_et()]
    games, available = [], True
    for day in days:
        found = game_context.fetch_games("nfl", str(day))
        available = available and found is not None
        games.extend(found or [])
    runtime.setdefault("games", []).extend(dict(g, sport="nfl") for g in games)
    active = [g for g in games if g.get("state") in ("in", "post")]
    if available and not active:
        _info(runtime, "nfl", days[0], "idle", matched=0)
        return
    contexts = {(str(g.get("season") or state.get("season")), g.get("week") or state.get("week"),
                 g.get("season_type", state.get("season_type", "regular"))) for g in active}
    if not contexts:
        contexts = {(str(state.get("season")), state.get("week"), state.get("season_type", "regular"))}
    for season, week, season_type in contexts:
        if not week or season_type not in ("regular", "post"):
            continue
        projections = bot.sleeper.get_nfl_week_projections(season, week, season_type)
        stats = bot.sleeper.get_nfl_week_stats(season, week, season_type)
        if not projections or not stats:
            bot.health.record_failure(health_state, "sleeper_nfl")
            _info(runtime, "nfl", days[0], "data unavailable", matched=0)
            continue
        bot.health.record_success(health_state, "sleeper_nfl")
        _info(runtime, "nfl", days[0], "checked", matched=len(set(stats) & set(projections)), week=week)
        period = f"nfl-{season}-{week}"
        alerted[period] = bot._ensure_new_alerted_format(alerted.get(period, {}))
        relevant = [g for g in games if not g.get("week") or str(g["week"]) == str(week)]
        for player_id, actual in stats.items():
            try:
                projected = projections.get(player_id)
                if not actual or not projected:
                    continue
                prior = alerted[period].get(player_id, {})
                prior_count, baseline = prior.get("count", 0), prior.get("baseline")
                if prior_count and baseline is None:
                    baseline = bot.scoring.calc_fantasy_points(actual, settings["scoring_settings"])
                    alerted[period][player_id] = {"count": prior_count, "baseline": baseline}
                    continue
                result = bot.scoring.football_breakout_level(actual, projected, settings["scoring_settings"],
                            settings["pct_threshold"], settings["point_floor"], prior_count, baseline, settings.get("extreme_multiplier", 1.5))
                result.update(actual_stats=actual, projected_stats=projected)
                player = dict(players.get(player_id, {}), _players=players)
                if not player.get("full_name"):
                    player["full_name"] = " ".join(filter(None, (player.get("first_name"), player.get("last_name")))) or (f"{player_id} D/ST" if str(player_id).isupper() else f"Player {player_id}")
                if not player.get("team") and str(player_id).isupper():
                    player["team"] = str(player_id)
                game = game_context.for_team(relevant, player.get("team"))
                day = date.fromisoformat(game["date"]) if game else days[0]
                is_delayed = delayed or day < bot.today_et()
                if result["is_breakout"]:
                    _candidate(bot, config, alerted, history, manager, batch, health_state,
                               "nfl", player_id, player, result, period, day, game, delayed=is_delayed)
                # Only describe a role change when the feed provides actual snap share.
                snap = actual.get("snap_pct")
                projected_snap = projected.get("snap_pct")
                if config.get("opportunity", {}).get("enabled", True) and snap is not None and projected_snap is not None and float(snap) >= max(float(projected_snap) * 1.4, float(projected_snap) + .15):
                    note = f"Expanded role: snap share {float(snap):.0%} vs {float(projected_snap):.0%} projected."
                    _candidate(bot, config, alerted, history, manager, batch, health_state,
                               "nfl", player_id, player, result, period, day, game, kind="opportunity", note=note, delayed=is_delayed)
                trend_settings = dict(config.get("trends", {}), nfl_threshold=settings["pct_threshold"] * 100,
                                      nfl_point_floor=settings["point_floor"])
                note = insights.observe(performance, "nfl", player_id, day, result, actual,
                                        bool(game and game.get("final")), trend_settings)
                if note:
                    _candidate(bot, config, alerted, history, manager, batch, health_state,
                               "nfl", player_id, player, result, period, day, game, kind="trend", note=note, delayed=is_delayed)
            except Exception as error:
                runtime["sports"]["nfl"]["skipped_players"] = runtime["sports"]["nfl"].get("skipped_players", 0) + 1
                runtime["sports"]["nfl"]["status"] = "partial"
                print(f"Skipped NFL player {player_id}: {type(error).__name__}")


def run_cycle(bot):
    config = bot.load_json(bot.CONFIG_PATH, {})
    for sport in ("football", "basketball"):
        if sport not in config:
            raise ValueError(f"Missing {sport} configuration")
    alerted = bot.load_json(bot.ALERTED_PATH, {})
    alerted = {key: bot._ensure_new_alerted_format(value) for key, value in alerted.items()}
    history = bot.load_json(bot.DASHBOARD_HISTORY_PATH, [])
    pending = bot.load_json(bot.PENDING_PATH, {})
    health_state = bot.health.load()
    performance = load(PERFORMANCE_PATH, {})
    runtime = load(RUNTIME_PATH, {})
    previous = timestamp(runtime.get("last_completed"))
    delayed = bool(previous and (utcnow() - previous).total_seconds() > config.get("health_check", {}).get("stale_minutes", 15) * 60)
    runtime.update(last_started=utcnow().isoformat(), sports={}, games=[])
    save(RUNTIME_PATH, runtime)
    delivery.apply_receipts(load(delivery.PATH, {}), alerted, history)
    threshold_state = bot.load_json(bot.TIER_THRESHOLD_PATH, {"threshold": bot.DEFAULT_TIER_THRESHOLD})
    players_nfl = bot.get_players_cached("nfl", bot.PLAYERS_CACHE_NFL) if config["football"]["enabled"] else {}
    players_nba = bot.get_players_cached("nba", bot.PLAYERS_CACHE_NBA) if config["basketball"]["enabled"] else {}
    managers = {sport: bot.build_yahoo_manager(config, sport, health_state)
                if config[field]["enabled"] else None for sport, field in (("nfl", "football"), ("nba", "basketball"))}
    batch = []
    try:
        try:
            bot.poll_and_handle_telegram(config, pending, managers, health_state, players_nfl, threshold_state,
                                         history=history, runtime=runtime, batch=batch)
        except Exception as error:
            print(f"Telegram polling failed; collection continues: {type(error).__name__}")
            bot.health.record_failure(health_state, "telegram")
        # Retry pending items before calculating fresh escalation levels.
        delivery.dispatch(config, pending, [], health_state, alerted, history)
        lookback = min(7, max(0, int(config.get("catchup", {}).get("lookback_days", 2))))
        nba_days = [bot.nba_game_day() - timedelta(days=n) for n in range(lookback, -1, -1)]
        for day in nba_days:
            checked = runtime.get("checked_days", {}).get(str(day), {})
            if day < bot.nba_game_day() and checked.get("complete"):
                continue
            try:
                run_basketball(config, alerted, players_nba, history, managers["nba"], batch, health_state,
                               bot=bot, game_day=day, performance=performance, runtime=runtime,
                               delayed=delayed or day < bot.nba_game_day())
            except Exception as error:
                print(f"NBA check failed: {type(error).__name__}")
                bot.health.record_failure(health_state, "sleeper_nba")
        try:
            run_football(config, alerted, players_nfl, history, managers["nfl"], batch, health_state,
                         bot=bot, performance=performance, runtime=runtime, delayed=delayed,
                         context_days=[bot.today_et() - timedelta(days=n) for n in range(lookback + 1)])
        except Exception as error:
            print(f"NFL check failed: {type(error).__name__}")
            bot.health.record_failure(health_state, "sleeper_nfl")
        delivery.dispatch(config, pending, batch, health_state, alerted, history)
        runtime["last_completed"] = utcnow().isoformat()
    finally:
        cutoff = utcnow() - timedelta(days=35)
        history[:] = [h for h in history if timestamp(h.get("timestamp")) and timestamp(h["timestamp"]) >= cutoff]
        runtime["checked_days"] = {key: value for key, value in runtime.get("checked_days", {}).items()
                                   if (bot.nba_game_day() - date.fromisoformat(key)).days <= 14}
        # Stable dashboard ordering, with no repeated games from catch-up.
        runtime["games"] = list({(g["sport"], g["date"], g["id"]): g for g in runtime["games"]}.values())
        save(RUNTIME_PATH, runtime)
        save(PERFORMANCE_PATH, performance)
        bot.save_json(bot.ALERTED_PATH, alerted)
        bot.save_json(bot.PENDING_PATH, pending)
        bot.save_json(bot.DASHBOARD_HISTORY_PATH, history)
        bot.save_json(bot.TIER_THRESHOLD_PATH, threshold_state)
        bot.health.save(health_state)
        bot.write_dashboard(history, pending, health_state, config, runtime=runtime)
