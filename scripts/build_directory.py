"""Step 1: build a worldwide list of news outlets from Wikidata.

Covers newspapers, daily newspapers, news agencies, news websites and online
newspapers for every country that has an ISO 3166-1 alpha-2 code in Wikidata,
plus every hand-curated outlet in data/manual_outlets.json (always kept).

How it queries: one SPARQL request per small batch of countries, using the
country's Wikidata item id (a P297 join over all of Wikidata is far too slow and
times out). If a batch fails - WDQS resets connections around its 60 second
limit - the batch is split in half and retried, down to single countries, so a
run always makes progress. Runs at most once a month unless --force is passed.

Usage:
    python scripts/build_directory.py [--force] [--batch 4] [--workers 4]
                                      [--types Q11032,Q1110794,...]
                                      [--contact you@example.com]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlunparse

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "directory.json"
MANUAL = ROOT / "data" / "manual_outlets.json"
WDQS = "https://query.wikidata.org/sparql"

# Wikidata item ids for outlet kinds we accept. Add more via --types.
TYPES = {
    "Q11032": "newspaper",
    "Q1110794": "daily newspaper",
    "Q192283": "news agency",
    "Q1754332": "news website",
    "Q1153191": "online newspaper",
}

# The 193 UN member states (ISO 3166-1 alpha-2).
UN = (
    "AF AL DZ AD AO AG AR AM AU AT AZ BS BH BD BB BY BE BZ BJ BT BO BA BW BR BN BG BF BI CV KH CM CA CF TD CL CN CO "
    "KM CG CR CI HR CU CY CZ KP CD DK DJ DM DO EC EG SV GQ ER EE SZ ET FJ FI FR GA GM GE DE GH GR GD GT GN GW GY HT "
    "HN HU IS IN ID IR IQ IE IL IT JM JP JO KZ KE KI KW KG LA LV LB LS LR LY LI LT LU MG MW MY MV ML MT MH MR MU MX "
    "FM MD MC MN ME MA MZ MM NA NR NP NL NZ NI NE NG MK NO OM PK PW PA PG PY PE PH PL PT QA KR RO RU RW KN LC VC WS "
    "SM ST SA SN RS SC SL SG SK SI SB SO ZA SS ES LK SD SR SE CH SY TJ TZ TH TL TG TO TT TN TR TM TV UG UA AE GB US "
    "UY UZ VU VE VN YE ZM ZW"
).split()

COUNTRY_MAP_QUERY = "SELECT ?c ?code WHERE { ?c wdt:P297 ?code }"

BATCH_QUERY = """SELECT ?item ?itemLabel ?site ?lang ?c WHERE {
  VALUES ?c { %(countries)s }
  VALUES ?type { %(types)s }
  ?item wdt:P31 ?type ; wdt:P17 ?c ; wdt:P856 ?site .
  FILTER NOT EXISTS { ?item wdt:P576 ?end }
  OPTIONAL { ?item wdt:P407 ?l . ?l wdt:P218 ?lang }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en,[AUTO_LANGUAGE]". }
} LIMIT %(limit)d"""


def normalise_site(url: str) -> str:
    """Return a clean absolute https URL without query or fragment."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if not parsed.netloc:
        return ""
    return urlunparse(("https", parsed.netloc.lower(), parsed.path or "/", "", "", "")).rstrip("/")


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


# WDQS cuts a query off at 60 seconds, so waiting much longer than that only delays the
# split-and-retry path. Two attempts per batch is enough: splitting is the better retry.
QUERY_TIMEOUT = 90
QUERY_TRIES = 2


def ask(query: str, session: requests.Session, timeout: int = QUERY_TIMEOUT, tries: int = QUERY_TRIES):
    """Run one SPARQL query; return bindings, or None when every attempt failed."""
    for attempt in range(tries):
        try:
            r = session.get(WDQS, params={"query": query, "format": "json"}, timeout=timeout)
            if r.ok:
                return r.json().get("results", {}).get("bindings", [])
            print(f"      WDQS HTTP {r.status_code} (try {attempt + 1}/{tries})", flush=True)
        except Exception as exc:  # noqa: BLE001 - timeouts and resets are expected
            print(f"      WDQS {type(exc).__name__} (try {attempt + 1}/{tries})", flush=True)
        time.sleep(5 * (attempt + 1))
    return None


def query_for(codes, qids, types, limit):
    return BATCH_QUERY % {
        "countries": " ".join("wd:" + qids[c] for c in codes if c in qids),
        "types": " ".join("wd:" + t for t in types),
        "limit": limit,
    }


def fetch(codes, qids, types, limit, session, depth=0):
    """Fetch a batch, splitting it in half on failure so a run always progresses."""
    codes = [c for c in codes if c in qids]
    if not codes:
        return []
    rows = ask(query_for(codes, qids, types, limit), session)
    if rows is not None:
        return rows
    if len(codes) == 1:
        print(f"    skipped {codes[0]} (no response)", flush=True)
        return []
    mid = len(codes) // 2
    time.sleep(3)
    left = fetch(codes[:mid], qids, types, limit, session, depth + 1)
    right = fetch(codes[mid:], qids, types, limit, session, depth + 1)
    return left + right


