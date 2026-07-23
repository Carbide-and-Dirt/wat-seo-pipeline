# Workflow: National Lead Discovery + Enrichment

## Objective
Build and enrich a national SQLite master store of excavating/site-work leads: every business of
a trade discovered across the US and Canada, deduped by `place_id`, scored by opportunity, and
optionally measured with paid signals. Output is a ranked Excel workbook ready for outreach
prioritization, plus a discover-JSON shim for the legacy local pipeline.

## Inputs
- **`targets/excavating-national.json`** — trade queries, budget caps, paid-step config.
  The sweep is vertical-agnostic; copy for a new trade.
- **`data/places_us_ca.csv`** — GeoNames seed built by `build_seed.py` (one-time; cached).
- **API keys in `.env`**: `GOOGLE_PLACES_API_KEY` (sweep), `DATAFORSEO_LOGIN`/`DATAFORSEO_PASSWORD`
  + `PERPLEXITY_API_KEY` (paid shortlist only).

## Outputs
- **`data/leads.sqlite`** — the master store (gitignored; rebuild via the sweep).
- **`output/excavating-prospects.xlsx`** + `.csv` — segmented prospect workbook, one tab per state.
- **`.tmp/discover/<slug>.json`** — discover-JSON shim for the legacy chain.
- **`.tmp/grid/` / `output/`** — intermediate JSON; regenerable any time.

## Tools (run in this order)

- `leads_db.py init` — create `data/leads.sqlite` and all tables (idempotent; run once per machine).
- `prospect_sweep.py` — density-ordered Places sweep into the master store; dedup by `place_id`.
- `enrich_sites.py` — free SEO/AEO-readiness + agency fingerprint for every lead with a website.
- `gbp_audit.py` — **PAID** (cheap) Google Business Profile neglect audit: `is_claimed`, rating,
  categories, photos, hours, description, attributes (my_business_info) + post recency
  (my_business_updates), rolled into a tunable neglect score. The GBP-side analog of `enrich_sites.py`,
  keyed on `place_id`. `--dry-run` first; live needs a hard `--budget`. ~$0.004/prospect (info+posts).
- `gbp_diff.py` — **free ($0)** before-vs-after reducer over two GBP audits (sale-time vs later),
  for the client "since signup" report. Positive numbers = improvement.
- `scrape_leads.py` — best-effort email + owner-name from each lead's public homepage.
- `leads_report.py` — segmented workbook. `--lens budget` = ability-to-pay ranking (after enrichment);
  `--lens gbp` = most-neglected-profile-first (after `gbp_audit.py`), the GBP-update pitch list.
- `measure_shortlist.py` — **PAID** shortlist pass: real SERP rank + domain authority + Perplexity citation.
- `export_leads.py` — export shim: writes master store back to discover-JSON for the legacy chain.

## Steps

1. **One-time machine setup:**
   ```bash
   python tools/leads_db.py init
   ```
   Idempotent — safe to re-run. Confirms table creation with row-count output.

2. **Dry-run (always first — $0):**
   ```bash
   python tools/prospect_sweep.py --region "TN KY" --dry-run
   ```
   Prints the request count and cost estimate without touching Google. Review before proceeding.

3. **Live sweep (requires a HARD `--budget`):**
   ```bash
   python tools/prospect_sweep.py --region "TN KY" --budget 20
   ```
   Density-ordered, resumable (re-run with the same region to pick up where it stopped).
   Adds only new `place_id` rows; existing rows are updated, never duplicated.

4. **Free enrichment pass:**
   ```bash
   python tools/enrich_sites.py [--region "TN KY"] [--limit 50]
   ```
   Skips already-enriched leads; use `--refresh` to re-enrich. Classifies each site
   DIY / self-managed / likely-agency-managed and records the evidence.

