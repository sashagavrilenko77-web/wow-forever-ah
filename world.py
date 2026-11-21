"""World index: how prices change across every Forever auction house (EU, US, KR, TW).

Price levels differ between regions (population, gold supply), so we compare *changes*:
for each faction house, price now vs ~1 day / ~7 days ago, then the median over houses.
  * item index  = median change of one item over all houses where it trades;
  * market index = median of item indices over items traded on many houses.
If our house moves and the world doesn't -> local anomaly (speculation, dump); if the world moves too ->
a game-wide shift (patch, phase, people learned to farm it) that a "cheap" signal shouldn't fight.

Usage:  python world.py        # market index by region + biggest world movers
"""

import time

from blizzard import EMPTY, HOUSES, REGIONS

WINDOWS = {"6h": 6 * 3600, "1d": 86400, "7d": 7 * 86400}
TOLERANCE = 3 * 3600     # accept a snapshot up to 3 h away from the wanted moment
MIN_QTY = 5              # ignore items with almost nothing listed: their price is noise
MIN_HOUSES = 5           # an item index needs this many houses


def faction_houses():
    """Alliance and Horde houses everywhere (Blackwater is too thin for a stable median)."""
    return [(r, h) for r in REGIONS for h, (_, f) in HOUSES.items() if f != "blackwater" and (r, h) not in EMPTY]


def snapshot(db, region, house, ts):
    return {i: p for i, p, q in db.execute(
        "SELECT item_id, p25, qty FROM hourly WHERE region=? AND house=? AND ts=?", (region, house, ts)) if q >= MIN_QTY}


def ts_near(db, region, house, target):
    row = db.execute("""SELECT ts FROM hourly WHERE region=? AND house=? AND ts BETWEEN ? AND ?
                        GROUP BY ts ORDER BY ABS(ts - ?) LIMIT 1""",
                     (region, house, target - TOLERANCE, target + TOLERANCE, target)).fetchone()
    return row[0] if row else None


def house_changes(db, region, house):
    """{window: {item_id: change}} for one house, comparing the latest snapshot with ~1d / ~7d earlier."""
    last = db.execute("SELECT MAX(ts) FROM hourly WHERE region=? AND house=?", (region, house)).fetchone()[0]
    if not last:
        return {}
    now = snapshot(db, region, house, last)
    out = {}
    for name, seconds in WINDOWS.items():
        then_ts = ts_near(db, region, house, last - seconds)
        if not then_ts and name == "6h":
            # short history (first day of collecting): take the oldest snapshot that is at least 2 h old
            then_ts = db.execute("SELECT MIN(ts) FROM hourly WHERE region=? AND house=? AND ts BETWEEN ? AND ?",
                                 (region, house, last - seconds, last - 2 * 3600)).fetchone()[0]
        if then_ts:
            then = snapshot(db, region, house, then_ts)
            out[name] = {i: now[i] / then[i] - 1 for i in now if then.get(i)}
    return out


def median(v):
    v = sorted(v)
    n = len(v)
    if not n:
        return None
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def compute(db):
    """{"houses": {(region, house): {window: {item: change}}},
        "items": {window: {item: (median change, houses)}},
        "market": {window: (median, items)}, "regions": {window: {region: median}}}"""
    houses = {rh: house_changes(db, *rh) for rh in faction_houses()}
    items, market, regions = {}, {}, {}
    for w in WINDOWS:
        per_item = {}
        for rh, ch in houses.items():
            for i, c in ch.get(w, {}).items():
                per_item.setdefault(i, []).append(c)
        items[w] = {i: (median(v), len(v)) for i, v in per_item.items() if len(v) >= MIN_HOUSES}
        broad = [m for m, n in items[w].values() if n >= 10]
        market[w] = (median(broad), len(broad)) if broad else (None, 0)
        regions[w] = {}
        for r in REGIONS:
            vals = [median(list(ch[w].values())) for (rr, _), ch in houses.items() if rr == r and ch.get(w)]
            if vals:
                regions[w][r] = median(vals)
    return {"houses": houses, "items": items, "market": market, "regions": regions}


def classify(local, world, threshold=0.10):
    """Where does a price move come from? -> short Russian tag."""
    if local is None or world is None:
        return "нет истории"
    if local - world <= -threshold:
        return "дешевеет только у нас"
    if local - world >= threshold:
        return "дорожает только у нас"
    if world <= -threshold:
        return "дешевеет во всей игре"
    if world >= threshold:
        return "дорожает во всей игре"
    return "как во всём мире"


def pick(changes, item, prefer=("7d", "1d", "6h")):
    """First available window -> (window, value)."""
    for w in prefer:
        v = changes.get(w, {}).get(item)
        if v is not None:
            return w, (v[0] if isinstance(v, tuple) else v)
    return None, None


if __name__ == "__main__":
    from ah import open_db, pct
    from blizzard import russian_names

    db = open_db()
    t = time.time()
    wd = compute(db)
    for w in WINDOWS:
        m, n = wd["market"][w]
        regs = ", ".join(f"{r.upper()} {pct(v)}" for r, v in wd["regions"][w].items())
        print(f"Мировой индекс рынка за {w}: {pct(m) if m is not None else 'нет данных'} ({n} товаров). "
              f"По регионам: {regs or 'нет данных'}")
    w = next((w for w in ("7d", "1d", "6h") if wd["items"][w]), "6h")
    movers = sorted(((m, i, n) for i, (m, n) in wd["items"][w].items() if n >= 15), key=lambda x: x[0])
    if movers:
        names = russian_names(db, [i for _, i, _ in movers[:10] + movers[-10:]])
        print(f"\nСильнее всего подешевели в мире за {w}:")
        for m, i, n in movers[:10]:
            print(f"  {names.get(i, i)}: {pct(m)} ({n} аукционов)")
        print(f"Сильнее всего подорожали в мире за {w}:")
        for m, i, n in movers[-10:][::-1]:
            print(f"  {names.get(i, i)}: {pct(m)} ({n} аукционов)")
    print(f"\n({time.time() - t:.1f} с)")
