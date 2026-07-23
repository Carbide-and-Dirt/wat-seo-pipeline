# Agent Instructions — SEO + Agentic-Search Analysis

You're working inside the **WAT framework** (Workflows, Agents, Tools): probabilistic AI handles reasoning, deterministic code handles execution. That separation is what makes the analysis reliable and repeatable instead of a one-off read-the-pages-by-eye effort.

## The WAT Architecture
**Layer 1 — Workflows (`workflows/`):** Markdown SOPs. Each defines the objective, inputs, which tools to run in what order, expected outputs, the scoring rubric, and known gotchas. Read the relevant workflow before doing anything.

**Layer 2 — Agents (you):** Intelligent coordination. Read the workflow, run the tools in sequence, handle failures, and — critically for this skill — **write the interpretive narrative** the deterministic tools deliberately leave to you. You connect intent to execution; you don't eyeball pages when a tool can measure them.

**Layer 3 — Tools (`tools/`):** Python scripts that do the measuring. They fetch raw HTML, parse schema, read the live robots/sitemap, run Core Web Vitals, and query a real AI engine for citations. Consistent, testable, fast. Secrets live in `.env`.

**Why this matters:** the manual version of this analysis had to hedge every structured-data and robots claim as "not observed / unverified" and could only hand over a *self-test prompt pack* because nothing could query a live AI engine. The tools here remove those hedges by **measuring**, so the report states verified facts.

## This skill at a glance
- **Workflow:** `workflows/competitive_seo_audit.md` — competitive audit SOP + scoring rubric. `workflows/prospecting.md` — national lead pipeline. `workflows/client_reporting.md` — geo-grid client delivery. Start with the relevant SOP.
- **Tools:**
  - `audit_site.py` — raw-HTML on-page + technical audit (schema read from source, live robots/sitemap/llms, AI-bot blocking).
  - `pagespeed.py` — Core Web Vitals via PageSpeed Insights.
  - `check_ai_visibility.py` — live Perplexity citation measurement (mentioned/cited per entity).
  - `places_reviews.py` — verifiable Google rating + review count per entity (Places API).
  - `dataforseo.py backlinks|serp` — domain authority + backlinks, and organic SERP rank (paid).
  - `score_report.py` — applies the rubric, writes the scored report scaffold + auto-detected market gaps (folds in reviews/authority/SERP tables when present).
- **Config:** `targets/<market>.json` — one file drives audit + AI-visibility + scoring. See `targets/example.json`.
- **Run order:** define `targets/*.json` → `audit_site.py` (+`pagespeed.py`) → `check_ai_visibility.py` → `score_report.py` → you write the narrative.

### Prospecting pipeline (lead-gen, separate from the competitive audit)
Used to enumerate every business of a trade across an area and rank them as website-sales prospects. **Vertical-agnostic**: one discovery config drives any trade.
- `places_discover.py <area>.json` — find every business of a type across the config's towns (Places API New, paginated). Flags `no_website` leads. Config keys: `query`, `type_keywords`, `primary_types`, `vertical`, `industry_schema`.
- `normalize_prospects.py <discover>.json --config <area>.json` — classify + dedup: `tier` (chain vs local from the config `chains` list), `in_footprint` (`out_of_area_towns`), templated-microsite flag (`microsite_adjectives`), and collapses multi-location brands sharing one website.
- `scrape_contacts.py <discover>.json --out .tmp/contacts/<v>-contacts.json` — best-effort email/owner/phone from each site (never fabricated).
- `hvac_report.py <final>.json --contacts <...> --out output/<slug>-prospects` — assembles the tiered opportunity report (md/csv/xlsx); labels/schema come from the discovery JSON's `vertical`/`industry_schema` (despite the legacy filename it's not HVAC-specific).
- **Pipeline:** `places_discover` → `normalize_prospects` → `scrape_contacts` → `audit_site --firecrawl auto` → `hvac_report`. Example config: `targets/example-discovery.json` (copy it for your trade/area).

