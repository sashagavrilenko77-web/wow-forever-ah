"""Night collector for GitHub Actions: one snapshot of every Forever auction house -> gzipped CSV.

Runs in the cloud while your PC is off (see .github/workflows/night.yml). The CSV is uploaded as a
workflow artifact; cloud_sync.py on your PC downloads it into data/ah.sqlite.
Needs BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET in the environment (repository secrets).
"""

import csv
import gzip
import os
import sys
import time

from blizzard import fetch_all

OUT = "snapshots"


def main():
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, time.strftime("%Y%m%d-%H%M", time.gmtime()) + ".csv.gz")
    houses = failed = 0
    with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["region", "house", "ts", "item_id", "min_price", "p25", "qty", "auctions"])
        for region, house, ts, items, error in fetch_all():
            if error:
                print(f"! {region} {house}: {error}", file=sys.stderr)
                failed += 1
                continue
            houses += 1
            for i, s in items.items():
                w.writerow([region, house, ts, i, round(s[0]), round(s[1]), s[2], s[3]])
    print(f"{path}: {houses} houses, {failed} failed, {os.path.getsize(path) // 1024} KB")
    if not houses:
        sys.exit(1)


if __name__ == "__main__":
    main()
