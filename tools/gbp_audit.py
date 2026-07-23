#!/usr/bin/env python3
"""
gbp_audit.py — prospect-side Google Business Profile neglect audit (DESIGN-gbp-prospect-audit.md).

For a cold prospect the agency does NOT manage, the owner-scoped GBP APIs are closed, so this
reads the public profile through DataForSEO's Business Data API (ToS-safe, no OAuth, no scraping):
  - my_business_info    -> is_claimed, rating, categories, photos, hours, description, attributes
  - my_business_updates -> GMB posts + recency ("no posts in 2 years")
and rolls them into a tunable neglect score (higher = more neglected = better pitch).

Fits the WAT framework: deterministic measurement here, agent writes the narrative. Snapshots are
append-only, so the sale-time audit is the "before" for the client before/after report.

Paid-run discipline (mirrors geo_grid.py / measure_shortlist.py): ALWAYS supports --dry-run ($0);
a live run REQUIRES a hard --budget that stops before the request that would cross it; resumable
via --skip-existing. Lookup + match are by place_id only (keyword="place_id:<id>"), never by name.

  # $0 cost estimate — makes no API calls
  python tools/gbp_audit.py --relevance match --dry-run

  # live pass over confirmed-trade prospects, capped
  python tools/gbp_audit.py --relevance match --budget 2.00

  # one prospect; info-only (skip the posts endpoint)
  python tools/gbp_audit.py --place-id ChIJ... --no-updates --budget 0.01
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

DB_PATH = Path("data/leads.sqlite")

# DataForSEO Business Data pricing (verified 2026-07-05, official Business Data table).
INFO_RATE_USD = {"standard": 0.0015, "priority": 0.003, "live": 0.0054}
# Updates is a task (no live mode): a setup fee + a per-10-posts fee. We estimate one
# 10-post bucket per prospect; the ACTUAL cost is read back from each task response.
UPDATES_SETUP_USD = {"standard": 0.0015, "priority": 0.003}
UPDATES_PER_10_USD = {"standard": 0.00075, "priority": 0.0015}

# Neglect-score weights (approved 2026-07-05, tunable in one place). Higher total = more
# neglected profile = stronger sales opening.
# 'unclaimed' weight is 0 by decision (2026-07-07): DataForSEO's is_claimed is an unreliable
# inference (it false-negatives on clearly-claimed businesses), so it must not drive the
# ranking, and claim status must never be asserted as fact in a customer-facing artifact
# (SEC-D, ARCHITECTURE.md section 9). is_claimed is still collected as raw reference; it just
# scores nothing. With unclaimed at 0 the reachable maximum is 60, not 100.
NEGLECT_WEIGHTS = {
    "unclaimed": 0,
    "stale_posts": 15,  # no post within STALE_POST_DAYS (or ever)
    "thin_reviews": 10,  # rating_votes < 10
    "few_photos": 10,  # total_photos < 10
    "no_secondary_categories": 8,
    "no_hours": 7,
    "sparse_attributes": 5,  # attr_available_count < 3
    "no_description": 5,
}
STALE_POST_DAYS = 90


# --------------------------------------------------------------------------- #
# Pure extraction — defensive against the exact DataForSEO field names.        #
# Confirm these against the first live task response (the geo_grid precedent). #
# --------------------------------------------------------------------------- #
def _count_attrs(node) -> Optional[int]:
    """Attributes come back as a list, a dict of category->list, or a flat dict. Count leaves."""
    if node is None:
        return None
    if isinstance(node, list):
        return len(node)
    if isinstance(node, dict):
        total = 0
        for v in node.values():
            total += len(v) if isinstance(v, (list, dict)) else 1
        return total
    return None


def extract_info(items, expected_place_id: Optional[str] = None) -> dict:
    """Pull the completeness fields from a my_business_info result. `items` is the result
    list (one business). Returns {"found": bool, ...fields}. Never fabricates: an absent
    field stays None (unknown), distinct from a measured 0."""
    if not items:
        return {"found": False, "found_place_id": None}
    it = items[0] if isinstance(items, list) else items
    rating = it.get("rating") or {}
    add_cats = it.get("additional_categories")
    desc = it.get("description") or it.get("snippet")
    attrs = it.get("attributes") or {}
    rd = it.get("rating_distribution") or {}
    neg = (int(rd.get("1") or 0) + int(rd.get("2") or 0)) if rd else None
    return {
        "found": True,
        "found_place_id": it.get("place_id") or it.get("cid") or expected_place_id,
        "is_claimed": (1 if it.get("is_claimed") else 0)
        if it.get("is_claimed") is not None
        else None,
        "rating_value": rating.get("value"),
        "rating_votes": rating.get("votes_count"),
        "category": it.get("category"),
        "additional_categories_count": len(add_cats) if isinstance(add_cats, list) else None,
        "has_description": (1 if str(desc).strip() else 0) if desc is not None else 0,
        "total_photos": it.get("total_photos"),
        "has_hours": 1 if it.get("work_time") else 0,
        "attr_available_count": _count_attrs(attrs.get("available_attributes")),
        "attr_unavailable_count": _count_attrs(attrs.get("unavailable_attributes")),
        "neg_reviews": neg,
        "rating_distribution_json": json.dumps(rd) if rd else None,
    }


def _parse_ts(raw) -> Optional[datetime]:
    """Tolerant parse of a DataForSEO timestamp ('YYYY-MM-DD HH:MM:SS +00:00' or ISO)."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def extract_updates(items, now: datetime) -> dict:
    """Post count + recency from a my_business_updates result. `now` is injected for
    testability. days_since_post is None when there are no dated posts."""
    posts = items or []
    times = []
    for p in posts:
        dt = _parse_ts(p.get("timestamp") or p.get("time") or p.get("update_date"))
        if dt:
            times.append(dt)
    latest = max(times) if times else None
    return {
        "post_count": len(posts),
        "last_post_ts": latest.isoformat(timespec="seconds") if latest else None,
        "days_since_post": (now - latest).days if latest else None,
    }


