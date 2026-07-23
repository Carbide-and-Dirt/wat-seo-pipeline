#!/usr/bin/env python3
"""
places_discover.py — DISCOVER every business of a type across an area (Places API New).

The skill's places_reviews.py *resolves a known list* of entities; this *finds*
them. Given a discovery config (query + a list of towns), it runs Places
searchText with pagination across each town, dedupes by place_id, and records
each company's name, address, phone, website, rating and review count — flagging
companies that have **no website** (prime leads for a web-build pitch). This is
the discovery front-end for prospecting an entire local market.

Requires GOOGLE_PLACES_API_KEY in .env with **Places API (New)** enabled on the
Google Cloud project (https://console.cloud.google.com/apis/library/places.googleapis.com).

Usage:
    python tools/places_discover.py targets/<area>-discovery.json
    python tools/places_discover.py targets/<area>-discovery.json --max-pages 3 --out .tmp/discover/<slug>.json

Output: .tmp/discover/<slug>.json
    {area, query, towns, companies:[{place_id, name, address, phone, website,
     no_website, rating, review_count, types, primary_type, relevance,
     found_via:[towns], maps_url}]}
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.primaryTypeDisplayName",
        "places.types",
        "places.businessStatus",
        "places.googleMapsUri",
        "nextPageToken",
    ]
)

from lib.common import load_env, utf8_stdout

utf8_stdout()


def search_town(query, town, key, max_pages):
    """Yield place dicts for '<query> in <town>', following pagination."""
    body = {"textQuery": f"{query} in {town}", "pageSize": 20}
    page = 0
    while True:
        r = requests.post(
            SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": key,
                "X-Goog-FieldMask": FIELD_MASK,
            },
            json=body,
            timeout=30,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        for p in data.get("places", []):
            yield p
        token = data.get("nextPageToken")
        page += 1
        if not token or page >= max_pages:
            break
        # New-API paging requests MUST repeat the original textQuery/pageSize,
        # only ADDING pageToken — sending {pageToken} alone returns "Empty text_query".
        body = {**body, "pageToken": token}
        time.sleep(2)  # page tokens need a moment to become valid


DEFAULT_ADJACENT = ("plumb", "electric", "contractor", "mechanical", "repair")


def relevance(name, types, primary, keywords, primary_types=(), adjacent=DEFAULT_ADJACENT):
    """'match' = clearly the target trade · 'maybe' = adjacent (kept, flagged) ·
    'other' = unrelated (dropped unless --keep-other). Vertical-agnostic: the
    trade is defined entirely by the config's primary_types + type_keywords."""
    hay = " ".join([name or "", primary or "", " ".join(types or [])]).lower()
    if any(pt in (types or []) for pt in primary_types) or any(k in hay for k in keywords):
        return "match"
    if any(k in hay for k in adjacent):
        return "maybe"
    return "other"


def main():
    ap = argparse.ArgumentParser(
        description="Discover all businesses of a type across an area (Places API New)."
    )
    ap.add_argument("config", help="Path to a discovery config (query + towns).")
    ap.add_argument(
        "--max-pages",
        type=int,
        default=3,
        help="Pages per town (20 results each; default 3 = up to 60).",
    )
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--keep-other", action="store_true", help="Keep results that don't match the trade at all."
    )
    args = ap.parse_args()

    load_env()
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        print("ERROR: GOOGLE_PLACES_API_KEY not set in .env.", file=sys.stderr)
        return 2

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    query = cfg["query"]
    towns = cfg.get("towns", [])
    keywords = [k.lower() for k in cfg.get("type_keywords", [])]
    primary_types = cfg.get(
        "primary_types", ["hvac_contractor"]
    )  # Google place types that are a sure match
    adjacent = tuple(cfg.get("adjacent_keywords", DEFAULT_ADJACENT))
    vertical = cfg.get("vertical", "businesses")
    if not towns:
        print("ERROR: config has no 'towns'.", file=sys.stderr)
        return 1

    companies = {}  # place_id -> record
    for town in towns:
        try:
            n_before = len(companies)
            for p in search_town(query, town, key, args.max_pages):
                pid = p.get("id")
                if not pid:
                    continue
                name = (p.get("displayName") or {}).get("text")
                types = p.get("types", [])
                primary = (p.get("primaryTypeDisplayName") or {}).get("text")
                rel = relevance(name, types, primary, keywords, primary_types, adjacent)
                if rel == "other" and not args.keep_other:
                    continue
                rec = companies.get(pid)
                if rec is None:
                    rec = {
                        "place_id": pid,
                        "name": name,
                        "address": p.get("formattedAddress"),
                        "phone": p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber"),
                        "website": p.get("websiteUri"),
                        "no_website": not bool(p.get("websiteUri")),
                        "rating": p.get("rating"),
                        "review_count": p.get("userRatingCount"),
                        "business_status": p.get("businessStatus"),
                        "primary_type": primary,
                        "types": types,
                        "relevance": rel,
                        "maps_url": p.get("googleMapsUri"),
                        "location": p.get("location"),
                        "found_via": [],
                    }
                    companies[pid] = rec
                if town not in rec["found_via"]:
                    rec["found_via"].append(town)
            print(f"  OK {town}: +{len(companies) - n_before} new (total {len(companies)})")
        except Exception as e:  # noqa: BLE001
            print(f"  ! {town} -> {e}", file=sys.stderr)

    rows = sorted(companies.values(), key=lambda r: r.get("review_count") or 0, reverse=True)
    out = {
        "area": cfg.get("area"),
        "slug": cfg.get("slug"),
        "vertical": vertical,
        "industry_schema": cfg.get("industry_schema"),
        "source": "Google Places API (New) searchText",
        "query": query,
        "towns": towns,
        "count": len(rows),
        "companies": rows,
    }
    out_path = (
        Path(args.out)
        if args.out
        else Path(".tmp/discover") / f"{cfg.get('slug', 'discovery')}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    no_web = sum(1 for r in rows if r["no_website"])
    matched = sum(1 for r in rows if r["relevance"] == "match")
    print(
        f"\n{len(rows)} businesses ({matched} clear {vertical}, {len(rows) - matched} adjacent) · "
        f"{no_web} with NO website -> {out_path}"
    )
    for r in rows[:40]:
        flag = "  [NO WEBSITE]" if r["no_website"] else ""
        print(
            f"  {r.get('review_count') or 0:>4} rev · {r.get('rating') or '—'}★ · {r['name']}{flag}"
        )


if __name__ == "__main__":
    sys.exit(main())
