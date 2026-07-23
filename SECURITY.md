# Security Policy

This project handles API credentials (Perplexity, Google, DataForSEO, Anthropic,
Firecrawl) via a local `.env` file and never stores them elsewhere. If you find a
vulnerability — a way credentials could leak, a path traversal in the report
writers, injection via fetched page content, anything — please report it
privately.

## Reporting a vulnerability

Use **GitHub's private vulnerability reporting** on this repository
(Security tab → "Report a vulnerability"). Please don't open a public issue for
security problems.

You can expect an acknowledgement within a week. This is production tooling we
maintain for our own use; fixes for real vulnerabilities are prioritized, but
there is no formal SLA.

## Scope notes

- Only the latest commit on `main` is supported.
- API keys belong in `.env` (gitignored). A committed key is a bug in your
  clone, not in this project — but if the tooling *let* it happen (e.g. the
  secrets gate missed a pattern), that's in scope and we want to know.
