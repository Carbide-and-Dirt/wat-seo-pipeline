#!/usr/bin/env python3
"""
grid_report_section.py — Local Search Visibility section for the downstream client-report assembler (ADR-LP-001 Addendum A).

The report assembler builds Steel & Amber HTML and renders it to PDF (headless Chromium).
This returns a self-contained HTML <section> (inline styles, inline SVG — no external files,
PDF-safe) that drops into the report body. One call does everything:

    import sqlite3
    from grid_report_section import build_grid_section
    conn = sqlite3.connect("data/leads.sqlite")
    section_html = build_grid_section(conn, client_place_id)   # -> str, or "" if no grid data
    # ...concatenate section_html into the report body where the other sections go,
    #    then render the full HTML to PDF as usual.

No network, no spend — pure reducer + render over leads.sqlite.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

import grid_diff as gd
import grid_heatmap as gh
import leads_db_grid as gdb

# Steel & Amber (reuse the heatmap's palette so section + image match)
PAPER = gh.PAPER
STEEL = gh.STEEL
STEEL_DARK = gh.STEEL_DARK
STEEL_GREY = gh.STEEL_GREY
AMBER = gh.AMBER
AMBER_DEEP = gh.AMBER_DEEP
GOOD = AMBER_DEEP  # improvement
BAD = "#B4462F"  # decline (brand-compatible brick, not a jarring red)
FONT = "'Inter','Helvetica Neue',Arial,sans-serif"


# --------------------------------------------------------------------------- #
# formatting helpers                                                          #
# --------------------------------------------------------------------------- #
def _num(v, pct=False):
    if v is None:
        return "—"
    return f"{v * 100:.0f}%" if pct else f"{v:.1f}"


def _chg(v, higher_better=True, pct=False):
    """Colored change cell HTML (positive=good unless higher_better=False)."""
    if v is None:
        return f'<td style="{_TD}color:{STEEL_GREY}">—</td>'
    good = (v > 0) if higher_better else (v < 0)
    color = GOOD if (good and v != 0) else (BAD if v != 0 else STEEL_GREY)
    shown = f"{v * 100:+.0f}%" if pct else f"{v:+.1f}"
    return f'<td style="{_TD}color:{color};font-weight:700">{shown}</td>'


_TD = "padding:6px 12px;border-bottom:1px solid #E4DFD3;font-size:13px;"
_TH = (
    "padding:6px 12px;border-bottom:2px solid "
    + STEEL
    + ";font-size:12px;letter-spacing:.5px;text-transform:uppercase;color:"
    + STEEL
    + ";text-align:left;"
)


def _esc(s):
    return gh._esc(s)


def _responsive_svg(svg: str) -> str:
    """Strip the root svg's fixed width/height so it scales to the page (viewBox preserved)."""
    import re

    return re.sub(r'(<svg[^>]*?)\swidth="\d+"\s+height="\d+"', r'\1 width="100%"', svg, count=1)


def _section_open(title: str) -> str:
    return (
        f'<section style="font-family:{FONT};color:{STEEL_DARK};margin:24px 0;">'
        f'<h2 style="font-size:20px;font-weight:800;margin:0 0 2px 0;">{_esc(title)}</h2>'
        f'<div style="width:52px;height:4px;background:{AMBER};margin-bottom:14px;"></div>'
    )


def _metrics_table(h: dict) -> str:
    rows = [
        (
            "Share of Local Voice",
            _num(h["solv"]["baseline"]),
            _num(h["solv"]["current"]),
            _chg(h["solv"]["change"]),
        ),
        (
            "Avg. rank (lower is better)",
            _num(h["avg_rank"]["baseline"]),
            _num(h["avg_rank"]["current"]),
            _chg(h["avg_rank"]["improvement"]),
        ),  # improvement already sign-corrected (positive=better)
        (
            "In the 3-pack",
            _num(h["top3_share"]["baseline"], pct=True),
            _num(h["top3_share"]["current"], pct=True),
            _chg(h["top3_share"]["change"], pct=True),
        ),
        (
            "Appearing at all",
            _num(h["found_share"]["baseline"], pct=True),
            _num(h["found_share"]["current"], pct=True),
            _chg(h["found_share"]["change"], pct=True),
        ),
    ]
    body = "".join(
        f'<tr><td style="{_TD}font-weight:600">{label}</td>'
        f'<td style="{_TD}">{b}</td><td style="{_TD}">{c}</td>{chg}</tr>'
        for (label, b, c, chg) in rows
    )
    return (
        f'<table style="border-collapse:collapse;width:100%;margin:10px 0;">'
        f'<tr><th style="{_TH}">Metric</th><th style="{_TH}">At signup</th>'
        f'<th style="{_TH}">Now</th><th style="{_TH}">Change</th></tr>{body}</table>'
    )


def _keyword_table(per_keyword: list) -> str:
    body = "".join(
        f'<tr><td style="{_TD}font-weight:600">{_esc(k["keyword"])}</td>'
        f'<td style="{_TD}">{_num(k["baseline_solv"])}</td>'
        f'<td style="{_TD}">{_num(k["current_solv"])}</td>{_chg(k["solv_change"])}</tr>'
        for k in per_keyword
    )
    return (
        f'<table style="border-collapse:collapse;width:100%;margin:10px 0;">'
        f'<tr><th style="{_TH}">Keyword</th><th style="{_TH}">At signup</th>'
        f'<th style="{_TH}">Now</th><th style="{_TH}">SoLV change</th></tr>{body}</table>'
    )


