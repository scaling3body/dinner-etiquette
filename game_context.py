"""Optional ESPN scoreboard context. Unknown/mismatched games stay unknown."""
import requests
from state_store import utcnow

_session = requests.Session()
_cache = {}
ALIASES = {"WSH": "WAS", "NY": "NYK", "GS": "GSW", "SA": "SAS", "NO": "NOP",
           "PHO": "PHX", "UTAH": "UTA", "BRK": "BKN", "JAC": "JAX", "LAR": "LA"}


def team_key(team):
    team = (team or "").upper()
    return ALIASES.get(team, team)


def fetch_games(sport, day):
    key = (sport, str(day))
    if key in _cache:
        return _cache[key]
    league = "basketball/nba" if sport == "nba" else "football/nfl"
    try:
        response = _session.get(f"https://site.api.espn.com/apis/site/v2/sports/{league}/scoreboard",
                                params={"dates": str(day).replace("-", ""), "limit": 100}, timeout=8)
        response.raise_for_status()
        data = response.json()
        events = data.get("events")
        if not isinstance(events, list):
            raise ValueError("invalid scoreboard")
        games = []
        for event in events:
            status = event.get("status", {})
            kind = status.get("type", {})
            season = event.get("season") or data.get("season") or (data.get("leagues") or [{}])[0].get("season", {})
            week = event.get("week") or data.get("week") or {}
            for competition in event.get("competitions", []):
                teams = [team_key(c.get("team", {}).get("abbreviation"))
                         for c in competition.get("competitors", [])]
                if len(teams) != 2 or not all(teams):
                    continue
                games.append({"id": str(event["id"]), "date": str(day), "teams": teams,
                              "week": week.get("number"),
                              "season": season.get("year"),
                              "season_type": {1: "pre", 2: "regular", 3: "post"}.get(season.get("type") if not isinstance(season.get("type"), dict) else int(season["type"].get("id", 2)), "regular"),
                              "matchup": " vs ".join(teams), "state": kind.get("state"),
                              "final": bool(kind.get("completed")), "period": status.get("period", 0),
                              "clock": status.get("displayClock"), "remaining_seconds": status.get("clock"),
                              "detail": kind.get("shortDetail") or kind.get("description"),
                              "source": "ESPN", "fetched_at": utcnow().isoformat()})
        _cache[key] = games
    except (requests.RequestException, ValueError, KeyError, TypeError):
        _cache[key] = None
    return _cache[key]


def for_team(games, team, opponent=None):
    matches = [g for g in games or [] if team_key(team) in g["teams"]
               and (not opponent or team_key(opponent) in g["teams"])]
    return matches[0] if len(matches) == 1 else None


def display(game):
    if not game:
        return None
    if game.get("final"):
        return "Final"
    if game.get("state") == "in":
        period = game.get("period", 0)
        part = f"Q{period}" if period <= 4 else f"OT{period - 4}"
        return f"{part} · {game.get('clock') or 'Live'}"
    return game.get("detail")


def elapsed_minutes(game):
    if not game or game.get("state") != "in":
        return None
    period = int(game.get("period") or 0)
    remaining = game.get("remaining_seconds")
    if period < 1 or remaining is None:
        return None
    if period <= 4:
        return (period - 1) * 12 + 12 - float(remaining) / 60
    return 48 + (period - 5) * 5 + 5 - float(remaining) / 60