# --------------------------------------------------------------------------- #
# Neglect score — deterministic reducer (score_report.py style), unit-tested.  #
# --------------------------------------------------------------------------- #
def neglect_score(
    fields: dict, weights: dict = NEGLECT_WEIGHTS, stale_days: int = STALE_POST_DAYS
) -> tuple[float, dict]:
    """Return (score 0..100, {signal: bool}). A signal fires only on evidence: unknown
    (None) fields do NOT fire, so missing data never inflates the score."""
    dsp = fields.get("days_since_post")
    posts = fields.get("post_count")
    stale = (posts == 0) or (dsp is not None and dsp > stale_days) if posts is not None else False

    votes, photos = fields.get("rating_votes"), fields.get("total_photos")
    add_cats, avail = fields.get("additional_categories_count"), fields.get("attr_available_count")
    fired = {
        "unclaimed": fields.get("is_claimed") == 0,
        "stale_posts": stale,
        "thin_reviews": votes is not None and votes < 10,
        "few_photos": photos is not None and photos < 10,
        "no_secondary_categories": add_cats == 0,
        "no_hours": fields.get("has_hours") == 0,
        "sparse_attributes": avail is not None and avail < 3,
        "no_description": fields.get("has_description") == 0,
    }
    # A signal only counts if it fired AND carries weight. A zero-weight signal (the
    # neutralized 'unclaimed') never enters the score or the returned signal set, so it
    # cannot leak into a downstream customer-facing renderer (SEC-D).
    scored = {k: on for k, on in fired.items() if on and weights.get(k, 0) > 0}
    score = sum(weights[k] for k in scored)
    return float(min(score, 100)), scored


def estimate_cost(n_prospects: int, priority: str, with_updates: bool) -> dict:
    if priority not in INFO_RATE_USD:
        raise ValueError(f"priority must be one of {sorted(INFO_RATE_USD)}")
    per = INFO_RATE_USD[priority]
    if with_updates:
        upd_pri = priority if priority in UPDATES_SETUP_USD else "standard"
        per += UPDATES_SETUP_USD[upd_pri] + UPDATES_PER_10_USD[upd_pri]  # assume <=10 posts
    return {
        "prospects": n_prospects,
        "per_prospect_usd": round(per, 6),
        "usd": round(n_prospects * per, 4),
    }


# --------------------------------------------------------------------------- #
# Client seam — inject a fake in tests, DataForSEO in prod.                     #
# --------------------------------------------------------------------------- #
class GbpClient(Protocol):
    def fetch_info(
        self, place_id: str, lat: Optional[float], lng: Optional[float], priority: str
    ) -> dict: ...
    def fetch_updates(
        self, place_id: str, lat: Optional[float], lng: Optional[float], priority: str
    ) -> dict: ...


