"""Tests for grid_diff.py — pure reducer, no network, no spend."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import grid_diff as gd
import leads_db_grid as gdb


def _hdr(**kw):
    base = dict(
        id=1,
        place_id="TARGET",
        scanned_ts="2026-01-01T00:00:00+00:00",
        depth=20,
        grid_rows=1,
        grid_cols=2,
        spacing_km=1.0,
        center_lat=36.0,
        center_lng=-86.0,
        zoom=14,
        solv=30.0,
        avg_rank=4.0,
        top3_share=0.0,
        found_share=0.75,
    )
    base.update(kw)
    return base


BASE_PTS = [
    {"row": 0, "col": 0, "keyword": "a", "rank": 5},
    {"row": 0, "col": 1, "keyword": "a", "rank": 2},
    {"row": 0, "col": 0, "keyword": "b", "rank": None},
    {"row": 0, "col": 1, "keyword": "b", "rank": 7},
]
CUR_PTS = [
    {"row": 0, "col": 0, "keyword": "a", "rank": 2},  # 5 -> 2 improved
    {"row": 0, "col": 1, "keyword": "a", "rank": 2},  # 2 -> 2 unchanged
    {"row": 0, "col": 0, "keyword": "b", "rank": 4},  # None -> 4 gained
    {"row": 0, "col": 1, "keyword": "b", "rank": None},  # 7 -> None lost
]


def test_status_counts():
    s = gd.diff_scans(_hdr(id=1), BASE_PTS, _hdr(id=2, solv=45.0, avg_rank=2.5), CUR_PTS)
    c = s["counts"]
    assert c["improved"] == 1 and c["unchanged"] == 1
    assert c["gained"] == 1 and c["lost"] == 1
    assert c["declined"] == 0 and c["still_absent"] == 0
    assert s["net_pins_improved"] == 1  # (1 improved + 1 gained) - (0 declined + 1 lost)
    assert s["compared_cells"] == 4


def test_headline_changes_positive_is_better():
    s = gd.diff_scans(_hdr(), BASE_PTS, _hdr(id=2, solv=45.0, avg_rank=2.5), CUR_PTS)
    assert s["headline"]["solv"]["change"] == 15.0
    # avg_rank improvement = baseline(4.0) - current(2.5) = +1.5
    assert s["headline"]["avg_rank"]["improvement"] == 1.5


def test_geometry_change_sets_warning():
    s = gd.diff_scans(_hdr(), BASE_PTS, _hdr(id=2, spacing_km=2.0), CUR_PTS)
    assert s["warning"] is not None


def test_top_movers_sorted():
    s = gd.diff_scans(_hdr(), BASE_PTS, _hdr(id=2), CUR_PTS)
    # only ('a' @0,0) is a both-found mover here (+3); unchanged is excluded
    assert s["top_gains"] and s["top_gains"][0]["improvement"] == 3
    assert all(p["improvement"] > 0 for p in s["top_gains"])


def test_render_markdown_has_key_sections():
    s = gd.diff_scans(_hdr(), BASE_PTS, _hdr(id=2, solv=45.0, avg_rank=2.5), CUR_PTS)
    md = gd.render_markdown(s)
    assert "Share of Local Voice" in md
    assert "net pins improved" in md
    assert "Per-keyword" in md


def _seed_scan(conn, scan_type, ts_suffix, points):
    sid = gdb.insert_grid_scan(
        conn,
        place_id="TARGET",
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
            found_place_id=("TARGET" if p["rank"] else None),
            result_depth=20,
        )
    ranks = [p["rank"] for p in points]
    import geo_grid as gg

    m = gg.compute_solv(ranks)
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


def test_diff_place_end_to_end():
    conn = sqlite3.connect(":memory:")
    gdb.create_grid_tables(conn)
    _seed_scan(conn, "baseline", "01", BASE_PTS)
    _seed_scan(conn, "monthly", "02", CUR_PTS)
    s = gd.diff_place(conn, "TARGET")  # auto-picks baseline vs latest monthly
    assert s["compared_cells"] == 4
    assert s["counts"]["gained"] == 1 and s["counts"]["lost"] == 1
    # SoLV rose (baseline has two None/low, current has more presence)
    assert s["headline"]["solv"]["change"] is not None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
