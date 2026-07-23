#!/usr/bin/env python3
"""
cell_planner.py - turn a region into density-ordered search cells (HLD FR-1, FR-2, FR-4).

The sweep can't query "a whole state" in one shot (Places caps at ~60 results), so
this module reads the GeoNames seed, filters it to the requested region, orders the
places densest-first (cost-control-first: work the biggest markets before the
budget runs out), and emits one base "cell" per place - a center point plus a
search radius scaled to the town's size.

This module is pure planning: no Google calls, no DB writes. The live sweep (Phase 2)
consumes the cells; FR-4 subdivision of a saturated metro happens at sweep time (when
a real query returns the 60 cap), so here we only expose the *expected* subdivision
factors the cost estimator needs.
"""

import csv
import math
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SEED = "data/places_us_ca.csv"

# FR-2: search radius (meters) by town size. A small town needs a tight radius; a big
# metro a wider one (and is then more likely to saturate and subdivide at sweep time).
# Tunable - these trade coverage overlap against missed outskirts.
RADIUS_BANDS = [
    (5_000, 8_000),
    (25_000, 14_000),
    (100_000, 22_000),
    (500_000, 32_000),
]
RADIUS_MAX = 45_000

# FR-4 cost modeling only: how many cells a place is expected to expand into once
# saturated metros subdivide at sweep time. Small towns stay 1; big metros fan out.
# (low case = no subdivision at all; see the estimator.)
SUBDIV_EXPECTED = [(200_000, 1), (750_000, 4)]
SUBDIV_EXPECTED_MAX = 9
SUBDIV_HIGH = [(100_000, 1), (200_000, 4), (750_000, 9)]
SUBDIV_HIGH_MAX = 16

# Phase-2 live sweep tuning.
SMALL_TOWN_POP = 10_000  # below this, query only the core excavation bucket (cost lever)
SUBDIV_CHILDREN = 4  # quadrants a saturated cell splits into (FR-4)
# Default subdivision depth: ONE refinement level (a saturated metro -> 4 quarter-cells).
# Depth 2 fans a dense metro into 1+4+16=21 cells/bucket - exhaustive but ~4x the cost and
# time per metro, which starves broad coverage. CLI --max-subdiv overrides per run.
MAX_SUBDIV_DEPTH = 1
OVERLAP_GRID_DEG = 0.5  # spatial-hash bucket size for prune_overlapping


@dataclass(frozen=True)
class Place:
    name: str
    state_code: str
    state_name: str
    country: str
    lat: float
    lng: float
    population: int


@dataclass(frozen=True)
class Cell:
    cell_id: str  # stable location identity; sweep appends the trade bucket
    lat: float
    lng: float
    radius_m: int
    population: int
    place_name: str
    state_code: str
    country: str


