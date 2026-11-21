"""Detailed card for one item: prices on your triad, corridor, history, other servers, world index,
your stock, and a trading plan with branches (what to do if the market goes this or that way).

Usage:
    python item_card.py "Ткань Скверны"      # by Russian or English name (part of it) or item id
"""

import csv
import os
import sys

import character
import world
from ah import DATA, gold, num, open_db, pct
from blizzard import HOUSES, LEVEL_REGIONS, REGIONS, russian_names

REGION, HOME, OPP, BW = "eu", 110, 113, 116
BW_COST, AH_CUT = 0.15, 0.05
MIN_GAIN = 0.10        # buy only if the corridor target still leaves 10% after the AH cut
STOP = 0.15            # exit if our price falls 15% below entry
HORIZON = 7            # days to wait for the target


def latest_rows(db, region, house, item):
    """(min, p25, qty, ts) of the item in the latest snapshot of a house."""
    last = db.execute("SELECT MAX(ts) FROM hourly WHERE region=? AND house=?", (region, house)).fetchone()[0]
    r = db.execute("SELECT min_price, p25, qty FROM hourly WHERE region=? AND house=? AND ts=? AND item_id=?",
                   (region, house, last, item)).fetchone() if last else None
    return (*r, last) if r else (None, None, 0, last)


def price_ago(db, region, house, item, seconds):
    last = db.execute("SELECT MAX(ts) FROM hourly WHERE region=? AND house=?", (region, house)).fetchone()[0]
    ts = world.ts_near(db, region, house, last - seconds) if last else None
    if not ts:
        return None, None
    r = db.execute("SELECT p25, qty FROM hourly WHERE region=? AND house=? AND ts=? AND item_id=?",
                   (region, house, ts, item)).fetchone()
    return r if r else (None, None)


def usual_ratio(db, item, days=7):
    a = dict(db.execute("SELECT day, p25 FROM daily WHERE region=? AND house=? AND item_id=? AND day>=date('now',?)",
                        (REGION, HOME, item, f"-{days} day")))
    h = dict(db.execute("SELECT day, p25 FROM daily WHERE region=? AND house=? AND item_id=? AND day>=date('now',?)",
                        (REGION, OPP, item, f"-{days} day")))
    r = [a[d] / h[d] for d in a if h.get(d)]
    return (world.median(r), len(r)) if r else (None, 0)


def tsm_stats(item):
    path = os.path.join(DATA, "raw", f"{REGION}_region_items.csv")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if int(r["itemId"]) == item:
                    return r["name"], num(r["soldPerDay"]) or 0, num(r["saleRate"]) or 0
    return None, 0, 0


def find_item(db, query):
    if query.isdigit():
        return int(query)
    q = query.lower()  # SQLite lower() is ASCII-only, so match Cyrillic names in Python
    hits = [(len(ru or en or ""), i) for i, ru, en in db.execute("SELECT item_id, ru, en FROM item_names")
            if q in (ru or "").lower() or q in (en or "").lower()]
    if hits:
        return min(hits)[1]
    path = os.path.join(DATA, "raw", f"{REGION}_region_items.csv")
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if q in r["name"].lower():
                return int(r["itemId"])
    return None


