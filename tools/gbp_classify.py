#!/usr/bin/env python3
"""
gbp_classify.py — follow-up-complaint classifier (review-scan step 4), one command.

Replaces the manual "spawn one haiku subagent per batch" seam in workflows/review_scan.md: reads
the batch JSONL files from gbp_review_batches.py, asks a cheap model (Claude Haiku) to flag each
business whose worst reviews complain about poor follow-up / responsiveness, and writes the exact
flags/batch_NN.json that gbp_pitch_list.py consumes. WAT framework: deterministic orchestration
here, the model does the reading; the classification is commodity plumbing kept behind one seam.

Paid-run discipline (mirrors gbp_audit.py / geo_grid.py): ALWAYS supports --dry-run ($0); a live
run REQUIRES a hard --budget that stops before the batch that would cross it. Resumable: a batch
whose flags file already exists is skipped (no re-spend) unless --overwrite.

  # $0 cost estimate -- makes no API calls
  python tools/gbp_classify.py --dry-run

  # live pass over every batch, capped
  python tools/gbp_classify.py --budget 0.50
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))  # tools/ on path

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
ALLOWED_TYPES = ("no_callback", "unresponsive", "no_show", "no_followthrough")

# Claude Haiku 4.5 list price, USD per million tokens (confirm at anthropic.com/pricing).
# Used for the --dry-run estimate AND, against ACTUAL returned token counts, for COGS + the
# --budget guard. A stale rate only shifts the dollar figure; the guard still stops near --budget.
RATE_USD_PER_MTOK = {"input": 1.00, "output": 5.00}

SYSTEM_PROMPT = (
    "You screen negative Google reviews for excavation, construction, septic, and plumbing "
    "contractors. Goal: find businesses whose customers complain about POOR FOLLOW-UP OR "
    "RESPONSIVENESS (missed calls, no callbacks, no-shows, no follow-through), the exact problem a "
    "call-answering service fixes.\n"
    "FLAG a business only if >=1 of its reviews shows one of: "
    "no_callback (didn't return calls / never called back / ignored messages); "
    "unresponsive (hard to reach / ghosted / no reply); "
    "no_show (missed a scheduled appointment with no notice); "
    "no_followthrough (never sent a promised quote / no follow-up after the visit). "
    "Do NOT flag reviews only about price, workmanship, rudeness, or billing unless they also "
    "describe one of those responsiveness failures. Be strict.\n"
    "OUTPUT: reply with ONLY a JSON array (no prose, no code fence), one object per FLAGGED "
    "business:\n"
    '[{"place_id":"...","name":"...","complaint_count":<int>,'
    '"complaint_types":[<subset of no_callback,unresponsive,no_show,no_followthrough>],'
    '"evidence":["<verbatim quote <=160 chars>","<optional 2nd>"]}]\n'
    "Omit businesses with no qualifying complaint. Return [] if none qualify."
)


# --------------------------------------------------------------------------- #
# Pure helpers — no I/O, unit-tested                                            #
# --------------------------------------------------------------------------- #
def build_user_message(businesses: list[dict]) -> str:
    """One JSON object per line, same shape as the batch file the model is screening."""
    lines = [
        json.dumps(
            {
                "place_id": b.get("place_id"),
                "name": b.get("name"),
                "negative_reviews": b.get("negative_reviews") or [],
            },
            ensure_ascii=False,
        )
        for b in businesses
    ]
    return "Screen these businesses (one JSON object per line):\n" + "\n".join(lines)


def parse_flags(text: str) -> list[dict]:
    """Tolerant parse of the model's reply into the exact gbp_pitch_list schema. Slices the first
    '[' to the last ']' (a stray preamble/fence is ignored), then normalizes every object:
    known complaint_types only, evidence clamped to <=160 chars and <=2 quotes, a sane
    complaint_count. Objects without a place_id are dropped."""
    lo, hi = text.find("["), text.rfind("]")
    if lo == -1 or hi == -1:
        raise ValueError("no JSON array found in classifier output")
    arr = json.loads(text[lo : hi + 1])
    out = []
    for o in arr:
        if not isinstance(o, dict) or not o.get("place_id"):
            continue
        types = [t for t in (o.get("complaint_types") or []) if t in ALLOWED_TYPES]
        evidence = [str(e)[:160] for e in (o.get("evidence") or [])][:2]
        count = o.get("complaint_count")
        if not isinstance(count, int) or count < 1:
            count = max(len(evidence), 1)
        out.append(
            {
                "place_id": o["place_id"],
                "name": o.get("name"),
                "complaint_count": count,
                "complaint_types": types,
                "evidence": evidence,
            }
        )
    return out


def cost_from_usage(usage: dict) -> float:
    return (
        usage.get("input_tokens", 0) / 1e6 * RATE_USD_PER_MTOK["input"]
        + usage.get("output_tokens", 0) / 1e6 * RATE_USD_PER_MTOK["output"]
    )


def estimate_batch_cost(businesses: list[dict]) -> dict:
    """Rough pre-call estimate (chars/4 for input; ~60 output tokens/business, assuming all flagged
    -- deliberately conservative so the --budget guard errs toward stopping early)."""
    chars = len(SYSTEM_PROMPT) + sum(len(json.dumps(b, ensure_ascii=False)) for b in businesses)
    in_tok = chars // 4 + 200
    out_tok = 60 * len(businesses) + 40
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "usd": round(cost_from_usage({"input_tokens": in_tok, "output_tokens": out_tok}), 6),
    }


def max_tokens_for(n_businesses: int) -> int:
    return min(8192, 80 * n_businesses + 512)


# --------------------------------------------------------------------------- #
# Client seam — inject a fake in tests, Anthropic in prod                       #
# --------------------------------------------------------------------------- #
class ClassifierClient(Protocol):
    def classify(self, businesses: list[dict]) -> tuple[str, dict]:
        """Return (raw_text, {"input_tokens", "output_tokens"}) for one batch."""
        ...


class AnthropicClassifier:
    """Wired to the anthropic SDK; reads ANTHROPIC_API_KEY from .env via lib.common.load_env."""

    def __init__(self, model: str = DEFAULT_MODEL):
        import anthropic  # local import so --dry-run / tests never need the SDK
        from lib.common import load_env

        load_env()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY not set in .env.")
        self._client = anthropic.Anthropic()
        self.model = model

    def classify(self, businesses):
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens_for(len(businesses)),
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_message(businesses)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return text, {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def _read_batch(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def run_classify(
    *,
    batches_dir,
    out_dir,
    client: Optional[ClassifierClient],
    model,
    dry_run,
    budget_usd,
    overwrite=False,
    log=print,
) -> dict:
    """Classify every batch. In --dry-run makes zero API calls. Live mode stops cleanly before the
    batch that would exceed budget_usd (remaining batches left for a resume, status 'partial')."""
    batch_files = sorted(Path(batches_dir).glob("batch_*.jsonl"))
    if not batch_files:
        raise SystemExit(f"No batch_*.jsonl in {batches_dir}. Run gbp_review_batches.py first.")

    plan = [(bf, _read_batch(bf)) for bf in batch_files]
    total_biz = sum(len(biz) for _, biz in plan)
    est_total = round(sum(estimate_batch_cost(biz)["usd"] for _, biz in plan), 4)
    log(f"batches={len(plan)} businesses={total_biz} model={model} -> est ${est_total:.4f}")
    if dry_run:
        return {
            "dry_run": True,
            "batches": len(plan),
            "businesses": total_biz,
            "est_usd": est_total,
        }
    assert client is not None, "client is required for live runs"

    if budget_usd is None:
        raise SystemExit(
            "Refusing to run live without --budget. Add --budget <usd> or use --dry-run."
        )

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    spent = 0.0
    classified = skipped = flagged = 0
    status = "complete"
    for bf, biz in plan:
        out_f = out_path / f"{bf.stem}.json"
        if out_f.exists() and not overwrite:
            skipped += 1
            continue
        if spent + estimate_batch_cost(biz)["usd"] > budget_usd:
            status = "partial"
            log(f"budget stop: ${spent:.4f} spent, next batch would exceed ${budget_usd:.2f}")
            break
        text, usage = client.classify(biz)
        spent += cost_from_usage(usage)
        flags = parse_flags(text)
        out_f.write_text(json.dumps(flags, ensure_ascii=False, indent=2), encoding="utf-8")
        classified += 1
        flagged += len(flags)
        log(f"  {bf.name}: flagged {len(flags)}/{len(biz)}")
    return {
        "status": status,
        "classified_batches": classified,
        "skipped": skipped,
        "flagged": flagged,
        "cost_usd": round(spent, 4),
    }


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Classify review batches for follow-up complaints (Claude Haiku)."
    )
    ap.add_argument("--batches-dir", default=".tmp/gbp/batches")
    ap.add_argument("--out-dir", default=".tmp/gbp/flags")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-classify batches that already have a flags file",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print cost estimate; make no API calls")
    ap.add_argument("--budget", type=float, help="HARD cap in USD; required for a live run")
    args = ap.parse_args(argv)

    if args.dry_run:
        out = run_classify(
            batches_dir=args.batches_dir,
            out_dir=args.out_dir,
            client=None,
            model=args.model,
            dry_run=True,
            budget_usd=None,
        )
        print(json.dumps(out, indent=2))
        return 0

    if args.budget is None:
        raise SystemExit(
            "Refusing to run live without --budget. Add --budget <usd> or use --dry-run."
        )

    print("note: Claude API calls cost credits -- running deliberately.", file=sys.stderr)
    client = AnthropicClassifier(model=args.model)
    out = run_classify(
        batches_dir=args.batches_dir,
        out_dir=args.out_dir,
        client=client,
        model=args.model,
        dry_run=False,
        budget_usd=args.budget,
        overwrite=args.overwrite,
    )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
