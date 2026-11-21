"""Downloads night snapshots made by GitHub Actions (cloud_collect.py) into data/ah.sqlite.

Called by market.py on every run; can also be run alone: python cloud_sync.py
Uses the GitHub CLI (gh), which is already logged in on this PC.
"""

import csv
import gzip
import io
import json
import os
import shutil
import subprocess
import zipfile

REPO = "sashagavrilenko77-web/wow-forever-ah"
GH = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash in scheduled runs


def gh_api(path):
    r = subprocess.run([GH, "api", path], capture_output=True, timeout=300, creationflags=NO_WINDOW)
    if r.returncode:
        raise RuntimeError(r.stderr.decode("utf-8", "replace").strip())
    return r.stdout


def sync(db, store_snapshot):
    if not os.path.exists(GH):
        return
    db.execute("CREATE TABLE IF NOT EXISTS cloud_imports (artifact_id INTEGER PRIMARY KEY, name TEXT)")
    done = {i for (i,) in db.execute("SELECT artifact_id FROM cloud_imports")}
    arts = json.loads(gh_api(f"repos/{REPO}/actions/artifacts?per_page=100"))["artifacts"]
    todo = [a for a in arts if a["name"].startswith("night-") and not a["expired"] and a["id"] not in done]
    for a in sorted(todo, key=lambda a: a["created_at"]):
        snapshots = {}
        with zipfile.ZipFile(io.BytesIO(gh_api(f"repos/{REPO}/actions/artifacts/{a['id']}/zip"))) as z:
            for name in z.namelist():
                with gzip.open(io.BytesIO(z.read(name)), "rt", encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        key = (r["region"], int(r["house"]), int(r["ts"]))
                        snapshots.setdefault(key, []).append(
                            (int(r["item_id"]), int(r["min_price"]), int(r["p25"]), int(r["qty"]), int(r["auctions"])))
        new = sum(store_snapshot(db, *key, rows) for key, rows in snapshots.items())
        db.execute("INSERT INTO cloud_imports VALUES (?,?)", (a["id"], a["name"]))
        db.commit()
        print(f"  из GitHub: {a['name']} — {len(snapshots)} снимков, новых {new}")


if __name__ == "__main__":
    from ah import open_db
    from market import ensure_tables, rollup, store_snapshot

    db = open_db()
    ensure_tables(db)
    sync(db, store_snapshot)
    rollup(db)
