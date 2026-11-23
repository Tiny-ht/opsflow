"""Isolated tests for bounded views (brief, context, query, show) and check."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from opsflow.config import OpsContext, default_config
from opsflow.index import search
from opsflow.records import add_result
from opsflow.storage import atomic_json, atomic_text
from opsflow.tracks import close_track, set_track
from opsflow.views import brief, check, context, observe_status, query_output, show


class ViewsAndCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cfg = default_config(project_name="DemoProject", tz="+08:00")
        self.ctx = OpsContext(root=self.root, config_path=self.root / ".opsflow/config.json", config=cfg)
        atomic_json(self.ctx.project_file, {
            "schema_version": 1,
            "baseline": ["Official baseline v1.0"],
            "constraints": ["Never treat offline metrics as prod verification"],
            "evidence_refs": [],
        })

    def _eval_ledger(self, records: list[dict]) -> None:
        atomic_text(
            self.ctx.eval_log_path(None),
            "".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in records),
        )

    def _large_record(self) -> None:
        self._eval_ledger([{
            "eval_id": "large_record", "ts": "2026-11-11T10:00:00+08:00",
            "summary": "Huge record requiring field directory and pagination",
            "metrics": {f"metric_{i}": {"value": i, "unit": "ms", "aggregation": "mean"} for i in range(1000)},
            "details": [{"step": i, "description": "detail-entry" * 20} for i in range(200)],
            "notes": "\n".join(f"Line {i} complete note." for i in range(200)),
            "a/b~c": {"value": "escaped"},
        }])

    def test_brief_stays_bounded_on_repeated_updates_and_exits_on_close(self) -> None:
        card = set_track(self.ctx, {"track_id": "one", "title": "Primary track", "status": "active",
                                    "next_actions": ["Verify final report"]})
        for idx in range(100):
            card = set_track(self.ctx, {"track_id": "one", "summary": f"Progress {idx}"}, card["revision"])
        text = brief(self.ctx, write=True)
        self.assertEqual(text.count("`one`"), 1)
        self.assertNotIn("Progress 99", text)
        self.assertLessEqual(len(text.encode("utf-8")), 12 * 1024)
        self.assertEqual(self.ctx.brief_file.read_text(encoding="utf-8"), text)
        close_track(self.ctx, "one", "Completed verification", expected_revision=card["revision"])
        closed = brief(self.ctx)
        self.assertNotIn("`one`", closed)
        self.assertIn("open: 0", closed)
        self.assertIn("does not confirm", closed)

    def test_brief_many_tracks_bounded_and_announces_omissions(self) -> None:
        for idx in range(40):
            set_track(self.ctx, {"track_id": f"track_{idx:03}", "title": "Long track description " * 8, "status": "active"})
        text = brief(self.ctx, budget=4096)
        self.assertLessEqual(len(text.encode("utf-8")), 4096)
        self.assertIn("omitted", text)
        self.assertIn("Registered tracks: 40; open: 40", text)


    def test_query_growth_stays_bounded_and_pages_advance_without_overlap(self) -> None:
        max_bytes = 0
        for count in (100, 1000, 10000):
            with self.subTest(records=count):
                self._eval_ledger([{
                    "eval_id": f"run_{i:05}", "ts": "2026-11-11T10:00:00+08:00",
                    "summary": "Complete evaluation finding. " * 12, "title": f"Run {i}",
                } for i in range(count)])
                first = search(self.ctx, source="eval", limit=100)
                rendered = query_output(first, budget=8192, as_json=True)
                max_bytes = max(max_bytes, len(rendered.encode("utf-8")))
                page1 = json.loads(rendered)
                self.assertEqual(page1["total"], count)
                self.assertGreater(len(page1["records"]), 0)
                self.assertEqual(page1["next_offset"], len(page1["records"]))
                second = search(self.ctx, source="eval", limit=100, offset=page1["next_offset"])
                page2 = json.loads(query_output(second, budget=8192, as_json=True))
                self.assertTrue({r["key"] for r in page1["records"]}.isdisjoint(r["key"] for r in page2["records"]))
                text = query_output(second, budget=4096, offset=page1["next_offset"])
                self.assertLessEqual(len(text.encode("utf-8")), 4096)
        self.assertLessEqual(max_bytes, 8192)

    def test_show_field_directory_pagination_escaping_and_detail_verification(self) -> None:
        self._large_record()
        catalog = json.loads(show(self.ctx, "large_record", budget=2048, limit=2))
        self.assertNotIn("record", catalog)
        self.assertEqual(len(catalog["fields"]), 2)
        self.assertEqual(catalog["next_offset"], 2)
        page = json.loads(show(self.ctx, "large_record", fields=["/details"], offset=3, limit=2, budget=4096))
        self.assertEqual([item["step"] for item in page["fields"]["/details"]], [3, 4])
        lines = json.loads(show(self.ctx, "large_record", fields=["/notes"], offset=3, limit=2))
        self.assertEqual(lines["fields"]["/notes"], ["Line 3 complete note.", "Line 4 complete note."])
        self.assertEqual(lines["pagination"]["/notes"]["unit"], "line")
        escaped = json.loads(show(self.ctx, "large_record", fields=["/a~1b~0c/value"]))
        self.assertEqual(escaped["fields"]["/a~1b~0c/value"], ["escaped"])
        metrics_page = json.loads(show(self.ctx, "large_record", fields=["/metrics"], offset=2, limit=3, budget=4096))
        self.assertEqual(list(metrics_page["fields"]["/metrics"]), ["metric_2", "metric_3", "metric_4"])
        self.assertEqual(metrics_page["pagination"]["/metrics"]["unit"], "key")
        for offset, limit in ((-1, 20), (0, 0)):
            with self.assertRaises(ValueError):
                show(self.ctx, "large_record", fields=["/details"], offset=offset, limit=limit)
        ext = add_result(self.ctx, {"track_id": "t1", "eval_id": "with_detail", "extra": {"rows": list(range(10000))}})
        sliced = json.loads(show(self.ctx, "with_detail", detail=True, fields=["/extra/rows"], offset=11, limit=3))
        self.assertEqual(sliced["fields"]["/extra/rows"], [11, 12, 13])
        detail_file = self.ctx.ops_dir / ext["detail_ref"]["path"]
        detail_file.write_text(detail_file.read_text(encoding="utf-8") + " ", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            show(self.ctx, "with_detail", detail=True)


    def test_context_status_observations_and_edge_cases(self) -> None:
        atomic_json(self.ctx.ops_dir / "tracks/ctx_case/STATUS.json", {
            "status": "running", "completed": 7, "pending": list(range(100)), "enabled": False,
        })
        atomic_text(self.ctx.lessons_dir / "evidence.md", "# Evidence\nKeep layers separate.\n")
        set_track(self.ctx, {
            "track_id": "ctx_case", "title": "Observation test", "status": "active",
            "next_actions": ["Verify live worker"],
            "status_refs": ["tracks/ctx_case/STATUS.json", "tracks/ctx_case/missing.json"],
        })
        rendered = context(self.ctx, "ctx_case", as_json=True)
        val = json.loads(rendered)
        self.assertLessEqual(len(rendered.encode("utf-8")), 24 * 1024)
        self.assertEqual(val["observations"][0]["pending"], {"count": 100})
        self.assertIn("does not verify live process", val["observations"][0]["meaning"])
        self.assertIn("error", val["observations"][1])
        self.assertIn("lessons/evidence.md", val["lessons"])
        self.assertIn("Verify live worker", context(self.ctx, "ctx_case"))
        atomic_text(self.ctx.ops_dir / "big.json", " " * (256 * 1024 + 1))
        atomic_text(self.ctx.ops_dir / "broken.json", "{broken")
        self.assertIn("not loaded", observe_status(self.ctx, "big.json")["note"])
        self.assertIn("error", observe_status(self.ctx, "broken.json"))

    def test_check_stale_brief_retired_docs_warnings_and_utf8_budgets(self) -> None:
        brief(self.ctx, write=True)
        self.assertTrue(check(self.ctx)["ok"])
        set_track(self.ctx, {"track_id": "new_t", "title": "New track", "status_refs": ["tracks/new_t/missing.json"]})
        self.assertIn("stale", " ".join(check(self.ctx)["errors"]))
        brief(self.ctx, write=True)
        res = check(self.ctx)
        self.assertTrue(res["ok"])
        self.assertIn("not accessible", " ".join(res["warnings"]))

        for rel in ("current_state.md", "handoff/CURRENT_HANDOFF.md", "trace/failure_patterns.md"):
            atomic_text(self.ctx.ops_dir / rel, "recreated\n")
        retired = check(self.ctx)
        self.assertFalse(retired["ok"])
        for name in ("current_state.md", "CURRENT_HANDOFF.md", "failure_patterns.md"):
            self.assertIn(name, " ".join(retired["errors"]))
            (self.ctx.ops_dir / [r for r in ("current_state.md", "handoff/CURRENT_HANDOFF.md", "trace/failure_patterns.md") if name in r][0]).unlink()

        for filename in ("AGENTS.md", "CLAUDE.md"):
            path = self.root / filename
            exact = "中" * (8192 // 3) + "x" * (8192 % 3)
            atomic_text(path, exact)
            self.assertTrue(check(self.ctx)["ok"])
            atomic_text(path, exact + "x")
            over = check(self.ctx)
            self.assertFalse(over["ok"])
            self.assertIn("8193 bytes", over["errors"][0])
            self.assertEqual(path.read_text(encoding="utf-8"), exact + "x")
            path.unlink()

    def test_detail_ref_traversal_is_rejected(self) -> None:
        outside = self.root / "secret.json"
        outside.write_text('{"secret": true}', encoding="utf-8")
        self._eval_ledger([{"eval_id": "escape", "detail_ref": {"path": "../../secret.json", "sha256": "0" * 64}}])
        with self.assertRaisesRegex(ValueError, "escapes ops directory"):
            show(self.ctx, "escape", detail=True)


if __name__ == "__main__":
    unittest.main()
