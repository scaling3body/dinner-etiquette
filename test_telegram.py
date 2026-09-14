"""
test_telegram.py

One-off script to confirm your Telegram bot setup actually works, without
needing a live NFL/NBA game. Sends a fake breakout alert through the same
config.json and telegram_notifier.py the real bot uses.

Run locally:
    python test_telegram.py

Or via GitHub Actions: Actions tab -> "Fantasy Breakout Alerts" -> "Run
workflow" -> check the "Send a test Telegram message" box.
"""

import json
import os
import sys

import telegram_notifier as notifier

CONFIG_PATH = "config.json"


def main():
    if not os.path.exists(CONFIG_PATH):
        print(f"Missing {CONFIG_PATH}. Copy config.example.json to config.json and fill it in first.")
        sys.exit(1)

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    test_message = (
        "\U0001F3C8 TEST ALERT (not a real breakout)\n\n"
        "Test Player (FA) -- 42.0 pts vs 18.5 projected (227%)\n\n"
        "If you're seeing this, your bot token and chat_id are working."
    )

    try:
        notifier.send_message(config, test_message)
        print("Test message sent -- check Telegram.")
    except Exception as e:
        print(f"Failed to send test message: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
