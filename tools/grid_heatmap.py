#!/usr/bin/env python3
"""
grid_heatmap.py — render geo-grid rank data to a Steel & Amber SVG heatmap (ADR-LP-001 Addendum A).

Pure rendering + a thin DB CLI. No network, no spend. Feeds the client report:
  - one grid per keyword, or an aggregate (mean rank per cell across keywords)
  - a before/after pair (baseline vs current) — the sales money-shot

  python tools/grid_heatmap.py --place-id ChIJ... --mode before-after --out output/acme-grid.svg
  python tools/grid_heatmap.py --place-id ChIJ... --mode current --keyword "land clearing"

SVG is self-contained (embeds in the report HTML for PDF rendering, or saves standalone).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import slug

DB_PATH = Path("data/leads.sqlite")

# Carbide and Dirt — Steel & Amber
PAPER = "#F4F1EA"
STEEL = "#34404A"
STEEL_DARK = "#2E3640"
STEEL_GREY = "#7A8590"
AMBER = "#E0922F"
AMBER_DEEP = "#C0721F"

# Rank tiers (bright amber = you own it, fading to dark steel = invisible).
TIER_COLOR = {"top3": AMBER, "page1": AMBER_DEEP, "deep": STEEL_GREY, "absent": STEEL_DARK}
TIER_TEXT = {"top3": STEEL_DARK, "page1": PAPER, "deep": PAPER, "absent": "#9AA0A6"}
TIER_LABEL = {"top3": "1–3  (3-pack)", "page1": "4–10", "deep": "11–20", "absent": "Not found"}

_CELL = 58  # px per grid cell
_R = 22  # pin radius
_PAD = 28
_TITLE_H = 64
_LEGEND_H = 44
_FONT = "'Inter','Helvetica Neue',Arial,sans-serif"


def rank_tier(rank) -> str:
    if rank is None:
        return "absent"
    if rank <= 3:
        return "top3"
    if rank <= 10:
        return "page1"
    if rank <= 20:
        return "deep"
    return "absent"  # >20 = effectively not visible


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cells_lookup(cells: Sequence[dict]) -> dict:
    return {(c["row"], c["col"]): c.get("rank") for c in cells}


def _dims(cells: Sequence[dict], rows: Optional[int], cols: Optional[int]):
    if rows and cols:
        return rows, cols
    return max(c["row"] for c in cells) + 1, max(c["col"] for c in cells) + 1


def _grid_elements(ox: int, oy: int, lookup: dict, rows: int, cols: int) -> str:
    out = []
    for r in range(rows):
        for c in range(cols):
            rank = lookup.get((r, c))
            tier = rank_tier(rank)
            cx = ox + c * _CELL + _CELL // 2
            cy = oy + r * _CELL + _CELL // 2
            label = "–" if rank is None or tier == "absent" else str(int(round(rank)))
            out.append(
                f'<circle cx="{cx}" cy="{cy}" r="{_R}" fill="{TIER_COLOR[tier]}" '
                f'stroke="{PAPER}" stroke-width="2"/>'
            )
            out.append(
                f'<text x="{cx}" y="{cy}" fill="{TIER_TEXT[tier]}" font-family="{_FONT}" '
                f'font-size="16" font-weight="700" text-anchor="middle" '
                f'dominant-baseline="central">{label}</text>'
            )
    return "\n".join(out)


def _panel(
    ox: int, oy: int, cells: Sequence[dict], rows: int, cols: int, label: str, solv: Optional[float]
) -> str:
    grid_w = cols * _CELL
    parts = [
        f'<text x="{ox}" y="{oy - 26}" fill="{STEEL}" font-family="{_FONT}" '
        f'font-size="15" font-weight="700" letter-spacing="1.5">{_esc(label.upper())}</text>'
    ]
    if solv is not None:
        parts.append(
            f'<text x="{ox + grid_w}" y="{oy - 26}" fill="{AMBER_DEEP}" font-family="{_FONT}" '
            f'font-size="15" font-weight="700" text-anchor="end">SoLV {solv:.1f}</text>'
        )
    parts.append(_grid_elements(ox, oy, _cells_lookup(cells), rows, cols))
    return "\n".join(parts)


def _legend(x: int, y: int) -> str:
    parts = []
    order = ["top3", "page1", "deep", "absent"]
    step = 168
    for i, tier in enumerate(order):
        lx = x + i * step
        parts.append(
            f'<circle cx="{lx}" cy="{y}" r="9" fill="{TIER_COLOR[tier]}" '
            f'stroke="{PAPER}" stroke-width="1.5"/>'
        )
        parts.append(
            f'<text x="{lx + 16}" y="{y}" fill="{STEEL}" font-family="{_FONT}" '
            f'font-size="13" dominant-baseline="central">{TIER_LABEL[tier]}</text>'
        )
    return "\n".join(parts)


def _svg(width: int, height: int, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">\n'
        f'<rect width="{width}" height="{height}" fill="{PAPER}"/>\n'
        f"{body}\n</svg>\n"
    )


def render_heatmap(
    cells: Sequence[dict],
    *,
    title: str,
    subtitle: str = "",
    rows: Optional[int] = None,
    cols: Optional[int] = None,
    solv: Optional[float] = None,
) -> str:
    """One grid. `cells` = [{'row','col','rank'}, ...]; rank None/>20 = not found."""
    if not cells:
        raise ValueError("no cells to render")
    rows, cols = _dims(cells, rows, cols)
    grid_w, grid_h = cols * _CELL, rows * _CELL
    width = grid_w + 2 * _PAD
    height = _TITLE_H + grid_h + _LEGEND_H + 2 * _PAD

    title_el = (
        f'<text x="{_PAD}" y="{_PAD + 20}" fill="{STEEL_DARK}" font-family="{_FONT}" '
        f'font-size="22" font-weight="800">{_esc(title)}</text>'
        f'<rect x="{_PAD}" y="{_PAD + 30}" width="52" height="4" fill="{AMBER}"/>'
    )
    sub_el = (
        f'<text x="{_PAD}" y="{_PAD + 52}" fill="{STEEL_GREY}" font-family="{_FONT}" '
        f'font-size="13">{_esc(subtitle)}</text>'
        if subtitle
        else ""
    )
    panel = _panel(_PAD, _TITLE_H + _PAD + 8, cells, rows, cols, "coverage", solv)
    legend = _legend(_PAD + 6, _TITLE_H + _PAD + grid_h + 30)
    return _svg(width, height, title_el + sub_el + panel + legend)


def render_before_after(
    baseline_cells: Sequence[dict],
    current_cells: Sequence[dict],
    *,
    title: str,
    subtitle: str = "",
    rows: Optional[int] = None,
    cols: Optional[int] = None,
    base_solv: Optional[float] = None,
    cur_solv: Optional[float] = None,
) -> str:
    """Two grids side by side: baseline (At signup) vs current (Now)."""
    cells = list(baseline_cells) + list(current_cells)
    rows, cols = _dims(cells, rows, cols)
    grid_w, grid_h = cols * _CELL, rows * _CELL
    gap = 64
    width = 2 * grid_w + gap + 2 * _PAD
    height = _TITLE_H + 24 + grid_h + _LEGEND_H + 2 * _PAD

    title_el = (
        f'<text x="{_PAD}" y="{_PAD + 20}" fill="{STEEL_DARK}" font-family="{_FONT}" '
        f'font-size="22" font-weight="800">{_esc(title)}</text>'
        f'<rect x="{_PAD}" y="{_PAD + 30}" width="52" height="4" fill="{AMBER}"/>'
    )
    sub = subtitle
    if base_solv is not None and cur_solv is not None:
        sub = (
            subtitle + "   " if subtitle else ""
        ) + f"Share of Local Voice {base_solv:.1f} → {cur_solv:.1f} ({cur_solv - base_solv:+.1f})"
    sub_el = (
        f'<text x="{_PAD}" y="{_PAD + 52}" fill="{STEEL_GREY}" font-family="{_FONT}" '
        f'font-size="13">{_esc(sub)}</text>'
        if sub
        else ""
    )
    gy = _TITLE_H + _PAD + 24
    left = _panel(_PAD, gy, baseline_cells, rows, cols, "At signup", base_solv)
    right = _panel(_PAD + grid_w + gap, gy, current_cells, rows, cols, "Now", cur_solv)
    legend = _legend(_PAD + 6, gy + grid_h + 30)
    return _svg(width, height, title_el + sub_el + left + right + legend)


# --------------------------------------------------------------------------- #
# DB helpers: turn a scan's points into cells (one keyword or aggregate)        #
# --------------------------------------------------------------------------- #
def cells_for_keyword(points: Sequence[dict], keyword: str) -> list[dict]:
    return [
        {"row": p["row"], "col": p["col"], "rank": p["rank"]}
        for p in points
        if p["keyword"] == keyword
    ]


def cells_aggregate(points: Sequence[dict]) -> list[dict]:
    """Mean rank per (row,col) across keywords; None where the target is nowhere found."""
    acc: dict = {}
    for p in points:
        acc.setdefault((p["row"], p["col"]), []).append(p["rank"])
    out = []
    for (r, c), ranks in acc.items():
        found = [x for x in ranks if x is not None]
        out.append({"row": r, "col": c, "rank": (sum(found) / len(found)) if found else None})
    return out


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Render a Steel & Amber geo-grid heatmap SVG.")
    ap.add_argument("--place-id", required=True)
    ap.add_argument("--mode", choices=["current", "before-after"], default="before-after")
    ap.add_argument("--keyword", help="Single keyword; omit for the aggregate grid")
    ap.add_argument("--title", default="Local Search Coverage")
    ap.add_argument("--out", type=Path, help="Write SVG here (default output/<place>-grid.svg)")
    args = ap.parse_args(argv)

    import sqlite3
    import leads_db_grid as gdb

    conn = sqlite3.connect(DB_PATH)

    def cells_from(scan_id):
        pts = gdb.get_grid_points(conn, scan_id)
        return cells_for_keyword(pts, args.keyword) if args.keyword else cells_aggregate(pts)

    def solv_of(scan_id):
        s = gdb.get_scan(conn, scan_id)
        return s.get("solv") if s else None

    sub = f"Keyword: {args.keyword}" if args.keyword else "All keywords (aggregate)"

    if args.mode == "current":
        cur_id = gdb.latest_scan_id(
            conn, args.place_id, exclude_baseline=True
        ) or gdb.latest_scan_id(conn, args.place_id)
        if cur_id is None:
            raise SystemExit(f"No completed scan for {args.place_id}.")
        svg = render_heatmap(
            cells_from(cur_id), title=args.title, subtitle=sub, solv=solv_of(cur_id)
        )
    else:
        b_id = gdb.baseline_scan_id(conn, args.place_id)
        c_id = gdb.latest_scan_id(conn, args.place_id, exclude_baseline=True)
        if b_id is None or c_id is None:
            raise SystemExit("Need both a baseline and a later scan for before-after.")
        svg = render_before_after(
            cells_from(b_id),
            cells_from(c_id),
            title=args.title,
            subtitle=sub,
            base_solv=solv_of(b_id),
            cur_solv=solv_of(c_id),
        )

    out = args.out or Path("output") / f"{slug(args.place_id)}-grid.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg, encoding="utf-8")
    print(f"[svg -> {out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
