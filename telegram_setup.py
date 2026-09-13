"""
telegram_setup.py

RUN THIS LOCALLY ONCE to find your Telegram chat_id.

Before running:
1. In Telegram, message @BotFather -> /newbot -> follow the prompts.
   BotFather gives you a bot token like "123456789:AAExampleTokenHere".
2. Open a chat with YOUR new bot (search its username in Telegram) and send
   it any message, e.g. "/start". This is required -- Telegram won't let a
   bot message you until you've messaged it first.
3. Run: python telegram_setup.py YOUR_BOT_TOKEN
"""

import sys
import requests


def main():
    if len(sys.argv) != 2:
        print("Usage: python telegram_setup.py <bot_token>")
        sys.exit(1)

    token = sys.argv[1]
    resp = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    resp.raise_for_status()
    updates = resp.json().get("result", [])

    if not updates:
        print("No messages found yet. Make sure you messaged your bot first, then try again.")
        return

    chat_ids = set()
    for u in updates:
        msg = u.get("message")
        if msg:
            chat_ids.add(msg["chat"]["id"])

    if not chat_ids:
        print("Found updates but no message chat_id -- try sending your bot a plain text message.")
        return

    print("Found chat_id(s):", chat_ids)
    print("\nSet these as GitHub repo secrets:")
    print(f"  TELEGRAM_BOT_TOKEN = {token}")
    print(f"  TELEGRAM_CHAT_ID = {list(chat_ids)[0]}")


if __name__ == "__main__":
    main()
