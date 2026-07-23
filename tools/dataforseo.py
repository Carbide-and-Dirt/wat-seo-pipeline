#!/usr/bin/env python3
"""
dataforseo.py — backlinks / domain authority / SERP rank via DataForSEO.

Closes two manual-report gaps at once: "no backlink/domain-authority numbers
without a paid tool" and "couldn't measure who actually ranks." DataForSEO is
one provider with Basic-auth shared across endpoints, so both live here as
subcommands.

Requires DATAFORSEO_LOGIN + DATAFORSEO_PASSWORD in .env (https://dataforseo.com/).
These are PAID endpoints (small per-call cost) — invoke deliberately.

Usage:
    # Domain authority + backlink counts for each site:
    python tools/dataforseo.py backlinks theclimbgyms.com iloverockclimbing.com

    # Organic SERP rank for the config's seo_keywords, with each entity's position:
    python tools/dataforseo.py serp targets/<name>.json --location "Nashville,Tennessee,United States"

    # Local-pack (Google Maps) rank for the same keywords — the surface that matters
    # most for local businesses; organic alone understates who is actually findable:
    python tools/dataforseo.py maps targets/<name>.json
    python tools/dataforseo.py maps targets/<name>.json --lat 37.27 --lng -107.88 --zoom 13

Output:
    .tmp/dataforseo/backlinks.json   (per domain: rank 0-1000, backlinks, referring_domains)
    .tmp/dataforseo/serp.json        (per keyword: each entity domain's best organic position)
    .tmp/dataforseo/maps.json        (per keyword: each entity's local-pack rank + pack leaders)
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE = "https://api.dataforseo.com"

from lib.common import load_env, utf8_stdout

utf8_stdout()


def creds():
    load_env()
    login = os.environ.get("DATAFORSEO_LOGIN")
    pw = os.environ.get("DATAFORSEO_PASSWORD")
    return (login, pw) if (login and pw) else None


def post(endpoint, payload, auth):
    r = requests.post(BASE + endpoint, auth=auth, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    if data.get("status_code") != 20000:
        raise RuntimeError(f"API error {data.get('status_code')}: {data.get('status_message')}")
    task = (data.get("tasks") or [{}])[0]
    if task.get("status_code") != 20000:
        raise RuntimeError(f"Task error {task.get('status_code')}: {task.get('status_message')}")
    return task.get("result") or []


def norm(domain):
    d = domain.lower().strip()
    if "://" in d:
        d = urlparse(d).netloc
    return d.removeprefix("www.")


def cmd_backlinks(args, auth):
    out = {}
    for domain in args.domains:
        d = norm(domain)
        try:
            result = post(
                "/v3/backlinks/summary/live", [{"target": d, "backlinks_status_type": "live"}], auth
            )
            row = result[0] if result else {}
            rec = {
                "rank": row.get("rank"),  # DataForSEO domain rank, 0-1000
                "backlinks": row.get("backlinks"),
                "referring_domains": row.get("referring_domains"),
                "referring_main_domains": row.get("referring_main_domains"),
                "referring_pages": row.get("referring_pages"),
            }
            out[d] = rec
            print(
                f"  OK {d}: rank={rec['rank']} backlinks={rec['backlinks']} ref_domains={rec['referring_domains']}"
            )
        except Exception as e:  # noqa: BLE001
            out[d] = {"error": str(e)}
            print(f"  ! {d} -> {e}", file=sys.stderr)
    _write("backlinks.json", out)


def cmd_serp(args, auth):
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    keywords = cfg.get("seo_keywords", [])
    if not keywords:
        print(
            "ERROR: config has no 'seo_keywords' (list of search terms like 'climbing gym nashville').",
            file=sys.stderr,
        )
        return
    location = args.location or cfg.get("serp_location") or "United States"
    ents = [cfg["brand"]] + cfg.get("competitors", [])
    ent_domains = [(e["name"], norm(e.get("domain", ""))) for e in ents if e.get("domain")]

    out = {"location": location, "keywords": {}}
    for kw in keywords:
        try:
            result = post(
                "/v3/serp/google/organic/live/advanced",
                [{"keyword": kw, "location_name": location, "language_code": "en", "depth": 20}],
                auth,
            )
            items = (result[0].get("items") if result else []) or []
            organic = [it for it in items if it.get("type") == "organic"]
            positions = {}
            for name, dom in ent_domains:
                best = None
                for it in organic:
                    itd = (it.get("domain") or "").lower().removeprefix("www.")
                    # dot-boundary match so 'climb.com' != 'myclimb.com'
                    if dom and (itd == dom or itd.endswith("." + dom)):
                        rank = it.get("rank_absolute")
                        if best is None or (rank and rank < best):
                            best = rank
                positions[name] = best  # None = not in top 20
            out["keywords"][kw] = positions
            shown = ", ".join(f"{n}:{'#' + str(p) if p else '—'}" for n, p in positions.items())
            print(f"  OK '{kw}' [{location}] -> {shown}")
        except Exception as e:  # noqa: BLE001
            out["keywords"][kw] = {"error": str(e)}
            print(f"  ! '{kw}' -> {e}", file=sys.stderr)
    _write("serp.json", out)


MAPS_LIVE = "/v3/serp/google/maps/live/advanced"
MAPS_RATE_USD = 0.002  # per request (one keyword x one location), live queue


def maps_positions(items, entities_meta):
    """(positions, leaders) for one Maps result list.

    entities_meta: [(name, domain_or_empty, aliases)]. Matching is domain-first
    (dot-boundary, same discipline as cmd_serp); listings without a website fall
    back to a word-boundary title match. Ads/non-business rows (no place_id/cid)
    are skipped, mirroring geo_grid.extract_rank. leaders = top 3 pack entries.
    """
    from check_ai_visibility import mentioned  # tested word-boundary matcher

    positions = {name: None for name, _, _ in entities_meta}
    leaders = []
    pos = 0
    for it in items:
        if not (it.get("place_id") or it.get("cid")):
            continue
        pos += 1
        rank = it.get("rank_absolute") or it.get("rank_group") or pos
        itd = (it.get("domain") or "").lower().removeprefix("www.")
        title = it.get("title") or ""
        rating = it.get("rating") or {}
        if len(leaders) < 3:
            leaders.append(
                {
                    "title": title,
                    "rating": rating.get("value"),
                    "reviews": rating.get("votes_count"),
                    "domain": itd or None,
                }
            )
        for name, dom, aliases in entities_meta:
            if positions[name] is not None:
                continue
            dom_hit = dom and (itd == dom or itd.endswith("." + dom))
            if dom_hit or (not itd and mentioned(title, aliases)):
                positions[name] = int(rank)
    return positions, leaders


def cmd_maps(args, auth):
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    keywords = cfg.get("seo_keywords", [])
    if not keywords:
        print("ERROR: config has no 'seo_keywords'.", file=sys.stderr)
        return
    ents = [cfg["brand"]] + cfg.get("competitors", [])
    meta = [
        (e["name"], norm(e.get("domain", "")), [e["name"], *e.get("aliases", [])]) for e in ents
    ]

    base = {"language_code": "en", "depth": 20}
    if args.lat is not None and args.lng is not None:
        base["location_coordinate"] = f"{args.lat},{args.lng},{args.zoom}z"
        where = base["location_coordinate"]
    else:
        where = args.location or cfg.get("serp_location") or "United States"
        base["location_name"] = where

    print(
        f"local-pack scan: {len(keywords)} keywords x ${MAPS_RATE_USD}/request "
        f"= est ${len(keywords) * MAPS_RATE_USD:.4f} [{where}]",
        file=sys.stderr,
    )
    out = {"location": where, "keywords": {}}
    for kw in keywords:
        try:
            result = post(MAPS_LIVE, [{**base, "keyword": kw}], auth)
            items = (result[0].get("items") if result else []) or []
        except Exception as e:  # noqa: BLE001
            if "40102" in str(e):  # No Search Results = an empty pack, which is data
                items = []
            else:
                out["keywords"][kw] = {"error": str(e)}
                print(f"  ! '{kw}' -> {e}", file=sys.stderr)
                continue
        positions, leaders = maps_positions(items, meta)
        out["keywords"][kw] = {"positions": positions, "leaders": leaders}
        shown = ", ".join(f"{n}:{'#' + str(p) if p else '—'}" for n, p in positions.items())
        lead = leaders[0] if leaders else None
        lead_s = f" | #1: {lead['title']} ({lead['reviews']} reviews)" if lead else ""
        print(f"  OK '{kw}' -> {shown}{lead_s}")
    _write("maps.json", out)


def _write(fname, obj):
    dest = Path(".tmp/dataforseo") / fname
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"-> {dest}")


def main():
    ap = argparse.ArgumentParser(
        description="Backlinks / domain authority / SERP rank via DataForSEO."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backlinks", help="Domain rank + backlink/referring-domain counts.")
    b.add_argument("domains", nargs="+")

    s = sub.add_parser("serp", help="Organic SERP rank for the config's seo_keywords.")
    s.add_argument("config")
    s.add_argument("--location", default=None, help='e.g. "Nashville,Tennessee,United States"')

    m = sub.add_parser("maps", help="Local-pack (Google Maps) rank for the config's seo_keywords.")
    m.add_argument("config")
    m.add_argument("--location", default=None, help="Overrides config serp_location.")
    m.add_argument("--lat", type=float, default=None, help="Pin the query to coordinates instead.")
    m.add_argument("--lng", type=float, default=None)
    m.add_argument("--zoom", type=int, default=13, help="Maps zoom for --lat/--lng (default 13).")

    args = ap.parse_args()
    auth = creds()
    if not auth:
        print(
            "ERROR: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set in .env. "
            "Get them at https://dataforseo.com/. Skipping.",
            file=sys.stderr,
        )
        return 2

    print("note: DataForSEO calls cost credits — running deliberately.", file=sys.stderr)
    if args.cmd == "backlinks":
        cmd_backlinks(args, auth)
    elif args.cmd == "serp":
        cmd_serp(args, auth)
    elif args.cmd == "maps":
        cmd_maps(args, auth)


if __name__ == "__main__":
    sys.exit(main())