#### National lead discovery + enrichment (`prospect_sweep`, supersedes `places_discover` for big areas)
SQLite master store (`data/leads.sqlite`, gitignored); built per `docs/HLD.md`. `place_id` is the only permanently stored field (Google ToS); all other fields are a refreshable cache. Relevance tiers: `match` = confirmed trade, `maybe` = review (`general_contractor` alone is never a confirm). Always `--dry-run` first; live runs require a HARD `--budget`.
- `leads_db.py` — owns the schema + connection; `python tools/leads_db.py init|status`.
- `prospect_sweep.py` — density-ordered, budget-capped, resumable Places sweep into the store.
- `enrich_sites.py` — free SEO/AEO-readiness + agency fingerprint pass.
- `gbp_audit.py` — **PAID** (cheap, ~$0.004/prospect) Google Business Profile neglect audit via
  DataForSEO Business Data (`is_claimed`, rating, categories, photos, hours, description, attributes
  + post recency), keyed by `place_id`, no owner access. Append-only snapshots -> tunable neglect
  score. `--dry-run` first; live needs `--budget`. The GBP analog of `enrich_sites.py`.
- `gbp_diff.py` — **free ($0)** before/after reducer over two GBP audits (the client "since signup" report).
- **Follow-up-complaint review scan** (SOP: `workflows/review_scan.md`) — find good businesses whose
  reviews complain about missed calls / no follow-up (the best cold-outreach targets):
  `gbp_reviews.py` (**PAID**, fetch lowest-rated reviews to transient `.tmp`) -> `gbp_review_batches.py`
  (batch in-scope trades) -> `gbp_classify.py` (**PAID**, cheap; Claude Haiku flags follow-up
  complaints, one command) -> `gbp_pitch_list.py` (ranked, trade-tabbed pitch list). Trade scope in `gbp_trades.py`.
- `scrape_leads.py` — free DB-integrated email + owner scrape.
- `leads_report.py` — free segmented workbook; `--lens budget` (ability-to-pay) or `--lens gbp`
  (most-neglected Google Business Profile first, from `gbp_audit.py`) ranking.
- `measure_shortlist.py` — **PAID** SERP + backlinks + Perplexity shortlist pass; always dry-run first.
- `export_leads.py` — legacy shim: writes store back to discover-JSON for the local pipeline.

SOP: `workflows/prospecting.md`

#### Geo-grid rank tracking (ADR-LP-001 — the client-delivery/monitoring layer)
Maps rank at every pin of a rows×cols grid, per keyword; baseline at sale time + monthly re-scans + diff. Config = `grid` block in `targets/<client>.json`. Only `priority: live` is wired ($0.002/request). Always `--dry-run` first; live requires `--budget`. Match by `place_id` only, never name. Scans are resumable; budget-stop marks status `partial`.
- `geo_grid.py` — **PAID** scan; writes `grid_scans`/`grid_points`, computes SoLV.
- `leads_db_grid.py` — owns the grid schema + helpers (created by `leads_db.py init`).
- `grid_diff.py` — **free ($0)** baseline-vs-latest reducer; positive numbers always mean improvement.
- `grid_heatmap.py` — **free ($0)** Steel & Amber SVG heatmap, single grid or before/after pair.
- `grid_report_section.py` — **free ($0)** `build_grid_section(conn, place_id)` → HTML `<section>` for a downstream client-report assembler.

SOP: `workflows/client_reporting.md`

