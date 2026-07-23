"""
GBP-audit schema + helpers for the prospect-side Google Business Profile neglect audit
(DESIGN-gbp-prospect-audit.md).

leads_db.py OWNS the schema and connection for data/leads.sqlite. Keep that invariant:
this is a submodule leads_db.py imports so `leads_db.py init` calls create_gbp_tables(conn)
alongside the other tables (same pattern as leads_db_grid.py).

Snapshots are APPEND-ONLY (one row per audit run per business), not INSERT OR REPLACE: the
sale-time row is the "before" and a later re-audit is the "after", so the same data serves
prospecting AND the client before/after report (mirrors grid_scans' baseline/monthly model).

ToS posture: place_id is the durable key; every measured field is a refreshable cache stamped
with last_refreshed_ts. Counts, booleans, and a single last_post_ts only — no review text,
reviewer names, or photo URLs are persisted.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


CREATE_STATEMENTS = [
    # One GBP audit snapshot for one business. place_id is the only permanent identity;
    # every other column is a refreshable cache. Append-only so before/after works.
    """
    CREATE TABLE IF NOT EXISTS gbp_audits (
        id                          INTEGER PRIMARY KEY,
        place_id                    TEXT NOT NULL,        -- -> businesses.place_id (canonical key)
        audit_type                  TEXT NOT NULL DEFAULT 'prospect', -- prospect | baseline | monthly | adhoc
        is_claimed                  INTEGER,              -- 1 / 0 / NULL(unknown)
        rating_value                REAL,
        rating_votes                INTEGER,
        category                    TEXT,
        additional_categories_count INTEGER,
        has_description             INTEGER,              -- 1 if description non-empty
        total_photos                INTEGER,
        has_hours                   INTEGER,              -- 1 if work_time present
        attr_available_count        INTEGER,
        attr_unavailable_count      INTEGER,
        neg_reviews                 INTEGER,              -- 1 + 2 star count (from rating_distribution)
        rating_distribution_json    TEXT,                 -- full 1..5 star histogram
        post_count                  INTEGER,
        last_post_ts                TEXT,                 -- NULL = no posts found
        days_since_post             INTEGER,              -- at audit time; NULL = never/unknown
        neglect_score               REAL,                 -- computed reducer, 0..100 (NULL if no_data)
        signals_json                TEXT,                 -- which neglect signals fired
        api_cost_usd                REAL,
        provider                    TEXT NOT NULL DEFAULT 'dataforseo',
        status                      TEXT NOT NULL DEFAULT 'complete', -- complete | no_data | error
        audited_ts                  TEXT NOT NULL,
        last_refreshed_ts           TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gbp_audits_place ON gbp_audits (place_id, audit_type, audited_ts)",
]


# Column order for inserts (matches the table, minus the autoincrement id).
_GBP_COLUMNS = (
    "place_id",
    "audit_type",
    "is_claimed",
    "rating_value",
    "rating_votes",
    "category",
    "additional_categories_count",
    "has_description",
    "total_photos",
    "has_hours",
    "attr_available_count",
    "attr_unavailable_count",
    "neg_reviews",
    "rating_distribution_json",
    "post_count",
    "last_post_ts",
    "days_since_post",
    "neglect_score",
    "signals_json",
    "api_cost_usd",
    "provider",
    "status",
    "audited_ts",
    "last_refreshed_ts",
)

# Columns added after the initial ship — created on fresh DBs by CREATE_STATEMENTS and
# back-filled onto an existing gbp_audits via ALTER (safe: additive, nullable).
_GBP_MIGRATIONS = {"neg_reviews": "INTEGER", "rating_distribution_json": "TEXT"}


def create_gbp_tables(conn) -> None:
    """Call this from leads_db.py's init() alongside the other CREATE TABLE calls."""
    cur = conn.cursor()
    for stmt in CREATE_STATEMENTS:
        cur.execute(stmt)
    existing = {r[1] for r in cur.execute("PRAGMA table_info(gbp_audits)").fetchall()}
    for col, typ in _GBP_MIGRATIONS.items():
        if col not in existing:
            cur.execute(f"ALTER TABLE gbp_audits ADD COLUMN {col} {typ}")
    conn.commit()


