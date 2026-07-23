#!/usr/bin/env python3
"""
audit_site.py — deterministic on-page + technical SEO auditor.

Closes the biggest gap from the manual analysis: WebFetch only returned the
*rendered body*, so <head>-level JSON-LD / canonical / OG could never be
byte-confirmed. This fetches the RAW HTML source (requests) so structured
data is read directly, with an optional --render pass (Playwright) for sites
that inject schema via JavaScript.

Usage:
    python tools/audit_site.py https://example.com [https://example.com/pricing ...]
    python tools/audit_site.py https://example.com --render        # also parse JS-rendered DOM
    python tools/audit_site.py https://example.com --out .tmp/audit # output dir (default)
    python tools/audit_site.py https://example.com --firecrawl always  # route every fetch through Firecrawl

Firecrawl fallback (optional): set FIRECRAWL_API_KEY in .env and a plain-requests
fetch that gets WAF-blocked (403/202/5xx) or returns a JS-only shell is retried
through Firecrawl's proxy/render — converting "couldn't audit" rows into scored
ones. It requests rawHtml so the JSON-LD this tool parses survives. No key set =>
no-op; --firecrawl off disables it; --firecrawl always forces it on every page.
Each page records which fetcher served it under "fetched_via".

Output: one JSON file per host at <out>/<host>.json, plus a short stdout summary.
Each page audited is keyed by its path under "pages"; site-level signals
(robots.txt / sitemap.xml / llms.txt) are fetched once per host.
"""

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


from lib.common import load_env, utf8_stdout

# Windows consoles default to cp1252 and crash on non-ASCII; emit UTF-8 safely.
utf8_stdout()

# A realistic browser UA + headers. A bot-style UA gets WAF-blocked (Sucuri/GoDaddy/
# Cloudflare/WPEngine return 400/403/520 to crawlers), which made live sites look dead.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 25
AI_BOTS = [
    "GPTBot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "PerplexityBot",
    "ClaudeBot",
    "Claude-Web",
    "Google-Extended",
    "CCBot",
    "Bingbot",
    "Applebot-Extended",
]

# Statuses that mean "a WAF/CDN stonewalled the crawler" — the site is probably
# live, we just couldn't read it with plain requests. These are worth escalating
# to Firecrawl. NOTE: 404/410 are deliberately NOT here — those are genuinely
# dead sites (real leads), not blocks, so we never waste a credit reviving one.
BLOCKED_STATUSES = {202, 400, 403, 406, 408, 409, 429, 500, 502, 503, 520, 521, 522, 523, 524}
FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v1/scrape"


def _firecrawl_key():
    return os.environ.get("FIRECRAWL_API_KEY", "").strip()


def _looks_blocked(status, html):
    """True when plain requests failed or was stonewalled by a WAF/CDN."""
    if status is None:
        return True
    if status in BLOCKED_STATUSES:
        return True
    # A 200 with almost no markup is usually a JS-only shell (SPA) whose real
    # content — including JSON-LD — is injected client-side. Worth rendering.
    if status == 200 and len(html or "") < 2000:
        return True
    return False


def _fetch_firecrawl(url):
    """Fetch raw HTML via Firecrawl (proxy rotation + JS render). Returns the
    same 4-tuple as fetch(): (final_url, status, raw_html, error). We request
    rawHtml specifically so the JSON-LD <script> blocks audit_site parses are
    preserved byte-for-byte — markdown output would strip them."""
    key = _firecrawl_key()
    if not key:
        return url, None, "", "no FIRECRAWL_API_KEY"
    try:
        r = requests.post(
            FIRECRAWL_ENDPOINT,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "url": url,
                "formats": ["rawHtml"],
                "onlyMainContent": False,
                "proxy": "auto",
                "timeout": 45000,
            },
            timeout=90,
        )
    except Exception as e:  # noqa: BLE001
        return url, None, "", f"firecrawl {type(e).__name__}: {e}"
    if r.status_code != 200:
        return url, r.status_code, "", f"firecrawl HTTP {r.status_code}: {r.text[:160]}"
    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return url, None, "", f"firecrawl bad JSON: {e}"
    if not data.get("success"):
        return url, None, "", f"firecrawl error: {str(data.get('error'))[:160]}"
    d = data.get("data") or {}
    html = d.get("rawHtml") or d.get("html") or ""
    meta = d.get("metadata") or {}
    final = meta.get("sourceURL") or meta.get("url") or url
    status = meta.get("statusCode") or 200
    if not html:
        return final, status, "", "firecrawl returned no rawHtml"
    return final, status, html, None


