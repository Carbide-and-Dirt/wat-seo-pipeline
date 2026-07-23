#!/usr/bin/env python3
"""
prospect_sweep.py - national excavating/site-work lead discovery (HLD: prospect_sweep).

--dry-run is the COST ESTIMATOR (FR-5): project how many Places searches a sweep
would run and what they would cost, WITHOUT calling Google - price any region before
committing. Without --dry-run it runs the LIVE sweep (FR-3/4/6/8/9/10/12): density-
ordered Places queries under a HARD budget cap, deduped into the SQLite master store,
resumable and additive. A live run needs a cap (--budget and/or --max-requests) and
GOOGLE_PLACES_API_KEY; it spends real money, so confirm before running.

Usage:
    python tools/prospect_sweep.py --region "TN KY VA NC GA AL MS AR MO" --dry-run
    python tools/prospect_sweep.py --region all --dry-run --budget 100
    python tools/prospect_sweep.py --region "TN" --budget 50          # LIVE, capped at $50
    python tools/prospect_sweep.py --region "TN" --budget 50 --refresh
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cell_planner as cp
import leads_db
import places_discover as pd  # reuse FIELD_MASK, SEARCH_URL, relevance, load_env (DRY)

from lib.common import utf8_stdout

utf8_stdout()

DEFAULT_CONFIG = "targets/excavating-national.json"
DEFAULT_RATE_PER_1000 = 35.0  # ~Places (New) searchText w/ website+phone fields; --rate to override

# Requests per query once pagination is accounted for (each Places page = 1 request,
# up to 3 pages / 60 results). Most rural queries return <20 (1 page); metros page more.
PAGE_LOW, PAGE_EXPECTED, PAGE_HIGH = 1.0, 1.4, 2.2

MAX_PAGES = 3  # Places (New) caps a text query at ~60 results = 3 pages (FR-3)
# Backoff (seconds) used ONLY if a freshly issued page token is briefly rejected before
# Google propagates it. A ready token (the common case) waits zero - no fixed pre-sleep.
TOKEN_RETRY_WAITS = (0.4, 0.9, 1.8)


def queries_per_cell(config):
    """FR-12: total Places query phrases per cell (the main cost driver)."""
    buckets = config.get("trade_queries", [])
    total = sum(len(b.get("queries", [])) for b in buckets)
    breakdown = [(b.get("bucket", "?"), len(b.get("queries", []))) for b in buckets]
    return total, breakdown


def estimate(cells, q_per_cell, rate):
    """FR-5: project low / expected / high request counts and dollar cost.

    low      = one request per (cell, query), no metro subdivision (sparse case).
    expected = pagination + expected subdivision of big metros.
    high     = heavier pagination + worst-case subdivision.
    """
    exp_cells = sum(cp.expected_subdivision(c.population) for c in cells)
    high_cells = sum(cp.high_subdivision(c.population) for c in cells)
    req = {
        "low": len(cells) * q_per_cell * PAGE_LOW,
        "expected": exp_cells * q_per_cell * PAGE_EXPECTED,
        "high": high_cells * q_per_cell * PAGE_HIGH,
    }
    req = {k: int(v + 0.999) for k, v in req.items()}  # round up - never under-quote spend
    cost = {k: v / 1000.0 * rate for k, v in req.items()}
    return req, cost


def budget_coverage(cells, q_per_cell, rate, budget):
    """How far a dollar budget reaches, densest-first (cost-control-first, FR-1/FR-6).
    Returns (places_covered, population_floor_reached, spent_estimate)."""
    spent, covered, floor = 0.0, 0, None
    for c in cells:  # cells are already population-ordered by the planner
        cell_cost = (
            (cp.expected_subdivision(c.population) * q_per_cell * PAGE_EXPECTED) / 1000.0 * rate
        )
        if spent + cell_cost > budget:
            break
        spent += cell_cost
        covered += 1
        floor = c.population
    return covered, floor, spent


def record_dry_run(db_path, region_arg, budget, est_cost_expected):
    """Log the dry-run in the runs audit table (FR-7). Best-effort; never blocks output."""
    try:
        conn = leads_db.connect(db_path)
        leads_db.init_db(conn)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cur = conn.execute(
            "INSERT INTO runs(region, budget, mode, started_ts, ended_ts, requests_spent, est_cost) "
            "VALUES (?,?,?,?,?,?,?)",
            (region_arg, budget, "dry-run", now, now, 0, round(est_cost_expected, 2)),
        )
        conn.commit()
        run_id = cur.lastrowid
        conn.close()
        return run_id
    except Exception as e:  # noqa: BLE001 - auditing must not break the estimate
        print(f"  (note: could not record dry-run to {db_path}: {e})", file=sys.stderr)
        return None


def fmt_usd(x):
    return f"${x:,.2f}"


def run_dry(args):
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    q_per_cell, breakdown = queries_per_cell(config)
    if q_per_cell == 0:
        print(f"ERROR: {args.config} has no 'trade_queries'.", file=sys.stderr)
        return 1

    cells, region = cp.plan_region(args.region, args.seed)
    if not cells:
        print(
            f"No places matched region '{args.region}'. Check the state codes or run build_seed.py.",
            file=sys.stderr,
        )
        return 1

    req, cost = estimate(cells, q_per_cell, args.rate)
    bucket_str = ", ".join(f"{name} x{n}" for name, n in breakdown)

    print("DRY RUN - no Google calls, no spend (HLD FR-5)")
    print(f"  Region          : {args.region}  ({region['type']})")
    print(f"  Places (cells)  : {len(cells):,}   [{config.get('vertical', '')}]")
    print(f"  Queries / cell  : {q_per_cell}   ({bucket_str})")
    print(f"  Rate            : {fmt_usd(args.rate)} per 1,000 searches")
    print("  ---------------------------------------------------------------")
    print(f"  {'Scenario':<10} {'Searches':>12}   {'Est. cost':>12}")
    for k in ("low", "expected", "high"):
        print(f"  {k:<10} {req[k]:>12,}   {fmt_usd(cost[k]):>12}")
    print("  ---------------------------------------------------------------")

    if args.budget is not None:
        covered, floor, spent = budget_coverage(cells, q_per_cell, args.rate, args.budget)
        pct = (covered / len(cells) * 100) if cells else 0
        floor_str = f"{floor:,}" if floor is not None else "n/a"
        print(
            f"  Budget {fmt_usd(args.budget)}: covers ~{covered:,} of {len(cells):,} places "
            f"({pct:.0f}%, densest first), down to towns of ~{floor_str} people, "
            f"for ~{fmt_usd(spent)}."
        )
        if covered == 0:
            print("  (Budget too small for even the largest market at the expected rate.)")

    run_id = (
        None
        if args.no_record
        else record_dry_run(args.db, args.region, args.budget, cost["expected"])
    )
    if run_id:
        print(f"  Logged as dry-run #{run_id} in {args.db}.")
    print("\n  Note: 'expected' is the planning number; the live sweep (Phase 2) enforces a hard")
    print("  budget cap so actual spend never exceeds what you authorize. Trim query phrases in")
    print(f"  {args.config} or raise build_seed.py --min-pop to lower these figures.")
    return 0


# ----------------------------------------------------------------------------------
# Phase 2: live sweep (FR-3, FR-4, FR-6, FR-8, FR-9, FR-10, FR-12)
# ----------------------------------------------------------------------------------


class Budget:
    """Hard spend ceiling (FR-6/NFR-2). Checked BEFORE every request, so actual spend
    can never exceed the cap. Limit by dollars, by request count, or both."""

    def __init__(self, dollars=None, rate_per_1000=DEFAULT_RATE_PER_1000, max_requests=None):
        self.limit_dollars = dollars
        self.rate = rate_per_1000
        self.max_requests = max_requests
        self.requests = 0

    def cost(self):
        return self.requests / 1000.0 * self.rate

    def can_afford_one(self):
        if self.max_requests is not None and self.requests + 1 > self.max_requests:
            return False
        if (
            self.limit_dollars is not None
            and (self.requests + 1) / 1000.0 * self.rate > self.limit_dollars
        ):
            return False
        return True

    def charge(self):
        self.requests += 1


class GooglePlacesClient:
    """Real Places API (New) searchText client (FR-3). The sweep depends on the
    `.search(...)` shape, not this class, so tests inject a fake (no network/spend)."""

    def __init__(self, api_key):
        self.key = api_key

    def search(self, text_query, lat, lng, radius_m, page_token=None):
        """Return (places, next_page_token) for one biased search page.

        Pagination has no fixed pre-sleep: we send the next page immediately. A freshly
        issued token that Google hasn't propagated yet comes back as 400 - only then do
        we back off and retry it. A ready token (the common case) costs zero wait (FR-3)."""
        body = {
            "textQuery": text_query,
            "pageSize": 20,
            "locationBias": {
                "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(radius_m)}
            },
        }
        if page_token:  # New API: repeat textQuery/pageSize, only ADD pageToken
            body["pageToken"] = page_token
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self.key,
            "X-Goog-FieldMask": pd.FIELD_MASK,
        }
        r = requests.post(pd.SEARCH_URL, headers=headers, json=body, timeout=30)
        # A just-issued page token can momentarily 400 ("not ready"); back off and retry
        # that case only. A first-page 400 (no token) is a real error and is not retried.
        for wait in TOKEN_RETRY_WAITS:
            if not (page_token and r.status_code == 400):
                break
            time.sleep(wait)
            r = requests.post(pd.SEARCH_URL, headers=headers, json=body, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        return data.get("places", []), data.get("nextPageToken")


def _to_record(p, cell, bucket, rel):
    """Map a Places result + its cell into a businesses-table record (FR-7/FR-8).
    state_code is the cell's state as a proxy; Phase-3 normalize refines it from the
    address (FR-13). no_website is the headline lead signal."""
    loc = p.get("location") or {}
    website = p.get("websiteUri")
    types = p.get("types", [])
    return {
        "place_id": p.get("id"),
        "name": (p.get("displayName") or {}).get("text"),
        "address": p.get("formattedAddress"),
        "state_code": cell.state_code,
        "state_name": None,
        "country": cell.country,
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "phone": p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber"),
        "website": website,
        "no_website": 0 if website else 1,
        "rating": p.get("rating"),
        "review_count": p.get("userRatingCount"),
        "business_status": p.get("businessStatus"),
        "primary_type": (p.get("primaryTypeDisplayName") or {}).get("text"),
        "types": types,
        "types_json": json.dumps(types),
        "trade_bucket": bucket,
        "relevance": rel,
        "maps_url": p.get("googleMapsUri"),
        "found_via": [cell.place_name],
    }


def _run_query(client, budget, query, cell, max_pages=MAX_PAGES):
    """Paginate one search under the budget cap. Returns (places, saturated, budget_hit).
    saturated = more results existed than we pulled (hit the 60-cap), which triggers
    FR-4 subdivision."""
    places, token, pages = [], None, 0
    while pages < max_pages:
        if not budget.can_afford_one():
            return places, False, True
        budget.charge()
        batch, token = client.search(query, cell.lat, cell.lng, cell.radius_m, token)
        places.extend(batch)
        pages += 1
        if not token:
            break
    saturated = bool(token) or len(places) >= 60
    return places, saturated, False


def _filters(config):
    return (
        [k.lower() for k in config.get("type_keywords", [])],
        config.get("primary_types", []),
        tuple(config.get("adjacent_keywords", pd.DEFAULT_ADJACENT)),
    )


def _process(
    conn,
    client,
    budget,
    cell,
    bucket,
    queries,
    region,
    keywords,
    primary_types,
    adjacent,
    refresh,
    counters,
    now,
    depth,
    max_subdiv,
):
    """Sweep one (cell, trade bucket): run its phrases, filter, upsert, then subdivide
    if saturated. Returns 'ok' or 'budget' (cap reached - stop the whole run)."""
    swept_id = f"{cell.cell_id}#{bucket}"
    if not refresh and leads_db.cell_is_swept(conn, swept_id):
        counters["skipped"] += 1
        return "ok"

    saturated_any, found, hit_budget = False, 0, False
    for q in queries:
        places, saturated, budget_hit = _run_query(client, budget, q, cell)
        saturated_any = saturated_any or saturated
        for p in places:
            if not p.get("id"):
                continue
            name = (p.get("displayName") or {}).get("text")
            primary = (p.get("primaryTypeDisplayName") or {}).get("text")
            rel = pd.relevance(name, p.get("types", []), primary, keywords, primary_types, adjacent)
            if rel == "other":  # FR-12: drop unrelated results
                continue
            counters[
                leads_db.upsert_business(conn, _to_record(p, cell, bucket, rel), now, refresh)
            ] += 1
            found += 1
        if budget_hit:
            hit_budget = True
            break

    if hit_budget:
        # Partial sweep: do NOT mark the cell swept, so a resume re-runs it (dedup-safe).
        return "budget"

    leads_db.record_cell(conn, swept_id, cell, bucket, region, found, saturated_any, now)
    if saturated_any and depth < max_subdiv:  # FR-4
        for child in cp.subdivide(cell):
            if (
                _process(
                    conn,
                    client,
                    budget,
                    child,
                    bucket,
                    queries,
                    region,
                    keywords,
                    primary_types,
                    adjacent,
                    refresh,
                    counters,
                    now,
                    depth + 1,
                    max_subdiv,
                )
                == "budget"
            ):
                return "budget"
    return "ok"


def sweep(conn, client, cells, config, budget, region, refresh, now, max_subdiv=None):
    """Drive the density-ordered cells under the budget cap (FR-3/4/6/8/9/10/12).
    Checkpoints after every cell so a kill loses at most the in-flight cell (NFR-3)."""
    if max_subdiv is None:
        max_subdiv = cp.MAX_SUBDIV_DEPTH
    trade_queries = config["trade_queries"]
    keywords, primary_types, adjacent = _filters(config)
    counters = {"inserted": 0, "updated": 0, "skipped": 0, "cells": 0}
    stop = "complete"
    for cell in cells:
        if not budget.can_afford_one():
            stop = "budget"
            break
        for bq in cp.query_depth_for(cell.population, trade_queries):
            if (
                _process(
                    conn,
                    client,
                    budget,
                    cell,
                    bq["bucket"],
                    bq["queries"],
                    region,
                    keywords,
                    primary_types,
                    adjacent,
                    refresh,
                    counters,
                    now,
                    0,
                    max_subdiv,
                )
                == "budget"
            ):
                stop = "budget"
                break
        counters["cells"] += 1
        conn.commit()  # NFR-3 checkpoint
        if stop == "budget":
            break
    return counters, stop


def run_live(args, client=None):
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if not config.get("trade_queries"):
        print(f"ERROR: {args.config} has no 'trade_queries'.", file=sys.stderr)
        return 1
    if args.budget is None and args.max_requests is None:
        print(
            "Refusing an uncapped live run. Pass --budget DOLLARS (and/or --max-requests).",
            file=sys.stderr,
        )
        return 2

    pd.load_env()
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if client is None and not key:
        print(
            "ERROR: GOOGLE_PLACES_API_KEY not set in .env (needed for a live sweep).",
            file=sys.stderr,
        )
        return 2
    client = client or GooglePlacesClient(key)

    cells, _region_spec = cp.plan_region(args.region, args.seed)  # spec used only for planning
    if not cells:
        print(f"No places matched region '{args.region}'.", file=sys.stderr)
        return 1
    if not args.no_prune:
        before = len(cells)
        cells = cp.prune_overlapping(cells)
        print(f"  overlap-pruned {before:,} -> {len(cells):,} cells")

    budget = Budget(args.budget, args.rate, args.max_requests)
    conn = leads_db.connect(args.db)
    leads_db.init_db(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    mode = "refresh" if args.refresh else "live"
    run_id = leads_db.start_run(conn, args.region, args.budget, mode, now)
    cap = fmt_usd(args.budget) if args.budget is not None else f"{args.max_requests:,} requests"
    print(
        f"LIVE sweep #{run_id}: region '{args.region}', cap {cap}, mode {mode}. "
        f"Progress is checkpointed - Ctrl-C is safe."
    )

    try:
        counters, stop = sweep(
            conn,
            client,
            cells,
            config,
            budget,
            args.region,
            args.refresh,
            now,
            max_subdiv=getattr(args, "max_subdiv", None),
        )  # region label (str), not the spec dict
    except KeyboardInterrupt:
        counters, stop = {"inserted": 0, "updated": 0, "skipped": 0, "cells": 0}, "interrupted"
        print("\n  interrupted - finalizing run with progress so far.")

    leads_db.finish_run(
        conn,
        run_id,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        budget.requests,
        budget.cost(),
    )
    s = leads_db.status(conn)
    conn.close()
    print(f"\n  Stopped: {stop}.  Searches spent: {budget.requests:,} (~{fmt_usd(budget.cost())}).")
    print(
        f"  This run: +{counters['inserted']:,} new, {counters['updated']:,} updated, "
        f"{counters['skipped']:,} cells skipped (already swept)."
    )
    print(
        f"  Master store now: {s['businesses']:,} businesses "
        f"({s['no_website']:,} no-website) across {s['states']} states/provinces."
    )
    print(
        "  Next: export for the pipeline (Phase 3) or re-run to widen (additive skips swept cells)."
    )
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="National excavating lead discovery - estimator (FR-5) + live sweep (FR-3/4/6/8/9/10/12)."
    )
    ap.add_argument(
        "--region",
        required=True,
        help="'all', state/province codes ('TN KY VA'), or 'bbox:minlat,minlng,maxlat,maxlng'.",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="Project cost without calling Google (no spend)."
    )
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--seed", default=cp.DEFAULT_SEED)
    ap.add_argument(
        "--rate", type=float, default=DEFAULT_RATE_PER_1000, help="USD per 1,000 searches."
    )
    ap.add_argument(
        "--budget",
        type=float,
        default=None,
        help="Dollar cap. In --dry-run shows reach; in a live run it is the HARD spend ceiling (FR-6).",
    )
    ap.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="Alternative/extra hard cap by request count.",
    )
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Live: re-pull data for businesses already stored (FR-10).",
    )
    ap.add_argument(
        "--no-prune",
        action="store_true",
        help="Live: disable overlap-pruning of nearby small towns.",
    )
    ap.add_argument(
        "--max-subdiv",
        type=int,
        default=cp.MAX_SUBDIV_DEPTH,
        help="Live: subdivision depth for saturated metros (0=off, 1=default; higher=exhaustive but costly).",
    )
    ap.add_argument("--db", default=leads_db.DEFAULT_DB)
    ap.add_argument(
        "--no-record", action="store_true", help="Dry-run: do not log to the runs table."
    )
    args = ap.parse_args()

    return run_dry(args) if args.dry_run else run_live(args)


if __name__ == "__main__":
    sys.exit(main())
