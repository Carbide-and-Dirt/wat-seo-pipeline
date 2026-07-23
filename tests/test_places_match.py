#!/usr/bin/env python3
"""
Tests for places_reviews.py match validation — the guard added after a broadened
query for the target business silently "matched" an unrelated company in a
different city of the same state, because the tool took results[0] unvalidated.

No network: validate_candidate / pick_match / _dfs_to_place are pure.

    python tests/test_places_match.py     # standalone
    pytest tests/                          # with the suite
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from places_reviews import validate_candidate, pick_match, _dfs_to_place

CFG = {"city": "Rockridge, CO", "serp_location": "Rockridge,Colorado,United States"}
CAPROCK = {
    "name": "Caprock Excavation",
    "aliases": ["CapRock Excavation", "CAPROCK Excavation LLC"],
}


def _place(name, addr=None, rating=None, count=None, pid="x"):
    return {
        "displayName": {"text": name},
        "formattedAddress": addr,
        "rating": rating,
        "userRatingCount": count,
        "id": pid,
    }


def test_rejects_different_company_regression():
    """A same-state company in a different city must not match the target."""
    ok, why = validate_candidate(
        _place("Granite Peak Excavation LLC", "412 40th Ln, Granite Flats, CO 81006, USA"),
        CAPROCK,
        CFG,
    )
    assert not ok
    assert "does not match" in why


def test_accepts_exact_name_no_address():
    """Service-area businesses hide their address; name match alone is valid."""
    ok, why = validate_candidate(_place("Caprock Excavation"), CAPROCK, CFG)
    assert ok
    assert "hides its address" in why


def test_accepts_alias_and_suffix():
    ok, _ = validate_candidate(
        _place("CAPROCK Excavation LLC", "Rockridge, CO 81301, USA"), CAPROCK, CFG
    )
    assert ok


def test_rejects_right_name_wrong_state():
    """A same-named business in another state is a different business."""
    ok, why = validate_candidate(
        _place("Caprock Excavation", "Kettle Creek, TX 78610, USA"), CAPROCK, CFG
    )
    assert not ok
    assert "outside the expected state" in why


def test_state_ok_town_unconfirmed():
    """In-state but different town stays accepted (SABs pin outside the city)."""
    ok, why = validate_candidate(
        _place("Caprock Excavation", "Elkford, CO 81322, USA"), CAPROCK, CFG
    )
    assert ok
    assert "town not confirmed" in why


def test_rejects_single_word_reverse_match():
    """A generic one-word listing name must not reverse-match the entity."""
    ok, _ = validate_candidate(_place("Excavation", "Rockridge, CO 81301, USA"), CAPROCK, CFG)
    assert not ok


def test_no_substring_name_match():
    """Word-boundary discipline: 'Caprocker Excavationworks' is not 'Caprock'."""
    ok, _ = validate_candidate(
        _place("Caprocker Excavationworks", "Rockridge, CO 81301, USA"), CAPROCK, CFG
    )
    assert not ok


def test_pick_match_skips_bad_first_result():
    """Never trust results[0]: the validated second candidate wins."""
    places = [
        _place("Granite Peak Excavation LLC", "Granite Flats, CO 81006, USA", 5.0, 14, pid="bad"),
        _place("Caprock Excavation", "Rockridge, CO 81301, USA", 5.0, 2, pid="good"),
    ]
    place, detail = pick_match(places, CAPROCK, CFG)
    assert place["id"] == "good"
    assert "town ok" in detail


def test_pick_match_none_when_nothing_validates():
    place, detail = pick_match(
        [_place("Granite Peak Excavation LLC", "Granite Flats, CO 81006, USA")], CAPROCK, CFG
    )
    assert place is None and detail is None


def test_dfs_item_converts_and_validates():
    """DataForSEO Business Data items flow through the same validator."""
    item = {
        "title": "Caprock Excavation",
        "address": "Rockridge, CO 81301",
        "place_id": "ChIJxyz",
        "rating": {"value": 5.0, "votes_count": 2},
    }
    place = _dfs_to_place(item)
    ok, _ = validate_candidate(place, CAPROCK, CFG)
    assert ok
    assert place["userRatingCount"] == 2
    assert place["id"] == "ChIJxyz"


def test_no_geo_config_falls_back_to_name_only():
    """Without city/serp_location there is no state expectation to enforce."""
    ok, _ = validate_candidate(_place("Caprock Excavation", "Anywhere, TX 75001, USA"), CAPROCK, {})
    assert ok


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
