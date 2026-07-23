#!/usr/bin/env python3
"""
site_fingerprint.py - infer who builds/runs a lead's website (HLD FR-16).

Pure logic: given a page's raw HTML (+ optional response headers and the site's
own host), detect the **site builder / CMS**, the **marketing & call-tracking
tags**, any **agency footer credit**, and **Google Ads** presence, then classify
the site's management as DIY / self-managed / agency-managed with a confidence
level and the supporting evidence. No network, no DB - so it is fully unit-testable
with fixture HTML and reused by enrich_sites.py.

This is an INFERENCE from public footprints, never a confirmed fact: a real
agency relationship can only be verified by asking the business. The output is
always (status, confidence, evidence[]) so a human can judge the call.
"""

import re
from urllib.parse import urlparse

# --- Site builder / CMS fingerprints (substring markers in HTML or headers). ---
# Order matters: more specific CDNs first. A DIY drag-drop builder leans owner-built;
# a dev/CMS platform (WordPress/Webflow) is neutral - it can be DIY or agency.
_BUILDER_MARKERS = [
    (
        "Wix",
        ("wixstatic.com", "wix.com/", "_wixcssimports", "wix-warmup-data", 'content="wix.com"'),
    ),
    (
        "Squarespace",
        (
            "static1.squarespace.com",
            "squarespace-cdn.com",
            "squarespace.com",
            'content="squarespace"',
        ),
    ),
    (
        "GoDaddy Website Builder",
        ("img1.wsimg.com", "wsimg.com", "godaddy website builder", "/websitebuilder/"),
    ),
    ("Duda", ("irp.cdn-website.com", "multiscreensite.com", "dudaone", "_duda_")),
    ("Weebly", ("weebly.com", "editmysite.com")),
    (
        "Webflow",
        ("assets.website-files.com", "uploads-ssl.webflow.com", ".webflow.io", 'content="webflow"'),
    ),
    ("Shopify", ("cdn.shopify.com", "shopify.theme", "myshopify.com")),
    ("HubSpot CMS", ("hs-sites.com", "hubspotusercontent", "hscollectedforms")),
    ("Google Business Site", ("sites.google.com", "business.site")),
    ("Joomla", ('content="joomla',)),
    ("Drupal", ("drupal.settings", 'content="drupal')),
    ("WordPress", ("/wp-content/", "/wp-includes/", 'content="wordpress')),
]

# Drag-drop builders that strongly imply an owner built the site themselves.
DIY_BUILDERS = {
    "Wix",
    "Squarespace",
    "GoDaddy Website Builder",
    "Duda",
    "Weebly",
    "Google Business Site",
}

# --- Marketing / tracking tag fingerprints. ---
# "Managed" tags = tools a marketer/agency typically installs for a contractor
# (call tracking, marketing automation, paid-ads conversion). Their presence is the
# strongest cheap signal that *someone* is actively doing marketing for the business.
_TAG_MARKERS = [
    ("CallRail", ("callrail.com", "cdn.callrail")),
    ("HubSpot", ("js.hs-scripts.com", "js.hsforms.net", "hs-analytics.net", "hsforms.com")),
    ("Marketo", ("munchkin.js", "marketo.com", "mktoresp.com")),
    (
        "Google Ads",
        ("googleadservices.com", "googleads.g.doubleclick.net", "/aw-", "send_to': 'aw-"),
    ),
    ("Meta Pixel", ("connect.facebook.net", "fbq(")),
    ("Google Tag Manager", ("googletagmanager.com/gtm.js", "gtm-")),
    (
        "Google Analytics",
        (
            "googletagmanager.com/gtag/js",
            "google-analytics.com",
            "ga('create'",
            "gtag('config', 'g-",
        ),
    ),
    ("Hotjar", ("hotjar.com", "static.hotjar")),
    ("Bing UET", ("bat.bing.com",)),
    ("LinkedIn Insight", ("snap.licdn.com",)),
]

# Tags that indicate a managed/marketing operation (vs. plain analytics everyone has).
MANAGED_TAGS = {"CallRail", "HubSpot", "Marketo", "Google Ads"}
ACTIVE_MARKETING_TAGS = MANAGED_TAGS | {
    "Meta Pixel",
    "Google Tag Manager",
    "Hotjar",
    "Bing UET",
    "LinkedIn Insight",
}

# Domains that credit themselves but are NOT the site's marketing agency: the website
# PLATFORM ("Powered by Wix"), the host, or a bolt-on widget/SaaS vendor ("Powered by
# LiveChat"). Crediting one of these is not evidence of an agency relationship.
_PLATFORM_CREDIT_DOMAINS = (
    "wix.com",
    "squarespace.com",
    "godaddy.com",
    "weebly.com",
    "wordpress.com",
    "wordpress.org",
    "duda.co",
    "webflow.com",
    "shopify.com",
    "google.com",
    "wpengine.com",
    "bluehost.com",
    "hostgator.com",
    # widget / review / chat / listing SaaS - not an agency:
    "livechatinc.com",
    "livechat.com",
    "tawk.to",
    "podium.com",
    "birdeye.com",
    "yext.com",
    "broadly.com",
    "weschedule.com",
    "housecallpro.com",
    "jobber.com",
)

# "<credit phrase> by <a href=agency>Name</a>" - the common footer attribution.
_CREDIT_RE = re.compile(
    r"(?i)\b(website|web\s?site|web\s?design|design(?:ed)?|develop(?:ed)?|built|created|powered|marketing|site)"
    r"\s+(?:&amp;|&|and)?\s*(?:\w+\s+)?by\s*:?\s*"
    r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>([^<]{2,60})</a>"
)
# Phrases that genuinely imply a build/marketing vendor (vs. a bare "powered by" platform line).
_STRONG_CREDIT = ("design", "develop", "built", "created", "marketing", "web site", "website")


