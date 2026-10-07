"""Verify a list of candidate outlets: does the feed exist, parse, and have entries?

Used to build and maintain data/manual_outlets.json. Reads data/candidates.json
and writes data/verified.json plus a short report on stdout.

Usage:
    python scripts/verify_feeds.py [--in data/candidates.json] [--out data/verified.json]
                                   [--workers 16] [--contact you@example.com]
"""
from __future__ import annotations

import argparse
import calendar
import json
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor

import feedparser
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent


def check(session: requests.Session, outlet: dict):
    feed = outlet.get("feed") or ""
    result = dict(outlet)
    result["ok"] = False
    result["items"] = 0
    result["newest_days"] = None
    result["feed_lang"] = ""
    if not feed:
        return result
    try:
        r = session.get(feed, timeout=20)
        if not r.ok:
            result["error"] = f"HTTP {r.status_code}"
            return result
        parsed = feedparser.parse(r.content)
        if not parsed.entries:
            result["error"] = "no entries"
            return result
        result["ok"] = True
        result["items"] = len(parsed.entries)
        result["feed_lang"] = (parsed.feed.get("language") or "")[:5]
        stamps = [e.get("published_parsed") or e.get("updated_parsed") for e in parsed.entries]
        stamps = [s for s in stamps if s]
        if stamps:
            newest = max(calendar.timegm(s) for s in stamps)
            result["newest_days"] = round((time.time() - newest) / 86400, 1)
    except Exception as exc:  # noqa: BLE001
        result["error"] = type(exc).__name__
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default="data/candidates.json")
    ap.add_argument("--out", dest="dst", default="data/verified.json")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--contact", default="set-your-email@example.com")
    args = ap.parse_args()

    candidates = json.loads((ROOT / args.src).read_text())
    session = requests.Session()
    session.headers.update({"User-Agent": f"NewsFeedBot/0.2 (personal news app; contact: {args.contact})"})

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda o: check(session, o), candidates))

    good = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    (ROOT / args.dst).write_text(json.dumps(results, ensure_ascii=False, indent=1))

    print(f"{len(good)}/{len(results)} feeds OK")
    for r in sorted(bad, key=lambda x: x["country"]):
        print(f"  FAIL {r['country']} {r['name'][:34]:34s} {r.get('error', '?')} {r.get('feed', '')[:70]}")


if __name__ == "__main__":
    main()
