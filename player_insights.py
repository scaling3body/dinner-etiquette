"""Evidence-based opportunity and multi-game performance signals."""
from datetime import date
import game_context
from scoring import number


def minutes(stats):
    return number(stats.get("min")) or number(stats.get("sp")) / 60


def qualifies(stats, settings):
    if minutes(stats) < settings.get("min_minutes", 12):
        return False
    volume = sum(float(stats.get(k) or 0) for k in ("pts", "reb", "ast", "stl", "blk", "fg3m"))
    return volume >= settings.get("min_counting_stats", 12)


def opportunity(actual, projected, game, settings):
    if not settings.get("enabled", True) or not game or game.get("state") != "in":
        return None
    elapsed = game_context.elapsed_minutes(game)
    actual_min, projected_min = minutes(actual), minutes(projected)
    if not elapsed or elapsed < settings.get("min_game_minutes", 18) or actual_min < 8 or projected_min < 6:
        return None
    if actual.get("gs") == 1 and projected.get("gs") == 0:
        return "Starting-role opportunity: the stats feed reports a start against a projected bench role."
    pace = actual_min / elapsed * 48
    if pace >= max(projected_min + settings.get("extra_minutes", 8), projected_min * 1.35, 24):
        return f"Minutes opportunity: {actual_min:g} minutes played; pace {pace:.0f} vs {projected_min:.0f} projected. Pace is an estimate."
    actual_plays = sum(float(actual.get(k) or 0) * w for k, w in (("fga", 1), ("fta", .44), ("to", 1)))
    projected_plays = sum(float(projected.get(k) or 0) * w for k, w in (("fga", 1), ("fta", .44), ("to", 1)))
    if actual_plays >= 8 and projected_plays > 0 and actual_plays / actual_min >= projected_plays / projected_min * 1.5:
        return (f"Expanded involvement: {actual.get('fga', 0):g} shot attempts in {actual_min:g} minutes; "
                "shot/FT/turnover rate is at least 50% above projection. This is a usage proxy.")
    return None


def observe(state, sport, player_id, game_day, result, actual, final, settings):
    """Upsert one result per game date, never count repeated polling as games."""
    if not final:
        return None
    key = f"{sport}:{player_id}"
    games = state.setdefault("players", {}).setdefault(key, [])
    threshold = settings.get("nba_threshold", 6) if sport == "nba" else settings.get("nfl_threshold", 130)
    value = result.get("total_zscore") if sport == "nba" else result.get("pct_of_projection")
    if value is None and sport == "nfl" and result.get("projected_points", 0) > 0:
        value = result["actual_points"] / result["projected_points"] * 100
    qualifies_game = bool(value is not None and value >= threshold and result.get("eligible", True))
    if sport == "nfl":
        qualifies_game = qualifies_game and result.get("actual_points", 0) >= settings.get("nfl_point_floor", 12)
    entry = {"date": str(game_day), "score": value, "qualifies": qualifies_game,
             "minutes": minutes(actual) if sport == "nba" else None}
    games[:] = [g for g in games if g["date"] != str(game_day)] + [entry]
    games.sort(key=lambda g: g["date"])
    games[:] = games[-12:]
    recent = [g for g in games if 0 <= (date.fromisoformat(str(game_day)) - date.fromisoformat(g["date"])).days
              <= (30 if sport == "nba" else 70)][-settings.get("window_games", 5):]
    count = sum(g["qualifies"] for g in recent)
    if settings.get("enabled", True) and qualifies_game and count >= settings.get("min_breakouts", 3):
        return f"Sustained breakout: exceeded the performance bar in {count} of the last {len(recent)} completed games."
    return None
