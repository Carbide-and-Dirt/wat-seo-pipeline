#!/usr/bin/env python3
"""
check_ai_visibility.py — measure ACTUAL agentic-search visibility.

The manual reports could only hand over a "self-test prompt pack" because no
tool could query a live AI engine. This calls the Perplexity Sonar API (which
returns the sources it cited) for each market query, then scores whether the
target brand and each competitor were MENTIONED in the answer and/or CITED as a
source. That turns "run these prompts yourself" into a repeatable scoreboard.

Requires PERPLEXITY_API_KEY in .env (https://docs.perplexity.ai/). Perplexity
is used because, unlike most chat APIs, it returns citation URLs — the single
most useful GEO signal. Hooks for OpenAI web-search / Google can be added the
same way (see add_engine note at bottom).

Usage:
    python tools/check_ai_visibility.py targets/<name>.json
    python tools/check_ai_visibility.py targets/<name>.json --model sonar-pro --runs 2

Output: .tmp/ai_visibility.json  (per-query results + an aggregate scoreboard)
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

API_URL = "https://api.perplexity.ai/chat/completions"


from lib.common import load_env


def entities(cfg):
    """Yield (name, aliases[], domain) for brand + each competitor."""
    out = []
    b = cfg["brand"]
    out.append((b["name"], [b["name"], *b.get("aliases", [])], b.get("domain", "")))
    for c in cfg.get("competitors", []):
        out.append((c["name"], [c["name"], *c.get("aliases", [])], c.get("domain", "")))
    return out


def mentioned(text, aliases):
    """True if any alias appears as a whole word/phrase (not a substring of a
    larger word). Avoids false hits like alias 'Crag' matching 'cragsman'."""
    t = text or ""
    for a in aliases:
        if not a:
            continue
        if re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", t, re.I):
            return True
    return False


def cited(citations, domain):
    if not domain:
        return False
    d = domain.lower().removeprefix("www.")
    for c in citations:
        host = urlparse(c).netloc.lower().removeprefix("www.")
        # dot-boundary match: a citation to brand.com or blog.brand.com counts,
        # but myclimb.com must not match domain climb.com.
        if host == d or host.endswith("." + d):
            return True
    return False


def ask_perplexity(query, model, key):
    r = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": model, "messages": [{"role": "user", "content": query}]},
        timeout=90,
    )
    r.raise_for_status()
    data = r.json()
    content = data["choices"][0]["message"]["content"]
    # Perplexity returns sources as "citations" (list of urls) and/or "search_results".
    citations = data.get("citations") or []
    if not citations and isinstance(data.get("search_results"), list):
        citations = [s.get("url", "") for s in data["search_results"]]
    return content, [c for c in citations if c]


def main():
    ap = argparse.ArgumentParser(
        description="Measure live AI-search visibility via the Perplexity API."
    )
    ap.add_argument("config", help="Path to a targets/<name>.json config.")
    ap.add_argument(
        "--model", default="sonar", help="Perplexity model (sonar | sonar-pro | sonar-reasoning)."
    )
    ap.add_argument(
        "--runs", type=int, default=1, help="Repeat each query N times (answers vary by run)."
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Concurrent API calls (default 3; keep low to respect Perplexity rate limits).",
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="If --out already exists, do nothing (avoid re-spending API credits).",
    )
    ap.add_argument("--out", default=".tmp/ai_visibility.json")
    args = ap.parse_args()

    if args.skip_existing and Path(args.out).exists():
        print(f"  skip: {args.out} already exists (use without --skip-existing to re-query).")
        return

    load_env()
    key = os.environ.get("PERPLEXITY_API_KEY")
    if not key:
        print(
            "ERROR: PERPLEXITY_API_KEY not set. Add it to .env "
            "(get one at https://docs.perplexity.ai/). Skipping live AI-visibility.",
            file=sys.stderr,
        )
        return 2

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    ents = entities(cfg)
    queries = cfg.get("ai_queries", [])
    if not queries:
        print("ERROR: config has no 'ai_queries'.", file=sys.stderr)
        return 1

    score = {name: {"mentioned": 0, "cited": 0, "asked": 0} for name, _, _ in ents}

    def do_task(task):
        qi, q, run = task
        content, citations = ask_perplexity(q, args.model, key)
        row = {"query": q, "run": run + 1, "citations": citations, "entities": {}}
        for name, aliases, domain in ents:
            row["entities"][name] = {
                "mentioned": mentioned(content, aliases),
                "cited": cited(citations, domain),
            }
        return qi, run, row

    tasks = [(qi, q, run) for qi, q in enumerate(queries) for run in range(args.runs)]
    collected = []  # (qi, run, row) for stable ordering after concurrent completion
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(do_task, t): t for t in tasks}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                qi, run, row = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"  ! query failed: {t[1][:50]}... -> {e}", file=sys.stderr)
                continue
            for name, _, _ in ents:
                score[name]["asked"] += 1
                score[name]["mentioned"] += int(row["entities"][name]["mentioned"])
                score[name]["cited"] += int(row["entities"][name]["cited"])
            collected.append((qi, run, row))
            hit = (
                ", ".join(n for n, _, _ in ents if row["entities"][n]["mentioned"])
                or "(none of our set)"
            )
            print(f"  OK [{args.model}] {t[1][:48]}... -> mentioned: {hit}")

    results = [row for _, _, row in sorted(collected, key=lambda x: (x[0], x[1]))]

    out = {
        "market": cfg.get("market"),
        "model": args.model,
        "runs_per_query": args.runs,
        "scoreboard": score,
        "results": results,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nScoreboard (mentioned / cited / asked):")
    for name, s in score.items():
        print(f"  {name}: {s['mentioned']} / {s['cited']} / {s['asked']}")
    print(f"-> {args.out}")


# To add another engine: write ask_openai()/ask_google() returning (content, citations)
# and loop them alongside ask_perplexity, tagging each row with the engine name.

if __name__ == "__main__":
    sys.exit(main())
