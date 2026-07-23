#!/usr/bin/env python3
"""
Phase 1 tests for prospect_sweep (HLD FR-1, FR-2, FR-4, FR-5, FR-7, FR-8).

Pure-logic only - no Google calls, no network, no GeoNames download. Run either way:

    python tests/test_prospect_sweep.py     # standalone PASS/FAIL
    pytest tests/

Covers the parts that silently mis-scope or mis-price a sweep: region parsing /
filtering, density ordering, the cost model's monotonicity and arithmetic, budget
reach, and the master-store schema + place_id dedup key.
"""

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import cell_planner as cp
import prospect_sweep as ps
import places_discover as pd
import leads_db

ps.TOKEN_RETRY_WAITS = (0, 0, 0)  # no real backoff in tests


def _place(name, code, pop, lat=35.0, lng=-86.0, country="US", state_name=""):
    return cp.Place(
        name=name,
        state_code=code,
        state_name=state_name or code,
        country=country,
        lat=lat,
        lng=lng,
        population=pop,
    )


def _cells(pops):
    """Synthetic population-ordered cells for cost-model tests."""
    return cp.plan_cells([_place(f"P{p}", "TN", p) for p in pops])


# ---- FR-1: region parsing & filtering ----


def test_parse_region_all():
    assert cp.parse_region("all")["type"] == "all"
    assert cp.parse_region("")["type"] == "all"


def test_parse_region_codes():
    r = cp.parse_region("TN, KY VA")
    assert r["type"] == "codes"
    assert r["codes"] == {"tn", "ky", "va"}


def test_parse_region_bbox():
    r = cp.parse_region("bbox:34,-90,37,-82")
    assert r["type"] == "bbox"
    assert r["bounds"] == (34.0, -90.0, 37.0, -82.0)


def test_filter_region_by_code():
    places = [
        _place("Nashville", "TN", 700000),
        _place("Louisville", "KY", 600000),
        _place("Atlanta", "GA", 500000),
    ]
    out = cp.filter_region(places, cp.parse_region("TN KY"))
    assert {p.state_code for p in out} == {"TN", "KY"}


def test_filter_region_by_name():
    places = [_place("Nashville", "TN", 700000, state_name="Tennessee")]
    assert len(cp.filter_region(places, cp.parse_region("tennessee"))) == 1


def test_filter_region_bbox():
    inside = _place("In", "TN", 1000, lat=35.5, lng=-86.5)
    outside = _place("Out", "TX", 1000, lat=31.0, lng=-97.0)
    out = cp.filter_region([inside, outside], cp.parse_region("bbox:34,-90,37,-82"))
    assert [p.name for p in out] == ["In"]


# ---- FR-2: radius bands ----


def test_radius_monotonic_nondecreasing():
    pops = [500, 4000, 20000, 80000, 400000, 2_000_000]
    radii = [cp.radius_for(p) for p in pops]
    assert radii == sorted(radii)
    assert radii[0] == 8_000 and radii[-1] == cp.RADIUS_MAX


# ---- FR-4: subdivision factors (cost model) ----


def test_subdivision_small_town_is_one():
    assert cp.expected_subdivision(1500) == 1
    assert cp.high_subdivision(1500) == 1


def test_subdivision_metro_grows_and_high_ge_expected():
    for pop in (50_000, 300_000, 1_000_000):
        assert cp.high_subdivision(pop) >= cp.expected_subdivision(pop)
    assert cp.expected_subdivision(1_000_000) == cp.SUBDIV_EXPECTED_MAX


# ---- FR-1: density ordering & stable cell ids ----


def test_plan_cells_population_ordered():
    cells = cp.plan_cells([_place("a", "TN", 100), _place("b", "TN", 900), _place("c", "TN", 500)])
    assert [c.population for c in cells] == [900, 500, 100]


def test_cell_id_stable_and_deterministic():
    p = _place("Nashville", "TN", 700000, lat=36.16589, lng=-86.78444)
    a = cp.plan_cells([p])[0].cell_id
    b = cp.plan_cells([p])[0].cell_id
    assert a == b and a.startswith("TN|Nashville|")


# ---- FR-12 / FR-5: query count & cost model ----