## Commands
Standalone Python CLIs run from the repo root. Use a project venv. Each tool is its own `argparse` entry point with `-h`; outputs are JSON in `.tmp/`, the report is markdown in `output/`.
```bash
# One-time setup
pip install -r requirements.txt
cp .env.example .env                 # then fill in keys
# Only if you'll use audit_site.py --render (JS-rendered sites; heavy):
pip install -r requirements-render.txt && playwright install chromium

# Easiest: one command runs the whole pipeline from one config
python run.py targets/<market>.json --with-pagespeed --skip-existing
python run.py targets/<market>.json --with-pagespeed --with-paid --runs 2   # include paid DataForSEO

# Or run each step manually (each writes JSON the next step reads)
python tools/audit_site.py <all page URLs> [--skip-existing] [--workers N]   # -> .tmp/audit/<host>.json
python tools/pagespeed.py <home + key URLs>                                  # -> .tmp/pagespeed/<host>.json   (optional)
python tools/places_reviews.py targets/<market>.json                         # -> .tmp/places/reviews.json     (optional, needs GOOGLE_PLACES_API_KEY)
python tools/dataforseo.py backlinks <domain...>                             # -> .tmp/dataforseo/backlinks.json (optional, PAID)
python tools/dataforseo.py serp targets/<market>.json                        # -> .tmp/dataforseo/serp.json      (optional, PAID)
python tools/check_ai_visibility.py targets/<market>.json --runs 2 [--skip-existing]  # -> .tmp/ai_visibility.json (needs PERPLEXITY_API_KEY)
python tools/score_report.py targets/<market>.json                           # -> output/<slug>-report.md

# Tests — no network, no spend. Run the whole suite, or any file standalone.
pytest tests/                        # runs the full suite
python tests/test_tools.py           # or run one standalone (each has a __main__ runner)
```
- **`run.py` is the orchestrator** — it runs audit → pagespeed → places → dataforseo → ai-visibility → score in order, skipping any step whose API key is absent (paid DataForSEO only with `--with-paid`). You still write the narrative afterward.
- **Run a single tool in isolation** for debugging by passing just its inputs; each reads only the JSON it needs from `.tmp/`, so steps re-run independently. `--skip-existing` (on `audit_site`, `check_ai_visibility`, and `run.py`) avoids refetching/re-spending on hosts/outputs already present.
- `score_report.py` is **purely a reducer over `.tmp/`** — re-run it freely (no API cost) after editing the rubric or adding `.tmp/` inputs; it folds in whichever JSON files exist (audit, pagespeed CWV, AI-visibility, places, backlinks, SERP).
- Tools that spend credits (`check_ai_visibility.py`, `dataforseo.py`, `places_reviews.py`): **confirm with the user before re-running.**
- **After changing any matching/parsing logic, run `python tests/test_tools.py`** — it guards the robots.txt group parser and domain/mention matching (the parts that silently corrupt scores when wrong).

## How to operate
1. **Look for an existing tool first.** Check `tools/` against what the workflow needs before building anything new.
2. **Set up once.** `pip install -r requirements.txt`; copy `.env.example` → `.env` and add keys. `PERPLEXITY_API_KEY` gates the AI-visibility step; without it, skip that step and label the report.
3. **Don't fabricate.** Every number in the report must trace to a tool's JSON output. If a signal wasn't measured, say "not measured," not "absent."
4. **Learn when things fail.** Read the full error, fix the tool, retest (check with the user before re-running anything that spends API credits), and **document the gotcha in the workflow** so it's never hit twice.

## File structure
```
run.py              # Orchestrator for the competitive audit (audit→pagespeed→places→dataforseo→ai→score)
workflows/          # Markdown SOPs (what to do, the rubric, gotchas)
tools/              # Python execution scripts (each a standalone argparse CLI; leads_db.py owns the schema)
tools/lib/common.py # Shared utilities: load_env, utf8_stdout, slug (imported by all CLI tools)
targets/            # <market>.json configs — the reusable input/asset
tests/              # Standalone test files (run each directly, or `pytest tests/`)
data/               # GeoNames seed (places_us_ca.csv) + national master store (leads.sqlite, gitignored)
docs/HLD.md         # High-Level Design for the national prospect_sweep build
.tmp/               # Disposable intermediates (audit/pagespeed/ai JSON, raw HTML). Regenerable.
output/             # Generated reports
.env                # API keys — NEVER store secrets anywhere else
```

## The self-improvement loop
Every gap is a chance to strengthen the system: identify what's missing → build/fix the tool → verify → update the workflow → move on. Don't overwrite a workflow or config without asking — refine them.

## Bottom line
You sit between what the user wants (workflows) and what gets measured (tools). Read the SOP, run the right tools, recover from failures, write the narrative the tools leave to you, and keep the system sharper than you found it. Measure, don't guess.
