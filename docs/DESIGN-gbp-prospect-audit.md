# DESIGN: Prospect-side GBP Neglect Audit (`gbp_audit.py`)

**Status:** Proposed (no code written yet)
**Date:** 2026-07-05
**Scope:** Sales / prospecting only. Cold prospects the agency does NOT manage (no owner OAuth).
**Related:** ADR-LP-001 (owner-side control layer, approval-gated, NOT this), `prospect_sweep.py`,
`enrich_sites.py`, `geo_grid.py`, `leads_db.py`, `measure_shortlist.py` (paid-pass discipline).

---

## Why this exists

To pitch a business on GBP improvements, the pipeline needs evidence that its Google Business
Profile is neglected. The owner-scoped Google GBP APIs (Business Information, v4, Performance)
are closed for a cold prospect (they require the owner to grant manager access, plus Google's
Basic API Access application). So the prospecting pass needs a
signal that can be gathered for **any public listing, without access, without scraping**.

**Finding (researched 2026-07-05, primary sources):** DataForSEO's Business Data API returns
the GBP completeness fields for any public business, ToS-safe, over the same Basic auth the
skill's `dataforseo.py` already uses. **No Google Maps scraping is needed or wanted.**

Two endpoints carry everything:

| Endpoint | Gives us | Field names (exact) |
|---|---|---|
| `business_data/google/my_business_info` | claimed status, rating, categories, photos count, hours, description, attributes | `is_claimed`, `rating{value,votes_count}`, `rating_distribution`, `category`, `additional_categories`, `description`, `snippet`, `total_photos`, `main_image`, `work_time.work_hours`, `attributes{available,unavailable}`, `price_level`, `phone`, `url`, `address` |
| `business_data/google/my_business_updates` | GMB posts + recency | post `text`, post URL, image, author, **post date + timestamp** |

Google Places API (New) is the weaker option here: it does **not** return `is_claimed`, posts,
services, an owner description, or photo dates, and hides hours/reviews/attributes behind its
Enterprise SKU. Keep Places for discovery/reviews (`places_*`); use DataForSEO Business Data
for the GBP audit.

## Pricing (official DataForSEO Business Data table, verified 2026-07-05)

| Endpoint | Standard | Priority | Live |
|---|---|---|---|
| My Business Info | $0.0015 / req ($1.50/1k) | $0.003 | $0.0054 |
| My Business Updates | $0.0015/task + $0.00075 / 10 updates (~$2.25/1k) | 2x | (no Live) |

- **Full audit (both endpoints, Standard): ~$0.004 / prospect (~$4 / 1,000).**
- **Info-only (skip posts): $0.0015 / prospect (~$1.50 / 1,000).**
- Cost is a non-issue at this scale; the `--budget` gate is about discipline, not affordability.

---

## Tool: `tools/gbp_audit.py`

A standalone argparse CLI, run from repo root, matching every existing skill convention:
reuses `dataforseo.py`'s `creds()`/`post()` (DRY), keys on `place_id` only, writes to
`leads.sqlite` via `leads_db.py`, and follows the `measure_shortlist.py`/`geo_grid.py` paid-pass
discipline verbatim: **always `--dry-run` first; live requires a hard `--budget` that stops
before the request that would cross it; resumable; records cost per record.**

### CLI

```bash
# Cost estimate only, $0, prints request count + dollar total
python tools/gbp_audit.py --relevance match --dry-run

# Live pass over confirmed-trade prospects; resumable; hard budget
python tools/gbp_audit.py --relevance match --budget 2.00

# One prospect
python tools/gbp_audit.py --place-id ChIJ... --budget 0.01

# Cheaper: skip the posts endpoint (no recency signal, ~60% cheaper)
python tools/gbp_audit.py --relevance match --no-updates --budget 1.00
```

