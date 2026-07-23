# System Design-of-Record - wat-seo-pipeline engine

> Status: Approved   ·   Last updated: 2026-07-08
>
> **Altitude:** system-level. This documents the whole skill so any operator could
> run it from the documentation alone. It sits *above* the
> subsystem specs and points down to them:
> - `docs/HLD.md` - `prospect_sweep` national discovery + enrichment + paid measurement (Approved).
> - `docs/DESIGN-gbp-prospect-audit.md` - GBP neglect audit (`NEGLECT_WEIGHTS`, signals).
> - `docs/DESIGN-geo-grid.md` - geo-grid rank tracker + client reporting (SoLV, the longitudinal proof store).
> - `workflows/*.md` - the run-books (SOPs) for each motion.
>
> The FR/NFR/SEC IDs in `docs/HLD.md` remain the source of truth for `prospect_sweep`;
> this doc does not renumber them. Requirements here are stated at system altitude.

## 1. Overview

`wat-seo-pipeline` is a competitive SEO audit + local-lead-discovery engine. It turns a
target market into an evidence-backed prospect list and a longitudinal proof record, in
four moves:

1. **Discover + qualify** local trade contractors (e.g. excavation / septic-underground /
   demolition-hauling) in a market.
2. **Find the leak** - the specific, evidenced, fixable local-presence weakness
   (weak/absent site, neglected Google Business Profile, follow-up complaints,
   poor Maps rank).
3. **Emit the ranked list** - workbooks and pitch lists; any outreach/CRM layer that
   consumes them is external to the engine.
4. **Prove the outcome** - baseline a business's local presence, re-measure monthly,
   and hand the before/after to a downstream report assembler.

It is a command-line toolchain (the "WAT" pattern: Workflows / Agents / Tools), Python 3,
~30 standalone tools over one SQLite master store. There is no GUI and no server.

**Success** = two outputs, produced cheaply and within API terms of service:
- a ranked prospect list where every row carries a real, named leak, and
- credible longitudinal outcome proof (Share of Local Voice and GBP deltas) per tracked
  business.

The three durable data assets accrete as a byproduct of running the engine (see §4).

## 2. Goals & Non-Goals

**Goals**
- **G1** - Turn a named market into a ranked list of qualified prospects, each carrying a
  specific, evidenced, fixable local-presence leak.
- **G2** - Produce credible before/after outcome proof (geo-grid SoLV, GBP deltas) on a
  repeatable cadence, feeding client reporting.
- **G3** - Do both cheaply (dry-run-then-hard-budget discipline) and ToS-safely, so the
  data supply survives at scale.
- **G4** - Accumulate the three durable data assets as a byproduct of running: the refreshed
  prospect dataset, the longitudinal proof store, the codified SOPs.
- **G5** - Be operable by any operator from the documentation alone.

**Non-Goals**
- **N1** - Not a structural rewrite of the ~30-tool layout. Surgical changes only. *(Held
  on evidence, not preference: the one structural prize - a single vendor adapter - already
  exists; there are no dead tools to delete; physical foldering would break flat bare-module
  imports across the tools, ~12 test files, and the SOPs for zero functional gain. See §9.)*
- **N2** - Not building owned scrapers to replace rented commodity APIs by default. Rent and
  keep thin, unless a specific field we must have has no other reliable source.
- **N3** - Not the client-delivery report shell. That belongs to a downstream report
  assembler (a separate repo); this engine ends at the section-file handoff (§7).
- **N4** - Not the outreach / CRM / conversation layer. The engine produces the
  list; a CRM runs the motion.
- **N5** - Not a customer-facing SaaS with its own auth/UI. It is an internal operator
  toolchain.
- **N6** - Not a GBP *management* layer; the GBP sub-pipeline is read-only
  review/neglect intelligence.

## 3. Users & Use Cases

**Primary user:** an operator running the WAT pipeline from a shell. This doc is
deliberately written so a new operator could run the machine from it alone.

