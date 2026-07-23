#!/usr/bin/env python3
"""
normalize_prospects.py — turn raw Places discovery into a clean prospect list.

places_discover.py finds *every* listing; this applies the agent's deterministic
classification on top so the report layer (hvac_report.py) gets one row per real
business:
  - tier        : 'chain' if the name matches a config 'chains' brand, else 'local'
  - state       : 2-letter US state / Canadian province from the address (or carried
                  through from the export shim) - the grouping field the national report
                  uses (FR-13). Replaces the local-only 'in_footprint', which is now
                  computed ONLY when a config still defines 'out_of_area_towns' (so the
                  legacy single-market HVAC/pest pipelines keep working unchanged).
  - notes       : flags templated/lead-gen microsites (host starts with a generic
                  'microsite_adjectives' modifier) so junk doesn't masquerade as a lead
  - dedup       : multiple listings sharing ONE website host collapse to a single
                  row (the highest-reviewed one), merging found_via and noting the
                  extra locations; no-website listings dedup by normalized name.

Vertical-agnostic: every classification list comes from the targets config, and
the vertical label / industry_schema pass straight through to the report.

Usage:
    python tools/normalize_prospects.py .tmp/discover/<slug>.json --config targets/<area>-discovery.json
    python tools/normalize_prospects.py .tmp/discover/<slug>.json --config targets/<area>-discovery.json --out .tmp/discover/<slug>-final.json
"""

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from lib.common import utf8_stdout

utf8_stdout()


def host_of(url):
    if not url:
        return ""
    return urlparse(url).netloc.lower().removeprefix("www.")


# US states + DC and Canadian provinces: the 2-letter codes that sit before the
# postal/zip in a Google-formatted address. Validating against this set lets us parse
# the state from ANY US/CA address (FR-13), not just ", TN", without false hits.
US_STATES = {
    "AL",
    "AK",
    "AZ",
    "AR",
    "CA",
    "CO",
    "CT",
    "DE",
    "FL",
    "GA",
    "HI",
    "ID",
    "IL",
    "IN",
    "IA",
    "KS",
    "KY",
    "LA",
    "ME",
    "MD",
    "MA",
    "MI",
    "MN",
    "MS",
    "MO",
    "MT",
    "NE",
    "NV",
    "NH",
    "NJ",
    "NM",
    "NY",
    "NC",
    "ND",
    "OH",
    "OK",
    "OR",
    "PA",
    "RI",
    "SC",
    "SD",
    "TN",
    "TX",
    "UT",
    "VT",
    "VA",
    "WA",
    "WV",
    "WI",
    "WY",
    "DC",
}
CA_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
STATE_CODES = US_STATES | CA_PROVINCES


def parse_location(address):
    """Best-effort (City, STATE) from a '..., City, ST 12345, USA' or
    '..., City, ON A1A 1A1, Canada' address. Scans left-to-right and keeps the LAST
    valid '<City>, <ST>' pair (the one before the postal/country), so an uppercase
    token earlier in the street line can't be mistaken for the state. ('', '') if none."""
    town, state = "", ""
    for m in re.finditer(r",\s*([A-Za-z .'\-]+),\s*([A-Z]{2})\b", address or ""):
        if m.group(2) in STATE_CODES:
            town, state = m.group(1).strip().lower(), m.group(2)
    return town, state


def brand_key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def tier_of(name, chains):
    n = (name or "").lower()
    return "chain" if any(c in n for c in chains) else "local"


def is_microsite(host, adjectives):
    """A generic lead-gen template host, e.g. 'premier<town>pestcontrol.com'."""
    if not host or not adjectives:
        return False
    stem = host.split(".")[0]
    return any(stem.startswith(a) for a in adjectives)


