"""Persistent alert outbox. Baselines/history advance only on confirmed delivery.

Receipts are checkpointed after each Telegram message. An ambiguous network
timeout can still result in a duplicate: Telegram has no idempotency key.
"""
import hashlib
import secrets
from datetime import timedelta
import health
import telegram_notifier as notifier
from state_store import load, save, timestamp, utcnow

PATH = "delivery_state.json"


def event_id(item):
    identity = item.get("event_id") or "|".join(str(item.get(k, "")) for k in
                    ("sport", "alert_key", "sleeper_player_id", "kind", "count"))
    return hashlib.sha256(identity.encode()).hexdigest()[:24]


def apply_receipts(state, alerted, history):
    existing = {h.get("event_id") for h in history}
    for identity, item in state.get("items", {}).items():
        if not item.get("delivered_at"):
            continue
        if item.get("kind", "breakout") == "breakout" and item.get("alert_key"):
            period = alerted.setdefault(item["alert_key"], {})
            prior = period.get(item["sleeper_player_id"], {})
            if item.get("count", 0) >= prior.get("count", 0):
                period[item["sleeper_player_id"]] = {"count": item["count"], "baseline": item["baseline"], "score_version": 2}
        if identity not in existing and not item.get("exclude_history"):
            history.append(dict(item.get("history", {}), event_id=identity,
                                timestamp=item["delivered_at"], detected_at=item["created_at"],
                                notified=True, message_ids=item.get("message_ids", [])))
            existing.add(identity)


def summary(state):
    waiting = [i for i in state.get("items", {}).values() if not i.get("delivered_at")]
    failures = [m for m in state.get("messages", {}).values() if m.get("last_error") and not m.get("sent_at")]
    return {"pending": len(waiting), "failed_messages": len(failures),
            "oldest_pending_at": min((i["created_at"] for i in waiting), default=None),
            "last_error": failures[-1]["last_error"] if failures else None}


def dispatch(config, pending, batch, health_state, alerted, history, path=None, send=True):
    path = path or PATH
    state = load(path, {"items": {}, "messages": {}})
    items, messages = state.setdefault("items", {}), state.setdefault("messages", {})
    apply_receipts(state, alerted, history)
    new = []
    for incoming in batch:
        if not incoming.get("send"):
            continue
        identity = event_id(incoming)
        if identity in items:
            continue
        item = dict(incoming, created_at=utcnow().isoformat(), message_refs=[])
        item.setdefault("history", {"sport": item["sport"], "player_name": item["player_name"], "message": item["text"]})
        items[identity] = item
        new.append(identity)

    # Pack whole alerts where possible; split an oversized individual alert
    # into independently receipted messages, so a partial retry stays partial.
    groups, current, current_text = [], [], ""
    threshold = config.get("notifications", {}).get("batch_threshold", 3)
    for identity in new:
        item = items[identity]
        parts = notifier.split_text(item["text"], 3500)
        if len(parts) > 1:
            if current:
                groups.append((current, current_text))
                current, current_text = [], ""
            groups.extend(([identity], part) for part in parts)
            continue
        candidate = (current_text + "\n\n" if current_text else "") + parts[0]
        if current and (len(new) < threshold or notifier.text_units(candidate) > 3500):
            groups.append((current, current_text))
            current, current_text = [], ""
            candidate = parts[0]
        current.append(identity)
        current_text = candidate
    if current:
        groups.append((current, current_text))
    for identities, text in groups:
        message_key = secrets.token_hex(8)
        messages[message_key] = {"items": identities, "text": text, "attempts": 0}
        for identity in identities:
            items[identity]["message_refs"].append(message_key)
    save(path, state)  # outbox exists BEFORE any send
    if not send:
        return summary(state)

    failed = False
    sent_this_run = 0
    for message in messages.values():
        if message.get("sent_at"):
            continue
        retry_at = timestamp(message.get("retry_at"))
        if retry_at and retry_at > utcnow():
            failed = True
            continue
        buttons, codes = [], []
        for identity in message["items"]:
            item = items[identity]
            # Offer a roster action only on the final part of an alert.
            refs = item["message_refs"]
            age = utcnow() - timestamp(item["created_at"])
            action_fresh = age < timedelta(minutes=config.get("roster_management", {}).get("suggestion_expiry_minutes", 1440))
            if item.get("addable") and config.get("roster_management", {}).get("enabled") and action_fresh and messages[refs[-1]] is message:
                code = secrets.token_hex(4)
                buttons.append([(f"Add {item['player_name']}", f"add:{code}"), ("No thanks", f"decline:{code}")])
                codes.append((code, item))
        try:
            message["attempts"] += 1
            oldest = min(timestamp(items[identity]["created_at"]) for identity in message["items"])
            late_minutes = int((utcnow() - oldest).total_seconds() / 60)
            text = message["text"]
            if late_minutes >= config.get("health_check", {}).get("stale_minutes", 15):
                text = f"⏱ Delayed delivery · detected {late_minutes} minutes ago\n" + text
            message_id = notifier.send_message(config, text, buttons=buttons or None)
        except Exception as error:
            message["last_error"] = str(error)[:200]
            message["retry_at"] = (utcnow() + timedelta(seconds=getattr(error, "retry_after", 60))).isoformat()
            failed = True
            save(path, state)
            break
        message["message_id"] = message_id
        sent_this_run += 1
        message["sent_at"] = utcnow().isoformat()
        message.pop("last_error", None)
        for code, item in codes:
            pending[code] = {"sport": item["sport"], "player_id": item.get("player_id"),
                             "player_name": item["player_name"], "message_id": message_id,
                             "batched": len(message["items"]) > 1, "created_at": message["sent_at"]}
        for identity in message["items"]:
            item = items[identity]
            if all(messages[ref].get("sent_at") for ref in item["message_refs"]):
                item["delivered_at"] = message["sent_at"]
                item["message_ids"] = [messages[ref]["message_id"] for ref in item["message_refs"]]
        save(path, state)
        apply_receipts(state, alerted, history)
    if failed:
        health.record_failure(health_state, "telegram")
    elif sent_this_run:
        health.record_success(health_state, "telegram")
    cutoff = utcnow() - timedelta(days=35)
    old = [identity for identity, item in items.items()
           if timestamp(item.get("delivered_at")) and timestamp(item["delivered_at"]) < cutoff]
    for identity in old:
        del items[identity]
    referenced = {ref for item in items.values() for ref in item["message_refs"]}
    state["messages"] = {ref: message for ref, message in messages.items() if ref in referenced}
    save(path, state)
    return summary(state)
