# Workflow: Competitive SEO + Agentic-Search Audit

## Objective
Benchmark a set of businesses' websites against each other on **traditional SEO** and **AI/agentic-search visibility (GEO)**, and produce a scored, evidence-backed report. The whole point of this skill is to replace eyeballing pages with **deterministic measurement** — read raw HTML for schema, fetch the *live* robots/sitemap, run Core Web Vitals, and query a real AI engine for citations — so claims are verified, not inferred.

## Inputs
- **A targets config** — `targets/<market>.json` (schema below). One file drives the whole run.
- **API keys in `.env`** (copy from `.env.example`):
  - `PERPLEXITY_API_KEY` — required for live AI-visibility (Step 3). Without it, skip Step 3 and label the report accordingly.
  - `PAGESPEED_API_KEY` — optional, raises the CWV rate limit (Step 2b).

## Outputs
- **Per-host audit JSON** → `.tmp/audit/<host>.json` (raw structured signals)
- **Core Web Vitals JSON** → `.tmp/pagespeed/<host>.json`
- **AI-visibility JSON** → `.tmp/ai_visibility.json` (per-query results + scoreboard)
- **Final report** → `output/<slug>-report.md` (scored tables + leaderboard + gaps + limitations; you, the agent, add the narrative)

## Tools (look here before writing anything new)
- `tools/audit_site.py <url...>` — raw-HTML on-page + technical audit: title/meta/canonical/OG, **JSON-LD schema (read from raw source, not rendered)**, headings, word count, links, images; plus per-host `robots.txt` (sitemap declared? crawl-delay? **which AI bots are blocked**), `sitemap.xml`, `llms.txt`. `--render` adds a Playwright pass for JS-injected schema. `--firecrawl {auto,always,off}` (needs `FIRECRAWL_API_KEY`) escalates WAF-blocked / JS-shell pages through Firecrawl so they can still be audited — see the Firecrawl gotcha below. Each page records its `fetched_via` (requests | firecrawl | playwright).
- `tools/pagespeed.py <url...>` — Core Web Vitals + performance score via PageSpeed Insights (server-side Lighthouse + CrUX field data).
- `tools/check_ai_visibility.py <config>` — queries the **Perplexity Sonar API** (returns citations) for each `ai_queries` entry and scores whether each entity was **mentioned** and/or **cited**. This is the "measure the real AI answer" step the manual reports couldn't do.
- `tools/places_reviews.py <config>` — resolves each entity to its Google place and returns **verifiable** rating + review count (`--reviews` adds recent snippets). Kills the "review count is an unverified snapshot" caveat. Needs `GOOGLE_PLACES_API_KEY`. **Matches are validated** (entity name/alias on word boundaries + expected state when the listing shows an address; never `results[0]` blind) and every entity lands in one of three explicit states: `matched`, `mismatch` (counts withheld), or `not_found_via_api` (which is NOT "no profile exists"). On a Places miss/mismatch it can cross-check via DataForSEO Business Data (`--fallback auto`, PAID ~$0.005/entity, index-independent — catches listings Places Text Search can't see).
- `tools/dataforseo.py backlinks <domain...>` / `dataforseo.py serp <config>` / `dataforseo.py maps <config>` — **domain authority + backlink counts**, **organic SERP rank**, and **local-pack (Google Maps) rank + pack leaders** for the config's `seo_keywords`. The pack is the surface local customers actually see — never assert "can't be found" from the organic table alone. Paid (DataForSEO credits); needs `DATAFORSEO_LOGIN`/`DATAFORSEO_PASSWORD`.
- `tools/score_report.py <config>` — applies the rubric (below) to all collected JSON and writes the report scaffold with tables, leaderboard, and auto-detected market-wide gaps. Auto-includes **Core Web Vitals / verified-reviews / authority / SERP** tables when those JSON files exist (CWV is enrichment only — it does not affect the /20 rubric).
- `run.py <config>` — **orchestrator**: runs the whole pipeline in order (audit → pagespeed → places → dataforseo → ai-visibility → score), skipping any step whose API key is absent. Flags: `--with-pagespeed`, `--with-paid` (DataForSEO), `--runs N`, `--skip-existing`, `--render`. Prefer this over typing the steps by hand.

## Targets config schema (`targets/<market>.json`)
```json
{
  "market": "Nashville, TN climbing gyms",
  "slug": "nashville-climbing-gyms",
  "city": "Nashville, TN",
  "brand":       { "name": "...", "aliases": ["..."], "domain": "example.com", "places_query": "optional override" },
  "competitors": [ { "name": "...", "aliases": ["..."], "domain": "rival.com" } ],
  "pages": { "<entity name>": ["https://.../", "https://.../pricing", "..."] },
  "ai_queries": ["Who are the best ... in <city>?", "Tell me about <brand>.", "..."],
  "seo_keywords": ["climbing gym nashville", "bouldering nashville", "..."],
  "serp_location": "Nashville,Tennessee,United States"
}
```
- `aliases` — alternate names used to detect mentions/citations (e.g. a brand whose domain ≠ name).
- `city` — appended to entity names when resolving Google places (Step 2c). `places_query` (per entity) overrides it for hard-to-match names.
- `pages` — audit 2–4 representative pages per entity (home + a money page + a content/FAQ page). More pages = wider, less "not found = absent" risk.
- `ai_queries` — mix of generic "best in city", service+city long-tail, and branded prompts. Generic prompts are aggregator-dominated; branded prompts audit data accuracy.
- `seo_keywords` / `serp_location` — search terms and a DataForSEO location string for organic-rank measurement (Step 2d). Location format is `"City,State,United States"`.

## Steps
> **Fast path:** once `targets/<market>.json` exists, `python run.py targets/<market>.json --with-pagespeed --skip-existing` runs steps 2–4 in order (add `--with-paid` for DataForSEO), then you do step 5. The manual steps below are for debugging or partial re-runs.

1. **Define the field.** Build/confirm `targets/<market>.json`. Identify the real competitors first (search + maps); don't guess domains — fetch them. For multi-location brands, treat one website as one entity and audit a couple of location pages.
2. **Audit the sites.**
   - `python tools/audit_site.py <all page URLs>` → `.tmp/audit/`
   - (2b, optional) `python tools/pagespeed.py <home + key pages>` → `.tmp/pagespeed/`
   - (2c, optional — needs `GOOGLE_PLACES_API_KEY`) `python tools/places_reviews.py targets/<market>.json` → `.tmp/places/`. **Sanity-check the matched listing name/address** in the output before trusting a count.
   - (2d, optional — paid, needs DataForSEO creds) `python tools/dataforseo.py backlinks <domains>`, `python tools/dataforseo.py serp targets/<market>.json`, and `python tools/dataforseo.py maps targets/<market>.json` (local-pack rank) → `.tmp/dataforseo/`.
3. **Measure AI visibility** (needs `PERPLEXITY_API_KEY`):
   - `python tools/check_ai_visibility.py targets/<market>.json --runs 2` → `.tmp/ai_visibility.json`
   - Use `--runs 2` or 3: AI answers vary by call; trends matter more than one shot.
4. **Score + assemble.** `python tools/score_report.py targets/<market>.json` → `output/<slug>-report.md`.
5. **Add the narrative (your job as the agent).** Fill the "Agent narrative" section: a leaderboard with 2–3 sentences per site (strengths/weaknesses grounded in the tables), what the leader does that others don't, and the single highest-impact fix per site. **Do not invent numbers** — cite the tables. **Negative-existence claims need positive evidence:** never write "no GBP", "not findable", "no reviews", or "site is dead" from an empty/failed lookup — an API returning nothing is a fact about the API, not the world (see the Places false-negative gotcha below). If a surface wasn't measured (check the report's auto-generated Limitations), say "not measured", and scope every rank claim to its surface ("not in the top 20 *organic* results" ≠ "can't be found" — the local pack is measured separately by `dataforseo.py maps`).

## Scoring rubric (0–5 per dimension, /20 total) — documented so it stays consistent
Defined in `tools/score_report.py`; keep this section in sync if you change it.
- **Technical (5):** +1 sitemap (declared or present) · +1 citation bots (`OAI-SearchBot`/`PerplexityBot`/`Bingbot`) not blocked · +1 all pages well-titled (10–65c) · +1 all have a meta description (50–170c) · +1 canonical + HTTPS + viewport.
- **Content (5):** +1 avg ≥400 words · +1 avg ≥800 · +1 every page has an H1 and ≥2 H2 · +1 FAQPage schema present · +1 ≥3 pages audited.
- **Local (5, capped):** +2 LocalBusiness schema · +1 phone/NAP on page · +1 Review/AggregateRating schema · +1 Organization entity.
- **Agentic/GEO (5):** +1 any JSON-LD · +1 FAQPage schema · +1 citation bots not blocked · +1 `llms.txt` present · +1 mentioned in ≥1 live AI query.

## Edge cases / gotchas (learned — keep adding)
- **`robots.txt` is served by the CDN, not the repo.** Cloudflare's "Managed robots.txt / Block AI bots" injects AI-bot `Disallow` and `Content-Signal: ai-train=no` at the edge — so the *deployed* file differs from any repo copy. **Always trust the fetched live file** (that's what `audit_site.py` reads); a repo `robots.txt` can be misleading. Changing AI-bot access is a **CDN dashboard** action, not a file edit.
- **Distinguish citation bots from training bots.** `GPTBot`/`ClaudeBot`/`Google-Extended`/`CCBot` are AI-*training* crawlers; blocking them barely affects being *cited*. The bots that fetch to answer/cite are `OAI-SearchBot`, `ChatGPT-User`, `PerplexityBot`, `Bingbot` — those being blocked is the real GEO problem. The rubric scores on the citation bots only.
- **Windows console encoding.** Scripts force UTF-8 stdout; if you add prints, keep them ASCII or you'll hit `cp1252` crashes.
- **Sandbox / permission traps (when running tools via a sub-agent):**
  - Sub-agents can only **write inside a working directory**. Write outputs to `.tmp/` and `output/` *inside this skill folder*; don't target an external path (it'll be denied — the main session can relocate afterward if needed).
  - `WebFetch` is auto-denied for **background** agents (no way to approve the prompt). These tools use Python `requests`/Playwright directly, sidestepping `WebFetch` entirely — prefer them.
