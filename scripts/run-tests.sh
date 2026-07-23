#!/usr/bin/env bash
# run-tests.sh - auto-detects the test runner (pytest if present, else manage.py test).
# Prefers pytest if installed (it also collects Django/unittest-style tests); else
# falls back to Django's manage.py test. Exit code is the test run's exit code.
set -uo pipefail

PY=python3
command -v python3 >/dev/null 2>&1 || PY=python

if [ -x ".venv/bin/python" ] && .venv/bin/python -c "import pytest" >/dev/null 2>&1; then
  echo "run-tests: using project venv pytest"
  exec .venv/bin/python -m pytest -q
elif command -v pytest >/dev/null 2>&1; then
  echo "run-tests: using pytest"
  exec pytest -q
elif [ -f manage.py ]; then
  echo "run-tests: using manage.py test"
  exec "$PY" manage.py test
else
  echo "run-tests: neither pytest nor manage.py found - skipping (configure one)"
  exit 0
fi
