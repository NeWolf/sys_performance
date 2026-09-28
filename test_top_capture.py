"""Device-owned Top lifecycle, wire protocol and archive safety regressions."""
import hashlib
import io
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from perf_adb import AdbController, AdbError
from perf_api import create_app
from perf_top_capture import MAX_ARCHIVE_BYTES, REMOTE_ROOT, TopCapture

TOP = b"PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ ARGS\n1 root 20 0 1G 20M 1M S 12.5 1.0 0:01 app\n"
RAW = "========== Top 采集 #1 时间: 2026-09-22 12:34:56 ==========\n".encode() + TOP + b"\n\n"


class Device:
    """Fake the wire, not the controller: locking and decode stay real."""
    def __init__(self, adb):
        self.adb = adb
        self.jobs = {}
        self.commands = []
        self.payload = RAW
        self.fail_pull = False
        self.stop_delay = 0
        self.stop_pending = {}
        self.unconfirmed = False
        adb.devices = Mock(return_value=[{"serial": s, "state": "device"} for s in ("device-1", "device-2")])
        adb.shell = Mock(side_effect=self.shell)
        adb.run = Mock(side_effect=self.run)

    def shell(self, serial, command, **kwargs):
        assert self.adb.lock.locked()
        if command.startswith("[ ! -L"):
            return "ready"
        args = shlex.split(command)[2:]
        return self.action(serial, args)

    def run(self, *args, **kwargs):
        assert self.adb.lock.locked()
        command = shlex.split(args[-1])[2]
        if kwargs.get("output") is None:
            return self.shell(args[1], command)
        return self.action(args[1], shlex.split(command)[2:], kwargs.get("output"))

    def action(self, serial, args, output=None):
        self.commands.append((serial, args))
        action = args[0]
        if action == "start":
            identity, count, interval, title = args[1:]
            self.jobs[serial] = [identity, "running", "0", count, interval, "0", "none", title]
        elif action == "pull":
            output.write(self.payload)
            if self.fail_pull:
                raise AdbError("disconnected", 502)
        elif action == "clear":
            if self.jobs[serial][-1] != "cleaned":
                self.jobs[serial].append("cleaned")
        elif action == "stop":
            if serial not in self.jobs or self.jobs[serial][0] != args[1]:
                raise AdbError("Capture identity changed", 502)
            self.stop_pending[serial] = self.stop_delay
        elif action == "status":
            delay = self.stop_pending.get(serial)
            if delay is not None and delay >= 0:
                if delay == 0 and not self.unconfirmed:
                    self.jobs[serial][1] = "stopped"
                    self.stop_pending.pop(serial)
                else:
                    self.stop_pending[serial] = max(0, delay - 1)
        else:
            raise AssertionError(action)
        result = "\n".join(self.jobs[serial]).strip() if serial in self.jobs else "idle"
        if self.unconfirmed and action == "status" and serial in self.jobs:
            result = "\n".join(self.jobs[serial]) + "\n1700000045\nunconfirmed"
        return result

    def finish(self, serial="device-1"):
        job = self.jobs[serial]
        job[1:3] = ["completed", "1"]
        job[5] = str(len(RAW))


class TopCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adb = AdbController(self.root / "adb", executable="fake-adb")
        self.device = Device(self.adb)
        self.store = Mock()
        self.store.import_files.return_value = {"id": "session-1", "duplicate": False}
        self.import_lock = threading.Lock()
        self.top = self.controller()

    def controller(self):
        top = TopCapture(self.adb, self.root / "top", self.store, self.import_lock)
        self.addCleanup(top.close)
        return top

    def error(self, code, function, *args):
        with self.assertRaises(AdbError) as caught:
            function(*args)
        self.assertEqual(caught.exception.status, code)

    def finished(self):
        state = self.top.start("device-1", count=1, title=" test ")
        self.device.finish()
        self.top.status("device-1")
        return state["id"]

    def pulled(self):
        identity = self.finished()
        return self.top.pull("device-1", identity)

    def test_script_upload_normalizes_line_endings_before_hashing(self):
        normalized = self.top.script.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        remote = REMOTE_ROOT + "/control-" + hashlib.sha256(normalized).hexdigest() + ".sh"
        source = self.root / "windows checkout" / "device_top_capture.sh"
        source.parent.mkdir()
        self.top.script = source
        for newline in (b"\n", b"\r\n", b"\r"):
            with self.subTest(newline=newline):
                original = normalized.replace(b"\n", newline)
                source.write_bytes(original)
                adb = Mock()
                self.top.adb = adb
                adb.shell.side_effect = ["", "", "idle"]
                uploaded = []

                def push(*args, **kwargs):
                    self.assertEqual(args[:3], ("-s", "device-1", "push"))
                    local = Path(args[3])
                    uploaded.append(local)
                    self.assertNotEqual(local, source)
                    self.assertEqual(local.read_bytes(), normalized)
                    self.assertNotIn(b"\r", local.read_bytes())
                    self.assertTrue(args[4].startswith(remote + "."))
                    self.assertEqual(kwargs["timeout"], 30)

                adb.run.side_effect = push
                self.assertEqual(self.top._command("device-1", "status"), "idle")
                adb.run.assert_called_once()
                self.assertIn(remote, adb.shell.call_args_list[0].args[1])
                self.assertEqual(adb.shell.call_args.args[1], "sh " + remote + " status")
                self.assertFalse(uploaded[0].exists())
                self.assertEqual(source.read_bytes(), original)

    def test_script_cache_uses_normalized_hash_without_upload(self):
        normalized = b"#!/system/bin/sh\numask 077\n"
        source = self.root / "device_top_capture.sh"
        source.write_bytes(normalized.replace(b"\n", b"\r\n"))
        self.top.script = source
        adb = Mock()
        self.top.adb = adb
        adb.shell.side_effect = ["ready", "idle"]
        with patch("perf_top_capture.tempfile.TemporaryDirectory") as temporary:
            self.assertEqual(self.top._command("device-1", "status"), "idle")
        temporary.assert_not_called()
        adb.run.assert_not_called()
        remote = REMOTE_ROOT + "/control-" + hashlib.sha256(normalized).hexdigest() + ".sh"
        self.assertIn(remote, adb.shell.call_args_list[0].args[1])
        self.assertNotIn(hashlib.sha256(source.read_bytes()).hexdigest(),
                         adb.shell.call_args_list[0].args[1])
        self.assertEqual(adb.shell.call_args.args[1], "sh " + remote + " status")

    def test_script_upload_failure_does_not_publish_or_execute(self):
        adb = Mock()
        self.top.adb = adb
        adb.shell.return_value = ""
        uploaded = []

        def failed_push(*args, **kwargs):
            local = Path(args[3])
            self.assertTrue(local.is_file())
            uploaded.append(local)
            raise AdbError("upload failed", 502)

        adb.run.side_effect = failed_push
        with self.assertRaisesRegex(AdbError, "upload failed"):
            self.top._command("device-1", "status")
        adb.shell.assert_called_once()
        self.assertFalse(uploaded[0].exists())

    def test_clear_keeps_local_archive_across_refresh_and_restart(self):
        state = self.pulled()
        identity = state["id"]
        cleared = self.top.clear("device-1", identity)
        self.assertTrue(cleared["remote_cleaned"])
        self.assertIsNone(cleared["remote_path"])
        self.assertEqual(cleared["archive"], state["archive"])
        self.assertTrue(self.top.status("device-1")["remote_cleaned"])
        self.assertEqual(self.top.archive_path(identity).read_bytes(), RAW)
        self.error(409, self.top.pull, "device-1", identity)
        restored = self.controller()
        self.assertTrue(restored.status("device-1")["remote_cleaned"])
        self.assertEqual(restored.archive_path(identity).read_bytes(), RAW)
        self.assertEqual(restored.import_capture(identity)["id"], "session-1")
        self.assertNotEqual(self.top.start("device-1")["id"], identity)

    def test_clear_rejects_active_stale_and_disconnected_tasks(self):
        identity = self.top.start("device-1")["id"]
        self.error(409, self.top.clear, "device-1", identity)
        self.error(409, self.top.clear, "device-1", "f" * 32)
        self.error(400, self.top.clear, "device-1", "../outside")
        self.device.stop_delay = -1
        with patch("perf_top_capture.STOP_TIMEOUT_SECONDS", 0.01):
            self.error(504, self.top.stop, "device-1", identity)
        self.error(409, self.top.clear, "device-1", identity)
        self.adb.devices.return_value = []
        with self.assertRaises(AdbError):
            self.top.clear("device-1", identity)
        self.assertFalse(any(args[0] == "clear" for _, args in self.device.commands))

    def test_clear_without_local_archive_does_not_invent_one(self):
        identity = self.finished()
        self.top.clear("device-1", identity)
        self.assertIsNone(self.top.status("device-1")["archive"])
        self.error(404, self.top.archive_path, identity)

    def test_capture_timing_and_legacy_status(self):
        self.top.start("device-1")
        legacy = "\n".join(self.device.jobs["device-1"])
        for raw in (legacy, legacy + "\n1700000045"):
            self.assertIsNone(self.top._decode("device-1", raw)["started_at"])
        self.device.jobs["device-1"] += ["1700000000", "0", "1700000045"]
        state = self.top.status("device-1")
        self.assertEqual(state["started_at"], 1700000000)
        self.assertEqual(state["elapsed_seconds"], 45)
        self.assertIsNone(state["ended_at"])
        self.assertEqual(self.controller().status("device-1")["elapsed_seconds"], 45)
        self.adb.devices.return_value = []
        self.assertEqual(self.top.status("device-1")["elapsed_seconds"], 45)
        self.device.finish()
        self.device.jobs["device-1"][9:] = ["1700000060", "1700000900"]
        stopped = self.top._decode("device-1", "\n".join(self.device.jobs["device-1"]))
        self.assertEqual(stopped["elapsed_seconds"], 60)
        self.assertEqual(stopped["ended_at"], 1700000060)
        self.device.jobs["device-1"][8] = "invalid"
        self.error(502, self.top._decode, "device-1", "\n".join(self.device.jobs["device-1"]))

    def test_detached_restart_discovery_and_disconnect_cache(self):
        state = self.top.start("device-1")
        self.assertTrue(state["running"])
        self.assertFalse(self.adb.lock.locked())
        self.assertEqual(state["max_bytes"], 1_000_000_000)
        calls = len(self.device.commands)
        self.top.close()
        self.assertEqual(len(self.device.commands), calls)
        restarted = self.controller()
        found = restarted.status("device-1")
        self.assertEqual(found["id"], state["id"])
        self.adb.devices.return_value = []
        lost = restarted.status("device-1")
        self.assertFalse(lost["connected"])
        self.assertTrue(lost["running"])
        self.assertTrue(lost["connection_error"])
        self.assertEqual(lost["id"], state["id"])
        calls = len(self.device.commands)
        self.assertEqual(restarted.status(), lost)
        self.assertEqual(len(self.device.commands), calls)

    def test_identity_stop_waits_for_confirmation(self):
        identity = self.top.start("device-1")["id"]
        self.error(409, self.top.start, "device-1")
        self.error(400, self.top.stop, "device-1", "../pid")
        self.error(409, self.top.stop, "device-1", "f" * 32)
        self.device.stop_delay = 2
        with patch("perf_top_capture.STOP_POLL_SECONDS", 0):
            state = self.top.stop("device-1", identity)
        self.assertFalse(state["running"])
        self.assertFalse(state["stopping"])
        self.assertEqual(state["status"], "stopped")
        self.assertFalse(self.adb.lock.locked())
        self.assertFalse(any(args[0] == "pull" for _, args in self.device.commands))

    def test_running_pull_stops_before_reading_archive(self):
        identity = self.top.start("device-1")["id"]
        self.device.jobs["device-1"][2] = "1"
        self.device.jobs["device-1"][5] = str(len(RAW))
        state = self.top.pull("device-1", identity)
        self.assertFalse(state["running"])
        self.assertTrue(state["archive"])
        actions = [args[0] for _, args in self.device.commands]
        self.assertLess(actions.index("stop"), actions.index("pull"))

    def test_stop_timeout_keeps_retry_available_and_never_pulls(self):
        identity = self.top.start("device-1")["id"]
        self.device.stop_delay = -1
        with patch("perf_top_capture.STOP_TIMEOUT_SECONDS", 0.01):
            self.error(504, self.top.pull, "device-1", identity)
        state = self.top.status()
        self.assertFalse(state["stopping"])
        self.assertFalse(state["connected"])
        self.assertTrue(state["running"])
        self.assertTrue(state["connection_error"])
        self.assertFalse(self.adb.lock.locked())
        self.assertFalse(any(args[0] == "pull" for _, args in self.device.commands))
        self.device.stop_delay = 0
        self.assertFalse(self.top.stop("device-1", identity)["running"])

    def test_empty_running_capture_stops_without_publishing_archive(self):
        identity = self.top.start("device-1")["id"]
        self.error(404, self.top.pull, "device-1", identity)
        self.assertFalse(self.top.status()["running"])
        self.assertFalse(any(args[0] == "pull" for _, args in self.device.commands))

    def test_unconfirmed_legacy_identity_survives_restart_and_can_retry(self):
        identity = self.top.start("device-1")["id"]
        self.device.unconfirmed = True
        restarted = self.controller()
        state = restarted.status("device-1")
        self.assertEqual(state["id"], identity)
        self.assertFalse(state["connected"])
        self.assertTrue(state["running"])
        with patch("perf_top_capture.STOP_TIMEOUT_SECONDS", 0.01):
            self.error(504, restarted.pull, "device-1", identity)
        self.assertTrue(any(args[0] == "stop" for _, args in self.device.commands))
        self.assertFalse(any(args[0] == "pull" for _, args in self.device.commands))
        self.device.unconfirmed = False
        self.assertFalse(restarted.stop("device-1", identity)["running"])

    def test_task_switch_after_stop_prevents_pull(self):
        identity = self.top.start("device-1")["id"]
        original = self.device.action
        def switched(serial, args, output=None):
            result = original(serial, args, output)
            if args[0] == "stop":
                self.device.jobs[serial][0] = "f" * 32
            return result
        with patch.object(self.device, "action", side_effect=switched):
            self.error(409, self.top.pull, "device-1", identity)
        self.assertFalse(any(args[0] == "pull" for _, args in self.device.commands))

    def test_status_failure_after_marker_can_retry_without_pull(self):
        identity = self.top.start("device-1")["id"]
        original = self.device.action
        def failing(serial, args, output=None):
            if args[0] == "status":
                raise AdbError("recovery unavailable", 502)
            return original(serial, args, output)
        with patch.object(self.device, "action", side_effect=failing):
            self.error(502, self.top.pull, "device-1", identity)
        self.assertFalse(self.top.status()["stopping"])
        self.assertTrue(any(args[0] == "stop" for _, args in self.device.commands))
        self.assertFalse(any(args[0] == "pull" for _, args in self.device.commands))
        self.assertFalse(self.top.stop("device-1", identity)["running"])

    def test_recovered_legacy_zero_start_does_not_invent_duration(self):
        self.top.start("device-1")
        self.device.jobs["device-1"] += ["0", "1700000060", "1700000900"]
        state = self.top.status("device-1")
        self.assertIsNone(state["started_at"])
        self.assertIsNone(state["elapsed_seconds"])

    def test_stop_commands_share_finite_deadline(self):
        identity = self.top.start("device-1")["id"]
        self.adb.run.reset_mock()
        self.top.stop("device-1", identity)
        self.assertTrue(self.adb.run.call_args_list)
        for call in self.adb.run.call_args_list:
            self.assertGreater(call.kwargs["timeout"], 0)
            self.assertLessEqual(call.kwargs["timeout"], 10)

    def test_explicit_pull_import_and_poll_preserves_metadata(self):
        identity = self.finished()
        self.error(404, self.top.archive_path)
        state = self.top.pull("device-1", identity)
        self.assertIn(identity, state["archive"])
        self.assertEqual(self.top.archive_path().read_bytes(), RAW)
        self.store.import_files.assert_not_called()
        self.top.import_capture(identity)
        self.assertTrue(self.top.import_capture(identity)["duplicate"])
        self.store.import_files.assert_called_once()
        polled = self.top.status("device-1")
        self.assertEqual(polled["status"], "imported")
        self.assertEqual(polled["session_id"], "session-1")
        self.assertEqual(polled["archive"], state["archive"])
        self.assertFalse(polled["importing"])
        before = sum(args[0] == "pull" for _, args in self.device.commands)
        self.top.pull("device-1", identity)
        self.assertEqual(sum(args[0] == "pull" for _, args in self.device.commands), before)
        restarted = self.controller()
        self.assertEqual(restarted.status("device-1")["archive"], state["archive"])
        self.assertEqual(restarted.archive_path().read_bytes(), RAW)

    def test_failed_pull_never_publishes_and_can_retry(self):
        identity = self.finished()
        self.device.fail_pull = True
        self.error(502, self.top.pull, "device-1", identity)
        self.assertFalse((self.top.archive_root / (identity + ".txt")).exists())
        self.device.fail_pull = False
        self.device.payload = RAW[:-1]
        self.error(502, self.top.pull, "device-1", identity)
        self.device.payload = RAW
        with patch("perf_top_capture.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.top.pull("device-1", identity)
        self.error(404, self.top.archive_path)
        self.top.pull("device-1", identity)
        self.assertEqual(self.top.archive_path().read_bytes(), RAW)

    def test_pull_stream_over_limit(self):
        identity = self.finished()
        self.device.payload = RAW + b"x"
        with patch("perf_top_capture.MAX_ARCHIVE_BYTES", len(RAW)):
            self.error(413, self.top.pull, "device-1", identity)
        self.error(404, self.top.archive_path)

    def test_existing_and_symlink_archive_not_overwritten(self):
        identity = self.finished()
        self.top.archive_root.mkdir()
        path = self.top.archive_root / (identity + ".txt")
        path.write_bytes(b"user data")
        self.error(409, self.top.pull, "device-1", identity)
        self.assertEqual(path.read_bytes(), b"user data")
        other = self.root / "outside"
        other.write_bytes(RAW)
        self.device.jobs["device-1"][0] = "a" * 32
        linked = self.top.archive_root / ("a" * 32 + ".txt")
        linked.symlink_to(other)
        self.error(409, self.top.pull, "device-1", "a" * 32)
        self.error(404, self.top.archive_path)
        self.assertEqual(other.read_bytes(), RAW)

    def test_import_lock_retry_and_close_wait(self):
        state = self.pulled()
        self.import_lock.acquire()
        try:
            self.error(409, self.top.import_capture)
        finally:
            self.import_lock.release()
        self.store.import_files.side_effect = ValueError("parse")
        with self.assertRaises(ValueError):
            self.top.import_capture()
        self.assertFalse(self.import_lock.locked())
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked(*args):
            entered.set()
            release.wait(3)
            return {"id": "done", "duplicate": False}
        self.store.import_files.side_effect = blocked
        worker = threading.Thread(target=self.top.import_capture)
        worker.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.top.status("device-1")["status"], "importing")
        self.top.status("device-2")
        self.assertEqual(self.top.status()["id"], state["id"])
        self.error(409, self.top.start, "device-2")
        closer = threading.Thread(target=lambda: (self.top.close(), closed.set()))
        closer.start()
        self.assertFalse(closed.wait(.05))
        release.set()
        worker.join(3)
        closer.join(3)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.top.status()["session_id"], "done")
        self.error(503, self.top.start, "device-1")

    def test_strict_device_state_and_parameters(self):
        identity = self.finished()
        for index, value in ((0, "../secret"), (1, "imported"), (2, "-1"),
                             (3, "0"), (4, "nan"), (5, str(MAX_ARCHIVE_BYTES + 1)),
                             (6, "$(shell)"), (7, "ff")):
            fields = list(self.device.jobs["device-1"])
            fields[index] = value
            self.error(502, self.top._decode, "device-1", "\n".join(fields))
        for count in (True, 0, -2, 100001):
            with self.assertRaises(ValueError):
                self.top.start("device-1", count=count)
        for interval in (True, float("nan"), float("inf"), 0, 3601):
            with self.assertRaises(ValueError):
                self.top.start("device-1", interval=interval)
        self.error(400, self.top.status, "device; id")
        self.assertEqual(self.top.status("device-1")["id"], identity)

    def test_pull_task_switch_does_not_publish(self):
        identity = self.finished()
        original = self.device.action
        def switch(serial, args, output=None):
            result = original(serial, args, output)
            if args[0] == "pull":
                self.device.jobs[serial][0] = "b" * 32
            return result
        self.device.action = switch
        self.assertRaises(AdbError, self.top.pull, "device-1", identity)
        self.assertFalse((self.top.archive_root / (identity + ".txt")).exists())

    def test_import_survives_same_device_task_switch(self):
        identity = self.pulled()["id"]
        def import_and_switch(*args):
            self.device.jobs["device-1"] = ["c" * 32, "running", "0", "-1", "1", "0", "none", ""]
            switched = self.top.status("device-1")
            self.assertEqual(switched["id"], "c" * 32)
            self.assertEqual(self.top.status()["id"], identity)
            return {"id": "old-session", "duplicate": False}
        self.store.import_files.side_effect = import_and_switch
        self.top.import_capture(identity)
        self.assertEqual(self.top.status()["session_id"], "old-session")
        latest = self.top.status("device-1")
        self.assertEqual(latest["id"], "c" * 32)
        self.assertIsNone(latest["session_id"])
        self.assertEqual(self.top.archive_path(identity).read_bytes(), RAW)

    def test_partial_writes_and_atomic_publish_collision(self):
        class ShortWriter(io.BytesIO):
            def write(self, data):
                return super().write(data[:3])
        stream = ShortWriter()
        self.top._write_all(stream, RAW)
        self.assertEqual(stream.getvalue(), RAW)
        with self.assertRaises(OSError):
            self.top._write_all(Mock(write=Mock(return_value=0)), RAW)
        identity = self.finished()
        with patch("perf_top_capture.os.link", side_effect=FileExistsError):
            self.error(409, self.top.pull, "device-1", identity)
        self.error(404, self.top.archive_path)
        self.top.pull("device-1", identity)
        self.assertEqual(self.top.archive_path().read_bytes(), RAW)

    def test_private_staging_collision_preserves_existing_file(self):
        identity = self.finished()
        self.top.archive_root.mkdir()
        pending = self.top.archive_root / (identity + "." + "e" * 32 + ".part")
        pending.write_bytes(b"existing private file")
        with patch("perf_top_capture.uuid.uuid4", return_value=Mock(hex="e" * 32)):
            with self.assertRaises(FileExistsError):
                self.top.pull("device-1", identity)
        self.assertEqual(pending.read_bytes(), b"existing private file")
        self.error(404, self.top.archive_path)
        self.top.pull("device-1", identity)
        self.assertEqual(pending.read_bytes(), b"existing private file")

    def test_api_protocol_auth_confirmation_and_explicit_pull(self):
        with patch("perf_api.TopCapture", return_value=self.top):
            app = create_app(self.root / "api.sqlite3")
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            self.assertEqual(client.get("/api/top/status").status_code, 403)
            headers = {"X-Session-Token": client.get("/api/token").json()["token"]}
            foreign = dict(headers, Origin="http://evil.example")
            self.assertEqual(client.get("/api/top/status", headers=foreign).status_code, 403)
            client.headers.update(headers)
            body = {"confirm": True, "serial": "device-1"}
            for invalid in (dict(body, confirm=1), dict(body, confirm=False),
                            dict(body, path="/tmp"), dict(body, count=True),
                            dict(body, count=0), dict(body, interval=True)):
                self.assertEqual(client.post("/api/top/start", json=invalid).status_code, 422)
            response = client.post("/api/top/start", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            identity = response.json()["id"]
            task = dict(body, capture_id=identity)
            for operation in ("stop", "pull", "clear"):
                url = "/api/top/" + operation
                for invalid in (body, {"confirm": True, "capture_id": identity},
                                dict(task, confirm="true"), dict(task, pid=123),
                                dict(task, capture_id="../outside")):
                    self.assertEqual(client.post(url, json=invalid).status_code, 422)
                self.assertEqual(client.post(url, json=dict(task, capture_id="f" * 32)).status_code, 409)
            self.assertEqual(client.post("/api/top/clear", json=task).status_code, 409)
            self.device.stop_delay = -1
            with patch("perf_top_capture.STOP_TIMEOUT_SECONDS", 0.01):
                self.assertEqual(client.post("/api/top/pull", json=task).status_code, 504)
            self.device.stop_delay = 0
            self.adb.devices.return_value = []
            lost = client.get("/api/top/status", params={"serial": "device-1"}).json()
            self.assertTrue(lost["running"])
            self.assertFalse(lost["connected"])
            self.assertEqual(client.get("/api/top/status").json(), lost)
            self.adb.devices.return_value = [{"serial": "device-1", "state": "device"}]
            self.assertEqual(client.post("/api/top/stop", json=task).status_code, 200)
            self.device.finish()
            client.get("/api/top/status", params={"serial": "device-1"})
            self.assertEqual(client.get("/api/top/archive").status_code, 404)
            pulled = client.post("/api/top/pull", json=task)
            self.assertEqual(pulled.status_code, 200, pulled.text)
            cleared = client.post("/api/top/clear", json=task)
            self.assertEqual(cleared.status_code, 200, cleared.text)
            self.assertTrue(cleared.json()["remote_cleaned"])
            self.assertEqual(client.get(pulled.json()["archive"]).content, RAW)
            imported = client.post("/api/top/import", json={"confirm": True, "capture_id": identity})
            self.assertEqual(imported.status_code, 200, imported.text)
            self.assertTrue(client.post("/api/top/import", json={"confirm": True}).json()["duplicate"])
            self.assertEqual(client.get("/api/top/archive", params={"capture_id": "../x"}).status_code, 422)


@unittest.skipIf(os.name == "nt", "POSIX shell harness")
class DeviceScriptTests(unittest.TestCase):
    """Run the worker logic with explicit mocks for Android-only capabilities."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.identity = "d" * 32
        self.job = self.root / self.identity
        self.job.mkdir()
        self.source = Path(__file__).with_name("device_top_capture.sh").read_text()
        self.script = self.root / "control.sh"
        self.fixture = self.root / "fixture"
        self.fixture.write_bytes(TOP)
        self.command("awk", 'case "$*" in */proc/*/stat) echo "$PPID";; *) exec /usr/bin/awk "$@";; esac')
        self.command("timeout", 'shift 3; exec "$@"')
        self.command("top", 'cat "$TOP_FIXTURE"')
        # macOS lacks truncate; use Python only in the test harness.
        import sys
        self.command("truncate", shlex.quote(sys.executable) +
                     ' -c \'import sys; f=open(sys.argv[2], "a+b"); f.truncate(int(sys.argv[1])); f.close()\' "$2" "$3"')
        # Real POSIX advisory locking on inherited descriptors, also on macOS.
        self.command("flock", shlex.quote(sys.executable) +
                     ' -c \'import fcntl,sys; fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)\' "$2"')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        TOP_FIXTURE=str(self.fixture), WORKER_LOCK=str(self.job / "worker.lock"))
        self.proc = self.root / "proc"
        (self.proc / "1" / "fd").mkdir(parents=True)
        (self.proc / "1" / "cmdline").write_bytes(b"init\0")
        (self.proc / "mounts").write_text(f"proc {self.proc} proc rw 0 0\n")
        self.proc_stat(1)
        (self.proc / "self").symlink_to("99999")
        # macOS readlink does not offer Android's multi-operand contract.
        self.batch_readlink = (shlex.quote(sys.executable) + ' -c ' + shlex.quote(
            "import os,sys\nrc=0\nfor p in sys.argv[1:]:\n"
            " try: print(os.readlink(p))\n except OSError: rc=1\nsys.exit(rc)"
        ) + ' "$@"')
        self.command("readlink", 'case "$1" in /proc/*/fd/0) echo "$WORKER_LOCK";; '
                     '/proc/*/fd/[12]) echo /dev/null;; *) ' + self.batch_readlink + ';; esac')

    def proc_stat(self, pid, start=100):
        path = self.proc / str(pid)
        path.mkdir(exist_ok=True)
        (path / "stat").write_text(f"{pid} (name with ) spaces) S " +
                                   "0 " * 18 + str(start) + " 0\n")
        return path

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o700)

    def write_script(self, limit=MAX_ARCHIVE_BYTES):
        # macOS /dev/fd reports devfs inodes, unlike Linux /proc descriptors.
        # Compare the actual inherited fd via fstat rather than mocking identity.
        import sys
        self.command("same_stdin", shlex.quote(sys.executable) + " -c " + shlex.quote(
            "import os,sys; a=os.stat(sys.argv[1]); b=os.fstat(0); "
            "sys.exit((a.st_dev,a.st_ino)!=(b.st_dev,b.st_ino))"
        ) + ' "$1"')
        self.script.write_text(self.source.replace("ROOT=" + REMOTE_ROOT,
                               "ROOT=" +shlex.quote(str(self.root))).replace(
                                   "MAX_BYTES=1000000000", "MAX_BYTES=" + str(limit)).replace(
                                       "PROC=/proc", "PROC=" + shlex.quote(str(self.proc))).replace(
                                           '[ "$DIR/worker.lock" -ef /proc/$$/fd/0 ]',
                                           'same_stdin "$DIR/worker.lock"'))

    def worker(self, limit=MAX_ARCHIVE_BYTES, target=1):
        (self.job / "worker.lock").touch()
        self.write_script(limit)
        result = subprocess.run(["sh", str(self.script), "worker", self.identity,
                                 str(target), "1", ""], env=self.env,
                                capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.job / "done").read_text().strip(), self.identity)
        return (self.job / "state").read_text().splitlines()

    def clear_command(self, identity=None):
        return subprocess.run(["sh", str(self.script), "clear", identity or self.identity],
                              env=self.env, capture_output=True, text=True, timeout=5)

    def prepare_clear(self):
        self.worker()
        (self.root / "current").write_text(self.identity)
        # Exercise the lock call contract on macOS; native flock is Android-only.
        self.command("flock", '[ "$1" = -n ] && [ "$2" = 0 ]')

    def test_clear_all_finished_logs_preserves_metadata_and_other_files(self):
        self.prepare_clear()
        historical = self.root / ("a" * 32)
        historical.mkdir()
        for name in ("raw.txt", "sample", "header", "state", "done"):
            content = (self.job / name).read_bytes().replace(self.identity.encode(), b"a" * 32)
            (historical / name).write_bytes(content)
        untouched = self.root / "sysmonitor.log"
        untouched.write_bytes(b"keep sysmonitor")
        control = self.script.read_bytes()
        before = (self.job / "state").read_bytes()
        result = self.clear_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        for job in (self.job, historical):
            for name in ("raw.txt", "sample", "header"):
                self.assertEqual((job / name).stat().st_size, 0)
            self.assertTrue((job / "cleaned").is_file())
        self.assertEqual((self.job / "state").read_bytes(), before)
        self.assertEqual(untouched.read_bytes(), b"keep sysmonitor")
        self.assertEqual(self.script.read_bytes(), control)
        status = subprocess.run(["sh", str(self.script), "status"], env=self.env,
                                capture_output=True, text=True, check=True)
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", status.stdout.strip())
        self.assertTrue(decoded["remote_cleaned"])
        self.assertEqual(self.clear_command().returncode, 0)
        pull = subprocess.run(["sh", str(self.script), "pull", self.identity],
                              env=self.env, capture_output=True)
        self.assertNotEqual(pull.returncode, 0)

    def test_clear_preflight_rejects_active_stale_symlink_and_lock_conflicts(self):
        self.prepare_clear()
        original = (self.job / "raw.txt").read_bytes()
        self.assertNotEqual(self.clear_command("f" * 32).returncode, 0)
        (self.job / "done").write_text("f" * 32)
        self.assertNotEqual(self.clear_command().returncode, 0)
        (self.job / "done").write_text(self.identity)
        state = (self.job / "state").read_text()
        (self.job / "state").write_text(state.replace("completed", "running"))
        self.assertNotEqual(self.clear_command().returncode, 0)
        (self.job / "state").write_text(state)
        self.command("flock", "exit 1")
        self.assertNotEqual(self.clear_command().returncode, 0)
        self.command("flock", "exit 0")
        (self.job / "cleaned").symlink_to(self.fixture)
        self.assertNotEqual(self.clear_command().returncode, 0)
        self.assertEqual((self.job / "raw.txt").read_bytes(), original)
        self.assertEqual(self.fixture.read_bytes(), TOP)

    def test_clear_rejects_unsafe_or_unfinished_historical_tasks(self):
        for kind in ("symlink", "unfinished", "linked_archive"):
            with self.subTest(kind=kind):
                self.prepare_clear()
                original = (self.job / "raw.txt").read_bytes()
                other = self.root / ({"symlink": "a", "unfinished": "b", "linked_archive": "c"}[kind] * 32)
                if kind == "symlink":
                    other.symlink_to(self.job, target_is_directory=True)
                else:
                    other.mkdir()
                    if kind == "linked_archive":
                        (other / "raw.txt").symlink_to(self.fixture)
                self.assertNotEqual(self.clear_command().returncode, 0)
                self.assertEqual((self.job / "raw.txt").read_bytes(), original)
                self.assertEqual(self.fixture.read_bytes(), TOP)
                # Retain fixtures without leaving them in the task-ID namespace.
                other.rename(self.root / (kind + "-checked"))

    def shell(self, mode, *args):
        return subprocess.run(["sh", str(self.script), mode, *args], env=self.env,
                              capture_output=True, text=True, timeout=4)

    def orphan(self, legacy=False, lines=10, status="running"):
        self.write_script()
        (self.root / "current").write_text(self.identity)
        fields = [self.identity, status, "1", "-1", "1", str(len(RAW)),
                  "none", "", "100", "0"]
        (self.job / "state").write_text("\n".join(fields[:lines]) + "\n")
        (self.job / "raw.txt").write_bytes(RAW + b"partial uncommitted sample")
        if not legacy:
            (self.job / "worker.lock").touch()

    def reject_worker_entry(self, *flags, stdin=None):
        paths = [self.job / name for name in ("raw.txt", "state", "done")]
        before = {p: p.read_bytes() if p.exists() else None for p in paths}
        result = subprocess.run(["sh", str(self.script), "worker", self.identity,
                                 "1", "1", "", *flags], env=self.env, stdin=stdin,
                                capture_output=True, timeout=4)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        for path, content in before.items():
            self.assertEqual(path.read_bytes() if path.exists() else None, content)
        for name in ("ready", "sample", "header", "state.new"):
            self.assertFalse((self.job / name).exists(), name)

    def test_worker_rejects_forged_inheritance_and_missing_lock(self):
        self.orphan()
        lock = self.job / "worker.lock"
        lock.write_bytes(b"do not truncate lock")
        for name in (os.devnull, self.fixture, self.job / "raw.txt"):
            with self.subTest(stdin=str(name)), open(name, "rb") as stream:
                self.reject_worker_entry("locked", stdin=stream)
        self.assertEqual(lock.read_bytes(), b"do not truncate lock")
        # A stale inherited descriptor must not stand in for the current inode.
        with lock.open("rb") as inherited:
            lock.rename(self.job / "old-lock")
            lock.write_bytes(b"replacement")
            self.reject_worker_entry("locked", stdin=inherited)
            lock.rename(self.job / "replacement-lock")
            for flags in ((), ("locked",)):
                with self.subTest(flags=flags):
                    self.reject_worker_entry(*flags, stdin=inherited)
                    self.assertFalse(lock.exists())
        # Preserve an existing completion marker as well, even for bad entries.
        (self.job / "done").write_text(self.identity + "\n")
        self.reject_worker_entry("locked")
        self.assertFalse(lock.exists())

    def test_worker_rejects_conflicting_or_unsafe_lock(self):
        import fcntl
        self.orphan()
        lock = self.job / "worker.lock"
        lock.write_bytes(b"existing lock contents")
        with lock.open("rb") as owner, lock.open("rb") as other:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.reject_worker_entry()
            self.reject_worker_entry("locked", stdin=other)
        self.assertEqual(lock.read_bytes(), b"existing lock contents")
        self.command("flock", "exit 2")
        with lock.open("rb") as inherited:
            self.reject_worker_entry("locked", stdin=inherited)
            self.reject_worker_entry()
        lock.rename(self.job / "real-lock")
        lock.symlink_to(self.job / "real-lock")
        with lock.open("rb") as stream:
            self.reject_worker_entry("locked", stdin=stream)
            self.reject_worker_entry()
        self.assertEqual(lock.read_bytes(), b"existing lock contents")

    def test_worker_accepts_real_inherited_lock(self):
        import fcntl
        self.orphan(status="starting")
        (self.job / "raw.txt").write_bytes(b"")
        lock = self.job / "worker.lock"
        with lock.open("rb") as inherited:
            fcntl.flock(inherited, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(["sh", str(self.script), "worker", self.identity,
                                     "1", "1", "", "locked"], env=self.env,
                                    stdin=inherited, capture_output=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            with lock.open("rb") as competitor:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = (self.job / "state").read_text().splitlines()
        self.assertEqual(state[1:3], ["completed", "1"])
        self.assertEqual((self.job / "done").read_text().strip(), self.identity)
        self.assertEqual(len((self.job / "raw.txt").read_bytes()), int(state[5]))

    def test_orphan_recovery_commits_only_state_bytes_for_both_formats(self):
        for lines in (8, 10):
            with self.subTest(lines=lines):
                self.orphan(lines=lines)
                self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
                before = time.monotonic()
                self.assertEqual(self.shell("stop", self.identity).returncode, 0)
                self.assertLess(time.monotonic() - before, 2)
                result = self.shell("status")
                self.assertEqual(result.returncode, 0, result.stderr)
                state = (self.job / "state").read_text().splitlines()
                self.assertEqual(state[1:3], ["stopped", "1"])
                self.assertEqual(len(state), 10)
                self.assertEqual((self.job / "raw.txt").read_bytes(), RAW)
                self.assertEqual((self.job / "done").read_text().strip(), self.identity)
                self.assertEqual(self.shell("pull", self.identity).stdout.encode(), RAW)

    def test_status_recovers_orphan_without_claiming_requested_stop(self):
        self.orphan()
        result = self.shell("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[1], "error")
        self.assertEqual(result.stdout.splitlines()[6], "interrupted")
        self.assertEqual((self.job / "raw.txt").read_bytes(), RAW)

    def test_live_lifetime_lock_and_control_lock_block_recovery(self):
        import fcntl
        self.orphan(status="starting")
        original = (self.job / "raw.txt").read_bytes()
        with (self.job / "worker.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.shell("stop", self.identity).returncode, 0)
            status = self.shell("status")
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(status.stdout.splitlines()[1], "starting")
            self.assertFalse((self.job / "done").exists())
            self.assertEqual((self.job / "raw.txt").read_bytes(), original)
            self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
        with (self.root / "control.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertNotEqual(self.shell("status").returncode, 0)
            self.assertNotEqual(self.shell("stop", self.identity).returncode, 0)
            self.assertFalse((self.job / "done").exists())
        self.assertEqual(self.shell("status").returncode, 0)
        self.assertTrue((self.job / "done").exists())

    def test_legacy_orphan_recovery_and_exact_live_worker_identity(self):
        self.orphan(legacy=True, lines=8)
        command = ["sh", str(self.root / ("control-" + "a" * 64 + ".sh")),
                   "worker", self.identity, "-1", "1", ""]
        (self.proc / "1" / "cmdline").write_bytes("\0".join(command).encode() + b"\0")
        self.assertEqual(self.shell("stop", self.identity).returncode, 0)
        result = self.shell("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[1], "running")
        self.assertFalse((self.job / "done").exists())
        command[1] = str(self.root / "unrecognised-controller.sh")
        (self.proc / "1" / "cmdline").write_bytes("\0".join(command).encode() + b"\0")
        self.assertEqual(self.shell("status").stdout.splitlines()[-1], "unconfirmed")
        self.assertFalse((self.job / "done").exists())
        command[3] += "f"  # Similar ID is not this worker.
        (self.proc / "1" / "cmdline").write_bytes("\0".join(command).encode() + b"\0")
        result = self.shell("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[1], "stopped")
        self.assertEqual((self.job / "raw.txt").read_bytes(), RAW)

    def test_signals_wait_for_live_child_before_publishing_done(self):
        import signal
        import sys
        for phase in ("top", "append"):
            for sig in (signal.SIGTERM, signal.SIGINT):
                with self.subTest(phase=phase, signal=sig):
                    self.orphan(status="starting")
                    (self.job / "raw.txt").write_bytes(b"")
                    entered = self.job / "child-entered"
                    release = self.job / "child-release"
                    exited = self.job / "child-exited"
                    # A real foreground child writes a partial sample, then
                    # waits for the test. Signals target only our Popen child.
                    code = (
                        "import os,sys,time\nfrom pathlib import Path\n"
                        "job=Path(os.environ['WORKER_LOCK']).parent\n"
                        "data=b''.join(Path(p).read_bytes() for p in sys.argv[1:])\n"
                        "half=len(data)//2\nos.write(1,data[:half])\n"
                        "(job/'child-entered').write_text(str(os.getpid()))\n"
                        "deadline=time.monotonic()+10\n"
                        "while not (job/'child-release').exists():\n"
                        " if time.monotonic()>deadline: sys.exit(1)\n"
                        " time.sleep(.02)\n"
                        "os.write(1,data[half:])\n"
                        "(job/'child-exited').touch()\n"
                    )
                    writer = "exec " + shlex.quote(sys.executable) + " -c " + shlex.quote(code)
                    self.command("cat", 'exec /bin/cat "$@"')
                    self.command("top", 'exec /bin/cat "$TOP_FIXTURE"')
                    if phase == "top":
                        self.command("top", writer + ' "$TOP_FIXTURE"')
                    else:
                        self.command("cat", writer + ' "$@"')
                    with open(os.devnull, "wb") as output:
                        process = subprocess.Popen(
                            ["sh", str(self.script), "worker", self.identity, "1", "1", ""],
                            env=self.env, stdout=output, stderr=output)
                        try:
                            deadline = time.monotonic() + 5
                            while not entered.exists() and time.monotonic() < deadline:
                                time.sleep(.02)
                            self.assertTrue(entered.exists())
                            original = (self.job / "raw.txt").read_bytes()
                            state = (self.job / "state").read_bytes()
                            process.send_signal(sig)
                            time.sleep(.25)
                            self.assertIsNone(process.poll())
                            self.assertFalse(exited.exists())
                            os.kill(int(entered.read_text()), 0)
                            self.assertFalse((self.job / "done").exists())
                            self.assertEqual((self.job / "state").read_bytes(), state)
                            self.assertEqual((self.job / "raw.txt").read_bytes(), original)
                        finally:
                            release.touch()
                            process.wait(timeout=12)
                    self.assertEqual(process.returncode, 0)
                    self.assertTrue(exited.exists())
                    state = (self.job / "state").read_text().splitlines()
                    self.assertEqual(state[1], "stopped")
                    self.assertEqual(state[6], "interrupted")
                    self.assertEqual(state[2], "0" if phase == "top" else "1")
                    raw = (self.job / "raw.txt").read_bytes()
                    self.assertEqual(len(raw), int(state[5]))
                    if phase == "append":
                        self.assertTrue(raw.endswith(TOP + b"\n\n"))
                    self.assertEqual((self.job / "done").read_text().strip(), self.identity)
                    self.job.rename(self.root / f"checked-{phase}-{sig}")
                    self.job.mkdir()

    def test_real_worker_remains_active_until_cooperative_stop(self):
        self.orphan()
        # The mocked top still runs as a real shell child and holds stdin's
        # lifetime lock. Never signal the worker or infer any process ID.
        self.command("top", 'sleep 1; cat "$TOP_FIXTURE"')
        with open(os.devnull, "wb") as output:
            process = subprocess.Popen(["sh", str(self.script), "worker", self.identity,
                                        "-1", "1", ""], env=self.env,
                                       stdout=output, stderr=output)
            try:
                deadline = time.monotonic() + 3
                while not (self.job / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((self.job / "ready").exists())
                status = self.shell("status")
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(status.stdout.splitlines()[1], "running")
                self.assertFalse((self.job / "done").exists())
                self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
                self.assertEqual(self.shell("stop", self.identity).returncode, 0)
            finally:
                (self.job / "stop").touch()
                process.wait(timeout=15)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(self.shell("status").stdout.splitlines()[1], "stopped")
        self.assertEqual(len((self.job / "raw.txt").read_bytes()),
                         int((self.job / "state").read_text().splitlines()[5]))

    def test_inherited_writer_lock_survives_controller_exit(self):
        import fcntl
        self.orphan()
        with (self.job / "worker.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            child = subprocess.Popen(["sh", "-c", "sleep 2"], stdin=lock)
        try:
            self.assertEqual(self.shell("status").stdout.splitlines()[1], "running")
            self.assertFalse((self.job / "done").exists())
        finally:
            child.wait(timeout=5)
        self.assertEqual(self.shell("status").returncode, 0)
        self.assertEqual((self.job / "raw.txt").read_bytes(), RAW)

    def test_legacy_unknown_visibility_and_delayed_launch_refuse_recovery(self):
        self.orphan(legacy=True)
        original = (self.job / "raw.txt").read_bytes()
        (self.proc / "mounts").write_text(f"proc {self.proc} proc rw,hidepid=2 0 0\n")
        result = self.shell("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "unconfirmed")
        self.assertIn("retry", result.stderr)
        self.assertFalse((self.job / "done").exists())
        self.assertEqual((self.job / "raw.txt").read_bytes(), original)
        self.assertEqual(self.shell("stop", self.identity).returncode, 0)
        self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
        (self.proc / "mounts").write_text(f"proc {self.proc} proc rw 0 0\n")
        (self.proc / "1" / "cmdline").rename(self.proc / "unreadable-cmdline")
        self.assertEqual(self.shell("status").stdout.splitlines()[-1], "unconfirmed")
        (self.proc / "unreadable-cmdline").rename(self.proc / "1" / "cmdline")
        self.orphan(legacy=True, status="starting")
        self.assertEqual(self.shell("status").stdout.splitlines()[-1], "unconfirmed")
        self.assertFalse((self.job / "done").exists())

    def test_legacy_hidepid_group_exemption_keeps_access_checks(self):
        self.orphan(legacy=True)
        self.command("id", 'echo "2000 3009 3011"')
        original = (self.job / "raw.txt").read_bytes()
        for options in ("hidepid=2,gid=309", "hidepid=4,gid=3009",
                        "hidepid=invisible,gid=3009,subset=pid"):
            with self.subTest(options=options):
                (self.proc / "mounts").write_text(f"proc {self.proc} proc rw,{options} 0 0\n")
                self.assertEqual(self.shell("status").stdout.splitlines()[-1], "unconfirmed")
                self.assertFalse((self.job / "done").exists())
                self.assertEqual((self.job / "raw.txt").read_bytes(), original)
        (self.proc / "mounts").write_text(f"proc {self.proc} proc rw,gid=3009,hidepid=invisible 0 0\n")
        command = self.proc / "1" / "cmdline"
        command.rename(self.proc / "hidden-cmdline")
        self.assertEqual(self.shell("status").stdout.splitlines()[-1], "unconfirmed")
        self.assertFalse((self.job / "done").exists())
        (self.proc / "hidden-cmdline").rename(command)
        self.assertEqual(self.shell("stop", self.identity).returncode, 0)
        self.assertEqual(self.shell("status").stdout.splitlines()[1], "stopped")
        self.assertEqual((self.job / "raw.txt").read_bytes(), RAW)

    def test_legacy_orphan_append_child_prevents_truncation(self):
        self.orphan(legacy=True)
        (self.proc / "1" / "cmdline").write_bytes(b"cat\0header\0sample\0")
        descriptor = self.proc / "1" / "fd" / "7"
        descriptor.symlink_to(self.job / "raw.txt")
        original = (self.job / "raw.txt").read_bytes()
        result = self.shell("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.job / "done").exists())
        self.assertEqual((self.job / "raw.txt").read_bytes(), original)
        descriptor.rename(self.proc / "closed-fd")
        self.assertEqual(self.shell("status").returncode, 0)
        self.assertEqual((self.job / "raw.txt").read_bytes(), RAW)

    def scan_legacy(self, expected):
        function = self.source.split("legacy_quiet()", 1)[1].split("\nrecover()", 1)[0]
        script = ("ROOT=" + shlex.quote(str(self.root)) + "\nPROC=" +
                  shlex.quote(str(self.proc)) + "\nID=" + self.identity +
                  "\nDIR=$ROOT/$ID\nlegacy_quiet()" + function + "\nlegacy_quiet\n")
        state = (self.job / "state").read_bytes()
        raw = (self.job / "raw.txt").read_bytes()
        result = subprocess.run(["sh"], input=script, env=self.env,
                                capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual((self.job / "state").read_bytes(), state)
        self.assertEqual((self.job / "raw.txt").read_bytes(), raw)
        self.assertFalse((self.job / "worker.lock").exists())
        self.assertFalse((self.job / "done").exists())

    def race_readlink(self, body):
        import sys
        code = ("import os,sys\nfrom pathlib import Path\n" +
                "proc=Path(" + repr(str(self.proc)) + ")\n" +
                "job=Path(" + repr(str(self.job)) + ")\n" + body + "\n" +
                "rc=0\nfor p in sys.argv[1:]:\n"
                " try: print(os.readlink(p))\n except OSError: rc=1\nsys.exit(rc)\n")
        self.command("readlink", shlex.quote(sys.executable) + " -c " +
                     shlex.quote(code) + ' "$@"')

    def test_legacy_closed_fd_gets_one_local_retry(self):
        self.orphan(legacy=True)
        (self.proc / "1/fd/7").symlink_to("/dev/null")
        self.race_readlink("p=proc/'1/fd/7'\nif p.is_symlink(): p.rename(proc/'closed')")
        self.scan_legacy(0)

    def test_legacy_live_unreadable_fd_and_unknown_batch_failure_refuse(self):
        self.orphan(legacy=True)
        (self.proc / "1/fd/7").symlink_to("/dev/null")
        for body in ('echo 99999; echo 99999; exit 1',
                     self.batch_readlink + '; exit 1'):
            with self.subTest(body=body):
                self.command("readlink", body)
                self.scan_legacy(2)

    def test_legacy_closed_fd_does_not_hide_another_unreadable_fd(self):
        self.orphan(legacy=True)
        for fd in (7, 8):
            (self.proc / f"1/fd/{fd}").symlink_to("/dev/null")
        self.race_readlink("(proc/'1/fd/7').rename(proc/'closed')\n"
                           "print('99999\\n99999\\n99999'); sys.exit(1)")
        self.scan_legacy(2)

    def test_legacy_retry_exhaustion_remains_unconfirmed(self):
        self.orphan(legacy=True)
        (self.proc / "1/fd/7").symlink_to("/dev/null")
        self.race_readlink("p=proc/'1/fd/7'\n"
                           "if p.is_symlink():\n p.rename(proc/'closed7')\n"
                           " (proc/'1/fd/8').symlink_to('/dev/null')\n"
                           "else: (proc/'1/fd/8').rename(proc/'closed8')")
        self.scan_legacy(2)

    def test_legacy_vanished_process_and_pid_reuse_are_distinct(self):
        self.orphan(legacy=True)
        p = self.proc_stat(2)
        (p / "cmdline").write_bytes(b"cat\0")
        (p / "fd").mkdir()
        (p / "fd/7").symlink_to("/dev/null")
        self.race_readlink("(proc/'2').rename(proc/'departed')")
        self.scan_legacy(0)
        (self.proc / "departed").rename(p)
        self.race_readlink("p=proc/'2/stat'\np.write_text(p.read_text().replace('100', '101'))")
        self.scan_legacy(2)

    def test_legacy_cmdline_and_fd_listing_identity_races(self):
        self.orphan(legacy=True)
        p = self.proc_stat(2)
        (p / "cmdline").write_bytes(b"cat\0")
        (p / "fd").mkdir()
        for command in ("tr", "ls"):
            executable = "/bin/ls" if command == "ls" else "/usr/bin/tr"
            for race in ("vanished", "reused", "unreadable", "unknown"):
                with self.subTest(command=command, race=race):
                    self.proc_stat(2)
                    marker = self.proc / (command + "-" + race)
                    departed = self.proc / "departed"
                    if race == "vanished":
                        action = "mv " + shlex.quote(str(p)) + " " + shlex.quote(str(departed))
                    elif race == "reused":
                        action = "printf '%s\\n' " + shlex.quote(
                            "2 (cat) S " + "0 " * 18 + "101 0") + " > " + shlex.quote(str(p / "stat"))
                    elif race == "unreadable":
                        action = "mv " + shlex.quote(str(p / "stat")) + " " + shlex.quote(str(marker) + "-stat")
                    else:
                        action = "exit 1"
                    # The first invocation scans init; the second scans PID 2.
                    self.command(command, "if [ -e " + shlex.quote(str(marker)) +
                                 " ]; then " + action + "; else : > " +
                                 shlex.quote(str(marker)) + "; fi\nexec " + executable + ' "$@"')
                    self.scan_legacy(0 if race == "vanished" else 2)
                    self.command(command, "exec " + executable + ' "$@"')
                    if race == "vanished":
                        departed.rename(p)

    def test_legacy_stat_missing_or_malformed_is_not_disappearance(self):
        self.orphan(legacy=True)
        stat = self.proc / "1/stat"
        for value in ("1 (comm) S 0\n", "1 (comm) S " + "0 " * 18 + "bad\n"):
            stat.write_text(value)
            self.scan_legacy(2)
        stat.rename(self.proc / "unreadable-stat")
        self.scan_legacy(2)

    def test_legacy_added_identity_is_scanned_including_orphan_writer(self):
        self.orphan(legacy=True)
        (self.proc / "1/fd/7").symlink_to("/dev/null")
        self.race_readlink("p=proc/'2'\nif not p.exists():\n"
                           " (p/'fd').mkdir(parents=True)\n"
                           " (p/'stat').write_text('2 (cat) S '+ '0 '*18+'200 0\\n')\n"
                           " (p/'cmdline').write_bytes(b'cat\\0')\n"
                           " (p/'fd/1').symlink_to(job/'raw.txt')")
        self.scan_legacy(1)
        (self.proc / "2/fd/1").rename(self.proc / "closed-writer")
        self.scan_legacy(0)

    def test_legacy_unbounded_new_identities_refuse(self):
        self.orphan(legacy=True)
        (self.proc / "1/fd/7").symlink_to("/dev/null")
        self.race_readlink("n=2\nwhile (proc/str(n)).exists(): n+=1\np=proc/str(n)\n"
                           "(p/'fd').mkdir(parents=True)\n"
                           "(p/'stat').write_text(str(n)+' (cat) S '+'0 '*18+'200 0\\n')\n"
                           "(p/'cmdline').write_bytes(b'cat\\0')\n"
                           "(p/'fd/1').symlink_to('/dev/null')")
        self.scan_legacy(2)

    def test_legacy_partial_batch_keeps_writer_reference_protection(self):
        self.orphan(legacy=True)
        for name in ('raw.txt', 'sample', 'header'):
            for deleted in ('', ' (deleted)'):
                with self.subTest(name=name, deleted=deleted):
                    self.command("readlink", "printf '%s\\n' 99999 " +
                                 shlex.quote(str(self.job / name) + deleted) +
                                 " 99999; exit 1")
                    if not (self.proc / '1/fd/7').is_symlink():
                        (self.proc / '1/fd/7').symlink_to('/dev/null')
                    self.scan_legacy(1)

    def test_recovery_rejects_short_archive_invalid_state_and_symlink(self):
        self.orphan()
        (self.job / "raw.txt").write_bytes(b"short")
        self.assertEqual(self.shell("status").stdout.splitlines()[-1], "unconfirmed")
        self.assertEqual((self.job / "raw.txt").read_bytes(), b"short")
        self.assertFalse((self.job / "done").exists())
        self.orphan()
        state = (self.job / "state").read_text()
        (self.job / "state").write_text(state.replace(str(len(RAW)), "-1"))
        self.assertNotEqual(self.shell("status").returncode, 0)
        (self.job / "state").write_text(state)
        (self.job / "raw.txt").rename(self.job / "safe-archive")
        (self.job / "raw.txt").symlink_to(self.fixture)
        self.assertEqual(self.shell("status").stdout.splitlines()[-1], "unconfirmed")
        self.assertEqual(self.fixture.read_bytes(), TOP)
        self.assertFalse((self.job / "done").exists())

    def test_pull_rejects_done_before_terminal_publication(self):
        self.orphan()
        (self.job / "done").write_text(self.identity + "\n")
        self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)

    def test_complete_sample_and_exact_capacity_boundary(self):
        state = self.worker(limit=len(RAW))
        archive = (self.job / "raw.txt").read_bytes()
        self.assertEqual(state[1:3], ["completed", "1"])
        self.assertEqual(int(state[5]), len(RAW))
        self.assertTrue(archive.endswith(TOP + b"\n\n"))
        self.assertGreater(int(state[8]), 0)
        self.assertGreaterEqual(int(state[9]), int(state[8]))
        (self.root / "current").write_text(self.identity)
        result = subprocess.run(["sh", str(self.script), "status"], env=self.env,
                                capture_output=True, text=True, check=True)
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", result.stdout.strip())
        self.assertEqual(decoded["elapsed_seconds"], int(state[9]) - int(state[8]))
        state = self.worker(limit=len(RAW) - 1)
        self.assertEqual(state[1:3], ["stopped", "0"])
        self.assertEqual(state[5:7], ["0", "size_limit"])
        self.assertEqual((self.job / "raw.txt").stat().st_size, 0)

    def test_compact_header_and_invalid_sample(self):
        self.fixture.write_bytes(TOP.replace(b"%CPU", b"[%CPU]"))
        self.assertEqual(self.worker()[1], "completed")
        self.fixture.write_bytes(b"top: unsupported options\n")
        self.assertEqual(self.worker()[6], "invalid_top")
        self.assertEqual((self.job / "raw.txt").stat().st_size, 0)

    def test_timeout_failure_and_file_size_limit_never_commit(self):
        self.command("timeout", "exit 137")
        self.assertEqual(self.worker()[6], "top_failed_or_timeout")
        self.command("timeout", 'shift 3; exec "$@"')
        self.command("top", 'head -c 9437184 /dev/zero')
        self.assertEqual(self.worker()[6], "top_failed_or_timeout")
        self.assertLessEqual((self.job / "sample").stat().st_size, 8388608)
        self.assertEqual((self.job / "raw.txt").stat().st_size, 0)

    def test_detachment_failure_and_cooperative_stop(self):
        self.orphan()
        self.command("readlink", 'case "$1" in /proc/*/fd/0) echo "$WORKER_LOCK";; *) echo /dev/pts/1;; esac')
        self.reject_worker_entry()
        self.command("readlink", 'case "$1" in /proc/*/fd/0) echo "$WORKER_LOCK";; /proc/*/fd/[12]) echo /dev/null;; *) exec /usr/bin/readlink "$@";; esac')
        (self.job / "stop").touch()
        state = self.worker(target=-1)
        self.assertEqual(state[1:3], ["stopped", "0"])
        self.assertFalse((self.job / "sample").exists())


if __name__ == "__main__":
    unittest.main()