def absorb(found: dict, bindings, qid2code) -> int:
    """Merge SPARQL bindings into the found map, keyed by Wikidata item id.

    Keying by item id matters: an item can carry several websites (P856) or several
    countries (P17), which produces one SPARQL row per combination. Keying by country and
    host instead would list the same outlet two or three times with the same id.
    """
    added = 0
    for b in bindings:
        try:
            name = b["itemLabel"]["value"].strip()
            site = normalise_site(b["site"]["value"])
            cc = qid2code.get(b["c"]["value"].rsplit("/", 1)[-1], "")
            item = b["item"]["value"].rsplit("/", 1)[-1]
        except (KeyError, AttributeError):
            continue
        if not site or cc not in UN or not item:
            continue
        if re.fullmatch(r"Q\d+", name):  # no usable label
            continue
        host = host_of(site)
        if not host or "." not in host:
            continue
        lang = ((b.get("lang") or {}).get("value") or "").strip().lower()[:2]
        if not re.fullmatch(r"[a-z]{2}", lang):
            lang = ""
        if item in found:
            if not found[item]["lang"] and lang:
                found[item]["lang"] = lang
            continue
        found[item] = {
            "id": "wd" + item,
            "name": name,
            "country": cc,
            "lang": lang,
            "site": site,
        }
        added += 1
    return added


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebuild even if the directory is fresh")
    ap.add_argument("--types", default=",".join(TYPES), help="comma-separated Wikidata type ids")
    ap.add_argument("--limit", type=int, default=20000, help="row limit per query")
    ap.add_argument("--batch", type=int, default=4, help="countries per query")
    ap.add_argument("--workers", type=int, default=4, help="queries in flight")
    ap.add_argument("--contact", default="set-your-email@example.com", help="contact address in the User-Agent")
    args = ap.parse_args()

    types = [t.strip() for t in args.types.split(",") if t.strip()]

    if OUT.exists() and not args.force:
        try:
            if time.time() - json.loads(OUT.read_text())["generated"] < 30 * 86400:
                print("Directory is fresh, skipping. Use --force to rebuild.")
                return
        except Exception:  # noqa: BLE001 - unreadable file, rebuild
            pass

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": f"NewsFeedBot/0.2 (personal news app; contact: {args.contact})",
            "Accept": "application/sparql-results+json",
        }
    )

    print("Resolving country codes to Wikidata items...", flush=True)
    rows = ask(COUNTRY_MAP_QUERY, session, timeout=60) or []
    qid2code = {}
    for b in rows:
        code = b["code"]["value"].strip().upper()
        if len(code) == 2:
            qid2code[b["c"]["value"].rsplit("/", 1)[-1]] = code
    qids = {code: qid for qid, code in qid2code.items()}
    print(f"  {len(qids)} country codes known to Wikidata", flush=True)

    batches = [UN[i : i + args.batch] for i in range(0, len(UN), args.batch)]
    found: dict = {}
    started = time.time()
    print(f"Querying {len(batches)} batches of {args.batch} countries ({args.workers} in flight)", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch, b, qids, types, args.limit, session) for b in batches]
        for i, fut in enumerate(futures, 1):
            added = absorb(found, fut.result(), qid2code)
            if i % 5 == 0 or i == len(batches):
                print(
                    f"  batch {i}/{len(batches)}: {len(found)} outlets, {time.time() - started:.0f}s elapsed",
                    flush=True,
                )

    # Hand-curated outlets win over Wikidata entries for the same country and host.
    manual = json.loads(MANUAL.read_text()) if MANUAL.exists() else []
    manual_ids, skipped = set(), []
    for m in manual:
        if m.get("country") not in UN:
            skipped.append(f"{m.get('name')} ({m.get('country')})")
            continue
        site = normalise_site(m.get("site", "")) or normalise_site(m.get("feed", ""))
        m = {**m, "site": site}
        manual_ids.add(m["id"])
        found[m["id"]] = m
    print(f"Manual outlets merged: {len(manual) - len(skipped)}", flush=True)
    if skipped:
        print(f"  skipped {len(skipped)} curated outlets outside the 193 UN countries: {', '.join(skipped)}")

    if not found:
        sys.exit("No outlets found and no manual outlets; keeping the old directory.")

    # One row per outlet: curated entries claim their host first, then everything else.
    outlets, seen_hosts = [], set()
    ordered = sorted(
        found.values(), key=lambda o: (o["id"] not in manual_ids, o["country"], o["name"].lower())
    )
    for o in ordered:
        key = (o["country"], host_of(o["site"]))
        if key in seen_hosts:
            continue
        seen_hosts.add(key)
        outlets.append(o)
    outlets.sort(key=lambda o: (o["country"], o["name"].lower()))

    duplicates = len(outlets) - len({o["id"] for o in outlets})
    if duplicates:
        sys.exit(f"Refusing to publish: {duplicates} duplicate outlet ids")
    OUT.write_text(json.dumps({"generated": time.time(), "outlets": outlets}, ensure_ascii=False, separators=(",", ":")))
    countries = {o["country"] for o in outlets}
    print(f"Saved {len(outlets)} outlets across {len(countries)} countries -> {OUT}")
    missing = [c for c in UN if c not in countries]
    if missing:
        print(f"Countries with no outlet found ({len(missing)}): {' '.join(missing)}")


if __name__ == "__main__":
    main()
