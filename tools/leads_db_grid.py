"""
Grid-tracking schema + helpers for the geo-grid rank tracker (ADR-LP-001 Addendum A).

leads_db.py OWNS the schema and connection for data/leads.sqlite. Keep that invariant:
either paste these CREATE statements + helpers into tools/leads_db.py, or keep this as a
submodule that leads_db.py imports and re-exports so `leads_db.py init` calls
create_grid_tables(conn) alongside the other tables.

Consistent with the store's ToS posture: place_id is the durable key; rank rows are a
refreshable cache stamped with last_refreshed_ts. Mirrors the existing site_rankings table.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CREATE_STATEMENTS = [
    # One geo-grid scan run for one business (baseline at sale time, or a monthly re-scan).
    """
    CREATE TABLE IF NOT EXISTS grid_scans (
        id                INTEGER PRIMARY KEY,
        place_id          TEXT NOT NULL,                 -- -> businesses.place_id (canonical key)
        scan_type         TEXT NOT NULL DEFAULT 'monthly',  -- baseline | monthly | adhoc
        grid_rows         INTEGER NOT NULL,
        grid_cols         INTEGER NOT NULL,
        spacing_km        REAL NOT NULL,
        center_lat        REAL NOT NULL,
        center_lng        REAL NOT NULL,
        zoom              INTEGER NOT NULL DEFAULT 14,
        keywords          TEXT NOT NULL,                 -- JSON array
        depth             INTEGER NOT NULL DEFAULT 20,
        priority          TEXT NOT NULL DEFAULT 'standard', -- standard | priority | live
        solv              REAL,                          -- Share of Local Voice (computed)
        avg_rank          REAL,
        top3_share        REAL,
        found_share       REAL,
        api_cost_usd      REAL,                          -- actual spend, for COGS tracking
        provider          TEXT NOT NULL DEFAULT 'dataforseo',
        status            TEXT NOT NULL DEFAULT 'pending', -- pending | complete | partial | error
        scanned_ts        TEXT NOT NULL,
        last_refreshed_ts TEXT NOT NULL
    )
    """,
    # One row per (grid point x keyword): the target's rank at that pin.
    """
    CREATE TABLE IF NOT EXISTS grid_points (
        id             INTEGER PRIMARY KEY,
        grid_scan_id   INTEGER NOT NULL REFERENCES grid_scans(id) ON DELETE CASCADE,
        row            INTEGER NOT NULL,
        col            INTEGER NOT NULL,
        keyword        TEXT NOT NULL,
        point_lat      REAL NOT NULL,
        point_lng      REAL NOT NULL,
        rank           INTEGER,                          -- NULL = not found within depth
        found_place_id TEXT,                             -- matched by place_id, never by name
        result_depth   INTEGER,
        raw_ref        TEXT,                             -- .tmp/ path to raw JSON (audit)
        UNIQUE (grid_scan_id, row, col, keyword)         -- makes a scan resumable
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_grid_points_scan ON grid_points (grid_scan_id, keyword)",
    "CREATE INDEX IF NOT EXISTS idx_grid_scans_place ON grid_scans (place_id, scan_type, scanned_ts)",
]


def create_grid_tables(conn) -> None:
    """Call this from leads_db.py's init() alongside the other CREATE TABLE calls."""
    cur = conn.cursor()
    for stmt in CREATE_STATEMENTS:
        cur.execute(stmt)
    conn.commit()


def insert_grid_scan(
    conn,
    *,
    place_id,
    scan_type,
    grid_rows,
    grid_cols,
    spacing_km,
    center_lat,
    center_lng,
    zoom,
    keywords,
    depth,
    priority,
    status="pending",
) -> int:
    """Create a scan header row and return its id. Points/metrics are filled in as they complete."""
    ts = _now_iso()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO grid_scans (place_id, scan_type, grid_rows, grid_cols, spacing_km,
                                center_lat, center_lng, zoom, keywords, depth, priority,
                                status, scanned_ts, last_refreshed_ts)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            place_id,
            scan_type,
            grid_rows,
            grid_cols,
            spacing_km,
            center_lat,
            center_lng,
            zoom,
            json.dumps(keywords),
            depth,
            priority,
            status,
            ts,
            ts,
        ),
    )
    conn.commit()
    return cur.lastrowid


