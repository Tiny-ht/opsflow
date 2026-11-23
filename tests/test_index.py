"""Isolated tests for rebuildable SQLite search index, incremental refresh, and edge cases."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from opsflow import index
from opsflow.config import OpsContext, default_config


def _concurrent_index(root: str) -> tuple[int, int]:
    cfg = default_config(project_name="Concurrent", tz="+08:00")
    ctx = OpsContext(root=Path(root), config_path=Path(root) / ".opsflow/config.json", config=cfg)
    result = index.reindex(ctx)
    return result["records"], index.search(ctx)["total"]


class IndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cfg = default_config(project_name="IndexTest", tz="+08:00")
        self.ctx = OpsContext(root=self.root, config_path=self.root / ".opsflow/config.json", config=cfg)

    def write(self, relative: str, text: str) -> Path:
        path = self.ctx.ops_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def ledger(self, relative: str, records: list[dict], newline: bool = True) -> Path:
        text = "\n".join(json.dumps(row, ensure_ascii=False) for row in records)
        return self.write(relative, text + ("\n" if newline else ""))

    def test_heterogeneous_records_without_invented_metadata(self) -> None:
        self.ledger("evals/eval_log.jsonl", [
            {"eval_id": "eval-1", "date": "2026-09-01", "summary": ["Eval done", "Offline only"],
             "artifacts": {"report": "full-raw-evidence"}},
        ])
        self.ledger("research/reference_intake.jsonl", [{"reference_id": "ref-1", "notes": "paper note"}])
        self.ledger("trace/trace_log.jsonl", [{"unfamiliar": {"nested": "any-field-searchable"}}])
        results = index.search(self.ctx)
        self.assertEqual(results["total"], 3)
        self.assertEqual(results["records"][0]["id"], "eval-1")
        self.assertEqual(results["records"][0]["summary"], "Eval done; Offline only")
        self.assertIsNone(results["records"][0]["evidence_layer"])
        self.assertEqual(index.search(self.ctx, keyword="full-raw-evidence")["total"], 1)
        unknown = index.search(self.ctx, keyword="any-field-searchable")["records"][0]
        self.assertIsNone(unknown["time"])
        self.assertIsNone(unknown["track_id"])
        self.assertIsNone(unknown["evidence_layer"])
        self.assertEqual(index.get_record(self.ctx, "ref-1")["record"]["notes"], "paper note")

    def test_time_sorting_and_iso_date_range_in_project_tz(self) -> None:
        self.ledger("trace/trace_log.jsonl", [
            {"id": "before", "ts": "2026-11-10T15:59:59Z"},
            {"id": "start", "ts": "2026-11-10T16:00:00Z"},
            {"id": "end", "ts": "2026-11-11T23:59:59.999999+08:00"},
            {"id": "after", "ts": "2026-11-12T00:00:00+08:00"},
            {"id": "plain-date", "date": "2026-11-11"},
            {"id": "unknown", "ts": "yesterday"},
        ])
        rows = index.search(self.ctx)["records"]
        self.assertEqual(rows[-1]["id"], "unknown")
        self.assertIsNone(rows[-1]["time"])
        day = index.search(self.ctx, since="2026-11-11", until="2026-11-11")["records"]
        self.assertEqual({r["id"] for r in day}, {"start", "end", "plain-date"})
        for bad in ({"since": "bad"}, {"since": "2026-11-12", "until": "2026-11-11"}, {"tag": ""}):
            with self.assertRaises(ValueError):
                index.search(self.ctx, **bad)


    def test_layered_eval_ledgers_and_legacy_coexistence(self) -> None:
        cfg = default_config(
            project_name="Layered",
            eval_layers={"offline": "evals/offline_eval_log.jsonl", "prod": "evals/prod_eval_log.jsonl"},
            tz="+08:00",
        )
        ctx = OpsContext(root=self.root, config_path=self.root / ".opsflow/config.json", config=cfg)
        self.ledger("evals/offline_eval_log.jsonl", [{"eval_id": "off-1", "trace_id": "t-1"}])
        self.ledger("evals/prod_eval_log.jsonl", [{"eval_id": "prod-1", "trace_id": "t-1"}])
        self.ledger("evals/eval_log.jsonl", [{"eval_id": "legacy-1"}])
        self.ledger("trace/trace_log.jsonl", [{"trace_id": "t-1"}])
        self.assertEqual(index.search(ctx, source="eval")["total"], 3)
        self.assertEqual(index.search(ctx, source="prod")["total"], 1)
        self.assertEqual(index.search(ctx, evidence_layer="offline")["total"], 1)
        self.assertEqual(index.get_record(ctx, "prod-1")["meta"]["evidence_layer"], "prod")
        self.assertEqual(index.get_record(ctx, "t-1")["meta"]["source"], "trace")

    def test_filters_exact_tags_duplicate_ids_and_literal_keywords(self) -> None:
        self.ledger("trace/trace_log.jsonl", [
            {"id": "dup", "track_id": "m1", "status": "running", "kind": "launch", "summary": "100%_ok", "tags": ["Arm-A"]},
            {"id": "dup", "track_id": "m1", "status": "running", "kind": "launch", "summary": "100%_ok", "tags": ["arm-ab"]},
        ])
        self.ledger("evals/eval_log.jsonl", [
            {"id": "dup", "extra": {"tags": ["arm-a"]}},
            {"id": "u", "summary": "中文 Straße"},
        ])
        self.assertEqual(index.search(self.ctx, keyword="%_")["total"], 2)
        self.assertEqual(index.search(self.ctx, keyword="' OR 1=1 --")["total"], 0)
        self.assertEqual(index.search(self.ctx, tag="ARM-A")["total"], 2)
        self.assertEqual(index.search(self.ctx, keyword="中文 STRASSE")["total"], 1)
        with self.assertRaisesRegex(ValueError, "matches 3 records"):
            index.get_record(self.ctx, "dup")
        for row in index.search(self.ctx, keyword="100%_ok")["records"]:
            self.assertEqual(index.get_record(self.ctx, row["key"])["record"]["id"], "dup")

    def test_incremental_refresh_ctime_corrupt_lines_and_retired_docs(self) -> None:
        path = self.ledger("trace/trace_log.jsonl", [{"id": "old", "summary": "keep"}], newline=False)
        old_key = index.search(self.ctx)["records"][0]["key"]
        with patch.object(index, "_read_snapshot", side_effect=AssertionError("should not re-read")):
            self.assertEqual(index.reindex(self.ctx)["unchanged_files"], 1)
        old_stat = path.stat()
        self.ledger("trace/trace_log.jsonl", [{"id": "new", "summary": "same"}])
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        self.assertEqual(index.search(self.ctx)["records"][0]["id"], "new")
        self.write("trace/trace_log.jsonl", '{"id":"first"}\n{broken}\n[1,2]\n{"id":"last"}')
        stats = index.reindex(self.ctx)
        self.assertEqual(stats["records"], 2)
        self.assertEqual(stats["error_count"], 2)
        self.assertEqual(index.get_record(self.ctx, "last")["record"]["id"], "last")
        self.write("tracks/t1/track.json", json.dumps({"track_id": "t1", "title": "T1", "status": "active"}))
        self.write("lessons/net.md", "# Net rule\n\nAlways set timeouts.\n")
        self.write("current_state.md", "retired-should-not-be-indexed")
        self.assertEqual(index.search(self.ctx, keyword="retired-should-not-be-indexed")["total"], 0)
        self.assertEqual(index.search(self.ctx, source="lesson")["total"], 1)
        self.assertEqual(index.search(self.ctx, track_id="t1")["total"], 1)
        with self.assertRaises(KeyError):
            index.get_record(self.ctx, old_key)


    def test_large_payload_corrupt_recovery_rollback_and_concurrency(self) -> None:
        original = {"eval_id": "large", "metrics": {"blob": "x" * 500_000 + "unique-tail-token"}}
        self.ledger("evals/eval_log.jsonl", [original])
        found = index.search(self.ctx, keyword="unique-tail-token")
        self.assertEqual(found["total"], 1)
        self.assertLess(len(json.dumps(found).encode("utf-8")), 2000)
        self.assertGreater(found["records"][0]["read_bytes"], 500_000)
        self.assertEqual(index.get_record(self.ctx, "large")["record"], original)

        (self.ctx.index_dir / "ops.sqlite").write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "reindex --force"):
            index.reindex(self.ctx)
        self.assertEqual(index.reindex(self.ctx, force=True)["records"], 1)

        with patch.object(index, "_insert_source", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                index.reindex(self.ctx, force=True)
        self.assertEqual(index.get_record(self.ctx, "large")["record"]["eval_id"], "large")
        self.assertEqual(list(self.ctx.index_dir.glob("rebuild-*.sqlite")), [])

        self.ledger("trace/trace_log.jsonl", [{"id": "trace-old"}])
        index.reindex(self.ctx)
        self.ledger("trace/trace_log.jsonl", [{"id": "trace-new"}])
        orig_insert = index._insert_source

        def fail_trace(conn, source, path, raw, **kwargs):
            if source == "trace":
                raise RuntimeError("abort")
            return orig_insert(conn, source, path, raw, **kwargs)

        with patch.object(index, "_insert_source", side_effect=fail_trace):
            with self.assertRaisesRegex(RuntimeError, "abort"):
                index.reindex(self.ctx)
        with closing(sqlite3.connect(self.ctx.index_dir / "ops.sqlite")) as conn:
            ids = {row[0] for row in conn.execute("SELECT id FROM records WHERE source='trace'")}
        self.assertEqual(ids, {"trace-old"})

        self.ledger("trace/trace_log.jsonl", [{"id": str(i)} for i in range(50)])
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(_concurrent_index, [str(self.root)] * 6))
        self.assertEqual(results, [(51, 51)] * 6)


if __name__ == "__main__":
    unittest.main()
