#!/usr/bin/env python3
"""
Tests for score_report.py truth-in-rendering helpers: places_cells (counts only
for validated matches) and not_measured_notes (limitations derived from which
inputs exist, so "not measured" can never silently read as "absent").

    python tests/test_report_states.py     # standalone
    pytest tests/                           # with the suite
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from score_report import places_cells, not_measured_notes


def test_cells_matched_shows_counts():
    rating, count, listing = places_cells(
        {
            "status": "matched",
            "source": "google_places",
            "matched_name": "Caprock Excavation",
            "rating": 5.0,
            "review_count": 2,
        }
    )
    assert (rating, count) == (5.0, 2)
    assert listing == "Caprock Excavation"


def test_cells_dataforseo_source_is_flagged():
    _, _, listing = places_cells(
        {
            "status": "matched",
            "source": "dataforseo_business_data",
            "matched_name": "Caprock Excavation",
            "rating": 5.0,
            "review_count": 2,
        }
    )
    assert "via DataForSEO" in listing


def test_cells_mismatch_withholds_counts():
    rating, count, listing = places_cells(
        {"status": "mismatch", "top_candidate": {"name": "Granite Peak Excavation LLC"}}
    )
    assert rating == "withheld" and count == "withheld"
    assert "MISMATCH" in listing


def test_cells_not_found_is_not_absence():
    rating, count, listing = places_cells({"status": "not_found_via_api"})
    assert rating == "not measured"
    assert "NOT proof of absence" in listing


def test_cells_legacy_shapes_still_render():
    """Pre-2026-07-15 records had no status field."""
    r, c, listing = places_cells({"matched_name": "Old Co", "rating": 4.8, "review_count": 12})
    assert (r, c) == (4.8, 12)
    r, c, listing = places_cells({"matched": None, "query": "x"})
    assert r == "not measured" and "hand-check" in listing


def test_notes_flag_every_missing_surface():
    notes = "\n".join(
        not_measured_notes(
            ai=None, cwv_rows=[], places=None, unproven=[], backlinks={}, serp=None, maps=None
        )
    )
    for surface in (
        "AI-visibility",
        "Core Web Vitals",
        "reviews",
        "Backlinks",
        "Organic SERP",
        "Local-pack",
    ):
        assert surface in notes, f"missing note for {surface}"


def test_notes_name_unproven_gbp_entities():
    notes = "\n".join(
        not_measured_notes(
            ai={},
            cwv_rows=[1],
            places={},
            unproven=["Caprock Excavation"],
            backlinks={"x": {}},
            serp={"kw": {}},
            maps={"kw": {}},
        )
    )
    assert "Caprock Excavation" in notes
    assert "NOT disproven" in notes


def test_notes_empty_when_everything_measured():
    notes = not_measured_notes(
        ai={"scoreboard": {}},
        cwv_rows=[1],
        places={},
        unproven=[],
        backlinks={"x": {}},
        serp={"kw": {}},
        maps={"kw": {}},
    )
    assert notes == []


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