def upsert_grid_point(
    conn,
    *,
    grid_scan_id,
    row,
    col,
    keyword,
    point_lat,
    point_lng,
    rank,
    found_place_id,
    result_depth,
    raw_ref=None,
) -> None:
    """Idempotent per (scan, row, col, keyword) so an interrupted scan can resume cleanly."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO grid_points (grid_scan_id, row, col, keyword, point_lat, point_lng,
                                 rank, found_place_id, result_depth, raw_ref)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (grid_scan_id, row, col, keyword) DO UPDATE SET
            rank=excluded.rank,
            found_place_id=excluded.found_place_id,
            result_depth=excluded.result_depth,
            raw_ref=excluded.raw_ref
        """,
        (
            grid_scan_id,
            row,
            col,
            keyword,
            point_lat,
            point_lng,
            rank,
            found_place_id,
            result_depth,
            raw_ref,
        ),
    )
    conn.commit()


def finalize_grid_scan(
    conn, *, grid_scan_id, solv, avg_rank, top3_share, found_share, api_cost_usd, status
) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE grid_scans
           SET solv=?, avg_rank=?, top3_share=?, found_share=?, api_cost_usd=?,
               status=?, last_refreshed_ts=?
         WHERE id=?
        """,
        (solv, avg_rank, top3_share, found_share, api_cost_usd, status, _now_iso(), grid_scan_id),
    )
    conn.commit()


def done_cells(conn, grid_scan_id) -> set:
    """(row, col, keyword) tuples already fetched for this scan — used to resume.

    Every fetched pin is upserted with a result_depth (even when the target wasn't found),
    so presence in grid_points == that cell is done.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT row, col, keyword FROM grid_points WHERE grid_scan_id=?",
        (grid_scan_id,),
    )
    return {(r, c, k) for (r, c, k) in cur.fetchall()}


def get_baseline_scan(conn, place_id):
    """The 'since you signed up' anchor: earliest completed baseline scan for a business."""
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM grid_scans WHERE place_id=? AND scan_type='baseline' AND status='complete' "
        "ORDER BY scanned_ts ASC LIMIT 1",
        (place_id,),
    )
    return cur.fetchone()


def latest_scan(conn, place_id, scan_type=None):
    cur = conn.cursor()
    if scan_type:
        cur.execute(
            "SELECT * FROM grid_scans WHERE place_id=? AND scan_type=? AND status='complete' "
            "ORDER BY scanned_ts DESC LIMIT 1",
            (place_id, scan_type),
        )
    else:
        cur.execute(
            "SELECT * FROM grid_scans WHERE place_id=? AND status='complete' "
            "ORDER BY scanned_ts DESC LIMIT 1",
            (place_id,),
        )
    return cur.fetchone()


# --------------------------------------------------------------------------- #
# Dict-returning read helpers (used by grid_diff.py — independent of row_factory) #
# --------------------------------------------------------------------------- #
def _rows_as_dicts(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def get_scan(conn, scan_id):
    """Full scan header as a dict (or None)."""
    cur = conn.cursor()
    cur.execute("SELECT * FROM grid_scans WHERE id=?", (scan_id,))
    rows = _rows_as_dicts(cur)
    return rows[0] if rows else None


def get_grid_points(conn, scan_id):
    """All pins for a scan as dicts: row, col, keyword, point_lat, point_lng, rank, ..."""
    cur = conn.cursor()
    cur.execute(
        "SELECT row, col, keyword, point_lat, point_lng, rank, found_place_id, result_depth "
        "FROM grid_points WHERE grid_scan_id=?",
        (scan_id,),
    )
    return _rows_as_dicts(cur)


def baseline_scan_id(conn, place_id):
    """id of the earliest completed baseline scan (the 'since signup' anchor)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM grid_scans WHERE place_id=? AND scan_type='baseline' AND status='complete' "
        "ORDER BY scanned_ts ASC LIMIT 1",
        (place_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def latest_scan_id(conn, place_id, scan_type=None, exclude_baseline=False):
    """id of the most recent completed scan (optionally of a type, optionally excluding baseline)."""
    q = "SELECT id FROM grid_scans WHERE place_id=? AND status='complete'"
    params = [place_id]
    if scan_type:
        q += " AND scan_type=?"
        params.append(scan_type)
    elif exclude_baseline:
        q += " AND scan_type != 'baseline'"
    q += " ORDER BY scanned_ts DESC LIMIT 1"
    cur = conn.cursor()
    cur.execute(q, params)
    row = cur.fetchone()
    return row[0] if row else None
