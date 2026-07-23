#!/usr/bin/env python3
"""
gbp_diff.py — before-vs-after Google Business Profile audit diff (DESIGN-gbp-prospect-audit.md).

A pure reducer over data/leads.sqlite, in the spirit of score_report.py / grid_diff.py: no network,
no spend, re-runnable at will. Compares a business's baseline audit (the sale-time "before", which
is the earliest completed baseline/prospect snapshot) against its latest audit, and produces the
"what we fixed on your profile" numbers for the monthly Steel & Amber report.

  python tools/gbp_diff.py --place-id ChIJ...                          # auto: baseline vs latest
  python tools/gbp_diff.py --place-id ChIJ... --baseline-id 1 --current-id 9
  python tools/gbp_diff.py --place-id ChIJ... --md output/acme-gbp-diff.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/ on path
from lib.common import slug

DB_PATH = Path("data/leads.sqlite")

# (field, label, higher_is_better). Neglect score is handled separately (lower is better).
_COUNT_FIELDS = [
    ("rating_votes", "Reviews", True),
    ("total_photos", "Photos", True),
    ("additional_categories_count", "Secondary categories", True),
    ("attr_available_count", "Attributes set", True),
    ("post_count", "Recent posts", True),
]


def _delta(cur, base):
    if cur is None or base is None:
        return None
    return round(cur - base, 2)


def diff_audits(baseline: dict, current: dict) -> dict:
    """Pure function over two audit-row dicts. Positive changes = improvement."""
    changes = []
    for field, label, _hib in _COUNT_FIELDS:
        b, c = baseline.get(field), current.get(field)
        changes.append(
            {"field": field, "label": label, "baseline": b, "current": c, "change": _delta(c, b)}
        )

    # Booleans that flip from missing to present. Claim status is deliberately excluded:
    # DataForSEO's is_claimed is an unreliable inference and must never be stated as fact in
    # a customer-facing report (SEC-D, ARCHITECTURE.md section 9). It stays raw-only in the DB.
    flips = []
    for field, label, good in [
        ("has_hours", "Hours set", 1),
        ("has_description", "Description added", 1),
    ]:
        b, c = baseline.get(field), current.get(field)
        if b != c and c == good:
            flips.append(label)

    ns_b, ns_c = baseline.get("neglect_score"), current.get("neglect_score")
    # neglect: lower is better, so improvement = baseline - current
    neglect = {"baseline": ns_b, "current": ns_c, "improvement": _delta(ns_b, ns_c)}

    def sig_set(row):
        try:
            return set(json.loads(row.get("signals_json") or "{}").keys())
        except ValueError, TypeError:
            return set()

    # neglect signals no longer firing; 'unclaimed' is excluded (SEC-D: claim status is an
    # unreliable inference, never surfaced in a client report even as a resolved signal).
    resolved = sorted(s for s in (sig_set(baseline) - sig_set(current)) if s != "unclaimed")

    return {
        "place_id": current.get("place_id"),
        "baseline_id": baseline.get("id"),
        "current_id": current.get("id"),
        "baseline_ts": baseline.get("audited_ts"),
        "current_ts": current.get("audited_ts"),
        "neglect": neglect,
        "flips": flips,
        "resolved_signals": resolved,
        "changes": changes,
    }


# --------------------------------------------------------------------------- #
# Markdown section for the report (agent writes the narrative around it)        #
# --------------------------------------------------------------------------- #
def _fmt(v, plus=False):
    if v is None:
        return "—"
    return f"{v:+.0f}" if plus else f"{v:.0f}"


def render_markdown(s: dict) -> str:
    n = s["neglect"]
    L = [
        "### Google Business Profile: since signup",
        f"*Baseline {s['baseline_ts']} to current {s['current_ts']}*\n",
        "| Signal | Before | After | Change |",
        "|---|---|---|---|",
        f"| Profile health (lower is better) | {_fmt(n['baseline'])} | {_fmt(n['current'])} | "
        f"{_fmt(n['improvement'], plus=True)} better |",
    ]
    for c in s["changes"]:
        L.append(
            f"| {c['label']} | {_fmt(c['baseline'])} | {_fmt(c['current'])} | "
            f"{_fmt(c['change'], plus=True)} |"
        )
    if s["flips"]:
        L.append("\n**Now fixed:** " + ", ".join(s["flips"]) + ".")
    if s["resolved_signals"]:
        L.append("**Neglect signals cleared:** " + ", ".join(s["resolved_signals"]) + ".")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- #
# DB wrapper + CLI                                                             #
# --------------------------------------------------------------------------- #
def diff_place(
    conn, place_id: str, baseline_id: Optional[int] = None, current_id: Optional[int] = None
) -> dict:
    import leads_db_gbp as gdb

    b_id = baseline_id or gdb.baseline_audit_id(conn, place_id)
    if b_id is None:
        raise SystemExit(f"No completed audit for {place_id}. Run gbp_audit.py first.")
    c_id = current_id or gdb.latest_audit_id(conn, place_id, exclude_id=b_id)
    if c_id is None or c_id == b_id:
        raise SystemExit(f"No later audit to compare against the baseline for {place_id}.")
    return diff_audits(gdb.get_audit(conn, b_id), gdb.get_audit(conn, c_id))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Before-vs-after GBP audit diff (pure reducer, $0).")
    ap.add_argument("--place-id", required=True)
    ap.add_argument("--baseline-id", type=int)
    ap.add_argument("--current-id", type=int)
    ap.add_argument("--out", type=Path, help="Write the JSON summary here (default .tmp/gbp/)")
    ap.add_argument("--md", type=Path, help="Also write the markdown section here")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args(argv)

    import sqlite3

    conn = sqlite3.connect(args.db)
    summary = diff_place(conn, args.place_id, args.baseline_id, args.current_id)

    out = args.out or Path(".tmp/gbp") / f"diff_{slug(args.place_id)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    md = render_markdown(summary)
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(md, encoding="utf-8")
    print(md)
    print(f"[json -> {out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