class DataForSEOGbpClient:
    """Wired to dataforseo.py (shared .env/Basic auth). Business Data endpoints use the
    task queue (task_post -> poll task_get); my_business_info also has a live mode but
    my_business_updates does not, so both go through one task runner for consistency.

    CONFIRM AGAINST THE FIRST LIVE TASK (geo_grid precedent): the task response contract
    (status 20100 on post, result under tasks[0].result, cost on tasks[0].cost) and the
    item field names the extractors read. Match is validated by place_id downstream.
    """

    INFO = "/v3/business_data/google/my_business_info"
    UPDATES = "/v3/business_data/google/my_business_updates"

    def __init__(self, *, language_code="en", poll_interval=5.0, poll_timeout=180.0):
        import requests  # local import so --dry-run / tests never need it
        import dataforseo as dfs

        auth = dfs.creds()
        if not auth:
            raise SystemExit("DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD not set in .env.")
        self._requests = requests
        self._base = dfs.BASE
        self._auth = auth
        self.language_code = language_code
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    def _payload(self, place_id, lat, lng, priority):
        p = {"keyword": f"place_id:{place_id}", "language_code": self.language_code}
        # A location is required; the stored pin gives an exact, business-local one.
        if lat is not None and lng is not None:
            p["location_coordinate"] = f"{lat},{lng},1000"
        else:
            p["location_name"] = "United States"
        p["priority"] = 2 if priority == "priority" else 1
        return p

    def _run_task(self, prefix, payload) -> dict:
        rq, auth = self._requests, self._auth
        r = rq.post(f"{self._base}{prefix}/task_post", auth=auth, json=[payload], timeout=60)
        r.raise_for_status()
        task = (r.json().get("tasks") or [{}])[0]
        task_id = task.get("id")
        if not task_id:
            raise RuntimeError(f"task_post returned no id: {task.get('status_message')}")
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            g = rq.get(f"{self._base}{prefix}/task_get/{task_id}", auth=auth, timeout=60)
            g.raise_for_status()
            gt = (g.json().get("tasks") or [{}])[0]
            if gt.get("status_code") == 20000:
                result = gt.get("result") or []
                items = (result[0].get("items") or []) if result else []
                return {"items": items, "cost": gt.get("cost") or 0.0}
            time.sleep(self.poll_interval)
        raise TimeoutError(f"task {task_id} not ready after {self.poll_timeout}s")

    def fetch_info(self, place_id, lat, lng, priority):
        return self._run_task(self.INFO, self._payload(place_id, lat, lng, priority))

    def fetch_updates(self, place_id, lat, lng, priority):
        upd_pri = priority if priority in UPDATES_SETUP_USD else "standard"
        return self._run_task(self.UPDATES, self._payload(place_id, lat, lng, upd_pri))

    # --- Bulk submit/collect (for large runs on the slow/standard queue) ---
    # task_get is free, so polling is cheap; the charge is at task_post. Codes that mean
    # "still working": 40602 In Queue, 40100/40101/40102 In Progress/Handed.
    IN_PROGRESS = {40100, 40101, 40102, 40601, 40602}

    def submit_batch(self, prefix, leads, priority, chunk=100, extra=None):
        """POST tasks in chunks (<=100/call, DataForSEO's cap). Tags each task with its
        place_id. `extra` merges endpoint-specific params (e.g. depth/sort_by for reviews).
        Returns ({place_id: task_id}, post_cost)."""
        rq, auth = self._requests, self._auth
        submitted, post_cost = {}, 0.0
        for i in range(0, len(leads), chunk):
            batch = leads[i : i + chunk]
            payload = []
            for lead in batch:
                p = self._payload(lead["place_id"], lead["lat"], lead["lng"], priority)
                p["tag"] = lead["place_id"]
                if extra:
                    p.update(extra)
                payload.append(p)
            r = rq.post(f"{self._base}{prefix}/task_post", auth=auth, json=payload, timeout=120)
            r.raise_for_status()
            data = r.json()
            post_cost += data.get("cost") or 0.0
            for lead, task in zip(batch, data.get("tasks") or [], strict=False):
                if task.get("id") and task.get("status_code") in (20000, 20100):
                    submitted[lead["place_id"]] = task["id"]
        return submitted, post_cost

    def collect_batch(self, prefix, submitted, deadline_s=2400, poll_interval=15, log=print):
        """Poll task_get for each submitted id until all resolve or the deadline passes.
        Returns ({place_id: {items, cost, [error]}}, pending) where pending never completed."""
        import time

        rq, auth = self._requests, self._auth
        results, pending = {}, dict(submitted)
        start = time.monotonic()
        while pending and time.monotonic() - start < deadline_s:
            done = []
            for pid, tid in list(pending.items()):
                try:
                    g = rq.get(f"{self._base}{prefix}/task_get/{tid}", auth=auth, timeout=60)
                    g.raise_for_status()
                    gt = (g.json().get("tasks") or [{}])[0]
                except Exception:  # noqa: BLE001 — transient; retry next round
                    continue
                sc = gt.get("status_code")
                if sc == 20000:
                    res = gt.get("result") or []
                    results[pid] = {
                        "items": (res[0].get("items") or []) if res else [],
                        "cost": gt.get("cost") or 0.0,
                    }
                    done.append(pid)
                elif sc not in self.IN_PROGRESS:
                    results[pid] = {
                        "items": [],
                        "cost": gt.get("cost") or 0.0,
                        "error": gt.get("status_message"),
                    }
                    done.append(pid)
            for pid in done:
                del pending[pid]
            log(f"  collected {len(results)}/{len(submitted)}, {len(pending)} pending")
            if pending:
                time.sleep(poll_interval)
        return results, pending


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def audit_one(client, lead, *, priority, with_updates, now) -> tuple[dict, float, str]:
    """Audit a single prospect. Returns (merged_fields, actual_cost, status). Never raises
    on a missing profile: an empty info result -> status 'no_data' with null fields."""
    pid, lat, lng = lead["place_id"], lead["lat"], lead["lng"]
    info_res = client.fetch_info(pid, lat, lng, priority)
    cost = info_res.get("cost") or 0.0
    info = extract_info(info_res.get("items"), expected_place_id=pid)
    if not info["found"]:
        return {}, cost, "no_data"
    updates = {"post_count": None, "last_post_ts": None, "days_since_post": None}
    if with_updates:
        upd_res = client.fetch_updates(pid, lat, lng, priority)
        cost += upd_res.get("cost") or 0.0
        updates = extract_updates(upd_res.get("items"), now)
    return {**info, **updates}, cost, "complete"


