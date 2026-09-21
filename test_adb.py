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
        if script.startswith("for p in /proc"):
            return " ".join(self.processes)
        if script.startswith("nohup ") and not self.fail_start:
            self.processes = ["123"]
        if "kill -TERM" in script and not self.keep_running:
            self.processes = []
        if script.startswith("rm -f -- "):
            if self.fail_delete:
                raise AdbError("permission denied", 502)
            if not self.keep_logs:
                self.remote_logs.clear()
        if script.startswith("for f in "):
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
        self.adb.remote_logs.update(f"perf.{i}.log" for i in range(1, 5))
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
                self.assertEqual(script.split()[3:], ["/log/sys/perf/perf.log"] +
                                 [f"/log/sys/perf/perf.{i}.log" for i in range(1, 5)])
            return original_shell(serial, script, root, output)

        with patch.object(self.adb, "shell", side_effect=checked_shell):
            result = self.adb.delete_logs("unit")
        self.assertEqual(result["pids"], [])
        self.assertEqual(result["properties"]["test"], "0")
        self.assertEqual(self.adb.remote_logs, set())
        self.assertEqual((Path(imported["archive"]) / "perf.log").read_bytes(), SAMPLE)
        self.assertEqual(len(self.store.sessions()), 1)
        commands = [call[1] for call in self.adb.calls if call[0] == "unit"]
        self.assertLess(next(i for i, c in enumerate(commands) if "kill -TERM" in c),
                        next(i for i, c in enumerate(commands) if c.startswith("rm -f")))
        self.adb.delete_logs("unit")  # Empty directory is also a successful cleanup.

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

    def test_subprocess_timeout_missing_and_arguments(self):
        adb = AdbController(self.temp.name, executable="adb")
        with patch("perf_adb.subprocess.run", side_effect=subprocess.TimeoutExpired("adb", 1)):
            with self.assertRaises(AdbError) as error:
                adb.devices()
        self.assertEqual(error.exception.status, 504)
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