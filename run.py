#!/usr/bin/env python3
"""
run.py — one-command orchestrator for the competitive SEO + GEO audit.

Runs the WAT pipeline end-to-end from a single targets config, in order:
  audit_site -> [pagespeed] -> [places] -> [dataforseo backlinks+serp+maps]
             -> ai_visibility -> score_report

Steps whose API key is absent are skipped with a notice (the underlying tool
exits 2 and says which key is missing). Paid DataForSEO steps run only with
--with-paid. After this, you (the agent) still write the "Agent narrative"
section of the report per workflows/competitive_seo_audit.md.

Usage:
  python run.py targets/<market>.json
  python run.py targets/<market>.json --with-pagespeed --skip-existing
  python run.py targets/<market>.json --with-pagespeed --with-paid --runs 2
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
TOOLS = ROOT / "tools"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


def entities(cfg):
    return [cfg["brand"]] + cfg.get("competitors", [])


def page_urls(cfg):
    urls = []
    for entity_pages in cfg.get("pages", {}).values():
        urls.extend(entity_pages)
    return urls


def homepages(cfg):
    """First listed URL per entity — the homepage when configs list it first."""
    return [pages[0] for pages in cfg.get("pages", {}).values() if pages]


def domains(cfg):
    return [e["domain"] for e in entities(cfg) if e.get("domain")]


def run_step(label, argv, *, optional=False):
    print(f"\n{'=' * 70}\n# {label}\n{'=' * 70}")
    rc = subprocess.run([sys.executable, str(TOOLS / argv[0]), *argv[1:]], cwd=ROOT).returncode
    if rc == 2 and optional:
        print(f"  (skipped: {label} — required API key not set)")
    elif rc not in (0, None):
        print(f"  ! {label} exited with code {rc} — continuing.", file=sys.stderr)
    return rc


def main():
    ap = argparse.ArgumentParser(
        description="Run the full SEO + GEO audit pipeline from one config."
    )
    ap.add_argument("config", help="Path to a targets/<market>.json config.")
    ap.add_argument(
        "--with-pagespeed", action="store_true", help="Also run Core Web Vitals on each homepage."
    )
    ap.add_argument(
        "--with-paid", action="store_true", help="Also run paid DataForSEO backlinks + SERP steps."
    )
    ap.add_argument("--runs", type=int, default=2, help="AI-visibility runs per query (default 2).")
    ap.add_argument(
        "--render", action="store_true", help="Pass --render to audit_site (JS-rendered DOM)."
    )
    ap.add_argument(
        "--firecrawl",
        choices=("auto", "always", "off"),
        default="auto",
        help="Firecrawl fallback for audit_site (needs FIRECRAWL_API_KEY): "
        "auto (default) / always / off. No-op without the key.",
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip audit hosts / AI-visibility output that already exist (don't refetch / re-spend).",
    )
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    urls = page_urls(cfg)
    if not urls:
        print("ERROR: config has no 'pages' to audit.", file=sys.stderr)
        return 1

    skip = ["--skip-existing"] if args.skip_existing else []

    # 1. On-page + technical audit
    audit_argv = ["audit_site.py", *urls, *skip, "--firecrawl", args.firecrawl]
    if args.render:
        audit_argv.append("--render")
    run_step("Audit sites (on-page + technical)", audit_argv)

    # 2. Core Web Vitals (optional)
    if args.with_pagespeed:
        hp = homepages(cfg)
        if hp:
            run_step("Core Web Vitals (PageSpeed Insights)", ["pagespeed.py", *hp])

    # 3. Verified Google reviews (optional — needs GOOGLE_PLACES_API_KEY).
    # The DataForSEO fallback lookup costs credits, so it only arms with --with-paid.
    fallback = ["--fallback", "auto" if args.with_paid else "off"]
    run_step(
        "Verified Google reviews (Places API)",
        ["places_reviews.py", args.config, *fallback],
        optional=True,
    )

    # 4. Authority/backlinks + organic SERP + local pack (paid — only with --with-paid)
    if args.with_paid:
        doms = domains(cfg)
        if doms:
            run_step(
                "Backlinks + domain authority (DataForSEO, paid)",
                ["dataforseo.py", "backlinks", *doms],
                optional=True,
            )
        run_step(
            "Organic SERP rank (DataForSEO, paid)",
            ["dataforseo.py", "serp", args.config],
            optional=True,
        )
        run_step(
            "Local-pack rank (DataForSEO Maps, paid)",
            ["dataforseo.py", "maps", args.config],
            optional=True,
        )

    # 5. Live AI-visibility (optional — needs PERPLEXITY_API_KEY)
    run_step(
        "AI-visibility (Perplexity)",
        ["check_ai_visibility.py", args.config, "--runs", str(args.runs), *skip],
        optional=True,
    )

    # 6. Score + assemble the report scaffold
    run_step("Score + assemble report", ["score_report.py", args.config])

    out = ROOT / "output" / f"{Path(args.config).stem}-report.md"
    print(f"\n{'=' * 70}")
    print(f"Pipeline complete. Report scaffold -> {out}")
    print("Next: fill in the 'Agent narrative' section per workflows/competitive_seo_audit.md.")


if __name__ == "__main__":
    sys.exit(main())
