"""
yahoo_client.py

Wraps the `yahoo_fantasy_api` + `yahoo_oauth` libraries for what this bot
needs: checking for an open roster spot, looking up a player's availability
status, and adding a player.

NOTE ON NAME MATCHING: Sleeper and Yahoo don't share player IDs, so we match
players by normalized full name (+ team where possible). This is reliable in
the vast majority of cases but can occasionally misfire for players sharing a
name -- if that ever happens, cross-check the suggestion text against
Yahoo before replying YES.

NOTE ON AVAILABILITY STATUS: Yahoo's player status field distinguishes "FA"
(free agent, claimable instantly) from "W" (on waivers, in the locked claim
period). There isn't a separate third state for "recently dropped, not yet
even on waivers" -- that's just what "on waivers" means. A player not found
in the free-agent/waiver scan at all is assumed to be rostered by a team in
your league (see the name-matching caveat above for why this is a "probably,"
not a certainty).

NOTE ON FAAB: if your league uses FAAB bidding (a dollar amount per waiver
claim) rather than plain waiver priority, this code does not currently submit
a bid amount and will need a small extension -- see add_player() below.

PERFORMANCE NOTE: create one YahooRosterManager per sport per run (not one
per breakout) -- it caches the free-agent scan for its own lifetime, so
reusing one instance across every alert in a run avoids re-scanning Yahoo's
API for every single player.
"""

import re
from yahoo_oauth import OAuth2
import yahoo_fantasy_api as yfa

STATUS_LABELS = {
    "FA": "Available as FA",
    "W": "On waivers",
}

ALL_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF",
                  "PG", "SG", "SF", "PF", "C", "G", "F", "Util")


def _normalize(name):
    return re.sub(r"[^a-z]", "", name.lower())


class YahooRosterManager:
    def __init__(self, oauth_json_path, sport, league_key, team_key):
        """
        sport: 'nfl' or 'nba'
        league_key / team_key: e.g. '423.l.123456' / '423.l.123456.t.4'
        Find these once by running league_ids()/team_key() interactively,
        or from your league's Yahoo URL.
        """
        self.sc = OAuth2(None, None, from_file=oauth_json_path)
        self.game = yfa.Game(self.sc, sport)
        self.league = self.game.to_league(league_key)
        self.team = self.league.to_team(team_key)
        self._free_agent_cache = None  # populated lazily, reused for this instance's life
        self._open_slot_cache = None

    def _get_available_players(self):
        """
        Returns the deduped list of free agent/waiver players across all
        relevant positions, fetched once and cached for the life of this
        manager instance -- reuse one instance across a whole run's worth
        of breakouts rather than creating a new one per player.
        """
        if self._free_agent_cache is not None:
            return self._free_agent_cache

        candidates = []
        for pos in ALL_POSITIONS:
            try:
                candidates.extend(self.league.free_agents(pos))
            except Exception:
                continue

        seen_ids = set()
        deduped = []
        for c in candidates:
            if c["player_id"] not in seen_ids:
                seen_ids.add(c["player_id"])
                deduped.append(c)

        self._free_agent_cache = deduped
        return deduped

    def get_open_bench_or_ir_slots(self):
        """
        Returns True if the roster currently has an unfilled bench or IR
        slot relative to league roster settings. This is a heuristic --
        double check against the Yahoo app if it seems wrong. Cached for
        this manager instance's lifetime (one run) since roster composition
        doesn't change mid-run.
        """
        if self._open_slot_cache is not None:
            return self._open_slot_cache

        settings = self.league.settings()
        roster_positions = settings.get("roster_positions", [])
        allowed = {}
        for rp in roster_positions:
            pos = rp.get("roster_position", {}).get("position")
            count = int(rp.get("roster_position", {}).get("count", 0))
            if pos:
                allowed[pos] = allowed.get(pos, 0) + count

        roster = self.team.roster()
        filled = {}
        for p in roster:
            pos = p.get("selected_position")
            if pos:
                filled[pos] = filled.get(pos, 0) + 1

        for bench_like in ("BN", "IR", "IR+"):
            if allowed.get(bench_like, 0) > filled.get(bench_like, 0):
                self._open_slot_cache = True
                return True
        self._open_slot_cache = False
        return False

    def find_player(self, full_name, team_abbr=None):
        """
        Looks for a matching free agent or waiver player in the league by
        name. Returns a dict with player_id/name/status, or None if not
        found (e.g. the player is rostered by another team already).
        """
        target = _normalize(full_name)
        for c in self._get_available_players():
            if _normalize(c["name"]) == target:
                if team_abbr and c.get("editorial_team_abbr", "").lower() != team_abbr.lower():
                    continue
                return c
        return None

    def get_player_status(self, full_name, team_abbr=None):
        """
        Returns (status_label, player_dict_or_None) for use in alert text.
        status_label is one of "Available as FA", "On waivers", "Rostered",
        or "Available" (matched but status field unrecognized).
        player_dict is the matched Yahoo player record if found (needed to
        add them), or None if not found/rostered.
        """
        match = self.find_player(full_name, team_abbr)
        if not match:
            return "Rostered", None
        label = STATUS_LABELS.get(match.get("status"), "Available")
        return label, match

    def add_player(self, player_id):
        """
        Adds a free agent or submits a waiver claim for player_id (Yahoo
        handles which one automatically based on the player's current
        status). Does NOT drop anyone -- only call this when
        get_open_bench_or_ir_slots() is True.

        FAAB leagues: this call does not currently pass a bid amount. If
        your league uses FAAB, this needs `faab_bid=...` added -- see
        yahoo_fantasy_api's Team.add_player signature for your installed
        version.
        """
        self.team.add_player(player_id)