def test_queries_per_cell_counts_phrases():
    cfg = {
        "trade_queries": [{"bucket": "x", "queries": ["a", "b"]}, {"bucket": "y", "queries": ["c"]}]
    }
    total, breakdown = ps.queries_per_cell(cfg)
    assert total == 3
    assert breakdown == [("x", 2), ("y", 1)]


def test_estimate_monotonic_low_expected_high():
    cells = _cells([2_000_000, 300_000, 5000, 1200])
    req, cost = ps.estimate(cells, q_per_cell=7, rate=35.0)
    assert req["low"] <= req["expected"] <= req["high"]
    assert cost["low"] <= cost["expected"] <= cost["high"]


def test_estimate_cost_is_requests_over_thousand_times_rate():
    cells = _cells([1200, 1300, 1400])  # all tiny -> subdivision 1, low == cells*q
    req, cost = ps.estimate(cells, q_per_cell=2, rate=40.0)
    assert req["low"] == 3 * 2  # 3 cells * 2 queries * PAGE_LOW(1.0)
    assert abs(cost["low"] - (req["low"] / 1000.0 * 40.0)) < 1e-9


def test_estimate_rounds_requests_up():
    # 1 cell * 1 query * PAGE_EXPECTED(1.4) = 1.4 -> must round UP to 2 (never under-quote)
    req, _ = ps.estimate(_cells([1200]), q_per_cell=1, rate=35.0)
    assert req["expected"] == 2


def test_budget_coverage_more_budget_covers_more():
    cells = _cells([2_000_000, 1_000_000, 50_000, 5000, 1200])
    few, _, _ = ps.budget_coverage(cells, 7, 35.0, budget=5.0)
    many, _, _ = ps.budget_coverage(cells, 7, 35.0, budget=500.0)
    assert many >= few


def test_budget_zero_covers_nothing():
    covered, floor, spent = ps.budget_coverage(_cells([1_000_000, 1200]), 7, 35.0, budget=0.0)
    assert covered == 0 and floor is None and spent == 0.0


# ---- FR-7 / FR-8: master-store schema & dedup key ----


def test_db_init_creates_tables_and_is_idempotent():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "t.sqlite")
        conn = leads_db.connect(db)
        leads_db.init_db(conn)
        leads_db.init_db(conn)  # idempotent - must not raise
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {"businesses", "swept_cells", "runs"} <= names
        conn.close()


def test_db_status_zero_on_fresh():
    with tempfile.TemporaryDirectory() as d:
        conn = leads_db.connect(str(Path(d) / "t.sqlite"))
        leads_db.init_db(conn)
        s = leads_db.status(conn)
        assert s == {"businesses": 0, "no_website": 0, "states": 0, "swept_cells": 0, "runs": 0}
        conn.close()


def test_place_id_is_unique_dedup_key():
    with tempfile.TemporaryDirectory() as d:
        conn = leads_db.connect(str(Path(d) / "t.sqlite"))
        leads_db.init_db(conn)
        conn.execute("INSERT INTO businesses(place_id, name) VALUES ('pid1','A')")
        raised = False
        try:
            conn.execute("INSERT INTO businesses(place_id, name) VALUES ('pid1','B')")
        except sqlite3.IntegrityError:
            raised = True
        assert raised, "duplicate place_id must violate the primary key (FR-8 dedup)"
        conn.close()


def test_seed_loads_if_present():
    """Light integration check - only if build_seed.py has produced the CSV."""
    seed = TOOLS.parent / "data" / "places_us_ca.csv"
    if not seed.exists():
        return  # seed not built in this environment; skip silently
    places = cp.load_seed(str(seed))
    assert len(places) > 1000
    assert any(p.state_code == "TN" for p in places)


# ---- Phase 2: live sweep (FR-3, FR-4, FR-6, FR-8, FR-9, FR-10, FR-12) ----
# Fake Places clients so the engine is tested with NO network and NO spend.

NOW = "2026-06-18T00:00:00+00:00"

CFG = {
    "trade_queries": [
        {"bucket": "excavation", "queries": ["excavating contractor"]},
        {"bucket": "septic_underground", "queries": ["septic system service"]},
    ],
    "type_keywords": ["excavat", "septic"],
    "primary_types": ["general_contractor"],
    "adjacent_keywords": ["contractor"],
}


