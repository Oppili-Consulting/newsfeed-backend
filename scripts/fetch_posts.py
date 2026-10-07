"""Step 3 (every 30 min): read every known feed and write the JSON the app downloads.

Only a headline, a short snippet, the link and the time are stored; readers are
always sent to the outlet's own page. Output (all under site/):

    api/index.json            {"generated","outlets","live","posts","countries"}
    api/outlets.json          the full directory with a live flag per outlet
    api/outlets/<CC>.json     per-country outlet lists (smaller downloads)
    api/posts/<CC>.json       newest-first posts, max 800, one file per country

Every one of the 193 UN countries gets a posts file, even when empty, so the app
never hits a 404 for a country the user selected.

Usage:
    python scripts/fetch_posts.py [--workers 32] [--per-outlet 10]
                                  [--max-age-days 45] [--limit-outlets N]
                                  [--contact you@example.com]
"""
from __future__ import annotations

import argparse
import calendar
import html
import json
import pathlib
import re
import time
from concurrent.futures import ThreadPoolExecutor

import feedparser
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
API = SITE / "api"
TAG = re.compile(r"<[^>]*>")

# The 193 UN member states (ISO 3166-1 alpha-2).
UN = (
    "AF AL DZ AD AO AG AR AM AU AT AZ BS BH BD BB BY BE BZ BJ BT BO BA BW BR BN BG BF BI CV KH CM CA CF TD CL CN CO "
    "KM CG CR CI HR CU CY CZ KP CD DK DJ DM DO EC EG SV GQ ER EE SZ ET FJ FI FR GA GM GE DE GH GR GD GT GN GW GY HT "
    "HN HU IS IN ID IR IQ IE IL IT JM JP JO KZ KE KI KW KG LA LV LB LS LR LY LI LT LU MG MW MY MV ML MT MH MR MU MX "
    "FM MD MC MN ME MA MZ MM NA NR NP NL NZ NI NE NG MK NO OM PK PW PA PG PY PE PH PL PT QA KR RO RU RW KN LC VC WS "
    "SM ST SA SN RS SC SL SG SK SI SB SO ZA SS ES LK SD SR SE CH SY TJ TZ TH TL TG TO TT TN TR TM TV UG UA AE GB US "
    "UY UZ VU VE VN YE ZM ZW"
).split()


def clean(text: str) -> str:
    """Strip tags and entities, collapse whitespace."""
    return re.sub(r"\s+", " ", html.unescape(TAG.sub(" ", text or ""))).strip()


