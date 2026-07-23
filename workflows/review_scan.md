# Workflow: Follow-up-Complaint Review Scan

## Objective
From the GBP-audited store, build a ranked pitch list of **good, busy, owner-operated excavation
(and adjacent: septic, plumbing) businesses whose reviews complain about follow-up / responsiveness**
(missed calls, no callbacks, no-shows, no follow-through). That leak is exactly what a call-answering /
rapid-follow-up service fixes, so those businesses are the best cold-outreach targets. Output: a trade-tabbed XLSX +
CSV with evidence quotes and contacts.

**Targeting principle (operator decision, 2026-07-05):** target good businesses with a *fixable leak*, NOT
neglected ones. The ICP is **claimed + rating >= 4.0 + more than 20 reviews**. A follow-up complaint
is the PITCH, not a disqualifier: you are reading a great business's *worst* reviews (we sort to
lowest-rating on purpose), so a 4.9-star shop with a few no-show complaints is the ideal target, not
a company to avoid. Rank by rating with a real-but-small leak; drop the big nationals.

## Prerequisites
- `gbp_audit.py` has been run on the cohort (so `gbp_audits` has `neg_reviews` per business).
- `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` in `.env` (the reviews endpoint is paid).

## Steps

### 1. Select the review cohort (ICP + has negatives)
Write the target `place_id`s to a file — ICP filter plus a negative-review floor:
```sql
SELECT g.place_id FROM gbp_audits g
WHERE g.status='complete' AND g.is_claimed=1 AND g.rating_value>=4.0
  AND g.rating_votes>20 AND g.neg_reviews>=3;   -- tune the floor
```
→ `.tmp/gbp/review_targets.txt` (one place_id per line).

### 2. Fetch the lowest-rated reviews (PAID; transient)
```bash
python tools/gbp_reviews.py --place-ids-file .tmp/gbp/review_targets.txt --dry-run          # estimate
python tools/gbp_reviews.py --place-ids-file .tmp/gbp/review_targets.txt --budget 1.50       # live
```
Pulls each target's `--depth` (default 20) **lowest-rated** reviews to `.tmp/gbp/reviews/<place_id>.json`.
Review TEXT is transient (PII) — it never enters the durable DB. Standard queue, ~$0.00225/target.

### 3. Batch the in-scope trades
```bash
python tools/gbp_review_batches.py            # -> .tmp/gbp/batches/batch_NN.jsonl
```
Keeps only excavation + septic + plumber trades (`gbp_trades.py`), drops the other trades, and only
businesses with negative review text. ~55 businesses per batch file.

### 4. Classify each batch (one command, PAID but cheap)
```bash
python tools/gbp_classify.py --dry-run           # $0 estimate: batches, businesses, est cost
python tools/gbp_classify.py --budget 0.50       # live: Claude Haiku flags each batch
```
`gbp_classify.py` reads every `batch_NN.jsonl`, asks Claude Haiku to flag businesses with a
follow-up/responsiveness complaint, and writes `.tmp/gbp/flags/batch_NN.json` in the exact schema
`gbp_pitch_list.py` consumes. It **replaces the old manual "spawn one subagent per batch" step**:
same prompt, same output, now one command. Needs `ANTHROPIC_API_KEY` in `.env`. Always `--dry-run`
first; a live run needs a hard `--budget` that stops before the batch that would cross it; a batch
whose flags file already exists is skipped unless `--overwrite` (resumable, no re-spend).

The classifier's rules (the strict follow-up-complaint definition and the JSON output schema) live
in `SYSTEM_PROMPT` in `tools/gbp_classify.py` — edit there to tune them. Complaint types:
`no_callback`, `unresponsive`, `no_show`, `no_followthrough`.

**Manual fallback (while `ANTHROPIC_API_KEY` is blank):** classify by hand instead. Spawn one
`haiku` subagent per `batch_NN.jsonl` in parallel, giving each the `SYSTEM_PROMPT` from
`tools/gbp_classify.py` as its instruction; each reads its batch and writes the same JSON array to
`.tmp/gbp/flags/batch_NN.json`. Keeping the prompt in the tool (not re-pasted here) means the manual
and one-command paths classify identically — so the switch to `gbp_classify.py` is a no-op when the
key is set.

Note: the classifier reads each business's *worst* reviews, so flag rates run high (50-90%); the
value is the ranking + evidence, not the raw flag.

### 5. Build the ranked, trade-tabbed pitch list
```bash
python tools/gbp_pitch_list.py                                    # cap 500 reviews, >=2 complaints
python tools/gbp_pitch_list.py --max-reviews 0 --min-complaints 1 # keep everything
```
Drops the big nationals (`--max-reviews`), requires a real leak (`--min-complaints`), ranks by
**rating DESC → follow-up-complaint count DESC → fewest reviews (owner-operated)**, and writes
`output/excavation-followup-pitch-list.xlsx` (tabs: All / Excavation / Septic / Plumber) + `.csv`
with evidence quotes and contacts.

## Outputs
| Step | Output |
|------|--------|
| fetch | `.tmp/gbp/reviews/<place_id>.json` (transient review text) |
| batch | `.tmp/gbp/batches/batch_NN.jsonl` |
| classify | `.tmp/gbp/flags/batch_NN.json` |
| pitch list | `output/excavation-followup-pitch-list.xlsx` + `.csv` |

## Gotchas
- **Reviews endpoint is slow** (Standard queue, minutes/task): `gbp_reviews.py` bulk-submits then
  collects. Give a generous `--deadline`; it is resumable (un-collected tasks just aren't written).
- **A subagent may prepend its summary line** before the JSON; `gbp_pitch_list.py` tolerates a stray
  preamble (slices first `[` to last `]`), but check for a malformed/empty flag file if a batch is missing.
- **Trade scope** lives in `gbp_trades.py` — edit there to change include/exclude categories.
- **Review text is PII**: keep it in `.tmp` only; never commit it or write it to `leads.sqlite`.
