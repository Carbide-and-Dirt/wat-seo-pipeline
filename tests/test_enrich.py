#!/usr/bin/env python3
"""
Phase 5 tests - free site enrichment (HLD FR-15, FR-16).

Pure-logic only: the agency/tech fingerprint runs on fixture HTML (no network), and
the storage helpers run on a temp SQLite DB (no Google, no spend). Run either way:

    python tests/test_enrich.py     # standalone PASS/FAIL
    pytest tests/

Guards the parts that silently mis-classify a lead: builder/tag/credit detection and
the DIY/self-managed/agency inference, plus the enrichment upsert + resumability query.
"""

import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import site_fingerprint as fp
import leads_db


# ---- FR-16: builder / tag / credit detection ----


def test_detect_builder_wix():
    html = '<html><head><script src="https://static.wixstatic.com/x.js"></script></head></html>'
    assert fp.detect_builder(html) == "Wix"


def test_detect_builder_wordpress():
    assert fp.detect_builder('<link href="/wp-content/themes/x/style.css">') == "WordPress"


def test_detect_builder_generator_meta_fallback():
    assert (
        fp.detect_builder('<meta name="generator" content="Joomla! 4.2">') == "Joomla"
    )  # marker hit
    assert (
        fp.detect_builder('<meta name="generator" content="Custom Stack 1.0">')
        == "Custom Stack 1.0"
    )


def test_detect_builder_none_for_plain_html():
    assert fp.detect_builder("<html><body><h1>Joe Excavating</h1></body></html>") is None


def test_detect_tags_finds_marketing_stack():
    html = (
        "<script src='https://www.googletagmanager.com/gtm.js?id=GTM-ABC'></script>"
        "<script src='https://cdn.callrail.com/companies/1/x.js'></script>"
        "<script>fbq('init','1');</script><script src='https://connect.facebook.net/x.js'></script>"
    )
    tags = fp.detect_tags(html)
    assert "Google Tag Manager" in tags and "CallRail" in tags and "Meta Pixel" in tags


def test_detect_google_ads():
    assert fp.detect_google_ads(
        "<script src='https://www.googleadservices.com/pagead/conversion.js'>"
    )
    assert not fp.detect_google_ads("<html><body>no ads here</body></html>")


def test_agency_credit_detected_and_external():
    html = (
        "<footer>Website designed by <a href='https://acme-digital.com'>Acme Digital</a></footer>"
    )
    cred = fp.detect_agency_credit(html, host="joesexcavating.com")
    assert cred and cred["domain"] == "acme-digital.com" and cred["strong"]


def test_agency_credit_ignores_platform_selfcredit():
    html = "<footer>Powered by <a href='https://wordpress.org'>WordPress</a></footer>"
    assert fp.detect_agency_credit(html, host="joesexcavating.com") is None


def test_agency_credit_ignores_link_to_own_site():
    html = "<footer>Built by <a href='https://joesexcavating.com/team'>our team</a></footer>"
    assert fp.detect_agency_credit(html, host="joesexcavating.com") is None


# ---- FR-16: management classification (DIY / self-managed / agency) ----


def test_classify_agency_credit_wins_high_confidence():
    status, conf, ev = fp.classify_management(
        "WordPress",
        ["Google Analytics"],
        {"label": "Acme", "domain": "acme.com", "strong": True},
        False,
    )
    assert status == "likely agency-managed" and conf == "high"


def test_classify_diy_builder_no_tags_is_unmanaged():
    status, conf, ev = fp.classify_management("Wix", [], None, False)
    assert status == "DIY / unmanaged"


def test_classify_diy_builder_with_callrail_is_self_managed():
    status, _, ev = fp.classify_management("Squarespace", ["CallRail"], None, False)
    assert status == "self-managed (active marketing)"
    assert any("CallRail" in e for e in ev)


def test_classify_custom_build_with_managed_tags_leans_agency():
    status, _, _ = fp.classify_management("WordPress", ["HubSpot"], None, False)
    assert status == "likely agency-managed"


def test_classify_unreachable_is_unknown():
    status, conf, _ = fp.classify_management(None, [], None, False, reachable=False)
    assert status == "unknown"