**Key use cases (across the motion):**
- "Price a national or regional prospect sweep before spending a cent."
- "Sweep a region under a hard budget, densest markets first; resume next week."
- "Give me a ranked pitch list where every lead has a named leak (weak site, neglected GBP,
  follow-up complaints)."
- "Benchmark a set of competitors for an RFP / positioning deck."
- "Baseline this new client's Maps presence at sale, then re-measure monthly."
- "Hand client reporting a before/after section proving we moved the needle."

## 4. Durable assets vs commodity plumbing (organizing principle)

Every tool here either feeds a **durable data asset** (compounds with every run and cannot
be reconstructed with a cheap API call after the fact) or is **commodity plumbing**
(rentable by anyone; keep it thin and swappable).

**The three durable assets:**
1. **The prospect dataset** - `data/leads.sqlite` plus the sweep/normalize/enrich engine
   that keeps it fresh: deduped, trade-segmented, enriched contractor records that grow
   and get more complete every run. Not an API call.
2. **The codified motion + SOPs** - the town -> prospect -> audit -> monthly-report
   system captured in `workflows/`, so the machine is a transferable process, not operator
   knowledge.
3. **The longitudinal outcome-proof store** - the append-only `gbp_audits` and
   `grid_scans`/`grid_points` snapshots: a before/after record (baseline vs monthly SoLV)
   per tracked business that cannot be re-measured retroactively.

**Commodity plumbing (keep lean):** the external data sources (Google Places, DataForSEO,
PageSpeed, Perplexity) and their thin adapters. These are rented. The design rule is that no
durable asset depends on a *specific* vendor: they sit behind the boundary in §7 so a vendor
can be swapped without touching the data.

The vendor boundary is already a single module, and there are no redundant tools to prune
(§9). So the highest-value maintenance work is keeping commodity flakiness from
contaminating a durable asset (the `is_claimed` fix, §9) and keeping this document current.

## 5. Architecture - six tool groups over one store

```
                          EXTERNAL DATA (rented, ToS-bound)
        Google Places (New)   DataForSEO   PageSpeed   Perplexity   GeoNames(seed)
              |                   |            |            |            |
              |            +------+------+     |            |            |
              |            | dataforseo.py|    |            |            |   <- single
              |            | (adapter)    |    |            |            |      vendor
              v            +------+------+     v            v            v      boundary
   +----------------------------------------------------------------------------------+
   | GROUP 1  COMPETITIVE AUDIT (RFP / benchmarking)                                   |
   |   run.py -> audit_site, pagespeed, check_ai_visibility, places_reviews,           |
   |             dataforseo(serp+backlinks) -> score_report                            |
   +----------------------------------------------------------------------------------+
   | GROUP 2  LOCAL DISCOVERY (legacy single-market)                                   |
   |   places_discover -> normalize_prospects -> scrape_contacts -> hvac_report        |
   +----------------------------------------------------------------------------------+
   | GROUP 3  NATIONAL SWEEP + ENRICH  (durable #1: the dataset)                       |
   |   build_seed/cell_planner -> prospect_sweep -> [leads.sqlite]                     |
   |     -> enrich_sites (+site_fingerprint) -> scrape_leads -> measure_shortlist      |
   |     -> export_leads (legacy bridge) / leads_report (workbook)                     |
   +----------------------------------------------------------------------------------+
   | GROUP 4  GBP NEGLECT + REVIEW INTEL (prospect intel)                              |
   |   gbp_audit -> gbp_diff ; gbp_reviews -> gbp_review_batches -> gbp_classify       |
   |     -> gbp_pitch_list ; gbp_trades (map) ; gbp_verify_google (cross-check)        |
   +----------------------------------------------------------------------------------+
   | GROUP 5  GEO-GRID PROOF  (durable #3: outcome proof)                              |
   |   geo_grid -> grid_diff -> grid_heatmap -> grid_report_section --file--> report assembler
   +----------------------------------------------------------------------------------+
   | GROUP 6  SHARED INFRA:  leads_db (+ _gbp, _grid submodules) owns all schema;      |
   |          lib/common (load_env, utf8_stdout, slug); dataforseo.py adapter          |
   +----------------------------------------------------------------------------------+
              |                                   |
              v                                   v
        output/*.{md,csv,xlsx}          outreach / CRM  (external, N4)
```

