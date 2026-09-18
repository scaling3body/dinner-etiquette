"""
tier_scraper.py

Fetches and parses NFL position tier lists from fantasyfootballtiers.com
(STD scoring only, per user preference -- not PPR/HALF). Credit to Boris
Chen (github.com/borisachen/fftiers) for pioneering this style of analysis;
fantasyfootballtiers.com republishes it built on FantasyPros consensus data.

HOW PARSING WORKS: each position's page renders a chart image, but the same
tier groupings are also present as plain text below the chart (confirmed by
fetching a live page directly -- lines like "Tier 1: Player A, Player B,
...", one line per tier, in ECR order within each tier). This is NOT an
official API -- it's a third-party site's HTML structure, which could
change without notice. If parsing ever returns nothing, that's the first
thing to check: fetch the URL directly and see if the "Tier N: ..." text
pattern still appears.

NOTE: QB, K, and DST have only one page each (scoring format doesn't
split their tiers the way it does for RB/WR/TE/FLEX, which each have
STD/HALF/PPR variants) -- this module always uses the STD variant where
one exists.
"""

import json
import os
import re
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

BASE = "https://fantasyfootballtiers.com/gallery_files"

POSITION_URLS = {
    "QB": f"{BASE}/QB.html",
    "RB": f"{BASE}/RB-STD.html",
    "WR": f"{BASE}/WR-STD.html",
    "TE": f"{BASE}/TE-STD.html",
    "FLEX": f"{BASE}/FLX-STD.html",
    "K": f"{BASE}/K.html",
    "DST": f"{BASE}/DST.html",
}

TIER_LINE_RE = re.compile(r"^\s*Tier\s*(\d+)\s*:\s*(.+)$", re.IGNORECASE)

CACHE_DIR = "tier_cache"
CACHE_MAX_AGE_HOURS = 12


def _cache_path(position):
    return os.path.join(CACHE_DIR, f"{position.upper()}.json")


def _fetch_and_parse(position):
    """
    Returns an ordered list of (tier_number, player_name) tuples, in ECR
    order within each tier (matching the site's own left-to-right meaning:
    earlier in a tier = higher consensus rank). Returns None on any
    failure -- caller should treat that as "couldn't get tier data this
    time" and fail gracefully, not as "empty tier list."
    """
    url = POSITION_URLS.get(position.upper())
    if not url:
        return None

    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            print(f"tier_scraper: {position} fetch returned status {resp.status_code}")
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text("\n")
    except Exception as e:
        print(f"tier_scraper: failed to fetch {position} ({url}): {e}")
        return None

    results = []
    for line in text.split("\n"):
        m = TIER_LINE_RE.match(line)
        if not m:
            continue
        tier_num = int(m.group(1))
        names = [n.strip() for n in m.group(2).split(",") if n.strip()]
        for name in names:
            results.append((tier_num, name))

    if not results:
        print(f"tier_scraper: found no 'Tier N:' lines for {position} at {url} -- "
              f"the site's page structure may have changed.")
        return None

    return results


def format_time_ago(iso_timestamp):
    """Turns an ISO timestamp into a short relative string like '3h ago' or 'just now'."""
    if not iso_timestamp:
        return "unknown"
    then = datetime.fromisoformat(iso_timestamp)
    delta = datetime.now(timezone.utc) - then
    minutes = int(delta.total_seconds() / 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def get_tier_list(position, max_age_hours=CACHE_MAX_AGE_HOURS):
    """
    Returns {"tiers": [(tier_num, name), ...], "fetched_at": ISO timestamp}
    for a position -- from cache if it's fresh enough, otherwise a new
    fetch. "fetched_at" reflects when THIS BOT last pulled the data, not
    necessarily when the site itself last updated its rankings (that's not
    something the page exposes) -- worth being precise about that
    distinction when showing it to the user. Returns None if fetching
    fails and there's no usable cache to fall back on.
    """
    position = position.upper()
    path = _cache_path(position)

    if os.path.exists(path):
        with open(path) as f:
            cached = json.load(f)
        # Freshness comes from the payload's own "fetched_at", NOT file
        # mtime -- GitHub Actions does a fresh `git checkout` every run,
        # which resets mtime to "now" regardless of the original commit
        # time, so an mtime-based check would always read as fresh and
        # this cache would never actually refresh past its first fetch.
        fetched_at = cached.get("fetched_at")
        if fetched_at:
            age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)).total_seconds() / 3600
            if age_hours < max_age_hours:
                return cached

    fresh = _fetch_and_parse(position)
    if fresh is not None:
        payload = {"tiers": fresh, "fetched_at": datetime.now(timezone.utc).isoformat()}
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f)
        return payload

    # Fetch failed -- fall back to a stale cache rather than nothing, if one exists.
    if os.path.exists(path):
        print(f"tier_scraper: using stale cache for {position} since a fresh fetch failed.")
        with open(path) as f:
            return json.load(f)

    return None