- **AI-visibility is volatile and engine-specific.** Perplexity ≠ ChatGPT ≠ Gemini ≠ Google AI Overviews. The report says so. Re-run on a cadence and trend it; add other engines via the `ask_*` hook in `check_ai_visibility.py`.
- **`--render` cost.** Only use it when a site is a JS SPA and `requests` shows no schema; it pulls Chromium (install separately: `pip install -r requirements-render.txt && playwright install chromium`) and is slow.
- **Deep Google-Business-Profile links 404 though the site is live (false "no site" leads).** GBP often lists a *deep* URL (e.g. `example.com/locations/nashville/`) that 404s while the root domain is fine. Two fixes are in place: (1) `audit_site.fetch()` falls back to the **root domain** when a path/query URL 404s; (2) `find_audit()` (in `score_report.py`, used by `hvac_report.py`) prefers a **live (200)** audit file and exact-host match, so a stale www/non-www sibling can't win — a host can produce both `www.x.com.json` (200) and `x.com.json` (404), and the old "first glob match" logic mislabeled live sites as dead. **After any prospecting audit, sanity-check that no build-from-scratch lead with a website URL is actually live** (fetch its root, spaced) — that scan is what caught this case (a multi-location home-services chain whose GBP deep link 404'd while the site was live).
- **Intermittent platform 404s under concurrency = false "dead" sites (false build-leads).** Some shared site platforms (common in pest-control/home-services CRMs — tell-tale identical JSON-LD signature `EntryPoint/Organization/SearchAction/WebSite` across unrelated domains) **IP-rate-limit by request volume** and serve a *branded 404* (real title + schema + content) to bursted/automated requests, but 200 to an isolated hit. A 2,000-review business "returning 404" is the red flag. The 404-page word count is **not** a reliable dead/live discriminator (one such site's rate-limited 404 was 24 words; isolated it was a live 3,148-word homepage). Mitigations now in place: (1) `audit_site.fetch()` does one **retry on 404** (cheap, free) — handles light flakiness; (2) for a whole batch behind one platform, re-fetch the affected hosts **isolated and spaced ~6s apart at `--workers 1`** (recovers them via plain requests — no Firecrawl spend), or force `--firecrawl always` (proxy rotation bypasses the IP limit, but costs credits). In one pest-control market run this turned 41 "build-from-scratch leads" into the correct 18 (24 were live sites). When a non-200 host has a *branded* error page (schema/real title/hundreds of words), suspect rate-limiting before calling it dead.
- **Firecrawl is the fallback fetcher, not the default — keep it bounded.** With `FIRECRAWL_API_KEY` set, `audit_site.py --firecrawl auto` (the default) only spends a credit when plain `requests` is *blocked* (403/202/408/429/5xx) or returns a *JS-only shell* (200 with <2000 bytes); a clean 200 and a genuinely *dead* 404/410 are never escalated (404/410 are real "needs a new site" leads — don't revive them). It requests `formats:["rawHtml"]` specifically so the JSON-LD parsing is unchanged; markdown output would strip the `<script type=ld+json>` blocks. `--firecrawl always` routes every page through it (use only when a whole market is behind one WAF); `off` disables it. Inspect `fetched_via` in the audit JSON to see which pages needed it. This is the intended replacement for the heavy Playwright `--render` path on JS sites. **Don't** use Firecrawl's LLM `/extract` for the audit — extraction stays deterministic per the WAT design.
- **Use a real browser User-Agent, and trust the HTTP status.** A bot-style UA gets WAF-blocked — Sucuri/GoDaddy/Cloudflare/WPEngine return 400/403/520/202 to crawlers, which makes *live* sites look broken and produces garbage "audits" of error pages. `audit_site.py`/`scrape_contacts.py` now send a Chrome UA + Accept headers. Even so, **check `status`**: a 404/410/DNS-fail is a *dead* site (a real lead — they need a new site), a 403/429/202 is *blocked* (couldn't audit — verify by hand), and a "website" that's a facebook.com/bbb.org URL is *not a real site*. Don't score non-200 pages as weak-but-live (a prior HVAC run did exactly that — e.g. a dead 404 ranked as the #1 "weak site"). `hvac_report.py:classify_site()` encodes these buckets.
- **Matching logic is deterministic and tested — don't loosen it.** Two bugs were fixed and are now guarded by `tests/test_tools.py`: (1) the `robots.txt` parser groups `User-agent:` blocks per spec, so a `Disallow: /` for one bot no longer leaks onto `*`/other bots (a prior version reported *all* citation bots blocked when only GPTBot was); (2) domain matching uses `removeprefix("www.")` + **dot-boundary** suffix match (`climb.com` ≠ `myclimb.com`), not `lstrip` or bare substring — `lstrip("www.")` silently mangles any domain starting with `w`. Mention detection is **word-boundary** (alias `Crag` won't match `cragsman`). **Run `python tests/test_tools.py` after touching any of these.**

- **A Places Text Search no-match does NOT mean "no GBP", and the first hit is not a verified match.** Caught on a Durango, CO prospect audit (2026-07-15): the prospect's profile was live on Maps (rating, reviews, hours, website, local-pack placement) yet `places:searchText` returned **zero results** for it under every query tried — city-qualified, town-qualified, and location-biased on the listing's own pin. Reason unknown; do not guess one. And when the query was broadened to coax a hit, the API returned a *different company entirely* (a similarly-named contractor in another city) which `places_reviews.py` accepted silently — it takes `results[0]`. Rules: (1) never report "no GBP" from the API alone — hand-check Google Maps first; (2) always eyeball `matched_name`/`matched_address` before trusting rating/review counts; (3) if the API can't see a live listing, put hand-verified figures in the report labeled as such and leave the tool output as a truthful no-match. Related known gap: the pipeline has no Maps/local-pack rank mode, so "not in top 20" claims apply to blue-link organic only — say so explicitly in prospect-facing narrative.

## Self-improvement notes
- **Built and available:** `places_reviews.py` (verifiable Google reviews) and `dataforseo.py` (backlinks/domain-authority + organic SERP rank). Their output auto-surfaces as enrichment tables in the report. `run.py` orchestrates the full pipeline; PageSpeed CWV now surfaces as its own enrichment table in the report (previously gathered but never used); `audit_site`/`check_ai_visibility` fetch concurrently and support `--skip-existing`; `audit_site` has an optional **Firecrawl fallback fetcher** (`--firecrawl`, `FIRECRAWL_API_KEY`) that recovers WAF-blocked / JS-shell pages without changing the raw-HTML parsing; `tests/test_tools.py` guards the matching/parsing logic.
- **Highest-value tools to add next:** a second AI engine in `check_ai_visibility.py` (OpenAI web-search or Google AI) so GEO isn't Perplexity-only; a DataForSEO **Google Maps / local-pack** mode in `dataforseo.py` (the map pack matters more than organic for local businesses); and folding verified-review volume/recency into the Local rubric line once it's reliably available.
- When a competitor set or market is reused, keep its `targets/*.json` — it's the reusable asset.
- If a check proves repeatedly useful (e.g., detecting Cloudflare AI-bot blocking), promote it from narrative to an explicit rubric line so it's never missed.
