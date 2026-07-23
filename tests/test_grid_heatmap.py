"""Tests for grid_heatmap.py — pure rendering, no network, no spend."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import grid_heatmap as gh


def test_rank_tiers():
    assert gh.rank_tier(1) == "top3"
    assert gh.rank_tier(3) == "top3"
    assert gh.rank_tier(4) == "page1"
    assert gh.rank_tier(10) == "page1"
    assert gh.rank_tier(11) == "deep"
    assert gh.rank_tier(20) == "deep"
    assert gh.rank_tier(21) == "absent"
    assert gh.rank_tier(None) == "absent"


def _grid_cells(rows, cols, rank=5):
    return [{"row": r, "col": c, "rank": rank} for r in range(rows) for c in range(cols)]


def test_render_heatmap_wellformed():
    svg = gh.render_heatmap(
        _grid_cells(5, 5), title="Coverage", subtitle="Keyword: land clearing", solv=42.0
    )
    assert svg.startswith("<svg") and svg.strip().endswith("</svg>")
    assert svg.count("<circle") == 25 + 4  # 25 pins + 4 legend swatches
    assert 'xmlns="http://www.w3.org/2000/svg"' in svg
    assert gh.PAPER in svg and gh.AMBER in svg  # brand palette present


def test_absent_pins_render_dash():
    svg = gh.render_heatmap(
        [{"row": 0, "col": 0, "rank": None}, {"row": 0, "col": 1, "rank": 2}],
        rows=1,
        cols=2,
        title="t",
    )
    assert ">–<" in svg  # absent shows an en-dash
    assert ">2<" in svg


def test_before_after_has_both_panels():
    base = _grid_cells(3, 3, rank=18)
    cur = _grid_cells(3, 3, rank=2)
    svg = gh.render_before_after(
        base, cur, title="Coverage since signup", base_solv=20.0, cur_solv=61.0
    )
    assert "AT SIGNUP" in svg and "NOW" in svg
    assert "20.0 → 61.0 (+41.0)" in svg
    assert svg.count("<circle") == 9 + 9 + 4  # two grids + legend


def test_aggregate_mean_and_all_none():
    pts = [
        {"row": 0, "col": 0, "keyword": "a", "rank": 4},
        {"row": 0, "col": 0, "keyword": "b", "rank": 8},
        {"row": 0, "col": 1, "keyword": "a", "rank": None},
        {"row": 0, "col": 1, "keyword": "b", "rank": None},
    ]
    agg = {(c["row"], c["col"]): c["rank"] for c in gh.cells_aggregate(pts)}
    assert agg[(0, 0)] == 6.0  # mean of 4 and 8
    assert agg[(0, 1)] is None  # all absent


def test_escaping():
    svg = gh.render_heatmap(_grid_cells(1, 1), title="Ace & Sons <Excavating>")
    assert "&amp;" in svg and "&lt;" in svg


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
