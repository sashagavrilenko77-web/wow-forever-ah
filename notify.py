"""Telegram notifications.

Setup (once):
  1. In Telegram open @BotFather, send /newbot, pick a name; copy the token it gives you.
  2. Put it in .env as TELEGRAM_BOT_TOKEN=...
  3. Send any message to your new bot, then run:  python notify.py setup
     It finds your chat id, saves TELEGRAM_CHAT_ID to .env and sends a test message.

Usage elsewhere:  from notify import send;  send("text")   (HTML formatting, does nothing if not set up)
Send a file:      python notify.py send reports/brief.txt
"""

import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(HERE, ".env")


def env():
    out = {}
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8-sig") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    out[k.strip()] = v.strip().strip('"')
    return out


def api(token, method, **params):
    data = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=30) as r:
        return json.load(r)


def send(text):
    e = env()
    token, chat = e.get("TELEGRAM_BOT_TOKEN"), e.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    # Telegram allows 4096 characters per message; split between paragraphs so HTML tags stay closed.
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if cur and len(cur) + len(para) + 2 > 3900:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n\n{para}" if cur else para
    parts.append(cur)
    for part in parts:
        api(token, "sendMessage", chat_id=chat, text=part, parse_mode="HTML", disable_web_page_preview="true")
    return True


def setup():
    e = env()
    token = e.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Добавь TELEGRAM_BOT_TOKEN=... в .env (токен от @BotFather).")
    updates = api(token, "getUpdates").get("result", [])
    chats = [u["message"]["chat"] for u in updates if "message" in u]
    if not chats:
        sys.exit("Бот пока не получил сообщений. Напиши ему что-нибудь в Telegram и запусти снова.")
    chat = chats[-1]
    lines = [l for l in open(ENV, encoding="utf-8-sig").read().splitlines() if not l.startswith("TELEGRAM_CHAT_ID=")]
    lines.append(f"TELEGRAM_CHAT_ID={chat['id']}")
    with open(ENV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    send("✅ Бот подключён. Сюда будут приходить сигналы по аукциону WoW Forever.")
    print(f"Готово: чат {chat.get('first_name') or chat.get('title')} сохранён, тестовое сообщение отправлено.")


if __name__ == "__main__":
    if sys.argv[1:] == ["setup"]:
        setup()
    elif len(sys.argv) == 3 and sys.argv[1] == "send":
        with open(sys.argv[2], encoding="utf-8") as f:
            print("отправлено" if send(f.read()) else "бот не настроен: python notify.py setup")
    elif sys.argv[1:] == ["test"]:
        print("отправлено" if send("Тестовое сообщение 👋") else "бот не настроен: python notify.py setup")
    else:
        print(__doc__)