def fetch(url, render=False, firecrawl="auto"):
    """Return (final_url, status, raw_html, error, fetched_via).

    Strategy: plain requests first (free, fast). Escalate to Firecrawl only when
    it's worth a credit — `firecrawl="always"` forces it; `"auto"` (default) uses
    it when the key is set AND the plain fetch looks blocked/JS-shelled; `"off"`
    never uses it. `--render` (local Playwright) still takes precedence if set.
    """
    if render:
        try:
            html, final = _fetch_rendered(url)
            return final, 200, html, None, "playwright"
        except Exception as e:  # noqa: BLE001
            return url, None, "", f"{type(e).__name__}: {e}", "playwright"

    status = html = err = None
    final = url
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        final, status, html = r.url, r.status_code, r.text
    except Exception as e:  # noqa: BLE001 - report any fetch failure to the report layer
        err = f"{type(e).__name__}: {e}"

    # Some shared site platforms intermittently serve a branded 404 to bursted/
    # automated requests but 200 to an isolated hit. One cheap retry on 404 keeps
    # a live site from being mislabeled dead (a false "needs a website" lead). A
    # genuinely dead page returns 404 again and is correctly kept as dead.
    if status == 404:
        time.sleep(1.5)
        try:
            r2 = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            if r2.status_code == 200:
                final, status, html, err = r2.url, r2.status_code, r2.text, None
        except Exception:  # noqa: BLE001 - keep the original 404 result
            pass

    # Google Business Profile often lists a DEEP link (e.g. /locations/<city>/) that
    # 404s even though the site is live. If a path/query URL still 404s, fall back
    # to the root domain — that's the homepage we want to score anyway.
    if status == 404:
        p = urlparse(url)
        if p.path not in ("", "/") or p.query:
            try:
                r3 = requests.get(
                    f"{p.scheme}://{p.netloc}/",
                    headers=HEADERS,
                    timeout=TIMEOUT,
                    allow_redirects=True,
                )
                if r3.status_code == 200:
                    final, status, html, err = r3.url, r3.status_code, r3.text, None
            except Exception:  # noqa: BLE001 - keep the original 404 result
                pass

    if firecrawl == "off" or not _firecrawl_key():
        return final, status, html or "", err, "requests"

    if firecrawl == "always" or _looks_blocked(status, html):
        f_final, f_status, f_html, f_err = _fetch_firecrawl(url)
        if f_html and not f_err:
            return f_final, f_status, f_html, None, "firecrawl"
        # Firecrawl didn't help — keep the original result but surface its error
        # if plain requests had nothing useful either.
        if err or not html:
            return final, status, html or "", err or f_err, "requests"

    return final, status, html or "", err, "requests"


def _fetch_rendered(url):
    from playwright.sync_api import sync_playwright  # imported lazily; only needed for --render

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=UA)
        page.goto(url, wait_until="networkidle", timeout=45000)
        html = page.content()
        final = page.url
        browser.close()
        return html, final


def _text(el):
    return el.get_text(" ", strip=True) if el else None


