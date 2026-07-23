#!/usr/bin/env python3
"""
score_report.py — apply the scoring rubric and assemble the benchmark report.

Reads the structured JSON produced by the other tools (.tmp/audit/*.json,
.tmp/pagespeed/*.json, .tmp/ai_visibility.json) plus the targets config, applies
a transparent 0-5 x 4-dimension rubric, and writes a scored markdown report with
comparison tables, a leaderboard, market-wide gaps, the live AI-visibility
scoreboard, and a limitations section.

Deterministic by design (the WAT split): this produces the SCORES, TABLES, and
DATA. The agent then adds narrative interpretation per the workflow — the
report ends with an "Agent narrative" placeholder to fill in.

Usage:
    python tools/score_report.py targets/<name>.json
    python tools/score_report.py targets/<name>.json --out output/<name>-report.md
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

CITATION_BOTS = ["OAI-SearchBot", "PerplexityBot", "Bingbot"]  # bots that fetch to CITE in answers


def load(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def domain_matches(host, domain):
    """True if a fetched host is the entity domain or a subdomain of it (ignoring
    www.). Dot-boundary match so 'climb.com' does NOT match 'myclimb.com'."""
    h, d = host.lower().removeprefix("www."), domain.lower().removeprefix("www.")
    return bool(d) and (h == d or h.endswith("." + d) or d.endswith("." + h))


def find_audit(domain, audit_dir):
    """Match an entity domain to its .tmp/audit/<host>.json file.

    A host can have www and non-www audit files (e.g. one 200, one stale 404).
    Prefer a file with a LIVE (200) homepage, then an exact host match, so a
    stale sibling can't mislabel a live site as dead."""
    best, best_key = None, (-1, -1)
    dn = domain.lower().removeprefix("www.")
    for f in Path(audit_dir).glob("*.json"):
        if not domain_matches(f.stem, domain):
            continue
        d = load(f) or {}
        live = (
            1
            if any(
                (not p.get("error")) and p.get("status") == 200 for p in d.get("pages", {}).values()
            )
            else 0
        )
        exact = 1 if f.stem.lower().removeprefix("www.") == dn else 0
        if (live, exact) > best_key:
            best_key, best = (live, exact), d
    return best


def find_pagespeed(domain, ps_dir):
    """Return the homepage CWV record for an entity domain, or None.

    pagespeed.py writes a list of per-URL records per host; the first record is
    the first URL audited (the homepage, when the config lists it first).
    """
    for f in Path(ps_dir).glob("*.json"):
        if domain_matches(f.stem, domain):
            recs = load(f) or []
            for rec in recs:
                if not rec.get("error"):
                    return rec
    return None


def agg_pages(audit):
    """Collapse a host's pages into representative signals (homepage + OR of flags)."""
    pages = [p for p in audit.get("pages", {}).values() if not p.get("error")]
    if not pages:
        return None
    home = pages[0]
    flags = {
        k: any(p.get("schema_flags", {}).get(k) for p in pages)
        for k in ("LocalBusiness", "FAQPage", "Organization", "AggregateRating", "Review")
    }
    return {
        "pages_n": len(pages),
        "avg_words": round(sum(p.get("word_count", 0) for p in pages) / len(pages)),
        "all_titled": all(10 <= p.get("title_length", 0) <= 65 for p in pages),
        "all_desc": all(50 <= p.get("meta_description_length", 0) <= 170 for p in pages),
        "all_h1": all(p.get("h1_count", 0) >= 1 for p in pages),
        "multi_h2": all(p.get("h2_count", 0) >= 2 for p in pages),
        "canonical": bool(home.get("canonical")),
        "https": home.get("https"),
        "viewport": home.get("viewport"),
        "any_jsonld": any(p.get("schema_types") for p in pages),
        "has_phone": any(p.get("has_phone") for p in pages),
        "flags": flags,
    }


def score_entity(audit, ai_mentioned):
    site = audit.get("site", {})
    robots = site.get("robots_txt", {})
    bots = robots.get("ai_bots", {})
    citation_ok = all(bots.get(b) != "blocked" for b in CITATION_BOTS)
    a = agg_pages(audit) or {}
    f = a.get("flags", {})

    technical = sum(
        [
            bool(robots.get("sitemap_declared") or site.get("sitemap_xml", {}).get("exists")),
            citation_ok,
            bool(a.get("all_titled")),
            bool(a.get("all_desc")),
            bool(a.get("canonical") and a.get("https") and a.get("viewport")),
        ]
    )
    content = sum(
        [
            a.get("avg_words", 0) >= 400,
            a.get("avg_words", 0) >= 800,
            bool(a.get("all_h1") and a.get("multi_h2")),
            bool(f.get("FAQPage")),
            a.get("pages_n", 0) >= 3,
        ]
    )
    local = min(
        5,
        sum(
            [
                2 if f.get("LocalBusiness") else 0,
                1 if a.get("has_phone") else 0,
                1 if (f.get("AggregateRating") or f.get("Review")) else 0,
                1 if f.get("Organization") else 0,
            ]
        ),
    )
    agentic = sum(
        [
            bool(a.get("any_jsonld")),
            bool(f.get("FAQPage")),
            citation_ok,
            bool(site.get("llms_txt", {}).get("exists")),
            bool(ai_mentioned),
        ]
    )
    return {
        "technical": technical,
        "content": content,
        "local": local,
        "agentic": agentic,
        "total": technical + content + local + agentic,
        "_agg": a,
        "_citation_ok": citation_ok,
        "_bots": bots,
        "_llms": site.get("llms_txt", {}).get("exists"),
    }


