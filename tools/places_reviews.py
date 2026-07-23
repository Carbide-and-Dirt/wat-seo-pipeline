#!/usr/bin/env python3
"""
places_reviews.py — VERIFIABLE Google review counts & ratings (Google Places API).

Kills the "review count is an unverified third-party snapshot" caveat that
dogged every manual report. Resolves each brand/competitor to its Google place
and returns the live rating + userRatingCount (and, with --reviews, recent
review snippets to read sentiment/recency).

Requires GOOGLE_PLACES_API_KEY in .env (Places API "New" enabled in Google
Cloud). https://developers.google.com/maps/documentation/places/web-service

Usage:
    python tools/places_reviews.py targets/<name>.json
    python tools/places_reviews.py targets/<name>.json --reviews       # also pull recent review snippets
    python tools/places_reviews.py targets/<name>.json --fallback off  # never spend DataForSEO credits

Output: .tmp/places/reviews.json — per entity, one of three explicit states:
    status "matched"           rating/review_count are usable (name+geo validated)
    status "mismatch"          the API returned listings but NONE matched this entity;
                               counts are withheld so a wrong business can't be reported
    status "not_found_via_api" no source returned the listing. This does NOT mean the
                               profile doesn't exist — hand-check Google Maps before
                               making any "no GBP" claim (learned 2026-07-15: a live,
                               local-pack-ranked profile was invisible to Text Search).

Match validation (never trust results[0]): the candidate's displayName must match
the entity name or an alias on word boundaries, and when the listing shows an
address it must sit in the config's expected state. Candidates are scanned in
order until one validates.

DataForSEO fallback (PAID, ~$0.005/entity, only fires on a Places miss/mismatch):
Business Data my_business_info reads Maps directly, independent of the Places
index, so it can see listings Text Search can't. Same validation applies.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import requests

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"
DFS_INFO_LIVE = "/v3/business_data/google/my_business_info/live"
DFS_FALLBACK_EST_USD = 0.0054  # my_business_info live rate (see gbp_audit.py INFO_RATE_USD)

from lib.common import load_env, utf8_stdout
from check_ai_visibility import mentioned  # tested word-boundary name/phrase matcher

utf8_stdout()


def entities(cfg):
    out = [cfg["brand"]] + cfg.get("competitors", [])
    return out


def query_for(entity, cfg):
    if entity.get("places_query"):
        return entity["places_query"]
    city = cfg.get("city") or cfg.get("market", "")
    return f"{entity['name']} {city}".strip()


def text_search(query, key):
    r = requests.post(
        SEARCH_URL,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.rating,places.userRatingCount",
        },
        json={"textQuery": query},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("places", [])


def get_reviews(place_id, key):
    r = requests.get(
        DETAILS_URL.format(place_id=place_id),
        headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "reviews"},
        timeout=30,
    )
    r.raise_for_status()
    out = []
    for rv in r.json().get("reviews", [])[:5]:
        out.append(
            {
                "rating": rv.get("rating"),
                "when": rv.get("relativePublishTimeDescription"),
                "text": (rv.get("text", {}) or {}).get("text", "")[:280],
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Match validation — the guard that keeps a wrong business out of the report.  #
# --------------------------------------------------------------------------- #
def _aliases(entity):
    return [entity["name"], *entity.get("aliases", [])]


def _expected_states(cfg):
    """State tokens from 'city' ('Durango, CO' -> 'co') and 'serp_location'
    ('Durango,Colorado,United States' -> 'colorado')."""
    toks = set()
    city = cfg.get("city") or ""
    if "," in city:
        toks.add(city.rsplit(",", 1)[1].strip().lower())
    parts = [p.strip().lower() for p in (cfg.get("serp_location") or "").split(",") if p.strip()]
    if len(parts) >= 2:
        toks.add(parts[1])
    return {t for t in toks if t}


def _name_matches(listing_name, entity):
    """Word-boundary match in either direction: entity name/alias inside the
    listing name, or a multi-word listing name inside the entity name."""
    if mentioned(listing_name, _aliases(entity)):
        return True
    return len(listing_name.split()) >= 2 and mentioned(entity["name"], [listing_name])


def validate_candidate(place, entity, cfg):
    """(ok, detail). Name is the primary gate; when the listing shows an address,
    it must sit in the expected state. No address is acceptable — service-area
    businesses hide theirs."""
    name = (place.get("displayName") or {}).get("text") or ""
    if not _name_matches(name, entity):
        return False, f"name '{name}' does not match entity"
    addr = place.get("formattedAddress") or ""
    if not addr:
        return True, "name ok; listing hides its address (common for service-area businesses)"
    states = _expected_states(cfg)
    if states and not mentioned(addr, sorted(states)):
        return False, f"name ok but address '{addr}' is outside the expected state"
    town = (cfg.get("city") or "").split(",")[0].strip()
    if town and mentioned(addr, [town]):
        return True, "name ok; town ok"
    return True, "name ok; state ok (town not confirmed)"


def pick_match(places, entity, cfg):
    """First candidate that validates, in API order. (place, detail) or (None, None)."""
    for place in places:
        ok, detail = validate_candidate(place, entity, cfg)
        if ok:
            return place, detail
    return None, None


def matched_record(place, query, detail, source):
    return {
        "status": "matched",
        "source": source,
        "query": query,
        "match_note": detail,
        "matched_name": (place.get("displayName") or {}).get("text"),
        "matched_address": place.get("formattedAddress"),
        "place_id": place.get("id"),
        "rating": place.get("rating"),
        "review_count": place.get("userRatingCount"),
    }


# --------------------------------------------------------------------------- #
# DataForSEO fallback — an index-independent second source (PAID, tiny).       #
# --------------------------------------------------------------------------- #
def _dfs_to_place(item):
    """Convert a Business Data my_business_info item to the Places candidate shape
    so one validator serves both sources."""
    rating = item.get("rating") or {}
    return {
        "displayName": {"text": item.get("title")},
        "formattedAddress": item.get("address"),
        "id": item.get("place_id"),
        "rating": rating.get("value"),
        "userRatingCount": rating.get("votes_count"),
    }


def dataforseo_lookup(entity, cfg):
    """Ask DataForSEO Business Data for the profile by name (validated the same way).
    Returns (place_or_None, note)."""
    import dataforseo as dfs

    auth = dfs.creds()
    if not auth:
        return None, "DataForSEO creds not set; fallback skipped"
    payload = {
        "keyword": entity["name"],
        "language_code": "en",
        "location_name": cfg.get("serp_location") or "United States",
    }
    try:
        result = dfs.post(DFS_INFO_LIVE, [payload], auth)
    except Exception as ex:  # noqa: BLE001
        return None, f"fallback error: {ex}"
    items = (result[0].get("items") or []) if result else []
    place, detail = pick_match([_dfs_to_place(it) for it in items], entity, cfg)
    if place:
        return place, detail
    if items:
        top = _dfs_to_place(items[0])
        return None, (
            f"fallback found only non-matching listings "
            f"(top: '{(top.get('displayName') or {}).get('text')}')"
        )
    return None, "fallback returned no results"


# --------------------------------------------------------------------------- #
# Per-entity resolution                                                        #
# --------------------------------------------------------------------------- #
def resolve_entity(entity, cfg, key, fallback):
    q = query_for(entity, cfg)
    try:
        places = text_search(q, key)
    except Exception as ex:  # noqa: BLE001
        return {"status": "error", "error": str(ex), "query": q}

    place, detail = pick_match(places, entity, cfg)
    if place:
        return matched_record(place, q, detail, "google_places")

    rec = {
        "status": "not_found_via_api",
        "query": q,
        "note": (
            "Places API returned no results. This does NOT mean no profile exists; "
            "hand-check Google Maps before claiming absence."
        ),
    }
    if places:
        _, why = validate_candidate(places[0], entity, cfg)
        top = places[0]
        rec = {
            "status": "mismatch",
            "query": q,
            "top_candidate": {
                "name": (top.get("displayName") or {}).get("text"),
                "address": top.get("formattedAddress"),
            },
            "note": f"Listings returned but none validated ({why}). Counts withheld.",
        }

    if fallback == "off":
        rec["fallback"] = "disabled"
        return rec
    fb_place, fb_note = dataforseo_lookup(entity, cfg)
    if fb_place:
        out = matched_record(fb_place, entity["name"], fb_note, "dataforseo_business_data")
        out["places_api_status"] = rec["status"]
        out["match_note"] += "; NOTE: invisible to Places Text Search, found via DataForSEO"
        return out
    rec["fallback"] = fb_note
    return rec


def main():
    ap = argparse.ArgumentParser(
        description="Verifiable Google review counts/ratings via Places API."
    )
    ap.add_argument("config")
    ap.add_argument(
        "--reviews",
        action="store_true",
        help="Also fetch up to 5 recent review snippets per entity.",
    )
    ap.add_argument(
        "--fallback",
        choices=["auto", "off"],
        default="auto",
        help=f"DataForSEO Business Data lookup on a Places miss (PAID, ~${DFS_FALLBACK_EST_USD}/entity). Default auto.",
    )
    ap.add_argument("--out", default=".tmp/places/reviews.json")
    args = ap.parse_args()

    load_env()
    key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not key:
        print(
            "ERROR: GOOGLE_PLACES_API_KEY not set. Add it to .env "
            "(enable Places API 'New' in Google Cloud). Skipping verified reviews.",
            file=sys.stderr,
        )
        return 2

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    results = {}
    for e in entities(cfg):
        rec = resolve_entity(e, cfg, key, args.fallback)
        if rec["status"] == "matched" and args.reviews and rec.get("place_id"):
            try:
                rec["recent_reviews"] = get_reviews(rec["place_id"], key)
            except Exception as ex:  # noqa: BLE001
                rec["recent_reviews_error"] = str(ex)
        results[e["name"]] = rec
        _print_status(e["name"], rec)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"market": cfg.get("market"), "reviews": results}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"-> {out}")


def _print_status(name, rec):
    s = rec["status"]
    if s == "matched":
        print(
            f"  OK {name}: {rec['rating']}* / {rec['review_count']} reviews  "
            f"(matched: {rec['matched_name']}, via {rec['source']})"
        )
    elif s == "mismatch":
        print(
            f"  ! {name}: MISMATCH — API listings didn't validate "
            f"(top: {rec['top_candidate']['name']}); counts withheld",
            file=sys.stderr,
        )
    elif s == "not_found_via_api":
        print(
            f"  ? {name}: not found via API — NOT proof of absence; hand-check Maps "
            f"({rec.get('fallback', 'no fallback info')})",
            file=sys.stderr,
        )
    else:
        print(f"  ! {name}: search failed -> {rec.get('error')}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