5. **GBP neglect audit (PAID, cheap — for the GBP-update pitch):**
   ```bash
   python tools/gbp_audit.py --relevance match --dry-run          # estimate first ($0)
   python tools/gbp_audit.py --relevance match --budget 2.00      # live, capped, resumable
   python tools/gbp_audit.py --relevance match --no-updates --budget 1.00  # cheaper, skip posts
   ```
   Reads each prospect's public profile via DataForSEO (by `place_id`, no owner access), writes an
   append-only snapshot to `gbp_audits`, and scores neglect (unclaimed, stale posts, few photos,
   thin reviews, missing categories/hours/attributes/description). `--skip-existing-days N` avoids
   re-spending on recently-audited leads. The sale-time snapshot is the "before" for `gbp_diff.py`.

6. **Contact scrape (free):**
   ```bash
   python tools/scrape_leads.py [--region "TN KY"] [--limit 200]
   ```
   Stores email + owner-name in `site_contacts`; flows into the report automatically.

7. **Opportunity report (free):**
   ```bash
   python tools/leads_report.py                               # opportunity lens (default)
   python tools/leads_report.py --lens budget --min-reviews 20  # ability-to-pay lens
   python tools/leads_report.py --lens gbp                    # most-neglected GBP first (needs step 5)
   ```
   Regenerate any time; it reads the current store state and writes nothing back to the DB.

8. **Paid shortlist (optional — confirm before running):**
   ```bash
   python tools/measure_shortlist.py --top 25 --dry-run        # estimate first
   python tools/measure_shortlist.py --top 25 --budget 5.00    # then live, capped
   ```
   Writes `site_rankings` (SERP rank, domain authority, Perplexity citation); picks up from
   where it stopped if interrupted.

9. **Legacy-chain export (optional):**
   ```bash
   python tools/export_leads.py [--region "TN KY"] [--out .tmp/discover/tn.json]
   ```
   Feeds `normalize_prospects → scrape_contacts → audit_site → hvac_report` unchanged.

## Expected outputs

| Step | Output |
|------|--------|
| sweep | rows in `businesses` + `swept_cells` |
| enrich | rows in `site_enrichment` |
| gbp audit | append-only snapshots in `gbp_audits` (neglect score + signals) |
| scrape | rows in `site_contacts` |
| report | `output/excavating-prospects.xlsx` + `.csv` (`--lens gbp` -> `output/leads-gbp-neglect.xlsx`) |
| measure | rows in `site_rankings` |
| export | `.tmp/discover/<slug>.json` |

## Gotchas

- **`data/leads.sqlite` is gitignored.** Never commit it. Downstream tooling may read it
  **read-only** at this fixed relative path. Do NOT move, rename, or delete the DB or this
  folder without updating those consumers too.

- **`place_id` is the only permanently stored field (Google ToS, SEC-4).** Every other Places
  field (`name`, `address`, `phone`, `website`, `rating`, `review_count`) is a refreshable cache
  with a `last_refreshed_ts` stamp, not a permanent record.

- **Always `--dry-run` first for every paid step.** `prospect_sweep`, `measure_shortlist`, and
  `geo_grid` all support `--dry-run` ($0) and require a HARD `--budget` for a live run. The budget
  is a hard stop — the tool halts before the request that would cross it.

- **Relevance tiers are `match` / `maybe`.** `match` = confirmed trade keyword hit. `maybe` = flagged
  for review. A bare Google `general_contractor` type is NOT a confirm — it is a catch-all category;
  those rows stay `maybe` until manually confirmed.

- **`--lens budget` requires enrichment first.** The paid signal columns (`has_google_ads`,
  `likely_agency_managed`, `has_crm_stack`) come from `enrich_sites.py`. An un-enriched lead can
  only reach Tier 2 (QUALIFIED DEMAND) via no-website + review count; run enrichment before the
  budget lens for full coverage.

- **Sweep is additive and resumable.** Re-running with the same region skips already-swept cells
  (`swept_cells` table). Use `--refresh` to force re-query of swept cells (rare; use when data is
  stale after several months).

- **`export_leads.py` is a legacy shim** (FR-11). The national pipeline writes directly to SQLite;
  export only when you need to hand national data to tools that consume the old discover-JSON format
  (`normalize_prospects`, `hvac_report`).

- **Tests (no network, no spend):** `test_prospect_sweep.py`, `test_enrich.py`, `test_export.py`,
  `test_report.py`, `test_measure.py`, `test_contacts.py`. Run `pytest tests/` or any file standalone.
