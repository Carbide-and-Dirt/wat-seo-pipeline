#!/usr/bin/env python3
"""check-encoding.py - files are valid UTF-8 with no BOM.

The checkable half of the old Windows rule. Less critical now you're on WSL
(native UTF-8), but a cheap guard against files that came across from Windows
mangled. Exit 1 = fail.
"""

import re
import subprocess
import sys

SKIP = re.compile(r"(^|/)(node_modules|dist|\.git|\.astro|venv|\.venv|__pycache__)/")
BINARY = re.compile(
    r"\.(png|jpe?g|gif|webp|ico|pdf|zip|gz|woff2?|ttf|otf|mp4|mov|svg|pyc|so)$", re.IGNORECASE
)


def tracked():
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout
        return [f for f in out.splitlines() if f]
    except Exception:
        return []


def main():
    bad = []
    for f in tracked():
        if SKIP.search(f) or BINARY.search(f):
            continue
        try:
            with open(f, "rb") as fh:
                buf = fh.read()
        except Exception:
            continue
        if buf[:3] == b"\xef\xbb\xbf":
            bad.append(f"{f}: has a UTF-8 BOM (strip it)")
            continue
        try:
            buf.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(f"{f}: not valid UTF-8 (re-save as UTF-8)")
    if bad:
        print("check-encoding: FAIL", file=sys.stderr)
        for b in bad:
            print("  " + b, file=sys.stderr)
        sys.exit(1)
    print("check-encoding: ok")


if __name__ == "__main__":
    main()
