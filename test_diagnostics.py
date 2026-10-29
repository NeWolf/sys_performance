"""Offline regression for bounded diagnostics and unchanged import semantics."""
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import perf_diagnostics as diagnostics


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        diagnostics.flush()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.directory = self.root / "diagnostics"
        diagnostics.configure(self.directory)

    def tearDown(self):
        diagnostics.flush()
        diagnostics.configure()
        self.temporary.cleanup()

    def records(self):
        self.assertTrue(diagnostics.flush())
        path = self.directory / "runtime.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_allowlist_drops_secrets_invalid_numbers_and_ids(self):
        diagnostics.emit("joyspace.port", status="error", port=8988, attempt=1,
                         code="ECONNREFUSED", phase="body", has_auth_header=False,
                         body="secret-body", url="https://secret", operation_id="a" * 32,
                         files=True, bytes=-1, elapsed_ms=float("nan"), source="secret-cookie")
        diagnostics.emit("secret-stage", token="secret-token")
        record, = self.records()
        self.assertEqual(record["port"], 8988)
        self.assertEqual(record["phase"], "body")
        self.assertFalse(record["has_auth_header"])
        for key in ("body", "url", "operation_id", "files", "bytes", "elapsed_ms", "source"):
            self.assertNotIn(key, record)
        self.assertNotIn("secret", json.dumps(record))

    def test_nested_spans_counts_identity_and_interrupt(self):
        error = KeyboardInterrupt("private-message")
        with self.assertRaises(KeyboardInterrupt) as caught:
            with diagnostics.span("import.total"):
                with diagnostics.span("import.parse", records=0) as progress:
                    progress.update(records=42)
                    raise error
        self.assertIs(caught.exception, error)
        records = self.records()
        self.assertEqual([r["status"] for r in records], ["begin", "begin", "error", "error"])
        self.assertEqual(records[2]["records"], 42)
        self.assertEqual(len({r["operation_id"] for r in records}), 1)
        self.assertEqual(records[1]["parent_span_id"], records[0]["span_id"])
        self.assertEqual(records[1]["span_id"], records[2]["span_id"])
        self.assertNotIn("private-message", json.dumps(records))

    def test_measured_return_value_and_concurrent_ids(self):
        result = object()

        @diagnostics.measured("adb.pull")
        def run():
            return result

        self.assertIs(run(), result)
        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        records = self.records()
        self.assertEqual(len(records), 10)
        self.assertEqual(len({r["operation_id"] for r in records}), 5)

    def test_sink_failure_does_not_change_result_or_exception(self):
        with patch.object(diagnostics, "_append", side_effect=OSError("secret-path")):
            with diagnostics.span("import.total"):
                pass
            error = ValueError("original")
            with self.assertRaises(ValueError) as caught:
                with diagnostics.span("import.parse"):
                    raise error
            self.assertIs(caught.exception, error)
            self.assertTrue(diagnostics.flush())
        self.assertEqual(self.records(), [])

    def test_clock_id_and_emitter_failures_preserve_business_behavior(self):
        error = ValueError("original")
        failures = ((diagnostics.time, "monotonic"), (diagnostics.uuid, "uuid4"),
                    (diagnostics, "emit"))
        for owner, name in failures:
            with self.subTest(name=name), patch.object(owner, name, side_effect=OSError("private")):
                with diagnostics.span("import.total") as progress:
                    progress["records"] = 1
                with self.assertRaises(ValueError) as caught:
                    with diagnostics.span("import.total"):
                        raise error
                self.assertIs(caught.exception, error)
        self.assertIsNone(diagnostics._operation.get())
        self.assertIsNone(diagnostics._current_span.get())
        self.assertIsNone(diagnostics._parent_span.get())
        self.assertNotIn("private", json.dumps(self.records()))

    def test_configuration_does_not_wait_for_disk_lock(self):
        done = threading.Event()

        def configure():
            diagnostics.configure(self.directory)
            done.set()

        with diagnostics._lock:
            thread = threading.Thread(target=configure)
            thread.start()
            completed = done.wait(1)
        thread.join(2)
        self.assertTrue(completed)

    def test_bounded_queue_does_not_block_on_slow_sink(self):
        entered, release = threading.Event(), threading.Event()
        original = diagnostics._append

        def slow(*args):
            entered.set()
            release.wait(5)
            original(*args)

        with patch.object(diagnostics, "_append", side_effect=slow):
            try:
                diagnostics.emit("import.total", status="begin")
                self.assertTrue(entered.wait(2))
                for _ in range(1500):
                    diagnostics.emit("import.parse", status="begin")
                self.assertLessEqual(diagnostics._queue.qsize(), 1024)
                self.assertFalse(diagnostics.flush(timeout=0.01))
            finally:
                release.set()
            diagnostics._queue.join()
        self.assertEqual(len(self.records()), 1025)

    def test_rotation_is_bounded_and_private(self):
        with patch.object(diagnostics, "_MAX_BYTES", 512):
            for _ in range(30):
                diagnostics.emit("import.total", status="ok")
            records = self.records()
        self.assertTrue(records)
        self.assertEqual(len(list(self.directory.iterdir())), 2)
        for path in self.directory.iterdir():
            self.assertLessEqual(path.stat().st_size, 512)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            for line in path.read_text().splitlines():
                json.loads(line)

    @unittest.skipIf(os.name == "nt", "POSIX link fixture")
    def test_symlink_and_hardlink_destinations_are_not_written(self):
        target = self.root / "outside"
        target.write_text("unchanged")
        self.directory.mkdir()
        path = self.directory / "runtime.jsonl"
        path.symlink_to(target)
        diagnostics.emit("import.total", status="ok")
        self.assertTrue(diagnostics.flush())
        self.assertEqual(target.read_text(), "unchanged")
        other = self.root / "other"
        other.mkdir()
        os.link(target, other / "runtime.jsonl")
        diagnostics.configure(other)
        diagnostics.emit("import.total", status="ok")
        self.assertTrue(diagnostics.flush())
        self.assertEqual(target.read_text(), "unchanged")

    @unittest.skipIf(os.name == "nt", "POSIX link fixture")
    def test_directory_symlink_is_not_followed(self):
        target = self.root / "outside-dir"
        target.mkdir()
        self.directory.symlink_to(target, target_is_directory=True)
        diagnostics.emit("import.total", status="ok")
        self.assertTrue(diagnostics.flush())
        self.assertEqual(list(target.iterdir()), [])

    def test_real_import_counters_cache_duplicate_and_rollback(self):
        from perf_store import Store
        from test_perf import SAMPLE
        store = Store(self.root / "test.sqlite")
        payload = SAMPLE.encode()
        result = store.import_files([("private-file", io.BytesIO(payload))])
        sid = result["id"]
        store.statistics(sid, "S")
        store.statistics(sid, "S")
        duplicate = store.import_files([("renamed", io.BytesIO(payload))])
        self.assertTrue(duplicate["duplicate"])
        with patch.object(store, "_insert", side_effect=ValueError("private-error")):
            with self.assertRaises(ValueError):
                store.import_files([("bad", io.BytesIO(SAMPLE.replace("42.04", "43.04").encode()))])
        self.assertEqual(len(store.sessions()), 1)
        records = self.records()
        ends = [r for r in records if r["status"] == "ok"]
        fingerprint = next(r for r in ends if r["stage"] == "import.fingerprint")
        self.assertEqual((fingerprint["files"], fingerprint["bytes"]), (1, len(payload)))
        self.assertEqual([r["cache_hit"] for r in ends if r["stage"] == "analysis.statistics"], [False, True])
        self.assertTrue(any(r.get("duplicate") is True for r in ends))
        self.assertTrue(any(r["stage"] == "import.total" and r["status"] == "error" for r in records))
        self.assertNotIn("private", json.dumps(records))


if __name__ == "__main__":
    unittest.main()