**Group responsibilities and classification:**

| Group | Responsibility | Feeds | Class |
|-------|----------------|-------|-------|
| 1 Competitive audit | Score a named set of competitors across on-page, performance, authority, AI-visibility for an RFP/positioning deck | `output/` report | Keep-lean (all rented signals) |
| 2 Local discovery | Legacy single-market discover -> report path; still live for small local sweeps | discover-JSON -> report | Keep-lean |
| 3 National sweep + enrich | Build and refresh the master prospect dataset; qualify, enrich (free), contact-scrape, and rank | `leads.sqlite`, workbooks | **Durable #1** (dataset) |
| 4 GBP neglect + review intel | Detect a prospect's neglected Google Business Profile and mine follow-up complaints from its worst reviews | `gbp_audits`, pitch-list XLSX | Mixed: intel = durable-adjacent; claim-scoring = broken commodity (§9) |
| 5 Geo-grid proof | Measure Maps rank on a city grid; baseline, re-scan monthly; render before/after | `grid_scans`, heatmap SVG, report section | **Durable #3** (proof store) |
| 6 Shared infra | One schema owner, one env/encoding helper set, one vendor adapter | all groups | Keep-lean (but load-bearing) |

**Boundaries.** The only external services are Google Places, DataForSEO, PageSpeed,
Perplexity, and (build-time) GeoNames. DataForSEO is reached through the single
`dataforseo.py` adapter. Two internal handoff boundaries leave the engine: a **file handoff**
(`grid_report_section` writes an HTML section that a downstream report assembler consumes -
never a cross-repo import), and the **list -> outreach/CRM** boundary (the engine's job ends
at the list).

## 6. Data model

One SQLite master store, `data/leads.sqlite`. Schema is owned entirely by
`leads_db.py`, which calls two submodules at init: `leads_db_gbp.py` (the `gbp_audits` table)
and `leads_db_grid.py` (`grid_scans`, `grid_points`). Column-level detail for the
`prospect_sweep` tables lives in `docs/HLD.md` §8; this is the system-level summary.

| Table | Grain | Purpose | Owner |
|-------|-------|---------|-------|
| `businesses` | one per `place_id` | Deduped master prospect list; refreshable cache | leads_db |
| `swept_cells` | one per swept cell | Coverage ledger -> resumability | leads_db |
| `runs` | one per sweep run | Spend/audit trail | leads_db |
| `site_enrichment` | one per enriched lead | Free SEO/AEO readiness + agency fingerprint | leads_db |
| `site_contacts` | one per scraped lead | Email/owner (public only, never fabricated) | leads_db |
| `site_rankings` | one per paid measurement | SERP rank / backlinks / AI citations | leads_db |
| `gbp_audits` | **append-only** snapshot | GBP completeness + neglect score over time | leads_db_gbp |
| `grid_scans` | one per scan | Geo-grid header: SoLV, avg rank, top-3 share | leads_db_grid |
| `grid_points` | one per (pin x keyword) | Rank at each grid point | leads_db_grid |

**The two append-only tables (`gbp_audits`, `grid_scans`/`grid_points`) are durable asset
#3:** a snapshot is never overwritten, so the baseline row is the "before" and any re-scan
is the "since baseline" delta. This is the same before/after that feeds the downstream
report assembler.

**ToS posture (load-bearing):** `place_id` is the only field stored indefinitely; everything
else carries a `last_refreshed_ts` and is treated as a refreshable cache. No review text,
reviewer names, photo URLs, or consumer PII is persisted in the durable store (GBP review
text lives only in transient `.tmp/`). See §8.

## 7. External interfaces & integrations

