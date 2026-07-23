"""
Tests for gbp_audit.py + gbp_diff.py — no network, no spend. Faked DataForSEO client
(test_geo_grid.py / test_measure.py style). Run standalone (`python tests/test_gbp_audit.py`)
or via `pytest tests/`.
"""

import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gbp_audit as ga
import gbp_diff as gd
import leads_db_gbp as gdb

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


# --------------------------- extraction --------------------------- #
def test_extract_info_full():
    items = [
        {
            "place_id": "TARGET",
            "is_claimed": True,
            "rating": {"value": 4.6, "votes_count": 32},
            "category": "Excavating contractor",
            "additional_categories": ["Demolition contractor", "Land clearing"],
            "description": "We dig.",
            "total_photos": 25,
            "work_time": {"work_hours": {}},
            "attributes": {
                "available_attributes": {"service_options": ["a", "b"], "x": ["c"]},
                "unavailable_attributes": {"y": ["d"]},
            },
            "rating_distribution": {"1": 5, "2": 3, "3": 2, "4": 6, "5": 16},
        }
    ]
    info = ga.extract_info(items, expected_place_id="TARGET")
    assert info["found"] and info["is_claimed"] == 1
    assert info["rating_value"] == 4.6 and info["rating_votes"] == 32
    assert info["additional_categories_count"] == 2
    assert info["has_description"] == 1 and info["total_photos"] == 25 and info["has_hours"] == 1
    assert info["attr_available_count"] == 3 and info["attr_unavailable_count"] == 1
    assert info["neg_reviews"] == 8  # 5 one-star + 3 two-star


def test_extract_info_empty_and_unclaimed():
    assert ga.extract_info([])["found"] is False
    info = ga.extract_info([{"place_id": "X", "is_claimed": False, "description": ""}])
    assert info["is_claimed"] == 0 and info["has_description"] == 0


def test_extract_updates_recency():
    items = [
        {"timestamp": "2026-06-01 09:00:00 +00:00"},
        {"timestamp": "2025-01-10 12:00:00 +00:00"},
    ]
    u = ga.extract_updates(items, NOW)
    assert u["post_count"] == 2 and u["days_since_post"] == 33  # newest 2026-06-01 09:00 -> 33d 15h
    empty = ga.extract_updates([], NOW)
    assert empty["post_count"] == 0 and empty["days_since_post"] is None


# --------------------------- neglect score --------------------------- #
def test_neglect_score_all_fire_caps_at_60():
    # Every signal fires, but 'unclaimed' is weightless (SEC-D fix): 15+10+10+8+7+5+5 = 60.
    fields = {
        "is_claimed": 0,
        "post_count": 0,
        "rating_votes": 2,
        "total_photos": 1,
        "additional_categories_count": 0,
        "has_hours": 0,
        "attr_available_count": 0,
        "has_description": 0,
        "days_since_post": None,
    }
    score, signals = ga.neglect_score(fields)
    assert score == 60.0
    assert "unclaimed" not in signals  # zero-weight signal never enters the set
    assert signals["stale_posts"] and signals["few_photos"]


def test_neglect_score_only_unclaimed_scores_zero():
    # An otherwise-healthy profile that DataForSEO merely reports as unclaimed must NOT rank
    # as neglected — this is the is_claimed contamination fix.
    fields = {
        "is_claimed": 0,
        "post_count": 5,
        "days_since_post": 10,
        "rating_votes": 80,
        "total_photos": 40,
        "additional_categories_count": 3,
        "has_hours": 1,
        "attr_available_count": 12,
        "has_description": 1,
    }
    score, signals = ga.neglect_score(fields)
    assert score == 0.0 and signals == {}


def test_neglect_score_healthy_profile_is_zero():
    fields = {
        "is_claimed": 1,
        "post_count": 5,
        "days_since_post": 10,
        "rating_votes": 80,
        "total_photos": 40,
        "additional_categories_count": 3,
        "has_hours": 1,
        "attr_available_count": 12,
        "has_description": 1,
    }
    score, signals = ga.neglect_score(fields)
    assert score == 0.0 and signals == {}


