"""Compact input for the daily market brief written by Claude (scheduled task, 10:30).

Prints, in a few screens: data coverage, market trend, top buy/sell signals, paper trading results,
your gold and stock. Reads only what market.py / paper.py / the addon already stored.

Usage:  python brief_data.py
"""

import csv
import os
import time

import character
import events
import item_card
import paper
import world
from ah import DATA, HERE, gold, open_db, pct
from market import median

REGION, HOME, OPP = "eu", 110, 113


def section(title):
    print(f"\n## {title}")


def coverage(db):
    section("Данные")
    day_ago = int(time.time()) - 86400
    n = db.execute("SELECT COUNT(DISTINCT ts) FROM hourly WHERE region=? AND house=? AND ts>?",
                   (REGION, HOME, day_ago)).fetchone()[0]
    last = db.execute("SELECT MAX(ts) FROM hourly WHERE region=? AND house=?", (REGION, HOME)).fetchone()[0]
    days = db.execute("SELECT COUNT(DISTINCT day) FROM daily WHERE region=? AND house=?", (REGION, HOME)).fetchone()[0]
    try:
        nights = db.execute("SELECT COUNT(*) FROM cloud_imports").fetchone()[0]
    except Exception:
        nights = 0
    print(f"Снимков твоего аукциона за 24 ч: {n} из ~24. Последний: "
          f"{time.strftime('%d.%m %H:%M', time.localtime(last)) if last else 'нет'}.")
    print(f"Дней истории (дневные сводки): {days}. Ночных загрузок из GitHub всего: {nights}.")
    log = os.path.join(DATA, "run.log")
    if os.path.exists(log):
        with open(log, encoding="utf-8", errors="replace") as f:
            errors = [l.strip() for l in f.readlines()[-400:] if l.lstrip().startswith("!") or "Traceback" in l]
        if errors:
            print("Ошибки в логе (последние):", *errors[-5:], sep="\n  ")


def market_trend(db):
    """Median change of liquid items on our house: today's prices vs the 7-day median."""
    section("Рынок в целом (твой аукцион)")
    hist = {}
    for i, p in db.execute("SELECT item_id, p25 FROM daily WHERE region=? AND house=? AND day >= date('now','-8 day')",
                           (REGION, HOME)):
        hist.setdefault(i, []).append(p)
    last = db.execute("SELECT MAX(ts) FROM hourly WHERE region=? AND house=?", (REGION, HOME)).fetchone()[0]
    now = dict(db.execute("SELECT item_id, p25 FROM hourly WHERE region=? AND house=? AND ts=? AND qty>=20",
                          (REGION, HOME, last)))
    moves = [now[i] / median(v) - 1 for i, v in hist.items() if i in now and len(v) >= 3 and median(v)]
    if len(moves) < 50:
        print("Мало истории для оценки тренда (нужно 3+ дня).")
        return
    print(f"Медианное изменение цен {len(moves)} ходовых товаров к их 7-дневной норме: {pct(median(moves))}. "
          f"Дешевле нормы: {sum(m < -0.05 for m in moves)}, дороже: {sum(m > 0.05 for m in moves)}.")


def signals(name, title, limit):
    section(title)
    path = os.path.join(HERE, "reports", name)
    if not os.path.exists(path):
        print("нет отчёта")
        return
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    print(f"Всего: {len(rows) - 1}. Колонки: {' | '.join(rows[0])}")
    for r in rows[1:limit + 1]:
        print(" | ".join(r))


