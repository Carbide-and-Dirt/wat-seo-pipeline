#!/usr/bin/env python3
"""
enrich_sites.py - free SEO/AEO-readiness + agency fingerprint pass (HLD FR-15, FR-16).

For every lead in the master store that HAS a website, fetch its homepage once and
record:
  - SEO health        (HTTPS, mobile viewport, title/meta lengths, content depth)   FR-15
  - AEO/GEO readiness (schema.org JSON-LD, llms.txt, AI-crawler blocking)            FR-15
  - a readiness score + gap list (reuses hvac_report.opportunity)                    FR-15
  - a marketing-agency / tech fingerprint -> DIY / self-managed / agency-managed     FR-16

No paid API calls (NFR-9): cost is HTTP/time only, bounded by --workers, and resumable
(an already-enriched lead is skipped unless --refresh). Reuses audit_site.py for fetch/
parse and site_fingerprint.py for the agency inference - nothing is rebuilt here.

Usage:
    python tools/enrich_sites.py                         # enrich every lead with a website
    python tools/enrich_sites.py --region "TN KY"        # only those states/provinces
    python tools/enrich_sites.py --limit 20              # most-reviewed 20 first (good for a smoke test)
    python tools/enrich_sites.py --refresh               # re-enrich already-done leads
    python tools/enrich_sites.py --summary               # just print coverage, no fetching
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import audit_site
import hvac_report
import leads_db
import site_fingerprint

from lib.common import utf8_stdout

utf8_stdout()

DEFAULT_CONFIG = "targets/excavating-national.json"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _origin(website):
    """Normalize a lead's website to its homepage origin (scheme://host/)."""
    url = website.strip()
    if "//" not in url:
        url = "https://" + url
    p = urlparse(url)
    scheme = p.scheme or "https"
    return f"{scheme}://{p.netloc}/", p.netloc


def _bool_int(v):
    return 1 if v else 0


def enrich_origin(origin, host, local_schema, vertical, firecrawl="off"):
    """Fetch + audit + fingerprint ONE homepage. Returns a place_id-free enrichment
    record the caller fans out to every lead that shares this website (a multi-location
    brand's homepage is fetched once, not once per location). No DB writes - runs in a
    worker thread."""
    rec = {"fetched_url": origin}

    # Skip social/directory hosts without a fetch - they aren't real sites to score.
    pre_status = hvac_report.classify_site(host, None, no_website=0)[0]
    if pre_status in ("social_only", "directory"):
        fp = site_fingerprint.fingerprint(None, host=host, reachable=False)
        return _record(
            rec,
            hp=None,
            site=None,
            site_status=pre_status,
            fp=fp,
            local_schema=local_schema,
            vertical=vertical,
            via="skipped",
        )

    final_url, status, html, err, via = audit_site.fetch(origin, render=False, firecrawl=firecrawl)
    hp = audit_site.parse_page(final_url, status, html) if (html and not err) else None
    site_status = hvac_report.classify_site(
        host, hp or {"error": err, "status": status}, no_website=0
    )[0]

    # Site-level signals (robots/sitemap/llms + AI-bot blocking) only matter for a live site.
    site = audit_site.audit_site_level(origin) if site_status == "live" else None
    reachable = site_status in ("live", "blocked", "parked", "dead") and bool(html)
    fp = site_fingerprint.fingerprint(
        html if reachable else None, host=host, reachable=(site_status == "live")
    )
    rec["fetched_url"] = final_url or origin
    rec["http_status"] = status
    return _record(
        rec,
        hp=hp,
        site=site,
        site_status=site_status,
        fp=fp,
        local_schema=local_schema,
        vertical=vertical,
        via=via,
    )


def _record(rec, hp, site, site_status, fp, local_schema, vertical, via):
    """Assemble a full site_enrichment row from the parsed page, site signals, and
    fingerprint. Only a 'live' page gets a readiness score (reuses opportunity())."""
    score, gaps = (None, [])
    if site_status == "live" and hp:
        score, gaps = hvac_report.opportunity(hp, site or {}, local_schema, vertical)
    elif site_status in ("dead", "parked", "social_only", "directory", "unreachable"):
        gaps = [f"{site_status.replace('_', ' ')} - no usable website (build opportunity)"]

    schema_types = (hp or {}).get("schema_types") or []
    flags = (hp or {}).get("schema_flags") or {}
    ai_blocked = []
    if site:
        ai_blocked = [
            b for b, v in site.get("robots_txt", {}).get("ai_bots", {}).items() if v == "blocked"
        ]
    llms = bool(site and site.get("llms_txt", {}).get("exists"))

    rec.update(
        {
            "reachable": _bool_int(site_status == "live"),
            "site_status": site_status,
            "https": _bool_int((hp or {}).get("https")),
            "mobile_viewport": _bool_int((hp or {}).get("viewport")),
            "title_len": (hp or {}).get("title_length"),
            "meta_desc_len": (hp or {}).get("meta_description_length"),
            "word_count": (hp or {}).get("word_count"),
            "jsonld_present": _bool_int(bool(schema_types)),
            "schema_localbusiness": _bool_int(flags.get("LocalBusiness")),
            "schema_faq": _bool_int(flags.get("FAQPage")),
            "llms_txt": _bool_int(llms),
            "ai_bots_blocked": json.dumps(ai_blocked),
            "readiness_score": score,
            "seo_gaps_json": json.dumps(gaps),
            "builder": fp["builder"],
            "marketing_tags_json": json.dumps(fp["marketing_tags"]),
            "agency_credit": fp["agency_credit"],
            "google_ads": _bool_int(fp["google_ads"]),
            "mgmt_status": fp["mgmt_status"],
            "mgmt_confidence": fp["mgmt_confidence"],
            "mgmt_evidence_json": json.dumps(fp["mgmt_evidence"]),
            "fetched_via": via,
        }
    )
    return rec


