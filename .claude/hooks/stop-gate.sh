#!/usr/bin/env bash
# stop-gate.sh (Python/Django) - blocks Claude declaring "done" while a FAST gate
# is red. Deliberately runs only quick static checks: ruff, format, migration
# drift, secrets, encoding. The slow gates (mypy, full test suite) live in
# pre-push and CI so the Stop hook stays fast.
set -uo pipefail
ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$ROOT" || exit 0
fail=""

mark() { fail="${fail:+$fail, }$1"; }

RUFF=ruff
[ -x ".venv/bin/ruff" ] && RUFF=".venv/bin/ruff"
if command -v "$RUFF" >/dev/null 2>&1; then
  "$RUFF" check . >/dev/null 2>&1 || mark "ruff lint"
  "$RUFF" format --check . >/dev/null 2>&1 || mark "ruff format"
else
  echo "stop-gate: ruff not installed (pip install -r requirements-dev.txt in the venv)" >&2
fi

if [ -f manage.py ]; then
  PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
  "$PY" manage.py makemigrations --check --dry-run >/dev/null 2>&1 || mark "migration drift"
fi

python3 scripts/check-secrets.py >/dev/null 2>&1 || mark "secrets"
python3 scripts/check-encoding.py >/dev/null 2>&1 || mark "encoding"

if [ -n "$fail" ]; then
  echo "Blocking completion: $fail failed. Run the gate locally and fix before finishing." >&2
  echo "  ruff check . ; ruff format --check . ; python manage.py makemigrations --check --dry-run" >&2
  exit 2
fi
exit 0