@contextlib.contextmanager
def _db():
    with tempfile.TemporaryDirectory() as d:
        conn = leads_db.connect(str(Path(d) / "t.sqlite"))
        leads_db.init_db(conn)
        try:
            yield conn
        finally:
            conn.close()


def _p(
    pid,
    name="Acme Excavating",
    website=None,
    types=("general_contractor",),
    primary="Excavating contractor",
):
    p = {
        "id": pid,
        "displayName": {"text": name},
        "formattedAddress": "1 Main St, Nashville, TN 37000, USA",
        "location": {"latitude": 36.0, "longitude": -86.0},
        "nationalPhoneNumber": "615-555-0100",
        "primaryTypeDisplayName": {"text": primary},
        "types": list(types),
        "googleMapsUri": "https://maps.example/?cid=1",
        "rating": 4.5,
        "userRatingCount": 12,
    }
    if website:
        p["websiteUri"] = website
    return p


def _cell(cid, pop=50_000, lat=36.0, lng=-86.0, radius=20_000, name="Town", state="TN"):
    return cp.Cell(
        cell_id=cid,
        lat=lat,
        lng=lng,
        radius_m=radius,
        population=pop,
        place_name=name,
        state_code=state,
        country="US",
    )


class OnePage:
    """Returns one fixed page (no pagination) for every search."""

    def __init__(self, places):
        self.places, self.calls = places, []

    def search(self, q, lat, lng, radius_m, token=None):
        self.calls.append((q, round(lat, 4), round(lng, 4)))
        return list(self.places), None


class AlwaysToken:
    """Always returns a page plus a next-page token -> every query looks saturated."""

    def __init__(self, places):
        self.places, self.calls = places, []

    def search(self, q, lat, lng, radius_m, token=None):
        self.calls.append((q, round(lat, 4), round(lng, 4)))
        return list(self.places), "TKN"


def test_budget_cap_is_hard_stop():
    with _db() as conn:
        cells = [_cell(f"c{i}", pop=50_000) for i in range(3)]  # up to 6 requests (2 buckets each)
        b = ps.Budget(dollars=None, max_requests=3)
        _, stop = ps.sweep(conn, OnePage([_p("p1")]), cells, CFG, b, "TN", False, NOW)
        assert b.requests == 3 and stop == "budget"


def test_budget_dollars_never_exceeded():
    with _db() as conn:
        cells = [_cell(f"c{i}", pop=5_000) for i in range(20)]
        b = ps.Budget(dollars=0.10, rate_per_1000=35.0)  # ~2 requests fit under $0.10
        ps.sweep(conn, OnePage([_p("p1")]), cells, CFG, b, "TN", False, NOW)
        assert b.cost() <= 0.10 + 1e-9


def test_dedup_one_row_merges_found_via():
    with _db() as conn:
        cells = [_cell("a", pop=5_000, name="Alpha"), _cell("b", pop=5_000, name="Beta")]
        b = ps.Budget(dollars=None, max_requests=100)
        counters, _ = ps.sweep(conn, OnePage([_p("dup")]), cells, CFG, b, "TN", False, NOW)
        n = conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        assert n == 1 and counters["inserted"] == 1 and counters["updated"] >= 1
        fv = conn.execute("SELECT found_via_json FROM businesses WHERE place_id='dup'").fetchone()[
            0
        ]
        assert set(json.loads(fv)) == {"Alpha", "Beta"}


def test_relevance_filters_out_unrelated():
    with _db() as conn:
        results = [
            _p("good", name="Acme Excavating", types=("general_contractor",)),
            _p("bad", name="Joe Pizza", types=("restaurant",), primary="Restaurant"),
        ]
        b = ps.Budget(dollars=None, max_requests=50)
        ps.sweep(conn, OnePage(results), [_cell("a", pop=5_000)], CFG, b, "TN", False, NOW)
        ids = [r[0] for r in conn.execute("SELECT place_id FROM businesses").fetchall()]
        assert ids == ["good"]


