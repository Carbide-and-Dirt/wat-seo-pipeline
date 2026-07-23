#!/usr/bin/env python3
"""
measure_shortlist.py - paid shortlist measurement (HLD FR-17, NFR-10).

The free pass (enrich_sites.py) scores how READY a site looks; this measures how it
ACTUALLY performs, for a small, deliberately-chosen shortlist of the best prospects:
  - real Google organic rank for a trade+city keyword   (DataForSEO SERP)        FR-17
  - domain authority + backlink counts                  (DataForSEO backlinks)   FR-17
  - live AI-engine citation/visibility                  (Perplexity)             FR-17

These are PAID APIs, so this tool is built cost-first (mirrors the sweep's FR-5/NFR-2):
  * it ALWAYS prints a dry-run cost estimate; `--dry-run` stops there and spends $0;
  * a live run REQUIRES a hard `--budget` and stops before the lead that would cross it;
  * each lead's est_cost and a measured-at timestamp are stored (provenance); the pass
    is resumable (an already-measured lead is skipped unless --refresh).

The paid APIs are reached through an injected `clients` object, so the whole flow is
unit-tested with a fake client - no network, no spend. Reuses dataforseo.py and
check_ai_visibility.py primitives; nothing is re-implemented.

Usage:
    python tools/measure_shortlist.py --top 25 --dry-run                 # estimate only ($0)
    python tools/measure_shortlist.py --top 25 --budget 5.00            # live, capped at $5
    python tools/measure_shortlist.py --region TN --top 10 --dry-run
    python tools/measure_shortlist.py --place-ids ChIJ...,ChIJ... --budget 2 --dry-run
    python tools/measure_shortlist.py --summary
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import check_ai_visibility
import dataforseo
import leads_db
import normalize_prospects  # reuse parse_location to derive the lead's city

from lib.common import utf8_stdout

utf8_stdout()

DEFAULT_CONFIG = "targets/excavating-national.json"

# Per-call USD estimates (DataForSEO live + Perplexity sonar). Config 'paid.costs'
# overrides these; they drive BOTH the dry-run projection and the stored est_cost.
DEFAULT_COSTS = {"serp": 0.003, "backlinks": 0.02, "perplexity": 0.005}
DEFAULT_PAID = {
    "trade_term": "excavating contractor",
    "serp_keyword_template": "{trade} {city}",
    "ai_query_template": "Who are the best {trade} companies in {city}, {state}?",
    "perplexity_model": "sonar",
    "ai_runs": 1,
}


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_paid_config(cfg):
    """Merge a config's optional 'paid' block over the defaults (DRY: one source)."""
    paid = dict(DEFAULT_PAID)
    costs = dict(DEFAULT_COSTS)
    block = cfg.get("paid", {}) or {}
    costs.update(block.get("costs", {}) or {})
    for k in DEFAULT_PAID:
        if k in block:
            paid[k] = block[k]
    paid["trade_term"] = block.get("trade_term") or cfg.get("vertical") or paid["trade_term"]
    return paid, costs


def per_lead_cost(costs, paid):
    """Deterministic cost of measuring one lead: 1 SERP + 1 backlinks + ai_runs * AI."""
    return costs["serp"] + costs["backlinks"] + int(paid["ai_runs"]) * costs["perplexity"]


def estimate(n, costs, paid):
    """Dry-run projection (NFR-10). +/-20% band covers token-variable Perplexity cost."""
    pl = per_lead_cost(costs, paid)
    return {
        "leads": n,
        "per_lead": round(pl, 4),
        "total": round(n * pl, 2),
        "low": round(n * pl * 0.8, 2),
        "high": round(n * pl * 1.2, 2),
        "breakdown": {
            "serp": round(n * costs["serp"], 2),
            "backlinks": round(n * costs["backlinks"], 2),
            "perplexity": round(n * int(paid["ai_runs"]) * costs["perplexity"], 2),
        },
    }


def _city_state(lead):
    """Derive (City, State, Country) for the keyword + DataForSEO location string."""
    city, _ = normalize_prospects.parse_location(
        lead["address"] if "address" in lead.keys() else None
    )
    city = (city or "").title()
    state = lead["state_name"] or lead["state_code"] or ""
    country = lead["country"] or "United States"
    return city, state, country


