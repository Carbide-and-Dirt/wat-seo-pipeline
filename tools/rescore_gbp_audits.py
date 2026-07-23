#!/usr/bin/env python3
"""
rescore_gbp_audits.py — recompute neglect_score + signals_json for every completed GBP audit
snapshot from its STORED fields, using the current NEGLECT_WEIGHTS (gbp_audit.py).

Why this exists: neglect_score and signals_json are DERIVED columns, not measurements. When
the scoring weights change (2026-07-07: the unreliable is_claimed 'unclaimed' weight was set
to 0 — see ARCHITECTURE.md section 9 / SEC-D), the append-only measurement rows are still
correct but their derived score is stale. This re-derives the score in place from the same
stored fields; it never touches a measured field, so the before/after proof store (moat #3)
is preserved. Reversible (re-run under any weighting) and idempotent (a second --apply is a
no-op). $0 — no network, no spend.

  python tools/rescore_gbp_audits.py            # dry-run: report what would change
  python tools/rescore_gbp_audits.py --apply    # rewrite neglect_score/signals_json in place
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/ on path
import gbp_audit as ga

DB_PATH = Path("data/leads.sqlite")

# neglect_score inputs, all stored columns on gbp_audits (see leads_db_gbp.py schema).
_SCORE_FIELDS = (
    "is_claimed",
    "days_since_post",
    "post_count",
    "rating_votes",
    "total_photos",
    "additional_categories_count",
    "has_hours",
    "attr_available_count",
    "has_description",
)


def recompute(row) -> tuple[float, str]:
    """(new_score, new_signals_json) for one audit row, derived from its stored fields only."""
    score, signals = ga.neglect_score({k: row[k] for k in _SCORE_FIELDS})
    return score, json.dumps(signals)


def rescore(conn, *, apply: bool, log=print) -> dict:
    """Recompute derived scores for all completed audits. Only rows whose score or signal
    set actually moved are written. Returns a summary."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, neglect_score, signals_json, "
        + ", ".join(_SCORE_FIELDS)
        + " FROM gbp_audits WHERE status='complete'"
    ).fetchall()

    updates = []
    for r in rows:
        new_score, new_sig = recompute(r)
        moved = new_score != r["neglect_score"] or json.loads(new_sig) != json.loads(
            r["signals_json"] or "{}"
        )
        if moved:
            updates.append((new_score, new_sig, r["id"], r["neglect_score"]))

    log(
        f"{len(rows)} completed audits; {len(updates)} would change"
        f"{' — APPLYING' if apply else ' (dry-run)'}"
    )
    for new_score, _sig, aid, old_score in updates[:10]:
        log(f"  audit {aid}: neglect {old_score} -> {new_score}")
    if len(updates) > 10:
        log(f"  ... and {len(updates) - 10} more")

    if apply and updates:
        conn.executemany(
            "UPDATE gbp_audits SET neglect_score=?, signals_json=? WHERE id=?",
            [(u[0], u[1], u[2]) for u in updates],
        )
        conn.commit()
        log(f"applied {len(updates)} updates.")

    return {"completed": len(rows), "changed": len(updates), "applied": bool(apply and updates)}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Recompute GBP neglect scores from stored fields ($0)."
    )
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    out = rescore(conn, apply=args.apply)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
