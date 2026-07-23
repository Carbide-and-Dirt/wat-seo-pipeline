#!/usr/bin/env python3
"""
Phase 6 tests - paid shortlist measurement (HLD FR-17, NFR-10).

NO network, NO spend: the paid APIs are reached through an injected client, so a
FakeClients stands in, and storage runs on a temp SQLite DB. Guards the parts that
matter for a money-spending tool: the dry-run cost math, the shortlist selection, the
per-lead measurement mapping, and - most importantly - the HARD budget cap.

    python tests/test_measure.py     # standalone PASS/FAIL
    pytest tests/
"""

import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import leads_db
import measure_shortlist as ms

TS = "2026-06-18T00:00:00+00:00"


class FakeClients:
    """Stand-in for the paid APIs - counts calls, returns canned data, spends nothing."""

    def __init__(self, serp_items=None, backlinks=None, ai=None):
        self.calls = {"serp": 0, "backlinks": 0, "ai": 0}
        self._serp = (
            serp_items if serp_items is not None else [{"domain": "x.com", "rank_absolute": 7}]
        )
        self._bl = backlinks if backlinks is not None else {"rank": 100, "backlinks": 10}
        self._ai = ai if ai is not None else ("an answer", ["https://x.com"])

    def serp(self, keyword, location):
        self.calls["serp"] += 1
        return self._serp

    def backlinks(self, domain):
        self.calls["backlinks"] += 1
        return self._bl

    def ai(self, query):
        self.calls["ai"] += 1
        return self._ai


def _db():
    conn = leads_db.connect(str(Path(tempfile.mkdtemp()) / "t.sqlite"))
    leads_db.init_db(conn)
    return conn


def _biz(conn, pid, reviews=0, state="TN", website=None):
    conn.execute(
        "INSERT INTO businesses (place_id, name, website, no_website, state_code, state_name, "
        "country, review_count, relevance, address) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            pid,
            f"Biz {pid}",
            website if website is not None else f"https://{pid}.com",
            0 if website != "" else 1,
            state,
            "Tennessee" if state == "TN" else state,
            "United States",
            reviews,
            "match",
            f"1 Rd, Nashville, {state} 37201, USA",
        ),
    )
    conn.commit()


def _enr(conn, pid, readiness, status="live"):
    leads_db.upsert_enrichment(
        conn, {"place_id": pid, "site_status": status, "readiness_score": readiness}, TS
    )


# ---- dry-run cost math ----


def test_per_lead_cost_and_estimate():
    assert ms.per_lead_cost(ms.DEFAULT_COSTS, ms.DEFAULT_PAID) == 0.003 + 0.02 + 0.005
    est = ms.estimate(10, ms.DEFAULT_COSTS, ms.DEFAULT_PAID)
    assert est["per_lead"] == 0.028 and est["total"] == 0.28
    assert est["low"] == round(0.28 * 0.8, 2) and est["high"] == round(0.28 * 1.2, 2)
    assert est["breakdown"]["backlinks"] == 0.2


def test_load_paid_config_merges_over_defaults():
    paid, costs = ms.load_paid_config({"paid": {"ai_runs": 3, "costs": {"serp": 0.01}}})
    assert paid["ai_runs"] == 3
    assert (
        costs["serp"] == 0.01 and costs["backlinks"] == ms.DEFAULT_COSTS["backlinks"]
    )  # merged, not replaced
    assert ms.per_lead_cost(costs, paid) == 0.01 + 0.02 + 3 * 0.005


# ---- shortlist selection ----


def test_shortlist_orders_by_readiness_and_excludes_no_site():
    conn = _db()
    _biz(conn, "weak", reviews=5)
    _enr(conn, "weak", 12)
    _biz(conn, "strong", reviews=99)
    _enr(conn, "strong", 2)
    _biz(conn, "nosite", website="")
    _enr(conn, "nosite", None, status="none")  # no readiness -> excluded
    got = [r["place_id"] for r in leads_db.shortlist_candidates(conn)]
    assert got == ["weak", "strong"]  # readiness 12 before 2; nosite excluded


def test_shortlist_region_and_resumable():
    conn = _db()
    _biz(conn, "tn1", reviews=5, state="TN")
    _enr(conn, "tn1", 8)
    _biz(conn, "ky1", reviews=5, state="KY")
    _enr(conn, "ky1", 9)
    assert [r["place_id"] for r in leads_db.shortlist_candidates(conn, state_codes={"tn"})] == [
        "tn1"
    ]
    leads_db.upsert_ranking(conn, {"place_id": "tn1", "est_cost": 0.02}, TS)
    assert [
        r["place_id"] for r in leads_db.shortlist_candidates(conn, state_codes={"tn"})
    ] == []  # measured -> skipped