def pull(job, session: requests.Session, max_age_ms: int, per_outlet: int):
    """Fetch one feed (RSS/Atom or YouTube) and return normalised posts."""
    url, is_yt, outlet = job
    try:
        r = session.get(url, timeout=20)
        if not r.ok:
            return []
        feed = feedparser.parse(r.content)
    except Exception:  # noqa: BLE001 - dead feeds are normal
        return []

    out, now = [], time.time() * 1000
    for entry in feed.entries[:per_outlet]:
        title = clean(entry.get("title", ""))
        link = (entry.get("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        ts = int(calendar.timegm(stamp) * 1000) if stamp else 0
        if ts and now - ts > max_age_ms:
            continue
        if is_yt:
            kind = "short" if "/shorts/" in link else "video"
        else:
            kind = "video" if "/video/" in link or "/watch?v=" in link else "article"
        img = ""
        thumbs = entry.get("media_thumbnail") or []
        if thumbs and isinstance(thumbs, list):
            img = (thumbs[0] or {}).get("url", "") or ""
        if not img:
            for enc in entry.get("enclosures") or []:
                if str(enc.get("type", "")).startswith("image/"):
                    img = enc.get("href", "") or enc.get("url", "")
                    break
        out.append(
            {
                "o": outlet["id"],
                "t": title[:200],
                "s": clean(entry.get("summary", "") or entry.get("description", ""))[:220],
                "l": link,
                "ts": ts,
                "y": kind,
                "i": img,
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--per-outlet", type=int, default=10, help="newest N entries read per feed")
    ap.add_argument("--max-age-days", type=int, default=45)
    ap.add_argument("--limit-outlets", type=int, default=0, help="only the first N outlets (0 = all)")
    ap.add_argument("--contact", default="set-your-email@example.com")
    args = ap.parse_args()

    directory = json.loads((ROOT / "data" / "directory.json").read_text())["outlets"]
    feeds_path = ROOT / "data" / "feeds.json"
    feeds = json.loads(feeds_path.read_text()) if feeds_path.exists() else {}
    if args.limit_outlets:
        directory = directory[: args.limit_outlets]

    jobs, live = [], set()
    for o in directory:
        feed = o.get("feed") or (feeds.get(o["id"]) or {}).get("feed")
        if not o.get("lang"):
            o["lang"] = (feeds.get(o["id"]) or {}).get("lang") or "und"
        if feed:
            jobs.append((feed, False, o))
            live.add(o["id"])
        if o.get("yt"):
            jobs.append((f"https://www.youtube.com/feeds/videos.xml?channel_id={o['yt']}", True, o))
            live.add(o["id"])

    print(f"Reading {len(jobs)} feeds for {len(directory)} outlets", flush=True)
    session = requests.Session()
    session.headers.update({"User-Agent": f"NewsFeedBot/0.2 (personal news app; contact: {args.contact})"})

    by_country: dict = {}
    total = 0
    max_age_ms = args.max_age_days * 86400 * 1000
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (job, posts) in enumerate(
            zip(jobs, pool.map(lambda j: pull(j, session, max_age_ms, args.per_outlet), jobs)), 1
        ):
            if posts:
                by_country.setdefault(job[2]["country"], []).extend(posts)
                total += len(posts)
            if i % 200 == 0:
                print(f"  {i}/{len(jobs)} feeds read, {total} posts", flush=True)

    API.mkdir(parents=True, exist_ok=True)
    (API / "posts").mkdir(exist_ok=True)
    (API / "outlets").mkdir(exist_ok=True)

    kept = 0
    for cc in UN:
        posts = by_country.get(cc, [])
        seen, unique = set(), []
        for p in sorted(posts, key=lambda x: -x["ts"]):
            key = p["l"] or (p["o"], p["t"])
            if key in seen:
                continue
            seen.add(key)
            unique.append(p)
        unique = unique[:800]
        kept += len(unique)
        (API / "posts" / f"{cc}.json").write_text(json.dumps(unique, ensure_ascii=False, separators=(",", ":")))

    published = []
    for o in directory:
        published.append(
            {
                "id": o["id"],
                "name": o["name"],
                "country": o["country"],
                "lang": o["lang"],
                "site": o.get("site", ""),
                "live": o["id"] in live,
            }
        )
    (API / "outlets.json").write_text(json.dumps(published, ensure_ascii=False, separators=(",", ":")))
    for cc in UN:
        rows = [o for o in published if o["country"] == cc]
        (API / "outlets" / f"{cc}.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))

    with_posts = sum(1 for cc in UN if by_country.get(cc))
    index = {
        "generated": int(time.time()),
        "outlets": len(published),
        "live": len(live),
        "posts": kept,
        "countries": len({o["country"] for o in published}),
        "countries_with_posts": with_posts,
    }
    (API / "index.json").write_text(json.dumps(index, separators=(",", ":")))
    (SITE / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>newsfeed API</title>"
        "<body style='font:16px system-ui;max-width:40em;margin:3em auto'>"
        "<h1>newsfeed API is running</h1>"
        f"<p>{index['outlets']} outlets, {index['live']} with a live feed, {index['posts']} posts, "
        f"{index['countries_with_posts']} countries with content.</p>"
        "<p><a href='api/index.json'>api/index.json</a> &middot; <a href='api/outlets.json'>api/outlets.json</a></p>"
    )
    (SITE / ".nojekyll").write_text("")
    print(json.dumps(index))


if __name__ == "__main__":
    main()
