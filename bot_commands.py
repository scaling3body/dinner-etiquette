"""Read-only Telegram controls; no credentials are included in responses."""
from state_store import load, timestamp, utcnow
import delivery

HELP = ("Commands\n/status — last check, data freshness and delivery queue\n"
        "/recent [nba|nfl] [1–10] — delivered alerts\n/settings — scoring and screening rules\n"
        "/help — this guide\n/check RB [count] [refresh] — available NFL tier recommendations (Yahoo required)\n"
        "/tier N — set the NFL tier filter\nReplies are picked up on the next bot run; GitHub may delay it.")


def age(value):
    then = timestamp(value)
    return f"{max(0, int((utcnow() - then).total_seconds() / 60))} minutes ago" if then else "never"


def reply(text, config, health_state, history, runtime):
    words = text.strip().split()
    if not words:
        return None
    command = words[0].split("@")[0].lower()
    if command in ("/help", "/start"):
        return HELP
    if command == "/settings":
        nba, nfl = config["basketball"], config["football"]
        return (f"NBA: {'on' if nba['enabled'] else 'off'}; breakout score ≥ {nba['zscore_alert_threshold']:g}; "
                f"minimum {nba.get('min_minutes', 12)} minutes and {nba.get('min_counting_stats', 12)} counting stats.\n"
                f"NFL: {'on' if nfl['enabled'] else 'off'}; ≥ {nfl['pct_threshold'] * 100:g}% of projection and {nfl['point_floor']:g} points.\n"
                f"Batch threshold: {config.get('notifications', {}).get('batch_threshold', 3)}.\n"
                f"Catch-up: {config.get('catchup', {}).get('lookback_days', 2)} days.\n"
                f"Yahoo: {'enabled' if config.get('roster_management', {}).get('enabled') else 'disabled'}.\n"
                "Shooting percentages are volume-weighted. Opportunity estimates are labeled separately.")
    if command == "/status":
        queue = delivery.summary(load(delivery.PATH, {}))
        lines = [f"Last completed check: {age(runtime.get('last_completed'))}",
                 f"Check started: {age(runtime.get('last_started'))}",
                 f"Delivery queue: {queue['pending']} alerts; {queue['failed_messages']} failed messages"]
        for component in ("sleeper_nba", "sleeper_nfl", "scoreboard", "yahoo", "yahoo_nba", "yahoo_nfl", "telegram"):
            state = health_state.get(component)
            if state:
                lines.append(f"{component}: last success {age(state.get('last_success'))}; {state.get('consecutive_failures', 0)} consecutive failures")
        for sport, info in runtime.get("sports", {}).items():
            lines.append(f"{sport.upper()}: {info.get('status', 'unknown')} · {info.get('matched', 0)} matched players")
        if queue.get("last_error"):
            lines.append("A delivery failed and remains queued for retry.")
        return "\n".join(lines)
    if command == "/recent":
        sport = next((word.lower() for word in words[1:] if word.lower() in ("nba", "nfl")), None)
        count = next((min(10, max(1, int(word))) for word in words[1:] if word.isdigit()), 5)
        rows = [h for h in history if h.get("notified") and (not sport or h.get("sport") == sport)][-count:]
        return "Recent delivered alerts:\n\n" + "\n\n".join(h["message"] for h in reversed(rows)) if rows else "No delivered alerts match that filter."
    return None
