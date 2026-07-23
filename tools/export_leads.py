#!/usr/bin/env python3
"""
export_leads.py - export the master store to the discover-JSON schema (HLD FR-11).

The national sweep writes into SQLite, but the rest of the pipeline
(normalize_prospects -> scrape_contacts -> audit_site -> hvac_report) was built to
consume the discover-JSON that places_discover.py emits. This shim is the seam that
keeps all of that working unchanged: it reads businesses (optionally filtered to a
region) and writes one discover-JSON file in exactly that schema.

It emits ONLY the public business-listing fields the downstream chain expects (SEC-2);
the free/paid enrichment lives in its own tables and is folded in later at the report
layer (FR-14/18), not here.

Usage:
    python tools/export_leads.py                                   # whole store -> .tmp/discover/<slug>.json
    python tools/export_leads.py --region "TN KY"                  # only those states/provinces
    python tools/export_leads.py --region TN --out .tmp/discover/tn.json
"""

import argparse
import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import leads_db

from lib.common import utf8_stdout

utf8_stdout()

DEFAULT_CONFIG = "targets/excavating-national.json"


def _json_list(blob):
    """Decode a stored JSON list column, tolerating NULL/garbage (-> [])."""
    try:
        v = json.loads(blob) if blob else []
        return v if isinstance(v, list) else []
    except ValueError, TypeError:
        return []


def row_to_company(r):
    """Map a businesses row to a discover-JSON company record (the schema
    places_discover.py emits, so normalize/scrape/audit/report run unchanged)."""
    return {
        "place_id": r["place_id"],
        "name": r["name"],
        "website": r["website"] or None,  # '' -> null, matching places_discover
        "phone": r["phone"],
        "address": r["address"],
        "rating": r["rating"],
        "review_count": r["review_count"],
        "no_website": bool(r["no_website"]),
        "maps_url": r["maps_url"],
        "found_via": _json_list(r["found_via_json"]),
        "types": _json_list(r["types_json"]),
        "primary_type": r["primary_type"],
        "relevance": r["relevance"],  # match / maybe (FR-12)
        "state_code": r["state_code"],  # carried through for FR-13 grouping
        "state_name": r["state_name"],
    }


def fetch_companies(conn, state_codes=None):
    """Businesses for the region (or all), most-reviewed first - the order
    places_discover.py used, so downstream output is stable."""
    sql = "SELECT * FROM businesses"
    params = []
    if state_codes:
        sql += " WHERE LOWER(state_code) IN (%s)" % ",".join("?" for _ in state_codes)
        params.extend(state_codes)
    sql += " ORDER BY COALESCE(review_count, 0) DESC, place_id"
    return [row_to_company(r) for r in conn.execute(sql, params).fetchall()]


def _area_label(conn, companies, state_codes, cfg_area):
    """A human area string for the export header. For a region filter, name the
    actual states covered; otherwise fall back to the config's area."""
    if not state_codes:
        return cfg_area
    names = sorted({c["state_name"] or c["state_code"] for c in companies if c["state_code"]})
    return ", ".join(names) if names else ", ".join(sorted(s.upper() for s in state_codes))


def parse_region(arg):
    s = (arg or "").strip().lower()
    if not s or s == "all":
        return None
    return {c.strip() for c in s.replace(",", " ").split() if c.strip()}


def run(args):
    conn = leads_db.connect(args.db)
    leads_db.init_db(conn)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))

    state_codes = parse_region(args.region)
    companies = fetch_companies(conn, state_codes)
    if not companies:
        print("No businesses match - nothing to export.")
        return 0

    out = {
        "area": _area_label(conn, companies, state_codes, cfg.get("area")),
        "slug": cfg.get("slug", "leads"),
        "vertical": cfg.get("vertical", "businesses"),
        "industry_schema": cfg.get("industry_schema", ["localbusiness"]),
        "source": "prospect_sweep master store (data/leads.sqlite) via export_leads.py",
        "query": None,  # the sweep used many per-cell queries; not a single string
        "towns": None,  # national: location came from the GeoNames seed, not a town list
        "count": len(companies),
        "companies": companies,
    }
    out_path = Path(args.out) if args.out else Path(".tmp/discover") / f"{out['slug']}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    no_web = sum(1 for c in companies if c["no_website"])
    states = len({c["state_code"] for c in companies if c["state_code"]})
    print(
        f"Exported {len(companies)} businesses ({no_web} no-website) across {states} "
        f"states/provinces -> {out_path}"
    )
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Export the master store to discover-JSON for the downstream pipeline (FR-11)."
    )
    ap.add_argument("--db", default=leads_db.DEFAULT_DB)
    ap.add_argument(
        "--region", default="all", help="'all' or state/province codes, e.g. \"TN KY\"."
    )
    ap.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="Trade config (for area/slug/vertical/industry_schema).",
    )
    ap.add_argument("--out", default=None, help="Output path (default .tmp/discover/<slug>.json).")
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
