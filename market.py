"""WoW Forever: medium-term trading signals from every auction house in EU and US.

Every run:
  1. downloads all listings of every Forever auction house in EU and US (Blizzard API),
  2. stores a compact per-item summary (min, 25th percentile price, quantity) in data/ah.sqlite,
     hourly for HOURLY_DAYS days, then rolled up into daily rows kept forever,
  3. prints signals for your house.

Signals:
  * Corridor. Alliance, Horde and Blackwater of one ruleset are linked: goods move between factions
    through Blackwater, which costs its cut (BW_COST). So Alliance and Horde prices can't drift apart
    by more than that for long. Alliance much cheaper than Horde -> goods will flow to Horde and our
    price should rise (buy). Alliance much dearer -> goods will flow to us (sell, don't buy).
  * Other economies. Other rulesets / regions are separate markets. We compare our price with their
    median relative to the *usual* ratio, so a house that is always cheaper doesn't look like a deal.
  * Own history. Our price now vs our 7-day median.

Run ah.py first: it provides TSM liquidity stats (sales per day) and item names.

Usage:
    python market.py                      # collect + report for EU PvP Alliance
    python market.py --no-collect         # report only, from what is stored
    python market.py --bw-cost 0.2        # Blackwater cost of moving goods between factions
"""

import argparse
import csv
import os
import sys
import time

import character
import cloud_sync
import events
import notify
import paper
import world
from ah import DATA, gold, num, open_db, pct, table, write_csv
from blizzard import HOUSES, LEVEL_REGIONS, fetch_all, russian_names

HOURLY_DAYS = 21
AH_CUT = 0.05


def house_id(ruleset, faction):
    return next(h for h, v in HOUSES.items() if v == (ruleset, faction))


# ---------- collect ----------


def ensure_tables(db):
    db.executescript("""
        DROP TABLE IF EXISTS live_snapshots;
        CREATE TABLE IF NOT EXISTS hourly (
            region TEXT, house INTEGER, ts INTEGER, item_id INTEGER,
            min_price INTEGER, p25 INTEGER, qty INTEGER, auctions INTEGER,
            PRIMARY KEY (region, house, item_id, ts)) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS hourly_ts ON hourly (region, house, ts);
        CREATE TABLE IF NOT EXISTS daily (
            region TEXT, house INTEGER, day TEXT, item_id INTEGER,
            min_price INTEGER, p25 INTEGER, qty INTEGER,
            PRIMARY KEY (region, house, item_id, day)) WITHOUT ROWID;
    """)


def store_snapshot(db, region, house, ts, rows):
    """rows: [(item_id, min, p25, qty, auctions)]. Returns False if this snapshot is already stored."""
    if db.execute("SELECT 1 FROM hourly WHERE region=? AND house=? AND ts=? LIMIT 1",
                  (region, house, ts)).fetchone():
        return False
    db.executemany("INSERT OR IGNORE INTO hourly VALUES (?,?,?,?,?,?,?,?)",
                   [(region, house, ts, *r) for r in rows])
    db.commit()
    return True


def collect(db):
    for region, house, ts, items, error in fetch_all():
        if error:
            print(f"  ! download failed: {error}", file=sys.stderr)
            continue
        new = store_snapshot(db, region, house, ts, [(i, round(s[0]), round(s[1]), s[2], s[3])
                                                     for i, s in items.items()])
        print(f"  {region} {house} {'/'.join(HOUSES[house])}: {len(items)} items, "
              f"{time.strftime('%d.%m %H:%M', time.localtime(ts))} ({'new' if new else 'already stored'})")
    try:
        cloud_sync.sync(db, store_snapshot)
    except Exception as e:
        print(f"  ! ночные данные из GitHub не загружены: {e}", file=sys.stderr)
    rollup(db)


