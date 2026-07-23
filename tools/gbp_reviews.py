#!/usr/bin/env python3
"""
gbp_reviews.py — fetch lowest-rated Google reviews for a cohort (Phase 2 review scan).

For the GBP-update pitch we want ICP businesses (claimed, 4+ stars, 20+ reviews) whose
reviews complain about follow-up / slow callbacks. This tool pulls each target's
LOWEST-rated reviews (sort_by=lowest_rating), where those complaints live, so a cheap
classifier can flag them.

ToS/PII posture: review TEXT is written to a TRANSIENT .tmp store only, never the durable
DB (the audit deliberately stores counts, not text). The durable output is just the flag a
downstream classifier produces.

Reuses gbp_audit's DataForSEO submit/collect (bulk task pattern; the reviews endpoint is
Standard-queue only and slow, so batch-submit then collect). Paid: --dry-run first; live
needs a hard --budget. Match/lookup by place_id.

  python tools/gbp_reviews.py --place-ids-file .tmp/gbp/review_targets.txt --dry-run
  python tools/gbp_reviews.py --place-ids-file .tmp/gbp/review_targets.txt --budget 1.50
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/ on path

DB_PATH = Path("data/leads.sqlite")
REVIEWS_ENDPOINT = "/v3/business_data/google/reviews"
# Official Business Data pricing (verified 2026-07-05): setup + per-10-reviews.
RATE_USD = {
    "standard": {"setup": 0.00075, "per10": 0.00075},
    "priority": {"setup": 0.0015, "per10": 0.0015},
}


def extract_reviews(items) -> list[dict]:
    """Pull (rating, text, when, author) from a reviews result. Field names confirmed against
    the DataForSEO reviews docs: review_text / rating.value / timestamp / profile_name."""
    out = []
    for it in items or []:
        rating = (it.get("rating") or {}).get("value")
        text = (it.get("review_text") or "").strip()
        if not text:
            continue  # ratings-only reviews carry no complaint text to read
        out.append(
            {
                "rating": rating,
                "text": text,
                "when": it.get("timestamp"),
                "author": it.get("profile_name"),
            }
        )
    return out


def estimate_cost(n: int, depth: int, priority: str) -> dict:
    rate = RATE_USD[priority]
    buckets = -(-depth // 10)  # charged per 10 reviews (ceil)
    per = rate["setup"] + buckets * rate["per10"]
    return {"targets": n, "depth": depth, "per_target_usd": round(per, 6), "usd": round(n * per, 4)}


def select_targets(conn, place_ids_file: str) -> list[dict]:
    ids = [
        ln.strip()
        for ln in Path(place_ids_file).read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _rev_cohort (place_id TEXT PRIMARY KEY)")
    conn.executemany("INSERT OR IGNORE INTO _rev_cohort VALUES (?)", [(i,) for i in ids])
    rows = conn.execute(
        "SELECT place_id, name, lat, lng FROM businesses "
        "WHERE place_id IN (SELECT place_id FROM _rev_cohort) AND lat IS NOT NULL AND lng IS NOT NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Fetch lowest-rated Google reviews for a cohort (transient .tmp)."
    )
    ap.add_argument("--place-ids-file", required=True)
    ap.add_argument(
        "--depth", type=int, default=20, help="Reviews per business (lowest-rated first)."
    )
    ap.add_argument("--priority", choices=["standard", "priority"], default="standard")
    ap.add_argument("--out-dir", default=".tmp/gbp/reviews")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--budget", type=float, help="HARD cap in USD; required for a live run.")
    ap.add_argument("--deadline", type=int, default=3600, help="Collect deadline in seconds.")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    leads = select_targets(conn, args.place_ids_file)
    if not leads:
        print("No targets (need place_ids with lat/lng).", file=sys.stderr)
        return 1

    est = estimate_cost(len(leads), args.depth, args.priority)
    print(
        f"targets={est['targets']} depth={est['depth']} priority={args.priority} "
        f"-> est ${est['usd']:.4f} (${est['per_target_usd']:.5f}/target)"
    )
    if args.dry_run:
        return 0
    if args.budget is None:
        raise SystemExit(
            "Refusing to run live without --budget. Add --budget <usd> or use --dry-run."
        )

    max_targets = int(args.budget / est["per_target_usd"])
    submit = leads[:max_targets]
    if len(submit) < len(leads):
        print(f"budget ${args.budget:.2f} caps submission to {len(submit)}/{len(leads)}")

    import gbp_audit

    print("note: DataForSEO reviews calls cost credits — running deliberately.", file=sys.stderr)
    client = gbp_audit.DataForSEOGbpClient()
    print(f"submitting {len(submit)} reviews tasks (depth {args.depth}, lowest-rating)...")
    submitted, _ = client.submit_batch(
        REVIEWS_ENDPOINT,
        submit,
        args.priority,
        extra={"depth": args.depth, "sort_by": "lowest_rating"},
    )
    print(f"submitted {len(submitted)}; collecting (deadline {args.deadline}s)...")
    results, pending = client.collect_batch(
        REVIEWS_ENDPOINT, submitted, deadline_s=args.deadline, log=print
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name_by = {ld["place_id"]: ld["name"] for ld in submit}
    written = with_negatives = 0
    for pid, res in results.items():
        revs = extract_reviews(res.get("items"))
        negs = [r for r in revs if r["rating"] is not None and r["rating"] <= 2]
        (out_dir / f"{pid}.json").write_text(
            json.dumps(
                {
                    "place_id": pid,
                    "name": name_by.get(pid),
                    "review_count": len(revs),
                    "negative_count": len(negs),
                    "reviews": revs,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        written += 1
        if negs:
            with_negatives += 1
    print(
        f"done: wrote {written} files ({with_negatives} with 1-2 star text), "
        f"{len(pending)} un-collected -> {out_dir}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