def test_general_contractor_is_review_tier_not_confirmed_match():
    """Regression (FR-12 config fix, 2026-06-18): Google's 'general_contractor' is a
    catch-all that plumbers, HVAC, concrete, foundation and tree firms all carry, so it
    must NOT alone confirm a lead. The national config keeps primary_types empty; a bare
    general_contractor with no trade keyword lands in the 'maybe' review tier (kept,
    flagged) - not 'match' (false confirm) and not 'other' (lost). Guards against a future
    edit re-adding general_contractor and re-flooding 'match' with off-trade businesses."""
    cfg = json.loads(
        (TOOLS.parent / "targets" / "excavating-national.json").read_text(encoding="utf-8")
    )
    assert "general_contractor" not in cfg["primary_types"]
    kw = [k.lower() for k in cfg["type_keywords"]]
    pt, adj = cfg["primary_types"], tuple(cfg["adjacent_keywords"])
    gc_only = pd.relevance(
        "AFS Foundation & Waterproofing", ["general_contractor"], "General Contractor", kw, pt, adj
    )
    trade = pd.relevance(
        "Acme Excavating", ["general_contractor"], "Excavating contractor", kw, pt, adj
    )
    unrelated = pd.relevance("Joe's Pizza", ["restaurant"], "Restaurant", kw, pt, adj)
    assert (gc_only, trade, unrelated) == ("maybe", "match", "other"), (gc_only, trade, unrelated)


def test_additive_skips_swept_then_refresh_reprocesses():
    with _db() as conn:
        cells = [_cell("a", pop=5_000)]
        ps.sweep(
            conn, OnePage([_p("p1")]), cells, CFG, ps.Budget(max_requests=50), "TN", False, NOW
        )
        # additive re-run: cell already swept -> skipped, client never called
        c2 = OnePage([_p("p1")])
        counters2, _ = ps.sweep(conn, c2, cells, CFG, ps.Budget(max_requests=50), "TN", False, NOW)
        assert c2.calls == [] and counters2["skipped"] >= 1 and counters2["inserted"] == 0
        # refresh re-run: re-queries and updates the mutable website field
        c3 = OnePage([_p("p1", website="https://x.com")])
        counters3, _ = ps.sweep(conn, c3, cells, CFG, ps.Budget(max_requests=50), "TN", True, NOW)
        assert c3.calls and counters3["updated"] >= 1
        assert (
            conn.execute("SELECT website FROM businesses WHERE place_id='p1'").fetchone()[0]
            == "https://x.com"
        )


def test_saturated_cell_subdivides_bounded_depth():
    with _db() as conn:
        client = AlwaysToken([_p("m1")])
        ps.sweep(
            conn,
            client,
            [_cell("base", pop=5_000, radius=16_000)],
            CFG,
            ps.Budget(max_requests=300),
            "TN",
            False,
            NOW,
        )
        swept = [r[0] for r in conn.execute("SELECT cell_id FROM swept_cells").fetchall()]
        assert any("/q" in s for s in swept)  # FR-4 children swept
        assert not any(s.count("/q") > cp.MAX_SUBDIV_DEPTH for s in swept)  # depth capped
        centers = {(la, ln) for (_q, la, ln) in client.calls}
        assert (36.0, -86.0) in centers and len(centers) > 1  # base + offset children


def test_overlap_pruning_drops_covered_small_town():
    big = _cell("big", pop=500_000, lat=36.0, lng=-86.0, radius=30_000)
    near = _cell("near", pop=3_000, lat=36.05, lng=-86.0, radius=8_000)  # ~5.5 km from big
    far = _cell("far", pop=3_000, lat=36.0, lng=-90.0, radius=8_000)  # ~360 km away
    ids = [c.cell_id for c in cp.prune_overlapping([big, near, far])]
    assert "big" in ids and "near" not in ids and "far" in ids


def test_query_depth_scales_with_town_size():
    small = cp.query_depth_for(2_000, CFG["trade_queries"])
    big = cp.query_depth_for(50_000, CFG["trade_queries"])
    assert [b["bucket"] for b in small] == ["excavation"]
    assert len(big) == 2


