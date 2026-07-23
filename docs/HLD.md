# High-Level Design - National Contractor Lead Discovery (`prospect_sweep`)

> Status: Approved   ·   Last updated: 2026-06-18

## 1. Overview

A new lead-discovery tool for the WAT project that enumerates excavating
and adjacent site-work contractors across **all US states and Canada**, as prospects for
website + automation services.

The existing `places_discover.py` searches a hand-typed list of towns and is capped by
Google's hard limit of ~60 results per text query. That works for a ~15-town local sweep
(on the order of 100 businesses) but **cannot scale to a nation**: a multi-state run
against a handful of broad locations yields a structural artifact - each query truncated
at 60 and biased toward prominent firms - exactly the opposite of the small no-website
operators the pipeline targets.

`prospect_sweep` solves this by slicing the continent into thousands of small,
population-ordered cells, querying each one under the 60-cap, deduping into a durable
master store, and stopping at a user-set budget. **Success** = a growing, de-duplicated,
state-segmented list of pitchable contractors - no-website and weak-website operators
ranked first - produced for a predictable, known-in-advance dollar cost, resumable across
many runs.

## 2. Goals & Non-Goals

**Goals:**
- Discover excavating + septic/underground + demolition/hauling contractors across all 50
  US states and all Canadian provinces/territories.
- Never exceed a per-run budget the user sets; project the cost *before* spending (dry run).
- Prioritize the densest, highest-opportunity markets first (cost-control-first coverage).
- Persist one growing, de-duplicated master list across many runs; resume without re-spending.
- Feed the existing `normalize -> scrape_contacts -> audit_site -> report` pipeline unchanged
  (by exporting the established discover-JSON schema).
- Deliver a state-segmented prospect workbook (per-state tabs) + master CSV.

**Non-Goals:**
- Exhaustive rural saturation in v1. Sparse areas between seed towns are deferred to a later
  grid-fill pass ("fill gaps later" was the chosen trade-off).
- Foundation/concrete-only contractors (explicitly out of the target trade set).
- A second data source (OpenStreetMap, DataForSEO) for discovery in v1 - Google Places is the
  spine because it is the only source with a reliable "no website" signal. Enrichment is later.
- Doing the outreach. This tool builds the list; sending/calling is downstream and has its own
  compliance constraints (see Section 10 / 11).
- A GUI. Command-line tools consistent with the rest of the WAT project.

## 3. Users & Use Cases

**Primary user:** the operator, running the WAT pipeline from a shell.

**Key use cases:**
- "Estimate what a full US + Canada sweep would cost before I spend anything."
- "Sweep the top markets nationally up to a $X budget, densest first."
- "Sweep just one state and its bordering states this week; do more next week."
- "Re-run to add newly-reachable markets without re-paying for ones already swept."
- "Refresh ratings/website status on leads I pulled a month ago."
- "Hand me a workbook with a tab per state so I can work the no-website leads first."

## 4. Functional Requirements

- **FR-1:** The tool accepts a **region spec** scoping a run: `all` (US + Canada), one or more
  US states / Canadian provinces, or an explicit bounding box. Default ordering within any
  region is by descending market density (population).
- **FR-2:** The tool carries a bundled **seed dataset** of US + Canadian populated places
  (name, state/province, lat/lng, population) used to generate query cells. No live call is
  needed to know where to look.
- **FR-3:** For each seed cell, the tool queries Google Places (New) for each configured
  **trade query set** (excavation; septic/underground; demolition/hauling) using a location
  bias, following pagination up to the 60-result cap.
- **FR-4:** When a cell returns a saturated result set (hits the 60 cap), the tool
  **subdivides** that cell into smaller child cells and re-queries, so dense metros are not
  silently truncated.
- **FR-5:** A **`--dry-run`** mode projects, without spending: number of seed cells in the
  region, number of trade queries, projected request count (low / expected / high), and
  projected dollar cost at a configurable per-1000-request rate. Live runs print a running
  spend tally.
- **FR-6:** A **`--budget`** cap (in dollars or request count) is enforced as a hard ceiling.
  The run stops cleanly before exceeding it and checkpoints progress.