def run_audit(
    *,
    conn,
    client: Optional[GbpClient],
    leads,
    audit_type,
    priority,
    with_updates,
    dry_run,
    budget_usd,
    skip_since_ts=None,
    now=None,
    log=print,
) -> dict:
    """Audit each lead. In --dry-run makes zero API calls. Live mode stops cleanly before the
    request that would exceed budget_usd (remaining leads left un-audited, status 'partial')."""
    import leads_db_gbp as gdb

    now = now or datetime.now(timezone.utc)

    est = estimate_cost(len(leads), priority, with_updates)
    log(
        f"prospects={est['prospects']} priority={priority} updates={with_updates} "
        f"-> est ${est['usd']:.4f} (${est['per_prospect_usd']:.5f}/prospect)"
    )
    if dry_run:
        return {"dry_run": True, **est}

    if budget_usd is None:
        raise SystemExit(
            "Refusing to run live without --budget. Add --budget <usd> or use --dry-run."
        )

    per_est = est["per_prospect_usd"]
    guard = 0.0  # deterministic wallet cap: pre-charged per prospect, never under-counts
    actual = 0.0  # true spend read back from responses, for COGS
    audited = skipped = 0
    counts = {"complete": 0, "no_data": 0, "error": 0}
    run_status = "complete"
    for lead in leads:
        pid = lead["place_id"]
        if skip_since_ts and gdb.recently_audited(conn, pid, skip_since_ts):
            skipped += 1
            continue
        if guard + per_est > budget_usd:
            run_status = "partial"
            log(f"budget stop: ${actual:.4f} spent, next prospect would exceed ${budget_usd:.2f}")
            break
        try:
            fields, cost, status = audit_one(
                client, lead, priority=priority, with_updates=with_updates, now=now
            )
        except Exception as e:  # noqa: BLE001 — one bad lead must not sink the batch
            log(f"  ! {pid}: {e}")
            gdb.insert_gbp_audit(
                conn,
                place_id=pid,
                audit_type=audit_type,
                fields={},
                neglect_score=None,
                signals={},
                api_cost_usd=0.0,
                status="error",
            )
            counts["error"] += 1
            guard += per_est
            continue
        actual += cost
        guard += max(cost, per_est)  # if a call cost more than estimated, stop sooner next time
        score, signals = neglect_score(fields) if status == "complete" else (None, {})
        gdb.insert_gbp_audit(
            conn,
            place_id=pid,
            audit_type=audit_type,
            fields=fields,
            neglect_score=score,
            signals=signals,
            api_cost_usd=cost,
            status=status,
        )
        counts[status] += 1
        audited += 1
        if status == "complete":
            log(
                f"  OK {lead.get('name') or pid}: neglect={score:.0f} "
                f"{'unclaimed ' if signals.get('unclaimed') else ''}"
                f"({', '.join(signals) or 'no signals'})"
            )
    return {
        "status": run_status,
        "audited": audited,
        "skipped": skipped,
        "cost_usd": round(actual, 4),
        "counts": counts,
    }


