#!/usr/bin/env python3
"""
grid_diff.py — baseline-vs-current geo-grid diff (ADR-LP-001 Addendum A).

A pure reducer over data/leads.sqlite, in the spirit of score_report.py: no network, no
spend, re-runnable at will. Compares a business's baseline scan (the "since you signed up"
anchor) against its latest scan and produces the numbers the monthly Steel & Amber report
needs — per-pin rank movement, per-keyword SoLV change, and the biggest movers.

  python tools/grid_diff.py --place-id ChIJ...                       # auto: baseline vs latest
  python tools/grid_diff.py --place-id ChIJ... --baseline-scan-id 1 --current-scan-id 9
  python tools/grid_diff.py --place-id ChIJ... --md output/acme-grid-diff.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/ on path
import geo_grid as gg  # reuse the SAME SoLV reducer so metrics are consistent
from lib.common import slug

DB_PATH = Path("data/leads.sqlite")

# Positive numbers always mean "better" in this module (moved up the results).
_GEO_KEYS = ("grid_rows", "grid_cols", "spacing_km", "center_lat", "center_lng", "zoom")


def _points_by_cell(points: list[dict]) -> dict:
    """(row, col, keyword) -> rank (int or None). A cell is present only if it was scanned."""
    return {(p["row"], p["col"], p["keyword"]): p["rank"] for p in points}


def _classify(base_rank, cur_rank):
    if base_rank is None and cur_rank is None:
        return "still_absent", None
    if base_rank is None and cur_rank is not None:
        return "gained", None
    if base_rank is not None and cur_rank is None:
        return "lost", None
    d = base_rank - cur_rank  # +ve = moved up
    return ("improved" if d > 0 else "declined" if d < 0 else "unchanged"), d


def _keyword_solv(points: list[dict], keyword: str, max_rank: int) -> dict:
    ranks = [p["rank"] for p in points if p["keyword"] == keyword]
    return gg.compute_solv(ranks, max_rank=max_rank)


def _delta(cur, base):
    if cur is None or base is None:
        return None
    return round(cur - base, 2)


def diff_scans(
    baseline: dict, base_points: list[dict], current: dict, cur_points: list[dict]
) -> dict:
    """
    Pure function. `baseline`/`current` are scan header dicts; `*_points` are pin dicts.
    Returns a JSON-serializable summary. Positive changes = improvement everywhere.
    """
    max_rank = min(int(current.get("depth") or 20), 20)

    base_cells = _points_by_cell(base_points)
    cur_cells = _points_by_cell(cur_points)
    shared = sorted(set(base_cells) & set(cur_cells))

    # geometry sanity: per-pin comparison is only valid if the grid didn't move.
    warning = None
    if any(baseline.get(k) != current.get(k) for k in _GEO_KEYS):
        warning = (
            "Grid geometry changed between scans (size/spacing/center); "
            "per-pin deltas cover only matching cells and may not be geographically aligned."
        )

    per_pin = []
    counts = {
        "improved": 0,
        "declined": 0,
        "unchanged": 0,
        "gained": 0,
        "lost": 0,
        "still_absent": 0,
    }
    for r, c, kw in shared:
        status, improvement = _classify(base_cells[(r, c, kw)], cur_cells[(r, c, kw)])
        counts[status] += 1
        per_pin.append(
            {
                "row": r,
                "col": c,
                "keyword": kw,
                "baseline_rank": base_cells[(r, c, kw)],
                "current_rank": cur_cells[(r, c, kw)],
                "improvement": improvement,
                "status": status,
            }
        )

    # per-keyword SoLV change (computed over each scan's full pin set for that keyword)
    keywords = sorted({p["keyword"] for p in cur_points} | {p["keyword"] for p in base_points})
    per_keyword = []
    for kw in keywords:
        b = _keyword_solv(base_points, kw, max_rank)
        c = _keyword_solv(cur_points, kw, max_rank)
        per_keyword.append(
            {
                "keyword": kw,
                "baseline_solv": b["solv"],
                "current_solv": c["solv"],
                "solv_change": _delta(c["solv"], b["solv"]),
                "baseline_avg_rank": b["avg_rank"],
                "current_avg_rank": c["avg_rank"],
            }
        )
    per_keyword.sort(key=lambda x: (x["solv_change"] is None, -(x["solv_change"] or 0)))

    # biggest movers among both-found pins (gains strictly up, drops strictly down)
    movers = [p for p in per_pin if p["improvement"] is not None and p["improvement"] != 0]
    top_gains = sorted(
        (p for p in movers if p["improvement"] > 0), key=lambda p: -p["improvement"]
    )[:5]
    top_drops = sorted((p for p in movers if p["improvement"] < 0), key=lambda p: p["improvement"])[
        :5
    ]

    # headline metrics: use the values already stored on each scan header
    def hv(scan, key):
        return scan.get(key)

    headline = {
        "solv": {
            "baseline": hv(baseline, "solv"),
            "current": hv(current, "solv"),
            "change": _delta(hv(current, "solv"), hv(baseline, "solv")),
        },
        # avg_rank: lower is better, so improvement = baseline - current
        "avg_rank": {
            "baseline": hv(baseline, "avg_rank"),
            "current": hv(current, "avg_rank"),
            "improvement": _delta(hv(baseline, "avg_rank"), hv(current, "avg_rank")),
        },
        "top3_share": {
            "baseline": hv(baseline, "top3_share"),
            "current": hv(current, "top3_share"),
            "change": _delta(hv(current, "top3_share"), hv(baseline, "top3_share")),
        },
        "found_share": {
            "baseline": hv(baseline, "found_share"),
            "current": hv(current, "found_share"),
            "change": _delta(hv(current, "found_share"), hv(baseline, "found_share")),
        },
    }
    net_improved = (counts["improved"] + counts["gained"]) - (counts["declined"] + counts["lost"])

    return {
        "place_id": current.get("place_id"),
        "baseline_scan_id": baseline.get("id"),
        "current_scan_id": current.get("id"),
        "baseline_ts": baseline.get("scanned_ts"),
        "current_ts": current.get("scanned_ts"),
        "compared_cells": len(shared),
        "warning": warning,
        "headline": headline,
        "net_pins_improved": net_improved,
        "counts": counts,
        "per_keyword": per_keyword,
        "top_gains": top_gains,
        "top_drops": top_drops,
        "per_pin": per_pin,
    }


# --------------------------------------------------------------------------- #
# Markdown section for the report (agent writes the narrative around it)        #
# --------------------------------------------------------------------------- #
def _fmt(v, pct=False, plus=False):
    if v is None:
        return "—"
    if pct:
        v = v * 100
    s = f"{v:+.1f}" if plus else f"{v:.1f}"
    return s + ("%" if pct else "")


def render_markdown(s: dict) -> str:
    h = s["headline"]
    L = []
    L.append("### Local visibility change since signup")
    L.append(
        f"*Baseline {s['baseline_ts']} → current {s['current_ts']} · "
        f"{s['compared_cells']} grid cells compared*\n"
    )
    if s["warning"]:
        L.append(f"> ⚠️ {s['warning']}\n")

    L.append("| Metric | Baseline | Current | Change |")
    L.append("|---|---|---|---|")
    L.append(
        f"| Share of Local Voice | {_fmt(h['solv']['baseline'])} | "
        f"{_fmt(h['solv']['current'])} | {_fmt(h['solv']['change'], plus=True)} |"
    )
    L.append(
        f"| Avg. rank (lower is better) | {_fmt(h['avg_rank']['baseline'])} | "
        f"{_fmt(h['avg_rank']['current'])} | {_fmt(h['avg_rank']['improvement'], plus=True)} better |"
    )
    L.append(
        f"| In the 3-pack | {_fmt(h['top3_share']['baseline'], pct=True)} | "
        f"{_fmt(h['top3_share']['current'], pct=True)} | {_fmt(h['top3_share']['change'], pct=True, plus=True)} |"
    )
    L.append(
        f"| Appearing at all | {_fmt(h['found_share']['baseline'], pct=True)} | "
        f"{_fmt(h['found_share']['current'], pct=True)} | {_fmt(h['found_share']['change'], pct=True, plus=True)} |"
    )
    c = s["counts"]
    L.append(
        f"\n**{s['net_pins_improved']:+d} net pins improved** — "
        f"{c['improved']} up, {c['gained']} newly appearing, "
        f"{c['declined']} down, {c['lost']} dropped off, {c['unchanged']} flat.\n"
    )

    L.append("**Per-keyword Share of Local Voice**\n")
    L.append("| Keyword | Baseline | Current | Change |")
    L.append("|---|---|---|---|")
    for k in s["per_keyword"]:
        L.append(
            f"| {k['keyword']} | {_fmt(k['baseline_solv'])} | "
            f"{_fmt(k['current_solv'])} | {_fmt(k['solv_change'], plus=True)} |"
        )

    if s["top_gains"]:
        L.append("\n**Biggest gains** (rank moved up):")
        for p in s["top_gains"]:
            L.append(
                f"- '{p['keyword']}' @ ({p['row']},{p['col']}): "
                f"{p['baseline_rank']} → {p['current_rank']} (+{p['improvement']})"
            )
    if s["top_drops"]:
        L.append("\n**Biggest drops** (needs attention):")
        for p in s["top_drops"]:
            L.append(
                f"- '{p['keyword']}' @ ({p['row']},{p['col']}): "
                f"{p['baseline_rank']} → {p['current_rank']} ({p['improvement']})"
            )
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- #
# DB wrapper + CLI                                                             #
# --------------------------------------------------------------------------- #
def diff_place(
    conn,
    place_id: str,
    baseline_scan_id: Optional[int] = None,
    current_scan_id: Optional[int] = None,
) -> dict:
    import leads_db_grid as gdb

    b_id = baseline_scan_id or gdb.baseline_scan_id(conn, place_id)
    c_id = (
        current_scan_id
        or gdb.latest_scan_id(conn, place_id, exclude_baseline=True)
        or gdb.latest_scan_id(conn, place_id)
    )
    if b_id is None:
        raise SystemExit(
            f"No completed baseline scan for {place_id}. Run a --scan-type baseline first."
        )
    if c_id is None or c_id == b_id:
        raise SystemExit(f"No later scan to compare against baseline for {place_id}.")
    baseline = gdb.get_scan(conn, b_id)
    current = gdb.get_scan(conn, c_id)
    return diff_scans(
        baseline, gdb.get_grid_points(conn, b_id), current, gdb.get_grid_points(conn, c_id)
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Baseline-vs-current geo-grid diff (pure reducer, $0)."
    )
    ap.add_argument("--place-id", required=True)
    ap.add_argument("--baseline-scan-id", type=int)
    ap.add_argument("--current-scan-id", type=int)
    ap.add_argument("--out", type=Path, help="Write the JSON summary here (default .tmp/grid/)")
    ap.add_argument("--md", type=Path, help="Also write the markdown section here")
    args = ap.parse_args(argv)

    import sqlite3

    conn = sqlite3.connect(DB_PATH)
    summary = diff_place(conn, args.place_id, args.baseline_scan_id, args.current_scan_id)

    out = args.out or Path(".tmp/grid") / f"diff_{slug(args.place_id)}.json"
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
