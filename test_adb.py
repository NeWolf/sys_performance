import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from perf_adb import AdbController, AdbError, PROPERTIES
from perf_api import create_app
from perf_store import Store

SAMPLE = b"S,1000,1,1,1,0,0,0,100,200,100\n"


class FakeAdb(AdbController):
    def __init__(self, folder):
        super().__init__(folder, executable="adb")
        self.props = dict.fromkeys(PROPERTIES, "0")
        self.processes = []
        self.calls = []
        self.uid = "0"
        self.abi = "arm64-v8a,armeabi-v7a"
        self.fail_start = False
        self.deployed = False
        self.fail_deploy = False
        self.remote_logs = {"perf.log"}
        self.keep_running = False
        self.fail_delete = False
        self.keep_logs = False

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if args == ("devices", "-l"):
            return "List of devices attached\nunit device model:Test\nother unauthorized\noff offline"
        return ""

    def shell(self, serial, script, root=False, output=None):
        self.calls.append((serial, script, root))
        if script == "id -u":
            return "0" if root else self.uid
        if script == "getprop ro.product.cpu.abilist":
            return self.abi
        if script.startswith("getprop persist.sm.perf."):
            return self.props[script.rsplit(".", 1)[1]]
        if script.startswith("setprop "):
            _, key, value = script.split()
            self.props[key.rsplit(".", 1)[1]] = value
        if 'echo sysmonitor-process-scan-v1' in script:
            return "\n".join([f"lrwxrwxrwx 1 0 0 0 /proc/{pid}/exe -> /data/local/tmp/sysmonitor_test"
                              for pid in self.processes] + ["sysmonitor-process-scan-v1"])
        if script.startswith("nohup ") and not self.fail_start:
            self.processes = ["123"]
        if "kill -TERM" in script and not self.keep_running:
            self.processes = []
        if script.startswith("rm -f -- "):
            if self.fail_delete:
                raise AdbError("permission denied", 502)
            if not self.keep_logs:
                targets = {Path(path).name for path in shlex.split(script)[3:]}
                self.remote_logs.difference_update(targets)
        if script == 'if [ -d /log/sys/perf ]; then ls -1A /log/sys/perf; fi':
            return "\n".join(sorted(self.remote_logs))
        if script == 'if [ -d /log/sys/perf ]; then ls -ln /log/sys/perf; fi':
            return "\n".join(sorted(self.remote_logs))
        if script.startswith("chmod 755 ") and not self.fail_deploy:
            self.deployed = True
        if script.startswith("if [ -f ") and "&& [ -x " in script:
            return "yes" if self.deployed else ""
        if script.startswith("if [ -f "):
            return "yes" if any(f"/{name} " in script for name in self.remote_logs) else ""
        if output is not None:
            output.write(SAMPLE)
        return ""


class AdbTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.adb = FakeAdb(Path(self.temp.name) / "logs")
        self.store = Store(Path(self.temp.name) / "test.sqlite3")

    def test_devices_and_root(self):
        self.assertEqual([x["state"] for x in self.adb.devices()], ["device", "unauthorized", "offline"])
        self.assertEqual(self.adb.status("unit")["privilege"], "root adbd")
        self.adb.uid = "2000"
        self.assertEqual(self.adb.status("unit")["privilege"], "su 0")

    def test_reject_serial_offline_and_busy(self):
        for serial in ("unit;id", "-s", "other", "off", "missing"):
            with self.assertRaises(AdbError):
                self.adb.status(serial)
        self.adb.lock.acquire()
        with self.assertRaises(AdbError) as error:
            self.adb.status("unit")
        self.assertEqual(error.exception.status, 409)
        self.adb.lock.release()
        self.adb.status("unit")

    @patch("perf_adb.time.sleep")
    def test_capture_timing_start_stop_pull_and_restart(self, _sleep):
        with patch("perf_adb.time.time", return_value=1700000000), \
                patch("perf_adb.time.monotonic", return_value=100):
            result = self.adb.start("unit", 2)
        self.assertEqual(result["started_at"], 1700000000)
        self.assertEqual(result["elapsed_seconds"], 0)
        with patch("perf_adb.time.monotonic", return_value=145):
            self.assertEqual(self.adb.status("unit")["elapsed_seconds"], 45)
            self.adb.pull("unit", self.store)
        with patch("perf_adb.time.monotonic", return_value=200):
            stopped = self.adb.status("unit")
            self.assertEqual(stopped["elapsed_seconds"], 45)
            self.assertIsNotNone(stopped["ended_at"])
            with patch("perf_adb.time.time", return_value=1700000100):
                restarted = self.adb.start("unit", 2)
            self.assertEqual(restarted["started_at"], 1700000100)
            self.assertEqual(restarted["elapsed_seconds"], 0)

    def test_capture_observation_pid_change_and_disabled_switch(self):
        self.adb.processes = ["2", "1"]
        self.adb.props["test"] = "1"
        with patch("perf_adb.time.monotonic", return_value=100):
            first = self.adb.status("unit")
        self.assertIsNone(first["started_at"])
        self.assertIsNotNone(first["observed_at"])
        self.adb.processes.reverse()
        with patch("perf_adb.time.monotonic", return_value=130):
            self.assertEqual(self.adb.status("unit")["elapsed_seconds"], 30)
            self.adb.processes = ["3"]
            self.assertEqual(self.adb.status("unit")["elapsed_seconds"], 0)
        self.adb.props["test"] = "0"
        with patch("perf_adb.time.monotonic", return_value=140):
            self.assertEqual(self.adb.status("unit")["elapsed_seconds"], 10)
        with patch("perf_adb.time.monotonic", return_value=180):
            self.assertEqual(self.adb.status("unit")["elapsed_seconds"], 10)
            self.adb.props["test"] = "1"
            self.assertEqual(self.adb.status("unit")["elapsed_seconds"], 0)

    def test_capture_timing_failure_preserves_record(self):
        self.adb.processes = ["123"]
        self.adb.props["test"] = "1"
        with patch("perf_adb.time.monotonic", return_value=100):
            self.adb.status("unit")
        before = self.adb.capture_times["unit"].copy()
        for method in ("pids", "properties", "is_deployed"):
            with patch.object(self.adb, method, side_effect=AdbError("unconfirmed", 504)):
                with self.assertRaises(AdbError):
                    self.adb.status("unit")
            self.assertEqual(self.adb.capture_times["unit"], before)
        with patch("perf_adb.time.monotonic", return_value=150):
            self.assertEqual(self.adb.status("unit")["elapsed_seconds"], 50)

    def test_capture_timing_expiry_is_not_extended_by_polling(self):
        with patch("perf_adb.time.monotonic", return_value=100):
            self.adb._capture_timing("unit", ["1"], True, (1700000000, 100))
            self.adb._capture_timing("other", ["1"], True)
            self.adb._capture_timing("other", [], False)
        with patch("perf_adb.time.monotonic", return_value=86499):
            self.assertEqual(self.adb._capture_timing("unit", ["1"], True)["elapsed_seconds"], 86399)
            self.assertIn("other", self.adb.capture_times)
        with patch("perf_adb.time.monotonic", return_value=86501):
            result = self.adb._capture_timing("unit", ["1"], True)
            self.assertIsNone(result["started_at"])
            self.assertEqual(result["elapsed_seconds"], 0)
            self.assertNotIn("other", self.adb.capture_times)
            self.assertIsNone(self.adb._capture_timing("other", [], False)["elapsed_seconds"])
        self.assertEqual(self.adb.calls, [])  # Retention never issues device commands.

    @patch("perf_adb.time.sleep")
    def test_start_stop_and_pull(self, _sleep):
        result = self.adb.start("unit", 2, False, True)
        self.assertEqual(result["pids"], ["123"])
        self.assertEqual(self.adb.props, {"test": "1", "interval": "2", "tofile": "1", "tologcat": "0", "async": "1"})
        result = self.adb.pull("unit", self.store)
        self.assertEqual(self.adb.props["test"], "0")
        self.assertEqual(self.adb.processes, [])
        self.assertEqual(result["files"], ["perf.log"])
        self.assertEqual((Path(result["archive"]) / "perf.log").read_bytes(), SAMPLE)
        self.assertEqual(self.store.overview(result["id"])["summary"]["cycles"], 1)
        self.assertTrue(self.adb.pull("unit", self.store)["duplicate"])
        self.assertFalse(any("pkill" in str(call) for call in self.adb.calls))

    @patch("perf_adb.time.sleep")
    def test_deploy_once_and_reuse_on_restart(self, _sleep):
        self.assertFalse(self.adb.status("unit")["deployed"])
        self.assertTrue(self.adb.start("unit", 1)["deployed"])
        self.assertEqual(sum("push" in call for call in self.adb.calls), 1)
        self.adb.stop("unit")
        self.adb.calls.clear()
        self.adb.binary = Path(self.temp.name) / "missing-binary"
        result = self.adb.start("unit", 3)
        self.assertEqual(result["pids"], ["123"])
        self.assertEqual(result["properties"]["interval"], "3")
        self.assertFalse(any("push" in call or "chmod 755" in str(call) for call in self.adb.calls))

    def test_deploy_failure_does_not_start_collection(self):
        self.adb.fail_deploy = True
        with self.assertRaisesRegex(AdbError, "部署失败"):
            self.adb.start("unit", 1)
        self.assertEqual(self.adb.props["test"], "0")
        self.assertFalse(any("nohup" in str(call) for call in self.adb.calls))

    @patch("perf_adb.time.sleep")
    def test_start_failure_disables_collection(self, _sleep):
        self.adb.fail_start = True
        with self.assertRaises(AdbError):
            self.adb.start("unit", 1)
        self.assertEqual(self.adb.props["test"], "0")

    def test_incompatible_or_existing_collection(self):
        self.adb.abi = "x86_64"
        with self.assertRaises(AdbError):
            self.adb.start("unit", 1)
        self.adb.abi = "arm64-v8a"
        self.adb.props["test"] = "1"
        with self.assertRaises(AdbError):
            self.adb.start("unit", 1)
        self.assertFalse(any("push" in call for call in self.adb.calls))

    @patch("perf_adb.time.sleep")
    def test_pull_auto_stop_before_reading_under_lock(self, _sleep):
        original_shell = self.adb.shell

        def checked_shell(serial, script, root=False, output=None):
            if output is not None:
                self.assertTrue(self.adb.lock.locked())
                self.assertEqual(self.adb.props["test"], "0")
                self.assertEqual(self.adb.processes, [])
            return original_shell(serial, script, root, output)

        for switch, processes in (("1", []), ("0", ["123"]), ("1", ["123"]), ("0", [])):
            with self.subTest(switch=switch, processes=processes):
                self.adb.props["test"] = switch
                self.adb.processes = processes.copy()
                with patch.object(self.adb, "shell", side_effect=checked_shell), \
                        patch.object(self.adb, "_stop", wraps=self.adb._stop) as stop:
                    result = self.adb.pull("unit", self.store)
                self.assertEqual(stop.call_count, int(switch != "0" or bool(processes)))
                self.assertEqual(result["files"], ["perf.log"])
                self.assertEqual(self.adb.remote_logs, {"perf.log"})

    @patch("perf_adb.time.sleep")
    def test_pull_stop_failure_does_not_read_or_import(self, _sleep):
        self.adb.props["test"] = "1"
        self.adb.processes = ["123"]
        self.adb.keep_running = True
        with self.assertRaises(AdbError) as error:
            self.adb.pull("unit", self.store)
        self.assertEqual(error.exception.status, 409)
        with patch.object(self.adb, "setprop", side_effect=AdbError("属性设置未生效", 502)):
            with self.assertRaises(AdbError):
                self.adb.pull("unit", self.store)
        with patch.object(self.adb, "_stop"):
            with self.assertRaisesRegex(AdbError, "未拉取日志"):
                self.adb.pull("unit", self.store)
        self.assertFalse(any("head -c" in str(c) for c in self.adb.calls))
        self.assertFalse(self.adb.archive.exists())
        self.assertEqual(self.store.sessions(), [])
        self.assertFalse(self.adb.lock.locked())

    def test_pull_size_limit_no_import(self):
        with patch("perf_adb.MAX_FILE_BYTES", len(SAMPLE) - 1):
            with self.assertRaises(AdbError) as error:
                self.adb.pull("unit", self.store)
        self.assertEqual(error.exception.status, 413)
        self.assertIn("1 GB", str(error.exception))
        self.assertEqual(self.store.sessions(), [])
        with patch("perf_adb.MAX_FILE_BYTES", len(SAMPLE)):
            result = self.adb.pull("unit", self.store)
        self.assertEqual(result["files"], ["perf.log"])
        self.assertEqual(self.store.overview(result["id"])["summary"]["cycles"], 1)

    @patch("perf_adb.time.sleep")
    def test_delete_logs_stops_first_and_preserves_local_data(self, _sleep):
        imported = self.adb.pull("unit", self.store)
        self.adb.remote_logs.update(f"perf.{i}.log" for i in range(1, 13))
        unrelated = {"notes.txt", "perf.bad.log", "perf.5.log.bak", "perf.1.log;id"}
        self.adb.remote_logs.update(unrelated)
        self.adb._capture_timing("unit", ["123"], True, (1000, 0))
        self.adb.props["test"] = "1"
        self.adb.processes = ["123"]
        self.adb.uid = "2000"
        self.adb.calls.clear()
        original_shell = self.adb.shell

        def checked_shell(serial, script, root=False, output=None):
            if script.startswith("rm -f -- "):
                self.assertTrue(self.adb.lock.locked())
                self.assertEqual(self.adb.props["test"], "0")
                self.assertEqual(self.adb.processes, [])
                self.assertTrue(root)
                self.assertEqual(set(shlex.split(script)[3:]), {"/log/sys/perf/perf.log"} |
                                 {f"/log/sys/perf/perf.{i}.log" for i in range(1, 13)})
            return original_shell(serial, script, root, output)

        with patch.object(self.adb, "shell", side_effect=checked_shell):
            result = self.adb.delete_logs("unit")
        self.assertEqual(result["pids"], [])
        self.assertEqual(result["properties"]["test"], "0")
        self.assertEqual(self.adb.remote_logs, unrelated)
        self.assertEqual(set(result["files"].splitlines()), unrelated)
        self.assertIsNone(result["started_at"])
        self.assertIsNone(result["elapsed_seconds"])
        self.assertEqual((Path(imported["archive"]) / "perf.log").read_bytes(), SAMPLE)
        self.assertEqual(len(self.store.sessions()), 1)
        commands = [call[1] for call in self.adb.calls if call[0] == "unit"]
        self.assertLess(next(i for i, c in enumerate(commands) if "kill -TERM" in c),
                        next(i for i, c in enumerate(commands) if c.startswith("rm -f")))
        self.adb.delete_logs("unit")  # No matching performance logs is also successful.

    def test_pull_includes_higher_rotations_only(self):
        self.adb.remote_logs = {"perf.5.log", "perf.12.log", "notes.txt", "perf.bad.log"}
        result = self.adb.pull("unit", self.store)
        self.assertEqual(set(result["files"]), {"perf.5.log", "perf.12.log"})

    def test_delete_detects_new_rotation_after_deletion(self):
        original_shell = self.adb.shell

        def shell(serial, script, root=False, output=None):
            result = original_shell(serial, script, root, output)
            if script.startswith("rm -f -- "):
                self.adb.remote_logs.add("perf.99.log")
            return result

        with patch.object(self.adb, "shell", side_effect=shell):
            with self.assertRaisesRegex(AdbError, "设备仍有未删除的性能日志"):
                self.adb.delete_logs("unit")

    @patch("perf_adb.time.sleep")
    def test_delete_logs_aborts_on_stop_failure(self, _sleep):
        self.adb.processes = ["123"]
        self.adb.keep_running = True
        with self.assertRaises(AdbError) as error:
            self.adb.delete_logs("unit")
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(any("rm -f" in str(c) for c in self.adb.calls))
        self.assertEqual(self.adb.remote_logs, {"perf.log"})
        self.assertFalse(self.adb.lock.locked())
        with patch.object(self.adb, "setprop", side_effect=AdbError("属性设置未生效", 502)):
            with self.assertRaises(AdbError):
                self.adb.delete_logs("unit")
        self.assertFalse(any("rm -f" in str(c) for c in self.adb.calls))

    def test_delete_logs_detects_failure_or_remaining_files(self):
        for attribute in ("fail_delete", "keep_logs"):
            with self.subTest(attribute=attribute):
                setattr(self.adb, attribute, True)
                with self.assertRaisesRegex(AdbError, "可能已部分删除"):
                    self.adb.delete_logs("unit")
                self.assertEqual(self.adb.props["test"], "0")
                self.assertFalse(self.adb.lock.locked())
                setattr(self.adb, attribute, False)

    def test_delete_logs_rejects_invalid_device_and_busy(self):
        for serial in ("unit;id", "other", "off", "missing"):
            with self.assertRaises(AdbError):
                self.adb.delete_logs(serial)
        with self.adb.lock:
            with self.assertRaises(AdbError) as error:
                self.adb.delete_logs("unit")
            self.assertEqual(error.exception.status, 409)
        self.assertFalse(any("rm -f" in str(c) for c in self.adb.calls))

    def test_process_batch_requires_exact_executable(self):
        rows = [
            "lrwxrwxrwx 1 root root 0 /proc/12/exe -> /data/local/tmp/sysmonitor_test",
            "lrwxrwxrwx 1 root root 0 /proc/13/exe -> /system/bin/sysmonitor",
            "lrwxrwxrwx 1 root root 0 /proc/14/exe -> /data/local/tmp/sysmonitor_test.other",
            "lrwxrwxrwx 1 root root 0 /proc/15/exe -> /data/local/tmp/sysmonitor_test (deleted)",
            "sysmonitor-process-scan-v1",
        ]
        with patch.object(self.adb, "shell", return_value="\n".join(rows)) as shell:
            self.assertEqual(self.adb.pids("unit", True), ["12"])
        self.assertEqual(shell.call_count, 1)
        self.assertTrue(shell.call_args.args[2])
        self.assertNotIn("readlink", shell.call_args.args[1])

    def test_process_batch_retry_and_timeout_never_use_slow_scan(self):
        for value in ("", "unexpected output", AdbError("process disappeared", 502)):
            with self.subTest(value=value), patch.object(self.adb, "shell", side_effect=[
                    value, "lrwx 1 0 0 0 /proc/12/exe -> /data/local/tmp/sysmonitor_test\nsysmonitor-process-scan-v1"
            ]) as shell:
                self.assertEqual(self.adb.pids("unit", False), ["12"])
                self.assertEqual(shell.call_count, 2)
                self.assertTrue(all("readlink" not in call.args[1] for call in shell.call_args_list))
        with patch.object(self.adb, "shell", side_effect=AdbError("timeout", 504)) as shell:
            with self.assertRaisesRegex(AdbError, "批量查询 SysMonitor 进程") as error:
                self.adb.pids("unit", False)
            self.assertEqual(error.exception.status, 504)
            self.assertEqual(shell.call_count, 1)
        for invalid in ("", "bad\nsysmonitor-process-scan-v1", AdbError("permission denied", 502)):
            with self.subTest(invalid=invalid), patch.object(self.adb, "shell", side_effect=[invalid, invalid]):
                with self.assertRaisesRegex(AdbError, "无法确认 SysMonitor 进程状态"):
                    self.adb.pids("unit", False)
        with patch.object(self.adb, "shell", return_value="sysmonitor-process-scan-v1"):
            self.assertEqual(self.adb.pids("unit", False), [])

    @unittest.skipIf(os.name == "nt", "POSIX shell fixture")
    def test_process_batch_script_on_real_symlinks(self):
        proc = Path(self.temp.name) / "proc"
        binary_root = Path(self.temp.name) / "executables"
        for pid, target in ((12, "/data/local/tmp/sysmonitor_test"), (13, "/system/bin/sysmonitor"),
                            (14, "/data/local/tmp/sysmonitor_test.other"), (15, "/missing")):
            folder = proc / str(pid)
            folder.mkdir(parents=True)
            executable = binary_root / target.lstrip("/")
            if pid != 15:
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.touch()
            (folder / "exe").symlink_to(executable)
        (proc / "self").symlink_to(proc / "12")
        calls = []

        def shell(_serial, script, _root):
            calls.append(script)
            local = script.replace("/proc/", shlex.quote(str(proc)) + "/")
            result = subprocess.run(["sh", "-c", local], capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.replace(str(proc) + "/", "/proc/").replace(str(binary_root), "").strip()

        with patch.object(self.adb, "shell", side_effect=shell):
            self.assertEqual(self.adb.pids("unit", True), ["12"])
        self.assertEqual(len(calls), 1)

    def test_status_timeout_identifies_step_and_releases_lock(self):
        for method, expected in (("root_mode", "查询 adbd 权限"),
                                 ("snapshot", "读取性能日志目录"),
                                 ("properties", "读取采集属性 test"),
                                 ("is_deployed", "检查 SysMonitor 部署状态")):
            with self.subTest(method=method), patch.object(self.adb, "shell", side_effect=AdbError("timeout", 504)):
                with self.assertRaisesRegex(AdbError, expected) as error:
                    if method == "root_mode":
                        self.adb.status("unit")
                    elif method == "properties":
                        self.adb.properties("unit")
                    else:
                        getattr(self.adb, method)("unit", True)
                self.assertEqual(error.exception.status, 504)
                self.assertFalse(self.adb.lock.locked())
        with patch.object(self.adb, "shell", side_effect=["2000", AdbError("timeout", 504)]):
            with self.assertRaisesRegex(AdbError, "查询 su 0 权限"):
                self.adb.status("unit")
        self.assertFalse(self.adb.lock.locked())

    def test_subprocess_timeout_missing_and_arguments(self):
        adb = AdbController(self.temp.name, executable="adb")
        with patch("perf_adb.subprocess.run", side_effect=subprocess.TimeoutExpired("adb", 1)):
            with self.assertRaises(AdbError) as error:
                adb.devices()
        self.assertEqual(error.exception.status, 504)
        self.assertIn("查询设备列表", str(error.exception))
        self.assertIn("10 秒", str(error.exception))
        with patch("perf_adb.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"0\r\n", b"")) as run:
            self.assertEqual(adb.shell("unit", "id -u", True), "0")
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["adb", "-s", "unit", "shell", "-T"])
        self.assertFalse(run.call_args.kwargs["shell"])
        adb.executable = None
        with self.assertRaises(AdbError) as error:
            adb.devices()
        self.assertEqual(error.exception.status, 503)

    def test_delete_logs_api_requires_auth_confirmation_and_fixed_paths(self):
        with patch("perf_api.AdbController", return_value=self.adb):
            app = create_app(Path(self.temp.name) / "delete-api.sqlite3")
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            url = "/api/adb/delete-logs"
            body = {"serial": "unit", "confirm": True}
            self.assertEqual(client.post(url, json=body).status_code, 403)
            headers = {"X-Session-Token": client.get("/api/token").json()["token"]}
            for invalid in ({"serial": "unit"}, dict(body, confirm=False),
                            dict(body, path="/log")):
                self.assertEqual(client.post(url, headers=headers, json=invalid).status_code, 422)
            self.assertFalse(any("rm -f" in str(c) for c in self.adb.calls))
            response = client.post(url, headers=headers, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["properties"]["test"], "0")
            self.assertEqual(self.adb.remote_logs, set())
            self.adb.fail_delete = True
            self.adb.remote_logs.add("perf.5.log")
            response = client.post(url, headers=headers, json=body)
            self.assertEqual(response.status_code, 502)
            self.assertIn("可能已部分删除", response.json()["detail"])

    def test_api_validation_auth_and_import(self):
        with patch("perf_api.AdbController", return_value=self.adb):
            app = create_app(Path(self.temp.name) / "api.sqlite3")
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = {"X-Session-Token": client.get("/api/token").json()["token"]}
            self.assertEqual(client.get("/api/adb/devices").status_code, 403)
            self.assertEqual(len(client.get("/api/adb/devices", headers=headers).json()), 3)
            for body in ({"serial": "unit"}, {"serial": "unit", "confirm": False},
                         {"serial": "unit", "confirm": True, "interval": 0},
                         {"serial": "unit", "confirm": True, "interval": "1"},
                         {"serial": "unit", "confirm": True, "path": "/tmp"}):
                self.assertEqual(client.post("/api/adb/start", headers=headers, json=body).status_code, 422)
            with patch("perf_adb.time.sleep"):
                self.assertEqual(client.post("/api/adb/start", headers=headers, json={"serial": "unit", "confirm": True}).status_code, 200)
                response = client.post("/api/adb/pull", headers=headers, json={"serial": "unit", "confirm": True})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.adb.props["test"], "0")
            self.assertEqual(self.adb.processes, [])
            self.assertEqual(len(app.state.store.sessions()), 1)
            response = client.get("/api/adb/status", headers=headers, params={"serial": "unit;id"})
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()