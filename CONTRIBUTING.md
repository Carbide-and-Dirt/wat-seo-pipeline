# Contributing

Thanks for your interest. First, expectation-setting: this is **production
tooling shared as-is**. Carbide and Dirt uses it daily, and we review issues and
PRs as time allows — there is no SLA, and PRs that fight the project's design
(below) will be declined kindly.

## The design you're contributing into

The WAT split is the whole point: **workflows** (markdown SOPs) say what to do,
**tools** (deterministic Python CLIs) do all measuring, and the interpretive
narrative is left to the operator/agent. Keep that separation:

- Tools never guess or fabricate — a number either comes from a measurement or
  is reported "not measured."
- Every paid API call sits behind an explicit flag, a `--dry-run` estimate, and
  (for anything that scales) a hard `--budget`.
- One tool = one standalone `argparse` CLI. Shared logic goes in `tools/lib/`.
- `place_id` is the only Google field stored permanently (ToS); everything else
  is refreshable cache.

## Ground rules for PRs

1. **Green gates.** `ruff check .`, `ruff format --check .`, `mypy .`,
   `python3 scripts/check-secrets.py`, `python3 scripts/check-encoding.py`, and
   `pytest tests/` all pass locally (`pip install -r requirements-dev.txt`).
   CI runs the same on 3.10 and 3.14.
2. **Tests are no-network, no-spend.** New tool logic gets tests that fake the
   API layer; nothing in `tests/` may hit the network or cost money.
3. **Never commit data.** No `.env`, no `leads.sqlite`, no scraped contacts, no
   real prospect/client data — including in test fixtures (use fictional
   businesses).
4. **Update the SOP.** If you change a tool's behavior or hit a gotcha, reflect
   it in the matching `workflows/*.md` — the SOPs are the user manual.

## Good first contributions

Bug reports with a failing case, additional AI-engine visibility checkers,
schema-parser edge cases, and provider-API drift fixes are all welcome.
