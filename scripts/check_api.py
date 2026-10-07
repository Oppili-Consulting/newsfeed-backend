"""Health check for the published API.

Verifies that the deployed JSON really matches the contract the app relies on:

  api/index.json        {"generated","outlets","live","posts","countries","countries_with_posts"}
  api/outlets.json      [{"id","name","country","lang","site","live"}]
  api/posts/<CC>.json   [{"o","t","s","l","ts","y","i"}]  newest first, max 800

Usage:
    python scripts/check_api.py                          # checks the live GitHub Pages site
    python scripts/check_api.py --base http://localhost:8000/api
    python scripts/check_api.py --sample 25              # countries whose post files are checked
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

DEFAULT_BASE = "https://oppili-consulting.github.io/newsfeed-backend/api"
UN = (
    "AF AL DZ AD AO AG AR AM AU AT AZ BS BH BD BB BY BE BZ BJ BT BO BA BW BR BN BG BF BI CV KH CM CA CF TD CL CN CO "
    "KM CG CR CI HR CU CY CZ KP CD DK DJ DM DO EC EG SV GQ ER EE SZ ET FJ FI FR GA GM GE DE GH GR GD GT GN GW GY HT "
    "HN HU IS IN ID IR IQ IE IL IT JM JP JO KZ KE KI KW KG LA LV LB LS LR LY LI LT LU MG MW MY MV ML MT MH MR MU MX "
    "FM MD MC MN ME MA MZ MM NA NR NP NL NZ NI NE NG MK NO OM PK PW PA PG PY PE PH PL PT QA KR RO RU RW KN LC VC WS "
    "SM ST SA SN RS SC SL SG SK SI SB SO ZA SS ES LK SD SR SE CH SY TJ TZ TH TL TG TO TT TN TR TM TV UG UA AE GB US "
    "UY UZ VU VE VN YE ZM ZW"
).split()

problems: list[str] = []
checks = 0


def ok(condition: bool, message: str) -> None:
    global checks
    checks += 1
    if not condition:
        problems.append(message)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--sample", type=int, default=15, help="how many country post files to check")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()
    base = args.base.rstrip("/")

    session = requests.Session()
    session.headers.update({"User-Agent": "newsfeed-check/1.0"})

    def get(path: str):
        r = session.get(f"{base}/{path}", timeout=args.timeout)
        r.raise_for_status()
        return r.json()

    started = time.time()
    try:
        index = get("index.json")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"FAIL: cannot read {base}/index.json ({type(exc).__name__}: {exc})")

    for key in ("generated", "outlets", "live", "posts"):
        ok(key in index, f"index.json is missing \"{key}\"")
    ok(isinstance(index.get("outlets"), int) and index.get("outlets", 0) > 0, "index.json reports no outlets")
    ok(index.get("live", 0) > 0, "index.json reports no live feeds")
    age_hours = (time.time() - index.get("generated", 0)) / 3600
    ok(age_hours < 6, f"index.json is stale: generated {age_hours:.1f} hours ago")
    print(f"index.json      outlets={index.get('outlets')} live={index.get('live')} posts={index.get('posts')} "
          f"countries_with_posts={index.get('countries_with_posts')} age={age_hours:.1f}h")

    outlets = get("outlets.json")
    ok(isinstance(outlets, list) and len(outlets) > 0, "outlets.json is not a non-empty array")
    ok(len(outlets) == index.get("outlets"), "outlets.json length does not match index.json")
    ids = set()
    bad_country = bad_lang = dup = 0
    for o in outlets:
        if set(o) >= {"id", "name", "country", "lang", "site", "live"} is False:
            problems.append(f"outlet is missing fields: {o}")
            break
        ids.add(o["id"])
        if o["country"] not in UN:
            bad_country += 1
        if not re.fullmatch(r"[a-z]{2}|und", o.get("lang", "")):
            bad_lang += 1
    dup = len(outlets) - len(ids)
    ok(bad_country == 0, f"{bad_country} outlets have a country outside the 193 UN codes")
    ok(bad_lang == 0, f"{bad_lang} outlets have a language that is not ISO 639-1 or und")
    ok(dup == 0, f"{dup} duplicate outlet ids")
    countries = {o["country"] for o in outlets}
    print(f"outlets.json    {len(outlets)} outlets, {len(countries)} countries, "
          f"{sum(1 for o in outlets if o['live'])} with a live feed")

    with_posts = [o["country"] for o in outlets if o["live"]]
    sample = sorted(set(with_posts))
    random.seed(7)
    if len(sample) > args.sample:
        sample = random.sample(sample, args.sample)
    sample = sample or ["US", "GB", "AU"]

    def check_country(cc: str):
        try:
            posts = get(f"posts/{cc}.json")
        except Exception as exc:  # noqa: BLE001
            return cc, 0, f"unreadable ({type(exc).__name__})", []
        issues = []
        if not isinstance(posts, list):
            return cc, 0, "not an array", []
        if len(posts) > 800:
            issues.append(f"{len(posts)} posts (max 800)")
        previous = None
        for p in posts:
            if set(p) < {"o", "t", "s", "l", "ts", "y"}:
                issues.append("a post is missing required fields")
                break
            if p["o"] not in ids:
                issues.append(f"post references unknown outlet {p['o']}")
                break
            if not p["l"].startswith("http"):
                issues.append(f"post link is not a URL: {p['l'][:40]}")
                break
            if len(p["s"]) > 220 or len(p["t"]) > 200:
                issues.append("a snippet or headline exceeds the documented length")
                break
            if p["y"] not in ("article", "video", "short"):
                issues.append(f"unknown post type {p['y']}")
                break
            if p["ts"] and previous is not None and p["ts"] > previous:
                issues.append("posts are not newest first")
                break
            previous = p["ts"] or previous
        return cc, len(posts), "; ".join(issues), posts

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(check_country, sample))

    empty = [cc for cc, n, _, _ in results if n == 0]
    for cc, n, issue, _ in results:
        ok(not issue, f"posts/{cc}.json: {issue}")
    print(f"posts/*.json    checked {len(results)} countries, "
          f"{sum(n for _, n, _, _ in results)} posts, {len(empty)} empty")
    if empty:
        print(f"                empty countries in this sample: {' '.join(empty)}")

    all_posts = [p for _, _, _, posts in results for p in posts]
    if all_posts:
        with_image = sum(1 for p in all_posts if p.get("i"))
        kinds: dict = {}
        for p in all_posts:
            kinds[p["y"]] = kinds.get(p["y"], 0) + 1
        print(f"                types: {kinds}; {with_image} of {len(all_posts)} posts carry an image")

    took = time.time() - started
    if problems:
        print(f"\nFAILED {len(problems)} of {checks} checks in {took:.1f}s")
        for problem in problems[:20]:
            print("  -", problem)
        sys.exit(1)
    print(f"\nOK: {checks} checks passed in {took:.1f}s")


if __name__ == "__main__":
    main()