def best_serp_rank(items, domain):
    """Best (lowest) absolute organic position whose domain matches the lead's, dot-
    boundary so 'climb.com' != 'myclimb.com'. None = not in the fetched depth."""
    best = None
    for it in items:
        itd = (it.get("domain") or "").lower().removeprefix("www.")
        if domain and (itd == domain or itd.endswith("." + domain)):
            r = it.get("rank_absolute")
            if r and (best is None or r < best):
                best = r
    return best


def measure_lead(lead, clients, costs, paid):
    """Measure ONE lead via the injected paid clients. Returns a site_rankings record
    (incl. the est_cost actually incurred). No DB writes - caller persists."""
    domain = dataforseo.norm(lead["website"])
    city, state, country = _city_state(lead)
    trade = paid["trade_term"]
    loc_city = city or state
    keyword = paid["serp_keyword_template"].format(trade=trade, city=loc_city, state=state).strip()
    location = ",".join(p for p in (city, state, country) if p)

    spent = 0.0
    items = clients.serp(keyword, location)
    spent += costs["serp"]
    serp_rank = best_serp_rank(items, domain)

    bl = clients.backlinks(domain) or {}
    spent += costs["backlinks"]

    ai_query = paid["ai_query_template"].format(trade=trade, city=loc_city, state=state)
    mentioned = cited = False
    for _ in range(int(paid["ai_runs"])):
        content, citations = clients.ai(ai_query)
        spent += costs["perplexity"]
        mentioned = mentioned or check_ai_visibility.mentioned(content, [lead["name"]])
        cited = cited or check_ai_visibility.cited(citations, domain)

    return {
        "place_id": lead["place_id"],
        "serp_rank": serp_rank,
        "serp_keyword": keyword,
        "domain_authority": bl.get("rank"),
        "backlinks": bl.get("backlinks"),
        "ai_mentioned": 1 if mentioned else 0,
        "ai_cited": 1 if cited else 0,
        "ai_engine": f"perplexity:{paid['perplexity_model']}",
        "est_cost": round(spent, 4),
    }


def run_measurements(conn, leads, clients, costs, paid, budget, now):
    """Measure each lead under a HARD budget cap (NFR-10): stop BEFORE the lead whose
    cost would cross the ceiling. Upserts + commits per lead (resumable). Returns
    (measured_count, spent, stopped_for_budget)."""
    pl = per_lead_cost(costs, paid)
    spent, measured = 0.0, 0
    stopped = False
    for lead in leads:
        if budget is not None and spent + pl > budget + 1e-9:
            stopped = True
            break
        rec = measure_lead(lead, clients, costs, paid)
        leads_db.upsert_ranking(conn, rec, now)
        conn.commit()
        spent += rec["est_cost"]
        measured += 1
    return measured, round(spent, 4), stopped


class PaidClients:
    """Live paid-API client (DataForSEO + Perplexity). Constructed only for a real
    run; injected so tests can substitute a fake. No spend happens until a method
    is called."""

    def __init__(self, auth, perplexity_key, model):
        self.auth = auth
        self.key = perplexity_key
        self.model = model

    def serp(self, keyword, location):
        result = dataforseo.post(
            "/v3/serp/google/organic/live/advanced",
            [{"keyword": keyword, "location_name": location, "language_code": "en", "depth": 20}],
            self.auth,
        )
        items = (result[0].get("items") if result else []) or []
        return [it for it in items if it.get("type") == "organic"]

    def backlinks(self, domain):
        result = dataforseo.post(
            "/v3/backlinks/summary/live",
            [{"target": domain, "backlinks_status_type": "live"}],
            self.auth,
        )
        return result[0] if result else {}

    def ai(self, query):
        return check_ai_visibility.ask_perplexity(query, self.model, self.key)


def parse_region(arg):
    s = (arg or "").strip().lower()
    if not s or s == "all":
        return None
    return {c.strip() for c in s.replace(",", " ").split() if c.strip()}


