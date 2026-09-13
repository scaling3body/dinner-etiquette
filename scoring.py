"""
scoring.py

Turns raw Sleeper stat dicts into:
  - football: a single fantasy point total, compared against projection
  - basketball: a 9-cat "z-score" total representing how far above a typical
    game this performance was, relative to what was projected

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


def check_football_breakout(actual_stats, projected_stats, scoring_settings,
                             pct_threshold, point_floor):
    proj_pts = calc_fantasy_points(projected_stats, scoring_settings)
    actual_pts = calc_fantasy_points(actual_stats, scoring_settings)

    if proj_pts <= 0:
        return None  # no meaningful projection to compare against

    if actual_pts >= point_floor and actual_pts >= proj_pts * pct_threshold:
        return {
            "actual_points": actual_pts,
            "projected_points": proj_pts,
            "pct_of_projection": round(actual_pts / proj_pts * 100, 1),
        }
    return None


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


def check_basketball_breakout(player_id, actual_stats, projected_stats,
                               all_day_projections, categories,
                               invert_categories, zscore_threshold):
    if not actual_stats or not projected_stats:
        return None

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

    if total_z >= zscore_threshold:
        return {"total_zscore": round(total_z, 2), "breakdown": breakdown}
    return None