def parse_region(arg):
    """'all'/'' -> None (every lead); else a lowercased set of state/province codes."""
    s = (arg or "").strip().lower()
    if not s or s == "all":
        return None
    return {c.strip() for c in s.replace(",", " ").split() if c.strip()}


def run(args):
    conn = leads_db.connect(args.db)
    leads_db.init_db(conn)  # ensure the enrichment tables exist

    if args.summary:
        s = leads_db.enrichment_status(conn)
        print(f"{args.db}: {s['enriched']}/{s['with_website']} leads-with-website enriched.")
        for k, v in s["by_mgmt"].items():
            print(f"   {k}: {v}")
        return 0

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    local_schema = tuple(s.lower() for s in cfg.get("industry_schema", ["localbusiness"]))
    vertical = cfg.get("vertical", "businesses")

    state_codes = parse_region(args.region)
    if args.requeue_failed:
        leads = leads_db.leads_to_reenrich(conn, state_codes=state_codes, limit=args.limit)
        if not leads:
            print("Nothing to requeue (no leads with a recoverable failed status).")
            return 0
    else:
        leads = leads_db.leads_to_enrich(
            conn, state_codes=state_codes, only_unenriched=not args.refresh, limit=args.limit
        )
        if not leads:
            print(
                "Nothing to enrich (all matching leads already enriched, or none have a website)."
            )
            return 0

    audit_site.load_env()  # FIRECRAWL_API_KEY, only used if --firecrawl auto/always

    # Group leads by homepage so a multi-location brand is fetched once (cost lever).
    groups = {}
    for lead in leads:
        origin, host = _origin(lead["website"])
        groups.setdefault((origin, host), []).append(lead)
    print(
        f"Enriching {len(leads)} leads ({len(groups)} unique sites) "
        f"(region={args.region or 'all'}, workers={args.workers}, firecrawl={args.firecrawl})..."
    )

    now = _now()
    done = {"live": 0, "no_site": 0, "error": 0}
    mgmt = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(enrich_origin, origin, host, local_schema, vertical, args.firecrawl): (
                origin,
                host,
            )
            for (origin, host) in groups
        }
        for i, fut in enumerate(as_completed(futs), start=1):
            key = futs[fut]
            members = groups[key]
            try:
                base = fut.result()
            except Exception as e:  # noqa: BLE001 - one site failing must not abort the pass (NFR-6 style)
                print(f"  ! {members[0]['name']} ({key[1]}): {type(e).__name__}: {e}")
                done["error"] += len(members)
                continue
            for lead in members:  # fan the one fetch out to every location sharing this site
                rec = dict(base)
                rec["place_id"] = lead["place_id"]
                leads_db.upsert_enrichment(conn, rec, now)
                mgmt[rec["mgmt_status"]] = mgmt.get(rec["mgmt_status"], 0) + 1
                done["live" if rec["site_status"] == "live" else "no_site"] += 1
            if i % 25 == 0:
                conn.commit()
                print(f"  ... {i}/{len(groups)} sites fetched")
    conn.commit()

    print(
        f"\nDone. {done['live']} live sites scored, {done['no_site']} no-usable-site, {done['error']} errors."
    )
    print("Management mix:")
    for k, v in sorted(mgmt.items(), key=lambda kv: -kv[1]):
        print(f"   {k}: {v}")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Free SEO/AEO-readiness + agency fingerprint pass over leads that have a website (FR-15/16)."
    )
    ap.add_argument("--db", default=leads_db.DEFAULT_DB)
    ap.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help="Trade config (for industry_schema + vertical labels).",
    )
    ap.add_argument(
        "--region", default="all", help="'all' or state/province codes, e.g. \"TN KY\"."
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="Cap the number of leads (most-reviewed first)."
    )
    ap.add_argument(
        "--refresh", action="store_true", help="Re-enrich leads already in site_enrichment."
    )
    ap.add_argument(
        "--requeue-failed",
        action="store_true",
        help="Re-fetch ONLY leads whose prior enrichment failed to read the site "
        "(blocked/unreachable/dead/parked). Pair with --firecrawl auto to recover them.",
    )
    ap.add_argument(
        "--workers", type=int, default=8, help="Concurrent homepage fetches (default 8)."
    )
    ap.add_argument(
        "--firecrawl",
        choices=("off", "auto", "always"),
        default="off",
        help="Firecrawl fallback for JS-blocked sites (needs FIRECRAWL_API_KEY; may cost). Default off = $0.",
    )
    ap.add_argument(
        "--summary", action="store_true", help="Print enrichment coverage and exit (no fetching)."
    )
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