def places_cells(rec):
    """(rating, reviews, listing) display cells for one entity's places record.
    Counts render ONLY for a validated match; every other state says what it is,
    and a not-found is explicitly marked as unproven absence (2026-07-15 lesson)."""
    if not rec:
        return "—", "—", "no lookup recorded"
    status = rec.get("status") or ("matched" if rec.get("matched_name") else "not_found_via_api")
    if status == "matched":
        listing = rec.get("matched_name") or "?"
        if rec.get("source") == "dataforseo_business_data":
            listing += " (via DataForSEO; invisible to Places Text Search)"
        return rec.get("rating", "—"), rec.get("review_count", "—"), listing
    if status == "mismatch":
        return "withheld", "withheld", "MISMATCH: API returned only other businesses"
    if status == "error":
        return "not measured", "not measured", f"lookup error: {rec.get('error', '?')}"
    return (
        "not measured",
        "not measured",
        "not found via API — NOT proof of absence; hand-check Google Maps",
    )


def not_measured_notes(*, ai, cwv_rows, places, unproven, backlinks, serp, maps):
    """Limitations bullets derived mechanically from which inputs exist, so the
    report itself says what was NOT measured instead of relying on the narrator."""
    notes = []
    if not ai:
        notes.append(
            "**AI-visibility: NOT measured** (no `.tmp/ai_visibility.json` — Perplexity "
            "step skipped; likely no `PERPLEXITY_API_KEY`). No claim about AI-engine "
            "mentions can be made from this report."
        )
    if not cwv_rows:
        notes.append("**Core Web Vitals: NOT measured** (no usable PageSpeed output).")
    if places is None:
        notes.append("**Verified Google reviews: NOT measured** (no Places output).")
    if unproven:
        notes.append(
            f"**GBP existence is NOT disproven for: {', '.join(unproven)}.** A Places API "
            "no-match/mismatch means the API couldn't return the listing, not that no "
            "profile exists — hand-check Google Maps before any 'no GBP' claim."
        )
    if not backlinks:
        notes.append("**Backlinks/authority: NOT measured** (no DataForSEO backlinks output).")
    if not serp:
        notes.append("**Organic SERP rank: NOT measured** (no DataForSEO SERP output).")
    if not maps:
        notes.append(
            "**Local-pack rank: NOT measured** — the organic table alone understates local "
            "visibility; do not claim a business 'can't be found' without the pack measured "
            "(`dataforseo.py maps`)."
        )
    return notes