- **FR-7:** Discovered businesses are written to a **durable SQLite master store**, keyed by
  Google `place_id`, recording name, address, state/province, phone, website, no_website flag,
  rating, review count, business status, types, primary type, trade bucket, lat/lng, maps URL,
  the cells it was found via, and first-seen / last-refreshed timestamps.
- **FR-8:** The master store is **de-duplicated** by `place_id`; a business found in multiple
  overlapping cells produces exactly one row, accumulating its found-via cells.
- **FR-9:** Runs are **resumable**: the store tracks which cells have been swept, so a killed or
  budget-stopped run continues from where it left off and an additive re-run skips swept cells.
- **FR-10:** Re-runs support **two modes**: *additive* (default - only spend on not-yet-swept
  cells, leave known records untouched) and **`--refresh`** (also re-pull rating / website /
  status for existing records within the region).
- **FR-11:** The tool **exports** the master store (filtered to a region if asked) to the
  existing discover-JSON schema consumed by `normalize_prospects.py` and `hvac_report.py`,
  so the rest of the pipeline runs unchanged.
- **FR-12:** A **relevance filter** keeps clear-trade and adjacent matches and drops unrelated
  results, driven entirely by the trade config's keywords / Google types (vertical-agnostic,
  as today). Trade buckets covered: excavation (excavating, grading, site work, dozer/dirt
  work, land clearing), septic/underground (septic, sewer/water line, utility, boring),
  demolition/hauling (demolition, debris removal, dump truck, dumpster).
  Tiering: a trade-keyword hit = **`match`** (confirmed prospect); an adjacent-only hit =
  **`maybe`** (kept, flagged for review); neither = **`other`** (dropped). Google's
  `general_contractor` is intentionally **not** a `primary_types` confirm — it is a catch-all
  that plumbers, HVAC, concrete, foundation, pool-removal, tree and landscaping firms all
  carry, so it lands a lead in the `maybe` review tier (via the `contractor` adjacent keyword),
  not `match`. (Config fix 2026-06-18: `general_contractor` in `primary_types` had confirmed
  ~51% of leads on zero trade signal; guarded by a regression test.)
- **FR-13:** `normalize_prospects.py` is generalized to parse **state/province from any US or
  Canadian address** (not a single hard-coded state), and the local-only `in_footprint` concept is replaced
  by a `state` / `region` grouping field.
- **FR-14:** The report layer delivers **one workbook with a tab per state/province** plus a
  master CSV, preserving the current ranking: no-website/broken leads first, then live sites
  ranked by website-weakness opportunity score. Each row is labelled by relevance tier
  (`match` = confirmed, `maybe` = review) so confirmed prospects lead and the review tier is
  visibly separated rather than silently mixed in (FR-12). When site-enrichment data (FR-15-17)
  is present, the report adds SEO/AEO-readiness, ranking, and agency-status columns and folds
  the readiness score into the opportunity ranking.

### Lead enrichment (for leads that have a website)

- **FR-15 (free readiness pass):** For every lead with a website, fetch and audit the homepage
  (reuse `audit_site.py`) to capture **SEO health** (HTTPS, mobile viewport, title/meta lengths,
  H1s, canonical, homepage content depth) and **AEO/GEO readiness** (schema.org JSON-LD presence
  + LocalBusiness/FAQ flags, `llms.txt`, and whether the site blocks AI crawlers like GPTBot /
  PerplexityBot / ClaudeBot / Google-Extended). Derive a **readiness score + gap list** (reuse
  `hvac_report.opportunity()`). No paid API calls; runs on all leads; resumable (skip already-
  enriched). This is the "how optimized is the site" proxy and the primary sales signal.
- **FR-16 (agency / tech fingerprint):** From the same fetched HTML, detect the **site builder /
  CMS** (Wix, Squarespace, GoDaddy, Duda, WordPress, custom), **marketing & call-tracking tags**
  (Google Tag Manager / GA4, Meta Pixel, HubSpot, CallRail and similar), any **agency footer
  credit** ("site by / designed by / powered by [link]"), and (best-effort) **Google Ads**
  presence. Classify each site's management as **DIY / likely self-managed / likely
  agency-managed** with a **confidence level and the supporting evidence list** (never a bare
  yes/no - it is an inference from public footprints, not a confirmed fact).
