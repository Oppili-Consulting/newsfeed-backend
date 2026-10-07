"""Step 2: find each outlet's public RSS/Atom feed automatically.

For every outlet in data/directory.json that has no known feed yet:
  1. fetch the outlet homepage and look for a <link rel="alternate" type="application/rss+xml">,
  2. otherwise try the usual feed paths (/feed, /rss, /rss.xml, /atom.xml, ...),
  3. keep the first candidate that actually parses and contains entries.

Results are stored in data/feeds.json as {outlet id: {feed, lang, title, tried}}
so later runs skip what already worked and retry failures after 30 days.
robots.txt is honoured for the paths we probe.

Usage:
    python scripts/discover_feeds.py [--limit 1500] [--workers 24] [--contact you@example.com]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import feedparser
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
D = ROOT / "data"
FEEDS = D / "feeds.json"

CANDIDATE_PATHS = (
    "/feed",
    "/rss",
    "/rss.xml",
    "/feed/",
    "/index.xml",
    "/atom.xml",
    "/feeds/all.atom.xml",
    "/news/feed",
    "/?feed=rss2",
    "/feed/rss",
)
FEED_TYPES = re.compile(r"application/(rss|atom)\+xml|application/feed\+json|text/xml", re.I)

_robots: dict = {}


def robots_for(session: requests.Session, url: str):
    """Cached robots.txt parser for a URL's origin (None when unavailable)."""
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    if origin in _robots:
        return _robots[origin]
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = session.get(origin + "/robots.txt", timeout=10)
        if r.ok and "html" not in r.headers.get("content-type", ""):
            rp.parse(r.text.splitlines())
            _robots[origin] = rp
            return rp
    except Exception:  # noqa: BLE001 - a missing robots.txt is not an error
        pass
    _robots[origin] = None
    return None


def allowed(session: requests.Session, url: str) -> bool:
    rp = robots_for(session, url)
    if rp is None:
        return True
    try:
        return rp.can_fetch(session.headers.get("User-Agent", "*"), url)
    except Exception:  # noqa: BLE001
        return True


def valid_feed(session: requests.Session, url: str):
    """Return (url, lang, title) when the URL really is a feed with entries."""
    try:
        r = session.get(url, timeout=12)
        if not r.ok or not r.content:
            return None
        f = feedparser.parse(r.content)
        if not f.entries:
            return None
        lang = (f.feed.get("language") or "")[:2].lower()
        if not re.fullmatch(r"[a-z]{2}", lang or ""):
            lang = ""
        return url, lang, (f.feed.get("title") or "").strip()[:120]
    except Exception:  # noqa: BLE001 - dead or blocking site
        return None


def discover(session: requests.Session, outlet: dict):
    site = outlet.get("site") or ""
    if not site:
        return None
    # 1) <link rel="alternate"> on the homepage.
    try:
        if allowed(session, site):
            r = session.get(site, timeout=15)
            if r.ok:
                head = r.text[:400_000]
                for tag in re.findall(r"<link\b[^>]*>", head, re.I):
                    if not FEED_TYPES.search(tag):
                        continue
                    m = re.search(r"""href\s*=\s*["']([^"']+)""", tag, re.I)
                    if not m:
                        continue
                    cand = urljoin(r.url, m.group(1))
                    if not allowed(session, cand):
                        continue
                    res = valid_feed(session, cand)
                    if res:
                        return res
    except Exception:  # noqa: BLE001
        pass
    # 2) Well-known paths.
    base = site if site.endswith("/") else site + "/"
    for path in CANDIDATE_PATHS:
        cand = urljoin(base, path.lstrip("/"))
        if not allowed(session, cand):
            continue
        res = valid_feed(session, cand)
        if res:
            return res
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=1500, help="outlets checked per run")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--retry-days", type=int, default=30, help="re-check outlets that failed this long ago")
    ap.add_argument("--contact", default="set-your-email@example.com")
    args = ap.parse_args()

    directory = json.loads((D / "directory.json").read_text())["outlets"]
    feeds = json.loads(FEEDS.read_text()) if FEEDS.exists() else {}
    now = time.time()
    todo = []
    for o in directory:
        if o.get("feed") or o.get("yt"):
            continue  # hand-curated entries already carry a working feed
        prev = feeds.get(o["id"])
        if prev and prev.get("feed"):
            continue  # already solved
        if prev and now - prev.get("tried", 0) < args.retry_days * 86400:
            continue  # recently failed, leave it alone
        todo.append(o)
    todo = todo[: args.limit]

    print(f"Checking {len(todo)} of {len(directory)} outlets for feeds ({args.workers} workers)", flush=True)
    session = requests.Session()
    session.headers.update({"User-Agent": f"NewsFeedBot/0.2 (personal news app; contact: {args.contact})"})
    found_now = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (o, res) in enumerate(zip(todo, pool.map(lambda x: discover(session, x), todo)), 1):
            feeds[o["id"]] = {
                "feed": res[0] if res else None,
                "lang": res[1] if res else "",
                "title": res[2] if res else "",
                "tried": now,
            }
            if res:
                found_now += 1
            if i % 50 == 0:
                print(f"  {i}/{len(todo)} checked, {found_now} feeds found", flush=True)

    FEEDS.write_text(json.dumps(feeds, ensure_ascii=False, separators=(",", ":")))
    total = sum(1 for v in feeds.values() if v.get("feed"))
    print(f"Done. Checked {len(todo)}, found {found_now}. Known working feeds: {total}")


if __name__ == "__main__":
    main()