Flags: `--relevance {match,maybe,all}` (which store rows to audit), `--place-id` (single),
`--no-updates` (info-only), `--skip-existing` (don't re-audit rows refreshed within N days),
`--priority {standard,priority}` (default standard), `--dry-run`, `--budget`.

### Input / identity

Reads `place_id`s from the `businesses` table in `leads.sqlite` (the canonical store).
**One implementation-time verification** (honest flag, same class as `geo_grid`'s "confirm
field names against first live response"): confirm which lookup key `my_business_info` accepts
(`place_id:` vs `cid:` vs name+location `keyword`) against the task_post params and the first
live response, and **validate the match-back by `place_id`** — never by name, per the skill rule.
A mismatched `place_id` in the response is dropped, not stored.

### Schema (added via `leads_db.py`, mirrors the `grid_scans` ToS-cache model)

```sql
-- One GBP audit snapshot per business. place_id is the only permanent identity;
-- every other column is a refreshable cache stamped with last_refreshed_ts.
CREATE TABLE gbp_audits (
    id                          INTEGER PRIMARY KEY,
    place_id                    TEXT NOT NULL,        -- FK -> businesses.place_id
    is_claimed                  INTEGER,              -- 1 / 0 / NULL(unknown)
    rating_value                REAL,
    rating_votes                INTEGER,
    category                    TEXT,
    additional_categories_count INTEGER,
    has_description             INTEGER,              -- description non-empty
    total_photos                INTEGER,
    has_hours                   INTEGER,              -- work_time present
    attr_available_count        INTEGER,
    attr_unavailable_count      INTEGER,
    last_post_ts                TEXT,                 -- NULL = no posts found
    post_count                  INTEGER,
    neglect_score               REAL,                 -- computed reducer, 0..100
    api_cost_usd                REAL,
    provider                    TEXT NOT NULL DEFAULT 'dataforseo',
    status                      TEXT NOT NULL DEFAULT 'complete', -- complete|partial|error
    audited_ts                  TEXT NOT NULL,
    last_refreshed_ts           TEXT NOT NULL
);
```

**ToS posture:** store counts, booleans, and the single `last_post_ts` timestamp only. Do NOT
persist review text, reviewer names, or photo URLs (PII / no need). Consistent with the store's
"`place_id` is the only permanently stored field; the rest is a refreshable cache" invariant.

### Neglect score (deterministic reducer, `score_report.py` style)

Pure function over the audited fields, unit-testable, no spend. **Proposed** starting weights
(tunable — higher score = more neglected = better pitch, capped at 100):

| Signal | Condition | Weight |
|---|---|---|
| Unclaimed profile | `is_claimed == false` | 0 (was 40) |
| No / stale posts | no post in 90 days (or ever) | 15 |
| Thin reviews | `rating_votes < 10` | 10 |
| Few photos | `total_photos < 10` | 10 |
| No secondary categories | `additional_categories_count == 0` | 8 |
| Hours not set | `has_hours == 0` | 7 |
| Sparse attributes | `attr_available_count < 3` | 5 |
| Empty description | `has_description == 0` | 5 |

**Updated 2026-07-08 (SEC-D fix, ARCHITECTURE.md section 9):** unclaimed was originally the
dominant signal (40 of 100), but DataForSEO's `is_claimed` proved an unreliable inference
(false-negatives on clearly-claimed businesses), so it corrupted the ranking and risked
asserting claim status as fact in a client report. Its weight is now **0**: `is_claimed` is
still collected as raw reference, but it scores nothing and never appears as a customer-facing
signal. The reachable maximum is now 60. The 1,687 existing snapshots were re-scored in place
(`rescore_gbp_audits.py`); a zero-weight signal never enters `signals_json`. Weights live in
one dict so tuning stays a one-line change.

### Reporting integration

- Extend `leads_report.py` with `--lens gbp`: rank prospects by `neglect_score` desc, join
  `gbp_audits`, so the workbook reads "worst GBP first = best pitch" (mirrors the existing
  `--lens budget`).
- One-line prospect summary for the sales artifact, e.g.
  *"Unclaimed profile, no posts in 2 years, 4 photos, no secondary categories."*
- Pairs with the existing geo-grid before-report: neglect signals (the cause) next to the
  Map-pack heatmap / SoLV (the effect).

### Pipeline placement

`gbp_audit.py` is the GBP-side analog of `enrich_sites.py` (which does the website side).
Prospecting SOP becomes:

```
prospect_sweep  ->  enrich_sites + gbp_audit  ->  scrape_leads  ->  leads_report --lens gbp
    (discovery)      (site + GBP neglect)          (contacts)         (ranked pitch list)
```

### Tests (`tests/test_gbp_audit.py`, no network, no spend)

Faked DataForSEO client returning canned `my_business_info` + `my_business_updates` payloads
(the `test_measure.py` / `test_geo_grid.py` precedent). Assert: field extraction, `neglect_score`
math at boundary values, budget-stop + resume, `place_id` match-back (wrong id dropped), and
`--no-updates` path. Runs under `pytest tests/`.

---

## Status — BUILT 2026-07-05 (verified, one live-confirm remaining)

Shipped as `tools/gbp_audit.py` (+ `tools/leads_db_gbp.py` schema, `tools/gbp_diff.py` before/after
reducer, `--lens gbp` in `leads_report.py`, `tests/test_gbp_audit.py`). Full skill test suite green.
Snapshots are append-only, so the baseline audit is the "before" and `gbp_diff.py` renders the
client "since signup" delta.

## Open items

1. ~~Lookup key for `my_business_info`~~ **RESOLVED:** `keyword` accepts `place_id:<ID>` directly
   (verified in the task_post params doc), so lookup is exact — no name matching. The tool passes
   `keyword="place_id:<id>"` + `location_coordinate` from the stored lat/lng.
2. **Live-confirm (the one real to-do):** the DataForSEO Business Data **task response contract**
   (task_post -> poll task_get; result path; the `cost` field) and the exact **field names**
   (`is_claimed`, `rating.votes_count`, `total_photos`, `work_time`, `attributes.*`, and the post
   `timestamp` in `my_business_updates`) are coded defensively but must be confirmed against the
   first live task before a bulk run (the `geo_grid.extract_rank` gotcha class). The deterministic
   logic is fully unit-tested; only the live wire format is unconfirmed.
3. **Neglect-score weights** — as approved; tune the one `NEGLECT_WEIGHTS` dict before scale.

## What this is NOT

Not the owner-side control layer (ADR-LP-001). No writes, no OAuth, no approval gate, no posting
or review replies. Read-only prospect intelligence. Once a business becomes a managed client,
grants owner access, and Google's Basic API Access application is approved, the owner-scoped
completeness picture comes from the official GBP APIs — this tool's job ends at the sale.