- **FR-17 (paid shortlist measurement):** For a **shortlist** the user selects (top-N by
  opportunity within a region, a named region, or an explicit list), measure **actual Google
  organic rank** (`dataforseo serp`), **backlinks / domain authority** (`dataforseo backlinks`),
  and **live AI-engine citation/visibility** (`check_ai_visibility.py`, Perplexity). Gated by a
  **hard dollar cap and a dry-run estimate shown before any spend** (mirrors FR-5/NFR-2);
  resumable; results stored with provenance and a fetched-at timestamp.

## 5. Non-Functional Requirements

- **NFR-1 (cost predictability):** The `--dry-run` projected request count is within +/-20% of
  the actual count for the same region/config (validated on at least one state).
- **NFR-2 (hard cap):** A live run never exceeds its `--budget`; it stops before the request
  that would cross the ceiling.
- **NFR-3 (resumability):** Killing the process mid-run loses at most the in-flight cell; on
  restart no already-swept cell is re-queried (verified via the swept-cells ledger).
- **NFR-4 (dedup integrity):** The master store contains zero duplicate `place_id` rows; export
  + re-import is idempotent.
- **NFR-5 (scale):** The store and report tolerate 50,000+ businesses and still build the
  workbook without exhausting memory (stream rows; per-state sheets).
- **NFR-6 (rate limits):** Respect Places API QPS; exponential backoff on HTTP 429/5xx; a single
  cell failure is logged and skipped without aborting the run (as `places_discover` does today).
- **NFR-7 (platform):** all generated console/file output is
  ASCII-safe; files written with explicit `encoding='utf-8'`.
- **NFR-8 (no new heavy deps):** Reuse the current stack - `requests`, stdlib `sqlite3`,
  `openpyxl` (already used). No database server, no framework.
- **NFR-9 (free enrichment is $0):** The readiness + agency pass (FR-15/16) makes **no paid API
  calls** - cost is HTTP/time only, bounded by a concurrency limit, and resumable (an
  already-enriched lead is skipped). JS-rendered sites that need a paid render (Firecrawl) are
  flagged, not silently charged.