def card(db, item, wd=None, mine=None):
    wd = wd or world.compute(db)
    mine = mine or {}
    en, sold, rate = tsm_stats(item)
    name = russian_names(db, [item]).get(item) or en or f"item {item}"
    L = [f"=== {name} (id {item}) ===",
         f"Ликвидность (TSM, весь EU): ~{sold:.0f} продаж в день, продаётся {rate:.0%} выставленного."]

    amin, a, aq, _ = latest_rows(db, REGION, HOME, item)
    hmin, h, hq, _ = latest_rows(db, REGION, OPP, item)
    bmin, b, bq, _ = latest_rows(db, REGION, BW, item)
    L.append(f"Цены сейчас (мин / типичная / кол-во): Альянс {gold(amin)} / {gold(a)} / {aq}; "
             f"Орда {gold(hmin)} / {gold(h)} / {hq}; Blackwater {gold(bmin)} / {gold(b)} / {bq or 'нет лотов'}.")
    if not a:
        L.append("На твоём аукционе товара нет — анализ невозможен.")
        return L

    # Corridor
    floor = ceiling = None
    if h:
        outlet = min(h, b) if b else h
        floor, ceiling = outlet * (1 - BW_COST), (min(h / (1 - BW_COST), b) if b else h / (1 - BW_COST))
        usual, ndays = usual_ratio(db, item)
        state = ("НИЖЕ коридора — цена должна подтянуться" if a < floor else
                 "ВЫШЕ коридора — сюда повезут товар" if a > ceiling else "внутри коридора")
        L.append(f"Коридор Blackwater: цель {gold(floor)}, потолок {gold(ceiling)}; Альянс сейчас {state}. "
                 f"А/О сейчас {a / h:.2f}" + (f", обычно {usual:.2f} ({ndays} дн.)" if usual else ", обычное А/О ещё неизвестно"))

    # Our history and supply
    hist = []
    for label, sec in (("6 ч", 6 * 3600), ("1 дн", 86400), ("7 дн", 7 * 86400)):
        p, q = price_ago(db, REGION, HOME, item, sec)
        if p:
            hist.append(f"{label} назад {gold(p)} ({pct(a / p - 1)}), кол-во {q}→{aq}")
    L.append("История у нас: " + ("; ".join(hist) if hist else "пока мало данных."))

    # Price levels on other big houses (EU/US)
    levels = {}
    for r in LEVEL_REGIONS:
        for hid, (rs, f) in HOUSES.items():
            if f == "blackwater" or (r == REGION and rs == "pvp"):
                continue
            p = latest_rows(db, r, hid, item)[1]
            if p:
                levels.setdefault(r, []).append(p)
    fair = world.median([p for v in levels.values() for p in v])
    if fair:
        L.append(f"Другие аукционы EU/US: медиана {gold(fair)} (у нас {pct(a / fair - 1)}); "
                 + ", ".join(f"{r.upper()} {gold(world.median(v))}" for r, v in levels.items()))

    # World index (changes, all regions incl. KR/TW)
    wl = []
    for w in ("6h", "1d", "7d"):
        m = wd["items"][w].get(item)
        here = wd["houses"].get((REGION, HOME), {}).get(w, {}).get(item)
        if m:
            regs = []
            for r in REGIONS:
                vals = [ch[w][item] for (rr, _), ch in wd["houses"].items() if rr == r and item in ch.get(w, {})]
                if vals:
                    regs.append(f"{r.upper()} {pct(world.median(vals))}")
            wl.append(f"за {w}: мир {pct(m[0])} ({m[1]} аукц.: {', '.join(regs)}), у нас {pct(here)}")
    win, here = world.pick(wd["houses"].get((REGION, HOME), {}), item)
    there = wd["items"][win].get(item, (None, 0))[0] if win else None
    L.append("Мировой индекс: " + ("; ".join(wl) if wl else "пока нет истории.") +
             f" Итог: {world.classify(here, there)}.")

    if mine.get(item):
        L.append(f"У тебя: {mine[item]} шт. (≈{gold(mine[item] * a)} по типичной цене).")

    # Plan with branches
    if floor and a < floor:
        entry_max = floor * (1 - AH_CUT) / (1 + MIN_GAIN)
        t2 = min(x for x in (ceiling, fair) if x) if (fair or ceiling) else None
        cap = max(1, int(sold * 0.05 * 5))
        L.append("ПЛАН:")
        L.append(f"  Вход: покупать до {gold(entry_max)} (тогда цель даёт ≥{MIN_GAIN:.0%} после комиссии); "
                 f"сейчас типичная цена {gold(a)}, минимальная {gold(amin)}. Объём: не больше ~{cap} шт. "
                 f"(≈5% дневных продаж региона × 5 дней), докупать частями.")
        L.append(f"  Цель 1: {gold(floor)} — нижняя граница коридора (продать большую часть).")
        if t2 and t2 > floor * 1.05:
            L.append(f"  Цель 2: {gold(t2)} — уровень других аукционов/потолок (остаток, если рост продолжится).")
        L.append(f"  Срок: до {HORIZON} дней; не дошло до цели — выходить по рынку.")
        L.append("РАЗВИЛКИ:")
        L.append(f"  А) Наша цена растёт, Орда держится ≥{gold(h * 0.95)} → сценарий работает, продаём по целям.")
        L.append(f"  Б) Орда дешевеет к нам (ниже {gold(a / (1 - BW_COST))}) → коридор закрылся сверху, "
                 f"прибыли не будет: не докупать, выходить около входа.")
        L.append(f"  В) Мировой индекс за 7 дн. падает сильнее −10% → это тренд всей игры, сигнал ложный: выходить.")
        L.append(f"  Г) У нас резко растёт количество (×1,5 и больше) при падении цены → кто-то сбрасывает: "
                 f"не докупать, ждать стабилизации.")
        L.append(f"  Стоп: типичная цена у нас ниже {gold(a * (1 - STOP))} (−{STOP:.0%} от текущей) — закрыть позицию.")
    elif ceiling and a > ceiling:
        L.append(f"ПЛАН: не покупать. Если товар есть — продавать сейчас: потолок {gold(ceiling)}, "
                 f"выше него цена долго не держится.")
    else:
        L.append("ПЛАН: явного сигнала нет — наблюдать.")
    return L


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    db = open_db()
    item = find_item(db, " ".join(sys.argv[1:]))
    if not item:
        sys.exit("Предмет не найден.")
    try:
        mine = character.holdings(character.load_chars(), "Alliance")
    except Exception:
        mine = {}
    print("\n".join(card(db, item, mine=mine)))


if __name__ == "__main__":
    main()
