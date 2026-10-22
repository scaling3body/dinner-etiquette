"""Small, atomic JSON stores shared by scheduled jobs."""
import json
import os
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError, AttributeError):
        return None


def load(path, default):
    try:
        with open(path, encoding="utf-8") as source:
            return json.load(source)
    except FileNotFoundError:
        return default


def save(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp = str(path) + ".tmp"
    with open(temp, "w", encoding="utf-8") as target:
        json.dump(data, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temp, path)
