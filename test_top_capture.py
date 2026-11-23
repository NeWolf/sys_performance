"""Device-owned Top lifecycle, wire protocol and archive safety regressions."""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote

from fastapi.testclient import TestClient

from perf_adb import AdbController, AdbError
from perf_api import create_app
from perf_store import Store
from perf_top_capture import (MAX_ARCHIVE_BYTES, REMOTE_ROOT, REBOOT_WARNING,
                              RECOVERY_WARNING, CLEAR_WARNING, TopCapture)

TOP = b"PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ ARGS\n1 root 20 0 1G 20M 1M S 12.5 1.0 0:01 app\n"
RAW = "========== Top 采集 #1 时间: 2026-09-22 12:34:56 ==========\n".encode() + TOP + b"\n\n"


class Device:
    """Fake the wire, not the controller: locking and decode stay real."""
    def __init__(self, adb, root):
        self.adb = adb
        self.root = root
        self.remote = root / "shared"
        self.legacy = root / "legacy"
        self.remote.mkdir()
        payload = Path(__file__).with_name("device_top_capture.sh").read_bytes()
        payload = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        self.cache = self.remote / ("control-" + hashlib.sha256(payload).hexdigest() + ".sh")
        self.cache.write_bytes(payload)
        self.version = "toybox 0.8.9-android"
        self.version_status = 0
        self.ls_error = None
        self.ls_status = 1
        self.hash_error = None
        self.script_hash_error = None
        self.before_shell = None
        self.before_push = None
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

    def checked_shell(self, serial, command, output=None, *, via_stdin=False):
        # Run the controller's complete guard in a real shell/filesystem. Only
        # Android toybox output formatting and the device action are emulated.
        if self.before_shell:
            self.before_shell(command)
        command = command.replace(REMOTE_ROOT, str(self.remote)).replace(
            "/data/local/tmp/sysmonitor_top_capture", str(self.legacy))
        command = command.replace("/system/bin/toybox", "test_toybox")
        invocation = None
        if "; sh " in command:
            command, invocation = command.rsplit("; sh ", 1)
        helper = (
            "import hashlib, os, sys\n"
            "action = sys.argv[1]\n"
            "if action == '--version':\n"
            f" print({self.version!r}); sys.exit({self.version_status!r})\n"
            "elif action == 'ls':\n"
            " path = sys.argv[-1]\n"
            f" forced = {self.ls_error!r}\n"
            " if forced:\n"
            f"  print('ls: ' + path + ': ' + forced, file=sys.stderr); sys.exit({self.ls_status!r})\n"
            " try: os.lstat(path)\n"
            " except OSError as exc:\n"
            "  print('ls: ' + path + ': ' + exc.strerror, file=sys.stderr); sys.exit(1)\n"
            " print(path)\n"
            "elif action == 'sha256sum':\n"
            f" forced = {self.hash_error!r}\n"
            " if forced == 'missing': sys.exit(127)\n"
            " data = sys.stdin.buffer.read()\n"
            f" if data != b'abc': forced = {self.script_hash_error!r} or forced\n"
            " if forced == 'missing': sys.exit(127)\n"
            " print((forced or hashlib.sha256(data).hexdigest()) + '  -')\n"
            "else: sys.exit(127)\n"
        )
        shim = 'test_toybox() { ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(helper) + ' "$@"; }; '
        result = subprocess.run(["sh", "-s"], input=AdbController.shell_input(shim + command).decode(),
                                capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise AdbError(result.stderr.strip(), 502)
        if invocation is not None:
            return self.action(serial, shlex.split(invocation)[1:], output)
        return result.stdout.strip()

    def shell(self, serial, command, **kwargs):
        assert self.adb.lock.locked()
        assert not kwargs.get("root"), "Top must not use su"
        if command == "id -u":
            return "2000"
        return self.checked_shell(serial, command)

    def run(self, *args, **kwargs):
        assert self.adb.lock.locked()
        assert args[2] not in {"root", "unroot", "wait-for-device", "reconnect"}, "Top must not restart adbd"
        if args[2] == "push":
            if self.before_push:
                self.before_push(args)
            target = Path(args[4].replace(REMOTE_ROOT, str(self.remote)))
            target.write_bytes(Path(args[3]).read_bytes())
            return ""
        assert args[-1] == "sh -s", "Top guard must not enter process argv"
        payload = kwargs["input_data"].decode("utf-8")
        assert payload.startswith("(\n") and payload.endswith("\n)\n")
        return self.checked_shell(args[1], payload[2:-3], kwargs.get("output"), via_stdin=True)

    def action(self, serial, args, output=None):
        self.commands.append((serial, args))
        action = args[0]
        job = self.jobs.get(serial)
        if job and job[0] == "corrupt" and action != "start":
            if action in {"stop", "clear", "pull", "pull-live", "clear-live"} and job[1] != args[1]:
                raise AdbError("Capture identity changed", 502)
            if action in {"clear", "clear-live"}:
                raise AssertionError("Corrupt tasks must never be cleared automatically")
            if action in {"pull", "pull-live"}:
                raise AssertionError("Corrupt logs must not be pulled")
            return "\n".join(job)
        if action == "start":
            identity, count, interval, title = args[1:]
            self.jobs[serial] = [identity, "running", "0", count, interval, "0", "none", title]
        elif action in {"pull", "pull-live"}:
            output.write(self.payload)
            if self.fail_pull:
                raise AdbError("disconnected", 502)
        elif action == "clear-live":
            if "clear_unconfirmed" not in self.jobs[serial]:
                self.jobs[serial].append("clear_unconfirmed")
            self.payload = b""
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
                if delay == 0 and not self.unconfirmed and "rebooted" not in self.jobs[serial]:
                    self.jobs[serial][1] = "stopped"
                    self.stop_pending.pop(serial)
                else:
                    self.stop_pending[serial] = max(0, delay - 1)
        else:
            raise AssertionError(action)
        fields = list(self.jobs[serial]) if serial in self.jobs else ["idle"]
        cleared = "clear_unconfirmed" in fields
        if cleared:
            fields.remove("clear_unconfirmed")
        result = "\n".join(fields).strip()
        if self.unconfirmed and action == "status" and serial in self.jobs:
            result = "\n".join(fields) + "\n1700000045\nunconfirmed"
        if cleared:
            result += "\nclear_unconfirmed"
        return result

    def finish(self, serial="device-1"):
        job = self.jobs[serial]
        job[1:3] = ["completed", "1"]
        job[5] = str(len(RAW))


class TopCaptureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.adb = AdbController(self.root / "adb", executable="fake-adb")
        self.device = Device(self.adb, self.root)
        legacy_patch = patch("perf_top_capture.LEGACY_ROOT", str(self.device.legacy))
        legacy_patch.start()
        self.addCleanup(legacy_patch.stop)
        self.store = Mock()
        self.store.import_files.return_value = {"id": "session-1", "duplicate": False}
        self.import_lock = threading.Lock()
        self.top = self.controller()

    def controller(self):
        top = TopCapture(self.adb, self.root / "top_logs", self.store, self.import_lock)
        self.addCleanup(top.close)
        return top

    def error(self, code, function, *args):
        with self.assertRaises(AdbError) as caught:
            function(*args)
        self.assertEqual(caught.exception.status, code)

    @contextmanager
    def stop_timeout(self):
        # Advance only controller time at polling sleeps. Real guard subprocesses
        # still run, but their host startup cost cannot consume the test budget.
        clock = Mock(wraps=time)
        clock.monotonic.return_value = 0.0
        def advance(seconds):
            clock.monotonic.return_value += seconds
        clock.sleep.side_effect = advance
        with patch("perf_top_capture.time", clock), patch("perf_top_capture.STOP_TIMEOUT_SECONDS", 0.01):
            yield

    def finished(self):
        state = self.top.start("device-1", count=1, title=" test ")
        self.device.finish()
        self.top.status("device-1")
        return state["id"]

    def pulled(self):
        identity = self.finished()
        return self.top.pull("device-1", identity)

    def naming_metadata(self, path):
        return json.loads(path.with_suffix(".name.json").read_text(encoding="utf-8"))

    def rebooted_job(self, size=None):
        identity = self.top.start("device-1", count=-1)["id"]
        self.device.jobs["device-1"] = [identity, "running", "1", "-1", "1",
            str(len(RAW) if size is None else size), "none", "", "100", "0", "500", "rebooted"]
        return identity

    def test_reboot_stop_confirms_once_without_polling_or_log_operations(self):
        identity = self.rebooted_job()
        self.device.commands.clear()
        with patch("perf_top_capture.time.sleep") as sleep:
            state = self.top.stop("device-1", identity)
        sleep.assert_not_called()
        self.assertEqual([args[0] for _, args in self.device.commands], ["stop", "status"])
        self.assertEqual(state["error"], "device_rebooted")
        self.assertEqual(state["status"], "error")
        self.assertFalse(state["running"])
        self.assertTrue(state["connected"])
        self.assertEqual(state["started_at"], 100)
        self.assertIsNone(state["ended_at"])
        self.assertIsNone(state["elapsed_seconds"])
        self.assertEqual(state["warning"], REBOOT_WARNING)

    def test_reboot_protocol_rejects_conflicting_or_duplicate_markers(self):
        self.rebooted_job()
        fields = self.device.jobs["device-1"]
        invalid = [fields + ["rebooted"], fields[:-1] + ["unconfirmed", "rebooted"],
                   fields[:-1] + ["rebooted", "cleaned"]]
        for status in ("stopped", "completed", "error"):
            invalid.append([fields[0], status] + fields[2:])
        ended = fields.copy()
        ended[9] = "400"
        invalid.append(ended)
        for values in invalid:
            with self.subTest(values=values):
                self.error(502, self.top._decode, "device-1", "\n".join(values))
        busy = self.top._decode("device-1", "\n".join(fields + ["control_busy"]))
        self.assertTrue(busy["running"])
        self.assertFalse(busy["connected"])
        self.assertIn("控制锁", busy["connection_error"])

    def test_reboot_pull_preserves_tail_and_warning_across_restart_and_import(self):
        identity = self.rebooted_job()
        self.device.payload = RAW + b"partial uncommitted sample"
        state = self.top.pull("device-1", identity)
        path = self.top.archive_path(identity)
        self.assertEqual(path.read_bytes(), self.device.payload)
        self.assertIn(REBOOT_WARNING, state["warning"])
        self.assertIn("字节数不一致", state["warning"])
        marker = json.loads(path.with_suffix(".warning.json").read_text())
        self.assertEqual(marker["reason"], "device_rebooted")
        restarted = self.controller()
        restored = restarted.status("device-1")
        self.assertTrue(restored["archive"])
        self.assertEqual(restored["warning"], state["warning"])
        self.assertIsNone(restored["elapsed_seconds"])
        restarted.import_capture(identity)
        self.assertTrue(restarted._import_done.wait(5))
        imported = restarted.status("device-1")
        self.assertEqual(imported["status"], "imported")
        self.assertEqual(imported["error"], "device_rebooted")
        self.assertEqual(imported["warning"], state["warning"])
        self.assertNotIn("clear", [args[0] for _, args in self.device.commands])

    def test_reboot_pull_with_equal_or_zero_recorded_bytes_keeps_warning(self):
        for size in (len(RAW), 0):
            with self.subTest(size=size):
                identity = self.rebooted_job(size=size)
                state = self.top.pull("device-1", identity)
                self.assertIn(REBOOT_WARNING, state["warning"])
                self.assertEqual("字节数不一致" in state["warning"], size == 0)
                self.assertEqual(self.top.archive_path(identity).read_bytes(), RAW)
                restored = self.controller().status("device-1")
                self.assertEqual(restored["warning"], state["warning"])
                self.assertTrue(restored["archive"])

    def test_reboot_warning_metadata_is_strict_and_merges_without_duplicates(self):
        path = self.root / "archive.txt"
        marker = path.with_suffix(".warning.json")
        valid = dict(size=1, before=1, after=1, reason="device_rebooted")
        marker.write_text(json.dumps(valid))
        self.assertEqual(self.top._saved_warning(path, 1), REBOOT_WARNING)
        for change in ({"reason": "unknown"}, {"extra": 1}, {"size": True},
                       {"before": -1}, {"after": MAX_ARCHIVE_BYTES + 1}, {"size": 2}):
            marker.write_text(json.dumps(dict(valid, **change)))
            self.assertIsNone(self.top._saved_warning(path, 1))
        self.assertEqual(self.top._merge_warnings(REBOOT_WARNING, REBOOT_WARNING, "other"),
                         REBOOT_WARNING + "\nother")

    def test_model_metadata_does_not_affect_ascii_download_name(self):
        model = "中文车机"
        self.adb.devices.return_value = [{"serial": "device-1", "state": "device", "model": model}]
        identity = self.pulled()["id"]
        path = self.top.archive_path(identity)
        expected = "Top_202609221234.txt"
        self.assertRegex(path.name, r"^Top[0-9]{14}\.txt$")
        self.assertEqual(self.top.archive_filename(path), expected)
        self.assertEqual(self.naming_metadata(path)["device_name"], model)
        with patch("perf_api.TopCapture", return_value=self.top):
            app = create_app(self.root / "naming-api.sqlite3")
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            client.headers["X-Session-Token"] = client.get("/api/token").json()["token"]
            response = client.get("/api/top/archive", params={"capture_id": identity})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, RAW)
            self.assertEqual(response.headers["content-disposition"],
                             'attachment; filename="' + expected + '"')

    def test_unknown_time_download_uses_rfc5987_header(self):
        identity = self.finished()
        self.device.payload = b"not a timestamp\n"
        self.device.jobs["device-1"][5] = str(len(self.device.payload))
        self.top.pull("device-1", identity)
        with patch("perf_api.TopCapture", return_value=self.top):
            app = create_app(self.root / "unknown-time-api.sqlite3")
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            client.headers["X-Session-Token"] = client.get("/api/token").json()["token"]
            response = client.get("/api/top/archive", params={"capture_id": identity})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, self.device.payload)
            self.assertEqual(response.headers["content-disposition"],
                             "attachment; filename*=utf-8''" + quote("Top_未知时间.txt"))

    def test_serial_fallback_for_missing_or_empty_model(self):
        for model in (None, ""):
            with self.subTest(model=model):
                device = {"serial": "device-1", "state": "device"}
                if model is not None:
                    device["model"] = model
                self.adb.devices.return_value = [device]
                identity = self.pulled()["id"]
                path = self.top.archive_path(identity)
                self.assertEqual(self.top.archive_filename(path), "Top_202609221234.txt")
                naming = self.naming_metadata(path)
                self.assertEqual(naming["device_name"], "device-1")
                self.assertEqual(naming["naming_time_source"], "first_record")

    def test_real_started_at_seconds_win_and_convert_to_milliseconds(self):
        started = 1790663405
        identity = self.finished()
        self.device.jobs["device-1"] += [str(started), str(started + 1), str(started + 2)]
        self.top.pull("device-1", identity)
        path = self.top.archive_path(identity)
        expected = "Top_" + time.strftime("%Y%m%d%H%M", time.localtime(started))
        naming = self.naming_metadata(path)
        self.assertEqual(self.top.archive_filename(path), expected + ".txt")
        self.assertEqual(naming["capture_started_at"], started * 1000)
        self.assertEqual(naming["naming_time_source"], "capture")
        self.top.import_capture(identity)
        kwargs = self.store.import_files.call_args.kwargs
        self.assertEqual(kwargs["capture_method"], "Top")
        self.assertEqual(kwargs["device_name"], "device-1")
        self.assertEqual(kwargs["started_at"], started * 1000)
        self.assertEqual(kwargs.get("naming_snapshot"), naming)
        self.assertEqual(self.store.import_files.call_args.args[0][0][0], path.name)

    def test_first_record_fallback_and_unknown_time(self):
        cases = ((RAW, "202609221234", "first_record"),
                 (b"not a timestamp\n", "未知时间", "unknown"))
        for payload, stamp, source in cases:
            with self.subTest(source=source):
                identity = self.finished()
                self.device.payload = payload
                self.device.jobs["device-1"][5] = str(len(payload))
                self.top.pull("device-1", identity)
                path = self.top.archive_path(identity)
                naming = self.naming_metadata(path)
                self.assertEqual(self.top.archive_filename(path), "Top_" + stamp + ".txt")
                self.assertEqual(naming["naming_time_source"], source)
                if source == "unknown":
                    self.assertIsNone(naming["capture_started_at"])
                else:
                    first = time.mktime(time.strptime("2026-09-22 12:34:56", "%Y-%m-%d %H:%M:%S"))
                    self.assertEqual(naming["capture_started_at"], first * 1000)
                self.top.import_capture(identity)
                kwargs = self.store.import_files.call_args.kwargs
                self.assertIsNone(kwargs["started_at"])
                self.assertEqual(kwargs.get("naming_snapshot"), naming)

    def test_naming_snapshot_survives_restart_model_change_and_task_switch(self):
        self.adb.devices.return_value = [{"serial": "device-1", "state": "device", "model": "原车机"}]
        identity = self.pulled()["id"]
        path = self.top.archive_path(identity)
        marker = path.with_suffix(".name.json")
        saved = marker.read_bytes()
        naming = self.naming_metadata(path)
        self.assertEqual(naming["name"], "Top_202609221234")
        self.adb.devices.return_value[0]["model"] = "新车机"
        self.top.status("device-1")
        recovered = self.controller()
        self.assertTrue(recovered.status("device-1")["archive"])
        self.assertEqual(recovered.archive_filename(path), naming["name"] + ".txt")
        recovered.import_capture(identity)
        imported_snapshot = self.store.import_files.call_args.kwargs.get("naming_snapshot")
        newer = recovered.start("device-1", count=1)["id"]
        self.device.finish()
        recovered.pull("device-1", newer)
        new_path = recovered.archive_path(newer)
        self.assertEqual(recovered.archive_filename(new_path), "Top_202609221234.txt")
        self.adb.devices.return_value = []
        self.assertFalse(recovered.status("device-1")["connected"])
        self.assertEqual(recovered.archive_filename(path), naming["name"] + ".txt")
        self.assertEqual(recovered.archive_path(identity).read_bytes(), RAW)
        self.assertEqual(marker.read_bytes(), saved)
        with patch("perf_api.TopCapture", return_value=recovered):
            app = create_app(self.root / "offline-api.sqlite3")
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            client.headers["X-Session-Token"] = client.get("/api/token").json()["token"]
            response = client.get("/api/top/archive", params={"capture_id": identity})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, RAW)
            self.assertEqual(response.headers["content-disposition"],
                             'attachment; filename="' + naming["name"] + '.txt"')
        self.assertEqual(imported_snapshot, naming)

    def test_naming_sidecar_corrupt_or_symlink_preserves_raw_and_retries(self):
        for kind in ("corrupt", "symlink", "wrong_device", "invalid_name"):
            with self.subTest(kind=kind):
                identity = self.pulled()["id"]
                path = self.top.archive_path(identity)
                marker = path.with_suffix(".name.json")
                saved = marker.read_bytes()
                backup = marker.with_suffix(".saved")
                marker.rename(backup)
                if kind == "symlink":
                    target = self.root / "untrusted-name.json"
                    target.write_bytes(saved)
                    marker.symlink_to(target)
                elif kind in ("wrong_device", "invalid_name"):
                    invalid = json.loads(saved)
                    invalid["name"] = ("Top_other-device_202609221234" if kind == "wrong_device"
                                       else "Top_202609221234_extra")
                    marker.write_text(json.dumps(invalid), encoding="utf-8")
                else:
                    marker.write_bytes(b'{"name":')
                untrusted = marker.read_bytes()
                recovered = self.controller()
                state = self.top.status()
                self.error(409, recovered._archive_naming, path, state)
                self.assertEqual(path.read_bytes(), RAW)
                self.assertEqual(marker.read_bytes(), untrusted)
                self.assertEqual(marker.is_symlink(), kind == "symlink")
                self.assertIsNone(recovered.status("device-1")["archive"])
                marker.rename(marker.with_suffix(".rejected"))
                backup.rename(marker)
                self.assertTrue(recovered.status("device-1")["archive"])
                self.assertEqual(recovered.archive_filename(path), "Top_202609221234.txt")
                self.assertEqual(path.read_bytes(), RAW)

    def test_naming_sidecar_write_failures_keep_published_raw_for_retry(self):
        real_dump, real_sync, real_link = json.dump, os.fsync, os.link
        for failure in ("write", "sync", "link"):
            with self.subTest(failure=failure):
                identity = self.finished()
                naming_fd = []

                def dump(data, stream, **kwargs):
                    if data.get("capture_method") == "Top":
                        naming_fd.append(stream.fileno())
                        if failure == "write":
                            stream.write('{"name":')
                            raise OSError("name write failed")
                    return real_dump(data, stream, **kwargs)

                def sync(fd):
                    if failure == "sync" and fd in naming_fd:
                        raise OSError("name sync failed")
                    return real_sync(fd)

                def link(source, destination, **kwargs):
                    if failure == "link" and str(destination).endswith(".name.json"):
                        raise OSError("name link failed")
                    return real_link(source, destination, **kwargs)

                with patch("perf_top_capture.json.dump", side_effect=dump), \
                     patch("perf_top_capture.os.fsync", side_effect=sync), \
                     patch("perf_top_capture.os.link", side_effect=link):
                    self.error(503, self.top.pull, "device-1", identity)
                directory = self.top.archive_root / identity
                paths = list(directory.glob("*.txt"))
                self.assertEqual(len(paths), 1)
                path = paths[0]
                self.assertEqual(path.read_bytes(), RAW)
                self.assertFalse(path.with_suffix(".name.json").exists())
                self.assertEqual(list(self.top.archive_root.rglob("*.part")), [])
                pulls = sum(args[0] == "pull" for _, args in self.device.commands)
                recovered = self.controller()
                self.assertTrue(recovered.pull("device-1", identity)["archive"])
                self.assertEqual(sum(args[0] == "pull" for _, args in self.device.commands), pulls)
                self.assertEqual(recovered.archive_filename(path), "Top_202609221234.txt")
                self.assertEqual(path.read_bytes(), RAW)
                self.assertFalse(any(args[0] == "clear" for _, args in self.device.commands))

    @unittest.skipUnless(hasattr(time, "tzset"), "需要可切换本地时区")
    def test_real_store_import_preserves_snapshot_across_timezone_change(self):
        old_timezone = os.environ.get("TZ")

        def restore_timezone():
            if old_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_timezone
            time.tzset()

        self.addCleanup(restore_timezone)
        for source, legacy in (("capture", False), ("first_record", False),
                               ("capture", True), ("first_record", True)):
            with self.subTest(source=source, legacy=legacy):
                os.environ["TZ"] = "UTC0"
                time.tzset()
                identity = self.finished()
                # Explicit UTC makes reparsing under another zone change the
                # computed name too, so first_record cannot pass accidentally.
                payload = RAW.replace(b"12:34:56 =", b"12:34:56 UTC =")
                self.device.payload = payload
                self.device.jobs["device-1"][5] = str(len(payload))
                if source == "capture":
                    self.device.jobs["device-1"] += ["1790663405", "1790663406", "1790663407"]
                self.top.pull("device-1", identity)
                path = self.top.archive_path(identity)
                naming = self.naming_metadata(path)
                download = self.top.archive_filename(path)
                self.assertEqual(naming["naming_time_source"], source)
                marker = path.with_suffix(".name.json")
                if legacy:
                    old_naming = dict(naming, name=naming["name"].replace(
                        "Top_", "Top_" + naming["device_name"] + "_", 1))
                    marker.write_text(json.dumps(old_naming), encoding="utf-8")
                saved = marker.read_bytes()
                os.environ["TZ"] = "JST-9"
                time.tzset()
                recovered = self.controller()
                recovered.store = Store(self.root / (source + ".sqlite3"))
                self.assertTrue(recovered.status("device-1")["archive"])
                self.assertEqual(recovered.archive_filename(path), download)
                with patch.object(recovered.store, "import_files", wraps=recovered.store.import_files) as imported:
                    result = recovered.import_capture(identity)
                session = recovered.store.session(result["id"])
                self.assertEqual(session["name"] + ".txt", download)
                for key, value in naming.items():
                    if key != "name":
                        self.assertEqual(session["summary"][key], value)
                self.assertEqual(imported.call_args.kwargs.get("naming_snapshot"), naming)
                self.assertEqual(path.read_bytes(), payload)
                self.assertEqual(marker.read_bytes(), saved)
                with patch("perf_api.TopCapture", return_value=recovered):
                    app = create_app(self.root / "timezone-api.sqlite3")
                with TestClient(app, base_url="http://127.0.0.1:8765") as client:
                    client.headers["X-Session-Token"] = client.get("/api/token").json()["token"]
                    response = client.get("/api/top/archive", params={"capture_id": identity})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, payload)
                    self.assertEqual(response.headers["content-disposition"],
                                     'attachment; filename="' + download + '"')
                self.assertEqual(marker.read_bytes(), saved)

    def test_each_device_operation_uses_shell_without_elevation(self):
        calls = Mock()
        calls.attach_mock(self.adb.run, "run")
        calls.attach_mock(self.adb.shell, "shell")

        def check(operation):
            calls.reset_mock()
            result = operation()
            for call in self.adb.run.call_args_list:
                self.assertNotIn(call.args[2], {"root", "unroot", "wait-for-device", "reconnect"})
            for call in self.adb.shell.call_args_list:
                self.assertFalse(call.kwargs.get("root"))
                self.assertNotIn("su 0", call.args[1])
            self.assertFalse(self.adb.lock.locked())
            return result

        check(lambda: self.top.status("device-1"))
        identity = check(lambda: self.top.start("device-1"))["id"]
        self.device.stop_delay = 2
        check(lambda: self.top.stop("device-1", identity))
        self.device.finish()
        check(lambda: self.top.pull("device-1", identity))
        check(lambda: self.top.clear("device-1", identity))
        calls.reset_mock()
        self.top.status()
        calls.assert_not_called()

    def test_permission_failure_blocks_operations_and_preserves_cached_task(self):
        identity = self.top.start("device-1")["id"]
        before = list(self.device.commands)
        for failure in (AdbError("旧任务目录不可访问，采集状态未确认", 502),
                        AdbError("shell timeout，采集状态未确认", 504)):
            with self.subTest(failure=str(failure)):
                self.adb.shell.side_effect = failure
                self.adb.run.side_effect = failure
                for operation in (
                    lambda: self.top.start("device-1"),
                    lambda: self.top.stop("device-1", identity),
                    lambda: self.top.pull("device-1", identity),
                    lambda: self.top.clear("device-1", identity),
                ):
                    with self.assertRaisesRegex(AdbError, "采集状态未确认"):
                        operation()
                    self.assertFalse(self.adb.lock.locked())
                state = self.top.status("device-1")
                self.assertEqual(state["id"], identity)
                self.assertTrue(state["running"])
                self.assertFalse(state["connected"])
                self.assertIn("采集状态未确认", state["connection_error"])
                self.assertEqual(self.device.commands, before)
        self.adb.shell.side_effect = self.device.shell
        self.adb.run.side_effect = self.device.run
        self.assertTrue(self.top.status("device-1")["connected"])

    def test_shell_failure_never_uploads_or_reads_task(self):
        self.adb.shell.side_effect = AdbError("permission denied", 502)
        self.error(502, self.top.start, "device-1")
        self.adb.run.assert_not_called()
        self.assertFalse(self.device.commands)
        self.assertFalse(self.adb.lock.locked())

    def test_shell_uid_2000_can_start_without_root_command(self):
        with self.adb.device("device-1"):
            self.assertEqual(self.device.shell("device-1", "id -u"), "2000")
        self.assertTrue(self.top.start("device-1")["running"])
        self.adb.run.assert_not_called()
        self.assertFalse(self.adb.lock.locked())

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "requires unprivileged POSIX shell")
    def test_inaccessible_old_directory_or_script_blocks_upload(self):
        remote = self.root / "legacy"
        remote.mkdir()
        payload = self.top.script.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        script = remote / ("control-" + hashlib.sha256(payload).hexdigest() + ".sh")
        script.write_bytes(payload)
        archive = remote / "raw.txt"
        archive.write_bytes(RAW)
        adb = Mock()
        self.top.adb = adb

        adb.shell.side_effect = self.device.checked_shell
        for directory_mode, script_mode in ((0o600, 0o600), (0o500, 0o600), (0o700, 0o000)):
            with self.subTest(directory_mode=directory_mode, script_mode=script_mode):
                script.chmod(script_mode)
                remote.chmod(directory_mode)
                try:
                    with patch("perf_top_capture.REMOTE_ROOT", str(remote)):
                        with self.assertRaisesRegex(AdbError, "采集状态未确认"):
                            self.top._command("device-1", "status")
                    adb.run.assert_not_called()
                finally:
                    remote.chmod(0o700)
                    script.chmod(0o600)
                self.assertEqual(archive.read_bytes(), RAW)
                self.assertEqual(script.read_bytes(), payload)

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
                self.top._deployments.clear()
                adb = Mock()
                self.top.adb = adb
                adb.shell.side_effect = self.device.checked_shell
                cache = self.device.cache
                if cache.exists():
                    cache.rename(self.root / ("saved-" + str(len(original))))
                self.device.commands.clear()
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
                    Path(args[4].replace(REMOTE_ROOT, str(self.device.remote))).write_bytes(local.read_bytes())

                adb.run.side_effect = push
                self.assertEqual(self.top._command("device-1", "status"), "idle")
                adb.run.assert_called_once()
                self.assertIn(remote, adb.shell.call_args_list[0].args[1])
                self.assertTrue(adb.shell.call_args.args[1].endswith("; sh " + remote +" status"))
                self.assertFalse(uploaded[0].exists())
                self.assertEqual(source.read_bytes(), original)

    def test_script_cache_uses_normalized_hash_without_upload(self):
        normalized = b"#!/system/bin/sh\numask 077\n"
        source = self.root / "device_top_capture.sh"
        source.write_bytes(normalized.replace(b"\n", b"\r\n"))
        self.top.script = source
        adb = Mock()
        self.top.adb = adb
        cached = self.device.remote / ("control-" + hashlib.sha256(normalized).hexdigest() + ".sh")
        cached.write_bytes(normalized)
        adb.shell.side_effect = self.device.checked_shell
        with patch("perf_top_capture.tempfile.TemporaryDirectory") as temporary:
            self.assertEqual(self.top._command("device-1", "status"), "idle")
        temporary.assert_not_called()
        adb.run.assert_not_called()
        remote = REMOTE_ROOT + "/control-" + hashlib.sha256(normalized).hexdigest() + ".sh"
        self.assertIn(remote, adb.shell.call_args_list[0].args[1])
        self.assertNotIn(hashlib.sha256(source.read_bytes()).hexdigest(),
                         adb.shell.call_args_list[0].args[1])
        self.assertTrue(adb.shell.call_args.args[1].endswith("; sh " + remote +" status"))

    def test_script_upload_failure_does_not_publish_or_execute(self):
        adb = Mock()
        self.top.adb = adb
        self.device.cache.rename(self.root / "saved-cache")
        adb.shell.side_effect = self.device.checked_shell
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

    def test_legacy_current_presence_never_touches_old_directory(self):
        for kind in ("file", "directory", "symlink", "dangling"):
            with self.subTest(kind=kind):
                legacy = self.root / ("legacy-" + kind)
                legacy.mkdir()
                current = legacy / "current"
                if kind == "directory":
                    current.mkdir()
                elif kind in {"symlink", "dangling"}:
                    current.symlink_to(self.device.cache if kind == "symlink" else legacy / "missing")
                else:
                    current.write_text("old identity")
                before = legacy.stat().st_mtime_ns
                with patch("perf_top_capture.LEGACY_ROOT", str(legacy)):
                    self.error(502, self.top.start, "device-1")
                self.assertEqual(legacy.stat().st_mtime_ns, before)
                self.adb.run.assert_not_called()
                self.assertFalse(self.device.commands)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "requires unprivileged POSIX shell")
    def test_legacy_search_only_allows_only_confirmed_absence(self):
        self.device.legacy.mkdir()
        current = self.device.legacy / "current"
        current.write_text("old identity")
        self.device.legacy.chmod(0o100)
        try:
            self.error(502, self.top.start, "device-1")
            current.rename(self.root / "old-current")
        except PermissionError:
            self.device.legacy.chmod(0o700)
            current.rename(self.root / "old-current")
        finally:
            self.device.legacy.chmod(0o100)
        try:
            self.assertTrue(self.top.status("device-1")["connected"])
            self.device.legacy.chmod(0o000)
            self.error(502, self.top.start, "device-1")
        finally:
            self.device.legacy.chmod(0o700)
        self.assertEqual((self.root / "old-current").read_text(), "old identity")

    def test_legacy_invalid_ancestor_or_unknown_ls_error_fails_closed(self):
        for kind in ("file", "symlink"):
            legacy = self.root / ("ancestor-" + kind)
            if kind == "file":
                legacy.write_text("not a directory")
            else:
                legacy.symlink_to(self.root / "absent")
            with self.subTest(kind=kind), patch("perf_top_capture.LEGACY_ROOT", str(legacy)):
                self.error(502, self.top.start, "device-1")
        for message in ("Permission denied", "Not a directory", "Input/output error",
                        "No such file or directory extra", "不存在"):
            with self.subTest(message=message):
                self.device.ls_error = message
                self.error(502, self.top.start, "device-1")
        self.adb.run.assert_not_called()
        self.assertFalse(self.device.commands)

    def test_unverified_toybox_and_unusable_hash_fail_closed(self):
        self.device.version = "toybox 0.8.10-android"
        self.error(502, self.top.start, "device-1")
        self.device.version = "toybox 0.8.9-android"
        for failure in ("missing", "0" * 64, "not a hash"):
            with self.subTest(failure=failure):
                self.device.hash_error = failure
                self.error(502, self.top.start, "device-1")
        self.adb.run.assert_not_called()
        self.assertFalse(self.device.commands)

    def test_guard_rejects_failed_version_and_ambiguous_absence_status(self):
        self.device.version_status = 1
        self.error(502, self.top.start, "device-1")
        self.device.version_status = 0
        self.device.ls_error = "No such file or directory"
        self.device.ls_status = 2
        self.error(502, self.top.start, "device-1")
        self.adb.run.assert_not_called()
        self.assertFalse(self.device.commands)

    def test_script_hash_read_failure_after_valid_probe_blocks_execution(self):
        for failure in ("missing", "0" * 64, "not a hash"):
            with self.subTest(failure=failure):
                self.device.script_hash_error = failure
                self.error(502, self.top.start, "device-1")
        self.adb.run.assert_not_called()
        self.assertFalse(self.device.commands)

    def test_bad_cache_is_never_overwritten_or_executed(self):
        self.device.cache.write_bytes(b"tampered")
        self.error(502, self.top.start, "device-1")
        self.assertEqual(self.device.cache.read_bytes(), b"tampered")
        self.device.cache.rename(self.root / "bad-cache")
        for target in (self.root / "bad-cache", self.root / "missing"):
            self.device.cache.symlink_to(target)
            self.error(502, self.top.start, "device-1")
            self.assertTrue(self.device.cache.is_symlink())
            self.device.cache.rename(self.root / ("link-" + target.name))
        self.device.cache.mkdir()
        self.error(502, self.top.start, "device-1")
        self.adb.run.assert_not_called()
        self.assertFalse(self.device.commands)

    def test_uploaded_bytes_and_publish_races_are_checked(self):
        for failure in ("bad-upload", "cache-race", "legacy-race"):
            with self.subTest(failure=failure):
                if self.device.cache.exists():
                    self.device.cache.rename(self.root / ("cache-" + failure))
                def mutate(command):
                    if "mv -n " not in command:
                        return
                    if failure == "bad-upload":
                        for staged in self.device.remote.glob("*.sh.*"):
                            staged.write_bytes(b"partial upload")
                    elif failure == "cache-race":
                        self.device.cache.write_bytes(b"unexpected cache")
                    else:
                        self.device.legacy.mkdir()
                        (self.device.legacy / "current").write_text("old task")
                self.device.before_shell = mutate
                self.error(502, self.top.start, "device-1")
                self.assertFalse(self.device.commands)
                if failure == "cache-race":
                    self.assertEqual(self.device.cache.read_bytes(), b"unexpected cache")
                elif failure == "legacy-race":
                    self.assertEqual((self.device.legacy / "current").read_text(), "old task")

    def test_execution_rechecks_legacy_and_hash_including_stream_and_deadline(self):
        for failure in ("hash", "legacy"):
            for mode in ("normal", "stream", "deadline"):
                with self.subTest(failure=failure, mode=mode):
                    legacy = self.root / (failure + "-" + mode)
                    payload = self.top.script.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    self.device.cache.write_bytes(payload)
                    def mutate(command):
                        if "; sh " not in command:
                            return
                        if failure == "hash":
                            self.device.cache.write_bytes(b"changed before execution")
                        else:
                            legacy.mkdir()
                            (legacy / "current").write_text("old task")
                    self.device.before_shell = mutate
                    output = io.BytesIO() if mode == "stream" else None
                    deadline = time.monotonic() + 10 if mode == "deadline" else None
                    with patch("perf_top_capture.LEGACY_ROOT", str(legacy)), self.adb.device("device-1"):
                        with self.assertRaises(AdbError):
                            self.top._command("device-1", "status", output=output, deadline=deadline)
                    if output is not None:
                        self.assertEqual(output.getvalue(), b"")
                    self.assertFalse(self.device.commands)

    def test_warm_poll_skips_preflight_but_rechecks_guard_for_each_device(self):
        for serial, expected_calls in (("device-1", 2), ("device-2", 2), ("device-1", 1)):
            self.adb.shell.reset_mock()
            self.assertTrue(self.top.status(serial)["connected"])
            self.assertEqual(self.adb.shell.call_count, expected_calls)
            for call in self.adb.shell.call_args_list:
                self.assertTrue(call.kwargs["via_stdin"])
            execution = self.adb.shell.call_args.args[1]
            self.assertIn("legacy_safe;", execution)
            self.assertIn("verify_script", execution)
            self.assertIn("SHA256 工具输出无效", execution)
        self.adb.run.assert_not_called()

    def test_warm_guard_failure_evicts_hint_without_replaying_action(self):
        for failure in ("hash", "legacy", "version", "missing"):
            with self.subTest(failure=failure):
                self.setUp()
                self.top.status("device-1")
                original = self.device.cache.read_bytes()
                if failure == "hash":
                    self.device.cache.write_bytes(b"tampered")
                elif failure == "legacy":
                    self.device.legacy.mkdir()
                    (self.device.legacy / "current").write_text("old task")
                elif failure == "version":
                    self.device.version = "unknown toybox"
                else:
                    self.device.cache.rename(self.root / "saved-cache")
                before = list(self.device.commands)
                self.adb.shell.reset_mock()
                with self.adb.device("device-1"), self.assertRaises(AdbError):
                    self.top._command("device-1", "status")
                self.assertEqual(self.adb.shell.call_count, 1)
                self.assertFalse(self.top._deployments)
                self.assertEqual(self.device.commands, before)
                self.adb.run.assert_not_called()
                if failure == "hash":
                    self.assertEqual(self.device.cache.read_bytes(), b"tampered")
                if failure == "missing":
                    self.assertTrue(self.top.status("device-1")["connected"])
                    self.assertEqual(self.device.cache.read_bytes(), original)

    def test_updated_script_gets_new_deployment_without_overwriting_worker(self):
        self.top.status("device-1")
        original = self.device.cache.read_bytes()
        updated = original + b"\n# changed version\n"
        source = self.root / "updated.sh"
        source.write_bytes(updated)
        self.top.script = source
        self.adb.shell.reset_mock()
        self.assertTrue(self.top.status("device-1")["connected"])
        self.assertEqual(self.adb.shell.call_count, 3)
        self.assertEqual(self.device.cache.read_bytes(), original)
        new_cache = self.device.remote / ("control-" + hashlib.sha256(updated).hexdigest() + ".sh")
        self.assertEqual(new_cache.read_bytes(), updated)
        self.adb.shell.reset_mock()
        self.top.status("device-1")
        self.adb.shell.assert_called_once()

    def test_reconnect_does_not_trust_warm_hint_or_replay_failed_action(self):
        self.top.status("device-1")
        self.adb.shell.side_effect = AdbError("transport disconnected", 502)
        with self.adb.device("device-1"), self.assertRaises(AdbError):
            self.top._command("device-1", "status")
        self.assertFalse(self.top._deployments)
        self.adb.shell.side_effect = self.device.shell
        self.adb.shell.reset_mock()
        self.assertTrue(self.top.status("device-1")["connected"])
        self.assertEqual(self.adb.shell.call_count, 2)
        # A disconnect/reconnect invisible to the host still gets full guards.
        self.device.cache.write_bytes(b"replaced while disconnected")
        with self.adb.device("device-1"), self.assertRaises(AdbError):
            self.top._command("device-1", "status")
        self.adb.run.assert_not_called()

    def test_stdin_transport_has_short_argv_for_normal_stream_and_deadline(self):
        # Exercise the real AdbController.shell wrapper, not just its mock.
        self.adb.shell.side_effect = lambda *args, **kwargs: AdbController.shell(self.adb, *args, **kwargs)
        identity = self.finished()
        for mode in ("normal", "stream", "deadline"):
            with self.subTest(mode=mode):
                self.adb.run.reset_mock()
                output = io.BytesIO() if mode == "stream" else None
                deadline = time.monotonic() + 5 if mode == "deadline" else None
                with self.adb.device("device-1"):
                    self.top._command("device-1", "pull" if output else "status",
                                      *([identity] if output else []), output=output, deadline=deadline)
                self.adb.run.assert_called_once()
                call = self.adb.run.call_args
                self.assertEqual(call.args, ("-s", "device-1", "shell", "-T", "sh -s"))
                self.assertIn(b"verify_script", call.kwargs["input_data"])
                if output is not None:
                    self.assertEqual(output.getvalue(), RAW)
                if deadline is not None:
                    self.assertGreater(call.kwargs["timeout"], 0)
                    self.assertLessEqual(call.kwargs["timeout"], 5)

    def test_mutation_applied_before_disconnect_is_not_replayed(self):
        for action in ("start", "stop", "clear"):
            with self.subTest(action=action):
                self.setUp()
                identity = self.finished()
                original = self.device.action
                def disconnected(serial, args, output=None):
                    result = original(serial, args, output)
                    if args[0] == action:
                        raise AdbError("disconnected after action took effect", 502)
                    return result
                args = ("f" * 32, 10, 1, "title") if action == "start" else (identity,)
                before = len(self.device.commands)
                with patch.object(self.device, "action", side_effect=disconnected), self.adb.device("device-1"):
                    with self.assertRaises(AdbError):
                        self.top._command("device-1", action, *args)
                self.assertEqual(len(self.device.commands), before + 1)
                self.assertFalse(self.top._deployments)
                if action == "start":
                    self.assertEqual(self.device.jobs["device-1"][0], "f" * 32)
                elif action == "stop":
                    self.assertIn("device-1", self.device.stop_pending)
                else:
                    self.assertEqual(self.device.jobs["device-1"][-1], "cleaned")

    def test_deployment_check_unknown_response_does_not_upload(self):
        self.adb.shell.return_value = ""
        self.adb.shell.side_effect = None
        self.error(502, self.top.start, "device-1")
        self.adb.run.assert_not_called()
        self.assertFalse(self.device.commands)

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

    def test_clear_stops_active_task_before_cleaning(self):
        identity = self.top.start("device-1")["id"]
        self.device.commands.clear()
        self.assertTrue(self.top.clear("device-1", identity)["remote_cleaned"])
        actions = [args[0] for _, args in self.device.commands]
        self.assertEqual(actions[:3], ["stop", "status", "clear"])

    def test_clear_rejects_unconfirmed_stale_and_disconnected_tasks(self):
        identity = self.top.start("device-1")["id"]
        self.error(409, self.top.clear, "device-1", "f" * 32)
        self.error(400, self.top.clear, "device-1", "../outside")
        self.device.stop_delay = -1
        with self.stop_timeout():
            self.error(504, self.top.stop, "device-1", identity)
            self.error(504, self.top.clear, "device-1", identity)
        self.adb.devices.return_value = []
        with self.assertRaises(AdbError):
            self.top.clear("device-1", identity)
        self.assertFalse(any(args[0] == "clear" for _, args in self.device.commands))

    def test_corrupt_protocol_and_safe_actions(self):
        identity = "c" * 32
        self.device.jobs["device-1"] = ["corrupt", identity, "unconfirmed", "present"]
        state = self.top.status("device-1")
        self.assertTrue(state["state_corrupt"])
        self.assertFalse(state["connected"])
        self.assertTrue(state["running"])
        for key in ("count", "target_count", "interval", "bytes", "remote_path"):
            self.assertIsNone(state[key])
        with self.stop_timeout():
            self.error(409, self.top.pull, "device-1", identity)
        self.assertFalse(any(args[0] == "pull" for _, args in self.device.commands))
        with self.stop_timeout():
            self.error(504, self.top.clear, "device-1", identity)
        self.error(409, self.top.start, "device-1")
        self.assertFalse(any(args[0] in {"clear", "start"} for _, args in self.device.commands))
        for raw in (f"corrupt\n{identity}\nfree\npresent", "corrupt\n../x\ninactive\npresent",
                    f"corrupt\n{identity}\ninactive\npresent\nextra"):
            self.error(502, self.top._decode, "device-1", raw)

    def test_corrupt_unconfirmed_keeps_stop_but_blocks_start_and_clear(self):
        identity = "c" * 32
        self.device.jobs["device-1"] = ["corrupt", identity, "unconfirmed", "present"]
        state = self.top.status("device-1")
        self.assertFalse(state["connected"])
        self.assertTrue(state["running"])
        self.error(409, self.top.start, "device-1")
        with self.stop_timeout():
            self.error(504, self.top.stop, "device-1", identity)
            self.error(504, self.top.clear, "device-1", identity)
        actions = [args[0] for _, args in self.device.commands]
        self.assertIn("stop", actions)
        self.assertNotIn("clear", actions)
        self.assertNotIn("start", actions)

    def test_corrupt_state_preserves_existing_local_archive_after_restart(self):
        state = self.pulled()
        identity = state["id"]
        self.device.jobs["device-1"] = ["corrupt", identity, "unconfirmed", "present"]
        restored = self.controller()
        state = restored.status("device-1")
        self.assertTrue(state["archive"])
        self.assertIn("无法核对", state["warning"])
        self.assertNotIn("None", state["warning"])
        self.assertEqual(restored.archive_path(identity).read_bytes(), RAW)
        self.device.commands.clear()
        self.adb.run.reset_mock()
        self.adb.shell.reset_mock()
        self.adb.devices.reset_mock()
        self.assertEqual(restored.import_capture(identity)["id"], "session-1")
        self.adb.run.assert_not_called()
        self.adb.shell.assert_not_called()
        self.adb.devices.assert_not_called()
        self.assertEqual(self.device.commands, [])
        with self.stop_timeout():
            self.error(504, restored.clear, "device-1", identity)
        self.assertEqual(restored.archive_path(identity).read_bytes(), RAW)

    def test_clear_without_local_archive_does_not_invent_one(self):
        identity = self.finished()
        self.top.clear("device-1", identity)
        self.assertIsNone(self.top.status("device-1")["archive"])
        self.error(404, self.top.archive_path, identity)

    def test_remote_timestamp_archive_path_and_legacy_fallback(self):
        identity = self.top.start("device-1")["id"]
        raw = "\n".join(self.device.jobs["device-1"] + ["1700000000", "0", "1700000045"])
        base = f"{REMOTE_ROOT}/{identity}/"
        self.assertEqual(self.top._decode("device-1", raw)["remote_path"], base + "raw.txt")
        named = raw + "\narchive=Top20261009123456.txt"
        self.assertEqual(self.top._decode("device-1", named)["remote_path"], base + "Top20261009123456.txt")
        self.assertIsNone(self.top._decode("device-1", named + "\ncleaned")["remote_path"])
        self.assertFalse(self.top._decode("device-1", named + "\nunconfirmed")["connected"])
        for name in ("../escape.txt", "Top2026.txt", "Top20261009123456.txt/other"):
            self.error(502, self.top._decode, "device-1", raw + "\narchive=" + name)

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
        with self.stop_timeout():
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
        with self.stop_timeout():
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
            self.assertEqual(call.args[2], "shell")
            limit = 10
            self.assertLessEqual(call.kwargs["timeout"], limit)

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

    def test_timestamp_archive_names_and_same_second_isolation(self):
        started = 1790663405
        filename = "Top" + time.strftime("%Y%m%d%H%M%S", time.localtime(started)) + ".txt"
        paths = []
        for serial in ("device-1", "device-2"):
            identity = self.top.start(serial, count=1)["id"]
            self.device.finish(serial)
            self.device.jobs[serial] += [str(started), str(started + 1), str(started + 2)]
            self.top.pull(serial, identity)
            path = self.top.archive_path(identity)
            self.assertEqual(path, self.top.archive_root / identity / filename)
            self.assertEqual(path.read_bytes(), RAW)
            download_name = "Top_" + time.strftime("%Y%m%d%H%M", time.localtime(started)) + ".txt"
            self.assertEqual(self.top.archive_filename(path), download_name)
            self.top.import_capture(identity)
            self.assertEqual(self.store.import_files.call_args.args[0][0][0], filename)
            recovered = self.controller()
            self.assertTrue(recovered.status(serial)["archive"])
            self.assertEqual(recovered.archive_path(identity), path)
            paths.append(path)
        self.assertNotEqual(*paths)

    def test_legacy_archive_recovered_without_rename(self):
        identity = self.finished()
        self.top.archive_root.mkdir()
        path = self.top.archive_root / (identity + ".txt")
        path.write_bytes(RAW)
        modified = 1790663405
        os.utime(path, (modified, modified))
        recovered = self.controller()
        self.assertTrue(recovered.status("device-1")["archive"])
        self.assertEqual(recovered.archive_path(identity), path)
        name = "Top" + time.strftime("%Y%m%d%H%M%S", time.localtime(modified)) + ".txt"
        self.assertEqual(recovered.archive_filename(path), "Top_202609221234.txt")
        recovered.import_capture(identity)
        self.assertEqual(self.store.import_files.call_args.args[0][0][0], name)
        self.assertEqual(path.read_bytes(), RAW)
        self.assertFalse(hasattr(self.store.import_files.call_args.args[0][0][1],
                                 "_capture_log_directory"))
        real = self.controller()
        real.store = Store(self.root / "legacy.sqlite3")
        real.status("device-1")
        real.import_capture(identity)
        with real.store.connect() as db:
            rows = db.execute("SELECT path,kind FROM storage_assets").fetchall()
            self.assertEqual([(r["path"], r["kind"]) for r in rows], [(str(path), "file")])
        self.assertTrue(path.with_suffix(".name.json").is_file())

    def test_legacy_task_fallback_filename_stays_stable_after_restart(self):
        identity = self.finished()
        with patch("perf_top_capture.time.time", return_value=1790663405):
            self.top.pull("device-1", identity)
        path = self.top.archive_path(identity)
        self.assertRegex(path.name, r"^Top[0-9]{14}\.txt$")
        recovered = self.controller()
        with patch("perf_top_capture.time.time", return_value=1790763405):
            self.assertTrue(recovered.status("device-1")["archive"])
            recovered.pull("device-1", identity)
        self.assertEqual(recovered.archive_path(identity), path)

    def test_timestamp_archive_rejects_directory_and_file_symlinks(self):
        identity = self.finished()
        directory = self.top.archive_root / identity
        outside = self.root / "outside"
        outside.mkdir()
        self.top.archive_root.mkdir()
        directory.symlink_to(outside, target_is_directory=True)
        self.error(409, self.top.pull, "device-1", identity)
        self.assertEqual(list(outside.iterdir()), [])
        # Use another task to test a symlink at the timestamp filename.
        self.device.jobs["device-1"][0] = "d" * 32
        directory = self.top.archive_root / ("d" * 32)
        directory.mkdir()
        original = outside / "original.txt"
        original.write_bytes(RAW)
        (directory / "Top20260929143005.txt").symlink_to(original)
        self.error(409, self.top.pull, "device-1", "d" * 32)
        self.error(404, self.top.archive_path)
        self.assertEqual(original.read_bytes(), RAW)

    def test_timestamp_archive_rejects_conflicting_local_targets(self):
        identity = self.finished()
        directory = self.top.archive_root / identity
        directory.mkdir(parents=True)
        first = directory / "Top20260929143005.txt"
        first.write_bytes(b"user data")
        self.error(409, self.top.pull, "device-1", identity)
        self.assertEqual(first.read_bytes(), b"user data")
        second = directory / "Top20260929143006.txt"
        second.write_bytes(RAW)
        self.error(409, self.top.pull, "device-1", identity)
        self.error(404, self.top.archive_path)
        self.assertEqual(second.read_bytes(), RAW)

    def test_pull_subprocess_output_uses_file_size_not_parent_position(self):
        staging_path = self.root / "subprocess-staging.bin"
        payload_path = self.root / "device-raw.bin"
        payload = RAW * 65536
        payload_path.write_bytes(payload)
        original = self.device.action
        writer = AdbController(self.root / "writer", executable=sys.executable)
        script = "import pathlib,sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())"
        for independent_position in (False, True):
            with self.subTest(independent_position=independent_position):
                identity = self.finished()
                self.device.jobs["device-1"][5] = str(len(payload))

                def pull(serial, args, output=None):
                    if args[0] != "pull":
                        return original(serial, args, output)
                    if independent_position:
                        # Model a child writer whose position is not shared with
                        # the parent's stream, without requiring a Windows host.
                        with staging_path.open("r+b") as child_output:
                            writer.run("-c", script, str(payload_path), output=child_output)
                        self.assertEqual(output.tell(), 0)
                    else:
                        writer.run("-c", script, str(payload_path), output=output)
                    self.assertEqual(os.fstat(output.fileno()).st_size, len(payload))
                    return ""

                self.device.action = pull
                # Patch only the binary pull staging, not the parser's text spool.
                temporary_file = tempfile.TemporaryFile

                def staging_file(*args, **kwargs):
                    if kwargs.get("dir") == self.top.archive_root:
                        return staging_path.open("w+b")
                    return temporary_file(*args, **kwargs)

                with patch("perf_top_capture.tempfile.TemporaryFile", side_effect=staging_file):
                    state = self.top.pull("device-1", identity)
                self.assertTrue(state["archive"])
                self.assertEqual(self.top.archive_path().read_bytes(), payload)
                self.store.import_files.assert_not_called()

    def test_failed_pull_never_publishes_and_can_retry(self):
        identity = self.finished()
        self.device.fail_pull = True
        self.device.payload = RAW[:len(RAW) // 2]
        before = len(self.device.commands)
        self.error(502, self.top.pull, "device-1", identity)
        self.assertEqual(sum(args[0] == "pull" for _, args in self.device.commands[before:]), 1)
        self.assertFalse(self.top._deployments)
        self.store.import_files.assert_not_called()
        self.assertEqual(list(self.top.archive_root.rglob("*.txt")), [])
        self.device.fail_pull = False
        self.device.payload = b""
        self.error(502, self.top.pull, "device-1", identity)
        self.device.payload = RAW
        with patch("perf_top_capture.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.top.pull("device-1", identity)
        self.error(404, self.top.archive_path)
        self.top.pull("device-1", identity)
        self.assertEqual(self.top.archive_path().read_bytes(), RAW)

    def test_pull_unsafe_state_diagnostics_and_safe_retry(self):
        original = self.device.action
        cases = (
            ("running", RAW, len(RAW), "running", None),
            ("cleaned", RAW, len(RAW), "stopped", "cleaned"),
            ("unconfirmed", RAW, len(RAW), "stopped", "unconfirmed"),
        )
        for name, payload, after_size, after_status, marker in cases:
            with self.subTest(case=name):
                self.device.action = original
                identity = self.finished()
                self.device.payload = payload

                def changed(serial, args, output=None):
                    result = original(serial, args, output)
                    if args[0] == "pull":
                        job = self.device.jobs[serial]
                        job[1], job[5] = after_status, str(after_size)
                        if marker:
                            job.append(marker)
                    return result

                self.device.action = changed
                with self.assertRaises(AdbError) as caught:
                    self.top.pull("device-1", identity)
                self.assertEqual(caught.exception.status, 502)
                detail = str(caught.exception)
                for text in (
                    "未发布本地文件", f"本地实际={len(payload)} 字节",
                    f"拉取前={len(RAW)} 字节", f"拉取后={after_size} 字节",
                    "拉取前状态=stopped", f"拉取后状态={after_status}",
                    "拉取后仍运行=" + ("是" if after_status == "running" or marker == "unconfirmed" else "否"),
                    "拉取后已清理=" + ("是" if marker == "cleaned" else "否"),
                ):
                    self.assertIn(text, detail)
                self.assertNotIn(str(self.root), detail)
                self.assertNotIn("PID USER", detail)
                self.assertEqual(list((self.top.archive_root / identity).glob("*.txt")), [])
                self.assertEqual(list(self.top.archive_root.glob("*.part")), [])
                unavailable = 409 if after_status == "running" or marker == "unconfirmed" else 404
                self.error(unavailable, self.top.archive_path, identity)
                self.store.import_files.assert_not_called()
                self.assertFalse(any(args[0] == "clear" for _, args in self.device.commands))
                self.device.action = original
                self.device.jobs["device-1"] = self.device.jobs["device-1"][:8]
                self.device.finish()
                self.device.payload = RAW
                self.top.pull("device-1", identity)
                self.assertEqual(self.top.archive_path(identity).read_bytes(), RAW)

    def test_warning_metadata_failure_does_not_block_retry(self):
        real_dump, real_sync, real_link = json.dump, os.fsync, os.link
        for failure in ("write", "sync", "link"):
            with self.subTest(case=failure):
                identity = self.finished()
                self.device.payload = RAW[:-1]
                warning_fd = []

                def dump(data, stream, **kwargs):
                    if str(stream.name).endswith(".warning.json"):
                        warning_fd.append(stream.fileno())
                        if failure == "write":
                            stream.write('{"size":')
                            raise OSError("metadata write failed")
                    return real_dump(data, stream, **kwargs)

                def sync(fd):
                    if failure == "sync" and fd in warning_fd:
                        raise OSError("warning sync failed")
                    return real_sync(fd)

                def link(source, destination, **kwargs):
                    if failure == "link" and Path(destination).suffix == ".txt":
                        raise OSError("archive link failed")
                    return real_link(source, destination, **kwargs)

                with patch("perf_top_capture.json.dump", side_effect=dump), \
                     patch("perf_top_capture.os.fsync", side_effect=sync), \
                     patch("perf_top_capture.os.link", side_effect=link):
                    with self.assertRaises(OSError):
                        self.top.pull("device-1", identity)
                directory = self.top.archive_root / identity
                self.assertEqual(list(directory.iterdir()), [])
                self.assertEqual(list(self.top.archive_root.glob("*.part")), [])
                self.error(404, self.top.archive_path, identity)
                self.assertTrue(self.top.pull("device-1", identity)["warning"])
                self.assertEqual(self.top.archive_path(identity).read_bytes(), RAW[:-1])

    def test_warning_metadata_conflicts_are_not_overwritten_or_trusted(self):
        for name in ("invalid", "symlink", "valid_but_different"):
            with self.subTest(case=name):
                identity = self.finished()
                self.device.payload = RAW[:-1]
                state = self.top.status("device-1")
                # Keep the legacy fake's missing start time deterministic.
                with patch("perf_top_capture.time.time", return_value=1700000045):
                    path = self.top._local_path(state)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    marker = path.with_suffix(".warning.json")
                    content = b"user metadata"
                    if name == "symlink":
                        target = self.root / "user-metadata.json"
                        target.write_bytes(content)
                        try:
                            marker.symlink_to(target)
                        except (OSError, NotImplementedError):
                            continue
                    else:
                        if name == "valid_but_different":
                            content = ('{"size":%d,"before":1,"after":2}' % (len(RAW) - 1)).encode()
                        marker.write_bytes(content)
                    self.error(409, self.top.pull, "device-1", identity)
                    self.assertEqual(marker.read_bytes(), content)
                    self.assertEqual(marker.is_symlink(), name == "symlink")
                    self.assertFalse(path.exists())
                    self.assertEqual(list(self.top.archive_root.glob("*.part")), [])
                    self.assertIsNone(self.controller().status("device-1")["archive"])

    def test_pull_size_warning_allows_download_import_retry_and_restart(self):
        original = self.device.action
        cases = (
            ("truncated", RAW[:-1], len(RAW)),
            ("expanded_newlines", RAW.replace(b"\n", b"\r\n"), len(RAW)),
            ("before_size_only", RAW[:-1], len(RAW) - 1),
            ("after_size_only", RAW, len(RAW) + 1),
        )
        for name, payload, after_size in cases:
            with self.subTest(case=name):
                self.device.action = original
                identity = self.finished()
                self.device.payload = payload

                def changed(serial, args, output=None):
                    result = original(serial, args, output)
                    if args[0] == "pull":
                        self.device.jobs[serial][5] = str(after_size)
                    return result

                self.device.action = changed
                pulled = self.top.pull("device-1", identity)
                warning = pulled["warning"]
                for text in ("字节数不一致", f"本地实际={len(payload)} 字节",
                             f"拉取前={len(RAW)} 字节", f"拉取后={after_size} 字节"):
                    self.assertIn(text, warning)
                self.assertNotIn(str(self.root), warning)
                self.assertNotIn("PID USER", warning)
                self.assertIsNone(pulled["error"])
                self.assertTrue(pulled["archive"])
                self.assertEqual(self.top.archive_path(identity).read_bytes(), payload)
                self.assertEqual(self.top.status("device-1")["warning"], warning)
                count = sum(args[0] == "pull" for _, args in self.device.commands)
                self.assertEqual(self.top.pull("device-1", identity)["warning"], warning)
                self.assertEqual(sum(args[0] == "pull" for _, args in self.device.commands), count)
                self.assertEqual(list(self.top.archive_root.glob("*.part")), [])
                self.assertFalse(any(args[0] == "clear" for _, args in self.device.commands))
                recovered = self.controller()
                restored = recovered.status("device-1")
                self.assertEqual(restored["archive"], pulled["archive"])
                self.assertEqual(restored["warning"], warning)
                self.assertEqual(recovered.pull("device-1", identity)["archive"], pulled["archive"])
                self.store.import_files.side_effect = ValueError("parse failure")
                with self.assertRaisesRegex(ValueError, "parse failure"):
                    recovered.import_capture(identity)
                self.assertEqual(recovered.archive_path(identity).read_bytes(), payload)

                def imported(files, title="", **kwargs):
                    self.assertEqual(files[0][1].read(), payload)
                    return {"id": "session-1", "duplicate": False}

                self.store.import_files.side_effect = imported
                self.assertEqual(recovered.import_capture(identity)["id"], "session-1")
                self.assertEqual(recovered.status("device-1")["status"], "imported")
                self.assertEqual(self.top.status("device-1")["warning"], warning)
                self.store.import_files.side_effect = None
                self.device.action = original
                # A warning belongs only to its capture, never the next task.
                self.assertIsNone(self.top.start("device-1", count=1)["warning"])
                self.device.finish()
                self.top.status("device-1")

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

    def test_real_store_tracks_complete_directories_on_duplicate(self):
        self.top.store = Store(self.root / "store.sqlite3")
        folders, ids = [], []
        for _ in range(2):
            identity = self.pulled()["id"]
            path = self.top.archive_path(identity)
            self.assertTrue(path.with_suffix(".name.json").is_file())
            folders.append(str(path.parent))
            ids.append(self.top.import_capture(identity)["id"])
        self.assertEqual(ids[0], ids[1])
        with self.top.store.connect() as db:
            rows = db.execute("SELECT path,kind FROM storage_assets").fetchall()
            self.assertEqual({(r["path"], r["kind"]) for r in rows},
                             {(p, "directory") for p in folders})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM session_logs").fetchone()[0], 2)

    def test_deleted_session_cache_is_revalidated_under_import_lock(self):
        self.top.store = Store(self.root / "store.sqlite3")
        identity = self.pulled()["id"]
        first = self.top.import_capture(identity)
        self.import_lock.acquire()
        try:
            self.error(409, self.top.import_capture, identity)
        finally:
            self.import_lock.release()
        # Emulate deletion with retained logs, without filesystem deletion.
        with self.top.store.connect() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (first["id"],))
        second = self.top.import_capture(identity)
        self.assertNotEqual(first["id"], second["id"])
        self.assertFalse(second["duplicate"])
        self.assertTrue(self.top.import_capture(identity)["duplicate"])
        self.assertFalse(self.import_lock.locked())
        path = self.top.archive_path(identity)
        path.parent.rename(self.root / "removed-capture")
        self.error(404, self.top.import_capture, identity)
        self.assertFalse(path.parent.exists())
        self.assertFalse(self.import_lock.locked())

    def test_cached_session_lookup_failure_releases_import_lock(self):
        identity = self.pulled()["id"]
        self.top.import_capture(identity)
        self.store.session.side_effect = OSError("database unavailable")
        with self.assertRaises(OSError):
            self.top.import_capture(identity)
        self.assertFalse(self.import_lock.locked())
        self.assertFalse(self.top._state["importing"])

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
        def blocked(*args, **kwargs):
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
        self.assertEqual(list(self.top.archive_root.rglob("*.txt")), [])

    def test_import_survives_same_device_task_switch(self):
        identity = self.pulled()["id"]
        def import_and_switch(*args, **kwargs):
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

    def test_recovery_pull_after_timeout_persists_without_stop_or_overwrite(self):
        identity = self.top.start("device-1", count=-1)["id"]
        self.device.stop_delay = -1
        with self.stop_timeout():
            self.error(504, self.top.pull, "device-1", identity)
        self.device.commands.clear()
        state = self.top.pull("device-1", identity, allow_unconfirmed=True)
        self.assertTrue(state["running"])
        self.assertEqual(state["bytes"], 0)
        self.assertIn(RECOVERY_WARNING, state["warning"])
        self.assertEqual([args[0] for _, args in self.device.commands],
                         ["status", "pull-live", "status"])
        path = self.top.archive_path(identity)
        self.assertEqual(path.read_bytes(), RAW)
        self.assertIs(json.loads(path.with_suffix(".warning.json").read_text())["recovery"], True)
        self.store.import_files.assert_not_called()
        restarted = self.controller()
        self.assertEqual(restarted.status("device-1")["warning"], state["warning"])
        self.device.payload = RAW + b"new sample"
        self.device.commands.clear()
        restarted.pull("device-1", identity, allow_unconfirmed=True)
        self.assertNotIn("pull-live", [args[0] for _, args in self.device.commands])
        self.assertEqual(path.read_bytes(), RAW)
        self.assertEqual(restarted.import_capture(identity)["id"], "session-1")
        self.store.import_files.assert_called_once()

    def test_recovery_clear_warning_survives_restart_and_later_pull(self):
        identity = self.top.start("device-1", count=-1)["id"]
        self.device.commands.clear()
        state = self.top.clear("device-1", identity, allow_unconfirmed=True)
        self.assertEqual([args[0] for _, args in self.device.commands],
                         ["status", "clear-live", "status"])
        self.assertTrue(state["running"])
        self.assertFalse(state["remote_cleaned"])
        self.assertIn(CLEAR_WARNING, state["warning"])
        restarted = self.controller()
        self.assertIn(CLEAR_WARNING, restarted.status("device-1")["warning"])
        self.assertIsNone(restarted.status()["archive"])
        self.device.payload = RAW
        state = restarted.pull("device-1", identity, allow_unconfirmed=True)
        self.assertIn(CLEAR_WARNING, state["warning"])
        self.assertIn(RECOVERY_WARNING, state["warning"])
        self.assertIn(CLEAR_WARNING, self.controller().status("device-1")["warning"])

    def test_recovery_rejects_invalid_confirmation_corruption_and_identity(self):
        identity = self.top.start("device-1")["id"]
        for method in (self.top.pull, self.top.clear):
            for value in (1, "true", None):
                with self.assertRaises(ValueError):
                    method("device-1", identity, allow_unconfirmed=value)
            with self.assertRaises(AdbError):
                method("device-1", "e" * 32, allow_unconfirmed=True)
        self.device.jobs["device-1"] = ["corrupt", identity, "unconfirmed", "present", "clear_unconfirmed"]
        self.assertIn(CLEAR_WARNING, self.controller().status("device-1")["warning"])
        for method in (self.top.pull, self.top.clear):
            with self.assertRaises(AdbError) as caught:
                method("device-1", identity, allow_unconfirmed=True)
            self.assertEqual(caught.exception.status, 409)

    def test_recovery_warning_metadata_requires_literal_flags(self):
        path = self.root / "recovery.txt"
        marker = path.with_suffix(".warning.json")
        valid = dict(size=1, before=1, after=1, recovery=True, cleared=True)
        marker.write_text(json.dumps(valid))
        warning = self.top._saved_warning(path, 1)
        self.assertIn(RECOVERY_WARNING, warning)
        self.assertIn(CLEAR_WARNING, warning)
        for key in ("recovery", "cleared"):
            for value in (False, 1, "true", None):
                marker.write_text(json.dumps(dict(valid, **{key: value})))
                self.assertIsNone(self.top._saved_warning(path, 1))

    def test_recovery_pull_rechecks_identity_and_state_before_publication(self):
        identity = self.top.start("device-1")["id"]
        original = self.device.action
        baseline = list(self.device.jobs["device-1"])
        for change in ("identity", "corrupt", "cleaned", "oversize"):
            with self.subTest(change=change):
                self.device.jobs["device-1"] = list(baseline)

                def changed(serial, args, output=None):
                    result = original(serial, args, output)
                    if args[0] == "pull-live":
                        if change == "identity":
                            self.device.jobs[serial][0] = "e" * 32
                        elif change == "corrupt":
                            self.device.jobs[serial] = ["corrupt", identity, "unconfirmed", "present"]
                        elif change == "cleaned":
                            self.device.jobs[serial].append("cleaned")
                        else:
                            output.truncate(MAX_ARCHIVE_BYTES + 1)
                    return result

                self.device.action = changed
                with self.assertRaises(AdbError):
                    self.top.pull("device-1", identity, allow_unconfirmed=True)
                self.assertNotIn(identity, self.top._archives)
                self.assertFalse(list(self.top.archive_root.rglob("*.txt")))
        self.store.import_files.assert_not_called()

    def test_recovery_empty_or_failed_transfer_never_publishes(self):
        identity = self.top.start("device-1")["id"]
        for payload, failure in ((b"", False), (RAW, True)):
            self.device.payload, self.device.fail_pull = payload, failure
            with self.assertRaises(AdbError):
                self.top.pull("device-1", identity, allow_unconfirmed=True)
            self.error(409, self.top.archive_path)
            self.assertNotIn(identity, self.top._archives)
        self.assertFalse(list(self.top.archive_root.rglob("*.txt")))

    def test_api_recovery_requires_strict_opt_in_and_skips_stop(self):
        identity = self.top.start("device-1", count=-1)["id"]
        self.device.stop_delay = -1
        with patch("perf_api.TopCapture", return_value=self.top):
            app = create_app(self.root / "api-recovery.sqlite3")
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            client.headers.update({"X-Session-Token": client.get("/api/token").json()["token"]})
            task = {"confirm": True, "serial": "device-1", "capture_id": identity}
            for operation in ("pull", "clear"):
                url = "/api/top/" + operation
                for invalid in (1, "true", None):
                    self.assertEqual(client.post(url, json=dict(task, allow_unconfirmed=invalid)).status_code, 422)
                with self.stop_timeout():
                    self.assertEqual(client.post(url, json=task).status_code, 504)
                self.device.commands.clear()
                response = client.post(url, json=dict(task, allow_unconfirmed=True))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["running"])
                self.assertNotIn("stop", [args[0] for _, args in self.device.commands])
            self.assertEqual(client.post("/api/top/stop", json=dict(task, allow_unconfirmed=True)).status_code, 422)
            self.store.import_files.assert_not_called()

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
            self.device.stop_delay = -1
            with self.stop_timeout():
                self.assertEqual(client.post("/api/top/clear", json=task).status_code, 504)
                self.assertFalse(any(args[0] == "clear" for _, args in self.device.commands))
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
            self.device.payload = RAW[:-1]
            pulled = client.post("/api/top/pull", json=task)
            self.assertEqual(pulled.status_code, 200, pulled.text)
            detail = pulled.json()["warning"]
            self.assertIn(f"本地实际={len(RAW) - 1} 字节", detail)
            self.assertIn(f"拉取前={len(RAW)} 字节", detail)
            self.assertIn(f"拉取后={len(RAW)} 字节", detail)
            self.assertEqual(client.get(pulled.json()["archive"]).content, RAW[:-1])
            polled = client.get("/api/top/status", params={"serial": "device-1"}).json()
            self.assertEqual(polled["warning"], detail)
            self.assertEqual(client.post("/api/top/pull", json=task).status_code, 200)
            cleared = client.post("/api/top/clear", json=task)
            self.assertEqual(cleared.status_code, 200, cleared.text)
            self.assertTrue(cleared.json()["remote_cleaned"])
            downloaded = client.get(pulled.json()["archive"])
            self.assertEqual(downloaded.content, RAW[:-1])
            self.assertEqual(cleared.json()["warning"], detail)
            filename = self.top.archive_path(identity).name
            self.assertRegex(filename, r"^Top[0-9]{14}\.txt$")
            self.assertEqual(downloaded.headers["content-disposition"],
                             'attachment; filename="Top_202609221234.txt"')
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
        import sys
        clock = shlex.quote(sys.executable) + " -c " + shlex.quote(
            'import time; print(int(time.clock_gettime(time.CLOCK_MONOTONIC) * 100) * 10)')
        self.command("awk", 'case "$*" in */proc/*/stat) echo "$PPID";; '
                     '*/proc/uptime) ' + clock + ';; *) exec /usr/bin/awk "$@";; esac')
        self.command("timeout", 'shift 3; exec "$@"')
        self.command("top", 'cat "$TOP_FIXTURE"')
        # macOS lacks truncate; use Python only in the test harness.
        import sys
        self.command("truncate", shlex.quote(sys.executable) +
                     ' -c \'import sys; f=open(sys.argv[2], "a+b"); f.truncate(int(sys.argv[1])); f.close()\' "$2" "$3"')
        # Any accidental reintroduction of flock must fail even on this host.
        self.command("flock", 'echo "flock must not be used" >&2; exit 99')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        TOP_FIXTURE=str(self.fixture), JOB=str(self.job))
        self.proc = self.root / "proc"
        (self.proc / "1" / "fd").mkdir(parents=True)
        (self.proc / "1" / "cmdline").write_bytes(b"init\0")
        (self.proc / "mounts").write_text(f"proc {self.proc} proc rw 0 0\n")
        self.boot_id = "12345678-1234-1234-1234-123456789abc"
        self.other_boot = "abcdef12-abcd-abcd-abcd-abcdef123456"
        self.boot_path = self.proc / "sys/kernel/random/boot_id"
        self.boot_path.parent.mkdir(parents=True)
        self.boot_path.write_text(self.boot_id + "\n")
        self.proc_stat(1)
        (self.proc / "self").symlink_to("99999")
        # macOS readlink does not offer Android's multi-operand contract.
        self.batch_readlink = (shlex.quote(sys.executable) + ' -c ' + shlex.quote(
            "import os,sys\nrc=0\nfor p in sys.argv[1:]:\n"
            " try: print(os.readlink(p))\n except OSError: rc=1\nsys.exit(rc)"
        ) + ' "$@"')
        self.command("readlink", 'case "$1" in /proc/*/fd/[012]) echo /dev/null;; '
                     '*) ' + self.batch_readlink + ';; esac')

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
        import re
        self.script.write_text(re.sub(r"^ROOT=.*$", "ROOT=" + shlex.quote(str(self.root)),
                self.source, count=1, flags=re.MULTILINE).replace(
            "MAX_BYTES=1000000000", "MAX_BYTES=" + str(limit)).replace(
            "PROC=/proc", "PROC=" + shlex.quote(str(self.proc))))

    def prepare_worker(self, target=1, interval=1):
        """Model launch publication without recycling a permanent claim."""
        if (self.job / "worker.claim").exists() or (self.job / "done").exists():
            self.identity = format(int(self.identity, 16) + 1, "032x")
            self.job = self.root / self.identity
            self.job.mkdir()
        self.env["JOB"] = str(self.job)
        (self.job / "boot").write_text(self.identity + "\n" + self.boot_id + "\n")
        (self.root / "current").write_text(self.identity)
        fields = [self.identity, "starting", "0", str(target), str(interval),
                  "0", "none", "", "100", "0"]
        (self.job / "state").write_text("\n".join(fields) + "\n")

    def worker(self, limit=MAX_ARCHIVE_BYTES, target=1, interval=1):
        self.prepare_worker(target, interval)
        self.write_script(limit)
        result = subprocess.run(["sh", str(self.script), "worker", self.identity,
                                 str(target), str(interval), ""], env=self.env,
                                capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.job / "done").read_text().strip(), self.identity)
        self.assertTrue((self.job / "worker.claim").is_dir())
        self.assertFalse((self.job / "worker.lock").exists())
        return (self.job / "state").read_text().splitlines()

    def simulated_timing(self, sample_ms=650, append_ms=150):
        """Deterministic device clock; top and archive writes both consume time."""
        import sys
        clock = self.root / "clock"
        clock.write_text("100200")
        starts = self.root / "sample-starts"
        prefix = (
            "import sys\nfrom pathlib import Path\n"
            f"clock=Path({str(clock)!r})\n"
            "now=int(clock.read_text())\n"
        )

        def python(code):
            return shlex.quote(sys.executable) + " -c " + shlex.quote(prefix + code)

        self.command("awk", 'case "$*" in */proc/*/stat) echo "$PPID";; '
                     '*/proc/uptime) ' + python("print(now)") +
                     ';; *) exec /usr/bin/awk "$@";; esac')
        self.command("sleep", python(
            'clock.write_text(str(now + round(float(sys.argv[1])*1000)))') + ' "$1"')
        self.command("top", python(
            f"with Path({str(starts)!r}).open('a') as f: f.write(str(now)+'\\n')\n"
            f"clock.write_text(str(now+{sample_ms}))\n"
            f"sys.stdout.buffer.write(Path({str(self.fixture)!r}).read_bytes())"))
        self.command("cat", 'case "$1" in */header) ' + python(
            f"clock.write_text(str(now+{append_ms}))") + ';; esac\nexec /bin/cat "$@"')
        self.command("date", 'if [ "$1" = "+%Y-%m-%d %H:%M:%S" ]; then ' + python(
            "from datetime import datetime, timezone\n"
            "print(datetime.fromtimestamp(now/1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))") +
            '; else exec /bin/date "$@"; fi')
        return starts

    def test_fixed_cadence_includes_top_and_archive_time(self):
        from datetime import datetime
        starts = self.simulated_timing(sample_ms=850, append_ms=50)
        state = self.worker(target=6)
        self.assertEqual(state[1:3], ["completed", "6"])
        self.assertEqual(list(map(int, starts.read_text().splitlines())),
                         [100200 + n * 1000 for n in range(6)])
        headers = [line for line in (self.job / "raw.txt").read_text().splitlines()
                   if line.startswith("========== Top")]
        stamps = [datetime.strptime(line.split("时间: ")[1].split(" ===")[0],
                                    "%Y-%m-%d %H:%M:%S") for line in headers]
        self.assertEqual([int((stamp- stamps[0]).total_seconds()) for stamp in stamps],
                         list(range(6)))
        self.assertEqual(len((self.job / "raw.txt").read_bytes()), int(state[5]))

    def test_fixed_cadence_retains_fractional_intervals(self):
        starts = self.simulated_timing()
        state = self.worker(target=4, interval=1.25)
        self.assertEqual(state[1], "completed")
        self.assertEqual(list(map(int, starts.read_text().splitlines())),
                         [100200 + n * 1250 for n in range(4)])

    def test_overrun_stops_without_fabricated_catchup_samples(self):
        starts = self.simulated_timing(sample_ms=2100)
        state = self.worker(target=3)
        self.assertEqual(state[1:3], ["error", "1"])
        self.assertEqual(state[6], "sampling_overrun")
        self.assertEqual(starts.read_text().splitlines(), ["100200"])
        self.assertEqual(len((self.job / "raw.txt").read_bytes()), int(state[5]))
        (self.root / "current").write_text(self.identity)
        status = self.shell("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", status.stdout.strip())
        self.assertEqual(decoded["error"], "sampling_overrun")
        self.assertFalse(decoded["running"])
        self.assertEqual(self.shell("pull", self.identity).stdout.encode(),
                         (self.job / "raw.txt").read_bytes())

    def test_device_uptime_clock_validation(self):
        self.command("awk", 'case "$*" in */proc/*/stat) echo "$PPID";; '
                     '*) exec /usr/bin/awk "$@";; esac')
        uptime = self.proc / "uptime"
        uptime.write_text("1234567.89 9876543.21\n")
        self.assertEqual(self.worker()[1], "completed")
        uptime.write_text("not-a-clock\n")
        state = self.worker()
        self.assertEqual(state[1:3], ["error", "0"])
        self.assertEqual(state[6], "clock_failed")
        (self.root / "current").write_text(self.identity)
        status = self.shell("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", status.stdout.strip())
        self.assertEqual(decoded["error"], "clock_failed")

    def test_stop_during_long_cadence_wait(self):
        starts = self.simulated_timing()
        self.command("sleep", 'touch ' + shlex.quote(str(self.job / "stop")))
        state = self.worker(target=-1, interval=3600)
        self.assertEqual(state[1:3], ["stopped", "1"])
        self.assertEqual(state[6], "none")
        self.assertEqual(len(starts.read_text().splitlines()), 1)

    def test_real_slow_top_does_not_add_a_full_interval(self):
        import sys
        starts = self.root / "sample-starts"
        code = (
            "import time\nfrom pathlib import Path\n"
            f"with Path({str(starts)!r}).open('a') as f: f.write(str(time.clock_gettime(time.CLOCK_MONOTONIC))+'\\n')\n"
            "time.sleep(.4)\n"
        )
        self.command("top", shlex.quote(sys.executable) + " -c " + shlex.quote(code) +
                     '; cat "$TOP_FIXTURE"')
        # Warm the newly created executable wrappers before timing the cadence.
        # On macOS their first execution can delay entry into the Python top
        # mock by >0.4 s, although the worker's first deadline already started.
        # That cold-start delay shortens the measured first-to-last mock span.
        subprocess.run(["timeout", "-s", "KILL", "10", "sh", "-c",
                        "ulimit -f 8192 || exit 1; exec top -b -n 1 -d 1"],
                       env=self.env, capture_output=True, check=True, timeout=8)
        starts.write_text("")
        self.assertEqual(self.worker(target=4)[1], "completed")
        times = list(map(float, starts.read_text().splitlines()))
        # Old finish-then-sleep loop takes at least 4.2 s for these three gaps.
        self.assertAlmostEqual(times[-1] - times[0], 3, delta=.4)

    def clear_command(self, identity=None):
        return subprocess.run(["sh", str(self.script), "clear", identity or self.identity],
                              env=self.env, capture_output=True, text=True, timeout=5)

    def prepare_clear(self):
        self.worker()
        (self.root / "current").write_text(self.identity)

    def test_launch_creates_timestamp_named_archive(self):
        self.write_script()
        identity = "e" * 32
        job = self.root / identity
        self.command("timeout", 'sleep 1; exit 124')
        self.command("setsid", 'exec "$@"')
        self.command("nohup", 'touch ' + shlex.quote(str(job / "ready")))
        result = self.shell("launch", identity, "1", "1", "")
        self.assertEqual(result.returncode, 0, result.stderr)
        name = (job / "archive_name").read_text().strip()
        self.assertRegex(name, r"^Top[0-9]{14}\.txt$")
        self.assertTrue((job / name).is_file())
        self.assertFalse((job / "raw.txt").exists())

    def test_timestamp_archive_worker_status_pull_and_clear(self):
        name = "Top20261009123456.txt"
        (self.job / "archive_name").write_text(name + "\n")
        self.worker()
        self.assertFalse((self.job / "raw.txt").exists())
        archive = self.job / name
        self.assertIn(TOP, archive.read_bytes())
        (self.root / "current").write_text(self.identity)
        status = self.shell("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("archive=" + name, status.stdout.splitlines())
        pulled = self.shell("pull", self.identity)
        self.assertEqual(pulled.returncode, 0, pulled.stderr)
        self.assertEqual(pulled.stdout.encode(), archive.read_bytes())
        cleared = self.clear_command()
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertEqual(archive.stat().st_size, 0)
        self.assertEqual((self.job / "archive_name").read_text(), name + "\n")

    def test_timestamp_archive_orphan_preserves_partial_sample(self):
        self.orphan()
        name = "Top20261009123456.txt"
        (self.job / "raw.txt").rename(self.job / name)
        (self.job / "archive_name").write_text(name + "\n")
        status = self.shell("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual((self.job / name).read_bytes(), RAW + b"partial uncommitted sample")
        self.assert_pending_receipt(status)
        self.assertIn("archive=" + name, status.stdout.splitlines())
        state = (self.job / "state").read_bytes()
        for mode in ("pull", "clear"):
            self.assertNotEqual(self.shell(mode, self.identity).returncode, 0)
        self.assertEqual((self.job / "state").read_bytes(), state)
        self.assertEqual((self.job / name).read_bytes(), RAW + b"partial uncommitted sample")
        self.assertFalse((self.job / "done").exists())

    def test_archive_name_rejects_traversal_and_symlinks(self):
        self.prepare_clear()
        marker = self.job / "archive_name"
        for name in ("../fixture", "Top2026.txt", "Top20261009123456.txt/other"):
            marker.write_text(name + "\n")
            self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
            self.assertNotEqual(self.clear_command().returncode, 0)
        marker.rename(self.job / "invalid_archive_name")
        marker.symlink_to(self.fixture)
        self.assertNotEqual(self.clear_command().returncode, 0)
        self.assertEqual(self.fixture.read_bytes(), TOP)

    def test_clear_all_finished_logs_preserves_metadata_and_other_files(self):
        self.prepare_clear()
        historical = self.root / ("a" * 32)
        historical.mkdir()
        for name in ("raw.txt", "sample", "header", "state", "done"):
            content = (self.job / name).read_bytes().replace(self.identity.encode(), b"a" * 32)
            (historical / name).write_bytes(content)
        (historical / "worker.claim").mkdir()
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
        (self.root / "control.busy").mkdir()
        self.assertNotEqual(self.clear_command().returncode, 0)
        (self.root / "control.busy").rename(self.root / "checked-control")
        (self.job / "cleaned").symlink_to(self.fixture)
        self.assertNotEqual(self.clear_command().returncode, 0)
        self.assertEqual((self.job / "raw.txt").read_bytes(), original)
        self.assertEqual(self.fixture.read_bytes(), TOP)

    def test_clear_rejects_unsafe_or_unfinished_historical_tasks(self):
        self.prepare_clear()
        valid = self.root / ("a" * 32)
        valid.mkdir()
        names = ("raw.txt", "sample", "header", "state", "done")
        for name in names:
            (valid / name).write_bytes((self.job / name).read_bytes().replace(
                self.identity.encode(), valid.name.encode()))
        (valid / "worker.claim").mkdir()
        # This valid task sorts before the invalid historical task: no task may
        # be partially cleared before the entire preflight has succeeded.
        before = {(job, name): (job / name).read_bytes()
                  for job in (valid, self.job) for name in names}
        for kind in ("symlink", "unfinished", "linked_archive", "corrupt_state",
                     "missing_done", "wrong_done", "linked_done"):
            with self.subTest(kind=kind):
                other = self.root / ("b" * 32)
                if kind == "symlink":
                    other.symlink_to(self.job, target_is_directory=True)
                else:
                    other.mkdir()
                    (other / "worker.claim").mkdir()
                    for name in names:
                        if name == "done" and kind in ("missing_done", "linked_done"):
                            continue
                        if name == "raw.txt" and kind == "linked_archive":
                            continue
                        data = (self.job / name).read_bytes().replace(
                            self.identity.encode(), other.name.encode())
                        if name == "state" and kind == "unfinished":
                            data = data.replace(b"completed", b"running")
                        elif name == "state" and kind == "corrupt_state":
                            data = b"damaged metadata\n"
                        elif name == "done" and kind == "wrong_done":
                            data = b"f" * 32 + b"\n"
                        (other / name).write_bytes(data)
                    if kind == "linked_archive":
                        (other / "raw.txt").symlink_to(self.fixture)
                    elif kind == "linked_done":
                        (other / "done").symlink_to(self.job / "done")
                invalid_before = {p.name: p.read_bytes() for p in other.iterdir()
                                  if p.is_file()}
                self.assertNotEqual(self.clear_command().returncode, 0)
                for (job, name), data in before.items():
                    self.assertEqual((job / name).read_bytes(), data)
                    self.assertFalse((job / "cleaned").exists())
                self.assertEqual({p.name: p.read_bytes() for p in other.iterdir()
                                  if p.is_file()}, invalid_before)
                self.assertFalse((other / "cleaned").exists())
                self.assertEqual(self.fixture.read_bytes(), TOP)
                other.rename(self.root / (kind + "-checked"))

    def shell(self, mode, *args):
        return subprocess.run(["sh", str(self.script), mode, *args], env=self.env,
                              capture_output=True, text=True, timeout=4)

    def orphan(self, lines=10, status="running"):
        self.write_script()
        (self.root / "current").write_text(self.identity)
        fields = [self.identity, status, "1", "-1", "1", str(len(RAW)),
                  "none", "", "100", "0"]
        (self.job / "state").write_text("\n".join(fields[:lines]) + "\n")
        (self.job / "raw.txt").write_bytes(RAW + b"partial uncommitted sample")

    def reject_worker_entry(self, *flags, stdin=None):
        paths = [self.job / name for name in ("raw.txt", "state", "done", "ready", "sample", "header", "state.new")]
        before = {p: p.read_bytes() if p.exists() else None for p in paths}
        result = subprocess.run(["sh", str(self.script), "worker", self.identity,
                                 "1", "1", "", *flags], env=self.env, stdin=stdin,
                                capture_output=True, timeout=4)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        for path, content in before.items():
            self.assertEqual(path.read_bytes() if path.exists() else None, content)

    def record_boot(self, value=None):
        (self.job / "boot").write_text(self.identity + "\n" + (value or self.boot_id) + "\n")

    def test_live_clear_only_truncates_current_logs_and_never_unlocks(self):
        self.orphan()
        self.record_boot()
        (self.job / "worker.claim").mkdir()
        for name in ("sample", "header"):
            (self.job / name).write_bytes(b"partial")
        history = self.root / ("e" * 32)
        history.mkdir()
        (history / "raw.txt").write_bytes(RAW)
        metadata = {name: (self.job / name).read_bytes() for name in ("state", "boot")}
        result = self.shell("clear-live", self.identity)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("raw.txt", "sample", "header"):
            self.assertEqual((self.job / name).read_bytes(), b"")
        self.assertEqual((history / "raw.txt").read_bytes(), RAW)
        for name, data in metadata.items():
            self.assertEqual((self.job / name).read_bytes(), data)
        self.assertTrue((self.job / "worker.claim").is_dir())
        self.assertTrue((self.job / "clear_unconfirmed").is_file())
        self.assertFalse((self.job / "done").exists())
        self.assertFalse((self.job / "cleaned").exists())
        status = self.shell("status")
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", status.stdout)
        self.assertTrue(decoded["running"])
        self.assertIn(CLEAR_WARNING, decoded["warning"])
        self.assertNotEqual(self.shell("launch", "f" * 32, "1", "1", "").returncode, 0)
        # A previously deployed worker can still publish an old-style receipt.
        fields = metadata["state"].decode().splitlines()
        fields[1] = "completed"
        (self.job / "state").write_text("\n".join(fields) + "\n")
        (self.job / "done").write_text(self.identity + "\n")
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", self.shell("status").stdout)
        self.assertTrue(decoded["running"])
        self.assertNotEqual(self.shell("launch", "f" * 32, "1", "1", "").returncode, 0)
        self.assertNotEqual(self.shell("clear", self.identity).returncode, 0)

    def test_live_operations_reject_unsafe_files_identity_and_busy_locks(self):
        self.orphan()
        archive = (self.job / "raw.txt").read_bytes()
        for operation in ("pull-live", "clear-live"):
            self.assertNotEqual(self.shell(operation, "f" * 32).returncode, 0)
        for name in ("control.busy", "commit.busy"):
            lock = (self.root if name == "control.busy" else self.job) / name
            lock.mkdir()
            operations = ("pull-live", "clear-live") if name == "control.busy" else ("clear-live",)
            for operation in operations:
                self.assertNotEqual(self.shell(operation, self.identity).returncode, 0)
                self.assertTrue(lock.is_dir())
            lock.rename(lock.with_name("checked-" + name))
        for name in ("raw.txt", "sample", "header", "clear_unconfirmed"):
            path = self.job / name
            saved = path.with_name("saved-" + name)
            if path.exists():
                path.rename(saved)
            for kind in ("link", "directory"):
                if kind == "link":
                    path.symlink_to(self.fixture)
                else:
                    path.mkdir()
                operations = ("pull-live", "clear-live") if name == "raw.txt" else ("clear-live",)
                for operation in operations:
                    self.assertNotEqual(self.shell(operation, self.identity).returncode, 0)
                path.rename(path.with_name(kind + "-" + name))
            if saved.exists():
                saved.rename(path)
        self.assertEqual((self.job / "raw.txt").read_bytes(), archive)
        self.assertEqual(self.fixture.read_bytes(), TOP)
        self.assertFalse((self.job / "clear_unconfirmed").exists())

    def test_live_pull_rejects_oversize_and_read_failures(self):
        self.orphan()
        archive = (self.job / "raw.txt").read_bytes()
        self.write_script(limit=len(archive) - 1)
        self.assertNotEqual(self.shell("pull-live", self.identity).returncode, 0)
        self.write_script()
        reader = ("import sys; from pathlib import Path; p=Path(sys.argv[3]); "
                  "sys.exit(7) if p.name == 'raw.txt' else None; "
                  "sys.stdout.buffer.write(p.open('rb').read(int(sys.argv[2])))")
        self.command("head", shlex.quote(sys.executable) + " -c " + shlex.quote(reader) + ' "$@"')
        self.assertNotEqual(self.shell("pull-live", self.identity).returncode, 0)
        self.assertEqual((self.job / "raw.txt").read_bytes(), archive)

    def test_live_clear_partial_failure_marks_before_truncating(self):
        self.orphan()
        (self.job / "sample").write_bytes(b"keep")
        original = (self.bin / "truncate").read_text().splitlines()[1]
        self.command("truncate", '[ -f "$JOB/clear_unconfirmed" ] || exit 88; '
                     '[ -d "$JOB/commit.busy" ] || exit 89; '
                     'case "$3" in */sample) exit 1;; esac; ' + original)
        result = self.shell("clear-live", self.identity)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.job / "raw.txt").read_bytes(), b"")
        self.assertEqual((self.job / "sample").read_bytes(), b"keep")
        self.assertTrue((self.job / "clear_unconfirmed").exists())
        self.assertFalse((self.job / "commit.busy").exists())
        self.assertFalse((self.root / "control.busy").exists())
        (self.job / "state").write_text("broken\n")
        status = self.shell("status")
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", status.stdout)
        self.assertTrue(decoded["state_corrupt"])
        self.assertIn(CLEAR_WARNING, decoded["warning"])

    def test_finish_and_live_clear_are_serialized_in_both_orders(self):
        for first in ("clear", "finish"):
            with self.subTest(first=first):
                self.prepare_worker()
                entered = self.root / (first + "-entered")
                resume = self.root / (first + "-resume")
                gate = ("from pathlib import Path; import time; "
                        f"Path({str(entered)!r}).touch(); deadline=time.monotonic()+5\n"
                        f"while not Path({str(resume)!r}).exists():\n"
                        " if time.monotonic()>deadline: raise SystemExit(77)\n"
                        " time.sleep(0.01)\n")
                self.command("test_gate", shlex.quote(sys.executable) + " -c " + shlex.quote(gate))
                original = self.source
                anchor = "    commit_lock\n    trap release_commit EXIT"
                replacement = ("    test_gate || exit 77\n" + anchor if first == "clear" else
                               anchor + "\n    test_gate || exit 77")
                self.source = original.replace(anchor, replacement, 1)
                self.write_script()
                self.source = original
                process = subprocess.Popen(["sh", str(self.script), "worker", self.identity,
                    "1", "1", ""], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 4
                    while not entered.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(entered.exists(), "worker did not reach terminal publication gate")
                    before = (self.job / "raw.txt").read_bytes()
                    result = self.shell("clear-live", self.identity)
                    if first == "clear":
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertTrue((self.job / "commit.busy").is_dir())
                        self.assertFalse((self.job / "clear_unconfirmed").exists())
                        self.assertEqual((self.job / "raw.txt").read_bytes(), before)
                finally:
                    resume.touch()
                    output, error = process.communicate(timeout=8)
                if first == "clear":
                    self.assertNotEqual(process.returncode, 0, error)
                    self.assertFalse((self.job / "done").exists())
                else:
                    self.assertEqual(process.returncode, 0, error)
                    self.assertTrue((self.job / "done").exists())
                    self.assertEqual(self.shell("clear-live", self.identity).returncode, 0)
                self.assertEqual((self.job / "raw.txt").read_bytes(), b"")
                self.assertTrue((self.job / "worker.claim").is_dir())
                self.assertFalse((self.job / "commit.busy").exists())
                decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", self.shell("status").stdout)
                self.assertTrue(decoded["running"])
                self.assertIn(CLEAR_WARNING, decoded["warning"])

    def test_live_pull_is_bounded_to_initial_length_during_growth(self):
        self.orphan()
        archive = (self.job / "raw.txt").read_bytes()
        reader = ("import sys; from pathlib import Path; p=Path(sys.argv[3]); "
                  "p.open('ab').write(b'growing') if p.name == 'raw.txt' else None; "
                  "sys.stdout.buffer.write(p.open('rb').read(int(sys.argv[2])))")
        self.command("head", shlex.quote(sys.executable) + " -c " + shlex.quote(reader) + ' "$@"')
        result = self.shell("pull-live", self.identity)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.encode(), archive)
        self.assertEqual((self.job / "raw.txt").read_bytes(), archive + b"growing")
        self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)

    def test_reboot_status_is_read_only_and_pull_keeps_uncommitted_tail(self):
        self.orphan()
        self.record_boot()
        before = {p.name: p.read_bytes() for p in self.job.iterdir() if p.is_file()}
        self.assertNotIn("rebooted", self.shell("status").stdout.splitlines())
        self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
        self.boot_path.write_text(self.other_boot + "\n")
        status = self.shell("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stdout.splitlines()[-1], "rebooted")
        self.assertEqual(status.stdout.splitlines()[1], "running")
        self.assertEqual({p.name: p.read_bytes() for p in self.job.iterdir() if p.is_file()}, before)
        pulled = self.shell("pull", self.identity)
        self.assertEqual(pulled.returncode, 0, pulled.stderr)
        self.assertEqual(pulled.stdout.encode(), before["raw.txt"])
        self.assertFalse((self.job / "done").exists())
        self.assertFalse((self.job / "cleaned").exists())
        self.assertEqual((self.job / "state").read_bytes(), before["state"])
        cleared = self.shell("clear", self.identity)
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertEqual((self.job / "raw.txt").stat().st_size, 0)
        self.assertTrue((self.job / "cleaned").exists())
        self.assertFalse((self.job / "done").exists())
        self.assertEqual((self.job / "state").read_bytes(), before["state"])

    def test_boot_metadata_missing_invalid_or_symlink_is_not_reboot_proof(self):
        self.orphan()
        self.boot_path.write_text(self.other_boot + "\n")
        boot = self.job / "boot"
        good = self.identity + "\n" + self.boot_id + "\n"
        values = [None, good[:-1], good + "x", "e" * 32 + good[32:],
                  good.replace(self.boot_id, self.boot_id.upper()), good.replace("-", "x", 1)]
        for index, value in enumerate(values):
            with self.subTest(value=value):
                if value is not None:
                    boot.write_text(value)
                self.assertNotIn("rebooted", self.shell("status").stdout.splitlines())
                self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
                self.assertNotEqual(self.shell("clear", self.identity).returncode, 0)
                self.assertFalse((self.job / "done").exists())
                self.assertEqual((self.job / "raw.txt").read_bytes(), RAW + b"partial uncommitted sample")
                if value is not None:
                    boot.rename(self.root / f"invalid-boot-{index}")
        target = self.root / "boot-target"
        target.write_text(good)
        boot.symlink_to(target)
        self.assertNotIn("rebooted", self.shell("status").stdout.splitlines())
        self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
        self.assertEqual(target.read_text(), good)

    def test_live_boot_missing_invalid_or_symlink_blocks_recovery_and_launch(self):
        self.orphan()
        self.record_boot()
        saved = self.boot_path.with_name("saved")
        self.boot_path.rename(saved)
        for value in (None, "invalid", self.other_boot.upper(), self.other_boot + "extra"):
            with self.subTest(value=value):
                if value is not None:
                    self.boot_path.write_text(value + "\n")
                self.assertNotIn("rebooted", self.shell("status").stdout.splitlines())
                self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
                self.assertNotEqual(self.shell("launch", "e" * 32, "1", "1", "").returncode, 0)
                self.assertFalse((self.root / ("e" * 32)).exists())
        self.boot_path.rename(self.boot_path.with_name("invalid"))
        self.boot_path.symlink_to(saved)
        self.assertNotIn("rebooted", self.shell("status").stdout.splitlines())
        # No previous task: boot validation itself must reject launch.
        (self.root / "current").rename(self.root / "saved-current")
        self.assertNotEqual(self.shell("launch", "e" * 32, "1", "1", "").returncode, 0)
        self.assertFalse((self.root / ("e" * 32)).exists())

    def test_worker_rejects_changed_or_missing_boot_before_claim(self):
        self.prepare_worker()
        self.write_script()
        self.boot_path.write_text(self.other_boot + "\n")
        self.reject_worker_entry()
        self.assertFalse((self.job / "worker.claim").exists())
        self.boot_path.write_text(self.boot_id + "\n")
        (self.job / "boot").rename(self.job / "saved-boot")
        self.reject_worker_entry()
        self.assertFalse((self.job / "worker.claim").exists())

    def test_reboot_does_not_bypass_control_lock_or_missing_terminal_receipt(self):
        self.orphan()
        self.record_boot()
        self.boot_path.write_text(self.other_boot + "\n")
        busy = self.root / "control.busy"
        busy.mkdir()
        (busy / "owner").write_text("preserve")
        status = self.shell("status")
        self.assertEqual(status.stdout.splitlines()[-2:], ["rebooted", "control_busy"])
        for action in ("stop", "pull", "clear"):
            self.assertNotEqual(self.shell(action, self.identity).returncode, 0)
            self.assertEqual((busy / "owner").read_text(), "preserve")
        busy.rename(self.root / "retained-lock")
        self.orphan(status="completed")
        status = self.shell("status")
        self.assertIn("unconfirmed", status.stdout.splitlines())
        self.assertNotIn("rebooted", status.stdout.splitlines())
        self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
        (self.job / "done").write_text(self.identity + "\n")
        status = self.shell("status")
        self.assertNotIn("unconfirmed", status.stdout.splitlines())
        self.assertNotIn("rebooted", status.stdout.splitlines())
        self.assertEqual(self.shell("pull", self.identity).returncode, 0)

    def test_launch_after_reboot_preserves_old_task_and_records_current_boot(self):
        self.orphan()
        self.record_boot()
        before = {p.name: p.read_bytes() for p in self.job.iterdir() if p.is_file()}
        identity = "e" * 32
        job = self.root / identity
        self.assertNotEqual(self.shell("launch", identity, "1", "1", "").returncode, 0)
        self.assertFalse(job.exists())
        self.boot_path.write_text(self.other_boot + "\n")
        self.command("timeout", 'sleep 1; exit 124')
        self.command("setsid", 'exec "$@"')
        self.command("nohup", 'touch ' + shlex.quote(str(job / "ready")))
        result = self.shell("launch", identity, "1", "1", "")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((job / "boot").read_text(), identity + "\n" + self.other_boot + "\n")
        self.assertEqual({p.name: p.read_bytes() for p in self.job.iterdir() if p.is_file()}, before)
        self.assertFalse((self.job / "done").exists())
        self.assertFalse((self.job / "cleaned").exists())

    def test_byte_reads_work_without_head_c(self):
        self.worker()
        archive = (self.job / "raw.txt").read_bytes()
        self.command("head", 'case "$1" in -c) echo "head: unsupported -c" >&2; exit 1;; esac; exec /usr/bin/head "$@"')
        status = self.shell("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stdout.splitlines()[0], self.identity)
        self.assertEqual(status.stdout.splitlines()[1], "completed")
        pulled = self.shell("pull", self.identity)
        self.assertEqual(pulled.returncode, 0, pulled.stderr)
        self.assertEqual(pulled.stdout.encode(), archive)
        self.assertEqual(self.shell("clear", self.identity).returncode, 0)

    def test_fallback_byte_reader_preserves_block_boundaries(self):
        self.command("head", 'exit 1')
        payload = bytes(range(256)) * 513
        self.fixture.write_bytes(payload)
        functions = self.source.split("MODE=$1\n", 1)[0]
        for limit in (0, 33, 65535, 65536, 65537, 131079, len(payload) + 10):
            with self.subTest(limit=limit):
                result = subprocess.run(
                    ["sh", "-c", functions + '\nread_bytes "$1" "$2"',
                     "reader", str(limit), str(self.fixture)],
                    env=self.env, capture_output=True, timeout=4)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, payload[:limit])

    def test_status_idle_busy_and_dangling_current(self):
        self.write_script()
        idle = self.shell("status")
        self.assertEqual(idle.returncode, 0, idle.stderr)
        self.assertEqual(idle.stdout.strip(), "idle")
        busy = self.root / "control.busy"
        self.assertFalse(busy.exists())
        busy.mkdir()
        (busy / "owner").write_text("preserve")
        result = self.shell("status")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("idle", result.stdout)
        self.assertEqual((busy / "owner").read_text(), "preserve")
        busy.rename(self.root / "retained-busy")
        (self.root / "current").symlink_to(self.root / "missing")
        result = self.shell("status")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("idle", result.stdout)
        self.assertTrue((self.root / "current").is_symlink())
        self.assertFalse(busy.exists())

    def test_status_validates_and_prints_one_snapshot(self):
        self.orphan()
        original = (self.job / "state").read_text()
        replacement = self.job / "replacement"
        replacement.write_text("corrupt replacement\n")
        # Replace the published state immediately after the bounded read.
        self.command("head", 'if [ "$1" = -c ] && [ "$2" = 0 ]; then exit 0; fi\n'
                     '/usr/bin/head "$@"; rc=$?\n'
                     'if [ "$rc" = 0 ] && [ "$1" = -c ] && [ "$2" = 2049 ] && '
                     '[ "$3" = "$JOB/state" ] && [ -f "$JOB/replacement" ]; then '
                     '/bin/mv "$JOB/replacement" "$JOB/state" || exit 1; fi\nexit "$rc"')
        result = self.shell("status")
        self.assert_pending_receipt(result)
        self.assertTrue(result.stdout.startswith(original))
        self.assertEqual((self.job / "state").read_text(), "corrupt replacement\n")
        self.assertFalse((self.root / "control.busy").exists())
        result = self.shell("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[:3],
                         ["corrupt", self.identity, "unconfirmed"])
        self.assertFalse((self.root / "control.busy").exists())

    def test_status_observes_retained_control_lock_without_authorizing_mutation(self):
        self.orphan(status="completed")
        (self.job / "done").write_text(self.identity)
        before = {p.name: p.read_bytes() for p in self.job.iterdir()}
        busy = self.root / "control.busy"
        busy.mkdir()
        (busy / "state").write_text("interrupted legacy query")
        inode = busy.stat().st_ino
        result = self.shell("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "control_busy")
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", result.stdout.strip())
        self.assertEqual(decoded["count"], 1)
        self.assertFalse(decoded["connected"])
        self.assertTrue(decoded["running"])
        self.assertIn("控制锁", decoded["connection_error"])
        for mode in ("stop", "pull", "clear", "start", "launch"):
            args = ("e" * 32, "1", "1", "") if mode in {"start", "launch"} else (self.identity,)
            self.assertNotEqual(self.shell(mode, *args).returncode, 0)
        self.assertEqual(busy.stat().st_ino, inode)
        self.assertEqual((busy / "state").read_text(), "interrupted legacy query")
        self.assertEqual({p.name: p.read_bytes() for p in self.job.iterdir()}, before)
        self.assertEqual(list(self.root.glob("status.*")), [])
        (self.job / "state").write_text("corrupt\n")
        result = self.shell("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", result.stdout.strip())
        self.assertTrue(decoded["state_corrupt"])
        self.assertTrue(decoded["running"])
        self.assertIn("控制锁", decoded["connection_error"])

    def test_interrupted_status_does_not_block_next_query_or_stop(self):
        import signal
        self.orphan()
        before = (self.job / "raw.txt").read_bytes()
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=sig):
                entered = self.root / ("snapshot-entered-" + str(sig))
                release = self.root / ("snapshot-release-" + str(sig))
                self.command("head", 'if [ "$1" = -c ] && [ "$2" = 0 ]; then exit 0; fi\n'
                       'if [ "$1" = -c ] && [ "$2" = 2049 ] && '
                             '/bin/mkdir ' + shlex.quote(str(entered)) + ' 2>/dev/null; then\n'
                             'while [ ! -f ' + shlex.quote(str(release)) + ' ]; do sleep .02; done\n'
                             'fi\nexec /usr/bin/head "$@"')
                process = subprocess.Popen(["sh", str(self.script), "status"], env=self.env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           start_new_session=True)
                try:
                    self.wait_for_path(entered, process)
                    self.assertFalse((self.root / "control.busy").exists())
                    process.send_signal(sig)
                    # Even while the first reader's child is alive, polling and
                    # a cooperative stop request are independent of its scratch.
                    self.assert_pending_receipt(self.shell("status"))
                    self.assertEqual(self.shell("stop", self.identity).returncode, 0)
                finally:
                    release.touch()
                    process.wait(timeout=5)
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                self.assertNotEqual(process.returncode, 0)
                self.assert_pending_receipt(self.shell("status"))
                self.assertFalse((self.root / "control.busy").exists())
                self.assertFalse((self.job / "done").exists())
                self.assertEqual((self.job / "raw.txt").read_bytes(), before)
                self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
                self.assertNotEqual(self.shell("clear", self.identity).returncode, 0)

    def test_current_accepts_only_optional_lf(self):
        self.orphan()
        for ending in (b"", b"\n"):
            with self.subTest(ending=ending):
                (self.root / "current").write_bytes(self.identity.encode() + ending)
                result = self.shell("status")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines()[0], self.identity)

    def test_current_read_failure_is_not_reported_as_invalid_identity(self):
        self.orphan()
        self.command("head", 'echo "read failed" >&2; exit 1')
        self.command("dd", 'echo "read failed" >&2; exit 1')
        before = (self.job / "raw.txt").read_bytes()
        status = self.shell("status")
        self.assertNotEqual(status.returncode, 0)
        self.assertIn("Cannot read current capture identity", status.stderr)
        self.assertEqual((self.job / "raw.txt").read_bytes(), before)
        self.assertFalse((self.job / "done").exists())

    def test_invalid_current_never_stops_or_changes_task(self):
        self.orphan()
        before = (self.job / "raw.txt").read_bytes()
        for identity in (b"", b"../outside", b"d" * 31, b"d" * 33,
                         b"d" * 32 + b"\r\n", b"d" * 32 + b"\nextra"):
            (self.root / "current").write_bytes(identity)
            for mode, args in (("status", ()), ("stop", (self.identity,)),
                               ("pull", (self.identity,)), ("clear", (self.identity,))):
                with self.subTest(identity=identity, mode=mode):
                    result = self.shell(mode, *args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("Invalid current capture identity", result.stderr)
                    self.assertEqual((self.job / "raw.txt").read_bytes(), before)
                    self.assertFalse((self.job / "stop").exists())
                    self.assertFalse((self.job / "done").exists())

    def wait_for_path(self, path, process, timeout=5):
        deadline = time.monotonic() + timeout
        while not path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(path.exists(), f"Missing {path.name}; exit={process.poll()}")

    def test_control_directory_conflicts_never_steal_or_modify(self):
        self.prepare_clear()
        before = {n: (self.job / n).read_bytes() for n in ("state", "raw.txt")}
        busy = self.root / "control.busy"
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    busy.mkdir()
                    (busy / "owner").write_text("interrupted owner")
                elif kind == "file":
                    busy.write_text("do not replace")
                else:
                    busy.symlink_to(self.fixture)
                inode = busy.lstat().st_ino
                for mode in ("start", "launch", "clear"):
                    args = (self.identity,) if mode == "clear" else ("e" * 32, "1", "1", "")
                    self.assertNotEqual(self.shell(mode, *args).returncode, 0)
                    self.assertEqual(busy.lstat().st_ino, inode)
                    self.assertEqual({n: (self.job / n).read_bytes() for n in before}, before)
                    self.assertFalse((self.root / ("e" * 32)).exists())
                self.assertEqual(self.fixture.read_bytes(), TOP)
                busy.rename(self.root / (kind + "-busy-checked"))

    def test_control_directory_normal_exit_releases_but_signals_preserve(self):
        import signal
        self.write_script()
        functions = self.script.read_text().split("MODE=$1\n", 1)[0]
        busy = self.root / "control.busy"
        for code in (0, 1):
            result = subprocess.run(["sh", "-c", functions + f'\ncontrol_lock; exit {code}'],
                                    env=self.env, capture_output=True, timeout=4)
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertFalse(busy.exists())
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig):
                entered = self.root / ("control-entered-" + str(sig))
                code = functions + '\ncontrol_lock\n: > "$1"\nwhile :; do sleep .02; done\n'
                process = subprocess.Popen(["sh", "-c", code, "control", str(entered)],
                                           env=self.env, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
                try:
                    self.wait_for_path(entered, process)
                    inode = busy.stat().st_ino
                    process.send_signal(sig)
                    process.wait(timeout=5)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertTrue(busy.is_dir())
                    self.assertEqual(busy.stat().st_ino, inode)
                    retry = subprocess.run(["sh", "-c", functions + '\ncontrol_lock'],
                                           env=self.env, capture_output=True, timeout=4)
                    self.assertNotEqual(retry.returncode, 0)
                    self.assertEqual(busy.stat().st_ino, inode)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    if busy.exists():
                        busy.rename(self.root / ("retained-control-" + str(sig)))

    def test_start_and_launch_share_atomic_control_directory(self):
        self.write_script()
        entered = self.root / "launch-entered"
        release = self.root / "launch-release"
        self.command("setsid", 'exec "$@"')
        self.command("timeout", 'sleep 1; exit 124')
        # Hold the successful atomic mkdir, outside the timeout capability probe.
        # Contenders use real mkdir and must fail without entering this barrier.
        self.command("mkdir", '/bin/mkdir "$@"; rc=$?\n'
                     'if [ "$rc" -eq 0 ] && [ "$1" = ' +
                     shlex.quote(str(self.root / "control.busy")) + ' ]; then\n: > ' +
                     shlex.quote(str(entered)) + '\nwhile [ ! -f ' +
                     shlex.quote(str(release)) + ' ]; do sleep .02; done\nfi\nexit "$rc"')
        self.command("nohup", 'for arg do case "$arg" in ' + "e" * 32 +
                     ') touch "$JOB/ready";; esac; done')
        identity = "e" * 32
        job = self.root / identity
        env = dict(self.env, JOB=str(job))
        process = subprocess.Popen(["sh", str(self.script), "start", identity, "1", "1", ""],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.wait_for_path(entered, process)
            busy = self.root / "control.busy"
            self.assertTrue(busy.is_dir())
            inode = busy.stat().st_ino
            for mode in ("start", "launch"):
                result = self.shell(mode, "f" * 32, "1", "1", "")
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(busy.stat().st_ino, inode)
                self.assertFalse((self.root / ("f" * 32)).exists())
            self.assertFalse((self.root / "current").exists())
        finally:
            release.touch()
            process.wait(timeout=12)
        self.assertEqual(process.returncode, 0)
        self.assertEqual((self.root / "current").read_text().strip(), identity)
        self.assertFalse((self.root / "control.busy").exists())
        self.assertFalse((job / "worker.lock").exists())

    def test_worker_requires_matching_current_and_starting_state(self):
        self.prepare_worker()
        self.write_script()
        state = (self.job / "state").read_bytes()
        for status in ("running", "completed", "stopped", "error"):
            with self.subTest(status=status):
                (self.job / "state").write_bytes(state.replace(b"starting", status.encode()))
                self.reject_worker_entry()
        (self.job / "state").write_bytes(state)
        (self.root / "current").write_text("e" * 32)
        self.reject_worker_entry()
        (self.root / "current").rename(self.root / "saved-current")
        self.reject_worker_entry()

    def test_worker_claim_conflicts_preserve_starting_task(self):
        self.prepare_worker()
        self.write_script()
        claim = self.job / "worker.claim"
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind):
                if kind == "directory":
                    claim.mkdir()
                elif kind == "file":
                    claim.write_text("occupied")
                else:
                    claim.symlink_to(self.fixture)
                inode = claim.lstat().st_ino
                self.reject_worker_entry()
                self.assertEqual(claim.lstat().st_ino, inode)
                self.assertEqual(self.fixture.read_bytes(), TOP)
                claim.rename(self.job / (kind + "-claim-checked"))

    def test_concurrent_workers_claim_once_and_publish_done_before_terminal(self):
        self.prepare_worker()
        self.write_script()
        entered = self.root / "terminal-entered"
        release = self.root / "terminal-release"
        # Pause the real state publication, not the worker or receipt logic.
        self.command("mv", 'if [ "$1" = "$JOB/state.new" ] && '
                     'grep -q "^completed$" "$1"; then\n: > ' + shlex.quote(str(entered)) +
                     '\nwhile [ ! -f ' + shlex.quote(str(release)) +
                     ' ]; do sleep .02; done\nfi\nexec /bin/mv "$@"')
        argv = ["sh", str(self.script), "worker", self.identity, "1", "1", ""]
        processes = []
        try:
            for _ in range(2):
                processes.append(subprocess.Popen(argv, env=self.env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            deadline = time.monotonic() + 6
            while not entered.exists() and time.monotonic() < deadline:
                if all(p.poll() is not None for p in processes):
                    break
                time.sleep(.02)
            self.assertTrue(entered.exists(), [p.poll() for p in processes])
            self.assertTrue((self.job / "worker.claim").is_dir())
            self.assertEqual((self.job / "done").read_text().strip(), self.identity)
            self.assertEqual((self.job / "state").read_text().splitlines()[1], "running")
            before = (self.job / "raw.txt").read_bytes()
            self.assertEqual(before.count(b"========== Top"), 1)
            self.assertTrue(before.endswith(TOP + b"\n\n"))
            self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
            self.assertNotEqual(self.shell("clear", self.identity).returncode, 0)
            self.assertEqual((self.job / "raw.txt").read_bytes(), before)
        finally:
            release.touch()
            for process in processes:
                process.wait(timeout=12)
        self.assertEqual(sum(p.returncode == 0 for p in processes), 1)
        state = (self.job / "state").read_text().splitlines()
        self.assertEqual(state[1:3], ["completed", "1"])
        self.assertEqual(int(state[5]), len(before))
        self.assertEqual((self.job / "raw.txt").read_bytes(), before)
        self.assertEqual(self.shell("pull", self.identity).stdout.encode(), before)

    def test_worker_claim_is_permanent_and_rejects_reentry(self):
        self.worker()
        claim = self.job / "worker.claim"
        self.assertTrue(claim.is_dir())
        inode = claim.stat().st_ino
        before = {p.name: p.read_bytes() for p in self.job.iterdir() if p.is_file()}
        self.assertNotEqual(self.shell("worker", self.identity, "1", "1", "").returncode, 0)
        self.assertEqual(claim.stat().st_ino, inode)
        self.assertEqual({p.name: p.read_bytes() for p in self.job.iterdir() if p.is_file()}, before)
        self.assertEqual(self.shell("clear", self.identity).returncode, 0)
        self.assertTrue(claim.is_dir())
        self.assertNotEqual(self.shell("worker", self.identity, "1", "1", "").returncode, 0)

    def test_proc_visibility_and_pid_reuse_never_authorize_recovery(self):
        self.orphan()
        before = {n: (self.job / n).read_bytes() for n in ("state", "raw.txt")}
        self.proc.rename(self.root / "original-proc")
        self.proc.mkdir()
        self.assertEqual(list(self.proc.iterdir()), [])
        for condition in ("empty", "reused_pid", "hidden"):
            with self.subTest(condition=condition):
                if condition == "reused_pid":
                    self.proc_stat(123, start=999999)
                    (self.proc / "123" / "cmdline").write_bytes(b"unrelated\0")
                elif condition == "hidden":
                    self.proc.rename(self.root / "hidden-proc")
                result = self.shell("status")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_pending_receipt(result)
                self.assertEqual(self.shell("stop", self.identity).returncode, 0)
                for mode in ("pull", "clear"):
                    self.assertNotEqual(self.shell(mode, self.identity).returncode, 0)
                self.assertEqual({n: (self.job / n).read_bytes() for n in before}, before)
                self.assertFalse((self.job / "done").exists())

    def test_append_failure_preserves_partial_bytes_without_done(self):
        self.prepare_worker()
        self.write_script()
        self.command("cat", 'case "$1" in */header) printf "partial append"; exit 1;; '
                     '*) exec /bin/cat "$@";; esac')
        result = self.shell("worker", self.identity, "1", "1", "")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.job / "worker.claim").is_dir())
        self.assertFalse((self.job / "done").exists())
        self.assertEqual((self.job / "raw.txt").read_bytes(), b"partial append")
        before = (self.job / "state").read_bytes()
        status = self.shell("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assert_pending_receipt(status)
        for mode in ("pull", "clear"):
            self.assertNotEqual(self.shell(mode, self.identity).returncode, 0)
        self.assertEqual((self.job / "state").read_bytes(), before)
        self.assertEqual((self.job / "raw.txt").read_bytes(), b"partial append")

    def assert_pending_receipt(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", result.stdout.strip())
        # Successful state reads are connected, not proof of a live worker.
        # Every state lacking a terminal receipt remains unsafe to pull/clear.
        self.assertTrue(decoded["running"])
        self.assertEqual(decoded["connected"], decoded["status"] in {"starting", "running"})

    def test_orphans_never_recover_or_truncate(self):
        for lines in (8, 10):
            for status in ("starting", "running", "completed", "stopped", "error"):
                with self.subTest(lines=lines, status=status):
                    self.orphan(lines=lines, status=status)
                    before = {n: (self.job / n).read_bytes() for n in ("state", "raw.txt")}
                    self.assertEqual(self.shell("stop", self.identity).returncode, 0)
                    self.assert_pending_receipt(self.shell("status"))
                    for mode in ("pull", "clear"):
                        self.assertNotEqual(self.shell(mode, self.identity).returncode, 0)
                    self.assertNotEqual(self.shell("launch", "e" * 32, "1", "1", "").returncode, 0)
                    self.assertEqual({n: (self.job / n).read_bytes() for n in before}, before)
                    self.assertFalse((self.job / "done").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root bypasses mode permissions")
    def test_unreadable_capture_metadata_never_reports_idle_or_changes_logs(self):
        self.orphan()
        original = (self.job / "raw.txt").read_bytes()
        script_content = self.script.read_text()
        for path, permissions, message in (
            (self.root / "current", 0o000, "current pointer permission denied"),
            (self.root, 0o600, "capture directory permission denied"),
        ):
            previous = path.stat().st_mode & 0o777
            try:
                path.chmod(permissions)
                for mode, args in (("status", ()), ("stop", (self.identity,)),
                                   ("pull", (self.identity,)), ("clear", (self.identity,))):
                    with self.subTest(path=path.name, mode=mode):
                        # Feed the script directly: the fixture stores it inside
                        # ROOT, whose search permission is intentionally removed.
                        result = subprocess.run(
                            ["sh", "-c", script_content, "control.sh", mode, *args],
                            env=self.env, capture_output=True, text=True, timeout=4,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(message, result.stderr)
                        self.assertIn("state unconfirmed", result.stderr)
                        self.assertNotIn("idle", result.stdout)
            finally:
                path.chmod(previous)
            self.assertEqual((self.job / "raw.txt").read_bytes(), original)
            self.assertFalse((self.job / "stop").exists())
            self.assertFalse((self.job / "done").exists())

    def test_abnormal_signals_wait_for_child_without_confirming_done(self):
        import signal
        import sys
        for phase in ("top", "append"):
            for sig in (signal.SIGTERM, signal.SIGINT):
                with self.subTest(phase=phase, signal=sig):
                    self.prepare_worker()
                    self.write_script()
                    (self.job / "raw.txt").write_bytes(b"")
                    entered = self.job / "child-entered"
                    release = self.job / "child-release"
                    exited = self.job / "child-exited"
                    # A real foreground child writes a partial sample, then
                    # waits for the test. Signals target only our Popen child.
                    code = (
                        "import os,sys,time\nfrom pathlib import Path\n"
                        "job=Path(os.environ['JOB'])\n"
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
                        self.command("cat", 'case "$1" in */header) ' + writer +
                                     ' "$@";; *) exec /bin/cat "$@";; esac')
                    with open(os.devnull, "wb") as output:
                        process = subprocess.Popen(
                            ["sh", str(self.script), "worker", self.identity, "1", "1", ""],
                            env=self.env, stdout=output, stderr=output)
                        try:
                            self.wait_for_path(entered, process)
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
                    self.assertNotEqual(process.returncode, 0)
                    self.assertTrue(exited.exists())
                    self.assertFalse((self.job / "done").exists())
                    self.assertTrue((self.job / "worker.claim").is_dir())
                    raw = (self.job / "raw.txt").read_bytes()
                    if phase == "append":
                        self.assertTrue(raw.endswith(TOP + b"\n\n"))
                    snapshot = (self.job / "state").read_bytes()
                    self.assert_pending_receipt(self.shell("status"))
                    for mode in ("pull", "clear"):
                        self.assertNotEqual(self.shell(mode, self.identity).returncode, 0)
                    self.assertEqual((self.job / "state").read_bytes(), snapshot)
                    self.assertEqual((self.job / "raw.txt").read_bytes(), raw)

    def test_real_worker_remains_active_until_cooperative_stop(self):
        self.prepare_worker(target=-1)
        self.write_script()
        # Stop through the task marker, never by discovering or signalling a PID.
        self.command("top", 'sleep 1; cat "$TOP_FIXTURE"')
        with open(os.devnull, "wb") as output:
            process = subprocess.Popen(["sh", str(self.script), "worker", self.identity,
                                        "-1", "1", ""], env=self.env,
                                       stdout=output, stderr=output)
            try:
                self.wait_for_path(self.job / "ready", process, timeout=3)
                status = self.shell("status")
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(status.stdout.splitlines()[1], "running")
                self.assert_pending_receipt(status)
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

    def test_numeric_identity_corruption_never_recovers_archive(self):
        self.identity = "0" * 31 + "1"
        self.job = self.root / self.identity
        self.job.mkdir()
        self.orphan()
        fields = (self.job / "state").read_text().splitlines()
        archive = (self.job / "raw.txt").read_bytes()
        for bad_id in ("1", "01", "0" * 30 + "1", "0" * 32 + "1"):
            with self.subTest(identity=bad_id):
                fields[0] = bad_id
                state = "\n".join(fields) + "\n"
                (self.job / "state").write_text(state)
                result = self.shell("status")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), ["corrupt", self.identity,
                                 "unconfirmed", "present"])
                for mode in ("pull", "clear"):
                    self.assertNotEqual(self.shell(mode, self.identity).returncode, 0)
                self.assertEqual((self.job / "state").read_text(), state)
                self.assertEqual((self.job / "raw.txt").read_bytes(), archive)
                self.assertFalse((self.job / "done").exists())
        fields[0] = self.identity
        fields[1] = "completed"
        (self.job / "state").write_text("\n".join(fields) + "\n")
        (self.job / "done").write_text(self.identity)
        result = self.shell("status")
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", result.stdout)
        self.assertEqual(decoded["id"], self.identity)
        self.assertFalse(decoded["state_corrupt"])

    def test_missing_final_newline_never_confirms_or_recovers(self):
        self.orphan(status="completed")
        (self.job / "done").write_text(self.identity)
        state = (self.job / "state").read_bytes()[:-1]
        (self.job / "state").write_bytes(state)
        archive = (self.job / "raw.txt").read_bytes()
        self.assertEqual(self.shell("status").stdout.splitlines(),
                     ["corrupt", self.identity, "unconfirmed", "present"])
        self.assertNotEqual(self.shell("launch", "e" * 32, "1", "1", "").returncode, 0)
        self.assertNotEqual(self.shell("clear", self.identity).returncode, 0)
        self.assertEqual(self.shell("stop", self.identity).returncode, 0)
        result = self.shell("status")
        self.assertEqual(result.stdout.splitlines(),
                 ["corrupt", self.identity, "unconfirmed", "present"])
        decoded = TopCapture._decode(TopCapture.__new__(TopCapture), "device-1", result.stdout)
        self.assertTrue(decoded["running"])
        self.assertTrue(decoded["state_corrupt"])
        self.assertFalse(decoded["connected"])
        self.assertNotEqual(self.shell("pull", self.identity).returncode, 0)
        self.assertEqual((self.job / "state").read_bytes(), state)
        self.assertEqual((self.job / "raw.txt").read_bytes(), archive)

    def test_shell_and_python_state_constraints_agree(self):
        self.orphan(status="completed")
        (self.job / "done").write_text(self.identity)
        baseline = (self.job / "state").read_text().splitlines()
        archive = (self.job / "raw.txt").read_bytes()
        invalid = [(2, "10000001"), (2, "00000000001"),
                   (3, "0"), (3, "100001"), (3, "0000001"),
                   (4, "1." + "0" * 23), (4, "3601"),
                   (5, "00000000000"), (5, str(MAX_ARCHIVE_BYTES + 1)),
                   (6, "unknown_error"), (7, "a"), (7, "ff"),
                   (7, "c080"), (7, "eda080"), (7, "f4908080"),
                   (7, "e4b8"), (7, "80"), (7, "c241"),
                   (7, "61" * 121), (7, "f09f9880" * 121),
                   (8, "0000000000000"), (9, "-1")]
        valid = [(2, "10000000"), (2, "0000000001"),
                 (3, "100000"), (3, "000001"), (3, "-1"),
                 (4, "1." + "0" * 22), (4, "3600"),
                 (5, str(MAX_ARCHIVE_BYTES)), (6, "clock_failed"),
                 (7, ""), (7, "00"), (7, "7f"), (7, "c280"),
                 (7, "e0a080"), (7, "ed9fbf"), (7, "f0908080"),
                 (7, "f48fbfbf"), (7, "f09f9880" * 120),
                 (8, "999999999999")]
        decoder = TopCapture.__new__(TopCapture)
        for expected_valid, cases in ((False, invalid), (True, valid)):
            for index, value in cases:
                with self.subTest(valid=expected_valid, field=index, value=value):
                    fields = baseline.copy()
                    fields[index] = value
                    state = "\n".join(fields) + "\n"
                    (self.job / "state").write_text(state)
                    if not expected_valid:
                        with self.assertRaises(AdbError):
                            decoder._decode("device-1", state + "102\n")
                    result = self.shell("status")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    decoded = decoder._decode("device-1", result.stdout)
                    self.assertEqual(decoded.get("state_corrupt", False), not expected_valid)
                    self.assertEqual(decoded["running"], not expected_valid)
                    self.assertEqual(decoded["connected"], expected_valid)
                    self.assertEqual(decoded["id"], self.identity)
                    self.assertEqual((self.job / "state").read_text(), state)
                    self.assertEqual((self.job / "raw.txt").read_bytes(), archive)

    def test_state_inspection_failures_never_allow_bypass(self):
        self.orphan()
        (self.job / "state").write_bytes(b"broken")
        archive = (self.job / "raw.txt").read_bytes()
        original_awk = (self.bin / "awk").read_text()
        for kind in ("awk", "wc", "unreadable", "linked"):
            with self.subTest(kind=kind):
                if kind == "awk":
                    self.command("awk", 'case "$*" in */state) exit 2;; *) exec /usr/bin/awk "$@";; esac')
                elif kind == "wc":
                    (self.bin / "awk").write_text(original_awk)
                    self.command("wc", 'n=$(/usr/bin/wc "$@") || exit 2; '
                                 'if [ "$n" -eq 6 ]; then exit 2; fi; echo "$n"')
                elif kind == "unreadable":
                    self.command("wc", 'exec /usr/bin/wc "$@"')
                    (self.job / "state").chmod(0)
                else:
                    (self.job / "state").chmod(0o600)
                    (self.job / "state").rename(self.job / "saved-state")
                    (self.job / "state").symlink_to(self.job / "saved-state")
                self.assertEqual(self.shell("status").stdout.splitlines()[2], "unconfirmed")
                self.assertNotEqual(self.shell("launch", "e" * 32, "1", "1", "").returncode, 0)
                self.assertNotEqual(self.shell("clear", self.identity).returncode, 0)
                self.assertEqual(self.shell("stop", self.identity).returncode, 0)
                self.assertEqual((self.job / "raw.txt").read_bytes(), archive)
                self.assertFalse((self.job / "done").exists())

    def test_recovery_rejects_short_archive_invalid_state_and_symlink(self):
        self.orphan()
        (self.job / "raw.txt").write_bytes(b"short")
        self.assert_pending_receipt(self.shell("status"))
        self.assertEqual((self.job / "raw.txt").read_bytes(), b"short")
        self.assertFalse((self.job / "done").exists())
        self.orphan()
        state = (self.job / "state").read_text()
        (self.job / "state").write_text(state.replace(str(len(RAW)), "-1"))
        self.assertEqual(self.shell("status").stdout.splitlines(),
                         ["corrupt", self.identity, "unconfirmed", "present"])
        (self.job / "state").write_text(state)
        (self.job / "raw.txt").rename(self.job / "safe-archive")
        (self.job / "raw.txt").symlink_to(self.fixture)
        self.assert_pending_receipt(self.shell("status"))
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
        self.prepare_worker()
        self.write_script()
        self.command("readlink", 'echo /dev/pts/1')
        self.reject_worker_entry()
        self.command("readlink", 'case "$1" in /proc/*/fd/[012]) echo /dev/null;; *) exec /usr/bin/readlink "$@";; esac')
        self.prepare_worker(target=-1)
        (self.job / "stop").touch()
        state = self.worker(target=-1)
        self.assertEqual(state[1:3], ["stopped", "0"])
        self.assertFalse((self.job / "sample").exists())


if __name__ == "__main__":
    unittest.main()

