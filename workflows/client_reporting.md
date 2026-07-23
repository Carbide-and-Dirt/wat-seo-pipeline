# Workflow: Geo-Grid Client Reporting

## Objective
Measure a signed client's Google Maps rank at every pin of a geographic grid, track it monthly,
diff it against the baseline ("at signup"), and produce the Steel & Amber heatmap + metrics section
that the downstream client-report assembler drops into the monthly PDF. Two lifecycles: a **baseline** scan at sale
time (the before-state anchor), and **monthly** re-scans that feed the diff.

## Inputs
- **Client's `targets/<client>.json`** — must contain a `grid` block (see `targets/example.json`).
- **`data/leads.sqlite`** — the master store; grid tables are created by `leads_db.py init`.
- **`DATAFORSEO_LOGIN` + `DATAFORSEO_PASSWORD` in `.env`** (paid scan only; `grid_diff` and
  `grid_heatmap` are free).

## Outputs
- **`data/leads.sqlite`** — `grid_scans` + `grid_points` tables.
- **`.tmp/grid/diff_<slug>.json`** — diff summary JSON.
- **`output/<place>-grid.svg`** — Steel & Amber heatmap SVG.
- **HTML `<section>`** — from `grid_report_section.py`; consumed by the downstream client-report assembler.

## Tools

- `geo_grid.py` — **PAID** scan: runs one DataForSEO Maps request per (pin × keyword), stores rank in `grid_points`, computes SoLV.
- `leads_db_grid.py` — owns the `grid_scans` / `grid_points` schema + read/write helpers; called by all grid tools.
- `grid_diff.py` — **free ($0)** pure reducer: baseline vs latest scan → headline changes, per-keyword SoLV delta, biggest movers.
- `grid_heatmap.py` — **free ($0)** Steel & Amber SVG heatmap (single grid or before/after pair).
- `grid_report_section.py` — **free ($0)** assembles a self-contained HTML `<section>` for the downstream client-report assembler.

## Lifecycle

### 1. Add the client's grid config

In `targets/<client>.json`, add (or confirm) a `grid` block:
```json
"grid": {
  "rows": 7,
  "cols": 7,
  "spacing_km": 1.6,
  "zoom": 14,
  "depth": 20,
  "keywords": ["excavating contractor nashville", "land clearing nashville"]
},
"paid": {
  "maps_priority": "live"
}
```
A 7×7 grid at 1.6 km covers roughly a 10×10 km service area. `priority: live` is the only
wired mode ($0.002/request); `standard`/`priority` await an async task flow that does not
exist yet.

### 2. Baseline scan (at sale time — run once)

```bash
# Dry-run first — $0, confirms request count + cost
python tools/geo_grid.py --config targets/<client>.json \
    --place-id <ChIJ...> --center-lat 36.16 --center-lng -86.78 \
    --scan-type baseline --dry-run

# Live — requires a HARD --budget
python tools/geo_grid.py --config targets/<client>.json \
    --place-id <ChIJ...> --center-lat 36.16 --center-lng -86.78 \
    --scan-type baseline --budget 1.50
```

The baseline scan is the "before" anchor for every future diff. It is stored with
`scan_type='baseline'` and is never overwritten by a monthly scan.

### 3. Monthly re-scan

Same command with `--scan-type monthly`. Each monthly scan is a new row in `grid_scans`;
`grid_diff.py` auto-selects baseline vs latest when you don't specify IDs.

### 4. Diff (free, re-runnable)

```bash
python tools/grid_diff.py --place-id <ChIJ...>
# or write to file:
python tools/grid_diff.py --place-id <ChIJ...> --md output/acme-grid-diff.md
```

Compares baseline vs the latest non-baseline scan. Positive numbers always mean improvement.
Emits JSON summary to `.tmp/grid/diff_<slug>.json` and prints a markdown table to stdout.

### 5. Heatmap (free, re-runnable)

```bash
# Before-after pair (the sales money-shot):
python tools/grid_heatmap.py --place-id <ChIJ...> --mode before-after \
    --out output/acme-grid.svg

# Single current grid, filtered by keyword:
python tools/grid_heatmap.py --place-id <ChIJ...> --mode current \
    --keyword "land clearing nashville"
```

SVG is self-contained (inline, PDF-safe). Amber = 3-pack, fading to dark steel = absent.

### 6. Report section (free, for the downstream client-report assembler)

```python
import sqlite3
from tools.grid_report_section import build_grid_section

conn = sqlite3.connect("data/leads.sqlite")
section_html = build_grid_section(conn, client_place_id)
# Drop section_html into the report body; the client-report assembler renders it to PDF (Chromium).
```

Returns `''` if no grid data exists for the `place_id`. Falls back to a baseline-only panel
when only one scan is present (no diff yet).

## Expected outputs

| Step | Output |
|------|--------|
| baseline scan | `grid_scans` row (`scan_type='baseline'`) + `grid_points` rows |
| monthly scan | new `grid_scans` row + `grid_points` rows |
| diff | `.tmp/grid/diff_<slug>.json` + stdout markdown |
| heatmap | `output/<place>-grid.svg` |
| report section | HTML `<section>` string (caller concatenates into report HTML) |

## Gotchas

- **Only `priority: live` is wired ($0.002/request).** `standard` and `priority` queue modes
  need an async `task_post` / `task_get` flow that `dataforseo.py` does not have yet. Attempting
  them in a live run raises `NotImplementedError`; `--dry-run` still estimates any priority.

- **Always `--dry-run` before a live scan.** The dry-run prints the exact request count and
  dollar estimate. A live run requires a HARD `--budget`; it stops cleanly before the request
  that would exceed it and marks the scan `partial`. Partial scans are resumable: re-run with
  the same `--place-id` and `--budget` to continue (completed pins are idempotent, never
  re-fetched).

- **Confirm the DataForSEO Maps item field names against the first live response.** The field
  names (`place_id`, `rank_absolute`, `rank_group`) are confirmed in `geo_grid.extract_rank`,
  but DataForSEO can change field names between API versions. Inspect the first live scan's
  raw JSON (`--save-raw` flag) before trusting rank values in bulk.

- **Match is by `place_id` only — never by name.** `geo_grid.py` ignores the business name
  in the Maps results. The client's `place_id` must be exact; a wrong `place_id` silently
  returns "not found" for every pin.

- **`data/leads.sqlite` is shared.** The grid tables (`grid_scans`, `grid_points`) live in the
  same SQLite file as the national prospect store. `leads_db.py init` creates them all. Do not
  move or rename the DB or the `data/` folder — downstream tooling reads it read-only at this
  fixed relative path.

- **`grid_report_section.py` is consumed by the downstream client-report assembler** (`build_grid_section(conn, place_id)`).
  The returned HTML is a `<section>` with inline styles and inline SVG — no external files,
  PDF-safe. The caller concatenates it into the full report HTML before rendering the PDF.

- **Tests (no network, no spend):** `test_geo_grid.py` (geometry, SoLV, budget-stop/resume,
  faked client), `test_grid_diff.py`, `test_grid_heatmap.py`, `test_grid_report_section.py`.
  Run `pytest tests/` or any file standalone.