def test_neglect_score_unknown_fields_do_not_fire():
    # All unknown (None) + no post data -> nothing fires, score 0 (missing data never inflates).
    score, signals = ga.neglect_score({"post_count": None})
    assert score == 0.0 and signals == {}


def test_stale_posts_by_age():
    old = ga.neglect_score({"post_count": 3, "days_since_post": 400})
    assert old[1].get("stale_posts") is True
    fresh = ga.neglect_score({"post_count": 3, "days_since_post": 20})
    assert "stale_posts" not in fresh[1]


# --------------------------- cost estimate --------------------------- #
def test_estimate_cost():
    with_upd = ga.estimate_cost(1000, "standard", with_updates=True)
    assert math.isclose(with_upd["per_prospect_usd"], 0.0015 + 0.0015 + 0.00075, rel_tol=1e-9)
    info_only = ga.estimate_cost(1000, "standard", with_updates=False)
    assert math.isclose(info_only["per_prospect_usd"], 0.0015, rel_tol=1e-9)
    assert with_upd["usd"] > info_only["usd"]


# --------------------------- end-to-end w/ fake client --------------- #
class FakeGbpClient:
    """Deterministic. Every prospect looks neglected; info+updates each report a cost."""

    def __init__(self, info_cost=0.0015, upd_cost=0.00225):
        self.info_cost, self.upd_cost = info_cost, upd_cost
        self.info_calls = self.upd_calls = 0

    def fetch_info(self, place_id, lat, lng, priority):
        self.info_calls += 1
        return {
            "items": [
                {
                    "place_id": place_id,
                    "is_claimed": False,
                    "rating": {"value": 3.9, "votes_count": 4},
                    "total_photos": 2,
                    "additional_categories": [],
                    "work_time": None,
                }
            ],
            "cost": self.info_cost,
        }

    def fetch_updates(self, place_id, lat, lng, priority):
        self.upd_calls += 1
        return {"items": [], "cost": self.upd_cost}  # no posts -> stale


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    gdb.create_gbp_tables(conn)
    return conn


def _leads(n):
    return [{"place_id": f"P{i}", "name": f"Biz {i}", "lat": 40.0, "lng": -80.0} for i in range(n)]


def test_run_audit_writes_and_scores():
    conn, client = _mem_db(), FakeGbpClient()
    out = ga.run_audit(
        conn=conn,
        client=client,
        leads=_leads(3),
        audit_type="prospect",
        priority="standard",
        with_updates=True,
        dry_run=False,
        budget_usd=5.0,
        now=NOW,
        log=lambda *_: None,
    )
    assert out["status"] == "complete" and out["audited"] == 3
    assert client.info_calls == 3 and client.upd_calls == 3
    assert math.isclose(out["cost_usd"], 3 * (0.0015 + 0.00225), abs_tol=5e-5)  # cost_usd is 4dp
    rows = conn.execute("SELECT neglect_score, is_claimed, status FROM gbp_audits").fetchall()
    assert len(rows) == 3
    assert all(
        r["status"] == "complete" and r["is_claimed"] == 0 and r["neglect_score"] > 0 for r in rows
    )


def test_run_audit_budget_stops_partial():
    conn, client = _mem_db(), FakeGbpClient()
    per = ga.estimate_cost(1, "standard", True)["per_prospect_usd"]
    out = ga.run_audit(
        conn=conn,
        client=client,
        leads=_leads(5),
        audit_type="prospect",
        priority="standard",
        with_updates=True,
        dry_run=False,
        budget_usd=2 * per + 1e-9,
        now=NOW,
        log=lambda *_: None,
    )
    assert out["status"] == "partial" and out["audited"] == 2


