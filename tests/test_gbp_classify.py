"""
Tests for gbp_classify.py — no network, no spend. Faked classifier client (the gbp_audit /
geo_grid seam pattern). Run standalone (`python tests/test_gbp_classify.py`) or via pytest.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gbp_classify as gc


# --------------------------- pure: parse_flags --------------------------- #
def test_parse_flags_tolerates_preamble_and_normalizes():
    reply = (
        "batch 03: flagged 1 of 2 businesses\n"
        '[{"place_id":"P0","name":"Ace","complaint_count":2,'
        '"complaint_types":["no_callback","unresponsive"],'
        '"evidence":["' + "x" * 200 + '","q2","q3"]}]'
    )
    flags = gc.parse_flags(reply)
    assert len(flags) == 1
    f = flags[0]
    assert f["place_id"] == "P0" and f["complaint_count"] == 2
    assert f["complaint_types"] == ["no_callback", "unresponsive"]
    assert len(f["evidence"]) == 2 and len(f["evidence"][0]) == 160  # clamped length + count


def test_parse_flags_drops_bad_rows_and_types():
    reply = (
        '[{"name":"no id here"},'  # dropped: no place_id
        '{"place_id":"P1","complaint_types":["price","no_show"],"evidence":[]}]'
    )
    flags = gc.parse_flags(reply)
    assert len(flags) == 1
    assert flags[0]["complaint_types"] == ["no_show"]  # unknown "price" filtered out
    assert flags[0]["complaint_count"] == 1  # fallback when absent/empty evidence


def test_parse_flags_raises_without_array():
    try:
        gc.parse_flags("the model refused and wrote prose")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# --------------------------- pure: cost --------------------------- #
def test_cost_from_usage_and_estimate():
    c = gc.cost_from_usage({"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    assert abs(c - (gc.RATE_USD_PER_MTOK["input"] + gc.RATE_USD_PER_MTOK["output"])) < 1e-9
    est = gc.estimate_batch_cost(
        [
            {
                "place_id": "P",
                "name": "B",
                "negative_reviews": [{"rating": 1, "when": "x", "text": "y"}],
            }
        ]
    )
    assert est["usd"] > 0 and est["input_tokens"] > 0


# --------------------------- fake client + e2e --------------------------- #
class FakeClassifier:
    REPLY = (
        '[{"place_id":"X","name":"Biz","complaint_count":1,'
        '"complaint_types":["no_callback"],"evidence":["never called back"]}]'
    )

    def __init__(self, usage=None):
        self.usage = usage or {"input_tokens": 500, "output_tokens": 80}
        self.calls = 0

    def classify(self, businesses):
        self.calls += 1
        return self.REPLY, self.usage


def _make_batches(root, n_batches=2, per=3):
    bdir = Path(root) / "batches"
    bdir.mkdir(parents=True)
    for i in range(n_batches):
        lines = [
            json.dumps(
                {
                    "place_id": f"P{i}_{j}",
                    "name": f"Biz {i}-{j}",
                    "negative_reviews": [
                        {"rating": 1, "when": "a month ago", "text": "never called back"}
                    ],
                }
            )
            for j in range(per)
        ]
        (bdir / f"batch_{i:02d}.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return bdir


def test_run_classify_dry_run_makes_no_calls():
    with tempfile.TemporaryDirectory() as d:
        bdir = _make_batches(d, n_batches=2, per=3)
        client = FakeClassifier()
        out = gc.run_classify(
            batches_dir=bdir,
            out_dir=Path(d) / "flags",
            client=client,
            model="m",
            dry_run=True,
            budget_usd=None,
            log=lambda *_: None,
        )
        assert out["dry_run"] is True and out["batches"] == 2 and out["businesses"] == 6
        assert client.calls == 0


def test_run_classify_writes_flag_files():
    with tempfile.TemporaryDirectory() as d:
        bdir = _make_batches(d, n_batches=2, per=3)
        fdir = Path(d) / "flags"
        client = FakeClassifier()
        out = gc.run_classify(
            batches_dir=bdir,
            out_dir=fdir,
            client=client,
            model="m",
            dry_run=False,
            budget_usd=5.0,
            log=lambda *_: None,
        )
        assert out["status"] == "complete" and out["classified_batches"] == 2
        assert client.calls == 2
        files = sorted(fdir.glob("batch_*.json"))
        assert [f.name for f in files] == ["batch_00.json", "batch_01.json"]
        arr = json.loads(files[0].read_text(encoding="utf-8"))
        assert arr[0]["place_id"] == "X" and arr[0]["complaint_types"] == ["no_callback"]


def test_run_classify_budget_stops_before_first_batch():
    with tempfile.TemporaryDirectory() as d:
        bdir = _make_batches(d, n_batches=2, per=3)
        biz = gc._read_batch(bdir / "batch_00.jsonl")
        tiny = gc.estimate_batch_cost(biz)["usd"] * 0.5  # under one batch's estimate
        client = FakeClassifier()
        out = gc.run_classify(
            batches_dir=bdir,
            out_dir=Path(d) / "flags",
            client=client,
            model="m",
            dry_run=False,
            budget_usd=tiny,
            log=lambda *_: None,
        )
        assert out["status"] == "partial" and out["classified_batches"] == 0
        assert client.calls == 0


def test_run_classify_skips_existing():
    with tempfile.TemporaryDirectory() as d:
        bdir = _make_batches(d, n_batches=2, per=3)
        fdir = Path(d) / "flags"
        fdir.mkdir()
        (fdir / "batch_00.json").write_text("[]", encoding="utf-8")  # pretend already done
        client = FakeClassifier()
        out = gc.run_classify(
            batches_dir=bdir,
            out_dir=fdir,
            client=client,
            model="m",
            dry_run=False,
            budget_usd=5.0,
            log=lambda *_: None,
        )
        assert out["skipped"] == 1 and out["classified_batches"] == 1
        assert client.calls == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
