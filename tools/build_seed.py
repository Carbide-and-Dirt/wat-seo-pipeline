#!/usr/bin/env python3
"""
build_seed.py - build the US + Canada place seed for prospect_sweep (HLD FR-2).

prospect_sweep can't ask Google for "every excavator in the US" (Places caps a
search at ~60 results), so it works through a list of *places* and searches around
each. This script builds that list from GeoNames (free, CC-BY): every US/Canadian
populated place above a population floor, with state/province, lat/lng and
population. Population drives the cost-control-first ordering (densest markets
first) and the per-cell search radius.

Source: GeoNames `cities500` + `admin1CodesASCII` dumps (https://www.geonames.org/),
licensed CC BY 4.0. Attribution is written to data/NOTICE-geonames.txt (FR-2).

Idempotent: the raw dumps are cached under data/.cache and reused; re-running just
rewrites the CSV. Network is only to GeoNames (NOT Google) - this spends no API
credits.

Usage:
    python tools/build_seed.py                     # default: population >= 1000
    python tools/build_seed.py --min-pop 5000      # fewer/larger towns, cheaper sweeps
    python tools/build_seed.py --refresh           # re-download the GeoNames dumps
    python tools/build_seed.py --out data/places_us_ca.csv
"""

import argparse
import csv
import io
import sys
import zipfile
from pathlib import Path

import requests

from lib.common import utf8_stdout

utf8_stdout()

GEONAMES_BASE = "https://download.geonames.org/export/dump"
CITIES_ARCHIVE = "cities500.zip"  # populated places, population >= 500, worldwide
CITIES_MEMBER = "cities500.txt"
ADMIN1_FILE = "admin1CodesASCII.txt"  # maps "US.47" -> "Tennessee"
COUNTRIES = ("US", "CA")

# GeoNames "geoname" table column indexes (tab-separated dump schema).
COL_ASCIINAME, COL_LAT, COL_LNG = 2, 4, 5
COL_FEATURE_CLASS, COL_COUNTRY, COL_ADMIN1, COL_POPULATION = 6, 8, 10, 14

# Map the GeoNames admin1 *name* to a friendly 2-letter code so region specs read
# naturally ("--region TN KY VA") regardless of GeoNames' internal admin1 coding.
US_STATE_CODES = {
    "Alabama": "AL",
    "Alaska": "AK",
    "Arizona": "AZ",
    "Arkansas": "AR",
    "California": "CA",
    "Colorado": "CO",
    "Connecticut": "CT",
    "Delaware": "DE",
    "District of Columbia": "DC",
    "Florida": "FL",
    "Georgia": "GA",
    "Hawaii": "HI",
    "Idaho": "ID",
    "Illinois": "IL",
    "Indiana": "IN",
    "Iowa": "IA",
    "Kansas": "KS",
    "Kentucky": "KY",
    "Louisiana": "LA",
    "Maine": "ME",
    "Maryland": "MD",
    "Massachusetts": "MA",
    "Michigan": "MI",
    "Minnesota": "MN",
    "Mississippi": "MS",
    "Missouri": "MO",
    "Montana": "MT",
    "Nebraska": "NE",
    "Nevada": "NV",
    "New Hampshire": "NH",
    "New Jersey": "NJ",
    "New Mexico": "NM",
    "New York": "NY",
    "North Carolina": "NC",
    "North Dakota": "ND",
    "Ohio": "OH",
    "Oklahoma": "OK",
    "Oregon": "OR",
    "Pennsylvania": "PA",
    "Rhode Island": "RI",
    "South Carolina": "SC",
    "South Dakota": "SD",
    "Tennessee": "TN",
    "Texas": "TX",
    "Utah": "UT",
    "Vermont": "VT",
    "Virginia": "VA",
    "Washington": "WA",
    "West Virginia": "WV",
    "Wisconsin": "WI",
    "Wyoming": "WY",
}
CA_PROVINCE_CODES = {
    "Alberta": "AB",
    "British Columbia": "BC",
    "Manitoba": "MB",
    "New Brunswick": "NB",
    "Newfoundland and Labrador": "NL",
    "Northwest Territories": "NT",
    "Nova Scotia": "NS",
    "Nunavut": "NU",
    "Ontario": "ON",
    "Prince Edward Island": "PE",
    "Quebec": "QC",
    "Saskatchewan": "SK",
    "Yukon": "YT",
}