def _movers(summary: dict) -> str:
    def line(p, color, sign):
        return (
            f'<li style="margin:2px 0;">“{_esc(p["keyword"])}” at ({p["row"]},{p["col"]}): '
            f"{p['baseline_rank']} → {p['current_rank']} "
            f'<span style="color:{color};font-weight:700">({sign}{abs(p["improvement"])})</span></li>'
        )

    out = []
    if summary["top_gains"]:
        out.append(
            f'<div style="font-weight:700;margin-top:8px;color:{STEEL}">Biggest gains</div>'
            f'<ul style="margin:4px 0 0 18px;padding:0;font-size:13px;">'
            + "".join(line(p, GOOD, "+") for p in summary["top_gains"])
            + "</ul>"
        )
    if summary["top_drops"]:
        out.append(
            f'<div style="font-weight:700;margin-top:8px;color:{STEEL}">Needs attention</div>'
            f'<ul style="margin:4px 0 0 18px;padding:0;font-size:13px;">'
            + "".join(line(p, BAD, "") for p in summary["top_drops"])
            + "</ul>"
        )
    return "".join(out)


def render_html_section(summary: dict, svg: str, *, title: str = "Local Search Visibility") -> str:
    """Full before/after section: heatmap + headline metrics + per-keyword + movers."""
    c = summary["counts"]
    net = summary["net_pins_improved"]
    caption = (
        f'<p style="font-size:13px;color:{STEEL};margin:6px 0 0 0;">'
        f'<b style="color:{GOOD}">{net:+d} net grid points improved</b> — '
        f"{c['improved']} moved up, {c['gained']} newly appearing, "
        f"{c['declined']} down, {c['lost']} dropped off, {c['unchanged']} flat.</p>"
    )
    warn = (
        f'<p style="font-size:12px;color:{BAD};margin:4px 0;">⚠ {_esc(summary["warning"])}</p>'
        if summary.get("warning")
        else ""
    )
    return (
        _section_open(title)
        + f'<div style="max-width:920px;margin:0 auto 12px;">{_responsive_svg(svg)}</div>'
        + warn
        + _metrics_table(summary["headline"])
        + caption
        + f'<h3 style="font-size:14px;margin:16px 0 0 0;color:{STEEL_DARK}">Per-keyword Share of Local Voice</h3>'
        + _keyword_table(summary["per_keyword"])
        + _movers(summary)
        + "</section>"
    )


def _render_single(scan: dict, svg: str, *, title: str) -> str:
    """Fallback when only one scan exists (e.g. the sale-time baseline): show it, no deltas yet."""
    return (
        _section_open(title)
        + f'<div style="max-width:520px;margin:0 auto 12px;">{_responsive_svg(svg)}</div>'
        + f'<p style="font-size:13px;color:{STEEL};">Share of Local Voice: '
        f'<b style="color:{AMBER_DEEP}">{_num(scan.get("solv"))}</b>. '
        f"Month-over-month comparison begins with your next report.</p>" + "</section>"
    )


# --------------------------------------------------------------------------- #
# one-call entry point for the downstream report assembler                     #
# --------------------------------------------------------------------------- #
def build_grid_section(
    conn, place_id: str, *, keyword: Optional[str] = None, title: str = "Local Search Visibility"
) -> str:
    """Returns a ready-to-embed HTML section, or '' if the business has no grid scans yet."""

    def cells(scan_id):
        pts = gdb.get_grid_points(conn, scan_id)
        return gh.cells_for_keyword(pts, keyword) if keyword else gh.cells_aggregate(pts)

    sub = f"Keyword: {keyword}" if keyword else "All keywords (aggregate)"
    b_id = gdb.baseline_scan_id(conn, place_id)
    c_id = gdb.latest_scan_id(conn, place_id, exclude_baseline=True)

    if b_id and c_id and b_id != c_id:
        summary = gd.diff_place(conn, place_id, baseline_scan_id=b_id, current_scan_id=c_id)
        bs, cs = gdb.get_scan(conn, b_id), gdb.get_scan(conn, c_id)
        svg = gh.render_before_after(
            cells(b_id),
            cells(c_id),
            title="Local Search Coverage",
            subtitle=sub,
            base_solv=bs.get("solv"),
            cur_solv=cs.get("solv"),
        )
        return render_html_section(summary, svg, title=title)

    only = c_id or b_id
    if not only:
        return ""
    scan = gdb.get_scan(conn, only)
    svg = gh.render_heatmap(
        cells(only), title="Local Search Coverage", subtitle=sub, solv=scan.get("solv")
    )
    return _render_single(scan, svg, title=title)


# --------------------------------------------------------------------------- #
# CLI — the file handoff the report assembler consumes ($0, pure reducer)      #
# --------------------------------------------------------------------------- #
def main(argv=None):
    import argparse
    import sqlite3

    ap = argparse.ArgumentParser(description="Write the Local Search Visibility HTML section.")
    ap.add_argument("--place-id", required=True)
    ap.add_argument("--keyword", help="Single keyword; omit for the aggregate grid")
    ap.add_argument("--title", default="Local Search Visibility")
    ap.add_argument("--db", type=Path, default=Path("data/leads.sqlite"))
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    section = build_grid_section(conn, args.place_id, keyword=args.keyword, title=args.title)
    if not section:
        raise SystemExit(f"No grid scans for {args.place_id}; run geo_grid.py first.")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(section, encoding="utf-8")
    print(f"[section -> {args.out}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