def load_seed(path=DEFAULT_SEED):
    """Read the GeoNames seed CSV into Place records."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Seed not found at {path}. Run: python tools/build_seed.py")
    places = []
    with open(p, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                places.append(
                    Place(
                        name=r["name"],
                        state_code=r["state_code"],
                        state_name=r["state_name"],
                        country=r["country"],
                        lat=float(r["lat"]),
                        lng=float(r["lng"]),
                        population=int(r["population"]),
                    )
                )
            except (ValueError, KeyError):
                continue  # skip malformed rows rather than abort the whole plan
    return places


def parse_region(arg):
    """Parse a --region argument into a region spec dict (FR-1).

    'all'                              -> every US/CA place
    'TN KY VA' or 'TN,KY,VA'           -> those states/provinces (2-letter or full name)
    'bbox:minlat,minlng,maxlat,maxlng' -> a bounding box
    """
    s = (arg or "").strip()
    if not s or s.lower() == "all":
        return {"type": "all"}
    if s.lower().startswith("bbox:"):
        nums = [float(x) for x in s[5:].split(",")]
        if len(nums) != 4:
            raise ValueError("bbox needs 4 numbers: bbox:minlat,minlng,maxlat,maxlng")
        return {"type": "bbox", "bounds": tuple(nums)}
    codes = [c.strip().lower() for c in s.replace(",", " ").split() if c.strip()]
    return {"type": "codes", "codes": set(codes)}


def filter_region(places, region):
    """Return the places inside a region spec (FR-1)."""
    if region["type"] == "all":
        return list(places)
    if region["type"] == "bbox":
        lo_lat, lo_lng, hi_lat, hi_lng = region["bounds"]
        return [p for p in places if lo_lat <= p.lat <= hi_lat and lo_lng <= p.lng <= hi_lng]
    codes = region["codes"]  # match either the 2-letter code or the full state name
    return [p for p in places if p.state_code.lower() in codes or p.state_name.lower() in codes]


def radius_for(population):
    """FR-2: search radius in meters for a town of the given population."""
    for max_pop, radius in RADIUS_BANDS:
        if population < max_pop:
            return radius
    return RADIUS_MAX


def _banded(value, bands, default):
    for threshold, result in bands:
        if value < threshold:
            return result
    return default


def expected_subdivision(population):
    """FR-4 cost model: expected cell count for a place once metros subdivide."""
    return _banded(population, SUBDIV_EXPECTED, SUBDIV_EXPECTED_MAX)


def high_subdivision(population):
    """FR-4 cost model: worst-case cell count for a place."""
    return _banded(population, SUBDIV_HIGH, SUBDIV_HIGH_MAX)


def plan_cells(places):
    """Density-first base cells (FR-1 ordering): one per place, biggest markets first."""
    ordered = sorted(places, key=lambda p: p.population, reverse=True)
    cells = []
    for p in ordered:
        cell_id = f"{p.state_code}|{p.name}|{p.lat:.4f},{p.lng:.4f}"
        cells.append(
            Cell(
                cell_id=cell_id,
                lat=p.lat,
                lng=p.lng,
                radius_m=radius_for(p.population),
                population=p.population,
                place_name=p.name,
                state_code=p.state_code,
                country=p.country,
            )
        )
    return cells


def plan_region(region_arg, seed_path=DEFAULT_SEED):
    """Convenience: seed -> filter -> ordered cells for a --region argument."""
    region = parse_region(region_arg)
    return plan_cells(filter_region(load_seed(seed_path), region)), region


def haversine_m(lat1, lng1, lat2, lng2):
    """Great-circle distance in meters."""
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def prune_overlapping(cells):
    """Drop a cell whose center already lies inside a kept (larger) cell's radius.

    Greedy and densest-first (cells must arrive population-ordered, as plan_cells
    returns them), so the bigger market's wider radius absorbs the small town next
    door instead of paying for a near-duplicate search. A spatial-hash grid keeps it
    near-linear instead of O(n^2). This is a cost optimization, not a coverage change:
    a pruned town sits inside a cell that already covers it.
    """
    kept = []
    grid = {}  # (gi, gj) -> indexes into `kept`, for fast neighbor lookup

    def bucket(lat, lng):
        return (math.floor(lat / OVERLAP_GRID_DEG), math.floor(lng / OVERLAP_GRID_DEG))

    for c in cells:
        gi, gj = bucket(c.lat, c.lng)
        covered = False
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for k in grid.get((gi + di, gj + dj), ()):
                    kc = kept[k]
                    if haversine_m(c.lat, c.lng, kc.lat, kc.lng) <= kc.radius_m:
                        covered = True
                        break
                if covered:
                    break
            if covered:
                break
        if not covered:
            grid.setdefault((gi, gj), []).append(len(kept))
            kept.append(c)
    return kept


def _meters_to_degrees(meters, lat):
    dlat = meters / 111_320.0
    dlng = meters / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
    return dlat, dlng


def subdivide(cell, n=SUBDIV_CHILDREN):
    """FR-4: split a saturated cell into n smaller child cells around its center, so
    operators the 60-result cap truncated get picked up. Children get ~60% radius."""
    child_r = max(2_000, int(cell.radius_m * 0.6))
    dlat, dlng = _meters_to_degrees(cell.radius_m * 0.5, cell.lat)
    quadrants = [(1, 1), (1, -1), (-1, 1), (-1, -1)][:n]
    children = []
    for i, (slat, slng) in enumerate(quadrants):
        children.append(
            Cell(
                cell_id=f"{cell.cell_id}/q{i}",
                lat=cell.lat + slat * dlat,
                lng=cell.lng + slng * dlng,
                radius_m=child_r,
                population=cell.population,
                place_name=cell.place_name,
                state_code=cell.state_code,
                country=cell.country,
            )
        )
    return children


def query_depth_for(population, buckets, core_bucket="excavation"):
    """Town-size query scaling (cost lever): small towns get only the core excavation
    bucket; larger towns get every bucket. `buckets` is the config trade_queries list."""
    if population >= SMALL_TOWN_POP:
        return list(buckets)
    core = [b for b in buckets if b.get("bucket") == core_bucket]
    return core or list(buckets)
