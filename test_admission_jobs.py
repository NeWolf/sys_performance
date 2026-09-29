"""Isolated admission job tests; retain temporary directories for inspection."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import perf_admission_jobs as jobs
from perf_joyspace import JoySpaceError, PUBLISH_UNKNOWN

S = "a" * 32
D = "b" * 32
D2 = "c" * 32


class AdmissionJobsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="admission-jobs-test-"))
        self.store = object()
        self.managers = []
        self.gates = []
        self.reader = patch.object(jobs, "read_template", return_value="template").start()
        self.builder = patch.object(jobs, "build_admission", side_effect=self.build).start()
        self.publisher = patch.object(jobs, "publish_markdown", return_value={"pageId": "Page123", "link": "ignored"}).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.shutdown)
        self.manager = self.open()

    def shutdown(self):
        for gate in self.gates:
            gate.set()
        for manager in self.managers:
            manager.close()

    def open(self, root=None):
        manager = jobs.AdmissionJobs(self.store, root or self.root)
        self.managers.append(manager)
        return manager

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def build(self, store, session, template, directory, scenes, factor):
        self.assertIs(store, self.store)
        self.assertEqual(template, "template")
        directory.joinpath("report.md").write_text("safe report", encoding="utf-8")
        return {"markdown": "safe report", "html": "<p>untrusted alternate preview</p>", "warnings": ["统计提示"]}

    def create(self, draft=D, session=S, manager=None):
        return (manager or self.manager).create(session, draft, "报告", {1: "unknown"}, None)

    def wait(self, draft=D, state="ready", manager=None):
        manager = manager or self.manager
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            value = manager.get(S, draft)
            if value["state"] == state:
                return value
            time.sleep(0.01)
        self.fail("state not reached: " + repr(value))

    def publish(self, draft=D, manager=None):
        return (manager or self.manager).publish(S, draft, "personal", None, True)

    def error(self, status, function, *args):
        with self.assertRaises(jobs.AdmissionJobError) as caught:
            function(*args)
        self.assertEqual(caught.exception.status, status)
        self.assertNotIn("SECRET", str(caught.exception))

    def test_artifact_sync_requires_writable_handle_and_fails_closed(self):
        original_open = Path.open
        original_sync = jobs.os.fsync
        for fail_sync, draft in ((False, D), (True, D2)):
            with self.subTest(fail_sync=fail_sync):
                streams = []
                synced = []

                def tracked_open(path, *args, **kwargs):
                    stream = original_open(path, *args, **kwargs)
                    if path.resolve() == (self.root / draft / "report.md").resolve():
                        streams.append(stream)
                    return stream

                def windows_sync(fd):
                    for stream in streams:
                        if not stream.closed and stream.fileno() == fd:
                            synced.append(stream.writable())
                            if not stream.writable() or fail_sync:
                                raise OSError("artifact flush rejected")
                    return original_sync(fd)

                with patch.object(Path, "open", tracked_open), patch.object(jobs.os, "fsync", windows_sync):
                    self.create(draft)
                    value = self.wait(draft, state="failed" if fail_sync else "ready")
                self.assertEqual(synced, [True])
                self.assertEqual((self.root / draft / "report.md").read_text(encoding="utf-8"), "safe report")
                if fail_sync:
                    self.assertIn("草稿生成失败", value["error"])
                    self.error(409, self.publish, draft)
        self.publisher.assert_not_called()

    def test_body_hash_blocks_changed_and_legacy_drafts(self):
        self.create()
        self.wait()
        preview = self.manager.preview(S, D)
        self.assertIn("safe report", preview)
        self.assertNotIn("untrusted alternate preview", preview)
        path = self.root / D / "report.md"
        for content in ("changed", "![image](remote)", " "):
            path.write_text(content, encoding="utf-8")
            self.error(409, self.manager.preview, S, D)
            self.error(409, self.publish)
        path.write_text("safe report", encoding="utf-8")
        with self.manager._mutex:
            record = dict(self.manager._records[D])
            record.pop("source_sha256")
            self.manager._persist(record)
            self.manager._records[D] = record
        self.manager.close()
        manager = self.open()
        self.error(409, manager.preview, S, D)
        self.error(409, self.publish, D, manager)
        self.publisher.assert_not_called()

    def test_worker_rechecks_body_and_passes_expected_hash(self):
        self.create()
        self.wait()
        digest = self.manager._records[D]["source_sha256"]
        self.publish()
        self.wait(state="published")
        self.assertEqual(self.publisher.call_args.kwargs, {"expected_sha256": digest})
        self.create(D2)
        self.wait(D2)
        record = dict(self.manager._records[D2], state="publishing",
                      publish_request={"placement": "personal", "parent_page": None})
        (self.root / D2 / "report.md").write_text("changed in queue", encoding="utf-8")
        self.publisher.reset_mock()
        self.manager._publish(record)
        self.assertEqual(self.manager.get(S, D2)["state"], "unknown")
        self.publisher.assert_not_called()

    def test_preview_never_allows_data_images(self):
        source = '<p>text</p><img src="data:image/png;base64,iVBORw0KGgo=">'
        preview = jobs._preview(source)
        self.assertNotIn("<img", preview)
        self.assertIn("img-src 'none'", preview)

    def test_concurrent_create_and_configuration_conflicts(self):
        gate = self.gate()
        original = self.build
        self.builder.side_effect = lambda *args: (gate.wait(5), original(*args))[1]
        with ThreadPoolExecutor(max_workers=12) as pool:
            replies = list(pool.map(lambda _: self.create(), range(24)))
        self.assertTrue(all(r["state"] == "generating" for r in replies))
        self.assertEqual(len(self.manager.list(S)["drafts"]), 1)
        self.error(409, self.manager.create, S, D, "other", {1: "unknown"}, None)
        self.error(409, self.create, D, "d" * 32)
        self.error(404, self.manager.get, "d" * 32, D)
        gate.set()
        self.wait()
        self.assertEqual(self.builder.call_count, 1)
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual(self.create()["state"], "ready")

    def test_publish_double_click_and_disconnected_caller(self):
        self.create()
        self.wait()
        gate, entered = self.gate(), self.gate()
        def remote(*args, **kwargs):
            entered.set()
            gate.wait(5)
            return {"pageId": "Verified"}
        self.publisher.side_effect = remote
        # Dropping the request result does not cancel the independent worker.
        self.publish()
        self.assertTrue(entered.wait(3))
        with ThreadPoolExecutor(max_workers=10) as pool:
            replies = list(pool.map(lambda _: self.publish(), range(20)))
        self.assertTrue(all(r["state"] == "publishing" for r in replies))
        self.error(409, self.manager.publish, S, D, "child", "Other", True)
        gate.set()
        result = self.wait(state="published")
        self.assertEqual(result["link"], "https://joyspace.jd.com/pages/Verified")
        self.assertEqual(self.publish()["state"], "published")
        self.assertEqual(self.publisher.call_count, 1)

    def test_unknown_blocks_session_but_not_other_sessions(self):
        self.create()
        self.create(D2)
        self.wait()
        self.wait(D2)
        self.publisher.side_effect = JoySpaceError(PUBLISH_UNKNOWN)
        self.publish()
        self.wait(state="unknown")
        self.error(409, self.create, "d" * 32)
        self.error(409, self.publish, D2)
        self.assertEqual(self.publish()["state"], "unknown")
        self.assertEqual(self.create()["state"], "unknown")
        self.assertEqual(self.create("e" * 32, "f" * 32)["state"], "generating")
        self.assertEqual(self.publisher.call_count, 1)

    def test_publishing_blocks_other_drafts(self):
        self.create()
        self.create(D2)
        self.wait()
        self.wait(D2)
        gate = self.gate()
        self.publisher.side_effect = lambda *a, **kw: (gate.wait(5), {"pageId": "P"})[1]
        self.publish()
        self.error(409, self.create, "d" * 32)
        self.error(409, self.publish, D2)
        gate.set()
        self.wait(state="published")
        self.publish(D2)
        self.wait(D2, "published")

    def test_restart_recovery_and_retention(self):
        self.create()
        self.create(D2)
        self.wait()
        self.wait(D2)
        with self.manager._mutex:
            first = dict(self.manager._records[D], state="publishing",
                         publish_request={"placement": "personal", "parent_page": None})
            second = dict(self.manager._records[D2], state="generating")
            self.manager._persist(first)
            self.manager._persist(second)
        self.manager.close()
        manager = self.open()
        self.assertEqual(manager.get(S, D)["state"], "unknown")
        self.assertEqual(manager.get(S, D2)["state"], "failed")
        self.assertEqual(len(manager.list(S)["drafts"]), 2)
        self.assertEqual(self.publish(manager=manager)["state"], "unknown")
        self.error(409, self.create, "d" * 32, S, manager)
        self.publisher.assert_not_called()
        self.assertIn("safe", manager.preview(S, D))
        self.assertTrue((self.root / D / "report.md").exists())

    def test_ready_and_published_survive_restart(self):
        self.create()
        self.wait()
        self.manager.close()
        manager = self.open()
        self.assertEqual(self.create(manager=manager)["state"], "ready")
        self.publish(manager=manager)
        self.wait(state="published", manager=manager)
        manager.close()
        manager = self.open()
        self.assertEqual(self.publish(manager=manager)["state"], "published")
        self.assertEqual(self.builder.call_count, 1)
        self.assertEqual(self.publisher.call_count, 1)

    def test_close_waits_and_releases_lock(self):
        self.create()
        self.wait()
        entered, gate = self.gate(), self.gate()
        self.publisher.side_effect = lambda *a, **kw: (entered.set(), gate.wait(5), {"pageId": "P"})[2]
        self.publish()
        self.assertTrue(entered.wait(3))
        thread = threading.Thread(target=self.manager.close)
        thread.start()
        time.sleep(0.05)
        self.assertTrue(thread.is_alive())
        gate.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.manager.close()
        self.assertEqual(self.open().get(S, D)["state"], "published")

    def test_validation(self):
        for invalid in (None, 1, "../bad", "a" * 31, "g" * 32, D + "\n"):
            with self.subTest(invalid=invalid):
                self.error(400, self.manager.list, invalid)
                self.error(400, self.manager.get, S, invalid)
                self.error(400, self.manager.preview, S, invalid)
                self.error(400, self.manager.publish, S, invalid, "personal", None, True)
        for title in(None, "", " ", "x" * 201, "x\x00"):
            self.error(400, self.manager.create, S, D, title, {}, None)
        for scenes in (None, [], {"1": "unknown"}, {True: "unknown"}, {1: "auto"}, {1: []}):
            self.error(400, self.manager.create, S, D, "t", scenes, None)
        for factor in (True, 0, -1, float("nan"), float("inf"), "1", 10 ** 1000):
            self.error(400, self.manager.create, S, D, "t", {}, factor)
        self.create()
        self.wait()
        for placement, target, confirmed in ((None, None, True), ([], None, True),
                ("personal", "", True), ("personal", None, 1), ("personal", None, False),
                ("child", None, True), ("sibling", "https://evil/pages/X", True),
                ("child", "https://joyspace.jd.com/documents/X", True)):
            self.error(400, self.manager.publish, S, D, placement, target, confirmed)
        self.publisher.assert_not_called()
        self.manager.publish(S, D, "child", "https://joyspace.jd.com/pages/Page", True)
        self.wait(state="published")
        self.assertEqual(self.publisher.call_args.args[2:], ("child", "Page"))

    def test_generation_failures_are_safe_and_never_retried(self):
        self.builder.side_effect = ValueError("SECRET /Users/private/password")
        self.create()
        result = self.wait(state="failed")
        self.assertNotIn("SECRET", str(result))
        self.assertNotIn("/Users", str(result))
        self.assertEqual(self.create()["state"], "failed")
        self.error(409, self.publish)
        self.error(409, self.manager.preview, S, D)
        self.assertEqual(self.builder.call_count, 1)

    def test_all_remote_errors_and_malformed_results_are_unknown(self):
        for index, failure in enumerate((TimeoutError("SECRET"), OSError("SECRET"),
                JoySpaceError("SECRET"), KeyboardInterrupt(), {"pageId": "https://evil"}, {})):
            manager = self.open(self.root / str(index))
            self.create(manager=manager)
            self.wait(manager=manager)
            if isinstance(failure, BaseException):
                self.publisher.side_effect = failure
            else:
                self.publisher.side_effect = None
                self.publisher.return_value = failure
            self.publish(manager=manager)
            result = self.wait(state="unknown", manager=manager)
            self.assertEqual(result["error"], PUBLISH_UNKNOWN)
            self.assertNotIn("SECRET", str(result))
            calls = self.publisher.call_count
            self.publish(manager=manager)
            self.assertEqual(self.publisher.call_count, calls)

    def test_preview_is_sanitized_and_metadata_isolated(self):
        original = self.build
        def build(*args):
            data = original(*args)
            data["html"] = ('<script>alert(1)</script><p onclick="bad()">safe /Users/private/a</p>'
                            '<p>/system/bin/surfaceflinger /vendor/bin/hw/service</p>'
                            '<p>https://example.invalid/report?a=1&amp;b=2</p>'
                            '<img src="file:///private/a"><a href="javascript:bad()">text</a>'
                            '<svg onload="bad()"></svg><iframe src="https://evil"></iframe>')
            args[3].joinpath("report.md").write_text(
                'safe /Users/private/a\n/system/bin/surfaceflinger /vendor/bin/hw/service\n'
                'https://example.invalid/report?a=1&b=2', encoding="utf-8")
            data["warnings"] = ["文件 /Users/private/a"]
            return data
        self.builder.side_effect = build
        self.create()
        result = self.wait()
        result["warnings"].append("mutation")
        preview = self.manager.preview(S, D)
        for forbidden in ("<script", "onclick", "file:", "javascript:", "<svg", "<iframe", "href="):
            self.assertNotIn(forbidden, preview)
        self.assertIn("/system/bin/surfaceflinger /vendor/bin/hw/service", preview)
        self.assertIn("https://example.invalid/report?a=1&amp;b=2", preview)
        self.assertIn("safe /Users/private/a", preview)
        self.assertIn("default-src 'none'", preview)
        self.assertIn("safe", preview)
        self.assertNotIn("mutation", str(self.manager.get(S, D)))
        self.assertNotIn("/Users", str(self.manager.get(S, D)))

    def test_persist_failure_prevents_remote_call(self):
        self.create()
        self.wait()
        self.manager._db.execute("PRAGMA query_only=ON")
        self.error(503, self.publish)
        self.publisher.assert_not_called()
        self.error(503, self.create, D2)
        self.manager._db.execute("PRAGMA query_only=OFF")
        self.manager.close()
        self.assertEqual(self.open().get(S, D)["state"], "ready")

    def test_post_remote_commit_failure_stays_unknown_after_restart(self):
        self.create()
        self.wait()
        def remote(*args, **kwargs):
            self.manager._db.execute("PRAGMA query_only=ON")
            return {"pageId": "AlreadyCreated"}
        self.publisher.side_effect = remote
        self.publish()
        self.wait(state="unknown")
        self.manager.close()
        manager = self.open()
        self.assertEqual(manager.get(S, D)["state"], "unknown")
        self.publish(manager=manager)
        self.assertEqual(self.publisher.call_count, 1)

    def test_remote_observes_committed_publishing_record(self):
        self.create()
        self.wait()
        def remote(*args, **kwargs):
            db = sqlite3.connect(self.root / "admission.sqlite3")
            try:
                row = db.execute("SELECT data FROM drafts WHERE id=?", (D,)).fetchone()
                self.assertEqual(json.loads(row[0])["state"], "publishing")
            finally:
                db.close()
            return {"pageId": "P"}
        self.publisher.side_effect = remote
        self.publish()
        self.wait(state="published")

    def test_global_capacity_and_concurrency_across_roots(self):
        gate = self.gate()
        lock = threading.Lock()
        active = peak = started = 0
        original = self.build
        def build(*args):
            nonlocal active, peak, started
            with lock:
                active += 1
                started += 1
                peak = max(peak, active)
            gate.wait(5)
            try:
                return original(*args)
            finally:
                with lock:
                    active -= 1
        self.builder.side_effect = build
        other = self.open(self.root / "other")
        for index in range(jobs.MAX_OUTSTANDING):
            self.create(format(index, "032x"), manager=other if index % 2 else self.manager)
        self.error(429, self.create, "f" * 32)
        time.sleep(0.1)
        self.assertEqual(peak, jobs.MAX_WORKERS)
        self.assertEqual(started, jobs.MAX_WORKERS)
        gate.set()
        for index in range(jobs.MAX_OUTSTANDING):
            self.wait(format(index, "032x"), manager=other if index % 2 else self.manager)
        self.assertLessEqual(peak, jobs.MAX_WORKERS)

    def test_cross_process_root_lock(self):
        self.error(503, jobs.AdmissionJobs, object(), self.root)
        code = ('import sys; from perf_admission_jobs import AdmissionJobs, AdmissionJobError; '
                'root=sys.argv[1]\ntry:\n m=AdmissionJobs(object(),root)\n'
                'except AdmissionJobError:\n sys.exit(23)\nelse:\n m.close()\n')
        blocked = subprocess.run([sys.executable, "-B", "-c", code, str(self.root)],
                                 capture_output=True, timeout=10)
        self.assertEqual(blocked.returncode, 23, blocked.stderr)
        self.manager.close()
        allowed = subprocess.run([sys.executable, "-B", "-c", code, str(self.root)],
                                 capture_output=True, timeout=10)
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_executor_rejection_is_safe(self):
        with patch.object(self.manager._executor, "submit", side_effect=RuntimeError("SECRET")):
            self.error(503, self.create)
        self.assertEqual(self.manager.get(S, D)["state"], "failed")
        self.builder.assert_not_called()
        self.publisher.assert_not_called()


    def test_windows_lock_branch(self):
        path = self.root / "windows.lock"
        windows = Mock()
        windows.LK_NBLCK = 1
        with patch.object(jobs.os, "name", "nt"), patch.dict(sys.modules, {"msvcrt": windows}):
            lock = jobs._RootLock(path)
            windows.locking.assert_called_once_with(lock.file.fileno(), 1, 1)
            lock.close()
            self.assertTrue(lock.file.closed)
            windows.locking.side_effect = OSError("SECRET")
            self.error(409, jobs._RootLock, path)
        self.assertEqual(path.read_bytes(), b"0")

    def test_queued_publish_is_blocked_when_journal_breaks(self):
        self.create()
        self.wait()
        for _ in range(jobs.MAX_WORKERS):
            jobs._RUNNING.acquire()
        try:
            self.publish()
            with self.manager._mutex:
                self.manager._broken = True
        finally:
            for _ in range(jobs.MAX_WORKERS):
                jobs._RUNNING.release()
        self.wait(state="unknown")
        self.publisher.assert_not_called()

    def test_input_snapshot_and_generator_coverage_validation(self):
        gate = self.gate()
        self.reader.side_effect = lambda: (gate.wait(5), "template")[1]
        scenes = {1: "unknown"}
        self.manager.create(S, D, "报告", scenes, 1.5)
        scenes[1] = "foreground"
        gate.set()
        self.wait()
        self.assertEqual(self.builder.call_args.args[-2:], ({1: "unknown"}, 1.5))
        self.builder.side_effect = ValueError("scenes必须恰好覆盖所有segment")
        self.manager.create(S, D2, "报告", {}, None)
        self.wait(D2, "failed")
        self.assertEqual(self.builder.call_args.args[-2], {})


if __name__ == "__main__":
    unittest.main()