- **NFR-10 (paid enrichment cap):** The paid shortlist pass (FR-17) **prints a dry-run cost
  estimate before spending** and **never exceeds its dollar cap**, stopping before the lookup
  that would cross the ceiling (same discipline as the sweep's FR-6/NFR-2).

## 6. Tech Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Language | Python 3 | Matches the entire WAT project; reuse existing tools verbatim. |
| Discovery API | Google Places API (New) `searchText` | Only source with a reliable per-business **no-website** signal; already integrated and field-masked in `places_discover.py`. |
| Geo seeding | Bundled CSV of US + CA populated places w/ population + lat/lng | Enables density-first ordering offline; no live geocoding cost. Source TBD (open question) - GeoNames `cities500`/`cities1000` (free, CC-BY) is the leading candidate. |
| Master store | SQLite (stdlib `sqlite3`) | Durable, de-duplicating, resumable, handles 50k+ rows, zero new dependency. |
| Pipeline interchange | Existing discover-JSON schema (export shim) | Keeps `normalize -> scrape -> audit -> report` working unchanged. |
| Reporting | `openpyxl` (already a dep) | Per-state tabs + filters; the report layer already writes multi-sheet xlsx. |

*Considered and rejected:* (a) **Per-run JSON files** like today - rejected because a national
list needs cross-run dedup, swept-cell tracking, and resumability that JSON blobs handle poorly.
(b) **Pure uniform lat/lng grid** - rejected for v1 because it can't prioritize "densest markets
first"; retained as a later *completeness* mode for rural fill. (c) **nearbySearch by Google
type** - rejected as the primary path because Google's New-API type list has no
excavating/site-work type (closest is `general_contractor`); text queries capture the trade's
messy naming far better. (d) **OpenStreetMap / data brokers** - rejected for v1: no reliable
no-website signal.

## 7. Architecture

Components and single responsibilities:

```
                         targets/excavating-national.json
                         (trade query sets, keywords, types,
                          chains list, microsite adjectives)
                                      |
   data/places_us_ca.csv             v
   (seed places: name, ST,    +--------------------+
    lat/lng, population) ----> |  cell planner      |  FR-1,2,4
                               |  region -> ordered  |
                               |  cells (density)   |
                               +---------+----------+
                                         | ordered cells
                                         v
        --dry-run  ------------> +--------------------+      data/leads.sqlite
        (project cost, FR-5) <-- |  sweep engine      | <--> (master store: FR-7,8,9)
        --budget   ------------> |  per cell x trade: |      place_id PK, swept-cells
        (hard cap, FR-6) ------> |  Places searchText |      ledger, timestamps
                                 |  paginate, filter, |
                                 |  subdivide if 60,  |
                                 |  upsert, checkpoint|
                                 +---------+----------+
                                           | export (FR-11)
                                           v
                              .tmp/discover/<slug>.json  (existing schema)
                                           |
        normalize_prospects.py (FR-13, any-state) -> scrape_contacts.py
                                           |                 -> audit_site.py
                                           v
                       report layer (FR-14): per-state tabs workbook + CSV
                              output/<slug>-prospects.{md,csv,xlsx}
```

**Data flow:** region spec + seed CSV -> cell planner emits density-ordered cells -> sweep
engine queries Places per cell per trade, filters for relevance, subdivides saturated cells,
upserts into SQLite under a budget cap with checkpointing -> export shim writes the existing
discover-JSON -> the unchanged normalize/scrape/audit chain runs -> the report layer segments
by state into a tabbed workbook.

**Boundaries:** The sweep engine and master store are the new modules. The export shim is the
seam that keeps everything downstream untouched except the two locality-specific fixes (normalize
state parsing, report segmentation). Google Places is the only external service in the
discovery path.

## 8. Data Model

**SQLite master store (`data/leads.sqlite`):**

- `businesses` - one row per `place_id` (PK):
  `place_id, name, address, state_province, country, lat, lng, phone, website, no_website,
  rating, review_count, business_status, primary_type, types_json, trade_bucket, maps_url,
  found_via_json, first_seen_ts, last_refreshed_ts`.
- `swept_cells` - coverage ledger: `cell_id (PK), center_lat, center_lng, radius_m,
  trade_bucket, region, result_count, saturated_bool, swept_ts`. Drives resumability (FR-9)
  and additive re-runs (FR-10).
- `runs` - audit trail: `run_id, region, budget, mode, started_ts, ended_ts,
  requests_spent, est_cost`.
- `site_enrichment` - one row per enriched lead (`place_id` PK/FK -> businesses), free pass
  (FR-15/16): `place_id, fetched_url, http_status, reachable, https, mobile_viewport,
  title_len, meta_desc_len, word_count, jsonld_present, schema_localbusiness, schema_faq,
  llms_txt, ai_bots_blocked, readiness_score, seo_gaps_json, builder, marketing_tags_json,
  agency_credit, google_ads, mgmt_status, mgmt_confidence, mgmt_evidence_json, enriched_ts`.
  Separate table (not extra `businesses` columns) so the enrichment is a self-contained,
  refreshable cache (SEC-4) with its own timestamp, and a re-enrich is a single-row replace.
- `site_rankings` - one row per lead measured in the paid pass (FR-17):
  `place_id, serp_rank, serp_keyword, domain_authority, backlinks, ai_mentioned, ai_cited,
  ai_engine, est_cost, measured_ts`. Kept apart from the free pass because it costs money and
  refreshes on a different cadence.

**Seed dataset (`data/places_us_ca.csv`):** `name, state_province, country, lat, lng,
population`. Read-only; bundled.

**Export JSON (unchanged contract):** `{area, slug, vertical, industry_schema, source, query,
towns, count, companies:[{place_id, name, address, phone, website, no_website, rating,
review_count, business_status, primary_type, types, relevance, maps_url, location,
found_via, ...}]}` - plus a new `state` field per company for segmentation (additive, backward
compatible).

## 9. External Interfaces & Integrations

- **Google Places API (New) - `places:searchText`.** Auth: `GOOGLE_PLACES_API_KEY` in `.env`
  (never in code or the HLD). Same field mask as `places_discover.py` plus `location`. Each
  request biased to a cell. Failure behavior: HTTP 429/5xx -> exponential backoff + retry;
  persistent cell failure -> log to stderr, mark cell failed, continue (NFR-6). The budget cap
  is checked before each request (FR-6).
- **Pricing input:** a configurable per-1000-request rate (default approximating the SKU the
  field mask lands in, ~$32-40/1000) drives the dry-run projection. **Ratings are kept** -
  `websiteUri` and the phone fields (both required) already place the request in the higher
  ("Enterprise") tier, so adding `rating` / `userRatingCount` costs nothing extra and gives a
  review-count signal for lead prioritization. The rate is a config value, not hard-coded.
- **Downstream tools (internal):** export JSON consumed by `normalize_prospects.py`,
  `scrape_contacts.py`, `audit_site.py`, and the report layer - file-based handoff via
  `.tmp/` and `output/`, identical to today.
- **Enrichment integrations (existing project tools, reused):**
  - **Free pass (FR-15/16):** `audit_site.py` over plain HTTP (`requests`); optional
    `FIRECRAWL_API_KEY` only for JS-rendered sites (off by default - flagged, not auto-charged).
    Failure behavior: an unreachable/blocked site is recorded as `reachable=0` with `http_status`
    and skipped, never aborting the pass (as the audit tool already degrades today).
  - **Paid pass (FR-17):** `dataforseo.py` (auth: `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD`)
    for SERP rank + backlinks; `check_ai_visibility.py` (auth: `PERPLEXITY_API_KEY`) for AI
    citations. All keys live only in `.env`. Each call is charged against the FR-17 dollar cap;
    a missing key skips that signal and labels it "not measured" (never fabricated).

## 10. Security

- **SEC-1:** `GOOGLE_PLACES_API_KEY` lives only in `.env` (gitignored); it is never logged,
  printed, committed, or written into the master store or exports.
- **SEC-2:** The master store and exports contain only business-listing data already public on
  Google Maps; no consumer PII. Scraped owner emails (from `scrape_contacts.py`) are kept only
  where publicly posted and never fabricated (existing rule preserved).
- **SEC-3:** Trade config and region specs are trusted local files, but all values
  interpolated into Places query strings are treated as data (parameterized JSON body, no shell
  interpolation) to avoid query-injection / command-injection surfaces.
- **SEC-4:** Google Places API Terms restrict long-term **caching/storage** of most Places
  content (`place_id` may be stored indefinitely; other fields are cache-limited). The store
  records a `last_refreshed_ts` per row and treats non-`place_id` fields as a refreshable cache
  so the design can honor a retention/refresh policy rather than holding stale Places data
  indefinitely. (Posture to confirm - see Open Questions.)
- **SEC-6:** Enrichment reads only **publicly served pages** of a lead's own website and derives
  signals from them; it stores derived facts (builder, tags, readiness score, evidence), not
  page copy. The agency classification is labelled an **inference** (status + confidence +
  evidence), never asserted as fact. Enrichment rows carry `enriched_ts` / `measured_ts` and are
  a refreshable cache (consistent with SEC-4). Paid-API credentials follow SEC-1 (`.env` only).
- **SEC-5:** Outreach that consumes these leads is subject to **CAN-SPAM (US)**, **CASL
  (Canada - strict opt-in for commercial email)**, and **Do-Not-Call** rules. Out of scope for
  this tool, but flagged so the list is used compliantly.

## 11. Risks, Trade-offs & Open Questions

**Trade-offs:**
- Cost-control-first => place-seeded, density-ordered discovery instead of an exhaustive grid.
  Buys predictable spend and fast time-to-first-leads; costs some rural completeness (deferred
  to a later grid-fill mode).
- SQLite master store instead of per-run JSON: gains dedup/resumability/coverage tracking;
  costs a one-time export shim to stay compatible with the existing JSON pipeline.
- Text-query discovery over type-based: captures the trade's messy naming; costs some noise,
  handled by the relevance filter.

**Risks:**
- **Cost overrun in dense metros** if many cells saturate and subdivide -> mitigated by the hard
  budget cap (NFR-2) and the dry-run projector (FR-5).
- **Per-cell query count is the dominant cost driver.** At the locked **Balanced** default
  (5 query phrases, full rural seed at min-pop 1000), the Phase-1 estimator (built) projects a
  full US+CA sweep at ~$5k expected, a mid-size state + its 8 neighbors at ~$711, and one
  mid-size state alone at ~$75. The estimate
  is deliberately conservative (rounds up; no overlap pruning or town-size query scaling yet).
  Levers to cut spend: fewer query phrases (config), higher `--min-pop` (fewer towns), Phase-2
  overlap pruning + query-depth-by-town-size, and the budget cap (densest markets first).
- **Trade synonyms over/under-match** (e.g., "site work" pulling unrelated firms; rural
  operators filed under `general_contractor` missed) -> tune keyword/type lists; relevance
  filter + manual spot-check.
- **Google ToS storage limits** could constrain how long non-`place_id` fields may be retained
  (SEC-4) -> refresh cadence + retention policy.
- **Seed-dataset gaps** (tiny towns absent from the seed) leave coverage holes -> later
  grid-fill mode; choose a comprehensive seed source.

**Resolved (2026-06-18):**
1. **Seed dataset** - **GeoNames** (`cities500`/`cities1000`, free, CC-BY, US + Canada, has
   population). Record the CC-BY attribution. Population threshold for inclusion is tunable to
   trade rural coverage against query count/cost.
2. **Ratings** - **kept** in the field mask (free given website + phone already set the tier;
   see Section 9). Per-1000 rate stays a config value.
3. **Google ToS retention posture** (SEC-4) - **pragmatic**: store `place_id` durably; store
   other fields with a `last_refreshed_ts` and treat them as a refreshable cache.

**Open Questions:**
4. **Default budget + first region** - what dollar cap and which region for the first real
   (non-dry) run, once the estimator's numbers are in front of you.
5. **AEO/GEO engine coverage (FR-17)** - the live AI-visibility tool currently measures
   **Perplexity** only. ChatGPT, Google AI Overviews / AI Mode, and Bing Copilot would each be
   additional (paid, separately built) measurements. Start with Perplexity as the AEO/GEO proxy;
   decide later whether the pitch needs more engines.
6. **Paid-pass shortlist default + SERP keyword [RESOLVED 2026-06-18]** - implemented as:
   default shortlist = **top-25 by opportunity** (readiness DESC among live-enriched leads, then
   reviews), overridable by `--top` / `--region` / explicit `--place-ids`; SERP keyword template
   **`"{trade} {city}"`** and AI query **`"Who are the best {trade} companies in {city}, {state}?"`**,
   both in the config `paid` block with per-call cost overrides. (Revisit the keyword wording
   after eyeballing the first live results.)

## 12. Build Sequence

- **Phase 1 - Seed + estimator + store (no spend) [BUILT 2026-06-18]:** bundled
  `data/places_us_ca.csv` (GeoNames, 19,701 US+CA places); SQLite schema (`businesses`,
  `swept_cells`, `runs`); cell planner + **`--dry-run` cost projector**. Satisfies FR-1, FR-2,
  FR-5, FR-7 (schema). *Shipped:* you can price any region before spending a cent.
- **Phase 2 - Sweep engine [BUILT 2026-06-18]:** place-seeded, density-ordered Places querying
  with pagination, relevance filter, saturated-cell subdivision, hard budget cap, per-cell
  checkpointing, upsert + dedup, resumability, additive + `--refresh`. Satisfies FR-3, FR-4,
  FR-6, FR-8, FR-9, FR-10, FR-12. Includes two cost optimizations the estimator doesn't credit:
  **overlap pruning** (skip a small town whose center already falls inside a larger nearby
  cell's swept radius) and **query-depth by town size** (all phrases in metros, core excavation
  only in hamlets). Tested with an injected fake Places client (no network/spend). Live-run
  tuning (set after a single-state pilot showed metros over-subdividing): default subdivision depth 1
  (`--max-subdiv` to override), 0.6s page pause. *Shipped:* real, de-duplicated leads in the
  master store for a capped region.
- **Phase 3 - Export + downstream compatibility [DONE 2026-06-18]:** `export_leads.py` writes
  the store (region-filterable) back to the discover-JSON schema (FR-11); `normalize_prospects.py`
  generalized to parse state/province from any US/CA address (last-valid `<City>, <ST>` before
  the postal code, validated against the 50 states + DC + 13 provinces) and adds a universal
  `state` field, replacing `in_footprint` - which is now computed only when a config still lists
  `out_of_area_towns`, so the legacy single-market HVAC/pest pipelines keep working (FR-13).
  Verified end-to-end on the 376-lead store (376 -> 347 after multi-location dedup) and guarded
  by `tests/test_export.py` (10 tests). *Shipped:* the full existing pipeline runs on national data.
- **Phase 4 - Segmented reporting [DONE 2026-06-18]:** `leads_report.py` writes one workbook
  with a Summary tab + one ranked tab per state/province, plus a flat master CSV (State, Rank,
  lead columns, place_id), preserving the no-website-first / opportunity-ranked ordering within
  each state and folding the enrichment columns (management verdict, builder, tags, agency
  credit, readiness, top gap) into every row (FR-14). Verified on the 376-lead store (Summary +
  one state tab + CSV); guarded by `tests/test_report.py`. *Shipped:* a usable national prospect
  workbook. **Extension (2026-06-18): a `--lens budget` ranking targets ability-to-pay over raw
  need - Tier 1 PROVEN BUDGET (running Google Ads / agency-managed / CallRail-HubSpot-Marketo,
  weakest-site-first for displacement), Tier 2 QUALIFIED DEMAND (>= --min-reviews + a web gap,
  busiest first), Tier 3 long tail; `--qualified-only` trims to tiers 1-2. Paid signals come
  from enrichment (FR-15/16).**
- **Phase 5 - Free site enrichment (SEO/AEO readiness + agency fingerprint) [no spend]:** a
  resumable pass over leads that have a website, reusing `audit_site.py` for SEO + AEO/GEO
  readiness signals and adding a marketing-agency / tech fingerprint; writes `site_enrichment`
  and a readiness score + gaps. Satisfies FR-15, FR-16, NFR-9. *Ship:* every lead with a site
  carries a readiness score, gap list, and DIY/self-managed/agency verdict - at $0. (Buildable
  now against the 376-lead pilot, independent of Phases 3-4.)
- **Phase 6 - Paid shortlist measurement [BUILT 2026-06-18, no spend run yet]:**
  `measure_shortlist.py` - shortlist selector (top-N by opportunity / region / explicit
  place-ids, via `leads_db.shortlist_candidates`), an always-on `--dry-run` cost estimate, and a
  HARD `--budget` cap that stops *before* the lead that would cross it. Reuses `dataforseo.py`
  (SERP + backlinks) and `check_ai_visibility.py` (Perplexity) primitives behind an injected
  `clients` object, recording actual rank / domain authority / backlinks / AI citation into
  `site_rankings` with per-lead `est_cost` + timestamp (provenance); resumable. Per-call costs +
  keyword/AI templates live in the config `paid` block. Satisfies FR-17, NFR-10. Guarded by
  `tests/test_measure.py` (10 tests, fake client - zero network/spend); dry-run verified on the
  store ($0.70 for top-25). *Built but not yet run live* - a live run needs DataForSEO + Perplexity
  keys in `.env`, an explicit `--budget`, and the user's go-ahead. *Ship (when run):* true
  SEO/AEO/GEO rankings for your best leads, spend-capped.
- **Phase 7 (later / non-goal for v1) - Completeness & refresh:** uniform grid-fill for rural
  saturation; optional OSM/second-source enrichment; refresh-policy automation for SEC-4.
