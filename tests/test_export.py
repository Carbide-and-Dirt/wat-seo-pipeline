#!/usr/bin/env python3
"""
Phase 3 tests - export shim (FR-11) + de-TN-ified normalize (FR-13).

Pure logic + temp-DB / temp-file round-trips (no network, no spend):
  - export_leads maps the store to the discover-JSON schema downstream expects
  - normalize parses state from ANY US/CA address and drops in_footprint when the
    config has no out_of_area_towns (national runs), keeping it for legacy local runs.

    python tests/test_export.py      # standalone PASS/FAIL
    pytest tests/
"""

import json
import sys
import tempfile
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(TOOLS))

import export_leads as ex
import leads_db
import normalize_prospects as norm


def _db():
    conn = leads_db.connect(str(Path(tempfile.mkdtemp()) / "t.sqlite"))
    leads_db.init_db(conn)
    return conn


def _biz(
    conn,
    pid,
    name,
    state,
    reviews,
    website="",
    no_web=1,
    relevance="match",
    address="123 Main St, Townville, TN 37000, USA",
):
    conn.execute(
        "INSERT INTO businesses (place_id, name, address, state_code, state_name, website, "
        "no_website, rating, review_count, primary_type, types_json, found_via_json, "
        "relevance, maps_url, phone) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            pid,
            name,
            address,
            state,
            "Tennessee" if state == "TN" else state,
            website,
            no_web,
            4.5,
            reviews,
            "general_contractor",
            json.dumps(["general_contractor"]),
            json.dumps(["Nashville, TN"]),
            relevance,
            f"https://maps.google.com/?cid={pid}",
            "(615) 555-0100",
        ),
    )
    conn.commit()


# ---- FR-11: export shim ----


def test_row_to_company_maps_schema_and_types():
    conn = _db()
    _biz(conn, "p1", "Joe Dirt Excavating", "TN", 40, website="https://joedirt.com", no_web=0)
    comp = ex.fetch_companies(conn)[0]
    assert comp["place_id"] == "p1"
    assert comp["website"] == "https://joedirt.com"
    assert comp["no_website"] is False  # int 0 -> bool
    assert comp["found_via"] == ["Nashville, TN"]  # json column decoded to list
    assert comp["types"] == ["general_contractor"]
    assert comp["relevance"] == "match" and comp["state_code"] == "TN"


def test_export_empty_website_becomes_null():
    conn = _db()
    _biz(conn, "p1", "No Site Septic", "TN", 10, website="", no_web=1)
    comp = ex.fetch_companies(conn)[0]
    assert comp["website"] is None and comp["no_website"] is True


def test_export_region_filter_and_order():
    conn = _db()
    _biz(conn, "tn1", "TN low", "TN", 5)
    _biz(conn, "tn2", "TN high", "TN", 90)
    _biz(conn, "ky1", "KY one", "KY", 50)
    assert [c["place_id"] for c in ex.fetch_companies(conn, {"tn"})] == [
        "tn2",
        "tn1",
    ]  # reviews desc
    assert [c["place_id"] for c in ex.fetch_companies(conn, {"ky"})] == ["ky1"]
    assert len(ex.fetch_companies(conn)) == 3  # all


# ---- FR-13: de-TN-ified address parsing ----


def test_parse_location_us():
    assert norm.parse_location("3744 Annex Ave b7, Nashville, TN 37209, USA") == ("nashville", "TN")
    assert norm.parse_location("100 Main St, Austin, TX 78701, USA") == ("austin", "TX")


def test_parse_location_canada():
    assert norm.parse_location("55 King St W, Toronto, ON M5H 1A1, Canada") == ("toronto", "ON")


def test_parse_location_picks_state_not_street_token():
    # 'St' / a 2-letter uppercase in the street line must not win over the real state.
    town, st = norm.parse_location("12 NE Industrial Rd, Kansas City, MO 64101, USA")
    assert st == "MO" and town == "kansas city"


def test_parse_location_none_when_unrecognized():
    assert norm.parse_location("somewhere with no state code") == ("", "")


# ---- FR-13: normalize adds state, drops in_footprint for national configs ----


def _run_normalize(companies, config):
    d = Path(tempfile.mkdtemp())
    disc, cfg, out = d / "disc.json", d / "cfg.json", d / "out.json"
    disc.write_text(json.dumps({"area": "X", "companies": companies}), encoding="utf-8")
    cfg.write_text(json.dumps(config), encoding="utf-8")
    argv = sys.argv
    sys.argv = ["normalize_prospects.py", str(disc), "--config", str(cfg), "--out", str(out)]
    try:
        norm.main()
    finally:
        sys.argv = argv
    return json.loads(out.read_text(encoding="utf-8"))["companies"]


def test_normalize_national_adds_state_no_footprint():
    companies = [
        {
            "name": "A Excavating",
            "address": "1 Rd, Memphis, TN 38103, USA",
            "website": "https://a.com",
            "review_count": 3,
        },
        {
            "name": "B Grading",
            "address": "2 Rd, Calgary, AB T2P 1J9, Canada",
            "website": "https://b.com",
            "review_count": 1,
        },
    ]
    out = _run_normalize(
        companies, {"vertical": "Excavating", "chains": []}
    )  # no out_of_area_towns
    states = {c["name"]: c["state"] for c in out}
    assert states["A Excavating"] == "TN" and states["B Grading"] == "AB"
    assert all("in_footprint" not in c for c in out)  # national: footprint concept dropped


def test_normalize_prefers_export_state_code():
    # When the export shim already carried state_code, trust it over the address.
    companies = [
        {
            "name": "C",
            "address": "no parseable state here",
            "state_code": "KY",
            "website": "https://c.com",
            "review_count": 1,
        }
    ]
    out = _run_normalize(companies, {"vertical": "x", "chains": []})
    assert out[0]["state"] == "KY"


def test_normalize_legacy_footprint_still_works():
    companies = [
        {
            "name": "D",
            "address": "9 Rd, Faraway, TN 37000, USA",
            "website": "https://d.com",
            "review_count": 1,
        }
    ]
    out = _run_normalize(
        companies, {"vertical": "x", "chains": [], "out_of_area_towns": ["faraway"]}
    )
    assert out[0]["in_footprint"] is False and "outside core footprint" in out[0]["notes"]


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
