"""
scoring.py

Turns raw Sleeper stat dicts into:
  - football: a single fantasy point total, compared against projection
  - basketball: a 9-cat "z-score" total representing how far above a typical
    game this performance was, relative to what was projected

REPEAT-BREAKOUT ESCALATION: a player isn't just alerted once and then
ignored for the rest of the game. Each alert sets a new "baseline" at that
player's current level, and the NEXT alert only fires once they clear the
same threshold again relative to that new baseline -- so a player who
plateaus after breaking out doesn't spam repeat alerts, but one who keeps
climbing gets "DOUBLE BREAKOUT", "3x BREAKOUT", etc. See
football_breakout_level() / basketball_breakout_level() below.

EXTREME-OUTLIER FLAG: independent of the repeat-breakout count, each result
also carries "is_extreme" -- true when the performance clears extreme_multiplier
times the normal trigger bar (relative to the ORIGINAL pre-game projection,
not the escalating baseline), so truly rare games stand out from routine
breakouts regardless of how many times that player has already alerted today.

Basketball z-score approach (heuristic, documented so it can be tuned):
For each category, we compute the standard deviation of *projected* values
across the full slate of players playing that day. That tells us how much
games in that category normally vary. We then measure how many of those
standard deviations this player's ACTUAL result beat their OWN projection by,
and sum across categories. Turnovers are inverted (fewer than projected is
good). This rewards a genuine outlier game across many categories, not just
one huge stat line.
"""

import statistics

DEFAULT_EXTREME_MULTIPLIER = 1.5


def breakout_label(count, is_extreme=False):
    """1 -> 'BREAKOUT', 2 -> 'DOUBLE BREAKOUT', 3+ -> 'Nx BREAKOUT'. Extreme results get a fire prefix."""
    if count <= 1:
        base = "BREAKOUT"
    elif count == 2:
        base = "DOUBLE BREAKOUT"
    else:
        base = f"{count}x BREAKOUT"
    return f"\U0001F525 {base}" if is_extreme else base


# ---------- Football ----------

def calc_fantasy_points(stat_line, scoring_settings):
    if not stat_line:
        return 0.0
    total = 0.0
    for stat_key, weight in scoring_settings.items():
        if not isinstance(weight, (int, float)):
            continue  # skips "_comment_*" explanatory keys in config.json
        total += stat_line.get(stat_key, 0) * weight
    return round(total, 2)


def football_breakout_level(actual_stats, projected_stats, scoring_settings,
                             pct_threshold, point_floor, prior_count, prior_baseline,
                             extreme_multiplier=DEFAULT_EXTREME_MULTIPLIER):
    """
    Returns a dict describing this check's result:
      {"is_breakout": bool, "is_extreme": bool, "count": int, "baseline": float,
       "actual_points": float, "projected_points": float, "pct_of_projection": float}

    prior_count: 0 if this player has never broken out yet today/this week.
    prior_baseline: the actual_points value recorded at their last alert, or
      None if prior_count is 0.

    First breakout: actual >= point_floor AND actual >= projected * pct_threshold.
    Every breakout after that: actual >= prior_baseline * pct_threshold (the
    point_floor doesn't re-apply -- they already cleared it to get here).
    is_extreme: actual >= projected * pct_threshold * extreme_multiplier,
    always measured against the original projection regardless of count.
    """
    proj_pts = calc_fantasy_points(projected_stats, scoring_settings)
    actual_pts = calc_fantasy_points(actual_stats, scoring_settings)
    is_extreme = bool(proj_pts > 0 and actual_pts >= proj_pts * pct_threshold * extreme_multiplier)

    if prior_count == 0:
        if proj_pts > 0 and actual_pts >= point_floor and actual_pts >= proj_pts * pct_threshold:
            return {
                "is_breakout": True, "is_extreme": is_extreme, "count": 1, "baseline": actual_pts,
                "actual_points": actual_pts, "projected_points": proj_pts,
                "pct_of_projection": round(actual_pts / proj_pts * 100, 1) if proj_pts else None,
            }
        return {"is_breakout": False, "is_extreme": False, "count": 0, "baseline": None,
                "actual_points": actual_pts, "projected_points": proj_pts, "pct_of_projection": None}

    required = prior_baseline * pct_threshold
    if actual_pts >= required:
        return {
            "is_breakout": True, "is_extreme": is_extreme, "count": prior_count + 1, "baseline": actual_pts,
            "actual_points": actual_pts, "projected_points": proj_pts,
            "pct_of_projection": round(actual_pts / proj_pts * 100, 1) if proj_pts else None,
        }
    return {"is_breakout": False, "is_extreme": False, "count": prior_count, "baseline": prior_baseline,
            "actual_points": actual_pts, "projected_points": proj_pts, "pct_of_projection": None}