def test_run_audit_no_updates_skips_posts_endpoint():
    conn, client = _mem_db(), FakeGbpClient()
    ga.run_audit(
        conn=conn,
        client=client,
        leads=_leads(2),
        audit_type="prospect",
        priority="standard",
        with_updates=False,
        dry_run=False,
        budget_usd=5.0,
        now=NOW,
        log=lambda *_: None,
    )
    assert client.upd_calls == 0 and client.info_calls == 2


def test_dry_run_makes_no_calls():
    conn, client = _mem_db(), FakeGbpClient()
    out = ga.run_audit(
        conn=conn,
        client=client,
        leads=_leads(4),
        audit_type="prospect",
        priority="standard",
        with_updates=True,
        dry_run=True,
        budget_usd=None,
        now=NOW,
        log=lambda *_: None,
    )
    assert out["dry_run"] is True and out["prospects"] == 4
    assert client.info_calls == 0 and client.upd_calls == 0


def test_skip_existing_avoids_respend():
    conn, client = _mem_db(), FakeGbpClient()
    # Seed one audit for P0, then re-run with a wide skip window.
    gdb.insert_gbp_audit(
        conn,
        place_id="P0",
        audit_type="prospect",
        fields={"is_claimed": 0},
        neglect_score=40.0,
        signals={"unclaimed": True},
        api_cost_usd=0.0037,
        status="complete",
    )
    out = ga.run_audit(
        conn=conn,
        client=client,
        leads=_leads(2),
        audit_type="prospect",
        priority="standard",
        with_updates=True,
        dry_run=False,
        budget_usd=5.0,
        skip_since_ts="2000-01-01T00:00:00+00:00",
        now=NOW,
        log=lambda *_: None,
    )
    assert out["skipped"] == 1 and out["audited"] == 1  # P0 skipped, P1 audited


# --------------------------- batch (bulk submit/collect) ------------- #
class FakeBatchClient:
    INFO = "/v3/business_data/google/my_business_info"

    def submit_batch(self, prefix, leads, priority, chunk=100):
        return {ld["place_id"]: f"tid-{ld['place_id']}" for ld in leads}, 0.0

    def collect_batch(self, prefix, submitted, deadline_s=2400, poll_interval=15, log=print):
        results = {
            pid: {
                "items": [
                    {
                        "place_id": pid,
                        "is_claimed": False,
                        "rating": {"value": 3.9, "votes_count": 4},
                        "total_photos": 1,
                        "rating_distribution": {"1": 8, "2": 2, "5": 10},
                    }
                ],
                "cost": 0.0,
            }
            for pid in submitted
        }
        return results, {}


def test_run_audit_batch_writes_and_scores():
    conn, client = _mem_db(), FakeBatchClient()
    out = ga.run_audit_batch(
        conn=conn,
        client=client,
        leads=_leads(5),
        audit_type="prospect",
        priority="standard",
        dry_run=False,
        budget_usd=5.0,
        now=NOW,
        log=lambda *_: None,
    )
    assert out["submitted"] == 5 and out["collected"] == 5 and out["counts"]["complete"] == 5
    assert math.isclose(out["cost_usd"], 5 * 0.0015, abs_tol=5e-5)
    rows = conn.execute("SELECT neg_reviews, is_claimed, neglect_score FROM gbp_audits").fetchall()
    assert all(
        r["neg_reviews"] == 10 and r["is_claimed"] == 0 and r["neglect_score"] > 0 for r in rows
    )


def test_run_audit_batch_budget_caps_submission():
    conn, client = _mem_db(), FakeBatchClient()
    # Standard info = $0.0015/task; budget for 3 -> only 3 submitted.
    out = ga.run_audit_batch(
        conn=conn,
        client=client,
        leads=_leads(10),
        audit_type="prospect",
        priority="standard",
        dry_run=False,
        budget_usd=3 * 0.0015 + 1e-9,
        now=NOW,
        log=lambda *_: None,
    )
    assert out["submitted"] == 3 and out["counts"]["complete"] == 3


