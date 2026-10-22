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

from datetime import datetime, timedelta, timezone
from state_store import load as load_json, save as save_json, timestamp

HEALTH_PATH = "health_state.json"

FAILURE_ALERT_THRESHOLD = 6        # consecutive failures before notifying (~30 min at 5-min interval)
YAHOO_BREAKER_THRESHOLD = 3        # consecutive Yahoo failures before we stop retrying every run
YAHOO_BREAKER_COOLDOWN_MINUTES = 30


def load(path=HEALTH_PATH):
    return load_json(path, {})


def save(health, path=HEALTH_PATH):
    save_json(path, health)


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
    """Delivery caller sets notified only after the failure alert succeeds."""
    c = _get(health, component)
    if c["consecutive_failures"] >= threshold and not c["notified"]:
        return True
    return False


def open_yahoo_circuit(health, cooldown_minutes=YAHOO_BREAKER_COOLDOWN_MINUTES, component="yahoo"):
    c = _get(health, component)
    until = datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)
    c["circuit_open_until"] = until.isoformat()


def yahoo_circuit_is_open(health, component="yahoo"):
    c = _get(health, component)
    until = timestamp(c.get("circuit_open_until"))
    if not until:
        return False
    return datetime.now(timezone.utc) < until


def summary_for_dashboard(health):
    """Compact view for docs/data.json -- just what the dashboard needs to show."""
    out = {}
    for component in ("sleeper_nfl", "sleeper_nba", "sleeper_nba_catchup", "scoreboard", "yahoo", "yahoo_nfl", "yahoo_nba", "telegram"):
        c = health.get(component)
        if not c:
            continue
        out[component] = {
            "consecutive_failures": c["consecutive_failures"],
            "last_success": c["last_success"],
            "last_failure": c.get("last_failure"),
            "circuit_open": yahoo_circuit_is_open(health, component) if component.startswith("yahoo") else False,
        }
    return out