def _print_estimate(est, paid, costs):
    print(
        f"Dry-run cost estimate for {est['leads']} leads "
        f"(per lead: SERP ${costs['serp']} + backlinks ${costs['backlinks']} + "
        f"{paid['ai_runs']}xAI ${costs['perplexity']} = ${est['per_lead']}):"
    )
    print(f"   SERP            ${est['breakdown']['serp']:.2f}")
    print(f"   backlinks       ${est['breakdown']['backlinks']:.2f}")
    print(f"   AI (Perplexity) ${est['breakdown']['perplexity']:.2f}")
    print(
        f"   EXPECTED TOTAL  ${est['total']:.2f}   (range ${est['low']:.2f} - ${est['high']:.2f})"
    )


def run(args):
    conn = leads_db.connect(args.db)
    leads_db.init_db(conn)

    if args.summary:
        s = leads_db.ranking_status(conn)
        print(f"{args.db}: {s['measured']} leads measured; est ${s['spent']} spent (recorded).")
        return 0

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    paid, costs = load_paid_config(cfg)
    if args.ai_runs is not None:
        paid["ai_runs"] = args.ai_runs

    place_ids = (
        [p.strip() for p in args.place_ids.replace(",", " ").split()] if args.place_ids else None
    )
    leads = leads_db.shortlist_candidates(
        conn,
        state_codes=parse_region(args.region),
        place_ids=place_ids,
        only_unmeasured=not args.refresh,
    )
    if args.top and not place_ids:
        leads = leads[: args.top]
    if not leads:
        print("Shortlist is empty (nothing matches, or all already measured - use --refresh).")
        return 0

    est = estimate(len(leads), costs, paid)
    _print_estimate(est, paid, costs)

    if args.dry_run:
        print("\n--dry-run: no API calls made, $0 spent. Re-run with --budget to measure for real.")
        return 0

    # --- live path (spends money) ---
    if args.budget is None:
        print(
            "\nREFUSING to run live without a hard cap. Pass --budget <dollars> "
            "(or --dry-run to just estimate).",
            file=sys.stderr,
        )
        return 2
    if est["high"] > args.budget:
        print(
            f"\nNote: the high estimate (${est['high']}) exceeds --budget ${args.budget:.2f}; "
            f"the run will stop early when the cap is reached."
        )

    auth = dataforseo.creds()
    check_ai_visibility.load_env()
    import os

    pkey = os.environ.get("PERPLEXITY_API_KEY")
    missing = [
        n for n, v in (("DATAFORSEO_LOGIN/PASSWORD", auth), ("PERPLEXITY_API_KEY", pkey)) if not v
    ]
    if missing:
        print(
            f"\nCannot run live: missing credential(s) in .env: {', '.join(missing)}. "
            f"Add them (DataForSEO + Perplexity) or use --dry-run.",
            file=sys.stderr,
        )
        return 2

    clients = PaidClients(auth, pkey, paid["perplexity_model"])
    print(f"\nMeasuring up to {len(leads)} leads, hard cap ${args.budget:.2f}...")
    measured, spent, stopped = run_measurements(
        conn, leads, clients, costs, paid, args.budget, _now()
    )
    print(
        f"\nDone. Measured {measured} leads; est ${spent} spent"
        + (f" (stopped early at the ${args.budget:.2f} cap)." if stopped else ".")
    )
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Paid shortlist measurement: real rank / authority / AI citations (FR-17)."
    )
    ap.add_argument("--db", default=leads_db.DEFAULT_DB)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument(
        "--region",
        default="all",
        help="'all' or state/province codes for the opportunity shortlist.",
    )
    ap.add_argument(
        "--top",
        type=int,
        default=25,
        help="Top-N by opportunity (readiness) to measure (default 25).",
    )
    ap.add_argument(
        "--place-ids",
        default=None,
        help="Explicit comma/space list of place_ids (overrides --top/--region).",
    )
    ap.add_argument(
        "--budget",
        type=float,
        default=None,
        help="HARD dollar cap for a live run (required unless --dry-run).",
    )
    ap.add_argument("--ai-runs", type=int, default=None, help="Override Perplexity runs per lead.")
    ap.add_argument(
        "--refresh", action="store_true", help="Re-measure leads already in site_rankings."
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the cost estimate and exit ($0, no API calls).",
    )
    ap.add_argument(
        "--summary", action="store_true", help="Print how many leads are measured and exit."
    )
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
