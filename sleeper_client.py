"""
sleeper_client.py

Thin wrapper around Sleeper's API.

IMPORTANT: The player-list and state endpoints below are OFFICIAL and documented
(https://docs.sleeper.com/). The stats/projections endpoints are UNOFFICIAL and
undocumented -- widely used by the fantasy dev community, but Sleeper could change
or break them without notice. If this bot suddenly stops finding stats, this is
the file to check/fix first. Print the raw response (see DEBUG flag) to see what
changed.
"""

import requests
import time

DEBUG = False

OFFICIAL_BASE = "https://api.sleeper.app/v1"
UNOFFICIAL_BASE = "https://api.sleeper.app"  # used for projections/stats

_session = requests.Session()


def _get(url, params=None):
    for attempt in range(3):
        try:
            resp = _session.get(url, params=params, timeout=15)
            if DEBUG:
                print(f"GET {resp.url} -> {resp.status_code}")
            if resp.status_code == 200:
                return resp.json()
            time.sleep(1.5 * (attempt + 1))
        except requests.RequestException as e:
            if DEBUG:
                print(f"Request error: {e}")
            time.sleep(1.5 * (attempt + 1))
    return None


def _as_player_dict(data):
    """
    The unofficial stats/projections endpoints are supposed to return a dict
    keyed by player_id, but have been observed to sometimes return a list of
    per-player records instead. Normalize either shape into a
    {player_id: stat_dict} dict so callers never have to care which one came
    back.
    """
    if data is None:
        return None
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        normalized = {}
        for entry in data:
            pid = entry.get("player_id")
            if pid is not None:
                normalized[str(pid)] = entry
        return normalized
    return data


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
    url = f"{UNOFFICIAL_BASE}/projections/nfl/{season_type}/{season}/{week}"
    return _as_player_dict(_get(url))


def get_nfl_week_stats(season, week, season_type="regular"):
    """Unofficial endpoint. Returns dict keyed by player_id -> actual stat dict."""
    url = f"{UNOFFICIAL_BASE}/stats/nfl/{season_type}/{season}/{week}"
    return _as_player_dict(_get(url))


def get_nba_day_projections(date_str, season, season_type="regular"):
    """
    Unofficial endpoint. date_str format: 'YYYY-MM-DD'.
    Returns dict keyed by player_id -> projected stat dict for that day's games.
    """
    url = f"{UNOFFICIAL_BASE}/projections/nba/{season_type}/{date_str}"
    return _as_player_dict(_get(url))


def get_nba_day_stats(date_str, season, season_type="regular"):
    """Unofficial endpoint. Returns dict keyed by player_id -> actual stat dict for that day."""
    url = f"{UNOFFICIAL_BASE}/stats/nba/{season_type}/{date_str}"
    return _as_player_dict(_get(url))
