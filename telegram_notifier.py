"""
telegram_notifier.py

Raw calls to the Telegram Bot API (no framework needed, since this bot runs
in short bursts every 10 minutes rather than staying online continuously).

Security note: only callback button taps and messages from your configured
TELEGRAM_CHAT_ID are ever acted on -- see _is_authorized() below.
"""

import os
import requests

API_BASE = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT_UNITS = 3900


def text_units(text):
    return len(text.encode("utf-16-le")) // 2


def split_text(text, limit=MAX_TEXT_UNITS):
    """Split on line boundaries where possible, including long Unicode lines."""
    chunks, current = [], ""
    for line in str(text).splitlines(keepends=True):
        if current and text_units(current + line) > limit:
            chunks.append(current.rstrip())
            current = ""
        if text_units(line) <= limit:
            current += line
            continue
        for char in line:
            if text_units(current) + text_units(char) > limit:
                chunks.append(current)
                current = ""
            current += char
    if current:
        chunks.append(current.rstrip())
    return chunks or [""]


class TelegramError(RuntimeError):
    def __init__(self, message, retry_after=60):
        super().__init__(message)
        self.retry_after = retry_after


def _get_cred(config, env_var, config_key):
    return os.environ.get(env_var) or config["notifications"].get(config_key)


def _token(config):
    return _get_cred(config, "TELEGRAM_BOT_TOKEN", "telegram_bot_token")


def _chat_id(config):
    return _get_cred(config, "TELEGRAM_CHAT_ID", "telegram_chat_id")


def _call(config, method, payload):
    url = API_BASE.format(token=_token(config), method=method)
    try:
        resp = requests.post(url, json=payload, timeout=15)
        result = resp.json()
    except (requests.RequestException, ValueError):
        # requests exceptions include the URL, which contains the bot token.
        raise TelegramError(f"Telegram {method}: network or invalid-response error") from None
    if resp.status_code != 200 or not result.get("ok"):
        retry_after = result.get("parameters", {}).get("retry_after", 60)
        raise TelegramError(f"Telegram {method}: HTTP {resp.status_code}, API {result.get('error_code', 'error')}", retry_after)
    return result


def send_message(config, text, buttons=None):
    """
    buttons: optional list of ROWS, where each row is a list of
    (label, callback_data) tuples -- e.g. [[("Add X", "add:1"), ("No", "decline:1")]]
    for one row, or multiple such rows stacked for a batch of players.
    Returns the sent message_id.
    """
    chunks = split_text(text)
    for chunk in chunks[:-1]:
        _call(config, "sendMessage", {"chat_id": _chat_id(config), "text": chunk})
    payload = {"chat_id": _chat_id(config), "text": chunks[-1]}
    if buttons:
        payload["reply_markup"] = {
            "inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in buttons
            ]
        }
    result = _call(config, "sendMessage", payload)
    return result["result"]["message_id"]


def edit_message(config, message_id, text):
    _call(config, "editMessageText", {
        "chat_id": _chat_id(config),
        "message_id": message_id,
        "text": text,
    })


def _is_authorized(config, update):
    cq = update.get("callback_query")
    if cq:
        return str(cq.get("message", {}).get("chat", {}).get("id")) == str(_chat_id(config))
    msg = update.get("message")
    if msg:
        return str(msg.get("chat", {}).get("id")) == str(_chat_id(config))
    return False


def get_new_updates(config, offset_state):
    """
    Polls getUpdates ONCE for anything new since the last check -- both
    button taps AND plain text messages (like "/check RB") come from this
    same call, sharing the same offset. Call this once per run and use
    both parts of the result; a second poll in the same run would see
    nothing new, since the first call already advances the offset past
    whatever it returned.

    offset_state: dict with an 'offset' key, mutated in place and should be
    saved by the caller afterward so the same update isn't processed twice.

    Returns {"taps": [...], "commands": [...]}
      taps: list of {"callback_data", "callback_query_id", "message_id"}
      commands: list of {"text"} -- raw message text, e.g. "/check RB"
    """
    token = _token(config)
    if not token:
        return {"taps": [], "commands": []}

    params = {"timeout": 0}
    if offset_state.get("offset"):
        params["offset"] = offset_state["offset"]

    updates = _call(config, "getUpdates", params).get("result", [])

    taps, commands = [], []
    for update in updates:
        offset_state["offset"] = update["update_id"] + 1
        if not _is_authorized(config, update):
            continue
        cq = update.get("callback_query")
        if cq and cq.get("data") and cq.get("message"):
            taps.append({
                "callback_data": cq["data"],
                "callback_query_id": cq["id"],
                "message_id": cq["message"]["message_id"],
            })
            continue
        msg = update.get("message")
        if msg and msg.get("text"):
            commands.append({"text": msg["text"], "update_id": update["update_id"]})
    return {"taps": taps, "commands": commands}


def acknowledge_tap(config, callback_query_id, text=""):
    """Clears the loading spinner on the tapped button, optionally with a small popup."""
    _call(config, "answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})


def format_football_alert(player_name, team, result, label="BREAKOUT", status_tag=None):
    tag = f" [{status_tag}]" if status_tag else ""
    return (
        f"\U0001F3C8 {label}: {player_name} ({team}) scored {result['actual_points']} pts{tag} "
        f"vs a projection of {result['projected_points']} "
        f"({result['pct_of_projection']}% of projection)."
    )


def format_basketball_alert(player_name, team, result, label="BREAKOUT", status_tag=None):
    tag = f" [{status_tag}]" if status_tag else ""
    b = result["breakdown"]
    line = (
        f"{b['pts']['actual']}pt/{b['reb']['actual']}reb/{b['ast']['actual']}ast, "
        f"{b['stl']['actual']}stl/{b['blk']['actual']}blk"
    )
    message = (
        f"\U0001F3C0 {label}: {player_name} ({team}) went {line}{tag} "
        f"(combined z-score {result['total_zscore']} vs. projections)."
    )
    names = {"pts": "PTS", "reb": "REB", "ast": "AST", "stl": "STL", "blk": "BLK", "fg3m": "3PM", "to": "TO"}
    comparisons = [f"{names[k]} {b[k]['actual']:g}/{b[k]['projected']:g}" for k in names if k in b]
    message += "\nActual/projected: " + " · ".join(comparisons)
    if result.get("actual_stats") is not None:
        message += f"\nMinutes: {result['actual_stats'].get('min', 0):g}/{result.get('projected_stats', {}).get('min', 0):g} projected"
    for cat, title in (("fgp", "FG"), ("ftp", "FT")):
        if cat in b:
            actual = result.get("actual_stats", {})
            made, attempted = ("fgm", "fga") if cat == "fgp" else ("ftm", "fta")
            if result.get("actual_stats") is not None and not actual.get(attempted):
                message += f"\n{title}: no attempts (projected {b[cat]['projected']:.1%})"
                continue
            volume = f" ({actual.get(made, 0):g}/{actual.get(attempted, 0):g})" if attempted in actual else ""
            message += f"\n{title}: {b[cat]['actual']:.1%}{volume} vs {b[cat]['projected']:.1%} projected"
    drivers = sorted(((k, v) for k, v in b.items() if v.get("z", 0) > 0), key=lambda x: x[1]["z"], reverse=True)[:3]
    if drivers:
        message += "\nDrivers: " + ", ".join(f"{names.get(k, k.upper())} ({v['z']:+.1f})" for k, v in drivers)
    return message
