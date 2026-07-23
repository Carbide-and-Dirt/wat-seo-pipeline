#!/usr/bin/env python3
"""
geo_grid.py — Google Maps geo-grid rank tracker (ADR-LP-001 Addendum A).

Fits the WAT framework: deterministic measurement, agent writes the narrative.
Runs one Maps SERP request per (grid point x keyword) through DataForSEO, finds the
target business by place_id, records rank per pin, and computes Share of Local Voice.

Two lifecycles, one tool:
  - baseline : run once at sale time  -> the "before" heatmap for the pitch
  - monthly  : re-scan on a schedule  -> diff vs baseline for the Steel & Amber report

Paid-run discipline (mirrors measure_shortlist.py): ALWAYS supports --dry-run ($0), and a
live run REQUIRES a hard --budget that stops before the request that would cross it. Scans
are resumable (idempotent per pin+keyword). Match is by place_id only, never by name.

  # $0 cost estimate — makes no API calls
  python tools/geo_grid.py --config targets/excavating-national.json \
      --place-id ChIJ... --center-lat 36.16 --center-lng -86.78 --scan-type baseline --dry-run

  # live run, capped
  python tools/geo_grid.py --config targets/excavating-national.json \
      --place-id ChIJ... --center-lat 36.16 --center-lng -86.78 --scan-type baseline --budget 1.50
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol, Sequence

DB_PATH = Path("data/leads.sqlite")

# DataForSEO Google Maps SERP, $ per request (one request = one pin x one keyword).
# Standard queue (~5 min) is the batch default; Live (~6s) only for interactive sale-time scans.
RATE_USD = {"standard": 0.0006, "priority": 0.0012, "live": 0.002}

# WGS84 conversion constants (good to well under a grid cell at metro scale).
_KM_PER_DEG_LAT = 110.574
_KM_PER_DEG_LNG_EQ = 111.320


# --------------------------------------------------------------------------- #
# Pure geometry — fully testable, no I/O                                       #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GridPoint:
    row: int  # 0 = north edge
    col: int  # 0 = west edge
    lat: float
    lng: float


def build_grid(
    center_lat: float, center_lng: float, rows: int, cols: int, spacing_km: float
) -> list[GridPoint]:
    """
    rows x cols points centered on (center_lat, center_lng), `spacing_km` apart.
    (0,0) is the NW corner so the grid maps directly onto a rendered heatmap.
    """
    if rows < 1 or cols < 1:
        raise ValueError("rows and cols must be >= 1")
    if spacing_km <= 0:
        raise ValueError("spacing_km must be > 0")

    lat_deg_per_km = 1.0 / _KM_PER_DEG_LAT
    lng_deg_per_km = 1.0 / (_KM_PER_DEG_LNG_EQ * math.cos(math.radians(center_lat)))

    row_mid = (rows - 1) / 2.0
    col_mid = (cols - 1) / 2.0

    points: list[GridPoint] = []
    for r in range(rows):
        north_km = (row_mid - r) * spacing_km  # r=0 -> northmost (+)
        for c in range(cols):
            east_km = (c - col_mid) * spacing_km  # c=0 -> westmost (-)
            lat = center_lat + north_km * lat_deg_per_km
            lng = center_lng + east_km * lng_deg_per_km
            points.append(GridPoint(row=r, col=c, lat=round(lat, 6), lng=round(lng, 6)))
    return points


def estimate_cost(rows: int, cols: int, n_keywords: int, priority: str) -> dict:
    if priority not in RATE_USD:
        raise ValueError(f"priority must be one of {sorted(RATE_USD)}")
    requests = rows * cols * n_keywords
    rate = RATE_USD[priority]
    return {"requests": requests, "rate_usd": rate, "usd": round(requests * rate, 4)}


# --------------------------------------------------------------------------- #
# Rank extraction — defensive against the exact DataForSEO field names         #
# --------------------------------------------------------------------------- #
def extract_rank(
    items: Sequence[dict], target_place_id: str
) -> tuple[Optional[int], Optional[str], int]:
    """
    Find the target in a Maps result list and return (rank, found_place_id, depth_seen).
    Matches by place_id. Prefers DataForSEO's own rank_absolute; falls back to position.
    Returns (None, None, depth) when the target isn't present within the returned depth.
    """
    depth = len(items)
    position = 0
    for item in items:
        # Organic map entries carry a place_id; ads / non-business rows may not — skip those.
        pid = item.get("place_id") or item.get("cid")
        if pid is None:
            continue
        position += 1
        if pid == target_place_id:
            rank = item.get("rank_absolute") or item.get("rank_group") or position
            return int(rank), pid, depth
    return None, None, depth


# --------------------------------------------------------------------------- #
# SoLV reducer — deterministic, tunable (score_report.py style)                #
# --------------------------------------------------------------------------- #
def _visibility(rank: Optional[int], max_rank: int = 20) -> float:
    """rank 1 -> 1.0, decaying linearly to ~0.05 at rank 20; 0 beyond depth or not found."""
    if rank is None or rank > max_rank:
        return 0.0
    return max(0.0, (max_rank + 1 - rank) / max_rank)


def compute_solv(ranks: Sequence[Optional[int]], max_rank: int = 20) -> dict:
    """
    Share of Local Voice + supporting metrics over every (pin x keyword) result.
      solv        : mean visibility across the whole grid, 0-100
      avg_rank    : mean rank of pins where found (None if never found)
      top3_share  : fraction of pins in the local 3-pack (0-1)
      found_share : fraction of pins where the business appeared at all (0-1)
    """
    n = len(ranks)
    if n == 0:
        return {"solv": 0.0, "avg_rank": None, "top3_share": 0.0, "found_share": 0.0}
    found = [r for r in ranks if r is not None]
    solv = 100.0 * sum(_visibility(r, max_rank) for r in ranks) / n
    top3 = sum(1 for r in found if r <= 3) / n
    return {
        "solv": round(solv, 2),
        "avg_rank": round(sum(found) / len(found), 2) if found else None,
        "top3_share": round(top3, 4),
        "found_share": round(len(found) / n, 4),
    }


# --------------------------------------------------------------------------- #
# Client seam — inject a fake in tests, DataForSEO in prod                     #
# --------------------------------------------------------------------------- #
class MapsClient(Protocol):
    def fetch(
        self, keyword: str, lat: float, lng: float, zoom: int, depth: int, priority: str
    ) -> list[dict]:
        """Return the ordered list of Maps result items (dicts) for one geo-targeted query."""
        ...


class DataForSEOMapsClient:
    """
    Wired to dataforseo.py: shares its .env loading, Basic auth, and response checking.
    priority='live' -> POST /v3/serp/google/maps/live/advanced (~6s, $0.002/request).
    'standard'/'priority' need an async task_post/task_get flow this repo doesn't have yet,
    so main() refuses them for live runs (dry-run still estimates any priority).
    Confirm the item field names against a live response — extract_rank maps them defensively.
    """

    LIVE_ENDPOINT = "/v3/serp/google/maps/live/advanced"

    def __init__(self, language_code: str = "en"):
        import dataforseo as dfs  # local import so --dry-run / tests never need requests

        auth = dfs.creds()
        if not auth:
            raise SystemExit("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set in .env.")
        self._dfs = dfs
        self._auth = auth
        self.language_code = language_code

    def fetch(self, keyword, lat, lng, zoom, depth, priority) -> list[dict]:
        if priority != "live":
            raise NotImplementedError(
                "Only priority='live' is wired (dataforseo.py has no async task_post/task_get "
                "flow yet). Run with --priority live, or build that flow first."
            )
        payload = [
            {
                "keyword": keyword,
                "language_code": self.language_code,
                "location_coordinate": f"{lat},{lng},{zoom}z",
                "depth": depth,
            }
        ]
        try:
            result = self._dfs.post(self.LIVE_ENDPOINT, payload, self._auth)
        except RuntimeError as exc:
            # A pin with no local pack for this keyword is data, not a failure: the
            # business is simply absent there (the darkest tier on the heatmap).
            # DataForSEO signals it as task error 40102 "No Search Results", so
            # record an empty result set instead of aborting the whole scan.
            if "40102" in str(exc):
                return []
            raise
        return (result[0].get("items") or []) if result else []


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def run_scan(
    *,
    conn,
    client: Optional[MapsClient],
    place_id: str,
    center_lat: float,
    center_lng: float,
    grid_cfg: dict,
    priority: str,
    dry_run: bool,
    budget_usd: Optional[float],
    scan_type: str,
    raw_dir: Optional[Path] = None,
    log: Callable[[str], None] = print,
) -> dict:
    """
    Returns a summary dict. In --dry-run makes zero API calls and writes nothing.
    In live mode, stops cleanly before the request that would exceed budget_usd (status='partial').
    """
    import leads_db_grid as gdb  # merged into / imported by leads_db.py in prod

    rows = int(grid_cfg["rows"])
    cols = int(grid_cfg["cols"])
    spacing_km = float(grid_cfg["spacing_km"])
    zoom = int(grid_cfg.get("zoom", 14))
    depth = int(grid_cfg.get("depth", 20))
    keywords = list(grid_cfg["keywords"])

    est = estimate_cost(rows, cols, len(keywords), priority)
    grid = build_grid(center_lat, center_lng, rows, cols, spacing_km)

    log(
        f"grid={rows}x{cols} spacing={spacing_km}km keywords={len(keywords)} "
        f"priority={priority} -> {est['requests']} requests, est ${est['usd']:.4f}"
    )

    if dry_run:
        return {
            "dry_run": True,
            **est,
            "rows": rows,
            "cols": cols,
            "keywords": len(keywords),
            "pins": len(grid),
        }
    assert client is not None, "client is required for live runs"

    if budget_usd is None:
        raise SystemExit(
            "Refusing to run live without --budget. Add --budget <usd> or use --dry-run."
        )
    if est["usd"] > budget_usd:
        log(
            f"WARNING: full-scan estimate ${est['usd']:.4f} exceeds --budget ${budget_usd:.2f}; "
            f"will stop partway and mark the scan 'partial'."
        )

    scan_id = gdb.insert_grid_scan(
        conn,
        place_id=place_id,
        scan_type=scan_type,
        grid_rows=rows,
        grid_cols=cols,
        spacing_km=spacing_km,
        center_lat=center_lat,
        center_lng=center_lng,
        zoom=zoom,
        keywords=keywords,
        depth=depth,
        priority=priority,
        status="pending",
    )
    already = gdb.done_cells(conn, scan_id)

    rate = est["rate_usd"]
    spent = len(already) * rate  # resumed work already paid for in a prior partial run
    ranks: list[Optional[int]] = []
    made_calls = 0
    status = "complete"

    for kw in keywords:
        for p in grid:
            cell = (p.row, p.col, kw)
            if cell in already:
                continue
            if spent + rate > budget_usd:
                status = "partial"
                log(f"budget stop: ${spent:.4f} spent, next request would exceed ${budget_usd:.2f}")
                break
            items = client.fetch(kw, p.lat, p.lng, zoom, depth, priority)
            spent += rate
            made_calls += 1
            rank, found_pid, seen_depth = extract_rank(items, place_id)
            ranks.append(rank)

            raw_ref = None
            if raw_dir is not None:
                raw_dir.mkdir(parents=True, exist_ok=True)
                raw_ref = str(raw_dir / f"{scan_id}_{p.row}_{p.col}_{_slug(kw)}.json")
                Path(raw_ref).write_text(json.dumps(items), encoding="utf-8")

            gdb.upsert_grid_point(
                conn,
                grid_scan_id=scan_id,
                row=p.row,
                col=p.col,
                keyword=kw,
                point_lat=p.lat,
                point_lng=p.lng,
                rank=rank,
                found_place_id=found_pid,
                result_depth=seen_depth,
                raw_ref=raw_ref,
            )
        if status == "partial":
            break

    metrics = compute_solv(ranks, max_rank=min(depth, 20))
    gdb.finalize_grid_scan(
        conn,
        grid_scan_id=scan_id,
        solv=metrics["solv"],
        avg_rank=metrics["avg_rank"],
        top3_share=metrics["top3_share"],
        found_share=metrics["found_share"],
        api_cost_usd=round(made_calls * rate, 4),
        status=status,
    )
    log(
        f"scan {scan_id} {status}: {made_calls} calls, ${made_calls * rate:.4f}, "
        f"SoLV={metrics['solv']} avg_rank={metrics['avg_rank']} "
        f"top3={metrics['top3_share']} found={metrics['found_share']}"
    )
    return {
        "scan_id": scan_id,
        "status": status,
        "calls": made_calls,
        "cost_usd": round(made_calls * rate, 4),
        **metrics,
    }


from lib.common import slug as _slug


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def _load_grid_cfg(config_path: Path, override_keywords: Optional[list[str]]) -> tuple[dict, str]:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    grid_cfg = dict(cfg.get("grid") or {})
    if not grid_cfg:
        raise SystemExit(
            f"{config_path} has no 'grid' block (need rows, cols, spacing_km, keywords)."
        )
    if override_keywords:
        grid_cfg["keywords"] = override_keywords
    priority = str((cfg.get("paid") or {}).get("maps_priority", "standard"))
    return grid_cfg, priority


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Google Maps geo-grid rank tracker (place_id-matched)."
    )
    ap.add_argument(
        "--config", required=True, type=Path, help="targets/<market>.json with a 'grid' block"
    )
    ap.add_argument("--place-id", required=True, help="Target business place_id (the match key)")
    ap.add_argument("--center-lat", type=float, required=True)
    ap.add_argument("--center-lng", type=float, required=True)
    ap.add_argument("--scan-type", choices=["baseline", "monthly", "adhoc"], default="monthly")
    ap.add_argument("--keywords", nargs="*", help="Override the config's grid.keywords")
    ap.add_argument("--priority", choices=list(RATE_USD), help="Override config paid.maps_priority")
    ap.add_argument("--dry-run", action="store_true", help="Print cost estimate; make no API calls")
    ap.add_argument("--budget", type=float, help="HARD cap in USD; required for a live run")
    ap.add_argument(
        "--save-raw", action="store_true", help="Persist raw JSON per pin under .tmp/grid/"
    )
    args = ap.parse_args(argv)

    grid_cfg, cfg_priority = _load_grid_cfg(args.config, args.keywords)
    priority = args.priority or cfg_priority

    if args.dry_run:
        run_scan(
            conn=None,
            client=None,
            place_id=args.place_id,
            center_lat=args.center_lat,
            center_lng=args.center_lng,
            grid_cfg=grid_cfg,
            priority=priority,
            dry_run=True,
            budget_usd=None,
            scan_type=args.scan_type,
        )
        return 0

    # Guard the wallet before touching the DB, client, or network.
    if args.budget is None:
        raise SystemExit(
            "Refusing to run live without --budget. Add --budget <usd> or use --dry-run."
        )
    if priority != "live":
        raise SystemExit(
            f"priority '{priority}' is not wired for live runs yet (no async task flow in "
            "dataforseo.py). Use --priority live, or --dry-run to estimate."
        )

    import sqlite3
    import leads_db_grid as gdb

    conn = sqlite3.connect(DB_PATH)
    gdb.create_grid_tables(conn)  # no-op if leads_db already created them
    client = DataForSEOMapsClient()
    raw_dir = Path(".tmp/grid") if args.save_raw else None
    run_scan(
        conn=conn,
        client=client,
        place_id=args.place_id,
        center_lat=args.center_lat,
        center_lng=args.center_lng,
        grid_cfg=grid_cfg,
        priority=priority,
        dry_run=False,
        budget_usd=args.budget,
        scan_type=args.scan_type,
        raw_dir=raw_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
