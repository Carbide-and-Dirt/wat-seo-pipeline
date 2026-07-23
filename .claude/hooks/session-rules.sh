#!/usr/bin/env bash
# session-rules.sh (Python/Django, project level) - SessionStart reinforcement of
# the gates that are enforced in THIS repo. Judgment rules live in your global
# ~/.claude hook; this just states the deterministic boundaries so Claude works
# with them instead of against them.
cat <<'EOF'
## Enforced gates in this repo (Python/Django) - don't fight them, they block
- ruff check . and ruff format --check . must pass (lint, complexity<=10, no
  leftover breakpoint()/pdb, no stray print outside tests/migrations/manage.py).
- No model/migration drift: `python manage.py makemigrations --check --dry-run`
  must be clean. If you changed models, generate the migration.
- mypy must pass (enforced in pre-push + CI).
- Tests must pass (pytest if present, else manage.py test) - in pre-push + CI.
- No secrets in source; secrets live only in .env. No invalid-UTF-8/BOM files.
- These run in the Stop hook (fast subset), pre-push, and CI. CI on main is the
  hard lock.
EOF
