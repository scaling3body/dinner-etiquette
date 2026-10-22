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
reusing it across every alert in a run avoids re-scanning Yahoo's API for
every single player.
"""

import re
import unicodedata
from yahoo_oauth import OAuth2
import yahoo_fantasy_api as yfa

STATUS_LABELS = {
    "FA": "Available as FA",
    "W": "On waivers",
}

ALL_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF",
                  "PG", "SG", "SF", "PF", "C", "G", "F", "Util")


def _normalize(name):
    if isinstance(name, dict):
        name = name.get("full", "")
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", name)
    return re.sub(r"[^a-z]", "", name)


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
        self.sport = sport
        self._roster_cache = None
        self._settings_cache = None
        self._name_index = None

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
        positions = ("QB", "RB", "WR", "TE", "K", "DEF") if self.sport == "nfl" else ("PG", "SG", "SF", "PF", "C")
        # A failed scan must not silently classify missing players as rostered.
        for pos in positions:
            candidates.extend(dict(p, status="FA") for p in self.league.free_agents(pos))
        candidates.extend(dict(p, status="W") for p in self.league.waivers())

        seen_ids = set()
        deduped = []
        for c in candidates:
            if c["player_id"] not in seen_ids:
                seen_ids.add(c["player_id"])
                deduped.append(c)

        self._free_agent_cache = deduped
        self._name_index = {}
        for candidate in deduped:
            self._name_index.setdefault(_normalize(candidate["name"]), []).append(candidate)
        return deduped

    def roster(self):
        if self._roster_cache is None:
            self._roster_cache = self.team.roster()
        return self._roster_cache

    def roster_context(self, position=None):
        roster = self.roster()
        injured = sum(bool(p.get("status") or p.get("injury_status")) for p in roster)
        matches = sum(position in (p.get("eligible_positions") or []) for p in roster) if position else None
        parts = ["Open active/bench slot" if self.get_open_bench_or_ir_slots() else "Roster full; no automatic drop suggested"]
        if matches is not None:
            parts.append(f"{matches} roster players eligible at {position}")
        if injured:
            parts.append(f"{injured} roster players carry status flags")
        return "; ".join(parts)

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

        allowed = {position: int(info.get("count", 0)) for position, info in self.league.positions().items()}
        roster = self.roster()
        reserved = {"IR", "IR+", "IL", "IL+", "NA"}
        capacity = sum(count for position, count in allowed.items() if position not in reserved)
        filled = sum(p.get("selected_position") not in reserved for p in roster)
        # An empty IR-only slot does not make a healthy free agent addable.
        self._open_slot_cache = capacity > filled
        return self._open_slot_cache

    def find_player(self, full_name, team_abbr=None, position=None):
        """
        Looks for a matching free agent or waiver player in the league by
        name. Returns a dict with player_id/name/status, or None if not
        found (e.g. the player is rostered by another team already).

        If multiple free agents share the exact same name (rare but
        possible), team_abbr and then position are used to disambiguate --
        purely a safety net: this never causes a valid single match to be
        rejected, it only narrows down when there's a genuine collision.
        If still ambiguous after both filters, falls back to the first
        match, same as before this existed.
        """
        target = _normalize(full_name)
        self._get_available_players()
        matches = list(self._name_index.get(target, []))

        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]

        # Collision: narrow by team first.
        if team_abbr:
            team_matches = [c for c in matches if c.get("editorial_team_abbr", "").lower() == team_abbr.lower()]
            if team_matches:
                matches = team_matches
                if len(matches) == 1:
                    return matches[0]
            else:
                raise ValueError("Ambiguous Yahoo names with no team match")

        # Still ambiguous -- narrow by position if we have it on both sides.
        if position:
            def _candidate_positions(c):
                elig = c.get("eligible_positions") or []
                single = c.get("position") or c.get("display_position")
                return set(elig) | ({single} if single else set())

            pos_matches = [c for c in matches if position in _candidate_positions(c)]
            if pos_matches:
                matches = pos_matches

        if len(matches) != 1:
            raise ValueError("Ambiguous Yahoo player match; no roster action offered")
        return matches[0]

    def get_player_status(self, full_name, team_abbr=None, position=None):
        """
        Returns (status_label, player_dict_or_None) for use in alert text.
        status_label is one of "Available as FA", "On waivers", "Rostered",
        or "Available" (matched but status field unrecognized).
        player_dict is the matched Yahoo player record if found (needed to
        add them), or None if not found/rostered.
        """
        match = self.find_player(full_name, team_abbr, position)
        if not match:
            own = any(_normalize(p.get("name")) == _normalize(full_name) for p in self.roster())
            return "On your roster" if own else "Rostered / unavailable", None
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
        self._roster_cache = self._open_slot_cache = self._free_agent_cache = self._name_index = None
        if not self.get_open_bench_or_ir_slots():
            raise ValueError("No open bench/IR slot; roster move refused")
        available = next((p for p in self._get_available_players() if str(p["player_id"]) == str(player_id)), None)
        if not available or available.get("status") != "FA":
            raise ValueError("Player is not currently a free agent; roster move refused")
        self.team.add_player(player_id)
        self._roster_cache = self._open_slot_cache = self._free_agent_cache = self._name_index = None