def code_for(country, admin1_name):
    """Friendly 2-letter state/province code, or '' if unmapped (still kept by name)."""
    table = US_STATE_CODES if country == "US" else CA_PROVINCE_CODES
    return table.get((admin1_name or "").strip(), "")


def download(url, dest, refresh=False):
    """Stream a GeoNames dump to a local cache file, reusing it unless --refresh."""
    if dest.exists() and not refresh:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return dest


def load_admin1(path):
    """'US.47' -> 'Tennessee'. File rows: code<TAB>name<TAB>asciiname<TAB>geonameid."""
    mapping = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            mapping[parts[0]] = parts[2]  # asciiname keeps output ASCII-safe (cp1252 host)
    return mapping


def iter_places(zip_path, admin1, min_pop):
    """Yield (asciiname, state_code, state_name, country, lat, lng, population) for
    US/CA populated places (feature class 'P') at or above the population floor."""
    with zipfile.ZipFile(zip_path) as z:
        with z.open(CITIES_MEMBER) as fh:
            for raw in io.TextIOWrapper(fh, encoding="utf-8"):
                c = raw.rstrip("\n").split("\t")
                if len(c) <= COL_POPULATION:
                    continue
                if c[COL_COUNTRY] not in COUNTRIES or c[COL_FEATURE_CLASS] != "P":
                    continue
                try:
                    pop = int(c[COL_POPULATION] or 0)
                except ValueError:
                    continue
                if pop < min_pop:
                    continue
                country = c[COL_COUNTRY]
                state_name = admin1.get(f"{country}.{c[COL_ADMIN1]}", "")
                yield (
                    c[COL_ASCIINAME],
                    code_for(country, state_name),
                    state_name,
                    country,
                    c[COL_LAT],
                    c[COL_LNG],
                    pop,
                )


def write_notice(data_dir):
    """CC-BY attribution required by the GeoNames license (FR-2)."""
    (data_dir / "NOTICE-geonames.txt").write_text(
        "Place seed data (data/places_us_ca.csv) is derived from GeoNames\n"
        "(cities500 + admin1CodesASCII dumps), (c) GeoNames, licensed under\n"
        "Creative Commons Attribution 4.0 (CC BY 4.0): https://www.geonames.org/\n"
        "Filtered to US + Canada populated places and reduced to\n"
        "name/state/country/lat/lng/population by tools/build_seed.py.\n",
        encoding="utf-8",
    )


def main():
    ap = argparse.ArgumentParser(
        description="Build the US+CA place seed from GeoNames (FR-2; no Google spend)."
    )
    ap.add_argument(
        "--min-pop",
        type=int,
        default=1000,
        help="Population floor for inclusion (default 1000). Higher = fewer towns = cheaper sweeps.",
    )
    ap.add_argument("--out", default="data/places_us_ca.csv")
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download the GeoNames dumps (else cache is reused).",
    )
    args = ap.parse_args()

    cache = Path("data/.cache")
    try:
        zip_path = download(
            f"{GEONAMES_BASE}/{CITIES_ARCHIVE}", cache / CITIES_ARCHIVE, args.refresh
        )
        admin1_path = download(f"{GEONAMES_BASE}/{ADMIN1_FILE}", cache / ADMIN1_FILE, args.refresh)
    except requests.RequestException as e:
        print(
            f"ERROR: GeoNames download failed ({e}). Check connectivity and retry.", file=sys.stderr
        )
        return 1

    admin1 = load_admin1(admin1_path)
    rows = list(iter_places(zip_path, admin1, args.min_pop))
    # Density-first: persist sorted by population desc so the file itself is readable
    # (the cell planner re-sorts per region anyway, FR-1).
    rows.sort(key=lambda r: r[6], reverse=True)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "state_code", "state_name", "country", "lat", "lng", "population"])
        w.writerows(rows)
    write_notice(out_path.parent)

    us = sum(1 for r in rows if r[3] == "US")
    ca = len(rows) - us
    no_code = sum(1 for r in rows if not r[1])
    print(f"\n{len(rows)} places (US {us}, CA {ca}) at population >= {args.min_pop} -> {out_path}")
    if no_code:
        print(
            f"  note: {no_code} rows have no 2-letter code (unmapped admin1; usable by state_name)."
        )


if __name__ == "__main__":
    sys.exit(main())
