"""Your characters: gold, bags, bank, mail and own auctions from the AukcionExport addon.

The addon writes WTF/Account/<account>/SavedVariables/AukcionExport.lua when you log out or /reload.
Bank and mail are remembered from the last time you opened them; own auctions from the last AH visit.

Usage:
    python character.py           # gold + what you own, valued at current prices on your house
"""

import glob
import os
import re
import sys
import time

from ah import HERE, gold, open_db, table

DEFAULT_WOW = r"C:\Program Files (x86)\World of Warcraft\_forever_"
HOUSE_BY_FACTION = {"Alliance": 110, "Horde": 113}  # EU PvP, see market.py


# ---------- SavedVariables (Lua) parser ----------

TOKEN = re.compile(r'\s*(?:(--[^\n]*)|("(?:\\.|[^"\\])*")|(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)|(\w+)|(.))', re.S)


def parse_lua(text):
    tokens = [t for t in TOKEN.findall(text) if not t[0] and not t[4].isspace()]
    pos = 0

    def peek():
        return tokens[pos] if pos < len(tokens) else ("", "", "", "", "")

    def take():
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def value():
        _, s, n, w, p = take()
        if s:
            return bytes(s[1:-1], "utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
        if n:
            return float(n) if "." in n or "e" in n.lower() else int(n)
        if w in ("true", "false"):
            return w == "true"
        if w == "nil":
            return None
        if p == "{":
            return table_()
        raise ValueError(f"unexpected token {s or n or w or p}")

    def table_():
        out, idx = {}, 1
        while peek()[4] != "}":
            if peek()[4] == "[":
                take()
                key = value()
                take()  # ]
                take()  # =
                out[key] = value()
            else:
                out[idx] = value()
                idx += 1
            if peek()[4] in (",", ";"):
                take()
        take()  # }
        return out

    result = {}
    while pos < len(tokens):
        _, _, _, name, _ = take()
        take()  # =
        result[name] = value()
    return result


# ---------- data access ----------

def wow_dir():
    env = os.path.join(HERE, ".env")
    if os.path.exists(env):
        with open(env, encoding="utf-8-sig") as f:
            for line in f:
                if line.startswith("WOW_DIR="):
                    return line.split("=", 1)[1].strip().strip('"')
    return DEFAULT_WOW


def load_chars():
    files = glob.glob(os.path.join(wow_dir(), "WTF", "Account", "*", "SavedVariables", "AukcionExport.lua"))
    chars = {}
    for path in files:
        with open(path, encoding="utf-8") as f:
            db = parse_lua(f.read()).get("AukcionExportDB") or {}
        chars.update(db.get("chars") or {})
    return chars


def holdings(chars, faction=None):
    """{item_id: qty} across bags, bank, mail and own (unsold) auctions."""
    out = {}
    for c in chars.values():
        if faction and c.get("faction") != faction:
            continue
        for part in ("bags", "bank", "mail"):
            for i, n in (c.get(part) or {}).items():
                out[int(i)] = out.get(int(i), 0) + n
        for a in (c.get("auctions") or {}).values():
            if not a.get("sold"):
                out[int(a["item"])] = out.get(int(a["item"]), 0) + (a.get("qty") or 1)
    return out


def latest_prices(db, region, house):
    row = db.execute("SELECT MAX(ts) FROM hourly WHERE region=? AND house=?", (region, house)).fetchone()
    if not row or not row[0]:
        return {}
    return dict(db.execute("SELECT item_id, p25 FROM hourly WHERE region=? AND house=? AND ts=?",
                           (region, house, row[0])))


def store_gold(db, chars):
    db.execute("CREATE TABLE IF NOT EXISTS char_gold (char TEXT, ts INTEGER, gold INTEGER, PRIMARY KEY (char, ts))")
    db.executemany("INSERT OR IGNORE INTO char_gold VALUES (?,?,?)",
                   [(k, c["updated"], c["gold"]) for k, c in chars.items() if c.get("updated") and "gold" in c])
    db.commit()


def ago(ts):
    if not ts:
        return "никогда"
    h = (time.time() - ts) / 3600
    return f"{h:.0f} ч назад" if h < 48 else f"{h / 24:.0f} дн назад"


def main():
    chars = load_chars()
    if not chars:
        sys.exit("Нет данных аддона AukcionExport. Зайди в игру, включи аддон и выйди из игры или набери /reload.")
    db = open_db()
    store_gold(db, chars)

    rows = []
    for k, c in sorted(chars.items()):
        rows.append((k, c.get("faction", "?"), gold(c.get("gold")), gold(c.get("mailMoney") or 0),
                     ago(c.get("updated")), ago(c.get("bankUpdated")), ago(c.get("mailUpdated")),
                     ago(c.get("auctionsUpdated"))))
    total = sum((c.get("gold") or 0) + (c.get("mailMoney") or 0) for c in chars.values())
    table("ПЕРСОНАЖИ", ["Персонаж", "Фракция", "Золото", "На почте", "Обновлено", "Банк", "Почта", "Аукцион"], rows)
    print(f"Всего золота: {gold(total)}")

    from blizzard import russian_names
    for faction, house in HOUSE_BY_FACTION.items():
        own = holdings(chars, faction)
        if not own:
            continue
        prices = latest_prices(db, "eu", house)
        names = russian_names(db, list(own))
        valued = sorted(((n * (prices.get(i) or 0), i, n) for i, n in own.items()), reverse=True)
        worth = sum(v for v, _, _ in valued)
        table(f"ИМУЩЕСТВО ({faction}): сумки + банк + почта + свои лоты, по текущим ценам аукциона {house}",
              ["Предмет", "Кол-во", "Цена", "Стоимость"],
              [(names.get(i) or f"item {i}", n, gold(prices.get(i)), gold(v)) for v, i, n in valued[:30] if v])
        print(f"Оценка имущества ({faction}): {gold(worth)} (без учёта комиссии 5%)")


if __name__ == "__main__":
    main()
