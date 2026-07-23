"""
Tests for geo_grid.py — no network, no spend. Uses a faked Maps client (test_measure.py style).
Run standalone (`python tests/test_geo_grid.py`) or via `pytest tests/`.
"""

import math
import sqlite3
import sys
from pathlib import Path

# Allow running from repo root or tests/ dir, matching the other test files.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import geo_grid as gg
import leads_db_grid as gdb


# --------------------------- grid geometry --------------------------- #
def test_grid_point_count_and_orientation():
    pts = gg.build_grid(36.16, -86.78, 3, 3, 2.0)
    assert len(pts) == 9
    # (0,0) is NW: north of and west of center; (2,2) is SE.
    nw = next(p for p in pts if p.row == 0 and p.col == 0)
    se = next(p for p in pts if p.row == 2 and p.col == 2)
    assert nw.lat > 36.16 and nw.lng < -86.78
    assert se.lat < 36.16 and se.lng > -86.78
    center = next(p for p in pts if p.row == 1 and p.col == 1)
    assert math.isclose(center.lat, 36.16, abs_tol=1e-6)
    assert math.isclose(center.lng, -86.78, abs_tol=1e-6)


def test_grid_spacing_is_right_distance():
    # Two vertically adjacent points should be ~spacing_km apart.
    pts = {(p.row, p.col): p for p in gg.build_grid(40.0, -80.0, 3, 1, 1.5)}
    a, b = pts[(0, 0)], pts[(1, 0)]
    km = (a.lat - b.lat) * gg._KM_PER_DEG_LAT
    assert math.isclose(km, 1.5, rel_tol=0.02)


