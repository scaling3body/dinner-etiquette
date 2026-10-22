"""
sleeper_client.py

Thin wrapper around Sleeper's API.

IMPORTANT: The player-list and state endpoints below are OFFICIAL and documented
(https://docs.sleeper.com/). The stats/projections endpoints are UNOFFICIAL and
undocumented -- and different community sources disagree on the exact URL shape
(whether it's prefixed with /v1/, and whether season_type is a path segment or
a query parameter). Rather than gamble on one guess, _get_stats_or_projections()
below tries several known candidate formats for NFL. NBA uses weekly record
lists, filtered to the requested date and normalized separately. Set DEBUG
= True to see requests in the Actions log.
"""

import requests
import time
from datetime import date, timedelta
from scoring import number

DEBUG = False

OFFICIAL_BASE = "https://api.sleeper.app/v1"

_session = requests.Session()
_request_cache = {}


def _get(url, params=None):
    cache_key = (url, tuple(sorted((params or {}).items())))
    if cache_key in _request_cache:
        return _request_cache[cache_key]
    for attempt in range(3):
        try:
            resp = _session.get(url, params=params, timeout=15)
            if DEBUG:
                print(f"GET {resp.url} -> {resp.status_code}")
            if resp.status_code == 200:
                result = resp.json()
                _request_cache[cache_key] = result
                return result
            if resp.status_code in (400, 401, 403, 404):
                break  # retrying the same invalid endpoint only wastes time
            time.sleep(1.5 * (attempt + 1))
        except (requests.RequestException, ValueError) as e:
            if DEBUG:
                print(f"Request error: {e}")
            time.sleep(1.5 * (attempt + 1))
    _request_cache[cache_key] = None
    return None


def _candidate_urls(kind, sport, season_type, season, period):
    """
    kind: 'stats' or 'projections'. period: NFL week number.
    Returns a list of (url, params) tuples to try in order, covering the
    different URL shapes seen in the wild for this undocumented endpoint.
    """
    return [
        # Shape A: /v1/ prefixed, season_type as a path segment (most commonly
        # cited shape in community write-ups)
        (f"https://api.sleeper.app/v1/{kind}/{sport}/{season_type}/{season}/{period}", None),
        # Shape B: no /v1/, season_type as a path segment
        (f"https://api.sleeper.app/{kind}/{sport}/{season_type}/{season}/{period}", None),
        # Shape C: no /v1/, season_type as a query parameter (seen in some Go/JS clients)
        (f"https://api.sleeper.app/{kind}/{sport}/{season}/{period}", {"season_type": season_type}),
        # Shape D: /v1/ prefixed, season_type as a query parameter
        (f"https://api.sleeper.app/v1/{kind}/{sport}/{season}/{period}", {"season_type": season_type}),
    ]


def _get_stats_or_projections(kind, sport, season_type, season, period):
    for url, params in _candidate_urls(kind, sport, season_type, season, period):
        result = _get(url, params=params)
        # A dict with at least one player entry is what we're after -- an
        # empty dict or non-dict response means this shape didn't work.
        if isinstance(result, dict) and any(isinstance(v, dict) and v for v in result.values()):
            if DEBUG:
                print(f"SUCCESS: {kind}/{sport} resolved via {url} (params={params})")
            return result
    if DEBUG:
        print(f"All URL shapes failed for {kind}/{sport} season_type={season_type} "
              f"season={season} period={period}")
    return None


def _candidate_schedule_urls(sport, season_type, season):
    """
    Candidate shapes for the unofficial schedule endpoint. Confirmed shape
    (per a recent third-party client, Sept 2026) is season_type as a path
    segment: /schedule/{sport}/{season_type}/{season}. NBA equivalent is
    unverified -- if none of these work, get_schedule() returns None and
    callers just omit game-status info gracefully.
    """
    return [
        (f"https://api.sleeper.app/v1/schedule/{sport}/{season_type}/{season}", None),
        (f"https://api.sleeper.app/schedule/{sport}/{season_type}/{season}", None),
    ]


def get_schedule(sport, season, season_type="regular"):
    """
    Returns a list of games for the season/week context Sleeper currently
    has loaded, each with a 'status' of pre_game/in_game/complete/canceled,
    plus 'home'/'away' team abbreviations -- or None if no candidate URL
    shape worked (this endpoint is unofficial and its exact shape isn't
    100% confirmed, especially for NBA).
    """
    for url, params in _candidate_schedule_urls(sport, season_type, season):
        result = _get(url, params=params)
        if isinstance(result, list) and len(result) > 0:
            if DEBUG:
                print(f"SUCCESS: schedule/{sport} resolved via {url}")
            return result
    if DEBUG:
        print(f"All URL shapes failed for schedule/{sport} season_type={season_type} season={season}")
    return None


