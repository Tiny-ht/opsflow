"""End-to-end subprocess CLI tests for opsflow v0.2."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_cli(self, *args: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, "-m", "opsflow.cli", "--root", str(self.root), *args],
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=20,
        )
        self.assertEqual(result.returncode, returncode, result.stderr + result.stdout)
        return result

    def test_init_and_check_pass_out_of_the_box_and_preserve_existing_files(self) -> None:
        self.run_cli("init", "--name", "Alpha")
        chk = json.loads(self.run_cli("check").stdout)
        self.assertTrue(chk["ok"])
        self.assertEqual(chk["errors"], [])
        self.assertEqual(chk["warnings"], [])
        self.assertTrue((self.root / "AGENTS.md").is_file())
        self.assertIn("AGENTS.md", (self.root / "CLAUDE.md").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "ops/BRIEF.md").is_file())
        self.assertFalse((self.root / "ops/current_state.md").exists())
        (self.root / "AGENTS.md").write_text("# Custom\n", encoding="utf-8")
        self.run_cli("add-trace", "--stage", "build", "--kind", "decision",
                     "--status", "completed", "--title", "Keep", "--summary", "Must not be truncated")
        self.run_cli("init", "--name", "Alpha")
        self.assertEqual((self.root / "AGENTS.md").read_text(encoding="utf-8"), "# Custom\n")
        self.assertEqual(len((self.root / "ops/trace/trace_log.jsonl").read_text(encoding="utf-8").splitlines()), 1)

    def test_track_lifecycle_and_linked_records(self) -> None:
        self.run_cli("init", "--name", "Beta")
        card_file = self.root / "new_track.json"
        card_file.write_text(json.dumps({
            "track_id": "parser", "title": "Parser rewrite", "status": "active",
            "next_actions": ["Add fuzz tests"],
        }), encoding="utf-8")
        created = json.loads(self.run_cli("track", "set", "--file", str(card_file)).stdout)
        self.assertEqual(created, {"track_id": "parser", "revision": 1, "status": "active"})
        self.run_cli("add-trace", "--trace-id", "tr-1", "--track", "parser",
                     "--stage", "build", "--kind", "decision", "--status", "completed",
                     "--title", "Use stdlib", "--summary", "Zero external deps")
        self.run_cli("add-trace", "--trace-id", "tr-1", "--track", "parser",
                     "--stage", "build", "--kind", "decision", "--status", "completed",
                     "--title", "Use stdlib", "--summary", "Zero external deps")
        self.run_cli("add-trace", "--track", "missing", "--stage", "build", "--kind", "decision",
                     "--status", "completed", "--title", "Bad", "--summary", "No track", returncode=2)
        found = json.loads(self.run_cli("query", "--track", "parser", "--source", "trace", "--json").stdout)
        self.assertEqual(found["total"], 1)
        self.assertIn("Parser rewrite", self.run_cli("context", "--track", "parser").stdout)
        closed = json.loads(self.run_cli(
            "track", "close", "parser", "--conclusion", "Shipped cleanly", "--expected-revision", "1",
        ).stdout)
        self.assertEqual(closed["status"], "closed")
        self.assertIn("open: 0", self.run_cli("brief").stdout)
        self.assertTrue(json.loads(self.run_cli("check").stdout)["ok"])


    def test_multi_layer_evals_externalized_result_and_paginated_query(self) -> None:
        self.run_cli(
            "init", "--name", "Layered", "--timezone", "+08:00",
            "--eval-layer", "dev=evals/dev_eval_log.jsonl",
            "--eval-layer", "prod=evals/prod_eval_log.jsonl",
        )
        self.run_cli("add-eval", "--run-id", "r1", "--status", "completed", "--summary", "Need layer", returncode=2)
        self.run_cli("add-eval", "--layer", "dev", "--run-id", "r1", "--status", "completed",
                     "--summary", "Dev smoke passed", "--tags", "smoke,dev")
        payload = self.root / "large_prod.json"
        payload.write_text(json.dumps({
            "eval_id": "prod-big", "evidence_layer": "prod", "status": "completed",
            "summary": "Prod canary metrics", "tags": ["canary"],
            "metrics": {"p99": {"value": 12.3, "unit": "ms", "aggregation": "p99_24h"}},
            "extra": {"rows": list(range(5000))},
        }), encoding="utf-8")
        out_id = self.run_cli("add-result", "--file", str(payload)).stdout.strip()
        self.assertEqual(out_id, "prod-big")
        all_evals = json.loads(self.run_cli("query", "--source", "eval", "--json").stdout)
        self.assertEqual(all_evals["total"], 2)
        prod_only = json.loads(self.run_cli("query", "--layer", "prod", "--tag", "canary", "--json").stdout)
        self.assertEqual(prod_only["total"], 1)
        detail = json.loads(self.run_cli("show", "prod-big", "--detail", "--field", "/extra/rows",
                                         "--offset", "5", "--limit", "3").stdout)
        self.assertEqual(detail["fields"]["/extra/rows"], [5, 6, 7])
        self.assertTrue(json.loads(self.run_cli("check").stdout)["ok"])

    def test_v1_config_backward_compatible_read_and_retired_doc_alert(self) -> None:
        (self.root / ".opsflow").mkdir(parents=True)
        (self.root / ".opsflow/config.json").write_text(json.dumps({
            "version": 1, "project_name": "LegacyApp", "ops_dir": "ops",
            "logs": {"trace": "trace/trace_log.jsonl", "eval": "evals/eval_log.jsonl", "research": "research/reference_intake.jsonl"},
            "defaults": {"owner": "codex"},
        }), encoding="utf-8")
        (self.root / "ops/trace").mkdir(parents=True)
        (self.root / "ops/trace/trace_log.jsonl").write_text(
            json.dumps({"trace_id": "legacy_1", "ts": "2026-06-09T14:00:00+08:00", "title": "Old trace", "summary": "From v1"}) + "\n",
            encoding="utf-8",
        )
        (self.root / "ops/current_state.md").write_text("# Old state\n", encoding="utf-8")
        res = json.loads(self.run_cli("query", "--keyword", "From v1", "--json").stdout)
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["records"][0]["id"], "legacy_1")
        chk = json.loads(self.run_cli("check", returncode=1).stdout)
        self.assertFalse(chk["ok"])
        self.assertIn("current_state.md", " ".join(chk["errors"]))

    def test_paginated_filtered_query_covers_every_matching_record_once(self) -> None:
        self.run_cli("init", "--name", "Pages", "--timezone", "+08:00")
        rows = [{
            "eval_id": f"rec-{i}", "summary": "Detailed evaluation summary " * 15,
            "ts": "2026-11-11T23:00:00+08:00", "evidence_layer": "offline",
            "tags": ["selected" if i < 10 else "other"],
        } for i in range(25)]
        rows.append({"eval_id": "no-date", "evidence_layer": "offline", "tags": ["selected"]})
        (self.root / "ops/evals/eval_log.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
        offset, seen = 0, []
        while offset is not None:
            out = self.run_cli(
                "query", "--layer", "offline", "--tag", "selected",
                "--since", "2026-11-11", "--until", "2026-11-11",
                "--offset", str(offset), "--budget-bytes", "2048", "--json",
            ).stdout
            self.assertLessEqual(len(out.encode("utf-8")), 2048)
            page = json.loads(out)
            self.assertEqual(page["total"], 10)
            seen.extend(r["id"] for r in page["records"])
            offset = page["next_offset"]
        self.assertEqual(set(seen), {f"rec-{i}" for i in range(10)})
        self.assertEqual(len(seen), 10)


    def test_paths_add_ref_track_list_reindex_and_lessons_budget(self) -> None:
        self.run_cli("init", "--name", "Gamma")
        paths = json.loads(self.run_cli("paths").stdout)
        self.assertIn("brief_file", paths)
        self.assertIn("tracks_dir", paths)
        card = self.root / "t.json"
        card.write_text(json.dumps({"track_id": "docs", "title": "Write docs", "status": "active"}), encoding="utf-8")
        self.run_cli("track", "set", "--file", str(card))
        listed = json.loads(self.run_cli("track", "list", "--status", "active", "--json").stdout)
        self.assertEqual([c["track_id"] for c in listed], ["docs"])
        ref_id = self.run_cli(
            "add-ref", "--track", "docs", "--source-type", "paper",
            "--title", "Bounded Context", "--status", "extracted", "--tags", "design",
        ).stdout.strip()
        self.assertTrue(ref_id.startswith("ref_"))
        reidx = json.loads(self.run_cli("reindex", "--force").stdout)
        self.assertGreaterEqual(reidx["records"], 3)
        (self.root / "ops/lessons/huge.md").write_text("x" * (65 * 1024), encoding="utf-8")
        chk = json.loads(self.run_cli("check", returncode=1).stdout)
        self.assertFalse(chk["ok"])
        self.assertIn("lessons/", " ".join(chk["errors"]))


if __name__ == "__main__":
    unittest.main()