# ---------- Basketball ----------

def _category_stdevs(all_projections, categories):
    """all_projections: dict of player_id -> projected stat dict for the day's slate."""
    stdevs = {}
    for cat in categories:
        values = [
            p.get(cat, 0)
            for p in all_projections.values()
            if p and p.get(cat) is not None
        ]
        values = [v for v in values if v > 0]
        if len(values) >= 5:
            stdevs[cat] = statistics.pstdev(values) or 1.0
        else:
            stdevs[cat] = 1.0  # fallback so we never divide by zero
    return stdevs


def compute_zscore(actual_stats, projected_stats, all_day_projections, categories, invert_categories):
    stdevs = _category_stdevs(all_day_projections, categories)
    total_z = 0.0
    breakdown = {}
    for cat in categories:
        actual_val = actual_stats.get(cat, 0) or 0
        proj_val = projected_stats.get(cat, 0) or 0
        diff = actual_val - proj_val
        if cat in invert_categories:
            diff = -diff
        z = diff / stdevs[cat]
        total_z += z
        breakdown[cat] = {"actual": actual_val, "projected": proj_val, "z": round(z, 2)}
    return total_z, breakdown


def basketball_breakout_level(actual_stats, projected_stats, all_day_projections,
                               categories, invert_categories, zscore_threshold,
                               prior_count, prior_baseline,
                               extreme_multiplier=DEFAULT_EXTREME_MULTIPLIER):
    """
    Same escalation pattern as football_breakout_level(), but additive
    instead of multiplicative since z-scores can be zero or negative:
    each repeat breakout requires total_z >= prior_baseline + zscore_threshold.
    is_extreme: total_z >= zscore_threshold * extreme_multiplier, always
    measured against the base threshold regardless of count.
    """
    if not actual_stats or not projected_stats:
        return {"is_breakout": False, "is_extreme": False, "count": prior_count, "baseline": prior_baseline,
                "total_zscore": None, "breakdown": None}

    total_z, breakdown = compute_zscore(actual_stats, projected_stats, all_day_projections,
                                         categories, invert_categories)
    total_z = round(total_z, 2)
    is_extreme = bool(total_z >= zscore_threshold * extreme_multiplier)

    if prior_count == 0:
        if total_z >= zscore_threshold:
            return {"is_breakout": True, "is_extreme": is_extreme, "count": 1, "baseline": total_z,
                    "total_zscore": total_z, "breakdown": breakdown}
        return {"is_breakout": False, "is_extreme": False, "count": 0, "baseline": None,
                "total_zscore": total_z, "breakdown": breakdown}

    required = prior_baseline + zscore_threshold
    if total_z >= required:
        return {"is_breakout": True, "is_extreme": is_extreme, "count": prior_count + 1, "baseline": total_z,
                "total_zscore": total_z, "breakdown": breakdown}
    return {"is_breakout": False, "is_extreme": False, "count": prior_count, "baseline": prior_baseline,
            "total_zscore": total_z, "breakdown": breakdown}
