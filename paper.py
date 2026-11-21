"""Paper trading: checks market.py buy signals without spending gold.

Every market.py run:
  * opens a virtual trade for each new buy signal (bought at the current 25th percentile price),
  * checks open trades against the latest snapshot: price reached the target -> sold at target,
    otherwise after MAX_DAYS -> sold at whatever the price is then. 5% AH cut is paid on every sale.
  * records the price after 3, 5 and 7 days.

Usage:
    python paper.py            # summary + open trades
    python paper.py --all      # also list closed trades
"""

import argparse
import time

from ah import gold, open_db, pct, table

AH_CUT = 0.05
MAX_DAYS = 7
CHECKPOINTS = (3, 5, 7)
STAKE = 1_000_000  # 100g per trade, for the "what if" gold figures


def ensure_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY, region TEXT, house INTEGER, item_id INTEGER, name TEXT,
        opened_ts INTEGER, entry REAL, target REAL, with_history INTEGER,
        day3 REAL, day5 REAL, day7 REAL,
        closed_ts INTEGER, exit REAL, outcome TEXT)""")


def open_trades(db, region, house, ts, signals, with_history):
    """signals: [(item_id, name, price, target)]"""
    ensure_table(db)
    is_open = {i for (i,) in db.execute(
        "SELECT item_id FROM paper_trades WHERE region=? AND house=? AND closed_ts IS NULL", (region, house))}
    new = [(region, house, i, name, ts, price, target, int(with_history))
           for i, name, price, target in signals if i not in is_open]
    db.executemany("""INSERT INTO paper_trades (region, house, item_id, name, opened_ts, entry, target, with_history)
                      VALUES (?,?,?,?,?,?,?,?)""", new)
    db.commit()
    return len(new)


def update_trades(db, region, house, ts, prices):
    """prices: {item_id: current 25th percentile price} from the latest snapshot at time ts."""
    ensure_table(db)
    closed = 0
    for tid, i, opened, target, d3, d5, d7 in db.execute(
            """SELECT id, item_id, opened_ts, target, day3, day5, day7 FROM paper_trades
               WHERE region=? AND house=? AND closed_ts IS NULL""", (region, house)).fetchall():
        price = prices.get(i)
        age = (ts - opened) / 86400
        marks = {"day3": d3, "day5": d5, "day7": d7}
        for d in CHECKPOINTS:
            if age >= d and marks[f"day{d}"] is None and price:
                db.execute(f"UPDATE paper_trades SET day{d}=? WHERE id=?", (price, tid))
        if price and price >= target:
            db.execute("UPDATE paper_trades SET closed_ts=?, exit=?, outcome='цель' WHERE id=?", (ts, target, tid))
            closed += 1
        elif age >= MAX_DAYS:
            # Nothing listed any more counts as unsold at the entry price (we keep the goods).
            db.execute("UPDATE paper_trades SET closed_ts=?, exit=?, outcome='таймаут' WHERE id=?",
                       (ts, price, tid))
            closed += 1
    db.commit()
    return closed


def trade_return(entry, exit_price):
    if not exit_price:
        return -AH_CUT  # couldn't sell: count as a small loss of the deposit/cut, goods kept
    return exit_price * (1 - AH_CUT) / entry - 1


def summary(db, show_all=False):
    ensure_table(db)
    rows = db.execute("""SELECT name, opened_ts, entry, target, with_history, day3, day5, day7,
                                closed_ts, exit, outcome FROM paper_trades ORDER BY opened_ts""").fetchall()
    if not rows:
        print("Виртуальных сделок пока нет.")
        return
    closed = [r for r in rows if r[8]]
    still_open = [r for r in rows if not r[8]]

    print(f"\nВиртуальные сделки: всего {len(rows)}, открыто {len(still_open)}, закрыто {len(closed)}.")
    for label, group in (("все", closed),
                         ("с историей (сигнал с учётом обычной разницы цен)", [r for r in closed if r[4]]),
                         ("без истории (первые сутки)", [r for r in closed if not r[4]])):
        if not group:
            continue
        rets = [trade_return(r[2], r[9]) for r in group]
        wins = sum(1 for r in group if r[10] == "цель")
        avg = sum(rets) / len(rets)
        print(f"  {label}: {len(group)} сделок, цель достигнута в {wins} ({wins / len(group):.0%}), "
              f"средний результат {pct(avg)}, по 100g на сделку: {gold(sum(rets) * STAKE)}")

    for d in CHECKPOINTS:
        col = 5 + CHECKPOINTS.index(d)  # day3 / day5 / day7 column
        moves = [r[col] / r[2] - 1 for r in rows if r[col]]
        if moves:
            up = sum(1 for m in moves if m > 0)
            print(f"  цена через {d} дн.: выросла в {up} из {len(moves)} ({up / len(moves):.0%}), "
                  f"в среднем {pct(sum(moves) / len(moves))}")

    now = time.time()
    headers = ["Предмет", "Открыта", "Вход", "Цель", "Через 3д", "Через 5д", "Через 7д", "Дней"]
    table("ОТКРЫТЫЕ ВИРТУАЛЬНЫЕ СДЕЛКИ", headers, [
        (r[0], time.strftime("%d.%m %H:%M", time.localtime(r[1])), gold(r[2]), gold(r[3]),
         gold(r[5]), gold(r[6]), gold(r[7]), f"{(now - r[1]) / 86400:.1f}") for r in still_open])
    if show_all:
        table("ЗАКРЫТЫЕ ВИРТУАЛЬНЫЕ СДЕЛКИ", ["Предмет", "Вход", "Выход", "Итог", "Результат"], [
            (r[0], gold(r[2]), gold(r[9]), r[10], pct(trade_return(r[2], r[9]))) for r in closed])


def main():
    p = argparse.ArgumentParser(description="Paper trading summary")
    p.add_argument("--all", action="store_true", help="also list closed trades")
    args = p.parse_args()
    summary(open_db(), args.all)


if __name__ == "__main__":
    main()