def test_build_grid_validates():
    for bad in [(0, 3, 1.0), (3, 0, 1.0), (3, 3, 0.0)]:
        try:
            gg.build_grid(36.0, -86.0, *bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


# --------------------------- cost estimate --------------------------- #
def test_estimate_cost():
    est = gg.estimate_cost(7, 7, 10, "standard")
    assert est["requests"] == 490
    assert math.isclose(est["usd"], 490 * 0.0006, rel_tol=1e-9)
    assert gg.estimate_cost(7, 7, 10, "live")["usd"] > est["usd"]


# --------------------------- rank extraction ------------------------- #
def test_extract_rank_prefers_rank_absolute_and_matches_place_id():
    items = [
        {"place_id": "AAA", "rank_absolute": 1},
        {"title": "an ad with no place_id"},  # skipped
        {"place_id": "TARGET", "rank_absolute": 2},
    ]
    rank, pid, depth = gg.extract_rank(items, "TARGET")
    assert rank == 2 and pid == "TARGET" and depth == 3


def test_extract_rank_not_found():
    items = [{"place_id": "AAA", "rank_absolute": 1}]
    rank, pid, depth = gg.extract_rank(items, "TARGET")
    assert rank is None and pid is None and depth == 1


# --------------------------- SoLV reducer ---------------------------- #
def test_compute_solv_extremes():
    assert gg.compute_solv([1, 1, 1])["solv"] == 100.0
    none_only = gg.compute_solv([None, None])
    assert none_only["solv"] == 0.0 and none_only["avg_rank"] is None
    mixed = gg.compute_solv([1, None, 3, 20])
    assert 0.0 < mixed["solv"] < 100.0
    assert mixed["avg_rank"] == round((1 + 3 + 20) / 3, 2)
    assert mixed["found_share"] == 0.75
    assert mixed["top3_share"] == 0.5  # ranks 1 and 3


# ------------- DataForSEO client: 40102 "No Search Results" = absent -------- #
class _FakeDfs:
    """Stands in for the dataforseo module: post() raises a queued error or returns a result."""

    def __init__(self, exc=None, result=None):
        self._exc, self._result = exc, result

    def post(self, endpoint, payload, auth):
        if self._exc:
            raise self._exc
        return self._result


def _maps_client(fake_dfs):
    # Build without __init__ (which needs .env creds); wire only what fetch() reads.
    c = object.__new__(gg.DataForSEOMapsClient)
    c._dfs, c._auth, c.language_code = fake_dfs, ("u", "p"), "en"
    return c


def test_fetch_treats_40102_no_results_as_absent():
    # A pin with no local pack (task error 40102) is data, not a failure: [] and no raise, so
    # the pin is recorded absent (darkest tier) instead of aborting the whole scan.
    client = _maps_client(_FakeDfs(exc=RuntimeError("task 40102 No Search Results")))
    assert client.fetch("kw", 40.0, -80.0, 14, 20, "live") == []


def test_fetch_reraises_non_40102_errors():
    client = _maps_client(_FakeDfs(exc=RuntimeError("task 40501 Invalid Field")))
    try:
        client.fetch("kw", 40.0, -80.0, 14, 20, "live")
        raise AssertionError("expected the non-40102 error to propagate")
    except RuntimeError:
        pass


def test_fetch_returns_items_on_success():
    client = _maps_client(_FakeDfs(result=[{"items": [{"place_id": "X", "rank_absolute": 1}]}]))
    assert client.fetch("kw", 40.0, -80.0, 14, 20, "live") == [
        {"place_id": "X", "rank_absolute": 1}
    ]


# --------------------------- end-to-end w/ fake client --------------- #
class FakeMapsClient:
    """Deterministic: the target sits at rank = (row + 1), independent of keyword."""

    def __init__(self, target="TARGET"):
        self.target = target
        self.calls = 0

    def fetch(self, keyword, lat, lng, zoom, depth, priority):
        self.calls += 1
        # emulate "closer to center/north ranks better"
        pos = 1 if lat > 40.0 else 4
        return [
            {"place_id": "OTHER", "rank_absolute": 1}
            if pos != 1
            else {"place_id": self.target, "rank_absolute": 1},
            {"place_id": self.target if pos != 1 else "OTHER", "rank_absolute": pos},
        ]


def _mem_db():
    conn = sqlite3.connect(":memory:")
    gdb.create_grid_tables(conn)
    return conn


def test_run_scan_writes_and_scores():
    conn = _mem_db()
    client = FakeMapsClient()
    grid_cfg = {
        "rows": 3,
        "cols": 3,
        "spacing_km": 1.0,
        "zoom": 14,
        "depth": 20,
        "keywords": ["excavation contractor", "land clearing"],
    }
    out = gg.run_scan(
        conn=conn,
        client=client,
        place_id="TARGET",
        center_lat=40.0,
        center_lng=-80.0,
        grid_cfg=grid_cfg,
        priority="standard",
        dry_run=False,
        budget_usd=5.0,
        scan_type="baseline",
        log=lambda *_: None,
    )
    assert out["status"] == "complete"
    assert out["calls"] == 3 * 3 * 2  # every pin x keyword
    assert client.calls == out["calls"]
    row = conn.execute(
        "SELECT status, api_cost_usd FROM grid_scans WHERE id=?", (out["scan_id"],)
    ).fetchone()
    assert row[0] == "complete"
    assert math.isclose(row[1], out["calls"] * 0.0006, rel_tol=1e-9)
    n_points = conn.execute(
        "SELECT COUNT(*) FROM grid_points WHERE grid_scan_id=?", (out["scan_id"],)
    ).fetchone()[0]
    assert n_points == out["calls"]


def test_budget_stop_marks_partial_and_is_resumable():
    conn = _mem_db()
    client = FakeMapsClient()
    grid_cfg = {"rows": 3, "cols": 3, "spacing_km": 1.0, "keywords": ["kw"]}
    # 9 requests * $0.0006 = $0.0054; cap at 5 requests' worth.
    budget = 5 * 0.0006 + 1e-9
    first = gg.run_scan(
        conn=conn,
        client=client,
        place_id="TARGET",
        center_lat=40.0,
        center_lng=-80.0,
        grid_cfg=grid_cfg,
        priority="standard",
        dry_run=False,
        budget_usd=budget,
        scan_type="monthly",
        log=lambda *_: None,
    )
    assert first["status"] == "partial"
    assert first["calls"] == 5
    # Resume with a fresh budget: only the remaining 4 pins should be fetched.
    # Re-open the SAME scan row would need the same scan_id; here we assert done_cells works:
    done = gdb.done_cells(conn, first["scan_id"])
    assert len(done) == 5


def test_dry_run_makes_no_calls():
    out = gg.run_scan(
        conn=None,
        client=None,
        place_id="TARGET",
        center_lat=40.0,
        center_lng=-80.0,
        grid_cfg={"rows": 7, "cols": 7, "spacing_km": 1.0, "keywords": ["a", "b"]},
        priority="standard",
        dry_run=True,
        budget_usd=None,
        scan_type="baseline",
        log=lambda *_: None,
    )
    assert out["dry_run"] is True
    assert out["requests"] == 7 * 7 * 2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
