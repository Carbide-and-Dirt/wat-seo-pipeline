#!/usr/bin/env python3
"""
Regression tests for the deterministic matching logic — the parts that silently
corrupt scores when wrong. Run either way:

    python tests/test_tools.py        # standalone, prints PASS/FAIL per test
    pytest tests/                     # if pytest is installed

Covers the two bugs that previously shipped:
  1. robots.txt user-agent groups leaking Disallow:/ across blocks.
  2. lstrip("www.") mangling any domain starting with w/period.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from audit_site import _robots_bot_status
from score_report import domain_matches
from check_ai_visibility import mentioned, cited


def test_robots_only_named_bot_blocked():
    """A Disallow:/ for GPTBot must NOT leak onto * or other bots."""
    txt = "User-agent: *\nDisallow: /wp-admin/\n\nUser-agent: GPTBot\nDisallow: /\n"
    r = _robots_bot_status(txt)
    assert r["GPTBot"] == "blocked"
    assert r["PerplexityBot"] == "allowed"
    assert r["OAI-SearchBot"] == "allowed"
    assert r["Bingbot"] == "allowed"


def test_robots_block_all():
    r = _robots_bot_status("User-agent: *\nDisallow: /\n")
    assert all(v == "blocked" for v in r.values())


def test_robots_allow_all_empty_disallow():
    r = _robots_bot_status("User-agent: *\nDisallow:\n")
    assert all(v == "allowed" for v in r.values())


def test_robots_group_after_other_directive():
    """A Sitemap: line must not bind a following User-agent group to earlier rules."""
    txt = (
        "Sitemap: https://x.com/s.xml\n"
        "User-agent: PerplexityBot\nDisallow: /\n"
        "User-agent: Bingbot\nDisallow: /private\n"
    )
    r = _robots_bot_status(txt)
    assert r["PerplexityBot"] == "blocked"
    assert r["Bingbot"] == "allowed"


def test_robots_shared_group():
    """Consecutive User-agent lines share one rule set."""
    txt = "User-agent: PerplexityBot\nUser-agent: OAI-SearchBot\nDisallow: /\n"
    r = _robots_bot_status(txt)
    assert r["PerplexityBot"] == "blocked"
    assert r["OAI-SearchBot"] == "blocked"
    assert r["Bingbot"] == "unspecified"


def test_robots_content_signal_ai_train_no():
    """Cloudflare's edge-injected Content-Signal opt-out counts as blocked."""
    txt = "User-agent: GPTBot\nContent-Signal: ai-train=no\n"
    r = _robots_bot_status(txt)
    assert r["GPTBot"] == "blocked"
    assert r["Bingbot"] == "unspecified"


def test_domain_matches_exact_and_www():
    assert domain_matches("theclimbgyms.com", "theclimbgyms.com")
    assert domain_matches("www.theclimbgyms.com", "theclimbgyms.com")
    assert domain_matches("theclimbgyms.com", "www.theclimbgyms.com")


def test_domain_matches_subdomain():
    assert domain_matches("blog.theclimbgyms.com", "theclimbgyms.com")


def test_domain_matches_rejects_substring():
    """The whole point of dot-boundary matching: climb.com != myclimb.com."""
    assert not domain_matches("myclimb.com", "climb.com")
    assert not domain_matches("notclimb.com", "climb.com")


def test_domain_matches_w_leading_regression():
    """Regression for the lstrip('www.') bug: w-leading domains must still match."""
    assert domain_matches("westside-climbing.com", "westside-climbing.com")
    assert domain_matches("www.westside-climbing.com", "westside-climbing.com")


def test_mentioned_word_boundary():
    assert mentioned("I love The Crag downtown", ["The Crag"])
    assert mentioned("Visit theclimbgyms today", ["The Climb Gyms", "theclimbgyms"])
    # substring inside a larger word must NOT count
    assert not mentioned("a skilled cragsman climbed", ["Crag"])


def test_mentioned_empty_and_none():
    assert not mentioned("", ["Anything"])
    assert not mentioned(None, ["Anything"])
    assert not mentioned("text", [""])


def test_cited_dot_boundary():
    cites = ["https://www.theclimbgyms.com/rates", "https://other.com"]
    assert cited(cites, "theclimbgyms.com")
    assert cited(["https://blog.theclimbgyms.com/x"], "theclimbgyms.com")
    assert not cited(["https://nottheclimbgyms.com"], "theclimbgyms.com")
    assert not cited(["https://x.com"], "")


def _run_standalone():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