# --------------------------- diff (before/after) --------------------- #
def test_diff_audits_shows_improvement():
    before = {
        "id": 1,
        "place_id": "P",
        "audited_ts": "2026-01-01T00:00:00+00:00",
        "is_claimed": 0,
        "rating_votes": 4,
        "total_photos": 2,
        "additional_categories_count": 0,
        "attr_available_count": 1,
        "post_count": 0,
        "has_hours": 0,
        "has_description": 0,
        "neglect_score": 90.0,
        "signals_json": '{"unclaimed": true, "no_hours": true, "few_photos": true}',
    }
    after = {
        "id": 9,
        "place_id": "P",
        "audited_ts": "2026-06-01T00:00:00+00:00",
        "is_claimed": 1,
        "rating_votes": 22,
        "total_photos": 18,
        "additional_categories_count": 2,
        "attr_available_count": 8,
        "post_count": 4,
        "has_hours": 1,
        "has_description": 1,
        "neglect_score": 10.0,
        "signals_json": '{"few_photos": false}',
    }
    d = gd.diff_audits(before, after)
    assert d["neglect"]["improvement"] == 80.0  # 90 - 10, lower is better
    reviews = next(c for c in d["changes"] if c["field"] == "rating_votes")
    assert reviews["change"] == 18
    assert "Profile claimed" not in d["flips"]  # SEC-D: claim status never customer-facing
    assert "Hours set" in d["flips"] and "Description added" in d["flips"]
    assert "unclaimed" not in d["resolved_signals"] and "no_hours" in d["resolved_signals"]
    assert "GBP" in gd.render_markdown(d) or "Business Profile" in gd.render_markdown(d)


def test_diff_never_states_claim_status_secd():
    # SEC-D: even when is_claimed flips 0->1 and the baseline fired 'unclaimed', no customer-
    # facing output may assert claim status — not the structured flips, not resolved signals,
    # not the rendered markdown.
    before = {
        "place_id": "P",
        "is_claimed": 0,
        "neglect_score": 60.0,
        "signals_json": '{"unclaimed": true, "few_photos": true}',
        "audited_ts": "2026-01-01T00:00:00+00:00",
    }
    after = {
        "place_id": "P",
        "is_claimed": 1,
        "neglect_score": 10.0,
        "signals_json": "{}",
        "audited_ts": "2026-06-01T00:00:00+00:00",
    }
    d = gd.diff_audits(before, after)
    assert all("claim" not in f.lower() for f in d["flips"])
    assert "unclaimed" not in d["resolved_signals"]
    md = gd.render_markdown(d).lower()
    assert "claim" not in md and "unclaimed" not in md


def test_rescore_zeroes_unclaimed_and_is_idempotent():
    import rescore_gbp_audits as rs

    conn = _mem_db()
    # A snapshot written under the OLD weighting: unclaimed fired and counted 40 of a 90 score.
    gdb.insert_gbp_audit(
        conn,
        place_id="P",
        audit_type="prospect",
        fields={
            "is_claimed": 0,
            "rating_votes": 80,
            "total_photos": 40,
            "additional_categories_count": 3,
            "has_hours": 1,
            "attr_available_count": 12,
            "has_description": 1,
            "post_count": 5,
            "days_since_post": 10,
        },
        neglect_score=40.0,
        signals={"unclaimed": True},
        api_cost_usd=0.0,
        status="complete",
    )
    first = rs.rescore(conn, apply=True, log=lambda *_: None)
    assert first["changed"] == 1
    row = conn.execute("SELECT neglect_score, signals_json FROM gbp_audits").fetchone()
    assert row[0] == 0.0 and "unclaimed" not in json.loads(row[1])  # claim status scores nothing
    second = rs.rescore(conn, apply=True, log=lambda *_: None)  # idempotent
    assert second["changed"] == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
