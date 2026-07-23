# wat-seo-pipeline

A local-SEO measurement and lead-discovery pipeline built on the **WAT framework**
(Workflows, Agents, Tools): markdown SOPs define *what* to do, an AI agent (e.g.
Claude Code) coordinates, and deterministic Python CLIs do all the measuring. The
agent never eyeballs a page a tool can measure — every number in a report traces
to a tool's JSON output.

Built and used in production by [Carbide and Dirt](https://github.com/Carbide-and-Dirt)
to audit local-service markets, discover website-sales prospects at national scale,
and deliver monthly geo-grid rank reports to clients.

## What's inside

Three pipelines share one toolbox (`tools/`, every script a standalone `argparse` CLI):

**1. Competitive SEO audit** — score a brand against its local competitors on
on-page/technical SEO, structured data, Core Web Vitals, Google reviews, domain
authority, organic SERP rank, and *AI visibility* (live Perplexity citation
measurement: is the brand mentioned/cited when a buyer asks an AI engine?).
One config (`targets/example.json`) drives the whole run:

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in the keys you have; steps without keys are skipped
python run.py targets/example.json --with-pagespeed
# -> output/<slug>-report.md (scored scaffold + auto-detected market gaps)
```

**2. Prospecting / lead discovery** — enumerate every business of a trade across
an area (Google Places API), classify chain vs local, flag no-website and
templated-microsite leads, scrape public contacts, audit their sites, and rank
them as website-sales prospects. Vertical-agnostic: copy
`targets/example-discovery.json` for any trade. At national scale,
`prospect_sweep.py` runs a density-ordered, budget-capped, resumable sweep into a
SQLite master store (`data/leads.sqlite`), with free enrichment
(`enrich_sites.py`), paid Google Business Profile neglect audits (`gbp_audit.py`,
~$0.004/prospect), a review-complaint scanner, and segmented report workbooks.

**3. Geo-grid rank tracking** — client delivery and monitoring. Scan Maps rank at
every pin of a rows×cols grid per keyword, compute Share of Local Voice, store
baseline + monthly snapshots, and render before/after SVG heatmaps and an HTML
report section. See `docs/samples/` for real output.

## Costs — read this before running anything

Some tools spend real API credits. The conventions are enforced in the tools
themselves and documented in each SOP:

| Tool | Cost | Guardrail |
|---|---|---|
| `audit_site.py`, `score_report.py`, `enrich_sites.py`, `grid_diff.py`, `grid_heatmap.py`, `gbp_diff.py`, `leads_report.py` | free | — |
| `pagespeed.py` | free (API key raises rate limit) | — |
| `check_ai_visibility.py` | paid (Perplexity, cheap) | `--skip-existing` avoids re-spend |
| `places_reviews.py`, `places_discover.py`, `dataforseo.py` | paid (Google Places / DataForSEO) | small, per-invocation scope — review the input list before running |
| `prospect_sweep.py`, `gbp_audit.py`, `gbp_reviews.py`, `gbp_classify.py`, `geo_grid.py`, `measure_shortlist.py` | paid, scales with prospect count | **`--dry-run` first (prints the cost estimate); live runs require a hard `--budget`** |

Budget-capped sweeps/scans stop at the cap and are resumable.

## Layout

```
run.py         # competitive-audit orchestrator (audit → pagespeed → places → dataforseo → ai → score)
workflows/     # the SOPs: competitive_seo_audit, prospecting, review_scan, client_reporting
tools/         # the measuring instruments (standalone CLIs; leads_db.py owns the DB schema)
targets/       # market/discovery configs — start from example.json / example-discovery.json
tests/         # no-network, no-spend test suite (pytest tests/)
docs/          # HLD + design docs (ARCHITECTURE.md is the map) + samples/ showcase output
data/          # GeoNames place seed (CC BY 4.0, see NOTICE) + leads.sqlite (gitignored)
```

`CLAUDE.md` contains the agent operating instructions — if you use Claude Code or
another coding agent, it picks up the WAT conventions (read the SOP first, never
fabricate a number, confirm before spending credits) automatically.

## Requirements

Python 3.10+ (CI tests 3.10 and 3.14). `pip install -r requirements.txt` (plus
`requirements-render.txt` + `playwright install chromium` only if you need
`audit_site.py --render` for JS-only sites). API keys are all optional and
per-feature — see `.env.example`; any step whose key is absent is skipped and the
report labels it "not measured."

## Development & quality gates

```bash
pip install -r requirements-dev.txt    # ruff (pinned), mypy, pytest — dev only, never shipped
```

The same checks run in three places: a Claude Code Stop hook (fast static
subset), the pre-push hook (`.husky/pre-push` — copy it to
`.git/hooks/pre-push`), and CI on pushes and PRs to `main`
(`.github/workflows/quality-gate.yml`): `ruff check`, `ruff format --check`,
`mypy`, a secrets scan, an encoding check, and the full pytest suite.

## Responsible use

This toolkit measures public web pages and public business listings, and it can
collect publicly posted business contact details for outreach. If you use it,
you own the compliance that comes with that:

- **Outreach laws.** Contact lists built with `scrape_contacts.py` /
  `scrape_leads.py` come from information businesses publish themselves — but
  how you contact them is regulated (CAN-SPAM in the US, CASL in Canada, GDPR/
  ePrivacy in the EU). Cold email/SMS compliance is your responsibility.
- **Provider terms.** Use of the Google Places API, DataForSEO, Perplexity, and
  Firecrawl is governed by their terms of service. This project deliberately
  stores only `place_id` permanently from Google responses — everything else is
  a refreshable cache, per Google's caching policy. Keep it that way.
- **Measurement, not abuse.** The auditing tools fetch a handful of pages per
  site at normal request rates. Don't turn them into a crawler; don't use the
  pipeline to harass businesses or spam at scale.

## Data attribution

`data/places_us_ca.csv` is derived from [GeoNames](https://www.geonames.org/)
(CC BY 4.0) — see `data/NOTICE-geonames.txt`. Google Places data is cached only
as Google's ToS permits (`place_id` is the only permanently stored field).

## License

MIT — see [LICENSE](LICENSE).
