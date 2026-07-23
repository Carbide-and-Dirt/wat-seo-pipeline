## What & why

## Checklist

- [ ] Gates green locally: `ruff check .`, `ruff format --check .`, `mypy .`,
      secrets + encoding checks, `pytest tests/`
- [ ] No network / no API spend in tests; no real business or contact data in
      fixtures
- [ ] Paid-call changes keep `--dry-run` estimates and `--budget` enforcement
- [ ] Matching `workflows/*.md` SOP updated if behavior changed
