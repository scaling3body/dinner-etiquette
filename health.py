"""
health.py

Tracks consecutive success/failure per component (sleeper_nfl, sleeper_nba,
yahoo, telegram) across runs. Two things depend on this:

1. FAILURE ALERTS: once a component crosses FAILURE_ALERT_THRESHOLD
   consecutive failures, exactly one Telegram alert is sent (not one per
   run) -- the whole point of this bot is catching things you're not
   watching for, so a silent multi-day outage is the worst failure mode.
   The alert resets once the component recovers, so a later fresh outage
   can alert again.

2. YAHOO CIRCUIT BREAKER: once Yahoo hits YAHOO_BREAKER_THRESHOLD
   consecutive failures (lower than the alert threshold, so it kicks in
   faster), main.py stops even attempting Yahoo calls for
   YAHOO_BREAKER_COOLDOWN_MINUTES, instead of retrying a known-broken
   connection every single run. After the cooldown, one attempt is made;
   success closes the circuit, failure reopens it.

HONEST LIMITATION: if Telegram itself is what's broken, notifying you
*through* Telegram about that obviously might not arrive -- it's still
attempted (transient failures do recover), but the real fallback for a
Telegram-specific outage is the dashboard's health section (step 8),
which doesn't depend on Telegram working at all.

NOTE: sleeper_nfl/sleeper_nba failures are only tracked on days
schedule-aware checking has already confirmed are active game days -- so
"no data" during an active day is a real anomaly, not just an off day.
"""

import json
import os
from datetime import datetime, timedelta, timezone

HEALTH_PATH = "health_state.json"

FAILURE_ALERT_THRESHOLD = 6        # consecutive failures before notifying (~30 min at 5-min interval)
YAHOO_BREAKER_THRESHOLD = 3        # consecutive Yahoo failures before we stop retrying every run
YAHOO_BREAKER_COOLDOWN_MINUTES = 30


def load(path=HEALTH_PATH):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def save(health, path=HEALTH_PATH):
    with open(path, "w") as f:
        json.dump(health, f, indent=2)


def _get(health, component):
    return health.setdefault(component, {
        "consecutive_failures": 0,
        "last_success": None,
        "last_failure": None,
        "notified": False,
        "circuit_open_until": None,
    })


def record_success(health, component):
    c = _get(health, component)
    c["consecutive_failures"] = 0
    c["last_success"] = datetime.now(timezone.utc).isoformat()
    c["notified"] = False
    c["circuit_open_until"] = None


def record_failure(health, component):
    c = _get(health, component)
    c["consecutive_failures"] += 1
    c["last_failure"] = datetime.now(timezone.utc).isoformat()
    return c["consecutive_failures"]


def should_notify(health, component, threshold=FAILURE_ALERT_THRESHOLD):
    """True at most once per outage -- flips "notified" so repeat runs
    during the same ongoing outage don't re-alert, but a fresh outage
    after a recovery (record_success resets "notified") can alert again."""
    c = _get(health, component)
    if c["consecutive_failures"] >= threshold and not c["notified"]:
        c["notified"] = True
        return True
    return False


def open_yahoo_circuit(health, cooldown_minutes=YAHOO_BREAKER_COOLDOWN_MINUTES):
    c = _get(health, "yahoo")
    until = datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)
    c["circuit_open_until"] = until.isoformat()


def yahoo_circuit_is_open(health):
    c = _get(health, "yahoo")
    until = c.get("circuit_open_until")
    if not until:
        return False
    return datetime.now(timezone.utc) < datetime.fromisoformat(until)


def summary_for_dashboard(health):
    """Compact view for docs/data.json -- just what the dashboard needs to show."""
    out = {}
    for component in ("sleeper_nfl", "sleeper_nba", "yahoo", "telegram"):
        c = health.get(component)
        if not c:
            continue
        out[component] = {
            "consecutive_failures": c["consecutive_failures"],
            "last_success": c["last_success"],
            "circuit_open": bool(c.get("circuit_open_until")) and yahoo_circuit_is_open(health) if component == "yahoo" else False,
        }
    return out
