"""Top capture lifecycle and API regressions without a physical Android device."""
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from perf_adb import AdbController, AdbError
from perf_api import create_app
from perf_top_capture import TopCapture


TOP = b"PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ ARGS\n1 root 20 0 1G 20M 1M S 12.5 1.0 0:01 app\n"


class TopCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adb = AdbController(self.root / "adb", executable="fake-adb")
        self.adb.devices = Mock(return_value=[{"serial": "device-1", "state": "device"}])
        self.store = Mock()
        self.store.import_files.return_value = {"id": "session-1", "duplicate": False}
        self.import_lock = threading.Lock()
        self.top = TopCapture(self.adb, self.root / "top", self.store, self.import_lock)
        self.addCleanup(self.top.close)

    def sample(self, serial, stream):
        self.assertEqual(serial, "device-1")
        self.assertTrue(self.adb.lock.locked())
        stream.write(TOP)
        return len(TOP)

    def finish(self, count=1, sample=None):
        with patch.object(self.top, "_sample", side_effect=sample or self.sample), \
                patch.object(self.top._stop, "wait", return_value=False):
            self.top.start("device-1", count=count, title=" test ")
            self.top._thread.join(3)
            self.assertFalse(self.top._thread.is_alive())
        return self.top.status()

    def assert_status_error(self, status, function, *args, **kwargs):
        with self.assertRaises(AdbError) as error:
            function(*args, **kwargs)
        self.assertEqual(error.exception.status, status)

    def test_finite_capture_local_durable_and_explicit_import(self):
        from datetime import datetime
        import perf_top_capture
        with patch("perf_top_capture.datetime") as clock, \
                patch("perf_top_capture.os.fsync", wraps=perf_top_capture.os.fsync) as sync:
            clock.now.return_value = datetime(2026, 9, 22, 12, 34, 56)
            state = self.finish(count=2)
        self.assertEqual(clock.now.call_count, 2)
        clock.now.assert_called_with()
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(state["count"], 2)
        self.assertEqual(state["status"], "completed")
        self.assertFalse(self.adb.lock.locked())
        path = self.top.archive_path()
        raw = path.read_bytes()
        self.assertEqual(state["bytes"], len(raw))
        self.assertIn("========== Top 采集 #1 时间: 2026-09-22 12:34:56 ==========", raw.decode())
        self.assertNotIn(" UTC ", raw.decode())
        self.assertIn("Top 采集 #2", raw.decode())
        self.assertIn(state["id"], state["archive"])
        self.store.import_files.assert_not_called()
        def do_import(sources, title):
            self.assertTrue(self.import_lock.locked())
            self.assertEqual(title, "test")
            self.assertEqual(sources[0][1].read(), raw)
            return {"id": "session-1", "duplicate": False}
        self.store.import_files.side_effect = do_import
        self.assertEqual(self.top.import_capture()["id"], "session-1")
        self.assertTrue(self.top.import_capture()["duplicate"])
        self.assertEqual(self.store.import_files.call_count, 1)
        self.assertFalse(self.import_lock.locked())

    def test_unlimited_stop_nonblocking_and_close_waits(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(serial, stream):
            entered.set()
            release.wait(3)
            return self.sample(serial, stream)
        with patch.object(self.top, "_sample", side_effect=blocked):
            self.top.start("device-1", count=-1)
            self.assertTrue(entered.wait(1))
            self.assert_status_error(409, self.top.start, "device-1")
            self.assert_status_error(409, self.top.import_capture)
            self.assert_status_error(409, self.top.archive_path)
            self.assert_status_error(409, self.adb.status, "device-1")
            started = time.monotonic()
            self.assertTrue(self.top.stop()["stopping"])
            self.assertLess(time.monotonic() - started, 0.5)
            waiter = threading.Thread(target=lambda: (self.top.close(), closed.set()))
            waiter.start()
            self.assertFalse(closed.wait(0.05))
            release.set()
            waiter.join(2)
            self.assertTrue(closed.is_set())
        self.assertEqual(self.top.status()["status"], "stopped")
        self.assertEqual(self.top.status()["count"], 1)
        self.assertFalse(self.adb.lock.locked())
        self.assert_status_error(503, self.top.start, "device-1")

    def test_sample_failure_preserves_previous_sample(self):
        calls = 0
        def fails(serial, stream):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise AdbError("device disconnected", 502)
            return self.sample(serial, stream)
        state = self.finish(count=3, sample=fails)
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["count"], 1)
        self.assertIn("device disconnected", state["error"])
        self.assertEqual(self.top.archive_path().read_bytes().count(TOP), 1)
        self.top.import_capture()
        self.assertIn("device disconnected", self.top.status()["error"])

    def test_archive_limit_does_not_append_partial_sample(self):
        with patch("perf_top_capture.MAX_ARCHIVE_BYTES", len(TOP) + 150):
            state = self.finish(count=3)
        self.assertEqual(state["count"], 1)
        self.assertIn("1 GiB", state["error"])
        self.assertEqual(self.top.archive_path().stat().st_size, state["bytes"])

    def test_import_failure_retry_and_shared_lock(self):
        self.finish()
        self.import_lock.acquire()
        self.assert_status_error(409, self.top.import_capture)
        self.import_lock.release()
        self.store.import_files.side_effect = [ValueError("parse failed"),
                                               {"id": "retry", "duplicate": True}]
        with self.assertRaisesRegex(ValueError, "parse failed"):
            self.top.import_capture()
        self.assertFalse(self.import_lock.locked())
        self.assertFalse(self.top.status()["importing"])
        self.assertIsNone(self.top.status()["session_id"])
        self.assertIn("可重试", self.top.status()["error"])
        self.assertEqual(self.top.import_capture()["id"], "retry")
        self.assertIsNone(self.top.status()["error"])

    def test_archives_are_bound_to_task_and_remain_downloadable(self):
        old = self.finish()
        old_path = self.top.archive_path(old["id"])
        self.finish()
        self.assertNotEqual(old_path, self.top.archive_path())
        self.assertEqual(self.top.archive_path(old["id"]), old_path)
        self.assert_status_error(409, self.top.import_capture, old["id"])
        self.assert_status_error(404, self.top.archive_path, "../../secret")

    def test_start_validation_and_missing_archive(self):
        self.assert_status_error(404, self.top.archive_path)
        for options in ({"count": 0}, {"count": True}, {"count": -2},
                        {"interval": float("nan")}, {"interval": True}, {"interval": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.top.start("device-1", **options)
        self.assert_status_error(400, self.top.start, "bad;serial")
        self.top._thread.join(1)
        self.assert_status_error(409, self.top.start, "offline")
        self.top._thread.join(1)
        self.assertFalse(self.adb.lock.locked())

    def test_close_waits_for_import_and_prevents_new_work(self):
        self.finish()
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(sources, title):
            entered.set()
            release.wait(3)
            return {"id": "imported", "duplicate": False}
        self.store.import_files.side_effect = blocked
        worker = threading.Thread(target=self.top.import_capture)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assert_status_error(409, self.top.start, "device-1")
        self.assert_status_error(409, self.top.import_capture)
        self.assertTrue(self.top.archive_path().is_file())
        closer = threading.Thread(target=lambda: (self.top.close(), closed.set()))
        closer.start()
        self.assertFalse(closed.wait(0.05))
        release.set()
        worker.join(2)
        closer.join(2)
        self.assertTrue(closed.is_set())
        self.assertFalse(self.import_lock.locked())
        self.assert_status_error(503, self.top.import_capture)

    def test_write_failure_rolls_back_incomplete_sample(self):
        write = self.top._write_all
        calls = 0
        def failing_write(stream, data):
            nonlocal calls
            calls += 1
            if calls == 5:
                stream.write(b"partial")
                raise OSError("disk full")
            return write(stream, data)
        with patch.object(self.top, "_write_all", side_effect=failing_write):
            state = self.finish(count=2)
        self.assertEqual(state["count"], 1)
        self.assertIn("写入失败", state["error"])
        self.assertEqual(self.top.archive_path().stat().st_size, state["bytes"])
        self.assertNotIn(b"partial", self.top.archive_path().read_bytes())

    def test_empty_and_invalid_sample_not_exposed(self):
        state = self.finish(sample=Mock(side_effect=ValueError("invalid top")))
        self.assertEqual(state["count"], 0)
        self.assertIsNone(state["archive"])
        self.assert_status_error(404, self.top.archive_path)
        for payload in (b"", b"permission denied", TOP.replace(b"12.5", b"nan")):
            self.assertFalse(self.top._valid_top(io.BytesIO(payload)))
        self.assertTrue(self.top._valid_top(io.BytesIO(TOP)))

    def test_compact_device_header_capture_and_import(self):
        from perf_store import Store

        payload = (
            b"Mem: 14725M total, 7385M used, 7340M free, 56M buffers\n"
            b"800%cpu 52%user 0%nice 119%sys 619%idle 0%iow 7%irq 4%sirq 0%host\n"
            b"PID USER PR NI VIRT RES VSWAP SHR S[%CPU]%SCPU %MEM TIME+ ARGS\n"
            b"23212 shell 20 0 10G 5.2M 0 4.1M R 11.1 7.4 0.0 0:00.03 top -b -n 1 -d 1\n"
            b"8647 system 18 -2 19G 413M 0 318M S 3.7 0.0 2.8 3:32.23 system_server\n")
        self.assertTrue(self.top._valid_top(io.BytesIO(payload)))
        self.assertFalse(self.top._valid_top(io.BytesIO(
            payload.replace(b"11.1", b"nan").replace(b"3.7", b"inf"))))
        process = Mock(stdout=io.BytesIO(payload), returncode=0)
        process.poll.return_value = 0
        self.top.store = Store(self.root / "real.sqlite3")
        with patch("perf_top_capture.subprocess.Popen", return_value=process):
            state = self.finish(sample=self.top._sample)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["count"], 1)
        self.assertIn(payload, self.top.archive_path().read_bytes())
        imported = self.top.import_capture()
        summary = self.top.store.session(imported["id"])["summary"]
        self.assertEqual(summary["counts"], dict(S=1, P=1, D=0, DE=0, DP=0))
        self.assertEqual(self.top.status()["status"], "imported")

    def test_sample_command_is_explicit_rootless_and_bounded(self):
        process = Mock(stdout=io.BytesIO(TOP), returncode=0)
        process.poll.return_value = 0
        with patch("perf_top_capture.subprocess.Popen", return_value=process) as popen:
            output = io.BytesIO()
            self.assertEqual(self.top._sample("device-1", output), len(TOP))
            self.assertEqual(output.read(), TOP)
        self.assertEqual(popen.call_args.args[0],
                         ["fake-adb", "-s", "device-1", "shell", "-T", "top", "-b", "-n", "1", "-d", "1"])
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertTrue(process.stdout.closed)
        process = Mock(stdout=io.BytesIO(b"x" * 17), returncode=0)
        process.poll.return_value = None
        with patch("perf_top_capture.subprocess.Popen", return_value=process), \
                patch("perf_top_capture.MAX_SAMPLE_BYTES", 16):
            with self.assertRaisesRegex(ValueError, "16 MiB"):
                self.top._sample("device-1", io.BytesIO())
        process.kill.assert_called_once()
        self.assertTrue(process.stdout.closed)

    def test_sample_timeout_kills_and_reaps_process(self):
        killed = threading.Event()
        process = Mock(returncode=-9)
        process.poll.side_effect = lambda: -9 if killed.is_set() else None
        process.kill.side_effect = killed.set
        process.stdout.read1.side_effect = lambda size: (killed.wait(2), b"")[1]
        with patch("perf_top_capture.subprocess.Popen", return_value=process), \
                patch("perf_top_capture.TOP_TIMEOUT", 0.02):
            self.assert_status_error(504, self.top._sample, "device-1", io.BytesIO())
        process.kill.assert_called_once()
        process.wait.assert_called()
        process.stdout.close.assert_called_once()

    def test_sample_execution_errors_and_fsync_failure(self):
        with patch("perf_top_capture.subprocess.Popen", side_effect=FileNotFoundError):
            self.assert_status_error(503, self.top._sample, "device-1", io.BytesIO())
        process = Mock(stdout=io.BytesIO(b"offline"), returncode=1)
        process.poll.return_value = 1
        with patch("perf_top_capture.subprocess.Popen", return_value=process):
            self.assert_status_error(502, self.top._sample, "device-1", io.BytesIO())
        with patch("perf_top_capture.os.fsync", side_effect=[None, OSError("disk"), None]):
            state = self.finish(count=2)
        self.assertEqual(state["count"], 1)
        self.assertEqual(self.top.archive_path().stat().st_size, state["bytes"])


class TopApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = create_app(Path(temporary.name) / "test.db")
        self.top = self.app.state.top
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        token = self.client.get("/api/token").json()["token"]
        self.client.headers["x-session-token"] = token
        self.app.state.adb.devices = Mock(return_value=[{"serial": "device-1", "state": "device"}])

    def test_security_and_explicit_confirmation(self):
        for headers in ({"x-session-token": "wrong"}, {"origin": "http://evil.test"},
                        {"host": "evil.test"}, {"sec-fetch-site": "cross-site"}):
            self.assertEqual(self.client.get("/api/top/status", headers=headers).status_code, 403)
        for action in ("start", "stop", "import"):
            for confirm in (None, False, 1, "true"):
                body = {} if confirm is None else {"confirm": confirm}
                if action == "start":
                    body["serial"] = "device-1"
                self.assertEqual(self.client.post("/api/top/" + action, json=body).status_code, 422)
        for options in ({"count": 0}, {"count": True}, {"interval": 0}, {"root": True}):
            body = dict(confirm=True, serial="device-1", **options)
            self.assertEqual(self.client.post("/api/top/start", json=body).status_code, 422)
        self.assertEqual(self.client.get("/api/top/archive?capture_id=../secret").status_code, 422)
        self.assertEqual(self.client.get("/api/top/archive").status_code, 404)
        self.assertEqual(self.client.post("/api/top/import", json={"confirm": True}).status_code, 404)

    def test_status_codes_and_shared_import_lock(self):
        for code in (409, 502, 503, 504):
            with patch.object(self.top, "start", side_effect=AdbError("test", code)):
                response = self.client.post("/api/top/start", json={"confirm": True, "serial": "device-1"})
                self.assertEqual(response.status_code, code)
        with patch.object(self.top, "import_capture", side_effect=ValueError("parse")):
            self.assertEqual(self.client.post("/api/top/import", json={"confirm": True}).status_code, 400)
        self.top.import_lock.acquire()
        try:
            self.assertEqual(self.client.post("/api/import").status_code, 409)
            response = self.client.post("/api/adb/pull", json={"confirm": True, "serial": "device-1"})
            self.assertEqual(response.status_code, 409)
        finally:
            self.top.import_lock.release()

    def test_capture_download_task_switch_and_explicit_import(self):
        def sample(serial, stream):
            self.assertEqual(serial, "device-1")
            self.assertTrue(self.app.state.adb.lock.locked())
            stream.write(TOP)
            return len(TOP)
        with patch.object(self.top, "_sample", side_effect=sample):
            response = self.client.post("/api/top/start", json={"confirm": True, "serial": "device-1", "count": 1})
            self.assertEqual(response.status_code, 200)
            self.top._thread.join(2)
        first = self.client.get("/api/top/status").json()
        raw = self.client.get(first["archive"])
        self.assertEqual(raw.status_code, 200)
        self.assertRegex(raw.text, r"时间: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ==========")
        self.assertNotIn(" UTC ", raw.text)
        self.assertIn("attachment", raw.headers["content-disposition"])
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(serial, stream):
            entered.set()
            release.wait(3)
            return sample(serial, stream)
        with patch.object(self.top, "_sample", side_effect=blocked):
            response = self.client.post("/api/top/start", json={"confirm": True, "serial": "device-1", "count": 1})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.client.get("/api/top/archive").status_code, 409)
            self.assertEqual(self.client.get(first["archive"]).content, raw.content)
            self.assertEqual(self.client.post("/api/top/import", json={"confirm": True}).status_code, 409)
            self.assertEqual(self.client.post("/api/top/stop", json={"confirm": True}).status_code, 200)
            release.set()
            self.top._thread.join(2)
        body = {"confirm": True, "capture_id": first["id"]}
        self.assertEqual(self.client.post("/api/top/import", json=body).status_code, 409)
        body["capture_id"] = self.top.status()["id"]
        with patch.object(self.app.state.store, "import_files", return_value={"id": "session", "duplicate": False}) as importer:
            self.assertEqual(self.client.post("/api/top/import", json=body).json()["id"], "session")
            self.assertTrue(self.client.post("/api/top/import", json=body).json()["duplicate"])
            importer.assert_called_once()


if __name__ == "__main__":
    unittest.main()


