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


def _get_cred(config, env_var, config_key):
    return os.environ.get(env_var) or config["notifications"].get(config_key)


def _token(config):
    return _get_cred(config, "TELEGRAM_BOT_TOKEN", "telegram_bot_token")


def _chat_id(config):
    return _get_cred(config, "TELEGRAM_CHAT_ID", "telegram_chat_id")


def _call(config, method, payload):
    url = API_BASE.format(token=_token(config), method=method)
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    return resp.json()


def send_message(config, text, buttons=None):
    """
    buttons: optional list of ROWS, where each row is a list of
    (label, callback_data) tuples -- e.g. [[("Add X", "add:1"), ("No", "decline:1")]]
    for one row, or multiple such rows stacked for a batch of players.
    Returns the sent message_id.
    """
    payload = {"chat_id": _chat_id(config), "text": text}
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
        return str(cq["message"]["chat"]["id"]) == str(_chat_id(config))
    msg = update.get("message")
    if msg:
        return str(msg["chat"]["id"]) == str(_chat_id(config))
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

    url = API_BASE.format(token=token, method="getUpdates")
    params = {"timeout": 0}
    if offset_state.get("offset"):
        params["offset"] = offset_state["offset"]

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    updates = resp.json().get("result", [])

    taps, commands = [], []
    for update in updates:
        offset_state["offset"] = update["update_id"] + 1
        if not _is_authorized(config, update):
            continue
        cq = update.get("callback_query")
        if cq:
            taps.append({
                "callback_data": cq["data"],
                "callback_query_id": cq["id"],
                "message_id": cq["message"]["message_id"],
            })
            continue
        msg = update.get("message")
        if msg and msg.get("text"):
            commands.append({"text": msg["text"]})
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
    return (
        f"\U0001F3C0 {label}: {player_name} ({team}) went {line}{tag} "
        f"(combined z-score {result['total_zscore']} vs. projections)."
    )
