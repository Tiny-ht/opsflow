"""Isolated tests for atomic storage and track card concurrency/lifecycle."""

from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opsflow.config import load_context
from opsflow.storage import atomic_json, stable_id
from opsflow.tracks import close_track, get_track, list_tracks, set_track


def _track_worker(root: str, ready, start, output, summary: str) -> None:
    ctx = load_context(root=root)
    card = get_track(ctx, "concurrent")
    ready.put(True)
    start.wait(10)
    try:
        set_track(ctx, {"track_id": "concurrent", "summary": summary}, card["revision"])
        output.put("updated")
    except ValueError:
        output.put("conflict")


class StorageAndTracksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ctx = load_context(root=self.root)

    def test_atomic_replace_failure_preserves_previous_file(self) -> None:
        path = self.root / "state.json"
        atomic_json(path, {"value": "old"})
        with patch("opsflow.storage.os.replace", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                atomic_json(path, {"value": "new"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": "old"})
        self.assertFalse(list(self.root.glob(".*.tmp")))

    def test_nan_fails_before_replacing_file_and_stable_id_ignores_key_order(self) -> None:
        path = self.root / "state.json"
        atomic_json(path, {"value": 2})
        with self.assertRaises(ValueError):
            atomic_json(path, {"value": float("nan")})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"value": 2})
        self.assertEqual(stable_id({"a": 1, "b": 2}), stable_id({"b": 2, "a": 1}))

    def test_track_updates_replace_and_close_clears_handoff(self) -> None:
        card = set_track(
            self.ctx,
            {"track_id": "auth", "title": "Auth refactor", "status": "active",
             "next_actions": ["Run smoke"], "blockers": ["Waiting on staging"]},
        )
        for idx in range(100):
            card = set_track(self.ctx, {"track_id": "auth", "summary": f"Step {idx}"}, card["revision"])
        self.assertEqual(card["revision"], 101)
        self.assertEqual(len(list_tracks(self.ctx)), 1)
        closed = close_track(self.ctx, "auth", "Rejected due to latency regression", ["report.md"], card["revision"])
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["next_actions"], [])
        self.assertEqual(closed["blockers"], [])
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            set_track(self.ctx, {"track_id": "auth", "status": "active"}, card["revision"])
        with self.assertRaisesRegex(ValueError, "reopening"):
            set_track(self.ctx, {"track_id": "auth", "status": "paused"}, closed["revision"])
        reopened = set_track(self.ctx, {"track_id": "auth", "status": "active"}, closed["revision"])
        self.assertEqual(reopened["status"], "active")


    def test_track_retry_is_idempotent_and_changes_require_revision(self) -> None:
        payload = {"track_id": "search", "title": "Search index", "status": "active"}
        first = set_track(self.ctx, payload)
        self.assertEqual(set_track(self.ctx, payload), first)
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            set_track(self.ctx, {"track_id": "search", "summary": "New state"})
        updated = set_track(self.ctx, {"track_id": "search", "summary": "New state"}, 1)
        self.assertEqual(set_track(self.ctx, {"track_id": "search", "summary": "New state"}, 1), updated)

    def test_track_validates_paths_fields_and_no_truncation(self) -> None:
        for bad_id in ("../outside", "a/b", "/tmp/outside", ".", ""):
            with self.subTest(track_id=bad_id), self.assertRaises(ValueError):
                set_track(self.ctx, {"track_id": bad_id, "title": "Bad"})
        with self.assertRaisesRegex(ValueError, "summary"):
            set_track(self.ctx, {"track_id": "ok", "title": "Valid", "summary": "x" * 2001})
        with self.assertRaisesRegex(ValueError, "unknown track fields"):
            set_track(self.ctx, {"track_id": "ok", "title": "Valid", "next_action": "typo"})
        with self.assertRaises(FileNotFoundError):
            close_track(self.ctx, "absent", "Done")
        with self.assertRaisesRegex(ValueError, "conclusion"):
            close_track(self.ctx, "absent", "")
        with self.assertRaisesRegex(ValueError, "conclusion"):
            set_track(self.ctx, {"track_id": "empty_close", "title": "Bypass", "status": "closed"})

    def test_concurrent_track_updates_conflict_instead_of_losing_change(self) -> None:
        set_track(self.ctx, {"track_id": "concurrent", "title": "Concurrent"})
        mp = multiprocessing.get_context("fork")
        ready, output, start = mp.Queue(), mp.Queue(), mp.Event()
        procs = [
            mp.Process(target=_track_worker, args=(str(self.root), ready, start, output, text))
            for text in ("change-one", "change-two")
        ]
        for proc in procs:
            proc.start()
        for _ in procs:
            self.assertTrue(ready.get(timeout=10))
        start.set()
        for proc in procs:
            proc.join(10)
            self.assertEqual(proc.exitcode, 0)
        self.assertCountEqual([output.get(timeout=10) for _ in procs], ["updated", "conflict"])
        self.assertEqual(get_track(self.ctx, "concurrent")["revision"], 2)


if __name__ == "__main__":
    unittest.main()