def rollup(db):
    """Daily rows for finished days; drop hourly rows older than HOURLY_DAYS."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    db.execute("""
        INSERT OR IGNORE INTO daily
        SELECT region, house, date(ts, 'unixepoch') AS day, item_id,
               MIN(min_price), CAST(AVG(p25) AS INTEGER), CAST(AVG(qty) AS INTEGER)
        FROM hourly WHERE date(ts, 'unixepoch') < ?
        GROUP BY region, house, day, item_id""", (today,))
    db.execute("DELETE FROM hourly WHERE ts < ?", (int(time.time()) - HOURLY_DAYS * 86400,))
    db.commit()


# ---------- analysis ----------

def latest(db, region, house):
    row = db.execute("SELECT MAX(ts) FROM hourly WHERE region=? AND house=?", (region, house)).fetchone()
    if not row or not row[0]:
        return {}
    return {i: (m, p, q) for i, m, p, q in db.execute(
        "SELECT item_id, min_price, p25, qty FROM hourly WHERE region=? AND house=? AND ts=?",
        (region, house, row[0]))}


def daily_history(db, region, house, days):
    """{item_id: [p25 per day]} for the last `days` finished days."""
    out = {}
    for i, p in db.execute(
            "SELECT item_id, p25 FROM daily WHERE region=? AND house=? AND day >= date('now', ?) ORDER BY day",
            (region, house, f"-{days} day")):
        out.setdefault(i, []).append(p)
    return out


def median(v):
    v = sorted(x for x in v if x)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def load_tsm_stats(region):
    path = os.path.join(DATA, "raw", f"{region}_region_items.csv")
    if not os.path.exists(path):
        sys.exit("TSM files missing: run `python ah.py` first.")
    stats = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            stats[int(r["itemId"])] = (r["name"], num(r["soldPerDay"]) or 0, num(r["saleRate"]) or 0)
    return stats


def report(db, args):
    home = house_id(args.ruleset, args.faction)
    other_faction = "horde" if args.faction == "alliance" else "alliance"
    opp, bw = house_id(args.ruleset, other_faction), house_id(args.ruleset, "blackwater")
    fac = {"alliance": "Альянс", "horde": "Орда"}

    A, H, B = latest(db, args.region, home), latest(db, args.region, opp), latest(db, args.region, bw)
    if not A:
        sys.exit("No snapshots yet: run without --no-collect first.")
    # Other economies: the faction houses of every other ruleset in both regions.
    others = [latest(db, r, h) for r in LEVEL_REGIONS for h, (rs, f) in HOUSES.items()
              if f != "blackwater" and not (r == args.region and rs == args.ruleset)]
    others = [o for o in others if o]

    stats = load_tsm_stats(args.region)
    names = {i: s[0] for i, s in stats.items()}
    if not args.en:
        names.update(russian_names(db, list(A), args.region))

    # History (daily rows appear after the first full day of collecting).
    hist_home = daily_history(db, args.region, home, args.days)
    hist_ratio = {}
    if hist_home:
        hist_opp = daily_history(db, args.region, opp, args.days)
        for i, ph in hist_home.items():
            po = hist_opp.get(i)
            if po and len(po) == len(ph):
                hist_ratio[i] = median([a / h for a, h in zip(ph, po) if a and h])

    # What your characters of this faction own (bags, bank, mail, own auctions), from the addon.
    try:
        mine = character.holdings(character.load_chars(), args.faction.capitalize())
    except Exception as e:
        print(f"  ! не удалось прочитать данные персонажей: {e}", file=sys.stderr)
        mine = {}

    wd = world.compute(db)
    local = wd["houses"].get((args.region, home), {})

    c = args.bw_cost
    buys, sells, alerts = [], [], []
    for i, (amin, a, aqty) in A.items():
        name, sold, rate = stats.get(i, (None, 0, 0))
        if sold < args.min_sold or rate < args.min_rate or not a or a < args.min_price:
            continue
        h = H.get(i, (None, None, None))[1]
        b = B.get(i, (None, None, None))[1]
        fair = median([o[i][1] for o in others if i in o])
        own = median(hist_home.get(i, []))

        vs_fair = a / fair - 1 if fair else None
        vs_own = a / own - 1 if own else None
        ratio = a / h if h else None
        usual = hist_ratio.get(i)
        # Our price move vs the median move on all faction houses of the world (same window).
        win, here = world.pick(local, i)
        there = wd["items"][win].get(i, (None, 0))[0] if win else None
        tag = world.classify(here, there)

        row = [names.get(i) or f"item {i}", gold(a), gold(h), gold(b),
               f"{ratio:.2f}" if ratio else "-", f"{usual:.2f}" if usual else "-",
               pct(vs_fair), pct(vs_own), f"{sold:.0f}", mine.get(i, ""),
               f"{pct(here)} / {pct(there)} за {win}" if win else "-", tag]

        if not h or aqty < args.min_qty or H[i][2] < args.min_qty:
            continue
        if ratio < args.min_ratio or ratio > 1 / args.min_ratio:
            continue  # gap too big to be a market swing: faction-only items, quest items, empty market
        if usual and abs(ratio / usual - 1) < args.min_dev:
            continue  # this is the usual gap for this item, nothing unusual right now

        # Our sellers can reach Horde buyers only by listing on Blackwater, competing with what is there.
        outlet = min(h, b) if b else h
        floor = outlet * (1 - c)
        # Horde sellers reach our buyers the same way; our buyers also buy straight from Blackwater.
        ceiling = min(h / (1 - c), b) if b else h / (1 - c)

        if a < floor:
            upside = floor * (1 - AH_CUT) / a - 1
            if tag == "дешевеет во всей игре":
                continue  # not a local discount but a game-wide decline: don't fight the trend
            if upside >= args.min_upside:
                score = (floor * (1 - AH_CUT) - a) * min(sold, 100)  # gold per day of regional sales
                buys.append((score, row + [gold(floor), pct(upside), i], (i, row[0], a, floor)))
                if usual and ratio / usual - 1 <= -args.alert_dev and sold >= args.alert_sold \
                        and upside >= args.alert_upside:
                    alerts.append(("buy", i, (
                        f"🟢 <b>{row[0]}</b>: {gold(a)} (Орда {gold(h)}, Blackwater {gold(b)})\n"
                        f"   цель {gold(floor)} ({pct(upside)} после комиссии), продаётся {sold:.0f}/день\n"
                        f"   А/О сейчас {ratio:.2f}, обычно {usual:.2f}")))
        elif a > ceiling * (1 + args.min_dev):
            sells.append(((a / ceiling - 1) * min(sold, 100), row + [gold(ceiling)]))
            if mine.get(i):
                alerts.append(("sell", i, (
                    f"🔴 <b>{row[0]}</b>: у тебя {mine[i]} шт. Сейчас {gold(a)}, коридор держит до {gold(ceiling)}\n"
                    f"   выше коридора — сюда повезут товар, цена скорее упадёт. Продаётся {sold:.0f}/день")))

    headers = ["Предмет", fac[args.faction], fac[other_faction], "Blackwater", "A/О сейчас", "A/О обычно",
               "vs др. серверы", "vs своя 7д", "Прод./день", "У тебя", "Изм. у нас / в мире", "Откуда движение"]
    buys.sort(key=lambda x: -x[0])
    sells.sort(key=lambda x: -x[0])
    buy_rows = [r for _, r, _ in buys]
    sell_rows = [r for _, r in sells]

    print(f"\nКоридор Blackwater: {pct(-c)}. Цены = 25-й перцентиль (типичная дешёвая цена) последнего снимка.")
    if not hist_home:
        print("История по дням появится после первых полных суток сбора: колонки 'обычно' и '7д' пока пустые.")
    table("ПОКУПАТЬ: у нас дешевле, чем позволяет коридор (цена должна подтянуться)",
          headers + ["Цель", "Потенциал", "id"], buy_rows[:args.top])
    table("ПРОДАВАТЬ / НЕ ПОКУПАТЬ: у нас дороже, чем позволяет коридор (сюда повезут товар)",
          headers + ["Потолок"], sell_rows[:args.top])

    write_csv("signals_buy.csv", headers + ["Цель", "Потенциал", "id"], buy_rows)
    write_csv("signals_sell.csv", headers + ["Потолок"], sell_rows)
    print("\nОтчёты: reports/signals_buy.csv, reports/signals_sell.csv")

    if not args.no_paper:
        ts = db.execute("SELECT MAX(ts) FROM hourly WHERE region=? AND house=?", (args.region, home)).fetchone()[0]
        closed = paper.update_trades(db, args.region, home, ts, {i: v[1] for i, v in A.items()})
        opened = paper.open_trades(db, args.region, home, ts, [s for _, _, s in buys], bool(hist_home))
        print(f"Виртуальные сделки: открыто новых {opened}, закрыто {closed}. Итоги: python paper.py")

    if not args.no_alerts:
        send_alerts(db, args.region, home, alerts, args)
        try:
            events.notify_events(db, {i: v[1] for i, v in A.items()}, mine, notify.send)
        except Exception as e:
            print(f"  ! события (ярмарка): {e}", file=sys.stderr)


def send_alerts(db, region, home, alerts, args):
    """Telegram message for strong signals; each item at most once a day per kind."""
    days = db.execute("SELECT COUNT(DISTINCT day) FROM daily WHERE region=? AND house=?", (region, home)).fetchone()[0]
    if days < args.alert_history:
        return  # without a few days of history the "usual" gap is unknown and alerts would be noise
    db.execute("CREATE TABLE IF NOT EXISTS alerts_sent (kind TEXT, item_id INTEGER, day TEXT, "
               "PRIMARY KEY (kind, item_id, day))")
    today = time.strftime("%Y-%m-%d")
    fresh = [(k, i, t) for k, i, t in alerts if not db.execute(
        "SELECT 1 FROM alerts_sent WHERE kind=? AND item_id=? AND day=?", (k, i, today)).fetchone()]
    if not fresh:
        return
    buys = [t for k, _, t in fresh if k == "buy"]
    sells = [t for k, _, t in fresh if k == "sell"]
    text = ""
    if buys:
        text += "<b>Сильные сигналы на покупку</b> (EU PvP Альянс)\n\n" + "\n\n".join(buys[:8]) + "\n\n"
    if sells:
        text += "<b>Продать из запасов</b>\n\n" + "\n\n".join(sells[:8])
    try:
        sent = notify.send(text.strip())
    except Exception as e:
        print(f"  ! Telegram: {e}", file=sys.stderr)
        return
    if sent:
        db.executemany("INSERT OR IGNORE INTO alerts_sent VALUES (?,?,?)", [(k, i, today) for k, i, _ in fresh])
        db.commit()
        print(f"Telegram: отправлено сигналов {len(fresh)}")


def main():
    p = argparse.ArgumentParser(description="WoW Forever medium-term signals")
    p.add_argument("--region", default="eu")
    p.add_argument("--ruleset", default="pvp", choices=["pvp", "normal", "roleplay", "roleplay-pvp"])
    p.add_argument("--faction", default="alliance", choices=["alliance", "horde"])
    p.add_argument("--bw-cost", type=float, default=0.15, help="cost of moving goods via Blackwater, 0.15 = 15%%")
    p.add_argument("--min-sold", type=float, default=10, help="min sold per day in region (TSM)")
    p.add_argument("--min-rate", type=float, default=0.2, help="min sale rate (TSM)")
    p.add_argument("--min-price", type=float, default=100, help="ignore items cheaper than this (copper)")
    p.add_argument("--min-upside", type=float, default=0.10, help="min expected gain after AH cut")
    p.add_argument("--min-qty", type=int, default=5, help="min quantity listed on both faction houses")
    p.add_argument("--min-ratio", type=float, default=0.4,
                   help="ignore items where one faction is cheaper than this share of the other")
    p.add_argument("--min-dev", type=float, default=0.15,
                   help="min deviation from the usual Alliance/Horde ratio (once history exists)")
    p.add_argument("--days", type=int, default=7, help="history window for 'usual' values")
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--no-collect", action="store_true", help="don't download, report from stored data")
    p.add_argument("--en", action="store_true", help="English item names")
    p.add_argument("--no-paper", action="store_true", help="don't open/update virtual trades")
    p.add_argument("--no-alerts", action="store_true", help="don't send Telegram alerts")
    p.add_argument("--alert-dev", type=float, default=0.25, help="alert: Alliance/Horde ratio below usual by this")
    p.add_argument("--alert-sold", type=float, default=50, help="alert: min sold per day in region")
    p.add_argument("--alert-upside", type=float, default=0.15, help="alert: min gain after AH cut")
    p.add_argument("--alert-history", type=int, default=3, help="alert only after this many days of history")
    p.add_argument("--log", action="store_true", help="append output to data/run.log")
    args = p.parse_args()

    if args.log:
        os.makedirs(DATA, exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(DATA, "run.log"), "a", encoding="utf-8")
        print(f"\n##### market run at {time.strftime('%Y-%m-%d %H:%M:%S')}")

    db = open_db()
    ensure_tables(db)
    if not args.no_collect:
        print("Собираю все аукционы EU и US...")
        collect(db)
    report(db, args)


if __name__ == "__main__":
    main()