| Service | Used by | Auth (`.env` only) | Failure behavior |
|---------|---------|--------------------|--------------------|
| Google Places API (New) | discovery, `places_reviews`, `gbp_verify_google` | `GOOGLE_PLACES_API_KEY` | 429/5xx -> backoff+retry; persistent cell failure logged and skipped, run continues |
| DataForSEO (Business Data, SERP, Backlinks) | `gbp_audit`, `geo_grid`, `measure_shortlist`, `dataforseo` | `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` | task-queue submit/poll; only `live` priority wired today (see §9); defensive field parsing |
| Google PageSpeed Insights | `pagespeed` | `PAGESPEED_API_KEY` (or unauthenticated quota) | missing key -> signal skipped, labeled not-measured |
| Perplexity | `check_ai_visibility` | `PERPLEXITY_API_KEY` | missing key -> AI-visibility skipped, never fabricated |
| GeoNames (build-time) | `build_seed` | none (CC-BY) | cached under `data/.cache`; attribution in `data/NOTICE-geonames.txt` |

**The vendor boundary is a data-protection device, not a convenience.** DataForSEO is reached
only through `dataforseo.py` (one auth path, one task-post contract). Every paid tool follows
the same discipline: an always-on `--dry-run` cost estimate, then a hard `--budget` ceiling
that stops *before* the request that would cross it, and resumability so a stop loses at most
the in-flight item.

**Internal handoffs (leave the engine):**
- **-> downstream report assembler (file handoff):** `grid_report_section.py --place-id ..
  --out ..` writes a self-contained HTML section; a separate report tool assembles it into
  its shell. No cross-repo imports in either direction.
- **-> outreach / CRM (list handoff):** the ranked prospect workbook / pitch list is the
  boundary. Outreach compliance (CAN-SPAM, CASL, DNC) lives beyond it (N4, §8).

## 8. Security & compliance posture

At system altitude; the `prospect_sweep` SEC-1..6 in `docs/HLD.md` §10 stand.

- **SEC-A (secrets):** all API credentials live only in `.env` (gitignored). Never logged,
  printed, committed, or written into the store or exports.
- **SEC-B (ToS storage):** honor Google Places caching limits - `place_id` durable, all other
  fields refreshable cache with `last_refreshed_ts`; no review text / reviewer / PII in the
  durable store (GBP review text transient only). DataForSEO is used for public business data,
  not owner-scoped GBP APIs (which need OAuth we do not hold for cold prospects).
- **SEC-C (no fabrication):** ratings, reviews, owner contacts, and client outcomes are only
  ever recorded from real sources; a missing signal is labeled "not measured," never invented.
- **SEC-D (no customer-facing falsehood):** any signal that is an *inference* (agency
  management verdict; GBP claim status) must be labeled as such and must never be asserted as
  fact in a customer-facing artifact. **Enforced for GBP claim status as of 2026-07-08 (§9,
  Phase A done):** the `is_claimed` neglect weight is 0, no customer-facing renderer (`gbp_diff`)
  emits claim status, and a regression test guards it.
- **SEC-E (injection surface):** trade/region configs are trusted local files, but all values
  are interpolated as data (parameterized JSON bodies, no shell interpolation).

## 9. Risks, trade-offs & open questions

**The one real code fix - `is_claimed` contamination (RESOLVED + IMPLEMENTED 2026-07-08, Phase A):**
DataForSEO's `is_claimed` is unreliable (it scrapes Google's public pages; claim status is a
brittle inference, proven to false-negative on clearly-claimed 100+ review businesses). Yet
`neglect_score` weights "unclaimed" at **40 of 100 points - the single dominant signal**
(`gbp_audit.py:49`, `NEGLECT_WEIGHTS["unclaimed"] = 40`). Consequences: ~151 of 1,687 audits
are wrongly ranked most-neglected, corrupting the `--lens gbp` outreach list, and a
customer-facing audit would print "unclaimed" as a flat falsehood (violates SEC-D).
- **Remediation (do regardless):** neutralize the weight, free-re-score the 1,687 existing
  snapshots, and add a hard rule + test that no customer-facing output states claim status as
  fact. **Ruled 2026-07-07: option (a)** - set the `unclaimed` weight to 0 but keep collecting
  the `is_claimed` field for reference (still visible in raw data, just zero scoring weight).
  Rejected: (b) removing the field entirely, and (c) gating it behind a `gbp_verify_google`
  confirmation (salvages the signal but adds cost and complexity).