def test_to_record_no_website_flag():
    cell = _cell("a")
    with_site = ps._to_record(_p("x", website="https://y.com"), cell, "excavation", "match")
    without = ps._to_record(_p("z"), cell, "excavation", "match")
    assert with_site["no_website"] == 0 and with_site["website"] == "https://y.com"
    assert without["no_website"] == 1
    assert with_site["trade_bucket"] == "excavation" and with_site["found_via"] == ["Town"]


def test_sweep_stores_region_label_as_text():
    """The region passed into record_cell must be the text label, not a region spec."""
    with _db() as conn:
        ps.sweep(
            conn,
            OnePage([_p("p1")]),
            [_cell("a", pop=5_000)],
            CFG,
            ps.Budget(max_requests=10),
            "TN KY",
            False,
            NOW,
        )
        reg = conn.execute("SELECT region FROM swept_cells LIMIT 1").fetchone()[0]
        assert isinstance(reg, str) and reg == "TN KY"


def test_run_live_wiring_end_to_end_with_fake_client():
    """Full run_live path (seed -> plan -> prune -> sweep -> record) with an injected
    fake client. Regression for the region-spec-dict that crashed record_cell's SQL
    binding - a unit test that fed sweep a string region never exercised this wiring."""
    os.environ.setdefault(
        "GOOGLE_PLACES_API_KEY", "test-key"
    )  # present; fake client makes no calls
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "seed.csv").write_text(
            "name,state_code,state_name,country,lat,lng,population\n"
            "Nashville,TN,Tennessee,US,36.16,-86.78,700000\n"
            "Memphis,TN,Tennessee,US,35.14,-90.04,650000\n",
            encoding="utf-8",
        )
        (d / "cfg.json").write_text(json.dumps(CFG), encoding="utf-8")
        db = str(d / "leads.sqlite")
        args = argparse.Namespace(
            region="TN",
            config=str(d / "cfg.json"),
            seed=str(d / "seed.csv"),
            rate=35.0,
            budget=5.0,
            max_requests=None,
            refresh=False,
            no_prune=True,
            db=db,
        )
        rc = ps.run_live(args, client=OnePage([_p("p1")]))
        assert rc == 0
        conn = leads_db.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 1
        reg = conn.execute("SELECT region FROM swept_cells LIMIT 1").fetchone()[0]
        assert isinstance(reg, str) and reg == "TN"
        conn.close()


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


class _Requests:
    """Stand-in for the requests module: hands back queued responses, counts posts."""

    def __init__(self, responses):
        self.responses, self.posts = list(responses), 0

    def post(self, url, headers=None, json=None, timeout=None):
        self.posts += 1
        return self.responses.pop(0)


def test_places_client_retries_fresh_page_token_then_succeeds():
    """A just-issued page token can 400 until Google propagates it; the client backs off
    and retries rather than the caller pre-sleeping before every page (FR-3)."""
    fake = _Requests(
        [
            _Resp(
                400, {"error": {"status": "INVALID_ARGUMENT", "message": "page token not ready"}}
            ),
            _Resp(200, {"places": [{"id": "x"}], "nextPageToken": None}),
        ]
    )
    saved_req, saved_waits = ps.requests, ps.TOKEN_RETRY_WAITS
    ps.requests, ps.TOKEN_RETRY_WAITS = fake, (0, 0, 0)
    try:
        places, token = ps.GooglePlacesClient("k").search(
            "q", 36.0, -86.0, 10_000, page_token="TKN"
        )
    finally:
        ps.requests, ps.TOKEN_RETRY_WAITS = saved_req, saved_waits
    assert fake.posts == 2 and places == [{"id": "x"}] and token is None


def test_places_client_first_page_400_is_not_retried():
    """A 400 on the FIRST page (no token) is a real error, not propagation lag -> raise once."""
    fake = _Requests([_Resp(400, {"error": {"message": "bad request"}})])
    saved_req = ps.requests
    ps.requests = fake
    try:
        raised = False
        try:
            ps.GooglePlacesClient("k").search("q", 36.0, -86.0, 10_000, page_token=None)
        except RuntimeError:
            raised = True
        assert raised and fake.posts == 1
    finally:
        ps.requests = saved_req


def _run_standalone():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
