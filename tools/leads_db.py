#!/usr/bin/env python3
"""
leads_db.py - the durable SQLite master store for prospect_sweep (HLD FR-7, FR-8, SEC-4).

A national lead list grows across many runs, so it needs cross-run dedup, a record
of which cells have been swept (resumability), and a run audit trail - which JSON
blobs handle poorly. This module owns the schema and connection; the sweep engine
(Phase 2) and report layer (Phase 4) build on it.

SEC-4 (Google ToS): `place_id` is the durable primary key (Google permits storing
it indefinitely); every other Places-derived field is stored with `last_refreshed_ts`
and treated as a refreshable cache, not a permanent record.

Usage:
    python tools/leads_db.py init                 # create data/leads.sqlite (idempotent)
    python tools/leads_db.py status               # row counts / coverage summary
    python tools/leads_db.py status --db path.sqlite
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from lib.common import utf8_stdout

utf8_stdout()

DEFAULT_DB = "data/leads.sqlite"

# FR-7/FR-8: place_id is the dedup key (one row per business). FR-13: state_code keeps
# the per-state grouping the report needs. SEC-4: first_seen_ts/last_refreshed_ts mark
# the cache window for non-place_id fields.
SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    place_id          TEXT PRIMARY KEY,
    name              TEXT,
    address           TEXT,
    state_code        TEXT,
    state_name        TEXT,
    country           TEXT,
    lat               REAL,
    lng               REAL,
    phone             TEXT,
    website           TEXT,
    no_website        INTEGER,
    rating            REAL,
    review_count      INTEGER,
    business_status   TEXT,
    primary_type      TEXT,
    types_json        TEXT,
    trade_bucket      TEXT,
    relevance         TEXT,
    maps_url          TEXT,
    found_via_json    TEXT,
    first_seen_ts     TEXT,
    last_refreshed_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_businesses_state    ON businesses(state_code);
CREATE INDEX IF NOT EXISTS idx_businesses_no_site  ON businesses(no_website);

-- FR-9/FR-10: coverage ledger. A cell is one (location, radius, trade_bucket) query
-- target; recording it lets a killed run resume and an additive re-run skip it.
CREATE TABLE IF NOT EXISTS swept_cells (
    cell_id       TEXT PRIMARY KEY,
    center_lat    REAL,
    center_lng    REAL,
    radius_m      INTEGER,
    trade_bucket  TEXT,
    region        TEXT,
    result_count  INTEGER,
    saturated     INTEGER,
    swept_ts      TEXT
);
CREATE INDEX IF NOT EXISTS idx_swept_region ON swept_cells(region);

-- Run audit trail (one row per sweep invocation, dry or live).
CREATE TABLE IF NOT EXISTS runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    region          TEXT,
    budget          REAL,
    mode            TEXT,
    started_ts      TEXT,
    ended_ts        TEXT,
    requests_spent  INTEGER,
    est_cost        REAL
);

-- FR-15/FR-16: free site-enrichment cache (SEO + AEO/GEO readiness + agency
-- fingerprint), one row per lead that has a website. Separate table (not extra
-- businesses columns) so it is a self-contained refreshable cache (SEC-4/SEC-6)
-- with its own timestamp; a re-enrich is a single-row replace.
CREATE TABLE IF NOT EXISTS site_enrichment (
    place_id            TEXT PRIMARY KEY REFERENCES businesses(place_id),
    fetched_url         TEXT,
    http_status         INTEGER,
    reachable           INTEGER,
    site_status         TEXT,      -- live / dead / parked / social_only / directory / blocked / unreachable
    https               INTEGER,
    mobile_viewport     INTEGER,
    title_len           INTEGER,
    meta_desc_len       INTEGER,
    word_count          INTEGER,
    jsonld_present      INTEGER,
    schema_localbusiness INTEGER,
    schema_faq          INTEGER,
    llms_txt            INTEGER,
    ai_bots_blocked     TEXT,      -- JSON list of AI crawlers the site blocks
    readiness_score     INTEGER,   -- higher = weaker site = better prospect (reuses opportunity())
    seo_gaps_json       TEXT,
    builder             TEXT,
    marketing_tags_json TEXT,
    agency_credit       TEXT,
    google_ads          INTEGER,
    mgmt_status         TEXT,      -- DIY / self-managed / likely agency-managed / unknown
    mgmt_confidence     TEXT,
    mgmt_evidence_json  TEXT,
    fetched_via         TEXT,
    enriched_ts         TEXT
);
CREATE INDEX IF NOT EXISTS idx_enrich_mgmt ON site_enrichment(mgmt_status);

-- FR-17: paid shortlist measurement (actual rank / authority / AI citations),
-- one row per lead measured. Apart from the free pass: it costs money and
-- refreshes on a different cadence.
CREATE TABLE IF NOT EXISTS site_rankings (
    place_id          TEXT PRIMARY KEY REFERENCES businesses(place_id),
    serp_rank         INTEGER,
    serp_keyword      TEXT,
    domain_authority  REAL,
    backlinks         INTEGER,
    ai_mentioned      INTEGER,
    ai_cited          INTEGER,
    ai_engine         TEXT,
    est_cost          REAL,
    measured_ts       TEXT
);

-- Best-effort contact scrape (email + owner name) from each lead's website, one row
-- per lead scraped. Phone already lives on businesses (from Places); this adds the
-- email + owner the public site exposes, to enable outreach. Free (HTTP/time only),
-- never fabricated - anything not found is left empty.
CREATE TABLE IF NOT EXISTS site_contacts (
    place_id          TEXT PRIMARY KEY REFERENCES businesses(place_id),
    email             TEXT,      -- best single email (own-domain / info@ preferred)
    emails_json       TEXT,      -- all emails found
    owner_name        TEXT,      -- best owner/founder guess (from JSON-LD)
    owner_hints_json  TEXT,      -- text snippets near owner/founder keywords
    extra_phones_json TEXT,      -- phones on the site (beyond the Places phone)
    pages_checked     INTEGER,
    scraped_ts        TEXT
);
"""