**Correction to the earlier refactor plan (recorded so it is not re-attempted):** a prior
punch-list (from stale notes) proposed deleting `export_leads`, `places_discover`,
`hvac_report`, and one of `scrape_contacts`/`scrape_leads`, and "building" the vendor adapter.
Verified false against the code: the adapter already exists (single `dataforseo.py`), and all
four "dead" tools serve live codepaths (legacy single-market vs national-DB). Names mislead
(`hvac_report` is vertical-agnostic) but the code is live. **No deletions. No foldering.**

**Other known risks / open items (not refactor work):**
- **PDF renderer [RESOLVED 2026-07-16]** - the downstream report assembler renders PDF with
  Playwright Chromium; the old renderer dependency is gone (upstream archived 2023-01).
- **DataForSEO live field-name confirmation** - `gbp_audit` and `geo_grid` parse the task
  response defensively but the exact field names / result path / `cost` are unconfirmed
  against a live task. Confirm on the first live run before any bulk paid pass.
- **Only `live` DataForSEO priority is wired** ($0.002/req). The cheaper async
  `standard`/`priority` batch flow (task_post -> task_get poll) is not implemented; guarded
  with a clear error.
- **`site_rankings` is empty** - the paid shortlist measurement (`measure_shortlist`) is built
  and dry-run-verified but has never run live. Not a defect; a not-yet-run capability.
- ~~**A manual seam remains in the motion:** the GBP review classifier is a spawned-agent step.~~
  **CLOSED 2026-07-08 (Phase C):** the classifier is now a single tool, `gbp_classify.py` (Claude
  Haiku, injected-client seam, `--dry-run`/`--budget` discipline), so the whole review scan is one
  command per step. This was the last non-one-command part of the machine, so it directly advances
  the operability goal (G5).

**Trade-offs (held):** cost-control-first density sweep over exhaustive grid (predictable
spend, some rural gaps); SQLite store over per-run JSON (dedup/resumability, one export shim);
text-query discovery over type-based (captures messy trade naming, some noise filtered).

## 10. Build / rework sequence (surgical)

- **Phase A - `is_claimed` remediation [DONE 2026-07-08].** Per §9 option (a):
  `NEGLECT_WEIGHTS["unclaimed"] = 0` in `gbp_audit.py` (is_claimed still collected raw);
  `neglect_score` drops zero-weight signals so `unclaimed` can never leak downstream;
  `gbp_diff.py` no longer emits any claim-status flip or resolved signal (SEC-D); the 1,687
  snapshots were free-re-scored in place via the new `rescore_gbp_audits.py` (151 rows moved,
  reachable max 100 -> 60, observed max 72 -> 32); regression tests added in
  `tests/test_gbp_audit.py`. No new spend. *Shipped:* the outreach ranking and any customer-facing
  GBP audit stop lying about claim status.
- **Phase B - documentation completion [DONE 2026-07-08, no code].** Wrote the missing geo-grid
  subsystem note (`docs/DESIGN-geo-grid.md`) matching `DESIGN-gbp-prospect-audit.md`, so all three
  paid subsystems (sweep, GBP, geo-grid) now have a spec this doc points down to. Closes the
  documentation gap for durable asset #3.
- **Phase C - operability polish [partly done 2026-07-08, no restructure].**
  DONE: wrapped the manual review-classifier seam into `gbp_classify.py` (§9), so the review scan
  is one command per step. DONE 2026-07-16: the downstream report assembler now renders PDF via
  Playwright Chromium (the previous renderer was dropped entirely - unmaintained upstream). STILL OPEN
  (operator action, not code): confirm the DataForSEO live field names on the next paid run
  (the gbp_audit + geo_grid extractors are defensive but unconfirmed at scale).
- **Explicitly NOT doing:** structural reorg / foldering, tool deletion, or an adapter rebuild
  (N1, §9). The code is healthy; the value is Phases A-B.