def run_audit_batch(
    *,
    conn,
    client,
    leads,
    audit_type,
    priority,
    dry_run,
    budget_usd,
    skip_since_ts=None,
    now=None,
    deadline_s=2400,
    log=print,
) -> dict:
    """INFO-ONLY bulk path for large runs: submit every task up front, then collect from the
    queue (the slow/standard-queue-friendly pattern). Budget caps the number SUBMITTED (the
    charge is at submission). Un-collected tasks (deadline) are left for a --skip-existing resume."""
    import leads_db_gbp as gdb

    now = now or datetime.now(timezone.utc)
    if skip_since_ts:
        leads = [
            ld for ld in leads if not gdb.recently_audited(conn, ld["place_id"], skip_since_ts)
        ]

    per = INFO_RATE_USD[priority]
    est = {
        "prospects": len(leads),
        "per_prospect_usd": round(per, 6),
        "usd": round(len(leads) * per, 4),
    }
    log(
        f"[batch] info-only prospects={est['prospects']} priority={priority} -> est ${est['usd']:.4f}"
    )
    if dry_run:
        return {"dry_run": True, **est}
    if budget_usd is None:
        raise SystemExit(
            "Refusing to run live without --budget. Add --budget <usd> or use --dry-run."
        )

    max_tasks = int(budget_usd / per)
    submit = leads[:max_tasks]
    if len(submit) < len(leads):
        log(f"budget ${budget_usd:.2f} caps submission to {len(submit)}/{len(leads)} tasks")
    log(f"submitting {len(submit)} info tasks (chunks of 100)...")
    submitted, _post_cost = client.submit_batch(client.INFO, submit, priority)
    log(f"submitted {len(submitted)}; collecting (deadline {deadline_s}s)...")
    results, pending = client.collect_batch(client.INFO, submitted, deadline_s=deadline_s, log=log)

    counts = {"complete": 0, "no_data": 0, "error": 0}
    actual = 0.0
    for pid in submitted:
        res = results.get(pid)
        if res is None:  # timed out; resume later with --skip-existing off for these
            continue
        actual += per  # Standard info rate is the reliable per-task COGS
        info = extract_info(res.get("items"), expected_place_id=pid)
        if res.get("error") or not info["found"]:
            gdb.insert_gbp_audit(
                conn,
                place_id=pid,
                audit_type=audit_type,
                fields={},
                neglect_score=None,
                signals={},
                api_cost_usd=per,
                status="error" if res.get("error") else "no_data",
            )
            counts["error" if res.get("error") else "no_data"] += 1
            continue
        fields = {**info, "post_count": None, "last_post_ts": None, "days_since_post": None}
        score, signals = neglect_score(fields)
        gdb.insert_gbp_audit(
            conn,
            place_id=pid,
            audit_type=audit_type,
            fields=fields,
            neglect_score=score,
            signals=signals,
            api_cost_usd=per,
            status="complete",
        )
        counts["complete"] += 1
    log(
        f"[batch] done: {counts['complete']} complete, {counts['no_data']} no_data, "
        f"{counts['error']} error, {len(pending)} un-collected; ${actual:.4f}"
    )
    return {
        "submitted": len(submitted),
        "collected": len(results),
        "pending": len(pending),
        "cost_usd": round(actual, 4),
        "counts": counts,
    }


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def _select_leads(conn, args):
    """Prospects to audit, from the businesses store, keyed on place_id (has lat/lng).
    A --place-ids-file targets an exact cohort (via a temp table, so it isn't capped by
    SQLite's bound-variable limit); else filter by --place-id or --relevance."""
    where = ["place_id IS NOT NULL", "lat IS NOT NULL", "lng IS NOT NULL"]
    params = []
    if getattr(args, "place_ids_file", None):
        ids = [
            ln.strip()
            for ln in Path(args.place_ids_file).read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _cohort (place_id TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO _cohort VALUES (?)", [(i,) for i in ids])
        where.append("place_id IN (SELECT place_id FROM _cohort)")
    elif args.place_id:
        where.append("place_id = ?")
        params.append(args.place_id)
    elif args.relevance != "all":
        where.append("relevance = ?")
        params.append(args.relevance)
    sql = (
        "SELECT place_id, name, lat, lng FROM businesses WHERE "
        + " AND ".join(where)
        + " ORDER BY COALESCE(review_count, 0) DESC, place_id"
    )
    if args.limit:
        sql += " LIMIT ?"
        params.append(args.limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Prospect-side GBP neglect audit (place_id-matched, DataForSEO)."
    )
    ap.add_argument("--place-id", help="Audit a single prospect by place_id")
    ap.add_argument(
        "--place-ids-file", help="Audit exactly the place_ids in this file (one per line)"
    )
    ap.add_argument(
        "--relevance",
        choices=["match", "maybe", "all"],
        default="match",
        help="Which businesses to audit (default: confirmed-trade 'match')",
    )
    ap.add_argument(
        "--audit-type", choices=["prospect", "baseline", "monthly", "adhoc"], default="prospect"
    )
    ap.add_argument("--priority", choices=["standard", "priority"], default="standard")
    ap.add_argument(
        "--no-updates", action="store_true", help="Skip the posts endpoint (cheaper, no recency)"
    )
    ap.add_argument(
        "--batch",
        action="store_true",
        help="Bulk submit/collect, INFO-ONLY (for large runs on the standard queue).",
    )
    ap.add_argument("--deadline", type=int, default=2400, help="Batch collect deadline in seconds.")
    ap.add_argument(
        "--skip-existing-days",
        type=int,
        default=None,
        help="Skip leads audited within N days (resumable / no re-spend)",
    )
    ap.add_argument("--limit", type=int, help="Cap the number of prospects")
    ap.add_argument("--dry-run", action="store_true", help="Print cost estimate; make no API calls")
    ap.add_argument("--budget", type=float, help="HARD cap in USD; required for a live run")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args(argv)

    import sqlite3

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    import leads_db_gbp as gdb

    gdb.create_gbp_tables(conn)  # no-op if leads_db already created it

    leads = _select_leads(conn, args)
    if not leads:
        print("No matching prospects (need businesses with place_id + lat/lng).", file=sys.stderr)
        return 1
    with_updates = not args.no_updates

    skip_since = None
    if args.skip_existing_days:
        from datetime import timedelta

        skip_since = (
            datetime.now(timezone.utc) - timedelta(days=args.skip_existing_days)
        ).isoformat(timespec="seconds")

    if args.dry_run:
        if args.batch:
            run_audit_batch(
                conn=conn,
                client=None,
                leads=leads,
                audit_type=args.audit_type,
                priority=args.priority,
                dry_run=True,
                budget_usd=None,
                skip_since_ts=skip_since,
            )
        else:
            run_audit(
                conn=conn,
                client=None,
                leads=leads,
                audit_type=args.audit_type,
                priority=args.priority,
                with_updates=with_updates,
                dry_run=True,
                budget_usd=None,
            )
        return 0

    if args.budget is None:
        raise SystemExit(
            "Refusing to run live without --budget. Add --budget <usd> or use --dry-run."
        )

    print(
        "note: DataForSEO Business Data calls cost credits — running deliberately.", file=sys.stderr
    )
    client = DataForSEOGbpClient()
    if args.batch:
        out = run_audit_batch(
            conn=conn,
            client=client,
            leads=leads,
            audit_type=args.audit_type,
            priority=args.priority,
            dry_run=False,
            budget_usd=args.budget,
            skip_since_ts=skip_since,
            deadline_s=args.deadline,
        )
    else:
        out = run_audit(
            conn=conn,
            client=client,
            leads=leads,
            audit_type=args.audit_type,
            priority=args.priority,
            with_updates=with_updates,
            dry_run=False,
            budget_usd=args.budget,
            skip_since_ts=skip_since,
        )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
