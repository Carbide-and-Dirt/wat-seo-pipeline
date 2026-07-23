#!/usr/bin/env python3
"""
scrape_contacts.py — best-effort owner/contact extraction from company websites.

For each company in a discovery JSON, fetches the homepage plus likely
about/contact pages and extracts: email addresses (mailto + text), phone
numbers, and OWNER-NAME hints (JSON-LD Person/founder fields, plus text near
'owner/founder/president/established by/family owned'). Per the skill's rule it
never fabricates — anything not found on the public site is reported empty so
the report can mark it "not found."

Usage:
    python tools/scrape_contacts.py .tmp/discover/<slug>.json
    python tools/scrape_contacts.py .tmp/discover/<slug>.json --out .tmp/contacts/contacts.json

Output: .tmp/contacts/contacts.json  (per company: emails, phones, owner_hints, owner_name_guess, pages_checked)
"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from lib.common import utf8_stdout

utf8_stdout()

# Browser UA + headers (a bot UA gets WAF-blocked → missed contacts on live sites).
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 20
CANDIDATE_PATHS = [
    "",
    "contact",
    "contact-us",
    "contact.html",
    "about",
    "about-us",
    "about.html",
    "our-team",
    "team",
]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
OWNER_KW = re.compile(
    r"(owner|founder|president|established by|family[- ]owned|started by|proprietor|principal)",
    re.I,
)
JUNK_EMAIL = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|webp)$|sentry|wixpress|example\.|@2x|@sentry", re.I
)


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        return None
    return None


def emails_from(html, soup):
    out = set()
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            addr = a["href"][7:].split("?")[0].strip()
            if addr and not JUNK_EMAIL.search(addr):
                out.add(addr.lower())
    for m in EMAIL_RE.findall(html):
        if not JUNK_EMAIL.search(m):
            out.add(m.lower())
    return out


def owner_from_jsonld(soup):
    """Pull Person names, and any founder/owner fields, from JSON-LD blocks."""
    names, founders = set(), set()

    def walk(d):
        if isinstance(d, dict):
            t = d.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Person" in types and d.get("name"):
                names.add(str(d["name"]).strip())
            for key in ("founder", "owner", "employee"):
                v = d.get(key)
                if isinstance(v, dict) and v.get("name"):
                    founders.add(str(v["name"]).strip())
                elif isinstance(v, list):
                    for x in v:
                        if isinstance(x, dict) and x.get("name"):
                            founders.add(str(x["name"]).strip())
            for v in d.values():
                walk(v)
        elif isinstance(d, list):
            for v in d:
                walk(v)

    for s in soup.find_all("script", attrs={"type": re.compile("application/ld\\+json", re.I)}):
        raw = s.string or s.get_text()
        if not raw:
            continue
        try:
            walk(json.loads(raw))
        except Exception:
            continue
    return names, founders


def owner_hints_from_text(soup):
    """Sentences/snippets mentioning owner/founder keywords (for manual read)."""
    hints = []
    text = soup.get_text(" ", strip=True)
    for m in OWNER_KW.finditer(text):
        start = max(0, m.start() - 70)
        end = min(len(text), m.end() + 90)
        snip = text[start:end].strip()
        if snip not in hints:
            hints.append(snip)
        if len(hints) >= 5:
            break
    return hints


def scrape_company(c):
    site = c.get("website")
    rec = {
        "name": c["name"],
        "website": site,
        "emails": [],
        "phones": [],
        "owner_names_schema": [],
        "founder_schema": [],
        "owner_hints": [],
        "pages_checked": [],
    }
    if not site:
        return rec
    origin = f"{urlparse(site).scheme}://{urlparse(site).netloc}"
    emails, phones, names, founders, hints = set(), set(), set(), set(), []
    for path in CANDIDATE_PATHS:
        url = site if path == "" else urljoin(origin + "/", path)
        html = fetch(url)
        if not html:
            continue
        rec["pages_checked"].append(url)
        soup = BeautifulSoup(html, "html.parser")
        emails |= emails_from(html, soup)
        for m in PHONE_RE.findall(html):
            phones.add(re.sub(r"\s+", " ", m.strip()))
        n, f = owner_from_jsonld(soup)
        names |= n
        founders |= f
        for h in owner_hints_from_text(soup):
            if h not in hints:
                hints.append(h)
    rec["emails"] = sorted(emails)
    rec["phones"] = sorted(phones)[:6]
    rec["owner_names_schema"] = sorted(names)
    rec["founder_schema"] = sorted(founders)
    rec["owner_hints"] = hints[:6]
    flag = ""
    if not emails and not founders and not names:
        flag = "  (no email/owner found)"
    print(
        f"  {c['name']}: {len(emails)} email(s), owner-schema={sorted(names) or '—'}, founder={sorted(founders) or '—'}{flag}"
    )
    return rec


def main():
    ap = argparse.ArgumentParser(description="Best-effort owner/contact scrape from company sites.")
    ap.add_argument("discover", help="Path to discovery JSON with a 'companies' list.")
    ap.add_argument("--out", default=".tmp/contacts/contacts.json")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    data = json.loads(Path(args.discover).read_text(encoding="utf-8"))
    companies = [c for c in data.get("companies", []) if c.get("website")]
    results = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(scrape_company, c): c for c in companies}
        for fut in as_completed(futs):
            try:
                rec = fut.result()
                results[rec["name"]] = rec
            except Exception as e:  # noqa: BLE001
                c = futs[fut]
                print(f"  ! {c['name']} -> {e}", file=sys.stderr)
                results[c["name"]] = {"name": c["name"], "error": str(e)}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"contacts": results}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n-> {out}  ({len(results)} companies)")


if __name__ == "__main__":
    sys.exit(main())
