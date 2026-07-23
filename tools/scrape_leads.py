#!/usr/bin/env python3
"""
scrape_leads.py - best-effort email/owner scrape for leads in the master store.

The free enrichment pass scores each site; this pass pulls the CONTACT details a
public site exposes - email address and owner/founder name - and stores them in
site_contacts (one row per lead, keyed by place_id) so they flow into the prospect
report alongside the Places phone number. Outreach-enablement for the shortlist.

Reuses scrape_contacts.py's extraction (emails / JSON-LD owner / owner-text hints) -
nothing is re-implemented. No paid API calls (NFR-9 discipline): HTTP/time only,
bounded by --workers, resumable (an already-scraped lead is skipped unless --refresh).
Never fabricates - anything not found is left empty.

Usage:
    python tools/scrape_leads.py                              # every un-scraped lead with a website
    python tools/scrape_leads.py --region "TN FL"            # only those states
    python tools/scrape_leads.py --place-ids ChIJ..,ChIJ..   # exactly these leads (e.g. a shortlist)
    python tools/scrape_leads.py --limit 200                 # most-reviewed 200 first
    python tools/scrape_leads.py --summary                   # coverage only, no fetching
"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import leads_db
import scrape_contacts as sc  # reuse fetch / emails_from / owner_from_jsonld / owner_hints / PHONE_RE

from lib.common import utf8_stdout

utf8_stdout()

# A focused subset of scrape_contacts.CANDIDATE_PATHS: homepage + the pages that
# actually carry contact/owner info. Fewer fetches per lead -> a national-scale pass
# stays tractable while still catching most emails/owners.
PATHS = ["", "contact", "contact-us", "about", "about-us"]
_PREFERRED_MAILBOXES = ("info@", "office@", "contact@", "sales@", "admin@", "hello@")


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def best_email(emails, host):
    """Pick the most useful single email: prefer one on the site's own domain, then a
    role mailbox (info@/office@...), else the first. emails is a sorted list."""
    if not emails:
        return None
    dom = (host or "").lower().removeprefix("www.")
    on_domain = [e for e in emails if dom and e.split("@")[-1] == dom]
    pool = on_domain or emails
    for kw in _PREFERRED_MAILBOXES:
        for e in pool:
            if e.startswith(kw):
                return e
    return pool[0]


def scrape_lead(lead):
    """Fetch a lead's homepage + contact/about pages and extract email/owner. Returns
    a site_contacts record (no DB write - runs in a worker thread). Reuses the
    scrape_contacts extractors so the logic lives in exactly one place (DRY)."""
    site = lead["website"]
    p = urlparse(site if "//" in site else "https://" + site)
    origin = f"{p.scheme or 'https'}://{p.netloc}"
    host = p.netloc.lower()

    emails, phones, names, founders, hints, checked = set(), set(), set(), set(), [], 0
    for path in PATHS:
        url = site if path == "" else urljoin(origin + "/", path)
        html = sc.fetch(url)
        if not html:
            continue
        checked += 1
        soup = BeautifulSoup(html, "html.parser")
        emails |= sc.emails_from(html, soup)
        for m in sc.PHONE_RE.findall(html):
            phones.add(re.sub(r"\s+", " ", m.strip()))
        n, f = sc.owner_from_jsonld(soup)
        names |= n
        founders |= f
        for h in sc.owner_hints_from_text(soup):
            if h not in hints:
                hints.append(h)

    owner = (sorted(founders) or sorted(names) or [None])[0]
    return {
        "place_id": lead["place_id"],
        "email": best_email(sorted(emails), host),
        "emails_json": json.dumps(sorted(emails)),
        "owner_name": owner,
        "owner_hints_json": json.dumps(hints[:6]),
        "extra_phones_json": json.dumps(sorted(phones)[:6]),
        "pages_checked": checked,
    }


def parse_region(arg):
    s = (arg or "").strip().lower()
    if not s or s == "all":
        return None
    return {c.strip() for c in s.replace(",", " ").split() if c.strip()}


def run(args):
    conn = leads_db.connect(args.db)
    leads_db.init_db(conn)

    if args.summary:
        s = leads_db.contact_status(conn)
        print(
            f"{args.db}: {s['scraped']} leads scraped; {s['with_email']} have an email, "
            f"{s['with_owner']} have an owner name."
        )
        return 0

    place_ids = (
        [p.strip() for p in args.place_ids.replace(",", " ").split()] if args.place_ids else None
    )
    if args.place_ids_file:  # a shortlist of ids is too long for the CLI; read from a file
        ids = Path(args.place_ids_file).read_text(encoding="utf-8").replace(",", " ").split()
        place_ids = (place_ids or []) + [i.strip() for i in ids if i.strip()]
    leads = leads_db.leads_to_scrape(
        conn,
        state_codes=parse_region(args.region),
        place_ids=place_ids,
        only_unscraped=not args.refresh,
        limit=args.limit,
    )
    if not leads:
        print("Nothing to scrape (all matching leads already scraped, or none have a website).")
        return 0

    print(
        f"Scraping contacts for {len(leads)} leads (region={args.region}, workers={args.workers})..."
    )
    now = _now()
    done = {"email": 0, "owner": 0}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(scrape_lead, lead): lead for lead in leads}
        for i, fut in enumerate(as_completed(futs), start=1):
            lead = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:  # noqa: BLE001 - one bad site must not abort the pass
                print(f"  ! {lead['name']}: {type(e).__name__}: {e}")
                continue
            leads_db.upsert_contact(conn, rec, now)
            done["email"] += 1 if rec["email"] else 0
            done["owner"] += 1 if rec["owner_name"] else 0
            if i % 25 == 0:
                conn.commit()
                print(
                    f"  ... {i}/{len(leads)} scraped ({done['email']} email, {done['owner']} owner)"
                )
    conn.commit()
    print(
        f"\nDone. Scraped {len(leads)} leads: {done['email']} with an email, {done['owner']} with an owner name."
    )
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Best-effort email/owner scrape for leads with a website (stores site_contacts)."
    )
    ap.add_argument("--db", default=leads_db.DEFAULT_DB)
    ap.add_argument(
        "--region", default="all", help="'all' or state/province codes, e.g. \"TN FL\"."
    )
    ap.add_argument(
        "--place-ids",
        default=None,
        help="Explicit comma/space list of place_ids (e.g. a shortlist); overrides --region.",
    )
    ap.add_argument(
        "--place-ids-file",
        default=None,
        help="File of place_ids (newline/comma/space-separated) - for shortlists too long for the CLI.",
    )
    ap.add_argument(
        "--limit", type=int, default=None, help="Cap the number of leads (most-reviewed first)."
    )
    ap.add_argument(
        "--refresh", action="store_true", help="Re-scrape leads already in site_contacts."
    )
    ap.add_argument("--workers", type=int, default=8, help="Concurrent site scrapes (default 8).")
    ap.add_argument(
        "--summary", action="store_true", help="Print contact coverage and exit (no fetching)."
    )
    args = ap.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
