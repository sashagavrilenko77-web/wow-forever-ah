"""Your real trades: what you bought, at what price, and when to sell.

Each position has targets and a stop. market.py checks open positions every hour and sends a Telegram
message (once a day per kind) when:
  * the typical price on your house reaches target 1 / target 2 -> sell there;
  * it falls below the stop -> exit;
  * Blackwater is a better outlet: Horde is so expensive that listing on Blackwater just under the
    Horde minimum nets more after the 15% cut than selling at home.
Prices are per unit, in gold (1.49 = 1g 49s).

Usage:
    python positions.py add 7078 25 1.49 --t1 1.95 --t2 2.40 --stop 1.25 --note "..."
    python positions.py list
    python positions.py sell <id> <qty> <price>       # record a sale (partial or full)
"""

import argparse
import time

from ah import gold, open_db, pct, table

AH_CUT, BW_CUT = 0.05, 0.15


def ensure(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY, item_id INTEGER, name TEXT, qty INTEGER, price REAL, opened INTEGER,
            t1 REAL, t2 REAL, stop REAL, note TEXT, closed INTEGER);
        CREATE TABLE IF NOT EXISTS position_sales (
            position_id INTEGER, qty INTEGER, price REAL, ts INTEGER);
    """)


def sold_qty(db, pid):
    return db.execute("SELECT COALESCE(SUM(qty),0) FROM position_sales WHERE position_id=?", (pid,)).fetchone()[0]


def open_positions(db):
    ensure(db)
    out = []
    for p in db.execute("SELECT id, item_id, name, qty, price, opened, t1, t2, stop, note FROM positions "
                        "WHERE closed IS NULL").fetchall():
        left = p[3] - sold_qty(db, p[0])
        if left > 0:
            out.append(dict(zip(("id", "item", "name", "qty", "price", "opened", "t1", "t2", "stop", "note"), p),
                            left=left))
    return out


def latest(db, house, item):
    last = db.execute("SELECT MAX(ts) FROM hourly WHERE region='eu' AND house=?", (house,)).fetchone()[0]
    r = db.execute("SELECT min_price, p25, qty FROM hourly WHERE region='eu' AND house=? AND ts=? AND item_id=?",
                   (house, last, item)).fetchone()
    return r or (None, None, 0)


def evaluate(db, p, home=110, opp=113, bw=116):
    """Current state of a position -> dict with prices, P/L and the list of triggered signals."""
    amin, a, aq = latest(db, home, p["item"])
    hmin, h, hq = latest(db, opp, p["item"])
    bmin, b, bq = latest(db, bw, p["item"])
    cost = p["price"]
    s = dict(amin=amin, a=a, aq=aq, hmin=hmin, h=h, hq=hq, bmin=bmin, b=b, bq=bq, signals=[])
    if a:
        s["pl"] = a * (1 - AH_CUT) / cost - 1
        if p["t2"] and a >= p["t2"]:
            s["signals"].append(("t2", f"типичная цена {gold(a)} ≥ цели 2 {gold(p['t2'])} — продавать остаток"))
        elif p["t1"] and a >= p["t1"]:
            s["signals"].append(("t1", f"типичная цена {gold(a)} ≥ цели 1 {gold(p['t1'])} — продавать большую часть"))
        if p["stop"] and a <= p["stop"]:
            s["signals"].append(("stop", f"типичная цена {gold(a)} ≤ стопа {gold(p['stop'])} — выходить"))
    # Blackwater outlet: list just under the cheapest Horde lot (and under Blackwater's own cheapest).
    if hmin:
        ask = min(hmin, bmin) - 100 if bmin else hmin - 100
        net = ask * (1 - BW_CUT)
        home_net = (a or 0) * (1 - AH_CUT)
        if net >= cost * 1.20 and net >= home_net * 1.15:
            s["bw_ask"], s["bw_net"] = ask, net
            s["signals"].append(("bw", f"выставить на Blackwater по ~{gold(ask)}: после 15% ≈{gold(net)}/шт "
                                       f"({pct(net / cost - 1)} к покупке) против {gold(home_net)} дома"))
    return s


def notify_positions(db, send, today=None):
    """Telegram messages for triggered position signals, each kind at most once a day per position."""
    today = today or time.strftime("%Y-%m-%d")
    db.execute("CREATE TABLE IF NOT EXISTS positions_sent (pid INTEGER, kind TEXT, day TEXT, "
               "PRIMARY KEY (pid, kind, day))")
    for p in open_positions(db):
        s = evaluate(db, p)
        fresh = [(k, t) for k, t in s["signals"] if not db.execute(
            "SELECT 1 FROM positions_sent WHERE pid=? AND kind=? AND day=?", (p["id"], k, today)).fetchone()]
        if not fresh:
            continue
        text = (f"💼 <b>{p['name']}</b> — твоя позиция {p['left']} шт. по {gold(p['price'])}\n"
                f"Альянс {gold(s['a'])} · Орда {gold(s['h'])} (мин {gold(s['hmin'])}) · "
                f"Blackwater {gold(s['b']) if s['b'] else 'нет лотов'}\n"
                + "\n".join(f"• {t}" for _, t in fresh))
        if send(text):
            db.executemany("INSERT OR IGNORE INTO positions_sent VALUES (?,?,?)",
                           [(p["id"], k, today) for k, _ in fresh])
            db.commit()


def lines(db):
    """Text for the morning brief."""
    out = []
    for p in open_positions(db):
        s = evaluate(db, p)
        days = (time.time() - p["opened"]) / 86400
        out.append(f"{p['name']} ×{p['left']} по {gold(p['price'])} ({days:.0f} дн.): сейчас Альянс {gold(s['a'])} "
                   f"(мин {gold(s['amin'])}), Орда {gold(s['h'])} (мин {gold(s['hmin'])}), "
                   f"Blackwater {gold(s['b']) if s['b'] else 'нет лотов'}; при продаже дома {pct(s.get('pl'))}; "
                   f"цели {gold(p['t1'])} / {gold(p['t2'])}, стоп {gold(p['stop'])}"
                   + (f". Сигналы: {'; '.join(t for _, t in s['signals'])}" if s["signals"] else "")
                   + (f". Заметка: {p['note']}" if p["note"] else ""))
    return out


def main():
    ap = argparse.ArgumentParser(description="Real trading positions")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("item", type=int)
    a.add_argument("qty", type=int)
    a.add_argument("price", type=float, help="gold per unit, e.g. 1.49")
    a.add_argument("--t1", type=float)
    a.add_argument("--t2", type=float)
    a.add_argument("--stop", type=float)
    a.add_argument("--note", default="")
    s = sub.add_parser("sell")
    s.add_argument("id", type=int)
    s.add_argument("qty", type=int)
    s.add_argument("price", type=float)
    sub.add_parser("list")
    args = ap.parse_args()

    db = open_db()
    ensure(db)
    g = lambda x: round(x * 10000) if x else None
    if args.cmd == "add":
        from blizzard import russian_names
        name = russian_names(db, [args.item]).get(args.item) or f"item {args.item}"
        db.execute("INSERT INTO positions (item_id, name, qty, price, opened, t1, t2, stop, note) VALUES (?,?,?,?,?,?,?,?,?)",
                   (args.item, name, args.qty, g(args.price), int(time.time()), g(args.t1), g(args.t2), g(args.stop),
                    args.note))
        db.commit()
        print(f"Добавлено: {name} ×{args.qty} по {gold(g(args.price))}")
    elif args.cmd == "sell":
        db.execute("INSERT INTO position_sales VALUES (?,?,?,?)", (args.id, args.qty, g(args.price), int(time.time())))
        p = db.execute("SELECT qty, price, name FROM positions WHERE id=?", (args.id,)).fetchone()
        if sold_qty(db, args.id) >= p[0]:
            db.execute("UPDATE positions SET closed=? WHERE id=?", (int(time.time()), args.id))
        db.commit()
        profit = (g(args.price) * (1 - AH_CUT) - p[1]) * args.qty
        print(f"Продажа записана: {p[2]} ×{args.qty} по {gold(g(args.price))}, прибыль ≈{gold(profit)} (после 5%)")
    rows = []
    for p in open_positions(db):
        st = evaluate(db, p)
        rows.append((p["id"], p["name"], p["left"], gold(p["price"]), gold(st["a"]), pct(st.get("pl")),
                     gold(p["t1"]), gold(p["t2"]), gold(p["stop"]), "; ".join(k for k, _ in st["signals"]) or "-"))
    table("ОТКРЫТЫЕ ПОЗИЦИИ", ["id", "Предмет", "Осталось", "Куплено по", "Сейчас", "Если продать дома",
                               "Цель 1", "Цель 2", "Стоп", "Сигналы"], rows)


if __name__ == "__main__":
    main()