def connect(db_path=DEFAULT_DB):
    """Open the store with sane pragmas. Creates the parent dir but not the schema."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # safe concurrent reads during long sweeps
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn):
    """Create all tables/indexes if absent. Idempotent (safe to re-run)."""
    import leads_db_grid  # sibling module; owns the geo-grid tables (ADR-LP-001)
    import leads_db_gbp  # sibling module; owns the GBP-audit table (prospect neglect audit)

    conn.executescript(SCHEMA)
    leads_db_grid.create_grid_tables(conn)
    leads_db_gbp.create_gbp_tables(conn)
    conn.commit()


def status(conn):
    """Return a small dict summary for the `status` CLI / health checks."""
    cur = conn.cursor()
    biz = cur.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    no_web = cur.execute("SELECT COUNT(*) FROM businesses WHERE no_website=1").fetchone()[0]
    cells = cur.execute("SELECT COUNT(*) FROM swept_cells").fetchone()[0]
    runs = cur.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    states = cur.execute(
        "SELECT COUNT(DISTINCT state_code) FROM businesses WHERE state_code != ''"
    ).fetchone()[0]
    return {
        "businesses": biz,
        "no_website": no_web,
        "states": states,
        "swept_cells": cells,
        "runs": runs,
    }


# Column order for inserts (matches the businesses table).
_BIZ_COLUMNS = (
    "place_id",
    "name",
    "address",
    "state_code",
    "state_name",
    "country",
    "lat",
    "lng",
    "phone",
    "website",
    "no_website",
    "rating",
    "review_count",
    "business_status",
    "primary_type",
    "types_json",
    "trade_bucket",
    "relevance",
    "maps_url",
    "found_via_json",
    "first_seen_ts",
    "last_refreshed_ts",
)

# Fields re-pulled when a known business is refreshed (FR-10). place_id and
# first_seen_ts are never overwritten (SEC-4: place_id is the durable anchor).
_REFRESHABLE = (
    "name",
    "address",
    "phone",
    "website",
    "no_website",
    "rating",
    "review_count",
    "business_status",
    "primary_type",
    "types_json",
    "maps_url",
    "lat",
    "lng",
)


def upsert_business(conn, b, now, refresh=False):
    """Insert a business or merge into the existing row (FR-8 dedup by place_id).

    New row  -> inserted with first_seen_ts/last_refreshed_ts = now.
    Existing -> found_via is unioned and last_refreshed_ts touched; the other fields
                are overwritten only when refresh=True (FR-10). Returns 'inserted'
                or 'updated'.
    """
    pid = b["place_id"]
    row = conn.execute(
        "SELECT found_via_json, first_seen_ts FROM businesses WHERE place_id=?", (pid,)
    ).fetchone()
    found_via = set(b.get("found_via") or [])
    if row is None:
        rec = dict(b)
        rec["found_via_json"] = json.dumps(sorted(found_via))
        rec["first_seen_ts"] = now
        rec["last_refreshed_ts"] = now
        rec.setdefault("types_json", json.dumps(b.get("types") or []))
        cols = ", ".join(_BIZ_COLUMNS)
        ph = ", ".join("?" for _ in _BIZ_COLUMNS)
        conn.execute(
            f"INSERT INTO businesses ({cols}) VALUES ({ph})",
            tuple(rec.get(c) for c in _BIZ_COLUMNS),
        )
        return "inserted"
    # merge discovery provenance regardless of mode
    found_via |= set(json.loads(row["found_via_json"] or "[]"))
    if refresh:
        sets = ", ".join(f"{c}=?" for c in _REFRESHABLE)
        vals = [b.get(c) for c in _REFRESHABLE]
        conn.execute(
            f"UPDATE businesses SET {sets}, found_via_json=?, last_refreshed_ts=? WHERE place_id=?",
            (*vals, json.dumps(sorted(found_via)), now, pid),
        )
    else:
        conn.execute(
            "UPDATE businesses SET found_via_json=?, last_refreshed_ts=? WHERE place_id=?",
            (json.dumps(sorted(found_via)), now, pid),
        )
    return "updated"


def cell_is_swept(conn, swept_id):
    """True if this (cell, trade bucket) was already queried (FR-9 resumability)."""
    return (
        conn.execute("SELECT 1 FROM swept_cells WHERE cell_id=?", (swept_id,)).fetchone()
        is not None
    )


def record_cell(conn, swept_id, cell, bucket, region, result_count, saturated, now):
    """Mark a (cell, bucket) swept so additive re-runs skip it (FR-9/FR-10)."""
    conn.execute(
        "INSERT OR REPLACE INTO swept_cells "
        "(cell_id, center_lat, center_lng, radius_m, trade_bucket, region, result_count, saturated, swept_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            swept_id,
            cell.lat,
            cell.lng,
            cell.radius_m,
            bucket,
            region,
            result_count,
            1 if saturated else 0,
            now,
        ),
    )


def start_run(conn, region, budget, mode, now):
    """Open a run row (audit trail, FR-7); returns run_id."""
    cur = conn.execute(
        "INSERT INTO runs (region, budget, mode, started_ts, requests_spent, est_cost) VALUES (?,?,?,?,0,0)",
        (region, budget, mode, now),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, now, requests_spent, est_cost):
    conn.execute(
        "UPDATE runs SET ended_ts=?, requests_spent=?, est_cost=? WHERE run_id=?",
        (now, requests_spent, round(est_cost, 2), run_id),
    )
    conn.commit()


# Column order for site_enrichment inserts (matches the table).
_ENRICH_COLUMNS = (
    "place_id",
    "fetched_url",
    "http_status",
    "reachable",
    "site_status",
    "https",
    "mobile_viewport",
    "title_len",
    "meta_desc_len",
    "word_count",
    "jsonld_present",
    "schema_localbusiness",
    "schema_faq",
    "llms_txt",
    "ai_bots_blocked",
    "readiness_score",
    "seo_gaps_json",
    "builder",
    "marketing_tags_json",
    "agency_credit",
    "google_ads",
    "mgmt_status",
    "mgmt_confidence",
    "mgmt_evidence_json",
    "fetched_via",
    "enriched_ts",
)


def upsert_enrichment(conn, e, now):
    """Insert or replace one lead's enrichment row (FR-15/16). Keyed on place_id, so
    a re-enrich (FR-15 refresh) overwrites the prior cache row in place."""
    rec = dict(e)
    rec["enriched_ts"] = now
    cols = ", ".join(_ENRICH_COLUMNS)
    ph = ", ".join("?" for _ in _ENRICH_COLUMNS)
    conn.execute(
        f"INSERT OR REPLACE INTO site_enrichment ({cols}) VALUES ({ph})",
        tuple(rec.get(c) for c in _ENRICH_COLUMNS),
    )


def enrichment_exists(conn, place_id):
    """True if this lead was already enriched (FR-15 resumability)."""
    return (
        conn.execute("SELECT 1 FROM site_enrichment WHERE place_id=?", (place_id,)).fetchone()
        is not None
    )


def leads_to_enrich(conn, state_codes=None, only_unenriched=True, limit=None):
    """Leads that have a website and so are enrichable (FR-15). Most-reviewed first so a
    partial/capped run covers the most prominent prospects. `state_codes` (lowercased
    set) filters by region; `only_unenriched` skips leads already in site_enrichment."""
    where = ["website IS NOT NULL", "website != ''", "no_website = 0"]
    params = []
    if state_codes:
        where.append("LOWER(state_code) IN (%s)" % ",".join("?" for _ in state_codes))
        params.extend(state_codes)
    if only_unenriched:
        where.append("place_id NOT IN (SELECT place_id FROM site_enrichment)")
    sql = (
        "SELECT place_id, name, website, state_code, relevance "
        "FROM businesses WHERE "
        + " AND ".join(where)
        + " ORDER BY COALESCE(review_count, 0) DESC, place_id"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


# site_status values worth retrying with a heavier fetcher (Firecrawl): a WAF block
# or a transport failure may succeed on a proxied/rendered retry. 'social_only' and
# 'directory' are deliberately excluded - they are correctly classified (no real
# business site exists to recover), so retrying them only wastes time, never spend.
RETRYABLE_SITE_STATUSES = ("blocked", "unreachable", "dead", "parked")


def leads_to_reenrich(conn, statuses=RETRYABLE_SITE_STATUSES, state_codes=None, limit=None):
    """Leads whose prior enrichment FAILED to read the site (FR-15 resumability):
    site_status in `statuses` (default: the recoverable failure modes). Lets a retry
    pass hit only the blocked/unreachable subset through a heavier fetcher without
    re-touching the live rows. Most-reviewed first; same row shape as leads_to_enrich."""
    where = [
        "b.website IS NOT NULL",
        "b.website != ''",
        "b.no_website = 0",
        "e.site_status IN (%s)" % ",".join("?" for _ in statuses),
    ]
    params = list(statuses)
    if state_codes:
        where.append("LOWER(b.state_code) IN (%s)" % ",".join("?" for _ in state_codes))
        params.extend(state_codes)
    sql = (
        "SELECT b.place_id, b.name, b.website, b.state_code, b.relevance "
        "FROM businesses b JOIN site_enrichment e ON e.place_id = b.place_id "
        "WHERE " + " AND ".join(where) + " ORDER BY COALESCE(b.review_count, 0) DESC, b.place_id"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def enrichment_status(conn):
    """Counts for the enrichment CLI / health checks."""
    cur = conn.cursor()
    with_site = cur.execute(
        "SELECT COUNT(*) FROM businesses WHERE no_website=0 AND website IS NOT NULL AND website!=''"
    ).fetchone()[0]
    enriched = cur.execute("SELECT COUNT(*) FROM site_enrichment").fetchone()[0]
    by_mgmt = {
        r[0]: r[1]
        for r in cur.execute(
            "SELECT mgmt_status, COUNT(*) FROM site_enrichment GROUP BY mgmt_status ORDER BY COUNT(*) DESC"
        )
    }
    return {"with_website": with_site, "enriched": enriched, "by_mgmt": by_mgmt}


# Column order for site_rankings inserts (matches the table). FR-17 paid pass.
_RANKING_COLUMNS = (
    "place_id",
    "serp_rank",
    "serp_keyword",
    "domain_authority",
    "backlinks",
    "ai_mentioned",
    "ai_cited",
    "ai_engine",
    "est_cost",
    "measured_ts",
)


def upsert_ranking(conn, r, now):
    """Insert or replace one lead's paid-measurement row (FR-17). Keyed on place_id,
    so a re-measure (--refresh) overwrites the prior row in place."""
    rec = dict(r)
    rec["measured_ts"] = now
    cols = ", ".join(_RANKING_COLUMNS)
    ph = ", ".join("?" for _ in _RANKING_COLUMNS)
    conn.execute(
        f"INSERT OR REPLACE INTO site_rankings ({cols}) VALUES ({ph})",
        tuple(rec.get(c) for c in _RANKING_COLUMNS),
    )


def ranking_exists(conn, place_id):
    """True if this lead was already measured (FR-17 resumability)."""
    return (
        conn.execute("SELECT 1 FROM site_rankings WHERE place_id=?", (place_id,)).fetchone()
        is not None
    )


def shortlist_candidates(conn, state_codes=None, place_ids=None, only_unmeasured=True):
    """Leads eligible for the paid pass (FR-17), best-opportunity first.

    Default (top-N by opportunity): leads that have a website AND a live enrichment
    with a readiness score, ordered by readiness DESC (weakest live site = best
    prospect) then review_count DESC. `place_ids` (explicit list) overrides the
    opportunity filter and returns exactly those leads. `only_unmeasured` skips
    leads already in site_rankings (resumable)."""
    sel = (
        "SELECT b.place_id, b.name, b.website, b.address, b.state_code, b.state_name, "
        "b.country, b.review_count, e.readiness_score, e.site_status "
        "FROM businesses b LEFT JOIN site_enrichment e ON e.place_id = b.place_id "
    )
    where, params = [], []
    if place_ids:
        where.append("b.place_id IN (%s)" % ",".join("?" for _ in place_ids))
        params.extend(place_ids)
    else:
        where += [
            "b.website IS NOT NULL",
            "b.website != ''",
            "b.no_website = 0",
            "e.readiness_score IS NOT NULL",
        ]
        if state_codes:
            where.append("LOWER(b.state_code) IN (%s)" % ",".join("?" for _ in state_codes))
            params.extend(state_codes)
    if only_unmeasured:
        where.append("b.place_id NOT IN (SELECT place_id FROM site_rankings)")
    sql = (
        sel
        + "WHERE "
        + " AND ".join(where)
        + " ORDER BY COALESCE(e.readiness_score, -1) DESC, COALESCE(b.review_count, 0) DESC, b.place_id"
    )
    return conn.execute(sql, params).fetchall()


def ranking_status(conn):
    """Counts for the paid-pass CLI / health checks."""
    cur = conn.cursor()
    measured = cur.execute("SELECT COUNT(*) FROM site_rankings").fetchone()[0]
    spent = cur.execute("SELECT COALESCE(SUM(est_cost), 0) FROM site_rankings").fetchone()[0]
    return {"measured": measured, "spent": round(spent or 0, 2)}


# Column order for site_contacts inserts (matches the table).
_CONTACT_COLUMNS = (
    "place_id",
    "email",
    "emails_json",
    "owner_name",
    "owner_hints_json",
    "extra_phones_json",
    "pages_checked",
    "scraped_ts",
)


def upsert_contact(conn, c, now):
    """Insert or replace one lead's scraped-contact row. Keyed on place_id, so a
    re-scrape overwrites the prior row in place."""
    rec = dict(c)
    rec["scraped_ts"] = now
    cols = ", ".join(_CONTACT_COLUMNS)
    ph = ", ".join("?" for _ in _CONTACT_COLUMNS)
    conn.execute(
        f"INSERT OR REPLACE INTO site_contacts ({cols}) VALUES ({ph})",
        tuple(rec.get(c2) for c2 in _CONTACT_COLUMNS),
    )


def contact_exists(conn, place_id):
    """True if this lead's site was already scraped (resumability)."""
    return (
        conn.execute("SELECT 1 FROM site_contacts WHERE place_id=?", (place_id,)).fetchone()
        is not None
    )


