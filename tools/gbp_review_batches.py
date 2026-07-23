#!/usr/bin/env python3
"""
gbp_review_batches.py — turn fetched reviews into classifier batches (review-scan step 2).

Reads the transient reviews pulled by gbp_reviews.py, keeps only in-scope trades (excavation +
septic + plumber; see gbp_trades.py) that HAVE negative (<=2 star) review text, and splits them
into batch JSONL files (one business per line) for the haiku follow-up-complaint classifiers.

  python tools/gbp_review_batches.py                       # defaults
  python tools/gbp_review_batches.py --per-batch 55 --reviews-dir .tmp/gbp/reviews

Next step: spawn one classifier subagent per batch (prompt template: workflows/review_scan.md),
then aggregate with gbp_pitch_list.py.
"""

from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gbp_trades

DB_PATH = Path("data/leads.sqlite")


def build_batches(reviews_dir: Path, batch_dir: Path, db_path: Path, per_batch: int) -> dict:
    conn = sqlite3.connect(db_path)
    category = {
        pid: cat
        for (pid, cat) in conn.execute(
            "SELECT place_id, category FROM gbp_audits WHERE status='complete'"
        )
    }

    batch_dir.mkdir(parents=True, exist_ok=True)
    for old in batch_dir.glob("batch_*.jsonl"):
        old.unlink()

    biz, skipped_trade, no_negs = [], 0, 0
    for f in sorted(glob.glob(str(reviews_dir / "*.json"))):
        d = json.loads(Path(f).read_text(encoding="utf-8"))
        if not gbp_trades.is_included(category.get(d["place_id"])):
            skipped_trade += 1
            continue
        negs = [
            {"rating": r["rating"], "when": r["when"], "text": r["text"]}
            for r in d["reviews"]
            if r["rating"] is not None and r["rating"] <= 2 and r["text"]
        ]
        if not negs:
            no_negs += 1
            continue
        biz.append({"place_id": d["place_id"], "name": d["name"], "negative_reviews": negs})

    for i in range(0, len(biz), per_batch):
        chunk = biz[i : i + per_batch]
        (batch_dir / f"batch_{i // per_batch:02d}.jsonl").write_text(
            "\n".join(json.dumps(b, ensure_ascii=False) for b in chunk), encoding="utf-8"
        )
    n_batches = -(-len(biz) // per_batch) if biz else 0
    return {
        "businesses": len(biz),
        "batches": n_batches,
        "total_negatives": sum(len(b["negative_reviews"]) for b in biz),
        "skipped_other_trade": skipped_trade,
        "skipped_no_negatives": no_negs,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Split fetched reviews into classifier batches (in-scope trades only)."
    )
    ap.add_argument("--reviews-dir", default=".tmp/gbp/reviews")
    ap.add_argument("--batch-dir", default=".tmp/gbp/batches")
    ap.add_argument("--per-batch", type=int, default=55)
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args(argv)

    s = build_batches(Path(args.reviews_dir), Path(args.batch_dir), Path(args.db), args.per_batch)
    print(
        f"in-scope businesses with negative text: {s['businesses']} "
        f"({s['total_negatives']} negative reviews) -> {s['batches']} batch files in {args.batch_dir}"
    )
    print(
        f"skipped: {s['skipped_other_trade']} other-trade, {s['skipped_no_negatives']} without negative text"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