def parse_page(url, status, html):
    soup = BeautifulSoup(html, "html.parser")
    head = soup.head or soup

    def meta(name=None, prop=None):
        if name:
            tag = soup.find("meta", attrs={"name": re.compile(f"^{name}$", re.I)})
        else:
            tag = soup.find("meta", attrs={"property": re.compile(f"^{re.escape(prop)}$", re.I)})
        return tag.get("content", "").strip() if tag and tag.get("content") else None

    title = _text(soup.title)
    desc = meta(name="description")
    canonical_tag = soup.find("link", rel=lambda v: v and "canonical" in v)
    canonical = canonical_tag.get("href") if canonical_tag else None

    # --- structured data ---
    jsonld_blocks, schema_types = [], set()
    for s in soup.find_all("script", attrs={"type": re.compile("application/ld\\+json", re.I)}):
        raw = s.string or s.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
            valid = True
        except Exception:
            jsonld_blocks.append({"valid": False, "types": [], "note": "JSON parse error"})
            continue
        types = _collect_types(data)
        schema_types.update(types)
        jsonld_blocks.append({"valid": valid, "types": sorted(types)})

    og = {
        k: meta(prop=f"og:{k}")
        for k in ("title", "description", "image", "type", "url", "site_name")
    }
    twitter = meta(name="twitter:card")

    h1 = [_text(h) for h in soup.find_all("h1")]
    body_text = soup.get_text(" ", strip=True)
    word_count = len(body_text.split())

    host = urlparse(url).netloc
    links = soup.find_all("a", href=True)
    internal = sum(1 for a in links if _same_host(a["href"], url, host))
    external = sum(
        1 for a in links if a["href"].startswith("http") and not _same_host(a["href"], url, host)
    )
    imgs = soup.find_all("img")
    missing_alt = sum(1 for im in imgs if not im.get("alt"))

    return {
        "url": url,
        "status": status,
        "https": url.lower().startswith("https://"),
        "title": title,
        "title_length": len(title) if title else 0,
        "meta_description": desc,
        "meta_description_length": len(desc) if desc else 0,
        "meta_robots": meta(name="robots"),
        "canonical": canonical,
        "html_lang": (soup.html.get("lang") if soup.html else None),
        "viewport": bool(head.find("meta", attrs={"name": re.compile("^viewport$", re.I)})),
        "open_graph": {k: v for k, v in og.items() if v},
        "twitter_card": twitter,
        "jsonld_blocks": jsonld_blocks,
        "schema_types": sorted(schema_types),
        "schema_flags": {
            "LocalBusiness": _has(
                schema_types,
                "LocalBusiness",
                "Excavating",
                "SportsActivityLocation",
                "HealthClub",
                "Organization",
            ),
            "FAQPage": "FAQPage" in schema_types,
            "Organization": "Organization" in schema_types,
            "BreadcrumbList": "BreadcrumbList" in schema_types,
            "Event": "Event" in schema_types,
            "AggregateRating": "AggregateRating" in schema_types,
            "Review": "Review" in schema_types,
        },
        "h1": h1,
        "h1_count": len(h1),
        "h2_count": len(soup.find_all("h2")),
        "h3_count": len(soup.find_all("h3")),
        "word_count": word_count,
        "internal_links": internal,
        "external_links": external,
        "images_total": len(imgs),
        "images_missing_alt": missing_alt,
        "has_phone": bool(re.search(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", body_text)),
    }


def _collect_types(data, acc=None):
    acc = acc if acc is not None else set()
    if isinstance(data, dict):
        t = data.get("@type")
        if isinstance(t, str):
            acc.add(t)
        elif isinstance(t, list):
            acc.update(x for x in t if isinstance(x, str))
        for v in data.values():
            _collect_types(v, acc)
    elif isinstance(data, list):
        for v in data:
            _collect_types(v, acc)
    return acc


def _has(types, *needles):
    return any(any(n.lower() in t.lower() for t in types) for n in needles)


def _same_host(href, base, host):
    try:
        full = urljoin(base, href)
        return urlparse(full).netloc == host
    except Exception:
        return False


def audit_site_level(origin):
    """Fetch robots.txt / sitemap.xml / llms.txt once per host."""
    out = {}
    # robots.txt
    robots = {"exists": False, "sitemap_declared": False, "crawl_delay": None, "ai_bots": {}}
    r_url = urljoin(origin, "/robots.txt")
    try:
        r = requests.get(r_url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200 and "<html" not in r.text.lower()[:200]:
            robots["exists"] = True
            robots["sitemap_declared"] = bool(re.search(r"(?im)^\s*sitemap\s*:", r.text))
            cd = re.search(r"(?im)^\s*crawl-delay\s*:\s*(\d+)", r.text)
            robots["crawl_delay"] = int(cd.group(1)) if cd else None
            robots["ai_bots"] = _robots_bot_status(r.text)
    except Exception as e:
        robots["error"] = str(e)
    out["robots_txt"] = robots

    # sitemap.xml
    sm = {"exists": False, "url_count": None}
    try:
        s = requests.get(urljoin(origin, "/sitemap.xml"), headers=HEADERS, timeout=TIMEOUT)
        if s.status_code == 200 and ("<urlset" in s.text or "<sitemapindex" in s.text):
            sm["exists"] = True
            sm["url_count"] = s.text.count("<loc>")
    except Exception as e:
        sm["error"] = str(e)
    out["sitemap_xml"] = sm

    # llms.txt
    llms = {"exists": False}
    try:
        resp = requests.get(urljoin(origin, "/llms.txt"), headers=HEADERS, timeout=TIMEOUT)
        llms["exists"] = resp.status_code == 200 and "<html" not in resp.text.lower()[:200]
    except Exception as e:
        llms["error"] = str(e)
    out["llms_txt"] = llms
    return out


def _robots_bot_status(text):
    """Return {bot: 'allowed'|'blocked'|'unspecified'} from robots.txt user-agent blocks.

    Groups consecutive `User-agent:` lines that share one rule set (per the robots
    spec): once a rule appears, the next `User-agent:` line starts a NEW group.
    A bot is 'blocked' if its group (or, if it has none, the `*` group) carries a
    site-wide `Disallow: /`, OR if a `Content-Signal: ai-train=no` directive applies
    to it (Cloudflare's edge-injected AI opt-out).
    """
    blocks = {}  # agent -> list of (("disallow"|"allow"), path)
    signals = {}  # agent -> raw Content-Signal value
    agents = []  # user-agents the current group applies to
    after_rule = False  # did we just consume a rule line for this group?
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.match(r"(?i)^user-agent\s*:\s*(.+)$", line)
        if m:
            if after_rule:  # a rule already closed the previous group -> start fresh
                agents = []
                after_rule = False
            agents.append(m.group(1).strip())
            blocks.setdefault(m.group(1).strip(), [])
            continue
        m = re.match(r"(?i)^(dis)?allow\s*:\s*(.*)$", line)
        if m:
            after_rule = True
            for a in agents:
                blocks[a].append(("disallow" if m.group(1) else "allow", m.group(2).strip()))
            continue
        m = re.match(r"(?i)^content-signal\s*:\s*(.+)$", line)
        if m:
            after_rule = True
            for a in agents:
                signals[a] = m.group(1).strip().lower()
            continue
        # Any other directive (e.g. Sitemap:) doesn't belong to a group; ignore it
        # but treat it like a rule boundary so a following User-agent starts fresh.
        after_rule = True

    def ai_train_off(sig):
        return bool(sig) and re.search(r"ai-train\s*=\s*no", sig) is not None

    def status(bot):
        rules = blocks.get(bot)
        sig = signals.get(bot)
        if rules is None:  # no explicit group for this bot -> fall back to *
            rules = blocks.get("*")
            sig = signals.get("*") if sig is None else sig
            if rules is None and sig is None:
                return "unspecified"
            rules = rules or []
        if any(d == "disallow" and p == "/" for d, p in rules):
            return "blocked"
        if ai_train_off(sig):
            return "blocked"
        return "allowed"

    return {bot: status(bot) for bot in AI_BOTS}


def _audit_one_page(url, render, firecrawl="auto"):
    final_url, status, html, err, via = fetch(url, render=render, firecrawl=firecrawl)
    if err or not html:
        print(f"  ! {url} -> {err or 'empty'} (via {via})")
        return url, {
            "url": url,
            "error": err or "empty response",
            "status": status,
            "fetched_via": via,
        }
    page = parse_page(final_url, status, html)
    page["fetched_via"] = via
    st = page["schema_types"] or ["(none)"]
    tag = "" if via == "requests" else f" via {via}"
    print(
        f"  OK {url} [{status}{tag}] title={page['title_length']}c desc={page['meta_description_length']}c "
        f"schema={','.join(st)} words={page['word_count']}"
    )
    return url, page


def main():
    ap = argparse.ArgumentParser(description="Deterministic on-page + technical SEO auditor.")
    ap.add_argument("urls", nargs="+", help="One or more page URLs to audit.")
    ap.add_argument(
        "--render", action="store_true", help="Use Playwright to parse the JS-rendered DOM too."
    )
    ap.add_argument(
        "--firecrawl",
        choices=("auto", "always", "off"),
        default="auto",
        help="Firecrawl fallback fetcher (needs FIRECRAWL_API_KEY): 'auto' "
        "(default) escalates only when plain requests is blocked or JS-shelled; "
        "'always' routes every page through it; 'off' disables it. No-op without the key.",
    )
    ap.add_argument("--out", default=".tmp/audit", help="Output directory (default .tmp/audit).")
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip any host whose <out>/<host>.json already exists (don't refetch).",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Concurrent page fetches (default 8; forced to 1 with --render).",
    )
    args = ap.parse_args()

    load_env()  # so FIRECRAWL_API_KEY is available before any fetch

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Group URLs by host so site-level signals are fetched once per host.
    by_host = {}
    for url in args.urls:
        host = urlparse(url).netloc
        origin = f"{urlparse(url).scheme}://{host}"
        rec = by_host.setdefault(
            host, {"host": host, "origin": origin, "pages": {}, "site": None, "urls": []}
        )
        rec["urls"].append(url)

    # --skip-existing drops whole hosts whose output file already exists.
    pending = {}
    for host, rec in by_host.items():
        if args.skip_existing and (out_dir / f"{host}.json").exists():
            print(f"  skip {host} (exists)")
            continue
        pending[host] = rec
    if not pending:
        print("Nothing to audit (all hosts skipped).")
        return

    # Site-level fetches (robots/sitemap/llms), one per host, concurrently.
    workers = 1 if args.render else max(1, args.workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        site_futs = {
            ex.submit(audit_site_level, rec["origin"]): host for host, rec in pending.items()
        }
        for fut in as_completed(site_futs):
            pending[site_futs[fut]]["site"] = fut.result()

    # Page fetches across all pending hosts, concurrently (Playwright is not
    # thread-safe, so --render falls back to a single worker).
    all_urls = [(host, u) for host, rec in pending.items() for u in rec["urls"]]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        page_futs = {
            ex.submit(_audit_one_page, u, args.render, args.firecrawl): host for host, u in all_urls
        }
        for fut in as_completed(page_futs):
            host = page_futs[fut]
            url, page = fut.result()
            pending[host]["pages"][url] = page

    for host, rec in pending.items():
        rec.pop("urls", None)
        dest = out_dir / f"{host}.json"
        dest.write_text(json.dumps(rec, indent=2, ensure_ascii=False), encoding="utf-8")
        sl = rec["site"]
        blocked = [b for b, s in sl["robots_txt"]["ai_bots"].items() if s == "blocked"]
        print(
            f"\n{host}: sitemap_declared={sl['robots_txt']['sitemap_declared']} "
            f"crawl_delay={sl['robots_txt']['crawl_delay']} llms.txt={sl['llms_txt']['exists']} "
            f"AI-bots-blocked={blocked or 'none'} -> {dest}"
        )


if __name__ == "__main__":
    sys.exit(main())
