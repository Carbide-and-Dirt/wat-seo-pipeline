#!/usr/bin/env python3
"""check-secrets.py - secrets live only in .env, never hardcoded.

Scans tracked text files for high-confidence secret patterns and fails if a real
.env file is tracked. Pure stdlib. Exit 1 = fail. High-confidence patterns block;
the generic "key = '...'" heuristic only warns (it false-positives on placeholders).
"""

import re
import subprocess
import sys

HIGH = [
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "Anthropic API key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "Google API key"),
]
GENERIC = re.compile(
    r"\b(api[_-]?key|secret|password|passwd|token|client[_-]?secret)\b\s*[:=]\s*[\"'][^\"']{12,}[\"']",
    re.IGNORECASE,
)
SKIP = re.compile(r"(^|/)(node_modules|dist|\.git|\.astro|venv|\.venv|__pycache__|migrations)/")
BINARY = re.compile(
    r"\.(png|jpe?g|gif|webp|ico|pdf|zip|gz|woff2?|ttf|otf|mp4|mov|pyc|so)$", re.IGNORECASE
)
ENV_OK = re.compile(r"\.env\.(example|sample|template)$", re.IGNORECASE)
ENV_BAD = re.compile(r"(^|/)\.env(\.[a-z]+)?$", re.IGNORECASE)
SAFE_LINE = re.compile(
    r"\.env|os\.environ|getenv|settings\.|example|placeholder|xxxx|<your", re.IGNORECASE
)


def tracked():
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
        return [f for f in out.splitlines() if f]
    except Exception:
        return []


def main():
    failures, warnings = [], []
    for f in tracked():
        if SKIP.search(f) or BINARY.search(f):
            continue
        if ENV_BAD.search(f) and not ENV_OK.search(f):
            failures.append(
                f"{f}: a real .env file is tracked by git - it should be gitignored, not committed"
            )
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception:
            continue
        for rx, name in HIGH:
            if rx.search(text):
                failures.append(f"{f}: looks like a committed {name}")
        for i, ln in enumerate(text.splitlines(), 1):
            if GENERIC.search(ln) and not SAFE_LINE.search(ln):
                warnings.append(f"{f}:{i}: possible hardcoded secret (verify): {ln.strip()[:80]}")

    if warnings:
        print("check-secrets: warnings (not blocking - verify these aren't real):", file=sys.stderr)
        for w in warnings:
            print("  " + w, file=sys.stderr)
    if failures:
        print("check-secrets: FAIL", file=sys.stderr)
        for f in failures:
            print("  " + f, file=sys.stderr)
        print("Move secrets into a gitignored .env. Never commit keys.", file=sys.stderr)
        sys.exit(1)
    print("check-secrets: ok")


if __name__ == "__main__":
    main()
