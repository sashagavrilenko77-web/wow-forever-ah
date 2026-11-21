"""Blizzard API access: OAuth token, JSON download, Forever auction houses and Russian item names."""

import base64
import concurrent.futures
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TOKEN_CACHE = os.path.join(DATA, "token.json")


def read_env():
    env = {k: v for k, v in os.environ.items() if k.startswith("BLIZZARD_")}  # GitHub Actions secrets
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env[k.strip()] = v.strip().strip('"')
    if not env.get("BLIZZARD_CLIENT_ID") or not env.get("BLIZZARD_CLIENT_SECRET"):
        sys.exit("Fill BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET in .env")
    return env


def token():
    if os.path.exists(TOKEN_CACHE):
        with open(TOKEN_CACHE) as f:
            t = json.load(f)
        if t["expires_at"] > time.time() + 300:
            return t["access_token"]
    env = read_env()
    auth = base64.b64encode(f"{env['BLIZZARD_CLIENT_ID']}:{env['BLIZZARD_CLIENT_SECRET']}".encode()).decode()
    req = urllib.request.Request("https://oauth.battle.net/token", data=b"grant_type=client_credentials",
                                 headers={"Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=30) as r:
        t = json.load(r)
    t["expires_at"] = time.time() + t["expires_in"]
    os.makedirs(DATA, exist_ok=True)
    with open(TOKEN_CACHE, "w") as f:
        json.dump(t, f)
    return t["access_token"]


def get_json(url):
    """Return (last_modified, data)."""
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token(), "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return r.headers.get("Last-Modified", ""), json.loads(body)


# ---------- Forever auction houses ----------

AUCTIONS = ("https://{region}.api.blizzard.com/data/wow/ruleset/1/auctions/{house}{suffix}"
            "?namespace=dynamic-forever-{region}&locale=en_US")

# Same ids in EU and US. Matched against TSM per-house prices (Blackwater by numbering).
HOUSES = {
    110: ("pvp", "alliance"), 113: ("pvp", "horde"), 116: ("pvp", "blackwater"),
    111: ("normal", "alliance"), 114: ("normal", "horde"), 117: ("normal", "blackwater"),
    119: ("roleplay", "alliance"), 120: ("roleplay", "horde"), 121: ("roleplay", "blackwater"),
    123: ("roleplay-pvp", "alliance"), 124: ("roleplay-pvp", "horde"), 125: ("roleplay-pvp", "blackwater"),
}
REGIONS = ("eu", "us", "kr", "tw")
# Price *levels* are comparable only between the big western regions; KR/TW are used for price *changes*.
LEVEL_REGIONS = ("eu", "us")
# KR/TW have only PvP and Normal houses; the rest are empty there.
EMPTY = {("kr", h) for h in (119, 120, 121, 123, 124, 125)} | {("tw", h) for h in (119, 120, 121, 123, 124, 125)}


def summarize(listings):
    """listings: [(unit_price, qty)] sorted -> (min, p25, qty, auctions); p25 is quantity-weighted."""
    total = sum(n for _, n in listings)
    acc, p25 = 0, listings[-1][0]
    for price, n in listings:
        acc += n
        if acc >= total * 0.25:
            p25 = price
            break
    return listings[0][0], p25, total, len(listings)


def fetch_house(region, house):
    """-> (region, house, snapshot unix time, {item_id: (min, p25, qty, auctions)})"""
    listings, modified = {}, None
    for suffix in ("", "/commodities"):
        lm, d = get_json(AUCTIONS.format(region=region, house=house, suffix=suffix))
        if lm:
            modified = max(modified or 0, int(parsedate_to_datetime(lm).timestamp()))
        for a in d.get("auctions", []):
            if a["item"].get("bonus_lists"):
                continue  # random-suffix gear: price depends on the suffix
            price = a.get("unit_price") or (a["buyout"] / a["quantity"] if a.get("buyout") else None)
            if price:
                listings.setdefault(a["item"]["id"], []).append((price, a["quantity"]))
    for v in listings.values():
        v.sort()
    return region, house, modified or int(time.time()), {i: summarize(v) for i, v in listings.items()}


def fetch_all():
    """Yield (region, house, ts, items, error) for every Forever house in EU, US, KR and TW."""
    token()  # get the token once before the threads start
    jobs = [(r, h) for r in REGIONS for h in HOUSES if (r, h) not in EMPTY]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_house, r, h): (r, h) for r, h in jobs}
        for f in concurrent.futures.as_completed(futures):
            r, h = futures[f]
            try:
                yield (*f.result(), None)
            except Exception as e:
                yield r, h, None, None, e


# ---------- item names ----------

SEARCH = ("https://{region}.api.blizzard.com/data/wow/search/item?namespace=static-forever-{region}"
          "&orderby=id&_pageSize=1000&id=[{start},]")
ITEM = "https://{region}.api.blizzard.com/data/wow/item/{id}?namespace=static-{game}-{region}&locale=ru_RU"
# Some old items are missing from Forever's item data; Classic Era / Classic have the same names.
ITEM_GAMES = ("forever", "classic1x", "classic")
MAX_SINGLE_LOOKUPS = 2000


def _save(db, rows):
    db.executemany("INSERT OR REPLACE INTO item_names VALUES (?,?,?)", rows)
    db.commit()


def _download(db, region, missing):
    # Bulk: the item search returns up to 1000 items (all locales) per request, walk it by id.
    if len(missing) > 50:
        start, last = min(missing), max(missing)
        while start <= last:
            _, d = get_json(SEARCH.format(region=region, start=start))
            results = d.get("results", [])
            if not results:
                break
            _save(db, [(r["data"]["id"], (r["data"].get("name") or {}).get("ru_RU"),
                        (r["data"].get("name") or {}).get("en_GB")) for r in results])
            start = results[-1]["data"]["id"] + 1
            print(f"  item names: up to id {start - 1}...")
    # Whatever the search missed: one request per item.
    have = {i for (i,) in db.execute("SELECT item_id FROM item_names")}
    for iid in sorted(missing - have)[:MAX_SINGLE_LOOKUPS]:
        name = None
        for game in ITEM_GAMES:
            try:
                _, d = get_json(ITEM.format(region=region, game=game, id=iid))
                name = d.get("name")
                break
            except urllib.error.HTTPError:
                continue
        _save(db, [(iid, name, None if name else "?")])  # "?" = unknown item, don't ask again


def russian_names(db, ids, region="eu"):
    """{item_id: Russian name} for the given ids, downloading and caching unknown ones."""
    db.execute("CREATE TABLE IF NOT EXISTS item_names (item_id INTEGER PRIMARY KEY, ru TEXT, en TEXT)")
    known = {i for (i,) in db.execute("SELECT item_id FROM item_names WHERE ru IS NOT NULL")}
    known |= {i for (i,) in db.execute("SELECT item_id FROM item_names WHERE ru IS NULL AND en = '?'")}
    missing = set(ids) - known
    if missing:
        print(f"Downloading Russian names for {len(missing)} items...")
        try:
            _download(db, region, missing)
        except (Exception, SystemExit) as e:
            print(f"  ! could not download item names: {e}", file=sys.stderr)
    return {i: ru for i, ru in db.execute("SELECT item_id, ru FROM item_names WHERE ru IS NOT NULL")}
