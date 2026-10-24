#!/usr/bin/env python3
"""Find your Telegram chat id and write it into .env.

Usage:
    venv/bin/python tools/telegram_setup.py <BOT_TOKEN>

Before running:
    1. Open Telegram and message @BotFather
    2. Send /newbot, pick a name, copy the token it gives you
    3. Open a chat with YOUR new bot and send it any message
       (a bot can never message you first - this step is required)
    4. Run this script with the token

It finds the chat id, saves both values to .env, and sends a test message.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx

ENV = Path(__file__).resolve().parent.parent / ".env"


def set_env(key: str, value: str) -> None:
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, re.M):
        text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    ENV.write_text(text, encoding="utf-8")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    token = sys.argv[1].strip()

    try:
        me = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=20).json()
    except Exception as exc:
        print(f"Could not reach Telegram: {exc}")
        return 1
    if not me.get("ok"):
        print(f"That token was rejected: {me.get('description')}")
        return 1
    print(f"Bot:  @{me['result']['username']}")

    updates = httpx.get(
        f"https://api.telegram.org/bot{token}/getUpdates", timeout=20
    ).json()
    results = updates.get("result") or []
    if not results:
        print(
            "\nNo messages found.\n"
            f"Open Telegram, find @{me['result']['username']}, and send it any\n"
            "message. Then run this again."
        )
        return 1

    chats = {}
    for item in results:
        msg = item.get("message") or item.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            name = chat.get("username") or chat.get("title") or chat.get("first_name") or "?"
            chats[str(chat["id"])] = name

    if len(chats) > 1:
        print("\nSeveral chats found:")
        for cid, name in chats.items():
            print(f"  {cid}  ({name})")
        print("\nUsing the first. Edit TELEGRAM_CHAT_ID in .env to change it.")

    chat_id, who = next(iter(chats.items()))
    print(f"Chat: {chat_id}  ({who})")

    set_env("TELEGRAM_BOT_TOKEN", token)
    set_env("TELEGRAM_CHAT_ID", chat_id)
    print(f"Saved both to {ENV}")

    sent = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "parse_mode": "HTML",
            "text": (
                "<b>Money Maker connected</b>\n\n"
                "Reply alerts will arrive here: who replied, what they said, "
                "and whether they sound interested."
            ),
        },
        timeout=20,
    ).json()
    if sent.get("ok"):
        print("Test message sent - check Telegram.")
        print("\nRestart the app to pick up the new settings.")
        return 0
    print(f"Saved, but the test message failed: {sent.get('description')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