def get_team_game_status(schedule, team_abbr, week=None):
    """
    Given a schedule list from get_schedule() and a team abbreviation,
    returns that team's game status ('pre_game'/'in_game'/'complete'/
    'canceled'), or None if not found (schedule unavailable, team on bye,
    or team abbreviation didn't match -- e.g. 'FA' for a free agent).

    get_schedule()'s URL has no week segment, so this list may span the
    whole season rather than just the current week -- a team's FIRST
    matching entry could be an old, already-completed game rather than
    today's. If a `week` is given and entries carry a 'week' field, this
    filters to that week first (comparing as strings so an int/str type
    mismatch between the schedule's 'week' and the caller's `week` -- an
    unverified third-party API, so its exact types aren't guaranteed --
    doesn't cause a spurious miss).

    If NO entry declares a week at all, this falls back to the old
    first-match behavior (best effort against an unknown schema). But if
    entries DO declare a week and none matches, that's suspicious rather
    than "field absent" -- returning the wrong-week status would be
    exactly the stale-tag bug this filtering exists to prevent, so this
    returns None (no tag shown) instead of guessing.
    """
    if not schedule or not team_abbr:
        return None
    matches = [g for g in schedule if g.get("home") == team_abbr or g.get("away") == team_abbr]
    if not matches:
        return None
    if week is not None:
        this_week = [g for g in matches if g.get("week") is not None and str(g.get("week")) == str(week)]
        if this_week:
            return this_week[0].get("status")
        if any(g.get("week") is not None for g in matches):
            if DEBUG:
                print(f"get_team_game_status: {team_abbr} has schedule entries with a 'week' "
                      f"field, but none match week={week!r} -- withholding status rather than "
                      f"guessing from a different week.")
            return None
    return matches[0].get("status")


def get_all_players(sport):
    """
    sport: 'nfl' or 'nba'
    Returns dict keyed by player_id. This payload is large (multiple MB) and
    only changes a few times a day, so callers should cache it to disk rather
    than fetching every run.
    """
    return _get(f"{OFFICIAL_BASE}/players/{sport}")


def get_nfl_state():
    """Returns current NFL season/week info, e.g. {'week': 3, 'season': '2026', ...}"""
    return _get(f"{OFFICIAL_BASE}/state/nfl")


def get_nba_state():
    """Returns current NBA season info."""
    return _get(f"{OFFICIAL_BASE}/state/nba")


def get_nfl_week_projections(season, week, season_type="regular"):
    """Unofficial endpoint. Returns dict keyed by player_id -> projected stat dict."""
    return _get_stats_or_projections("projections", "nfl", season_type, season, week)


def get_nfl_week_stats(season, week, season_type="regular"):
    """Unofficial endpoint. Returns dict keyed by player_id -> actual stat dict."""
    return _get_stats_or_projections("stats", "nfl", season_type, season, week)


def _normalize_nba_stats(stats):
    """Translate Sleeper's shooting fields to the configured nine categories."""
    normalized = {key: number(value) for key, value in stats.items()}
    normalized["min"] = normalized.get("min") or normalized.get("sp", 0) / 60
    normalized["fg3m"] = number(stats.get("tpm", stats.get("fg3m", 0)))
    for percentage, made, attempted in (("fgp", "fgm", "fga"), ("ftp", "ftm", "fta")):
        attempts = normalized.get(attempted) or 0
        normalized[percentage] = (normalized.get(made, 0) / attempts if attempts else number(stats.get(percentage)))
    return normalized


def _get_nba_day_data(kind, date_str, season, season_type, week):
    # NBA uses a WEEK in the path, not a date. The non-v1 endpoint returns
    # records with player_id/date/stats; the v1 endpoint can return thousands
    # of empty player dictionaries with HTTP 200. Even with a date query,
    # Sleeper can return the whole week, so filter each record explicitly.
    if week is None:
        state = get_nba_state() or {}
        if str(state.get("season")) != str(season):
            return None
        week = state.get("week")
    if not week:
        return None
    records = _get(f"https://api.sleeper.app/{kind}/nba/{season}/{week}", params={
        "season_type": season_type, "grouping": "day", "date": date_str,
    })
    if not isinstance(records, list):
        return None
    output = {}
    for record in records:
        if not isinstance(record, dict) or record.get("date") != date_str or record.get("player_id") is None:
            continue
        if not isinstance(record.get("stats"), dict) or not record["stats"]:
            continue
        stats = _normalize_nba_stats(record["stats"])
        player = record.get("player") or {}
        stats["_meta"] = {"game_id": str(record.get("game_id") or ""), "date": date_str,
                          "team": record.get("team"), "opponent": record.get("opponent"),
                          "name": " ".join(filter(None, (player.get("first_name"), player.get("last_name")))),
                          "updated_at": record.get("updated_at"), "week": record.get("week"),
                          "position": player.get("position")}
        output[str(record["player_id"])] = stats
    return output


def nba_week_for_day(day, state):
    """Use the requested slate's week across Monday and season boundaries."""
    target = date.fromisoformat(str(day))
    start = state.get("season_start_date")
    if start:
        opening = date.fromisoformat(start)
        first_monday = opening - timedelta(days=opening.weekday())
        return max(1, (target - first_monday).days // 7 + 1)
    return state.get("week")


def get_nba_day_projections(date_str, season, season_type="regular", week=None):
    """Return normalized player projections for the requested Eastern date."""
    return _get_nba_day_data("projections", date_str, season, season_type, week)


def get_nba_day_stats(date_str, season, season_type="regular", week=None):
    """Return normalized player stats for the requested Eastern date."""
    return _get_nba_day_data("stats", date_str, season, season_type, week)
