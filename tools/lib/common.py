"""Shared utilities for all seo-analysis CLI tools."""

import os
import sys
from pathlib import Path


def load_env(env_path=".env"):
    """Populate os.environ from a .env file (no python-dotenv dependency)."""
    p = Path(env_path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def utf8_stdout():
    """Reconfigure stdout/stderr to UTF-8 (guards against Windows cp1252 crashes)."""
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def slug(s: str) -> str:
    """URL-safe slug: lowercase, non-alphanum -> hyphen, strip leading/trailing hyphens."""
    return "".join(ch if ch.isalnum() else "-" for ch in s.lower()).strip("-")