def main():
    ap = argparse.ArgumentParser(
        description="Classify + dedup raw Places discovery into a clean prospect list."
    )
    ap.add_argument("discover", help="Raw discovery JSON from places_discover.py")
    ap.add_argument(
        "--config",
        required=True,
        help="Targets config (chains / out_of_area_towns / microsite_adjectives)",
    )
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    data = json.loads(Path(args.discover).read_text(encoding="utf-8"))
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    chains = [c.lower() for c in cfg.get("chains", [])]
    out_towns = {t.lower() for t in cfg.get("out_of_area_towns", [])}
    adjectives = [a.lower() for a in cfg.get("microsite_adjectives", [])]

    companies = data.get("companies", [])

    # --- classify ---
    for c in companies:
        c["tier"] = tier_of(c.get("name"), chains)
        town, parsed_state = parse_location(c.get("address"))
        # Prefer a state carried through from the export shim (FR-11), else parse it.
        c["state"] = (c.get("state_code") or parsed_state or "").upper()
        notes = []
        host = host_of(c.get("website"))
        if is_microsite(host, adjectives):
            notes.append("templated-style domain - verify it's a real local business")
        # in_footprint is a local-market concept; only meaningful when the config
        # lists out_of_area_towns (legacy single-market pipelines). National = skip.
        if out_towns:
            c["in_footprint"] = town not in out_towns
            if not c["in_footprint"]:
                notes.append(f"outside core footprint ({town.title()})")
        c["notes"] = "; ".join(notes)

    # --- dedup: sites by host, no-website listings by brand name ---
    by_key, order = {}, []

    def reviews(r):
        return r.get("review_count") or 0

    for c in companies:
        host = host_of(c.get("website"))
        key = ("host", host) if host else ("name", brand_key(c.get("name")))
        if key not in by_key:
            by_key[key] = c
            c["_dupes"] = 0
            order.append(key)
        else:
            keep = by_key[key]
            keep["_dupes"] = keep.get("_dupes", 0) + 1
            # merge discovery towns so the kept row reflects every place it was found
            for t in c.get("found_via", []):
                if t not in keep.setdefault("found_via", []):
                    keep["found_via"].append(t)
            # keep the higher-reviewed listing's identity/address/contact
            if reviews(c) > reviews(keep):
                c["_dupes"] = keep["_dupes"]
                c["found_via"] = keep["found_via"]
                c["notes"] = keep.get("notes", "")
                by_key[key] = c

    deduped = []
    for key in order:
        r = by_key[key]
        n = r.pop("_dupes", 0)
        if n:
            extra = (
                f"{n + 1} locations under one website (deduped)"
                if key[0] == "host"
                else f"{n + 1} listings (deduped)"
            )
            r["notes"] = r.get("notes", "") + ("; " if r.get("notes") else "") + extra
        deduped.append(r)

    out = {
        "area": data.get("area"),
        "slug": cfg.get("slug", data.get("slug")),
        "vertical": data.get("vertical") or cfg.get("vertical"),
        "industry_schema": data.get("industry_schema") or cfg.get("industry_schema"),
        "source": data.get("source", "Google Places API (New) searchText"),
        "query": data.get("query"),
        "towns": data.get("towns"),
        "count": len(deduped),
        "companies": deduped,
    }
    out_path = (
        Path(args.out)
        if args.out
        else Path(args.discover).with_name(Path(args.discover).stem + "-final.json")
    )
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    n_chain = sum(1 for r in deduped if r["tier"] == "chain")
    n_local = len(deduped) - n_chain
    n_noweb = sum(1 for r in deduped if r.get("no_website") or not r.get("website"))
    n_micro = sum(1 for r in deduped if "templated-style" in (r.get("notes") or ""))
    n_states = len({r.get("state") for r in deduped if r.get("state")})
    extra = ""
    if out_towns:  # legacy single-market run
        n_out = sum(1 for r in deduped if r.get("in_footprint") is False)
        extra = f" - {n_out} outside footprint"
    print(
        f"{len(companies)} raw -> {len(deduped)} after dedup "
        f"({n_local} local, {n_chain} chain - {n_noweb} no website - "
        f"{n_states} states/provinces{extra} - {n_micro} templated-style) -> {out_path}"
    )


if __name__ == "__main__":
    sys.exit(main())
