#!/usr/bin/env python3
"""
Tests for dataforseo.py maps_positions — local-pack rank extraction.
Pure function, no network.

    python tests/test_maps_rank.py     # standalone
    pytest tests/                       # with the suite
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from dataforseo import maps_positions

META = [
    ("Caprock Excavation", "caprock-excavation.com", ["Caprock Excavation", "CapRock Excavation"]),
    ("Bluffline Excavating", "blufflineexcavating.com", ["Bluffline Excavating"]),
]


def _item(title, domain=None, rank=None, pid="p", rating=5.0, votes=10):
    it = {"title": title, "place_id": pid, "rating": {"value": rating, "votes_count": votes}}
    if domain:
        it["domain"] = domain
    if rank:
        it["rank_absolute"] = rank
    return it


def test_domain_match_dot_boundary():
    items = [
        _item("Bluffline Excavating", "www.blufflineexcavating.com", 1),
        _item("Caprock Excavation", "caprock-excavation.com", 2),
    ]
    pos, _ = maps_positions(items, META)
    assert pos["Bluffline Excavating"] == 1
    assert pos["Caprock Excavation"] == 2


def test_lookalike_domain_rejected():
    items = [_item("Not Caprock", "notcaprock-excavation.com", 1)]
    pos, _ = maps_positions(items, META)
    assert pos["Caprock Excavation"] is None


def test_title_fallback_only_without_domain():
    """A listing with no website matches by word-boundary title."""
    items = [_item("CapRock Excavation", None, 3)]
    pos, _ = maps_positions(items, META)
    assert pos["Caprock Excavation"] == 3


def test_title_fallback_rejects_substring():
    items = [_item("Caprocker Excavations", None, 1)]
    pos, _ = maps_positions(items, META)
    assert pos["Caprock Excavation"] is None


def test_non_business_rows_skipped():
    """Rows without place_id/cid (ads, separators) don't count or rank."""
    items = [
        {"title": "Sponsored thing"},
        _item("Caprock Excavation", "caprock-excavation.com", None),
    ]
    pos, _ = maps_positions(items, META)
    assert pos["Caprock Excavation"] == 1


def test_leaders_top3_with_review_counts():
    items = [
        _item("Bluffline Excavating", "blufflineexcavating.com", 1, votes=24),
        _item("LandEx Earthworks", "landex.com", 2, votes=16),
        _item("Planet Excavation", "planetexcavation.com", 3, votes=3),
        _item("Caprock Excavation", "caprock-excavation.com", 4, votes=2),
    ]
    _, leaders = maps_positions(items, META)
    assert len(leaders) == 3
    assert leaders[0]["title"] == "Bluffline Excavating"
    assert leaders[0]["reviews"] == 24


def test_empty_pack_is_data():
    pos, leaders = maps_positions([], META)
    assert pos == {"Caprock Excavation": None, "Bluffline Excavating": None}
    assert leaders == []


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
