"""Persist only approved bot state paths; fail visibly if GitHub rejects it."""
import glob
import os
import subprocess
import sys

PROFILES = {
    "alerts": ["alerted.json", "pending.json", "telegram_offset.json", "dashboard_history.json",
               "health_state.json", "tier_threshold_state.json", "docs/data.json", "delivery_state.json",
               "runtime_state.json", "performance_state.json", "players_nfl_cache.json", "players_nba_cache.json",
               "players_nfl_cache.json.fetched_at", "players_nba_cache.json.fetched_at", "tier_cache/*.json"],
    "digest": ["digest_state.json"],
    "schedule": ["schedule_windows.json"],
}


def git(*args, check=True):
    return subprocess.run(["git", *args], check=check).returncode


def main(profile):
    files = [path for pattern in PROFILES[profile] for path in glob.glob(pattern) if os.path.isfile(path)]
    if not files:
        return
    git("config", "user.name", "fantasy-alert-bot")
    git("config", "user.email", "actions@users.noreply.github.com")
    git("add", "--", *files)
    if git("diff", "--cached", "--quiet", check=False) == 0:
        return
    git("commit", "-m", f"Update {profile} state [skip ci]")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    for attempt in range(3):
        git("pull", "--rebase", "origin", branch)
        if git("push", "origin", f"HEAD:{branch}", check=False) == 0:
            return
    raise RuntimeError("State push failed after three attempts")


if __name__ == "__main__":
    main(sys.argv[1])