def paper_results(db):
    section("Виртуальные сделки")
    paper.ensure_table(db)
    rows = db.execute("SELECT name, entry, target, with_history, day3, day5, day7, exit, outcome, closed_ts "
                      "FROM paper_trades").fetchall()
    closed = [r for r in rows if r[9]]
    print(f"Всего {len(rows)}, открыто {len(rows) - len(closed)}, закрыто {len(closed)}.")
    for label, group in (("с историей", [r for r in closed if r[3]]), ("без истории", [r for r in closed if not r[3]])):
        if group:
            rets = [paper.trade_return(r[1], r[7]) for r in group]
            wins = sum(r[8] == "цель" for r in group)
            print(f"  закрытые {label}: {len(group)}, цель {wins} ({wins / len(group):.0%}), "
                  f"средний результат {pct(sum(rets) / len(rets))}")
    for col, d in ((4, 3), (5, 5), (6, 7)):
        moves = [r[col] / r[1] - 1 for r in rows if r[col]]
        if moves:
            print(f"  цена через {d} дн.: выросла в {sum(m > 0 for m in moves)} из {len(moves)}, "
                  f"медиана {pct(median(moves))}")
    recent = sorted(closed, key=lambda r: -r[9])[:5]
    for r in recent:
        print(f"  закрыта: {r[0]} вход {gold(r[1])} → {gold(r[7])} ({r[8]}, {pct(paper.trade_return(r[1], r[7]))})")


def you(db):
    section("Твои персонажи")
    chars = character.load_chars()
    if not chars:
        print("Данных аддона нет (нужно зайти в игру и сделать /reload).")
        return
    for k, c in chars.items():
        print(f"{k} ({c.get('faction')}, ур. {c.get('level')}): {gold(c.get('gold'))}, "
              f"обновлено {character.ago(c.get('updated'))}")
    own = character.holdings(chars, "Alliance")
    prices = character.latest_prices(db, REGION, HOME)
    from blizzard import russian_names
    names = russian_names(db, list(own))
    top = sorted(((n * (prices.get(i) or 0), i, n) for i, n in own.items()), reverse=True)[:10]
    if top:
        print("Самое ценное у Альянса:", ", ".join(f"{names.get(i) or i} ×{n} ≈ {gold(v)}" for v, i, n in top if v))


def seasonal(db):
    section("События (ярмарка): закупка до / продажа во время, ← = выполнено условие")
    try:
        mine = character.holdings(character.load_chars(), "Alliance")
    except Exception:
        mine = {}
    lines = events.status(character.latest_prices(db, REGION, HOME), mine)
    print("\n".join(lines) if lines else "Нет предстоящих событий.")


def world_section(db, wd):
    section("Мировой индекс (медиана изменений цен по аукционам Альянса и Орды EU, US, Корея, Тайвань)")
    for w in world.WINDOWS:
        m, n = wd["market"][w]
        regs = ", ".join(f"{r.upper()} {pct(v)}" for r, v in wd["regions"][w].items())
        print(f"За {w}: весь рынок {pct(m) if m is not None else 'нет данных'} ({n} товаров); по регионам: {regs or '—'}")


def cards(db, wd, n):
    """Detailed cards (with plan and branches) for the best buy candidates."""
    section(f"Подробные карточки {n} лучших кандидатов (ходовые, не падающие во всей игре)")
    path = os.path.join(HERE, "reports", "signals_buy.csv")
    if not os.path.exists(path):
        print("нет отчёта")
        return
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    try:
        mine = character.holdings(character.load_chars(), "Alliance")
    except Exception:
        mine = {}
    good = [r for r in rows if float(r["Прод./день"] or 0) >= 30
            and not any(w in r["Предмет"] for w in ("ящик", "Задание", "задание"))]
    for r in good[:n]:
        print("\n".join(item_card.card(db, int(r["id"]), wd, mine)) + "\n")


def main():
    db = open_db()
    print(f"# Данные для разбора на {time.strftime('%d.%m.%Y %H:%M')} (EU PvP, Альянс, аукцион {HOME})")
    wd = world.compute(db)
    coverage(db)
    world_section(db, wd)
    market_trend(db)
    seasonal(db)
    signals("signals_buy.csv", "Сигналы на покупку (лучшие по ожидаемой прибыли)", 15)
    signals("signals_sell.csv", "Сигналы продавать / не покупать", 10)
    cards(db, wd, 3)
    paper_results(db)
    you(db)


if __name__ == "__main__":
    main()
