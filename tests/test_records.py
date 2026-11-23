"""Isolated tests for safe ledger appends, multi-layer eval routing, and externalization."""

from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opsflow.config import OpsContext, default_config, load_context
from opsflow.index import search
from opsflow.records import add_result, append_record
from opsflow.storage import atomic_text, sha256_file


def _append_worker(path: str, count: int, worker: int) -> None:
    for number in range(count):
        append_record(Path(path), {"trace_id": f"worker_{worker}_{number}", "summary": "event"})


class RecordsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = load_context(root=self.root)

    def _layered_ctx(self) -> OpsContext:
        cfg = default_config(
            project_name="Layered",
            eval_layers={
                "offline": "evals/offline_eval_log.jsonl",
                "staging": "evals/staging_eval_log.jsonl",
                "prod": "evals/prod_eval_log.jsonl",
            },
            layer_modes={"offline": ["unit", "benchmark"], "prod": ["canary", "live"]},
        )
        return OpsContext(root=self.root, config_path=self.root / ".opsflow/config.json", config=cfg)

    def test_append_idempotency_conflicts_and_legacy_bytes(self) -> None:
        path = self.root / "trace_log.jsonl"
        payload = {"trace_id": "one", "ts": "before", "status": "custom_legacy", "kind": "custom"}
        self.assertTrue(append_record(path, payload))
        self.assertFalse(append_record(path, {**payload, "ts": "after"}))
        with self.assertRaisesRegex(ValueError, "conflicting content"):
            append_record(path, {**payload, "status": "changed"})
        with self.assertRaises(ValueError):
            append_record(path, {"trace_id": "bad", "metric": float("nan")})
        atomic_text(path, path.read_text(encoding="utf-8") * 2)
        with self.assertRaisesRegex(ValueError, "duplicate ID"):
            append_record(path, payload)
        self.assertTrue(append_record(path, {"trace_id": "unrelated"}))

    def test_append_preserves_legacy_bytes_and_refuses_corrupt_line(self) -> None:
        path = self.root / "trace_log.jsonl"
        previous = '{ "trace_id": "legacy", "status":"untyped" }'
        path.write_text(previous, encoding="utf-8")
        append_record(path, {"trace_id": "next"})
        self.assertTrue(path.read_text(encoding="utf-8").startswith(previous + "\n"))
        path.write_text(path.read_text(encoding="utf-8") + '{"trace_id":', encoding="utf-8")
        corrupt = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "corrupt ledger"):
            append_record(path, {"trace_id": "new"})
        self.assertEqual(path.read_bytes(), corrupt)

    def test_concurrent_append_keeps_all_unique_records(self) -> None:
        path = self.root / "trace_log.jsonl"
        mp = multiprocessing.get_context("fork")
        procs = [mp.Process(target=_append_worker, args=(str(path), 15, worker)) for worker in range(4)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(15)
            self.assertEqual(proc.exitcode, 0)
        ids = [json.loads(line)["trace_id"] for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(ids), 60)
        self.assertEqual(len(set(ids)), 60)


    def test_result_default_single_ledger_and_strict_metrics(self) -> None:
        metric = {"value": 0.94, "unit": "ratio", "aggregation": "macro_avg"}
        record = add_result(self.ctx, {
            "track_id": "parser", "eval_id": "eval_001", "evidence_layer": "offline",
            "execution_mode": "benchmark", "summary": "Benchmark passed",
            "metrics": {"f1": metric}, "commit": "abc123", "artifacts": ["report.md"],
        })
        self.assertEqual(record["metrics"]["f1"], metric)
        self.assertEqual(record["extra"]["commit"], "abc123")
        self.assertEqual(record["evidence_layer"], "offline")
        self.assertTrue((self.root / "ops/evals/eval_log.jsonl").exists())
        with self.assertRaisesRegex(ValueError, "status"):
            add_result(self.ctx, {"track_id": "parser", "status": "maybe"})
        with self.assertRaisesRegex(ValueError, "unit"):
            add_result(self.ctx, {"track_id": "parser", "metrics": {"loss": {"value": 1}}})
        with self.assertRaisesRegex(ValueError, "aggregation"):
            add_result(self.ctx, {"track_id": "parser", "metrics": {"loss": {"value": 1, "unit": "scalar"}}})
        with self.assertRaisesRegex(ValueError, "bare numbers"):
            add_result(self.ctx, {"track_id": "parser", "metrics": {"loss": 0.2}})

    def test_configured_eval_layers_route_and_enforce_modes(self) -> None:
        ctx = self._layered_ctx()
        rec = add_result(ctx, {
            "track_id": "release", "eval_id": "prod_1", "evidence_layer": "prod",
            "execution_mode": "canary", "summary": "Canary healthy",
            "metrics": {"p99_ms": {"value": 42.5, "unit": "ms", "aggregation": "p99_1h"}},
        })
        self.assertEqual(rec["eval_id"], "prod_1")
        self.assertTrue((self.root / "ops/evals/prod_eval_log.jsonl").exists())
        self.assertFalse((self.root / "ops/evals/offline_eval_log.jsonl").exists())
        with self.assertRaisesRegex(ValueError, "eval_layers"):
            add_result(ctx, {"track_id": "release", "evidence_layer": "unknown"})
        with self.assertRaisesRegex(ValueError, "execution_mode"):
            add_result(ctx, {"track_id": "release", "evidence_layer": "offline", "execution_mode": "live"})

    def test_large_result_externalized_with_digest_retry_and_tags(self) -> None:
        payload = {
            "track_id": "perf", "eval_id": "large_1", "evidence_layer": "offline",
            "metrics": {f"m_{i}": {"value": i, "unit": "ms", "aggregation": "mean"} for i in range(20)},
            "tags": ["perf", "candidate"],
            "details": ["detail-block" * 1000 for _ in range(20)],
        }
        record = add_result(self.ctx, payload)
        detail_path = self.ctx.ops_dir / record["detail_ref"]["path"]
        full = json.loads(detail_path.read_text(encoding="utf-8"))
        self.assertEqual(full["extra"]["details"], payload["details"])
        self.assertEqual(len(full["metrics"]), 20)
        self.assertEqual(len(record["metrics"]), 10)
        self.assertEqual(record["detail_ref"]["sha256"], sha256_file(detail_path))
        self.assertEqual(record["tags"], ["perf", "candidate"])
        log = self.root / "ops/evals/eval_log.jsonl"
        self.assertLessEqual(len(log.read_bytes()), 16 * 1024)
        with patch("opsflow.records.now_iso", return_value="2099-01-01T00:00:00+00:00"):
            self.assertEqual(add_result(self.ctx, payload), record)
        self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)
        self.assertEqual(search(self.ctx, tag="candidate")["total"], 1)
        old_bytes = detail_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "conflicting content"):
            add_result(self.ctx, {**payload, "details": ["changed"] * 10000})
        self.assertEqual(detail_path.read_bytes(), old_bytes)

    def test_interrupted_externalized_result_and_content_derived_id(self) -> None:
        payload = {"track_id": "perf", "eval_id": "pending", "details": "x" * 30000}
        with patch("opsflow.records.append_record", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                add_result(self.ctx, payload)
        self.assertTrue((self.root / "ops/tracks/perf/results/pending.json").exists())
        record = add_result(self.ctx, payload)
        self.assertEqual(record["eval_id"], "pending")
        base = {"track_id": "perf", "run_id": "r1",
                "metrics": {"acc": {"value": 0.9, "unit": "ratio", "aggregation": "mean"}}}
        first = add_result(self.ctx, {**base, "ts": "2026-11-11T12:00:00+00:00", "recorded_at": "t1"})
        second = add_result(self.ctx, {**base, "ts": "2026-11-11T13:00:00+00:00", "recorded_at": "t2"})
        self.assertEqual(first["eval_id"], second["eval_id"])
        changed = add_result(self.ctx, {**base, "run_id": "r2"})
        self.assertNotEqual(first["eval_id"], changed["eval_id"])


if __name__ == "__main__":
    unittest.main()
