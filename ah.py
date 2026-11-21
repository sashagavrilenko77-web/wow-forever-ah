"""WoW Forever auction house helper.

Downloads TradeSkillMaster public pricing data (https://tradeskillmaster.com/public-data),
stores every snapshot in a local SQLite database to build our own history,
and prints trading ideas for your auction house.

Pricing data from TradeSkillMaster (https://tradeskillmaster.com).

Usage:
    python ah.py                      # EU, PvP Alliance (defaults)
    python ah.py --min-sold 5 --top 40
    python ah.py --item "Льняной"     # look up one item everywhere (Russian or English name)
    python ah.py --en                 # English item names
"""

import argparse
import csv
import io
import os
import sqlite3
import sys
import time
import urllib.request

BASE = "https://public-data.tradeskillmaster.com/forever"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
REPORTS = os.path.join(HERE, "reports")
DB_PATH = os.path.join(DATA, "ah.sqlite")

AH_CUT = 0.05          # auction house takes 5% of the sale
CACHE_SECONDS = 3600   # TSM asks to download each file at most once per update

# Houses TSM publishes for Forever. Slugs verified: pvp-alliance, pvp-horde, normal-alliance.
COMPARE_HOUSES = ["pvp-alliance", "pvp-horde", "normal-alliance", "normal-horde"]


# ---------- download ----------