def detect_builder(html, headers=None):
    """Return the site builder/CMS name, or None if unrecognized (custom/unknown)."""
    hay = (html or "").lower()
    # Response headers can name the platform even when HTML is ambiguous.
    hdr = " ".join(f"{k}:{v}" for k, v in (headers or {}).items()).lower()
    for name, markers in _BUILDER_MARKERS:
        if any(m in hay or m in hdr for m in markers):
            return name
    # Generic <meta name="generator" content="X"> fallback.
    m = re.search(
        r'(?i)<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)["\']', html or ""
    )
    return m.group(1).strip() if m else None


def detect_tags(html):
    """Return the sorted list of marketing/tracking tags found in the page."""
    hay = (html or "").lower()
    found = [name for name, markers in _TAG_MARKERS if any(m in hay for m in markers)]
    return sorted(set(found))


def detect_google_ads(html):
    """True if the page carries Google Ads conversion/remarketing tags."""
    hay = (html or "").lower()
    return any(m in hay for m in ("googleadservices.com", "googleads.g.doubleclick.net", "aw-"))


def _strip_www(domain):
    return domain[4:] if domain.startswith("www.") else domain


def detect_agency_credit(html, host=None):
    """Find a footer 'designed/built/site by <agency>' credit, ignoring platform
    self-credits and links back to the site's own host. Returns
    {'label': text, 'domain': domain, 'strong': bool} or None."""
    own = _strip_www((host or "").lower())
    for m in _CREDIT_RE.finditer(html or ""):
        phrase, href, label = m.group(1).lower(), m.group(2), m.group(3).strip()
        dom = _strip_www(urlparse(href if "//" in href else "//" + href).netloc.lower())
        if not dom or (own and dom == own):
            continue
        if any(p in dom for p in _PLATFORM_CREDIT_DOMAINS):
            continue
        return {"label": label, "domain": dom, "strong": any(s in phrase for s in _STRONG_CREDIT)}
    return None


def classify_management(builder, tags, agency_credit, google_ads, reachable=True):
    """Classify site management as a likelihood + evidence (FR-16).

    status: 'likely agency-managed' | 'self-managed (active marketing)' |
            'DIY / unmanaged' | 'custom/unclear - review' | 'unknown'.
    Returns (status, confidence, evidence[]). Always evidence-backed - this is an
    inference, so the evidence list is what a human actually acts on."""
    if not reachable:
        return "unknown", "n/a", ["site did not load - could not fingerprint"]

    evidence = []
    if builder:
        evidence.append(f"builder: {builder}")
    managed_tags = [t for t in tags if t in MANAGED_TAGS]
    if managed_tags:
        evidence.append("marketing stack: " + ", ".join(managed_tags))
    other_tags = [t for t in tags if t in ACTIVE_MARKETING_TAGS and t not in MANAGED_TAGS]
    if other_tags:
        evidence.append("tags: " + ", ".join(other_tags))
    if google_ads and "Google Ads" not in managed_tags:
        evidence.append("running Google Ads")

    # Strongest signal: an explicit agency credit in the footer.
    if agency_credit:
        cred = agency_credit.get("label") or agency_credit.get("domain")
        evidence.insert(0, f"footer credit: {cred} ({agency_credit.get('domain')})")
        conf = "high" if agency_credit.get("strong") else "medium"
        return "likely agency-managed", conf, evidence

    # Next: call-tracking / marketing-automation / ads = a pro is running marketing.
    if managed_tags or google_ads:
        # On a drag-drop builder it's more often the owner using a service; on a
        # custom/CMS build it leans agency. Either way it's an active-marketing lead.
        if builder in DIY_BUILDERS:
            return "self-managed (active marketing)", "medium", evidence
        return "likely agency-managed", "medium", evidence

    has_active = bool(other_tags)
    if builder in DIY_BUILDERS:
        if has_active:
            return "self-managed (active marketing)", "medium", evidence
        return "DIY / unmanaged", "medium", evidence

    # Custom or CMS build with no managed marketing tags - ambiguous.
    if has_active:
        return "self-managed (active marketing)", "low", evidence
    if builder:  # known CMS (e.g. WordPress) but nothing marketing-y on it
        return "custom/unclear - review", "low", evidence
    return (
        "custom/unclear - review",
        "low",
        evidence or ["no recognizable builder or marketing tags"],
    )


def fingerprint(html, headers=None, host=None, reachable=True):
    """Full fingerprint for one page: builder, tags, agency credit, ads, and the
    management classification. The dict maps onto the site_enrichment columns."""
    if not reachable or not html:
        status, conf, evidence = classify_management(None, [], None, False, reachable=False)
        return {
            "builder": None,
            "marketing_tags": [],
            "agency_credit": None,
            "google_ads": False,
            "mgmt_status": status,
            "mgmt_confidence": conf,
            "mgmt_evidence": evidence,
        }
    builder = detect_builder(html, headers)
    tags = detect_tags(html)
    ads = detect_google_ads(html)
    credit = detect_agency_credit(html, host)
    status, conf, evidence = classify_management(builder, tags, credit, ads, reachable=True)
    return {
        "builder": builder,
        "marketing_tags": tags,
        "agency_credit": (credit or {}).get("domain"),
        "google_ads": ads,
        "mgmt_status": status,
        "mgmt_confidence": conf,
        "mgmt_evidence": evidence,
    }
