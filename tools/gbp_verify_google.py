#!/usr/bin/env python3
"""
gbp_verify_google.py — cross-check DataForSEO GBP audit fields against Google Places (New).

Motivation: DataForSEO's `is_claimed` proved unreliable (false on 249-review businesses).
This asks "how much of the rest is wrong?" by treating Google Places Details as truth and
diffing each comparable field we stored in gbp_audits. Google Places does NOT expose claim
status, so this covers rating / review count / hours / category / photos only. Claim status
needs the Maps-page "Claim this business" scrape (separate).

Sampling is stratified: every is_claimed=0 record (the suspicious set) plus a spread of
claimed ones, so the reliability read isn't dominated by one stratum.

Cost: one Places Details call per sampled place_id (~$0.02). Prints an estimate; no silent
spend. Read-only against leads.sqlite.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
import sqlite3
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import load_env, utf8_stdout

utf8_stdout()

DETAILS_URL = "https://places.googleapis.com/v1/places/{pid}"
FIELDS = (
    "id,displayName,rating,userRatingCount,businessStatus,"
    "primaryTypeDisplayName,regularOpeningHours,photos"
)
EST_PER_CALL = 0.02


def google_details(pid, key):
    r = requests.get(
        DETAILS_URL.format(pid=pid),
        headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELDS},
        timeout=30,
    )
    if r.status_code == 404:
        return {"_status": "NOT_FOUND"}
    r.raise_for_status()
    return r.json()


def sample(conn, n_unclaimed, n_claimed):
    cur = conn.cursor()
    unc = cur.execute(
        """SELECT g.place_id FROM gbp_audits g JOIN businesses b ON b.place_id=g.place_id
        WHERE g.is_claimed=0 AND g.status='complete' AND b.rating IS NOT NULL
        ORDER BY b.review_count DESC LIMIT ?""",
        (n_unclaimed,),
    ).fetchall()
    cl = cur.execute(
        """SELECT g.place_id FROM gbp_audits g JOIN businesses b ON b.place_id=g.place_id
        WHERE g.is_claimed=1 AND g.status='complete' AND b.rating IS NOT NULL
        AND b.review_count >= 5 ORDER BY b.place_id LIMIT ?""",
        (n_claimed,),
    ).fetchall()
    return [r[0] for r in unc], [r[0] for r in cl]


def stored(conn, pid):
    c = conn.cursor()
    g = c.execute(
        """SELECT is_claimed, rating_value, rating_votes, total_photos, has_hours, category
                     FROM gbp_audits WHERE place_id=? ORDER BY audited_ts DESC LIMIT 1""",
        (pid,),
    ).fetchone()
    b = c.execute(
        "SELECT name, rating, review_count FROM businesses WHERE place_id=?", (pid,)
    ).fetchone()
    return g, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unclaimed", type=int, default=15, help="sample size from is_claimed=0 set")
    ap.add_argument("--claimed", type=int, default=25, help="sample size from is_claimed=1 set")
    ap.add_argument("--db", default="data/leads.sqlite")
    ap.add_argument("--out", default=".tmp/gbp_google_diff.json")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation prompt")
    args = ap.parse_args()

    load_env()
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        print("ERROR: GOOGLE_PLACES_API_KEY not set in .env", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    unc, cl = sample(conn, args.unclaimed, args.claimed)
    pids = unc + cl
    est = len(pids) * EST_PER_CALL
    print(
        f"sampling {len(unc)} unclaimed + {len(cl)} claimed = {len(pids)} Places Details calls, est ${est:.2f}"
    )
    if not args.yes:
        print("re-run with --yes to spend.")
        return 0

    rows, fails = [], 0
    for i, pid in enumerate(pids, 1):
        g, b = stored(conn, pid)
        try:
            gg = google_details(pid, key)
        except Exception as ex:  # noqa: BLE001
            print(f"  ! {pid}: {ex}", file=sys.stderr)
            fails += 1
            continue
        if gg.get("_status") == "NOT_FOUND":
            print(f"  ? {pid}: Google 404")
            continue
        g_rating = gg.get("rating")
        g_votes = gg.get("userRatingCount")
        g_hours = 1 if gg.get("regularOpeningHours") else 0
        g_photos = len(
            gg.get("photos") or []
        )  # NOTE: Places New caps this list (~10); not a full count
        rows.append(
            {
                "place_id": pid,
                "name": (b[0] if b else None),
                "stored_claimed": g[0],
                "rating_ds": g[1],
                "rating_g": g_rating,
                "votes_ds": g[2],
                "votes_g": g_votes,
                "photos_ds": g[3],
                "photos_g": g_photos,
                "hours_ds": g[4],
                "hours_g": g_hours,
                "cat_ds": g[5],
                "cat_g": (gg.get("primaryTypeDisplayName") or {}).get("text"),
                "status_g": gg.get("businessStatus"),
            }
        )
        if i % 10 == 0:
            print(f"  {i}/{len(pids)}")
        time.sleep(0.05)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(rows, indent=2), encoding="utf-8")

    # ---- reliability summary (Google = truth) ----
    def close(a, b, tol):
        return a is not None and b is not None and abs(a - b) <= tol

    n = len(rows)
    rating_ok = sum(1 for r in rows if close(r["rating_ds"], r["rating_g"], 0.11))
    votes_close = sum(
        1 for r in rows if close(r["votes_ds"], r["votes_g"], max(2, 0.05 * (r["votes_g"] or 0)))
    )
    hours_ok = sum(1 for r in rows if r["hours_ds"] == r["hours_g"])
    votes_big = [
        r
        for r in rows
        if r["votes_ds"] is not None
        and r["votes_g"] is not None
        and abs(r["votes_ds"] - r["votes_g"]) > max(5, 0.20 * (r["votes_g"] or 0))
    ]
    print(f"\n=== RELIABILITY vs Google Places (n={n}) ===")
    print(f"  rating within 0.1 : {rating_ok}/{n} ({100 * rating_ok / n:.0f}%)")
    print(f"  review count within 5%: {votes_close}/{n} ({100 * votes_close / n:.0f}%)")
    print(f"  hours-present agrees : {hours_ok}/{n} ({100 * hours_ok / n:.0f}%)")
    print(f"  review count off >20%: {len(votes_big)} cases")
    for r in votes_big[:8]:
        print(f"     {r['name']}: DataForSEO {r['votes_ds']} vs Google {r['votes_g']}")
    print("  NOTE: photos_g is capped by Places New (~10 max); not a fair full-count comparison.")
    print("  NOTE: is_claimed is NOT verifiable via Places API; that needs the Maps-page scrape.")
    print(f"\n[wrote {args.out}] fails={fails}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