def fetch(path):
    """Return CSV rows for a TSM path, using a local cache to avoid re-downloading."""
    cache = os.path.join(DATA, "raw", path.replace("/", "_"))
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < CACHE_SECONDS:
        with open(cache, encoding="utf-8") as f:
            text = f.read()
    else:
        req = urllib.request.Request(f"{BASE}/{path}", headers={"User-Agent": "wow-forever-ah-helper/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                text = r.read().decode("utf-8")
        except Exception as e:
            print(f"  ! could not download {path}: {e}", file=sys.stderr)
            return None
        with open(cache, "w", encoding="utf-8") as f:
            f.write(text)
    return list(csv.DictReader(io.StringIO(text)))


def num(v):
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


# ---------- storage ----------

def open_db():
    os.makedirs(DATA, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS house_prices (
            region TEXT, house TEXT, item_id INTEGER, name TEXT, updated_at TEXT,
            market_value REAL, min_buyout REAL, recent REAL, historical REAL,
            PRIMARY KEY (region, house, item_id, updated_at));
        CREATE TABLE IF NOT EXISTS region_prices (
            region TEXT, item_id INTEGER, name TEXT, updated_at TEXT,
            market_value REAL, historical REAL, avg_sale_price REAL,
            sale_rate REAL, sold_per_day REAL,
            PRIMARY KEY (region, item_id, updated_at));
    """)
    return db


def store_house(db, region, house, rows):
    db.executemany(
        "INSERT OR IGNORE INTO house_prices VALUES (?,?,?,?,?,?,?,?,?)",
        [(region, house, int(r["itemId"]), r["name"], r["updatedAt"], num(r["marketValue"]),
          num(r["minBuyout"]), num(r["recent"]), num(r["historical"])) for r in rows])


def store_region(db, region, rows):
    db.executemany(
        "INSERT OR IGNORE INTO region_prices VALUES (?,?,?,?,?,?,?,?,?)",
        [(region, int(r["itemId"]), r["name"], r["updatedAt"], num(r["marketValue"]),
          num(r["historical"]), num(r["avgSalePrice"]), num(r["saleRate"]),
          num(r["soldPerDay"])) for r in rows])


# ---------- formatting ----------

def gold(copper):
    if copper is None:
        return "-"
    c = int(round(copper))
    sign = "-" if c < 0 else ""
    c = abs(c)
    g, s, c = c // 10000, (c // 100) % 100, c % 100
    if g:
        return f"{sign}{g}g {s:02d}s"
    if s:
        return f"{sign}{s}s {c:02d}c"
    return f"{sign}{c}c"


def pct(x):
    return "-" if x is None else f"{x * 100:+.0f}%"


def table(title, headers, rows):
    print(f"\n=== {title} ===")
    if not rows:
        print("  (nothing matches the filters)")
        return
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) for i, h in enumerate(headers)]
    line = "  ".join(str(h).ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)))


def write_csv(name, headers, rows):
    os.makedirs(REPORTS, exist_ok=True)
    path = os.path.join(REPORTS, name)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)
    return path


# ---------- analysis ----------

def analyse(home, region_stats, others, args):
    items = []
    for r in home:
        iid = int(r["itemId"])
        reg = region_stats.get(iid)
        if not reg or not r["name"].strip():
            continue
        mv, mb, recent, hist = (num(r[k]) for k in ("marketValue", "minBuyout", "recent", "historical"))
        sold, rate = num(reg["soldPerDay"]) or 0, num(reg["saleRate"]) or 0
        if sold < args.min_sold or rate < args.min_rate or not mv:
            continue
        items.append(dict(id=iid, name=r["name"], mv=mv, mb=mb, recent=recent, hist=hist,
                          sold=sold, rate=rate, asp=num(reg["avgSalePrice"])))

    # Price trend: recent price vs historical on our house (TSM). Trading signals live in market.py.
    cheap, pricey = [], []
    for it in items:
        if not it["recent"] or not it["hist"] or it["hist"] < args.min_price:
            continue
        dev = it["recent"] / it["hist"] - 1
        row = (it["name"], gold(it["recent"]), gold(it["hist"]), pct(dev), f"{it['sold']:.0f}")
        if dev <= -args.trend:
            cheap.append((dev, row))
        elif dev >= args.trend:
            pricey.append((-dev, row))
    cheap.sort(key=lambda x: x[0])
    pricey.sort(key=lambda x: x[0])
    trend_headers = ["Item", "Recent", "Historical", "Change", "Sold/day"]

    table("CHEAPER THAN USUAL now (TSM: recent vs historical)", trend_headers, [r for _, r in cheap[:args.top]])
    table("MORE EXPENSIVE THAN USUAL now (TSM: recent vs historical)", trend_headers, [r for _, r in pricey[:args.top]])

    paths = [
        write_csv("cheap_now.csv", trend_headers, [r for _, r in cheap]),
        write_csv("expensive_now.csv", trend_headers, [r for _, r in pricey]),
    ]
    print("\nReports saved:", *[os.path.relpath(p, HERE) for p in paths], sep="\n  ")


def lookup(name, home, region_stats, others, args):
    q = name.lower()
    hits = [r for r in home if q in r["name"].lower() or q in r.get("name_en", "").lower()]
    if not hits:
        print(f"No item matching '{name}' on {args.region}/{args.house}.")
        return
    for r in hits[:10]:
        iid = int(r["itemId"])
        reg = region_stats.get(iid, {})
        print(f"\n{r['name']} (id {iid})")
        print(f"  {args.house}: market {gold(num(r['marketValue']))}, min buyout {gold(num(r['minBuyout']))}, "
              f"recent {gold(num(r['recent']))}, historical {gold(num(r['historical']))}")
        for h, o in others.items():
            if iid in o:
                print(f"  {h}: market {gold(num(o[iid]['marketValue']))}, min buyout {gold(num(o[iid]['minBuyout']))}")
        if reg:
            print(f"  {args.region} region: avg sale {gold(num(reg['avgSalePrice']))}, "
                  f"sold/day {reg['soldPerDay']}, sale rate {reg['saleRate']}")


def main():
    p = argparse.ArgumentParser(description="WoW Forever AH helper (TSM public data)")
    p.add_argument("--region", default="eu")
    p.add_argument("--house", default="pvp-alliance")
    p.add_argument("--min-sold", type=float, default=3, help="min sold per day in region (liquidity)")
    p.add_argument("--min-rate", type=float, default=0.15, help="min sale rate (share of listings that sell)")
    p.add_argument("--min-price", type=float, default=500, help="ignore items cheaper than this (copper)")
    p.add_argument("--trend", type=float, default=0.20, help="deviation threshold for trend/compare tables")
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--item", help="look up one item by name")
    p.add_argument("--log", action="store_true", help="append output to data/run.log (for scheduled runs)")
    p.add_argument("--en", action="store_true", help="English item names instead of Russian")
    args = p.parse_args()

    if args.log:
        os.makedirs(DATA, exist_ok=True)
        sys.stdout = sys.stderr = open(os.path.join(DATA, "run.log"), "a", encoding="utf-8")
        print(f"\n##### run at {time.strftime('%Y-%m-%d %H:%M:%S')}")

    print(f"Downloading TSM data for {args.region}/{args.house}...")
    home = fetch(f"{args.region}/realm/{args.house}/items.csv")
    region = fetch(f"{args.region}/region/items.csv")
    if not home or not region:
        sys.exit("Could not get the main data files.")
    others = {}
    for h in COMPARE_HOUSES:
        if h != args.house:
            rows = fetch(f"{args.region}/realm/{h}/items.csv")
            if rows:
                others[h] = {int(r["itemId"]): r for r in rows}

    db = open_db()
    store_house(db, args.region, args.house, home)
    store_region(db, args.region, region)
    for h, o in others.items():
        store_house(db, args.region, h, o.values())
    db.commit()
    n = db.execute("SELECT COUNT(DISTINCT updated_at) FROM house_prices WHERE house=?", (args.house,)).fetchone()[0]
    print(f"Data updated at {home[0]['updatedAt']}. Snapshots in local history: {n}.")

    if not args.en:
        from blizzard import russian_names
        ru = russian_names(db, [int(r["itemId"]) for r in home], args.region)
        for r in home:
            r["name_en"] = r["name"]
            r["name"] = ru.get(int(r["itemId"])) or r["name"]

    region_stats = {int(r["itemId"]): r for r in region}
    if args.item:
        lookup(args.item, home, region_stats, others, args)
    else:
        analyse(home, region_stats, others, args)
    print("\nPricing data from TradeSkillMaster (https://tradeskillmaster.com)")


if __name__ == "__main__":
    main()