def leads_to_scrape(conn, state_codes=None, place_ids=None, only_unscraped=True, limit=None):
    """Leads with a website to scrape for email/owner. `place_ids` (explicit list)
    targets exactly those leads (still must have a website); else filtered by region.
    Most-reviewed first; `only_unscraped` skips leads already in site_contacts."""
    where = ["website IS NOT NULL", "website != ''", "no_website = 0"]
    params = []
    if place_ids:
        where.append("place_id IN (%s)" % ",".join("?" for _ in place_ids))
        params.extend(place_ids)
    elif state_codes:
        where.append("LOWER(state_code) IN (%s)" % ",".join("?" for _ in state_codes))
        params.extend(state_codes)
    if only_unscraped:
        where.append("place_id NOT IN (SELECT place_id FROM site_contacts)")
    sql = (
        "SELECT place_id, name, website, state_code, review_count FROM businesses WHERE "
        + " AND ".join(where)
        + " ORDER BY COALESCE(review_count, 0) DESC, place_id"
    )
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def contact_status(conn):
    """Counts for the scrape CLI / health checks."""
    cur = conn.cursor()
    scraped = cur.execute("SELECT COUNT(*) FROM site_contacts").fetchone()[0]
    with_email = cur.execute(
        "SELECT COUNT(*) FROM site_contacts WHERE email IS NOT NULL AND email!=''"
    ).fetchone()[0]
    with_owner = cur.execute(
        "SELECT COUNT(*) FROM site_contacts WHERE owner_name IS NOT NULL AND owner_name!=''"
    ).fetchone()[0]
    return {"scraped": scraped, "with_email": with_email, "with_owner": with_owner}


def main():
    ap = argparse.ArgumentParser(
        description="Initialize / inspect the prospect_sweep master store (FR-7)."
    )
    ap.add_argument("command", choices=["init", "status"])
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    conn = connect(args.db)
    init_db(conn)  # always ensure schema exists, even for `status`
    if args.command == "init":
        print(f"Initialized master store at {args.db} (businesses, swept_cells, runs).")
    else:
        s = status(conn)
        print(
            f"{args.db}: {s['businesses']} businesses ({s['no_website']} no-website) "
            f"across {s['states']} states/provinces; "
            f"{s['swept_cells']} cells swept; {s['runs']} runs recorded."
        )
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
