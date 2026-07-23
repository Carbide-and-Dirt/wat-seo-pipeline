# DESIGN: Geo-Grid Rank Tracker (`geo_grid.py` + reporting)

**Status:** Built 2026-07-04 (ADR-LP-001 Addendum A); documented 2026-07-08.
**Scope:** Client delivery / proof. A SIGNED client the agency measures monthly. Read-only Maps
rank measurement, no owner access needed.
**Related:** ARCHITECTURE.md (this is durable asset #3, the longitudinal proof store), ADR-LP-001
(owner-side control layer, approval-gated, NOT this), `DESIGN-gbp-prospect-audit.md` (the sibling
paid subsystem), `measure_shortlist.py` (shared paid-pass discipline), `leads_db.py` (schema owner).

---

## Why this exists

When a client signs on for local-visibility work, the deliverable is "show up on Google Maps
across your service area." Proving delivery needs a **before/after record**: the client's own
Maps rank, measured at many points across their town, at signup and every month after. That
longitudinal record cannot be reconstructed retroactively, so it is durable asset #3 in
ARCHITECTURE.md.

A single "what rank are we" number is worthless for a local business: Google personalizes Maps
results by the searcher's location, so rank varies pin to pin across a city. The honest measure is
a **geo-grid**: query Maps from a lattice of coordinates covering the service area, record the
client's rank at each, and reduce to **Share of Local Voice (SoLV)** plus supporting metrics.

**Data source (same boundary as the rest of the skill):** DataForSEO's Google Maps SERP endpoint,
over the Basic auth `dataforseo.py` already holds. Match to the client is by `place_id` only, never
by name. No Google Maps scraping, no owner OAuth.

## Pricing (official DataForSEO Maps SERP table)

| Priority | $ / request | Latency | Wired? |
|---|---|---|---|
| standard | $0.0006 | ~5 min (async queue) | No (needs task_post/task_get flow) |
| priority | $0.0012 | faster async | No |
| **live** | **$0.002** | ~6 s | **Yes** (`/v3/serp/google/maps/live/advanced`) |

- **One request = one (pin x keyword).** Cost = `rows * cols * n_keywords * rate`.
- A 7x7 grid at 1.6 km spacing (roughly a 10x10 km service area) over 2 keywords = 98 requests =
  **~$0.196 per scan** on the live queue. A baseline plus twelve monthly scans is under $3/yr/client.
- Only `live` is wired today: `standard`/`priority` are cheaper but need an async task flow
  `dataforseo.py` does not have yet. `--dry-run` still estimates any priority; a live run refuses
  anything but `live` (clear error, no silent fallback).

---

## Tool: `tools/geo_grid.py` (PAID scan)

Standalone argparse CLI from repo root, matching every skill convention: reuses `dataforseo.py`
creds/post (DRY), keys on `place_id`, writes to `leads.sqlite` via `leads_db_grid.py`, and follows
the paid-pass discipline verbatim: **always `--dry-run` first; a live run REQUIRES a hard
`--budget` that stops before the request that would cross it; scans are resumable.**

### CLI

```bash
# $0 cost estimate -- makes no API calls
python tools/geo_grid.py --config targets/example.json --place-id ChIJ... \
    --center-lat 39.50 --center-lng -98.35 --scan-type baseline --dry-run

# live run, hard-capped; stops partway and marks the scan 'partial' if the cap binds
python tools/geo_grid.py --config targets/example.json --place-id ChIJ... \
    --center-lat 39.50 --center-lng -98.35 --scan-type baseline --budget 1.50
```

Flags: `--config` (a `targets/*.json` with a `grid` block), `--place-id` (the match key),
`--center-lat/--center-lng` (grid center), `--scan-type {baseline,monthly,adhoc}`,
`--keywords` (override the config list), `--priority` (override `paid.maps_priority`),
`--dry-run`, `--budget`, `--save-raw` (persist raw per-pin JSON under `.tmp/grid/` for audit).

### Input / identity / config

The `grid` block in `targets/<client>.json` drives the scan:

```json
"grid": { "rows": 7, "cols": 7, "spacing_km": 1.6, "zoom": 14, "depth": 20,
          "keywords": ["excavation springfield", "grading contractor springfield"] },
"paid": { "maps_priority": "live" }
```

- **Grid geometry** (`build_grid`, pure/tested): `rows x cols` points centered on the given
  coordinate, `spacing_km` apart, WGS84 conversion. `(row 0, col 0)` is the NW corner so the grid
  maps directly onto the rendered heatmap.
- **Match-back is by `place_id`.** `extract_rank` walks the Maps result items (skipping ad/non-
  business rows without a `place_id`), prefers DataForSEO's `rank_absolute`, falls back to
  `rank_group` then scanned position. A pin where the target never appears within `depth` records
  `rank = NULL` (not an error). **One implementation-time confirm remains** (same class as
  `gbp_audit`): verify the live item field names against the first real response; the extractor is
  defensive but the wire format is unconfirmed at scale.

### Schema (owned by `leads_db.py` via `leads_db_grid.py`)

Two tables. `grid_scans` is **append-only** (a baseline row is never overwritten; each monthly
scan is a new row), which is exactly what makes the before/after proof store durable.

```sql
CREATE TABLE grid_scans (             -- one scan run for one business
    id           INTEGER PRIMARY KEY,
    place_id     TEXT NOT NULL,        -- -> businesses.place_id (canonical key)
    scan_type    TEXT NOT NULL,        -- baseline | monthly | adhoc
    grid_rows    INTEGER, grid_cols INTEGER, spacing_km REAL,
    center_lat   REAL, center_lng REAL, zoom INTEGER, depth INTEGER,
    keywords     TEXT NOT NULL,        -- JSON array
    priority     TEXT NOT NULL,        -- standard | priority | live
    solv         REAL,                 -- Share of Local Voice (computed reducer)
    avg_rank     REAL, top3_share REAL, found_share REAL,
    api_cost_usd REAL,                 -- actual spend, for COGS
    status       TEXT NOT NULL,        -- pending | complete | partial | error
    scanned_ts   TEXT NOT NULL, last_refreshed_ts TEXT NOT NULL
);
CREATE TABLE grid_points (            -- one row per (pin x keyword)
    id             INTEGER PRIMARY KEY,
    grid_scan_id   INTEGER NOT NULL REFERENCES grid_scans(id) ON DELETE CASCADE,
    row INTEGER, col INTEGER, keyword TEXT NOT NULL,
    point_lat REAL, point_lng REAL,
    rank           INTEGER,            -- NULL = not found within depth
    found_place_id TEXT,               -- matched by place_id, never by name
    result_depth   INTEGER,
    raw_ref        TEXT,               -- .tmp/ path to raw JSON (audit only)
    UNIQUE (grid_scan_id, row, col, keyword)   -- makes a scan resumable
);
```

**ToS posture:** `place_id` is the durable key; every rank row is a refreshable cache stamped
`last_refreshed_ts`. Raw Maps JSON is persisted only under `.tmp/` and only with `--save-raw`
(regenerable audit trail, never the store of record). No reviewer names, review text, or consumer
PII is stored.

**Resumability:** the `UNIQUE (scan, row, col, keyword)` constraint plus `done_cells()` let an
interrupted or budget-stopped scan resume with no re-spend; cells already fetched are skipped, and
a partial run is marked `status='partial'` so a later run finishes it.

### Share of Local Voice reducer (`compute_solv`, pure/tested, `score_report.py` style)

Deterministic, no spend, re-runnable. Reused verbatim by `grid_diff.py` so the diff and the scan
agree by construction.

- **Per-pin visibility:** `rank 1 -> 1.0`, decaying linearly to `~0.05` at rank 20; `0` beyond
  `depth` or not found. `max_rank = min(depth, 20)`.
- **SoLV** = mean visibility across every `(pin x keyword)`, scaled 0-100. This is the headline
  number ("how much of the local map do you own").
- **Supporting metrics:** `avg_rank` (mean rank of pins where found; `None` if never found),
  `top3_share` (fraction of ALL pins in the local 3-pack), `found_share` (fraction of pins where
  the business appeared at all). All bounded and honest: a business that is invisible scores 0, not
  a fabricated floor.

`rank_tier` buckets a pin for the heatmap: `top3` (<=3), `page1` (<=10), `deep` (<=20),
`absent` (not found / >20).

### Failure / degraded behavior

- A pin with **no local pack** for a keyword (DataForSEO task error `40102` "No Search Results")
  is **data, not failure**: the business is simply absent there (the darkest heatmap tier). The
  client records an empty result set and the scan continues, rather than aborting.
- Missing `DATAFORSEO_LOGIN`/`DATAFORSEO_PASSWORD` -> hard exit before any DB or network work.
- Budget cap reached -> clean stop, `status='partial'`, resume later.

---

## Reporting integration (the free $0 half)

Once scans exist, the whole client-facing artifact is pure reducers over `leads.sqlite` -- no
network, no spend, re-runnable at will.

```
grid_scans / grid_points
        |
        v
   grid_diff.py  --(baseline vs latest)-->  headline metrics, per-keyword SoLV, biggest movers
        |                                         (positive numbers ALWAYS mean improvement)
        v
 grid_heatmap.py --> Steel & Amber SVG (single grid or before/after pair, PDF-safe)
        |
        v
grid_report_section.py : build_grid_section(conn, place_id) -> self-contained HTML <section>
        |
        v  (FILE HANDOFF, never a cross-repo import)
   a downstream report assembler folds it into the monthly PDF
```

- **`grid_diff.py`** (`diff_scans`, pure): compares the baseline scan against the latest non-
  baseline scan. Per-pin classification (`improved` / `declined` / `unchanged` / `gained` / `lost`
  / `still_absent`), per-keyword SoLV change, top gains and drops, and `net_pins_improved`. Every
  metric is sign-corrected so **positive = better** (for `avg_rank`, improvement = baseline -
  current, since lower rank is better). Emits a **geometry-change warning** if grid size/spacing/
  center moved between scans, because per-pin deltas are only valid on matching cells.
- **`grid_heatmap.py`** (`render_heatmap`, `render_before_after`): Steel & Amber inline-SVG
  heatmap, single grid or the before/after money-shot. `cells_for_keyword` / `cells_aggregate`
  pick the pin set. Inline styles + inline SVG only, so the PDF renderer needs no external
  assets.
- **`grid_report_section.py`** (`build_grid_section`): one call returns a ready-to-embed HTML
  `<section>` (heatmap + headline table + per-keyword table + movers), or `""` when the business
  has no scans. If only a baseline exists it renders a single-grid section ("comparison begins with
  your next report"). This is the **N3 handoff boundary**: it writes a section file that
  a downstream report assembler concatenates; neither repo imports the other.

## Lifecycle placement

```
   SALE                         + ~30 days, monthly                 EACH MONTH
geo_grid --scan-type baseline   geo_grid --scan-type monthly    grid_diff -> grid_report_section
   (the "before" anchor)          (a new grid_scans row)          -> file -> monthly PDF report
```

SOP: `workflows/client_reporting.md`. The baseline is run once at sale time and never overwritten;
monthly scans accrete; the diff auto-selects baseline vs latest.

## Tests (no network, no spend)

- `tests/test_geo_grid.py` -- grid geometry (NW-corner ordering, spacing), SoLV boundary math,
  `extract_rank` place_id match-back (wrong id dropped, ad rows skipped), budget-stop + resume,
  and the `40102`-as-absent path, against a faked Maps client.
- `tests/test_grid_diff.py`, `tests/test_grid_heatmap.py`, `tests/test_grid_report_section.py` --
  the reducer/render contract (positive = improvement, geometry warning, single-scan fallback,
  empty-input `""`). All run under the standalone runner (`python tests/test_*.py`).

## Status and open items

**Built and unit-tested; not yet run on a real client cadence.** The store currently holds 2
sample `grid_scans` / 196 `grid_points` (validation scans, not a live baseline/monthly client
pair) -- the geo-grid analog of the empty `site_rankings`. Open items:

1. **Live field-name confirm (the one real to-do):** confirm the Maps `live/advanced` item fields
   (`rank_absolute`, `place_id`, `items` path, `cost`) against the first real response before a
   bulk cadence. Deterministic logic is fully tested; only the wire format is unconfirmed.
2. **Only `live` priority is wired.** For a fleet of clients, build the async `standard`/`priority`
   task_post/task_get flow in `dataforseo.py` to cut per-scan cost ~3x. Not urgent at current
   client count.
3. **PDF render** happens in the downstream report assembler via Playwright Chromium (resolved
   2026-07-16; ARCHITECTURE.md Phase C); the HTML section builds with no renderer present at all.

## What this is NOT

Not the owner-side control layer (ADR-LP-001): no writes, no OAuth, no posting or review replies,
no approval gate. Read-only rank measurement of a business we do not need access to. Not the report
shell either -- this subsystem ends at the section-file handoff; the downstream report assembler
owns assembly and branding.
