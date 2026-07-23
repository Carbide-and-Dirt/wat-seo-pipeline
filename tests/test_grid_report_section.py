"""Tests for grid_report_section.py — pure render over an in-memory DB. No network, no spend."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import grid_report_section as grs
import geo_grid as gg
import leads_db_grid as gdb


def _seed(conn, scan_type, points):
    sid = gdb.insert_grid_scan(
        conn,
        place_id="T",
        scan_type=scan_type,
        grid_rows=1,
        grid_cols=2,
        spacing_km=1.0,
        center_lat=36.0,
        center_lng=-86.0,
        zoom=14,
        keywords=["a", "b"],
        depth=20,
        priority="standard",
    )
    for p in points:
        gdb.upsert_grid_point(
            conn,
            grid_scan_id=sid,
            row=p["row"],
            col=p["col"],
            keyword=p["keyword"],
            point_lat=36.0,
            point_lng=-86.0,
            rank=p["rank"],
            found_place_id=("T" if p["rank"] else None),
            result_depth=20,
        )
    m = gg.compute_solv([p["rank"] for p in points])
    gdb.finalize_grid_scan(
        conn,
        grid_scan_id=sid,
        solv=m["solv"],
        avg_rank=m["avg_rank"],
        top3_share=m["top3_share"],
        found_share=m["found_share"],
        api_cost_usd=0.0,
        status="complete",
    )
    return sid


BASE = [
    {"row": 0, "col": 0, "keyword": "a", "rank": 12},
    {"row": 0, "col": 1, "keyword": "a", "rank": None},
    {"row": 0, "col": 0, "keyword": "b", "rank": None},
    {"row": 0, "col": 1, "keyword": "b", "rank": 15},
]
CUR = [
    {"row": 0, "col": 0, "keyword": "a", "rank": 2},
    {"row": 0, "col": 1, "keyword": "a", "rank": 3},
    {"row": 0, "col": 0, "keyword": "b", "rank": 4},
    {"row": 0, "col": 1, "keyword": "b", "rank": 5},
]


def _mem():
    conn = sqlite3.connect(":memory:")
    gdb.create_grid_tables(conn)
    return conn


def test_responsive_svg_scales():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="500" viewBox="0 0 900 500"><rect/></svg>'
    out = grs._responsive_svg(svg)
    assert 'width="100%"' in out and 'height="500"' not in out
    assert 'viewBox="0 0 900 500"' in out  # aspect preserved


def test_full_section_end_to_end():
    conn = _mem()
    _seed(conn, "baseline", BASE)
    _seed(conn, "monthly", CUR)
    html = grs.build_grid_section(conn, "T")
    assert html.startswith("<section")
    assert "Local Search Visibility" in html
    assert "<svg" in html and 'width="100%"' in html
    assert "Share of Local Voice" in html and "In the 3-pack" in html
    assert "net grid points improved" in html
    assert "Per-keyword Share of Local Voice" in html
    # improvement should be colored with the GOOD (amber) token somewhere
    assert grs.GOOD in html


def test_single_scan_fallback():
    conn = _mem()
    _seed(conn, "baseline", BASE)  # only one scan
    html = grs.build_grid_section(conn, "T")
    assert html.startswith("<section")
    assert "next report" in html.lower()
    assert "<svg" in html


def test_no_data_returns_empty():
    conn = _mem()
    assert grs.build_grid_section(conn, "NOPE") == ""


def test_keyword_filter():
    conn = _mem()
    _seed(conn, "baseline", BASE)
    _seed(conn, "monthly", CUR)
    html = grs.build_grid_section(conn, "T", keyword="a")
    assert "Keyword: a" in html


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