def test_shortlist_explicit_place_ids_override():
    conn = _db()
    _biz(conn, "a")
    _enr(conn, "a", 1)
    _biz(conn, "b")
    _enr(conn, "b", 1)
    got = {r["place_id"] for r in leads_db.shortlist_candidates(conn, place_ids=["b"])}
    assert got == {"b"}


# ---- per-lead measurement ----


def test_measure_lead_maps_all_signals():
    lead = {
        "place_id": "p1",
        "name": "Example Co",
        "website": "https://example.com/contact",
        "address": "1 Rd, Nashville, TN 37201, USA",
        "state_code": "TN",
        "state_name": "Tennessee",
        "country": "United States",
    }
    fc = FakeClients(
        serp_items=[{"domain": "example.com", "rank_absolute": 3}],
        backlinks={"rank": 120, "backlinks": 45},
        ai=("The best is Example Co for sure", ["https://example.com/"]),
    )
    rec = ms.measure_lead(lead, fc, ms.DEFAULT_COSTS, ms.DEFAULT_PAID)
    assert rec["serp_rank"] == 3
    assert rec["domain_authority"] == 120 and rec["backlinks"] == 45
    assert rec["ai_mentioned"] == 1 and rec["ai_cited"] == 1
    assert rec["serp_keyword"] == "excavating contractor Nashville"
    assert rec["ai_engine"] == "perplexity:sonar"
    assert abs(rec["est_cost"] - 0.028) < 1e-9
    assert fc.calls == {"serp": 1, "backlinks": 1, "ai": 1}


def test_measure_lead_no_rank_no_citation():
    lead = {
        "place_id": "p2",
        "name": "Nowhere LLC",
        "website": "https://nowhere.com",
        "address": "1 Rd, Memphis, TN 38103, USA",
        "state_code": "TN",
        "state_name": "Tennessee",
        "country": "United States",
    }
    fc = FakeClients(
        serp_items=[{"domain": "competitor.com", "rank_absolute": 1}],
        backlinks={"rank": 5, "backlinks": 0},
        ai=("Some other companies", ["https://competitor.com"]),
    )
    rec = ms.measure_lead(lead, fc, ms.DEFAULT_COSTS, ms.DEFAULT_PAID)
    assert rec["serp_rank"] is None and rec["ai_mentioned"] == 0 and rec["ai_cited"] == 0


# ---- the hard budget cap (the critical guard) ----


def _seeded_leads(conn, n):
    """Seed n businesses (so the site_rankings FK holds) and return their lead dicts."""
    leads = []
    for i in range(n):
        _biz(conn, f"p{i}")
        leads.append(
            {
                "place_id": f"p{i}",
                "name": f"Co {i}",
                "website": f"https://c{i}.com",
                "address": "1 Rd, Nashville, TN 37201, USA",
                "state_code": "TN",
                "state_name": "Tennessee",
                "country": "United States",
            }
        )
    return leads


def test_budget_cap_stops_before_crossing():
    conn = _db()
    # per lead = 0.028; a $0.05 cap fits exactly ONE lead (a 2nd -> 0.056 > 0.05).
    measured, spent, stopped = ms.run_measurements(
        conn,
        _seeded_leads(conn, 5),
        FakeClients(),
        ms.DEFAULT_COSTS,
        ms.DEFAULT_PAID,
        budget=0.05,
        now=TS,
    )
    assert measured == 1 and stopped is True
    assert spent <= 0.05  # NEVER exceeds the cap
    assert leads_db.ranking_status(conn)["measured"] == 1


def test_budget_exact_fit_measures_two():
    conn = _db()
    measured, spent, stopped = ms.run_measurements(
        conn,
        _seeded_leads(conn, 5),
        FakeClients(),
        ms.DEFAULT_COSTS,
        ms.DEFAULT_PAID,
        budget=0.06,
        now=TS,
    )
    assert measured == 2 and spent <= 0.06  # 0.056 fits, a 3rd (0.084) would not


def test_no_budget_measures_all_and_persists():
    conn = _db()
    measured, spent, stopped = ms.run_measurements(
        conn,
        _seeded_leads(conn, 3),
        FakeClients(),
        ms.DEFAULT_COSTS,
        ms.DEFAULT_PAID,
        budget=None,
        now=TS,
    )
    assert measured == 3 and stopped is False
    assert leads_db.ranking_exists(conn, "p0") and leads_db.ranking_status(conn)["measured"] == 3


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_run_all())