def main():
    ap = argparse.ArgumentParser(description="Score the audit data and assemble the report.")
    ap.add_argument("config")
    ap.add_argument("--audit-dir", default=".tmp/audit")
    ap.add_argument("--ai", default=".tmp/ai_visibility.json")
    ap.add_argument("--places", default=".tmp/places/reviews.json")
    ap.add_argument("--backlinks", default=".tmp/dataforseo/backlinks.json")
    ap.add_argument("--serp", default=".tmp/dataforseo/serp.json")
    ap.add_argument("--maps", default=".tmp/dataforseo/maps.json")
    ap.add_argument("--pagespeed-dir", default=".tmp/pagespeed")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    ai = load(args.ai)
    ai_board = (ai or {}).get("scoreboard", {})
    places_file = load(args.places)
    places = (places_file or {}).get("reviews", {})
    backlinks = load(args.backlinks) or {}
    serp = load(args.serp) or {}
    maps = load(args.maps) or {}
    ent_domain = {
        e["name"]: e.get("domain", "").lower().removeprefix("www.")
        for e in [cfg["brand"]] + cfg.get("competitors", [])
    }

    ents = [cfg["brand"]] + cfg.get("competitors", [])
    rows = []
    for e in ents:
        audit = find_audit(e.get("domain", ""), args.audit_dir)
        if not audit:
            rows.append({"name": e["name"], "missing": True})
            continue
        mentioned = ai_board.get(e["name"], {}).get("mentioned", 0) > 0
        sc = score_entity(audit, mentioned)
        sc["name"] = e["name"]
        rows.append(sc)

    ranked = sorted(
        [r for r in rows if not r.get("missing")], key=lambda r: r["total"], reverse=True
    )

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L = []
    L.append(
        f"# Competitive SEO + Agentic-Search Benchmark — {cfg.get('market', 'Untitled market')}\n"
    )
    ai_note = (
        "AI-visibility reflects live Perplexity API queries. "
        if ai
        else "AI-visibility was NOT measured in this run. "
    )
    L.append(
        f"*Generated {stamp} by the WAT seo-analysis skill. Technical signals were obtained by "
        f"fetching the live sites (raw HTML, robots.txt, sitemap.xml, llms.txt). "
        f"{ai_note}See Limitations.*\n"
    )

    L.append("## Scoreboard (0–5 per dimension, /20 total)\n")
    L.append("| Rank | Site | Technical | Content | Local | Agentic | **Total** |")
    L.append("|---|---|:--:|:--:|:--:|:--:|:--:|")
    for i, r in enumerate(ranked, 1):
        L.append(
            f"| {i} | {r['name']} | {r['technical']} | {r['content']} | {r['local']} | "
            f"{r['agentic']} | **{r['total']}** |"
        )
    for r in [r for r in rows if r.get("missing")]:
        L.append(f"| – | {r['name']} | — | — | — | — | *no audit data* |")
    L.append("")

    L.append("## Key technical signals\n")
    L.append(
        "| Site | Pages | Avg words | JSON-LD types | FAQPage | LocalBusiness | Citation bots OK | llms.txt |"
    )
    L.append("|---|--:|--:|---|:--:|:--:|:--:|:--:|")
    for r in ranked:
        a = r["_agg"]
        f = a.get("flags", {})
        L.append(
            f"| {r['name']} | {a.get('pages_n', '?')} | {a.get('avg_words', '?')} | "
            f"{'yes' if a.get('any_jsonld') else 'NONE'} | {'yes' if f.get('FAQPage') else 'no'} | "
            f"{'yes' if f.get('LocalBusiness') else 'no'} | {'yes' if r['_citation_ok'] else 'BLOCKED'} | "
            f"{'yes' if r['_llms'] else 'no'} |"
        )
    L.append("")

    if ai_board:
        L.append("## Live AI-visibility scoreboard (Perplexity)\n")
        L.append(
            f"Model: `{ai.get('model')}` · {ai.get('runs_per_query')} run(s)/query · "
            f"mentioned / cited / asked\n"
        )
        L.append("| Site | Mentioned | Cited | Asked |")
        L.append("|---|--:|--:|--:|")
        for name, s in ai_board.items():
            L.append(f"| {name} | {s['mentioned']} | {s['cited']} | {s['asked']} |")
        L.append("")

    if places_file is not None:
        L.append("## Verified Google reviews (Places API)\n")
        L.append("| Site | Rating | Reviews | Matched listing |")
        L.append("|---|--:|--:|---|")
        for name in ent_domain:
            rating, count, listing = places_cells(places.get(name))
            L.append(f"| {name} | {rating} | {count} | {listing} |")
        L.append("")

    if backlinks:
        L.append("## Authority & backlinks (DataForSEO)\n")
        L.append("| Site | Domain rank (0–1000) | Backlinks | Referring domains |")
        L.append("|---|--:|--:|--:|")
        for name, dom in ent_domain.items():
            b = next((v for k, v in backlinks.items() if domain_matches(k, dom)), None)
            if not b or b.get("error"):
                continue
            L.append(
                f"| {name} | {b.get('rank', '—')} | {b.get('backlinks', '—')} | "
                f"{b.get('referring_domains', '—')} |"
            )
        L.append("")

    if serp.get("keywords"):
        L.append("## Organic SERP rank (DataForSEO)\n")
        L.append(
            f"Location: {serp.get('location', '?')} · `#n` = organic position, `—` = not in top 20\n"
        )
        names = list(ent_domain)
        L.append("| Keyword | " + " | ".join(names) + " |")
        L.append("|---|" + "|".join(["--:"] * len(names)) + "|")
        for kw, pos in serp["keywords"].items():
            if isinstance(pos, dict) and "error" in pos:
                continue
            cells = [("#" + str(pos.get(n)) if pos.get(n) else "—") for n in names]
            L.append(f"| {kw} | " + " | ".join(cells) + " |")
        L.append("")

    if maps.get("keywords"):
        L.append("## Local-pack rank (DataForSEO Google Maps)\n")
        L.append(
            f"Location: {maps.get('location', '?')} · `#n` = position among Maps/pack results, "
            f"`—` = not in top 20. A separate surface from organic — for local businesses, "
            f"usually the one that matters more.\n"
        )
        names = list(ent_domain)
        L.append("| Keyword | " + " | ".join(names) + " | Pack #1 |")
        L.append("|---|" + "|".join(["--:"] * len(names)) + "|---|")
        for kw, data in maps["keywords"].items():
            if "error" in data:
                continue
            pos = data.get("positions", {})
            leaders = data.get("leaders") or []
            lead = f"{leaders[0]['title']} ({leaders[0]['reviews']} reviews)" if leaders else "—"
            cells = [("#" + str(pos.get(n)) if pos.get(n) else "—") for n in names]
            L.append(f"| {kw} | " + " | ".join(cells) + f" | {lead} |")
        L.append("")

    # Core Web Vitals (PageSpeed Insights) — enrichment, not scored.
    cwv_rows = []
    for name, dom in ent_domain.items():
        ps = find_pagespeed(dom, args.pagespeed_dir)
        if ps:
            cwv_rows.append((name, ps))
    if cwv_rows:
        L.append("## Core Web Vitals (PageSpeed Insights)\n")
        strat = cwv_rows[0][1].get("strategy", "?")
        L.append(
            f"Strategy: `{strat}` · homepage · lab Lighthouse score + key metrics "
            f"(field CrUX data shown when Google has it)\n"
        )
        L.append("| Site | Perf score | LCP | CLS | TBT |")
        L.append("|---|--:|--:|--:|--:|")
        for name, ps in cwv_rows:
            lab = ps.get("lab", {})

            def disp(k, lab=lab):
                return (lab.get(k) or {}).get("display") or "—"

            L.append(
                f"| {name} | {ps.get('performance_score', '—')} | "
                f"{disp('LCP')} | {disp('CLS')} | {disp('TBT')} |"
            )
        L.append("")

    # Market-wide gaps: signals absent from EVERY audited entity.
    present = ranked
    gaps = []
    if present and all(not r["_agg"].get("flags", {}).get("FAQPage") for r in present):
        gaps.append(
            "No site has **FAQPage** schema — open lane for AI-Overview/Perplexity citation."
        )
    if present and all(not r["_llms"] for r in present):
        gaps.append("No site publishes **llms.txt**.")
    if present and all(not r["_agg"].get("any_jsonld") for r in present):
        gaps.append("No site has **any JSON-LD structured data**.")
    if present and all(not r["_agg"].get("flags", {}).get("AggregateRating") for r in present):
        gaps.append("No site exposes **Review/AggregateRating** schema.")
    if gaps:
        L.append("## Market-wide gaps (open lanes nobody owns)\n")
        L.extend(f"- {g}" for g in gaps)
        L.append("")

    L.append("## Limitations\n")
    L.append(
        "- Scores are a transparent rubric (see `workflows/competitive_seo_audit.md`), not Google's "
        "algorithm. Use them comparatively, not as absolute SEO health."
    )
    L.append(
        "- Technical/schema signals are from the audited pages only — a missing flag means *not found on "
        "the pages crawled*, not proven absent site-wide. Increase page coverage in the config to widen it."
    )
    if ai:
        L.append(
            "- AI-visibility reflects the Perplexity API at generation time and is **volatile** — re-run and "
            "trend it; it does not represent ChatGPT/Gemini/AI-Overviews, which need their own engine checks."
        )
    L.append(
        "- Core Web Vitals (when gathered) come from PageSpeed Insights and are shown as enrichment only — "
        "they do not affect the /20 rubric. Backlink/authority and SERP figures are DataForSEO snapshots."
    )
    unproven = [
        name
        for name in ent_domain
        if (places.get(name) or {}).get("status") in ("not_found_via_api", "mismatch")
    ]
    L.extend(
        f"- {n}"
        for n in not_measured_notes(
            ai=ai,
            cwv_rows=cwv_rows,
            places=places_file,
            unproven=unproven,
            backlinks=backlinks,
            serp=serp.get("keywords"),
            maps=maps.get("keywords"),
        )
    )
    L.append("")
    L.append("## Agent narrative\n")
    L.append(
        "> _Agent: per the workflow, add the interpretive leaderboard here — for each site, 2–3 sentences "
        "on what it does well and where it falls short, what the leader does that others don't, and the "
        "single highest-impact fix per site. Ground every claim in the tables above._\n"
    )

    out = Path(args.out) if args.out else Path("output") / f"{Path(args.config).stem}-report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")
    print(
        f"Wrote {out}  ({len(ranked)} scored, {sum(1 for r in rows if r.get('missing'))} missing)"
    )
    for r in ranked:
        print(f"  {r['total']:>2}/20  {r['name']}")


if __name__ == "__main__":
    main()
