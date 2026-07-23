#!/usr/bin/env python3
"""
pagespeed.py — Core Web Vitals + performance, via Google PageSpeed Insights.

Closes the "couldn't run Lighthouse" gap. PSI runs Lighthouse server-side and
also returns real-user CrUX field data when available. No headless browser
needed — it's a single HTTPS GET. An API key (PAGESPEED_API_KEY in .env) is
optional but raises the rate limit; without one you get a few calls/min.

Usage:
    python tools/pagespeed.py https://example.com [https://example.com/pricing ...]
    python tools/pagespeed.py https://example.com --strategy desktop

Output: .tmp/pagespeed/<host>.json  (per-URL lab score + key metrics + field data)
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


from lib.common import load_env


def run(url, strategy, key):
    params = {"url": url, "strategy": strategy, "category": "performance"}
    if key:
        params["key"] = key
    r = requests.get(API, params=params, timeout=90)
    r.raise_for_status()
    d = r.json()
    lh = d.get("lighthouseResult", {})
    audits = lh.get("audits", {})

    def metric(aid):
        a = audits.get(aid, {})
        return {"display": a.get("displayValue"), "score": a.get("score")}

    perf = lh.get("categories", {}).get("performance", {}).get("score")
    field = {}
    loading = d.get("loadingExperience", {}).get("metrics", {})
    for k, v in loading.items():
        field[k] = {"percentile": v.get("percentile"), "category": v.get("category")}

    return {
        "url": url,
        "strategy": strategy,
        "performance_score": round(perf * 100) if isinstance(perf, (int, float)) else None,
        "lab": {
            "LCP": metric("largest-contentful-paint"),
            "CLS": metric("cumulative-layout-shift"),
            "TBT": metric("total-blocking-time"),
            "FCP": metric("first-contentful-paint"),
            "SpeedIndex": metric("speed-index"),
        },
        "field_crux": field or None,
    }


def main():
    ap = argparse.ArgumentParser(description="Core Web Vitals via PageSpeed Insights.")
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--strategy", default="mobile", choices=["mobile", "desktop"])
    ap.add_argument("--out", default=".tmp/pagespeed")
    args = ap.parse_args()

    load_env()
    key = os.environ.get("PAGESPEED_API_KEY")
    if not key:
        print(
            "note: no PAGESPEED_API_KEY set — using anonymous quota (a few calls/min).",
            file=sys.stderr,
        )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    by_host = {}
    for url in args.urls:
        host = urlparse(url).netloc
        try:
            rec = run(url, args.strategy, key)
            by_host.setdefault(host, []).append(rec)
            print(
                f"  OK {url} [{args.strategy}] perf={rec['performance_score']} "
                f"LCP={rec['lab']['LCP']['display']} CLS={rec['lab']['CLS']['display']}"
            )
        except Exception as e:  # noqa: BLE001
            print(f"  ! {url} -> {e}", file=sys.stderr)
            by_host.setdefault(host, []).append({"url": url, "error": str(e)})

    for host, recs in by_host.items():
        dest = out_dir / f"{host}.json"
        dest.write_text(json.dumps(recs, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"-> {dest}")


if __name__ == "__main__":
    sys.exit(main())
