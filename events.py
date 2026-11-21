"""Seasonal events (Darkmoon Faire etc.): buy before, sell during. Config in watchlist.json.

For each event:
  * buy window (until buy_until): items priced below buy_below × reference price are worth stocking up;
  * sell window (sell_from..sell_until): items at or above sell_above × reference -> sell what you hold;
  * dated reminders.
Reference price = average price during the previous occurrence of the event.

market.py calls notify_events() every run (Telegram, each message once a day);
brief_data.py calls status() for the morning brief.  Run alone:  python events.py
"""

import json
import os
import time

from ah import HERE, gold, pct

WATCHLIST = os.path.join(HERE, "watchlist.json")


def load():
    with open(WATCHLIST, encoding="utf-8") as f:
        return json.load(f)["events"]


def active(today=None):
    today = today or time.strftime("%Y-%m-%d")
    return [e for e in load() if today <= e["end"]], today


def rows(event, prices, mine):
    """[(item_id, name, price, ref, ratio, held)] for the event's items with a current price."""
    out = []
    for iid, it in event["items"].items():
        p = prices.get(int(iid))
        if p:
            out.append((int(iid), it["name"], p, it["ref"], p / it["ref"], mine.get(int(iid), 0)))
    return out


def status(prices, mine, today=None):
    """Lines of text for the morning brief."""
    events, today = active(today)
    lines = []
    for e in events:
        r = rows(e, prices, mine)
        if today <= e["buy_until"]:
            phase = f"закупка до {e['buy_until']}"
            good = [x for x in r if x[4] <= e["buy_below"]]
        elif e["sell_from"] <= today <= e["sell_until"]:
            phase = f"продажа до {e['sell_until']}"
            good = [x for x in r if x[4] >= e["sell_above"]]
        else:
            phase, good = "ярмарка идёт, после неё цены падают", []
        lines.append(f"{e['name']} {e['start']}–{e['end']}: {phase}.")
        for iid, name, p, ref, ratio, held in sorted(r, key=lambda x: x[4]):
            mark = " ←" if any(g[0] == iid for g in good) else ""
            lines.append(f"  {name}: {gold(p)} ({ratio:.0%} от цены прошлой ярмарки {gold(ref)})"
                         f"{f', у тебя {held}' if held else ''}{mark}")
        if e["reminders"].get(today):
            lines.append("  Напоминание: " + e["reminders"][today])
    return lines


def notify_events(db, prices, mine, send, today=None):
    """Telegram: dated reminders, and sell-window alerts for items reaching the target. Once per day each."""
    events, today = active(today)
    db.execute("CREATE TABLE IF NOT EXISTS events_sent (key TEXT, day TEXT, PRIMARY KEY (key, day))")

    def once(key, text):
        if db.execute("SELECT 1 FROM events_sent WHERE key=? AND day=?", (key, today)).fetchone():
            return
        if send(text):
            db.execute("INSERT INTO events_sent VALUES (?,?)", (key, today))
            db.commit()

    for e in events:
        r = rows(e, prices, mine)
        if e["reminders"].get(today):
            body = "\n".join(l for l in status(prices, mine, today)[1:] if "Напоминание" not in l)
            once(f"{e['name']}:reminder", f"🎪 <b>{e['name']}</b>\n{e['reminders'][today]}\n\n{body}")
        if e["sell_from"] <= today <= e["sell_until"]:
            hits = [x for x in r if x[4] >= e["sell_above"]]
            if hits:
                text = "\n".join(f"🔴 <b>{name}</b>: {gold(p)} — {ratio:.0%} от цены прошлой ярмарки"
                                 f"{f', у тебя {held} шт.' if held else ''}"
                                 for _, name, p, _, ratio, held in hits)
                once(f"{e['name']}:sell", f"🎪 <b>Ярмарка: пора продавать</b>\n{text}")


if __name__ == "__main__":
    import character
    from ah import open_db

    db = open_db()
    prices = character.latest_prices(db, "eu", 110)
    try:
        mine = character.holdings(character.load_chars(), "Alliance")
    except Exception:
        mine = {}
    print("\n".join(status(prices, mine)) or "Нет предстоящих событий.")