def test_fingerprint_integration_diy_wix():
    html = (
        "<html><head><script src='https://static.wixstatic.com/x.js'></script></head>"
        "<body><h1>Joe Excavating</h1></body></html>"
    )
    out = fp.fingerprint(html, host="joeexcavating.com")
    assert out["builder"] == "Wix" and out["mgmt_status"] == "DIY / unmanaged"
    assert out["agency_credit"] is None and out["google_ads"] is False


# ---- FR-15: enrichment storage (temp DB, no network) ----


def _db():
    d = tempfile.mkdtemp()
    conn = leads_db.connect(str(Path(d) / "t.sqlite"))
    leads_db.init_db(conn)
    return conn


def _seed_business(conn, place_id, website, state="TN", reviews=0):
    conn.execute(
        "INSERT INTO businesses (place_id, name, website, no_website, state_code, review_count, relevance) "
        "VALUES (?,?,?,?,?,?,?)",
        (place_id, f"Biz {place_id}", website, 0 if website else 1, state, reviews, "match"),
    )
    conn.commit()


def test_enrichment_tables_exist():
    conn = _db()
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"site_enrichment", "site_rankings"} <= names


def test_upsert_and_exists_and_replace():
    conn = _db()
    _seed_business(conn, "p1", "https://x.com")
    assert not leads_db.enrichment_exists(conn, "p1")
    leads_db.upsert_enrichment(
        conn,
        {"place_id": "p1", "mgmt_status": "DIY / unmanaged", "readiness_score": 5},
        "2026-06-18T00:00:00+00:00",
    )
    assert leads_db.enrichment_exists(conn, "p1")
    # replace (refresh) keeps one row, overwrites fields
    leads_db.upsert_enrichment(
        conn,
        {"place_id": "p1", "mgmt_status": "likely agency-managed", "readiness_score": 2},
        "2026-06-19T00:00:00+00:00",
    )
    rows = conn.execute(
        "SELECT mgmt_status, readiness_score FROM site_enrichment WHERE place_id='p1'"
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == "likely agency-managed" and rows[0][1] == 2


def test_leads_to_enrich_filters_and_skips_done():
    conn = _db()
    _seed_business(conn, "p1", "https://a.com", state="TN", reviews=10)
    _seed_business(conn, "p2", "https://b.com", state="KY", reviews=99)
    _seed_business(conn, "p3", "", state="TN")  # no website -> never enrichable
    # region filter
    tn = leads_db.leads_to_enrich(conn, state_codes={"tn"})
    assert [r["place_id"] for r in tn] == ["p1"]
    # most-reviewed first across all
    allp = leads_db.leads_to_enrich(conn)
    assert [r["place_id"] for r in allp] == ["p2", "p1"]
    # only_unenriched skips done ones
    leads_db.upsert_enrichment(conn, {"place_id": "p2"}, "2026-06-18T00:00:00+00:00")
    rest = leads_db.leads_to_enrich(conn, only_unenriched=True)
    assert [r["place_id"] for r in rest] == ["p1"]


def test_leads_to_reenrich_targets_only_failed_statuses():
    conn = _db()
    _seed_business(conn, "live1", "https://live.com", reviews=50)
    _seed_business(conn, "blk1", "https://blocked.com", reviews=80)
    _seed_business(conn, "unr1", "https://down.com", reviews=10)
    _seed_business(conn, "soc1", "https://facebook.com/x", reviews=99)
    ts = "2026-06-18T00:00:00+00:00"
    leads_db.upsert_enrichment(conn, {"place_id": "live1", "site_status": "live"}, ts)
    leads_db.upsert_enrichment(conn, {"place_id": "blk1", "site_status": "blocked"}, ts)
    leads_db.upsert_enrichment(conn, {"place_id": "unr1", "site_status": "unreachable"}, ts)
    leads_db.upsert_enrichment(conn, {"place_id": "soc1", "site_status": "social_only"}, ts)
    got = {r["place_id"] for r in leads_db.leads_to_reenrich(conn)}
    # blocked + unreachable retried; live and social_only (correctly classified) excluded
    assert got == {"blk1", "unr1"}
    # most-reviewed-first ordering preserved
    assert [r["place_id"] for r in leads_db.leads_to_reenrich(conn)] == ["blk1", "unr1"]


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
