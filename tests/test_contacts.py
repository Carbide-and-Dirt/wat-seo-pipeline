#!/usr/bin/env python3
"""
Tests for scrape_leads.py - DB-integrated contact scrape (email/owner).

No network: scrape_contacts.fetch is monkeypatched to serve fixture HTML, and
storage runs on a temp SQLite DB. Guards the best-email pick, the extraction wiring
(reusing scrape_contacts), and the resumable selection/storage.

    python tests/test_contacts.py     # standalone PASS/FAIL
    pytest tests/
"""

import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import leads_db
import scrape_contacts as sc
import scrape_leads as sl

TS = "2026-06-19T00:00:00+00:00"


def _db():
    conn = leads_db.connect(str(Path(tempfile.mkdtemp()) / "t.sqlite"))
    leads_db.init_db(conn)
    return conn


def _biz(conn, pid, website, state="TN", reviews=0):
    conn.execute(
        "INSERT INTO businesses (place_id, name, website, no_website, state_code, review_count, relevance) "
        "VALUES (?,?,?,?,?,?,?)",
        (pid, f"Biz {pid}", website, 0 if website else 1, state, reviews, "match"),
    )
    conn.commit()


# ---- best_email pick ----


def test_best_email_prefers_own_domain_then_role_mailbox():
    emails = sorted(["joe@gmail.com", "info@acme.com", "sales@acme.com"])
    assert sl.best_email(emails, "www.acme.com") == "info@acme.com"  # own-domain + role mailbox
    assert sl.best_email(sorted(["bob@acme.com"]), "acme.com") == "bob@acme.com"
    assert sl.best_email([], "acme.com") is None


# ---- extraction wiring (fixture HTML via monkeypatched fetch) ----


def test_scrape_lead_extracts_email_and_owner(monkeypatch=None):
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"LocalBusiness","name":"Acme","founder":{"@type":"Person","name":"Jane Acme"}}'
        "</script></head><body>"
        '<a href="mailto:info@acme.com">email</a> call (615) 555-1212</body></html>'
    )
    orig = sc.fetch
    sc.fetch = lambda url: html if url.rstrip("/").endswith("acme.com") else None
    try:
        rec = sl.scrape_lead({"place_id": "p1", "website": "https://acme.com"})
    finally:
        sc.fetch = orig
    assert rec["email"] == "info@acme.com"
    assert rec["owner_name"] == "Jane Acme"
    assert "(615) 555-1212" in __import__("json").loads(rec["extra_phones_json"])
    assert rec["pages_checked"] >= 1


def test_scrape_lead_empty_when_nothing_found():
    orig = sc.fetch
    sc.fetch = lambda url: "<html><body>no contacts here</body></html>"
    try:
        rec = sl.scrape_lead({"place_id": "p2", "website": "https://blank.com"})
    finally:
        sc.fetch = orig
    assert rec["email"] is None and rec["owner_name"] is None


# ---- storage + resumable selection ----


def test_leads_to_scrape_filters_and_skips_done():
    conn = _db()
    _biz(conn, "a", "https://a.com", state="TN", reviews=10)
    _biz(conn, "b", "https://b.com", state="FL", reviews=99)
    _biz(conn, "c", "", state="TN")  # no website -> never scrapable
    assert [r["place_id"] for r in leads_db.leads_to_scrape(conn, state_codes={"tn"})] == ["a"]
    assert [r["place_id"] for r in leads_db.leads_to_scrape(conn)] == ["b", "a"]  # reviews desc
    assert {r["place_id"] for r in leads_db.leads_to_scrape(conn, place_ids=["a", "c"])} == {
        "a"
    }  # c has no site
    leads_db.upsert_contact(conn, {"place_id": "b", "email": "x@b.com"}, TS)
    assert [r["place_id"] for r in leads_db.leads_to_scrape(conn, only_unscraped=True)] == ["a"]


def test_upsert_and_contact_status():
    conn = _db()
    _biz(conn, "a", "https://a.com")
    assert not leads_db.contact_exists(conn, "a")
    leads_db.upsert_contact(conn, {"place_id": "a", "email": "info@a.com", "owner_name": "Sam"}, TS)
    assert leads_db.contact_exists(conn, "a")
    s = leads_db.contact_status(conn)
    assert s["scraped"] == 1 and s["with_email"] == 1 and s["with_owner"] == 1
    # replace overwrites in place (one row)
    leads_db.upsert_contact(conn, {"place_id": "a", "email": "", "owner_name": None}, TS)
    s2 = leads_db.contact_status(conn)
    assert s2["scraped"] == 1 and s2["with_email"] == 0


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