def insert_gbp_audit(
    conn,
    *,
    place_id,
    audit_type,
    fields,
    neglect_score,
    signals,
    api_cost_usd,
    status,
    provider="dataforseo",
) -> int:
    """Append one audit snapshot and return its id. `fields` is the dict from the
    extractors (missing keys default to NULL); `signals` is the fired-signal dict."""
    ts = _now_iso()
    rec = {
        "place_id": place_id,
        "audit_type": audit_type,
        "neglect_score": neglect_score,
        "signals_json": json.dumps(signals or {}),
        "api_cost_usd": round(api_cost_usd, 6) if api_cost_usd is not None else None,
        "provider": provider,
        "status": status,
        "audited_ts": ts,
        "last_refreshed_ts": ts,
    }
    rec.update(
        {
            k: fields.get(k)
            for k in (
                "is_claimed",
                "rating_value",
                "rating_votes",
                "category",
                "additional_categories_count",
                "has_description",
                "total_photos",
                "has_hours",
                "attr_available_count",
                "attr_unavailable_count",
                "neg_reviews",
                "rating_distribution_json",
                "post_count",
                "last_post_ts",
                "days_since_post",
            )
        }
    )
    cols = ", ".join(_GBP_COLUMNS)
    ph = ", ".join("?" for _ in _GBP_COLUMNS)
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO gbp_audits ({cols}) VALUES ({ph})", tuple(rec.get(c) for c in _GBP_COLUMNS)
    )
    conn.commit()
    return cur.lastrowid


def recently_audited(conn, place_id, since_ts) -> bool:
    """True if a completed (or no_data) audit exists for this place_id at/after since_ts.
    Used by --skip-existing so a re-run doesn't re-spend on freshly audited leads."""
    row = conn.execute(
        "SELECT 1 FROM gbp_audits WHERE place_id=? AND status IN ('complete','no_data') "
        "AND audited_ts >= ? LIMIT 1",
        (place_id, since_ts),
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# Dict-returning read helpers (used by gbp_diff.py — independent of row_factory) #
# --------------------------------------------------------------------------- #
def _rows_as_dicts(cur):
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def get_audit(conn, audit_id):
    """Full audit row as a dict (or None)."""
    cur = conn.cursor()
    cur.execute("SELECT * FROM gbp_audits WHERE id=?", (audit_id,))
    rows = _rows_as_dicts(cur)
    return rows[0] if rows else None


def baseline_audit_id(conn, place_id):
    """id of the earliest completed baseline audit (the 'since signup' anchor). Falls back
    to the earliest 'prospect' audit, since the sale-time prospect scan IS the before-state."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM gbp_audits WHERE place_id=? AND status='complete' "
        "AND audit_type IN ('baseline','prospect') ORDER BY audited_ts ASC, id ASC LIMIT 1",
        (place_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def latest_audit_id(conn, place_id, exclude_id=None):
    """id of the most recent completed audit (optionally excluding one id, e.g. the baseline)."""
    q = "SELECT id FROM gbp_audits WHERE place_id=? AND status='complete'"
    params = [place_id]
    if exclude_id is not None:
        q += " AND id != ?"
        params.append(exclude_id)
    q += " ORDER BY audited_ts DESC, id DESC LIMIT 1"
    cur = conn.cursor()
    cur.execute(q, params)
    row = cur.fetchone()
    return row[0] if row else None


def gbp_status(conn):
    """Counts for the CLI / health checks."""
    cur = conn.cursor()
    audited = cur.execute("SELECT COUNT(DISTINCT place_id) FROM gbp_audits").fetchone()[0]
    snapshots = cur.execute("SELECT COUNT(*) FROM gbp_audits").fetchone()[0]
    spent = cur.execute("SELECT COALESCE(SUM(api_cost_usd), 0) FROM gbp_audits").fetchone()[0]
    unclaimed = cur.execute(
        "SELECT COUNT(*) FROM gbp_audits WHERE is_claimed=0 AND status='complete'"
    ).fetchone()[0]
    return {
        "businesses_audited": audited,
        "snapshots": snapshots,
        "spent": round(spent or 0, 4),
        "unclaimed_snapshots": unclaimed,
    }
