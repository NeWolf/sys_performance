"""Offline JoySpace regression: no authentication, network or real publishing."""
import base64
import struct
import zlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import perf_joyspace as joy


def chart_fixture(*, width=1200, height=400, color=2, pixels=None, tail=b"", extra=b""):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    if pixels is None:
        pixels = (b"\x00" + b"\xff" * width * (4 if color == 6 else 3)) * height
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(pixels) + extra) + chunk(b"IEND", b"") + tail)
    return {"type": "image", "src": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
            "width": 1200, "height": 400, "children": [{"text": ""}]}


def invalid_charts():
    chart = chart_fixture()
    crc = bytearray(base64.b64decode(chart["src"].split(",", 1)[1]))
    crc[-1] ^= 1
    return [chart_fixture(width=1199), chart_fixture(height=399), chart_fixture(color=3),
            chart_fixture(pixels=b"short"), chart_fixture(pixels=b"\x00" * 3000000),
            chart_fixture(pixels=(b"\x05" + b"\xff" * 3600) * 400),
            chart_fixture(tail=b"trailing"), chart_fixture(extra=zlib.compress(b"extra")),
            dict(chart, src="data:image/png;base64," + base64.b64encode(crc).decode("ascii"))]


class JoySpaceBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="jdperf-joyspace-test-"))
        cls.report = cls.folder / "report.json"
        cls.blocks = [{"type": "p", "children": [{"text":"report"}]}]
        cls.report.write_text(json.dumps(cls.blocks), encoding="utf-8")

    def test_chart_bridge_preflight_markers_and_recovery(self):
        path = self.folder / "charts.json"
        original = json.dumps(self.blocks + [chart_fixture()]).encode("utf-8")
        path.write_bytes(original)
        digest = hashlib.sha256(original).hexdigest()
        unsupported = {"status": "unsupported", "reason": "chart-browser-unavailable",
                       "remoteAttempted": False}
        valid = {"verified": True, "chartsVerified": True, "title": "report",
                 "sourceSha256": digest, "pageId": "chart123"}
        with patch.object(joy, "_check_runtime", return_value=(Path("/node"), self.folder)), \
                patch.object(joy, "_recover") as recover:
            with patch.object(joy, "_invoke", return_value=unsupported) as invoke:
                with self.assertRaisesRegex(joy.JoySpaceError, "Chrome"):
                    joy.publish_slate(path, "report", expected_sha256=digest)
                args = invoke.call_args.args[1]
                self.assertIn("--inline-charts", args)
                self.assertNotIn("--image-free", args)
                recover.assert_not_called()
            with patch.object(joy, "_invoke", return_value=valid):
                self.assertTrue(joy.publish_slate(path, "report")["verified"])
            for changes in ({"chartsVerified": False}, {"chartsVerified": 1},
                            {"sourceSha256": "0" * 64}, {"verified": False}):
                recover.reset_mock()
                with patch.object(joy, "_invoke", return_value={**valid, **changes}):
                    joy.publish_slate(path, "report")
                recover.assert_called_once_with("report")
            with patch.object(joy, "_invoke") as invoke:
                with self.assertRaisesRegex(joy.JoySpaceError, "内容已变化"):
                    joy.publish_slate(path, "report", expected_sha256="0" * 64)
                invoke.assert_not_called()
        self.assertEqual(path.read_bytes(), original)

    def test_strict_page_targets(self):
        for value in ("abc_123-xyz", "https://joyspace.jd.com/pages/abc_123-xyz/"):
            self.assertEqual(joy._page_id(value), "abc_123-xyz")
        for value in (None, "", " abc", "https://joyspace.jd.com.evil/pages/a",
                      "https://evil/https://joyspace.jd.com/pages/a", "http://joyspace.jd.com/pages/a",
                      "https://user@joyspace.jd.com/pages/a", "https://joyspace.jd.com:443/pages/a",
                      "https://joyspace.jd.com/documents/a", "https://joyspace.jd.com/pages/a?x=1",
                      "https://joyspace.jd.com/pages/a#x", "https://joyspace.jd.com/pages/%61"):
            with self.subTest(value=value), self.assertRaises(joy.JoySpaceError):
                joy._page_id(value)

    def test_frozen_never_falls_back_to_path(self):
        with patch.object(joy.sys, "frozen", True, create=True), \
                patch.object(joy.sys, "_MEIPASS", str(self.folder), create=True), \
                patch.object(Path, "is_file", lambda p: p.name not in ("node", "node.exe")), \
                patch.object(joy.shutil, "which") as which:
            with self.assertRaisesRegex(joy.JoySpaceError, "内置 Node"):
                joy._runtime()
            which.assert_not_called()

    def test_development_may_find_node(self):
        with patch.object(joy.sys, "frozen", False, create=True), \
                patch.object(Path, "is_file", lambda p: p.name not in ("node", "node.exe")), \
                patch.object(joy.shutil, "which", return_value="/fake/node") as which, \
                patch.object(joy.os, "access", return_value=True):
            node, skill = joy._runtime()
            self.assertEqual(node.name, "node")
            self.assertEqual(skill.name, "joyspace-kit")
            which.assert_called_once_with("node")

    def test_read_returns_only_slate(self):
        response = {"raw": {"content": {"content": self.blocks}}, "auth": "secret"}
        with patch.object(joy, "_check_runtime", return_value=(Path("/node"), self.folder)), \
                patch.object(joy, "_invoke", return_value=response) as invoke:
            self.assertEqual(joy.read_template(), self.blocks)
            self.assertEqual(invoke.call_args.args[1][-3:], ["--url", joy.TEMPLATE_URL, "--raw"])
            invoke.assert_called_once()

    def test_read_falls_back_on_invalid_or_unavailable_template(self):
        valid = {"raw": {"content": {"content": self.blocks}}}
        for first in ({"content": "old markdown"}, {"raw": {"content": {"content": []}}},
                      joy.JoySpaceError("unavailable")):
            with self.subTest(first=first), \
                    patch.object(joy, "_check_runtime", return_value=(Path("/node"), self.folder)), \
                    patch.object(joy, "_invoke", side_effect=[first, valid]) as invoke:
                self.assertEqual(joy.read_template(), self.blocks)
                self.assertEqual([call.args[1][-2] for call in invoke.call_args_list], list(joy.TEMPLATE_URLS))
        with patch.object(joy, "_check_runtime", return_value=(Path("/node"), self.folder)), \
                patch.object(joy, "_invoke", return_value={}) as invoke:
            with self.assertRaises(joy.JoySpaceError):
                joy.read_template()
            self.assertEqual(invoke.call_count, len(joy.TEMPLATE_URLS))

    def test_publish_mapping_and_allowlisted_result(self):
        for placement in ("personal", "child", "sibling"):
            with self.subTest(placement=placement), \
                    patch.object(joy, "_check_runtime", return_value=(Path("/node"), self.folder)), \
                    patch.object(joy, "_invoke", return_value={"verified": True, "pageId": "new123",
                                      "title": "a ; $(echo test)", "imageFree": True,
                                      "sourceSha256": joy.report_sha256(self.report),
                                      "link": "https://evil", "auth": "secret", "images": ["local/path"]}) as invoke:
                result = joy.publish_slate(self.report, "a ; $(echo test)", placement,
                                              None if placement == "personal" else "parent123")
                self.assertEqual(result, {"pageId": "new123", "link": "https://joyspace.jd.com/pages/new123",
                                          "verified": True})
                args = invoke.call_args.args[1]
                self.assertEqual(args[0], str(self.folder / joy.SLATE_SCRIPT))
                self.assertEqual(args[args.index("--file") + 1], str(self.report.resolve()))
                self.assertIn("--image-free", args)
                self.assertEqual(args[args.index("--source-sha256") + 1], joy.report_sha256(self.report))
                if placement == "child":
                    self.assertEqual(args[-2:], ["--parent-page-id", "parent123"])
                if placement == "sibling":
                    self.assertEqual(args[-2:], ["--page-url", "https://joyspace.jd.com/pages/parent123"])

    def test_validation_before_subprocess(self):
        with patch.object(joy, "_check_runtime") as check:
            for args in ((self.report, "x", "bad"), (self.report, "", "personal"),
                         (self.report, "x", "child", "https://evil/pages/a"),
                         (self.folder, "x", "personal")):
                with self.assertRaises(joy.JoySpaceError):
                    joy.publish_slate(*args)
            check.assert_not_called()

    def test_invalid_body_or_hash_never_starts_runtime(self):
        path = self.folder / "invalid.json"
        invalid_trees = [None, {}, [], [{"text": 1}],
                         [{"type": "p", "children": []}],
                         [{"type": "p", "children": [{"text": "x", "url": "https://evil"}]}]]
        for kind in ("image", "file", "embed", "iframe", "unknown"):
            node = {"type": kind, "children": [{"text": "x"}]}
            invalid_trees.extend(([node], [{"type": "p", "children": [node]}]))
        contents = [b'', b' \r\n', b'\xff', b'not-json']
        contents.extend(json.dumps(tree).encode("utf-8") for tree in invalid_trees)
        with patch.object(joy, "_check_runtime") as check, patch.object(joy, "_invoke") as invoke:
            for content in contents:
                path.write_bytes(content)
                with self.subTest(content=content), self.assertRaises(joy.JoySpaceError):
                    joy.publish_slate(path, "report")
            with self.assertRaisesRegex(joy.JoySpaceError, "内容已变化"):
                joy.publish_slate(self.report, "report", expected_sha256="0" * 64)
            check.assert_not_called()
            invoke.assert_not_called()

    def test_unverified_or_unsafe_id_is_unknown(self):
        valid = {"pageId": "abc", "verified": True, "imageFree": True,
                 "title": "report", "sourceSha256": joy.report_sha256(self.report)}
        for changes in ({"verified": False}, {"pageId": "../evil"}, {"title": "other"},
                        {"sourceSha256": "0" * 64}, {"imageFree": False}, {"imageFree": 1}):
            with patch.object(joy, "_check_runtime", return_value=(Path("/node"), self.folder)), \
                    patch.object(joy, "_invoke", return_value={**valid, **changes}), \
                    patch.object(joy, "find_published", return_value=[]) as lookup, \
                    self.assertRaisesRegex(joy.JoySpaceError, "结果未知"):
                try:
                    joy.publish_slate(self.report, "report")
                finally:
                    # A lost confirmation is checked by reading, never by republishing.
                    lookup.assert_called_once_with("report")

    def test_find_published_keeps_only_self_consistent_report_matches(self):
        pages = [{"id": "keep1", "title": " report ", "page_type": 13,
                  "link": "https://joyspace.jd.com/pages/keep1", "created_at": "2026-09-30T10:00:00Z"},
                 {"id": "keep2", "title": "report", "page_type": 13,
                  "link": "https://joyspace.jd.com/pages/keep2", "created_at": "not-a-time"},
                 {"id": "x1", "title": "report", "page_type": 1,
                  "link": "https://joyspace.jd.com/pages/x1", "created_at": None},
                 {"id": "x2", "title": "report draft", "page_type": 13,
                  "link": "https://joyspace.jd.com/pages/x2"},
                 {"id": "x3", "title": "report", "page_type": 13, "link": "https://evil/pages/x3"},
                 {"id": "x4", "title": "report", "page_type": 13,
                  "link": "https://joyspace.jd.com/pages/other4"},
                 "junk"]
        with patch.object(joy, "_check_runtime", return_value=(Path("/node"), self.folder)), \
                patch.object(joy, "_invoke", return_value=pages) as invoke:
            self.assertEqual(joy.find_published("report"),
                             [{"pageId": "keep1", "link": "https://joyspace.jd.com/pages/keep1",
                               "createdAt": "2026-09-30T10:00:00Z"},
                              {"pageId": "keep2", "link": "https://joyspace.jd.com/pages/keep2",
                               "createdAt": None}])
            args = invoke.call_args.args[1]
            self.assertEqual(args[-4:], ["--limit", "100", "--grep", "report"])
            self.assertIn("--json", args)
            self.assertIs(invoke.call_args.kwargs["shape"], list)

    def test_find_published_validates_before_subprocess(self):
        with patch.object(joy, "_check_runtime") as check:
            for args, kwargs in ((("",), {}), ((" ",), {}), (("x" * 201,), {}), (("a\x00b",), {}),
                                 ((None,), {}), (("report",), {"limit": 0}), (("report",), {"limit": 201}),
                                 (("report",), {"limit": True}), (("report",), {"limit": "5"})):
                with self.subTest(args=args, kwargs=kwargs), self.assertRaises(joy.JoySpaceError):
                    joy.find_published(*args, **kwargs)
            check.assert_not_called()

    def test_recovery_reports_only_what_a_lookup_proves(self):
        one = [{"pageId": "p1", "link": "https://joyspace.jd.com/pages/p1", "createdAt": None}]
        two = one + [{"pageId": "p2", "link": "https://joyspace.jd.com/pages/p2", "createdAt": None}]
        with patch.object(joy, "find_published", return_value=one):
            self.assertEqual(joy._recover("report"),
                             {"pageId": "p1", "link": "https://joyspace.jd.com/pages/p1",
                              "createdAt": None, "verified": False, "message": joy.PUBLISH_RECOVERED})
        with patch.object(joy, "find_published", return_value=[]), \
                self.assertRaisesRegex(joy.JoySpaceError, "未发现同名页面"):
            joy._recover("report")
        with patch.object(joy, "find_published", return_value=two) as lookup, \
                self.assertRaises(joy.JoySpaceError) as raised:
            joy._recover("report")
        self.assertIn("发现 2 个同名页面", str(raised.exception))
        self.assertIn("https://joyspace.jd.com/pages/p2", str(raised.exception))
        with patch.object(joy, "find_published", side_effect=joy.JoySpaceError("secret detail")), \
                self.assertRaises(joy.JoySpaceError) as raised:
            joy._recover("report")
        self.assertEqual(str(raised.exception), joy.PUBLISH_UNKNOWN)
        lookup.assert_called_once()

    def test_recovered_link_is_returned_but_never_marked_verified(self):
        with patch.object(joy, "_check_runtime", return_value=(Path("/node"), self.folder)), \
                patch.object(joy, "_invoke", side_effect=joy.JoySpaceError(joy.PUBLISH_UNKNOWN)), \
                patch.object(joy, "find_published", return_value=[
                    {"pageId": "p1", "link": "https://joyspace.jd.com/pages/p1", "createdAt": None}]):
            result = joy.publish_slate(self.report, "report")
        self.assertEqual(result["link"], "https://joyspace.jd.com/pages/p1")
        self.assertIs(result["verified"], False)
        self.assertIn("未经内容校验", result["message"])

    def test_errors_never_leak_stdout_stderr_and_do_not_retry(self):
        outcomes = [subprocess.CompletedProcess([], 1, "secret-stdout", "secret-stderr"),
                    subprocess.CompletedProcess([], 0, "secret-invalid-json", "secret-stderr"),
                    subprocess.TimeoutExpired("secret-command", 1, "secret-out", "secret-error"),
                    OSError("secret-path")]
        for outcome in outcomes:
            for publish in (False, True):
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                target, name = (joy, "_run_publish") if publish else (joy.subprocess, "run")
                with patch.object(target, name, **kwargs) as run, \
                        patch.object(joy.subprocess, "Popen") as popen:
                    with self.assertRaises(joy.JoySpaceError) as raised:
                        joy._invoke(Path("/node"), ["script"], self.folder, timeout=1, publish=publish)
                    self.assertNotIn("secret", str(raised.exception))
                    self.assertEqual(run.call_count, 1)
                    popen.assert_not_called()
                    if publish:
                        run.assert_called_once_with(Path("/node"), ["script"], self.folder,
                                                    timeout=1, probe=False)
                        self.assertIn("结果未知", str(raised.exception))
                        self.assertIn("残留", str(raised.exception))
                    else:
                        self.assertFalse(run.call_args.kwargs["shell"])

    def test_status_only_probes_runtime_without_credentials(self):
        data = {"version": "v22.7.0", "platform": "linux", "arch": "x64"}
        with patch.object(joy, "_runtime", return_value=(Path("/node"), self.folder)), \
                patch.object(joy.platform, "system", return_value="Linux"), \
                patch.object(joy.platform, "machine", return_value="x86_64"), \
                patch.dict(os.environ, {"ME_TOKEN": "secret", "NODE_OPTIONS": "bad", "LD_PRELOAD": "bad"}), \
                patch.object(joy.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(data))) as run:
            result = joy.status()
            self.assertTrue(result["ready"])
            self.assertEqual(set(result), {"ready", "message"})
            self.assertIn("尚未验证认证", result["message"])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][1], "-e")
            for key in ("ME_TOKEN", "NODE_OPTIONS", "LD_PRELOAD"):
                self.assertNotIn(key, run.call_args.kwargs["env"])
            self.assertEqual(run.call_args.kwargs["env"]["PATH"], "")

    def _publish_process_fixture(self):
        # Deliberately retain fixtures, like the existing tests: never delete files.
        folder = Path(tempfile.mkdtemp(prefix="jp-"))
        script = folder / "publisher.py"
        script.write_text(r'''
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

mode = sys.argv[1]
if mode == 'descendant':
    if sys.argv[3] == 'ignore':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with socket.socket(socket.AF_UNIX) as control:
        control.connect(sys.argv[2])
        Path(sys.argv[2] + '.ready').write_text('ready')
        control.sendall(b'ready')
        control.settimeout(0.1)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if control.recv(16) != b'':
                    break
                break
            except socket.timeout:
                pass
    sys.exit(0)
if mode.startswith('child-'):
    subprocess.Popen([sys.executable, __file__, 'descendant', sys.argv[2],
                      'ignore' if mode != 'child-stdout' else 'normal'])
    deadline = time.monotonic() + 5
    while not Path(sys.argv[2] + '.ready').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('descendant startup failed')
        time.sleep(0.01)
if mode in ('timeout', 'child-timeout'):
    time.sleep(15)
if mode == 'child-invalid':
    os.write(1, b'\xffsecret-invalid-utf8')
else:
    print(json.dumps({'ok': True, 'pid': os.getpid(),
                      'pgid': os.getpgrp(), 'sid': os.getsid(0)}), flush=True)
''', encoding="utf-8")
        return folder, script

    def _run_offline_publish(self, mode, *, invoke=False):
        import select
        import signal
        import socket
        import sys
        import time

        folder, script = self._publish_process_fixture()
        address = str(folder / "s")
        connections = []
        leaders = []
        real_popen = subprocess.Popen
        real_wait = joy._wait_publish
        listener = None
        if mode.startswith("child-"):
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(address)
            listener.listen(1)
            listener.settimeout(5)

        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            leaders.append(process)
            return process

        def wait_ready(process, timeout):
            if listener is not None:
                connection, _ = listener.accept()
                connections.append(connection)
                connection.settimeout(5)
                self.assertEqual(connection.recv(5), b"ready")
            return real_wait(process, timeout)

        timeout = 0.2 if mode.endswith("timeout") else 3
        started = time.monotonic()
        try:
            with patch.object(joy.subprocess, "Popen", side_effect=launch) as popen, \
                    patch.object(joy, "_wait_publish", side_effect=wait_ready), \
                    patch.object(joy, "_PUBLISH_GRACE", 0.25), \
                    patch.object(joy.os, "killpg", wraps=os.killpg) as killpg:
                try:
                    runner = joy._invoke if invoke else joy._run_publish
                    kwargs = {"publish": True} if invoke else {}
                    return runner(sys.executable, [str(script), mode, address], folder,
                                  timeout=timeout, probe=True, **kwargs)
                finally:
                    self.assertLess(time.monotonic() - started, 5)
                    self.assertEqual(len(leaders), 1)
                    self.assertIsNotNone(leaders[0].returncode, "leader was not reaped")
                    options = popen.call_args.kwargs
                    self.assertTrue(options["start_new_session"])
                    self.assertFalse(options["shell"])
                    self.assertNotEqual(options["stdout"], subprocess.PIPE)
                    self.assertTrue(options["stdout"].closed)
                    for call in killpg.call_args_list:
                        self.assertEqual(call.args[0], leaders[0].pid)
                        self.assertNotEqual(call.args[0], os.getpgrp())
                    if mode in ("child-ignore", "child-timeout", "child-invalid"):
                        self.assertIn((leaders[0].pid, signal.SIGKILL),
                                      [call.args for call in killpg.call_args_list])
                    for connection in connections:
                        self.assertTrue(select.select([connection], [], [], 1)[0],
                                        "descendant still holds inherited descriptors")
                        self.assertEqual(connection.recv(16), b"", "descendant survived")
        finally:
            # Control channels stop only our own descendants, even after a failed
            # assertion. The script also has a 15-second self-expiry fallback.
            for connection in connections:
                try:
                    connection.sendall(b"quit")
                except OSError:
                    pass
                connection.close()
            if listener is not None:
                listener.close()
            for process in leaders:
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        # Some hosts reject group signalling during session setup;
                        # this Popen handle still identifies only our own child.
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    process.wait(timeout=3)

    @unittest.skipUnless(os.name == "posix", "POSIX private sessions required")
    def test_publish_process_success(self):
        result = self._run_offline_publish("success")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        data = json.loads(result.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["pid"], data["pgid"])
        self.assertEqual(data["pid"], data["sid"])
        self.assertNotEqual(data["pgid"], os.getpgrp())

    @unittest.skipUnless(os.name == "posix", "POSIX private sessions required")
    def test_publish_process_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            self._run_offline_publish("timeout")
        self.assertEqual(raised.exception.timeout, 0.2)

    @unittest.skipUnless(os.name == "posix", "POSIX private sessions required")
    def test_publish_process_term_ignoring_descendant(self):
        result = self._run_offline_publish("child-ignore")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["ok"])
        with self.assertRaises(subprocess.TimeoutExpired):
            self._run_offline_publish("child-timeout")

    @unittest.skipUnless(os.name == "posix", "POSIX private sessions required")
    def test_publish_process_inherited_stdout_does_not_wait_for_eof(self):
        result = self._run_offline_publish("child-stdout")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["ok"])

    @unittest.skipUnless(os.name == "posix", "POSIX private sessions required")
    def test_publish_process_invalid_utf8_cleans_before_error(self):
        with self.assertRaises(UnicodeDecodeError):
            self._run_offline_publish("child-invalid")
        with self.assertRaises(joy.JoySpaceError) as raised:
            self._run_offline_publish("child-invalid", invoke=True)
        self.assertEqual(str(raised.exception), joy.PUBLISH_UNKNOWN)
        self.assertNotIn("secret", str(raised.exception))

    @unittest.skipUnless(os.name == "posix", "POSIX private sessions required")
    def test_publish_process_unrelated_process_survives(self):
        import sys

        # Popen owns this sentinel; never locate processes by executable/name.
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(15)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
            env=joy._environment(probe=True))
        try:
            for mode in ("child-ignore", "child-timeout"):
                with self.subTest(mode=mode):
                    if mode.endswith("timeout"):
                        with self.assertRaises(joy.JoySpaceError) as raised:
                            self._run_offline_publish(mode, invoke=True)
                        self.assertEqual(str(raised.exception), joy.PUBLISH_UNKNOWN)
                    else:
                        self._run_offline_publish(mode)
                    self.assertIsNone(sentinel.poll(), "unrelated process was terminated")
        finally:
            if sentinel.poll() is None:
                sentinel.terminate()
            sentinel.wait(timeout=3)

    def test_publish_process_windows_job_assignment_failure_keeps_gate_closed(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, call

        process = Mock(pid=12345, returncode=None)
        job = Mock()
        job.assign.side_effect = OSError("assignment refused")
        events = Mock()
        events.attach_mock(job, "job")
        events.attach_mock(process, "process")
        # Replace the module reference, not global os.name: tempfile stays native.
        with patch.object(joy, "os", SimpleNamespace(name="nt")), \
                patch.object(joy, "_environment", return_value={}), \
                patch.object(joy, "_WindowsPublishJob", return_value=job), \
                patch.object(joy.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, create=True), \
                patch.object(joy.subprocess, "Popen", return_value=process) as popen, \
                patch.object(joy, "_wait_publish") as wait, \
                patch.object(joy, "_stop_publish_tree") as stop:
            with self.assertRaisesRegex(OSError, "assignment refused"):
                joy._run_publish("offline-node", ["offline-script"], self.folder, timeout=1)
        self.assertEqual(popen.call_args.args[0],
                         ["offline-node", "--input-type=module", "-e",
                          joy._WINDOWS_PUBLISH_GATE, "offline-script"])
        self.assertEqual(popen.call_args.kwargs["creationflags"], 512)
        self.assertNotIn("start_new_session", popen.call_args.kwargs)
        process.stdin.write.assert_not_called()
        process.stdin.flush.assert_not_called()
        wait.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(events.mock_calls, [
            call.job.assign(process), call.process.kill(),
            call.process.wait(timeout=joy._PUBLISH_GRACE), call.job.close(),
            call.process.stdin.close()])
        self.assertTrue(popen.call_args.kwargs["stdout"].closed)


class JoySpaceImageTests(unittest.TestCase):
    def test_generated_png_validation_and_template_rejection(self):
        slate = joy.perf_slate
        for color in (2, 6):
            chart = chart_fixture(color=color)
            self.assertEqual(slate.validate_tree([chart], allow_images=True), [chart])
            with self.assertRaises(slate.SlateError):
                slate.validate_tree([chart])
            self.assertIn(chart["src"], slate.to_html([chart]))
        chart = chart_fixture()
        bad = invalid_charts() + [dict(chart, **change) for change in (
            {"src": "/tmp/a.png"}, {"src": "https://evil/a.png"},
            {"src": "data:image/svg+xml;base64,AAAA"}, {"src": "data:image/png;base64,AAAA"},
            {"src": chart["src"] + " "}, {"width": True}, {"height": 399},
            {"children": [{"text": "not empty"}]}, {"onload": "bad()"},
            {"src": "data:image/png;base64," + "A" * (3 * 1024 * 1024)})]
        for block in bad:
            with self.subTest(keys=list(block)), self.assertRaises(slate.SlateError):
                slate.validate_tree([block], allow_images=True)
        for tree in ([chart] * 3, [{"type": "p", "children": [chart]}]):
            with self.assertRaises(slate.SlateError):
                slate.validate_tree(tree, allow_images=True)

    def test_slate_chart_validation_and_side_effect_free_preflight(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable for offline JS test")
        folder = Path(tempfile.mkdtemp(prefix="jdperf-chart-preflight-"))
        report = folder / "report.json"
        original = json.dumps([{"type": "p", "children": [{"text": "report"}]}, chart_fixture()]).encode("utf-8")
        report.write_bytes(original)
        script = (Path(__file__).parent / "joyspace_assets/joyspace-kit" / joy.SLATE_SCRIPT).resolve()
        code = r'''
import assert from 'node:assert/strict';
const {assertLocalCharts, assertImageFree, parseArgs} = await import(SCRIPT_URL);
const chart = CHART;
assertLocalCharts([chart]);
assert.throws(() => assertImageFree([chart]));
assert.throws(() => parseArgs(['--inline-charts', '--image-free']));
assertLocalCharts([chart, chart]);
for (const invalid of BAD_IMAGES) assert.throws(() => assertLocalCharts([invalid]));
for (const changed of [{src: '/tmp/a.png'}, {src: 'https://evil/a.png'},
    {src: 'data:image/svg+xml;base64,AAAA'}, {src: 'data:image/png;base64,AAAA'},
    {width: 1}, {height: 1}, {title: 'other'}, {extra: 'field'},
    {children: [{text: 'not empty'}]}, {src: chart.src + ' '},
    {src: chart.src.slice(0, -8)}]) {
  assert.throws(() => assertLocalCharts([{...chart, ...changed}]));
}
for (const tree of [[], [chart, chart, chart], [{type: 'p', children: [chart]}],
    [{type: 'p', children: [{text: '', url: '/tmp/a'}]}, chart],
    [{type: 'p', metadata: {type: 'img'}, children: [{text: ''}]}, chart]]) {
  assert.throws(() => assertLocalCharts(tree));
}
let calls = 0;
globalThis.fetch = async () => {calls++; throw new Error('Unexpected network');};
process.on('beforeExit', () => assert.equal(calls, 0));
process.argv = [process.execPath, SCRIPT_PATH, '--file', REPORT_PATH, '--title', 'report',
               MODE, '--source-sha256', DIGEST];
await import(SCRIPT_URL + '?run');
'''
        for mode, digest, success in (("--inline-charts", hashlib.sha256(original).hexdigest(), True),
                                     ("--image-free", hashlib.sha256(original).hexdigest(), False),
                                     ("--inline-charts", "0" * 64, False)):
            source = code
            for key, value in {"SCRIPT_URL": script.as_uri(), "SCRIPT_PATH": str(script),
                               "REPORT_PATH": str(report), "CHART": chart_fixture(),
                               "MODE": mode, "DIGEST": digest, "BAD_IMAGES": invalid_charts()}.items():
                source = source.replace(key, json.dumps(value))
            env = joy._environment(probe=True)
            env.update(HOME=str(folder), USERPROFILE=str(folder),
                       JOYSPACE_BROWSER_PATH=str(folder / "missing-browser"))
            result = subprocess.run([node, "--input-type=module", "-e", source], shell=False,
                                    capture_output=True, text=True, timeout=20, cwd=folder, env=env)
            with self.subTest(mode=mode, success=success):
                self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
                self.assertNotIn("AssertionError", result.stderr)
                if success:
                    self.assertEqual(json.loads(result.stdout), {
                        "status": "unsupported", "reason": "chart-browser-unavailable",
                        "remoteAttempted": False})
                else:
                    self.assertEqual(result.stdout, "")
                self.assertEqual(report.read_bytes(), original)


    def test_chart_publish_single_attempt_pipeline(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        script = (Path(__file__).parent / "joyspace_assets/joyspace-kit" / joy.SLATE_SCRIPT).resolve()
        source = r'''
import assert from 'node:assert/strict';
const {publishCharts, assertRemoteCharts, uploadChart} = await import(SCRIPT_URL);
const chart = CHART;
const p = {type:'p', children:[{text:'Keep manual explanation'}]};
const table = {type:'table', children:[{type:'table-row',children:[
  {type:'table-cell',children:[{type:'p',children:[{text:'42'}]}]}]}]};
const remote = i => ({type:'img',id:'img'+i,
  url:'https://apijoyspace.jd.com/v1/files/chart'+i+'/link',
  width:770,height:257,children:[{text:''}]});
for (const url of ['https://evil/link','https://apijoyspace.jd.com.evil/v1/files/x/link',
  'https://apijoyspace.jd.com/v1/files/x/link?secret=a',undefined]) {
  assert.throws(() => assertRemoteCharts([{...remote(0),url}]));
}
for (const failure of ['', 'create', 'initial', 'upload', 'open', 'insert', 'read', 'refresh', 'final']) {
  const calls = [];
  const hit = name => {calls.push(name); if (failure === name) throw Error(name);};
  let reads = 0;
  const blocks = [p,table,chart,chart,p];
  const original = JSON.stringify(blocks);
  const deps = {
    createPage: async args => {hit('create');assert.deepEqual(args.blocks,[p,table,p]);return {id:'newpage'};},
    verifyPage: async args => {assert.equal(args.pageId,'newpage');
      hit(!args.charts ? 'initial' : ++reads === 1 || failure === 'read' ? 'read' : 'final');
      assert.deepEqual(args.blocks,args.charts ? [p,table,remote(0),remote(1),p] : [p,table,p]);
      return {title:'report',blocks:args.blocks.length};},
    uploadChart: async args => {hit('upload');assert.equal(args.pageId,'newpage');return remote(args.index);},
    delay: async () => {},
  };
  const browser = {
    open: async id => {hit('open');assert.equal(id,'newpage');},
    insertCharts: async (id,initial,final) => {hit('insert');assert.equal(id,'newpage');
      assert.deepEqual(initial,[p,table,p]);assert.deepEqual(final,[p,table,remote(0),remote(1),p]);},
    verifyImages: async () => hit('refresh'),
  };
  const run = () => publishCharts({blocks,title:'report',location:{teamId:'team'},browser},deps);
  if (failure) await assert.rejects(run); else assert.equal((await run()).created.id,'newpage');
  for (const name of ['create','open','insert','refresh']) assert.ok(calls.filter(x=>x===name).length<=1);
  assert.ok(calls.filter(x=>x==='upload').length<=2);
  if (failure === 'read') assert.equal(reads,20);
  if (['create','initial','upload','open'].includes(failure)) assert.ok(!calls.includes('insert'));
  assert.equal(JSON.stringify(blocks),original);
}
// Only the exact, single-leaf template heading is changed; styles and body stay intact.
for (const heading of [
  {type:'p',align:'center',children:[{text:'性能准入模版',bold:true}]},
  p, {type:'p',children:[{text:'性能准入模版 '} ]},
  {type:'p',children:[{text:'性能准入模板'}]},
  {type:'p',children:[{text:'性能准入模版'},{text:'extra'}]},
]) {
  const blocks = [heading,table,chart,p,{type:'p',children:[{text:'性能准入模版'}]}];
  const original = structuredClone(blocks), expected = structuredClone(blocks);
  if (heading.align === 'center') expected[0].children[0].text = 'report';
  expected[2] = remote(0);
  const initial = expected.filter(b=>b.type !== 'img');
  await publishCharts({blocks,title:'report',browser:{
    open:async()=>{},
    insertCharts:async(id,i,f)=>{assert.deepEqual(i,initial);assert.deepEqual(f,expected);},
    verifyImages:async(id,f)=>assert.deepEqual(f,expected),
  }},{
    createPage:async args=>{assert.deepEqual(args.blocks,initial);return {id:'newpage'};},
    verifyPage:async args=>{
      assert.deepEqual(args.blocks,args.charts ? expected : initial);return {title:'report'};
    },
    uploadChart:async()=>remote(0),delay:async()=>{},
  });
  assert.deepEqual(blocks,original);
}
// Abort after any completed stage must prevent the very next stage, including success.
const stages = ['create','initial','upload0','upload1','open','insert','read','refresh','final'];
for (const stage of ['before',...stages,'delay']) {
  const controller = new AbortController(), calls = [];
  const hit = name => { calls.push(name); if (name === stage) controller.abort(); };
  let reads = 0;
  const deps = {
    createPage: async () => {hit('create');return {id:'newpage'};},
    verifyPage: async args => {
      hit(!args.charts ? 'initial' : ++reads === 1 ? 'read' : 'final');
      if (stage === 'delay' && args.charts) throw Error('Not saved yet');
      return {title:'report',blocks:args.blocks.length};
    },
    uploadChart: async args => {hit('upload'+args.index);return remote(args.index);},
    delay: async () => hit('delay'),
  };
  const browser = {
    open: async () => hit('open'),
    insertCharts: async (id,initial,final,title) => {assert.equal(title,'report');hit('insert');},
    verifyImages: async (id,final,title) => {assert.equal(title,'report');hit('refresh');},
  };
  if (stage === 'before') controller.abort();
  await assert.rejects(() => publishCharts({blocks:[p,chart,chart],title:'report',
    browser,signal:controller.signal},deps), {name:'AbortError'});
  assert.deepEqual(calls, stage === 'before' ? [] : stage === 'delay'
    ? [...stages.slice(0,7),'delay'] : stages.slice(0,stages.indexOf(stage)+1));
}
let uploads = 0;
globalThis.fetch = async (url, opts) => {
  uploads++; assert.equal(url,'https://apijoyspace.jd.com/v2/pages/newpage/uploadImage');
  assert.equal(opts.redirect,'error');assert.equal(opts.method,'POST');
  assert.equal(opts.headers['focus-team-id'],'team');
  const file = opts.body.get('file');assert.equal(file.type,'image/png');
  assert.deepEqual(Buffer.from(await file.arrayBuffer()),Buffer.from(chart.src.split(',')[1],'base64'));
  return {ok:true,json:async()=>({status:'success',data:{imgUrl:remote(0).url}})};
};
assert.equal((await uploadChart({pageId:'newpage',chart,index:0,teamHeaderId:'team'})).url,remote(0).url);
assert.equal(uploads,1);
'''.replace("SCRIPT_URL", json.dumps(script.as_uri())).replace("CHART", json.dumps(chart_fixture()))
        result = subprocess.run([node, "--input-type=module", "-e", source],
                                capture_output=True, text=True, timeout=20,
                                env=joy._environment(probe=True))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_chart_editor_preserves_content_and_rejects_conflicts(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        script = (Path(__file__).parent / "joyspace_assets/joyspace-kit/skills" /
                  "markdown-to-joyspace/scripts/chart_browser.mjs").resolve()
        source = r'''
import assert from 'node:assert/strict';
const {editorAction, findBrowser, startBrowser} = await import(SCRIPT_URL);
const p = {type:'p',id:'manual',children:[{text:'Manual note',bold:true}]};
const table = {type:'table',colWidths:[200,300],children:[{type:'table-row',children:[
  {type:'table-cell',colSpan:2,children:[{type:'p',children:[{text:'Value'}]}]}]}]};
const image = {type:'img',url:'https://apijoyspace.jd.com/v1/files/x/link',
  width:770,height:257,children:[{text:''}]};
const initial = [table,p];
const final = [table,image,image,p];
globalThis.location = {origin:'https://joyspace.jd.com',pathname:'/pages/page'};
const title = {type:'p',children:[{text:'re',bold:true},{text:'port'}]};
for (const prefix of [[],[title]]) {
  const children = structuredClone([...prefix,...initial]);
  const ops = [];
  const editor = {children,isVoid:n=>n.type==='img',apply:op=>{
    ops.push(op);children.splice(op.path[0],0,structuredClone(op.node));}};
  const element = {'__reactFiber$test':{memoizedProps:{editor}}};
  globalThis.document = {querySelector:()=>element};
  assert.equal(editorAction({pageId:'page',title:'report',final:initial,action:'check'}),true);
  assert.equal(ops.length,0);
  assert.equal(editorAction({pageId:'page',title:'report',initial,final,action:'insert'}),true);
  assert.deepEqual(children,[...prefix,...final]);
  assert.deepEqual(ops.map(op=>op.path),[[prefix.length+1],[prefix.length+2]]);
  assert.equal(editorAction({pageId:'page',title:'report',final,action:'check'}),true);
  assert.equal(editorAction({pageId:'page',title:'report',initial,final,action:'insert'}),false);
  children.at(-1).children[0].text='Concurrent change';
  assert.equal(editorAction({pageId:'page',title:'report',final,action:'check'}),false);
  assert.equal(ops.length,2);
}
for (const prefix of [[p],[title,title],[{type:'p',children:[]}],
    [{type:'p',children:[{type:'link',text:'report'}]}],
    [{type:'p',children:[{text:'report',children:[]}]}]]) {
  const editor = {children:[...prefix,...initial],apply:()=>assert.fail('Unexpected write')};
  globalThis.document = {querySelector:()=>({'__reactFiber$test':{memoizedProps:{editor}}})};
  assert.equal(editorAction({pageId:'page',title:'report',final:initial,action:'check'}),false);
  assert.equal(editorAction({pageId:'page',title:'report',initial,final,action:'insert'}),false);
}
globalThis.location.pathname='/login';
assert.equal(editorAction({pageId:'page',title:'report',final,action:'check'}),false);
assert.throws(()=>editorAction({pageId:'page',title:'report',initial,final,action:'insert'}));
process.env.JOYSPACE_BROWSER_PATH='not-absolute';
assert.equal(await findBrowser(),null);
await assert.rejects(()=>startBrowser('/nonexistent-jdperf-browser'));
assert.equal(process.listenerCount('SIGTERM'),0);
assert.equal(process.listenerCount('SIGINT'),0);
'''.replace("SCRIPT_URL", json.dumps(script.as_uri()))
        result = subprocess.run([node, "--input-type=module", "-e", source],
                                capture_output=True, text=True, timeout=20,
                                env=joy._environment(probe=True))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_slate_strict_title_prefix_and_content_verification(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        script = (Path(__file__).parent / "joyspace_assets/joyspace-kit" / joy.SLATE_SCRIPT).resolve()
        source = r'''
import assert from 'node:assert/strict';
const {verifyPage} = await import(SCRIPT_URL);
const p = text => ({type:'p',children:[{text}]});
const blocks = [p('Manual explanation'),{type:'table',width:[200],children:[
  {type:'table-row',children:[{type:'table-cell',children:[p('42')]}]}]}];
const image = {type:'img',id:'chart',url:'https://apijoyspace.jd.com/v1/files/chart/link',
  width:770,height:257,children:[{text:''}]};
let data, basic = {title:'report'}, calls = 0;
globalThis.fetch = async (url,options) => {
  calls++;
  let response;
  if (url.endsWith('/v1/pages/content')) {
    assert.equal(options.method,'POST');
    assert.deepEqual(JSON.parse(options.body),{pageId:'page',page_id:'page'});
    response = data;
  } else {
    assert.ok(url.endsWith('/v3/pages/page/basic?sendRecent=0'));
    assert.equal(options.method,'GET');assert.equal(options.body,undefined);
    response = basic;
  }
  return {ok:true,json:async()=>({status:'success',data:response})};
};
for (const charts of [false,true]) {
  const expected = charts ? [...blocks,image] : blocks;
  const run = () => verifyPage({pageId:'page',title:'report',blocks:expected,charts});
  for (const prefix of [[],[p('report')],[{type:'p',children:[{text:'re',bold:true},{text:'port'}]}]]) {
    for (const nested of [false,true]) {
      const stored = structuredClone([...prefix,...expected]);
      stored.at(-1).id = 'server-id';
      data = {pageType:13,content:nested ? {content:stored} : stored};
      assert.deepEqual(await run(),{title:'report',blocks:stored.length});
    }
  }
  for (const title of [undefined,null,'',' ','other','report ',42]) {
    basic = {title};data = {title:'report',content:expected};await assert.rejects(run);
  }
  for (const value of [undefined,null,{}]) {
    basic = value;await assert.rejects(run);
  }
  basic = {name:'report'};data = {pageType:13,content:expected};await run();
  basic = {title:'',name:'report'};await assert.rejects(run);
  basic = {title:'report'};
  for (const prefix of [[p('manual')],[p('')],[p('report'),p('report')],
      [{type:'p',children:[]}],[{type:'p',children:[{text:'report',type:'link'}]}],
      [{type:'p',children:[{text:'report',children:[]}]}]]) {
    data = {pageType:13,content:[...prefix,...expected]};await assert.rejects(run);
  }
  for (const content of [[],expected.slice(1),[...expected,p('extra')],
      [p('changed'),...expected.slice(1)]]) {
    data = {title:'report',content};await assert.rejects(run);
  }
  for (const mutate of [
    value=>{value[1].width=[300];},
    value=>{value[0].children[0].bold=true;},
    value=>{value[1].children[0].children[0].children[0].children[0].text='43';},
    ...(charts ? [value=>{value.at(-1).url='https://apijoyspace.jd.com/v1/files/other/link';},
      value=>{value.pop();}] : []),
  ]) {
    const changed = structuredClone(expected);mutate(changed);
    data = {pageType:13,content:changed};await assert.rejects(run);
  }
}
assert.ok(calls>30);
'''.replace("SCRIPT_URL", json.dumps(script.as_uri()))
        result = subprocess.run([node, "--input-type=module", "-e", source],
                                capture_output=True, text=True, timeout=20,
                                env=joy._environment(probe=True))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inline_chart_cli_success_and_cleanup_abort(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable")
        script = (Path(__file__).parent / "joyspace_assets/joyspace-kit" / joy.SLATE_SCRIPT).resolve()
        folder = Path(tempfile.mkdtemp(prefix="jdperf-chart-cli-"))
        report = folder / "report.json"
        blocks = [
            {"type": "p", "align": "center", "children": [{"text": "性能准入模版", "bold": True}]},
            {"type": "p", "children": [{"text": "保留手工说明：性能准入模版"}]},
            {"type": "table", "width": [200], "children": [{"type": "table-row", "children": [
                {"type": "table-cell", "children": [{"type": "p", "children": [{"text": "42"}]}]}]}]},
            chart_fixture(), chart_fixture(),
        ]
        raw = json.dumps(blocks).encode()
        report.write_bytes(raw)
        source = r'''
import assert from 'node:assert/strict';
import {registerHooks} from 'node:module';
const {editorAction} = await import(BROWSER_URL);
const original = BLOCKS, expected = structuredClone(original.filter(b=>b.type !== 'image'));
expected[0].children[0].text = 'report';
const calls = [], realTimeout = globalThis.setTimeout;
let abortDeadline, stored, title, reads = 0, editor;
globalThis.location = {origin:'https://joyspace.jd.com',pathname:'/pages/formal123'};
globalThis.document = {querySelector:()=>({__reactFiber$offline:{memoizedProps:{editor}}})};
globalThis.setTimeout = (fn,ms,...args) => {
  if (ms === 120000) abortDeadline = fn;
  return realTimeout(fn,ms,...args);
};
globalThis.offlineBrowser = {
  authenticate:async()=>calls.push('auth'),
  open:async()=>calls.push('open'),
  insertCharts:async(id,initial,final,t)=>{
    calls.push('insert');assert.equal(t,'report');assert.deepEqual(initial,expected);
    assert.deepEqual(final.filter(b=>b.type !== 'img'),expected);
    assert.equal(final.filter(b=>b.type === 'img').length,2);
    editor = {children:structuredClone(stored),isVoid:n=>n.type === 'img',
      apply:op=>editor.children.splice(op.path[0],0,op.node)};
    assert.equal(editorAction({pageId:id,title:t,final:initial,action:'check'}),true);
    assert.equal(editorAction({pageId:id,title:t,initial,final,action:'insert'}),true);
    stored = editor.children;
  },
  verifyImages:async(id,final,t)=>{
    calls.push('refresh');editor.children = structuredClone(stored);
    assert.equal(editorAction({pageId:id,title:t,final,action:'check'}),true);
    editor.children[0].children[0].text = '性能准入模版';
    assert.equal(editorAction({pageId:id,title:t,final,action:'check'}),false);
    editor.children = structuredClone(stored);
  },
  close:async()=>{calls.push('close');if (MODE === 'close-abort') abortDeadline();
    if (MODE === 'close-error') throw Error('mock cleanup failure');},
};
registerHooks({resolve(specifier,context,next) {
  if (specifier.endsWith('/chart_browser.mjs')) return {
    url:'data:text/javascript,'+encodeURIComponent(
      'export const findBrowser=async()=>"offline";export const startBrowser=async()=>globalThis.offlineBrowser;'),
    shortCircuit:true};
  return next(specifier,context);
}});
globalThis.fetch = async (url,options) => {
  assert.equal(options.redirect,'error');
  let data;
  if (url.endsWith('/v1/pages')) {
    calls.push('create');const body=JSON.parse(options.body);stored=body.content;title=body.title;
    assert.deepEqual(stored,expected);data={id:'formal123'};
  } else if (url.endsWith('/v3/pages/formal123/basic?sendRecent=0')) {
    assert.equal(options.method,'GET');calls.push('basic');data={title};
  } else if (url.endsWith('/uploadImage')) {
    calls.push('upload');data={imgUrl:'https://apijoyspace.jd.com/v1/files/chart/link'};
  } else if (url.endsWith('/v1/pages/content')) {
    assert.equal(options.method,'POST');
    calls.push('read');reads++;data={pageType:13,content:stored};
    if (MODE === 'final-abort' && reads === 3) abortDeadline();
  } else assert.fail('Unexpected network');
  return {ok:true,json:async()=>({status:'success',data})};
};
process.on('beforeExit',()=>{
  assert.deepEqual(calls,['auth','create','read','basic','upload','upload','open','insert',
    'read','basic','refresh','read',...(MODE === 'final-abort' ? [] : ['basic']),'close']);
  assert.equal(original[0].children[0].text,'性能准入模版');
});
process.argv=[process.execPath,SCRIPT_PATH,'--file',REPORT_PATH,'--title','report',
  '--inline-charts','--source-sha256',DIGEST];
await import(SCRIPT_URL);
'''
        for mode in ("success", "close-abort", "close-error", "final-abort"):
            code = source
            for key, value in {"SCRIPT_PATH": str(script), "SCRIPT_URL": script.as_uri(),
                               "REPORT_PATH": str(report), "MODE": mode, "BLOCKS": blocks,
                               "BROWSER_URL": script.with_name("chart_browser.mjs").as_uri(),
                               "DIGEST": hashlib.sha256(raw).hexdigest()}.items():
                code = code.replace(key, json.dumps(value))
            env = joy._environment(probe=True)
            env.update(HOME=str(folder), USERPROFILE=str(folder), ME_TOKEN="offline-mock-token")
            result = subprocess.run([node, "--input-type=module", "-e", code],
                                    capture_output=True, text=True, timeout=20, cwd=folder, env=env)
            with self.subTest(mode=mode):
                self.assertEqual(result.returncode, 0 if mode == "success" else 1, result.stderr)
                self.assertNotIn("AssertionError", result.stderr)
                if mode == "success":
                    output = json.loads(result.stdout)
                    self.assertTrue(output["verified"])
                    self.assertTrue(output["chartsVerified"])
                    self.assertFalse(output["imageFree"])
                    self.assertEqual(output["sourceSha256"], hashlib.sha256(raw).hexdigest())
                else:
                    self.assertEqual(result.stdout, "")
                    self.assertIn("Publication not confirmed", result.stderr)
                self.assertEqual(report.read_bytes(), raw)

    def test_slate_image_free_publish_remains_verified(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable for offline JS test")
        folder = Path(tempfile.mkdtemp(prefix="jdperf-slate-plain-"))
        report = folder / "report.json"
        blocks = [{"type": "p", "children": [{"text": "CPU 12.5%"}]}]
        raw = json.dumps(blocks).encode("utf-8")
        report.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        script = (Path(__file__).parent / "joyspace_assets/joyspace-kit" / joy.SLATE_SCRIPT).resolve()
        code = r'''
import assert from 'node:assert/strict';
let calls = 0;
const blocks = BLOCKS;
globalThis.fetch = async (url, options) => {
  calls++;
  assert.equal(options.method, url.includes('/basic?') ? 'GET' : 'POST');
  let data;
  if (url.endsWith('/v1/pages')) {
    const body = JSON.parse(options.body);
    assert.deepEqual(body.content, blocks);
    assert.equal(body.contentType, undefined);
    data = {id: 'formal123'};
  } else if (url.endsWith('/v1/pages/content')) {
    data = {pageType: 13, content: [{type: 'p', children: [{text: 'report'}]}, ...blocks]};
  } else if (url.endsWith('/v3/pages/formal123/basic?sendRecent=0')) {
    data = {title: 'report'};
  } else throw new Error('Unexpected network target');
  return {ok: true, json: async () => ({status: 'success', data})};
};
process.on('beforeExit', () => assert.equal(calls, 3));
process.argv = [process.execPath, SCRIPT_PATH, '--file', REPORT_PATH, '--title', 'report',
               '--image-free', '--source-sha256', DIGEST];
await import(SCRIPT_URL);
'''
        for key, value in {"BLOCKS": blocks, "SCRIPT_PATH": str(script), "SCRIPT_URL": script.as_uri(),
                           "REPORT_PATH": str(report), "DIGEST": digest}.items():
            code = code.replace(key, json.dumps(value))
        env = joy._environment(probe=True)
        env.update(HOME=str(folder), USERPROFILE=str(folder), ME_TOKEN="offline-mock-token")
        result = subprocess.run([node, "--input-type=module", "-e", code], shell=False,
                                capture_output=True, text=True, timeout=20, cwd=folder, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = json.loads(result.stdout)
        self.assertIs(result["verified"], True)
        self.assertIs(result["imageFree"], True)
        self.assertEqual(result["sourceSha256"], digest)
        self.assertEqual(report.read_bytes(), raw)

    def test_offline_formal_page_verification(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable for offline JS test")
        folder = Path(tempfile.mkdtemp(prefix="jdperf-verify-test-"))
        script = (Path(__file__).parent / "joyspace_assets/joyspace-kit" / joy.IMPORT_SCRIPT).resolve()
        code = r'''
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
const {verifyJoySpacePage, assertImageFree} = await import(SCRIPT_URL);
const p = text => ({type: 'p', children: [{text}]});
let title = 'report', content = [p('report'), p('CPU 12.5%')], calls = [];
const base = {pageId: 'formal123', title, markdown: '# report\nCPU 12.5%',
              imageFree: true, cookieHeader: 'mock', teamHeaderId: 'root'};
globalThis.fetch = async (url, options) => {
  calls.push([url, options.method]);
  let data;
  if (url.endsWith('/v3/pages/formal123/basic?sendRecent=0')) {
    assert.equal(options.method, 'GET'); data = {title};
  } else if (url.endsWith('/v1/pages/content')) {
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), {pageId: 'formal123'}); data = {content};
  } else { throw new Error('Unexpected network target'); }
  return {ok: true, json: async () => ({status: 'success', data})};
};
const result = await verifyJoySpacePage(base);
assert.equal(result.title, title);
assert.equal(result.sourceSha256, createHash('sha256').update(base.markdown).digest('hex'));
assert.equal(calls.length, 2);
for (const badTitle of ['other', 'report ', '[hermes-upload-scratch] 123']) {
  title = badTitle; await assert.rejects(verifyJoySpacePage(base));
}
await assert.rejects(verifyJoySpacePage({...base, title}));
title = 'report';
for (const badContent of [[], [p('report')], [p('report'), p('CPU 99%')],
    [p('report'), p('CPU 12.5%'), p('extra')], [p('report CPU 12.5%')]]) {
  content = badContent; await assert.rejects(verifyJoySpacePage(base));
}
for (const type of ['img', 'image', 'picture', 'svg', 'diagram']) {
  content = [p('report'), {...p('CPU 12.5%'), metadata: {deep: [{type}]}}];
  await assert.rejects(verifyJoySpacePage(base));
}
calls = [];
for (const options of [{pageId: '../bad'}, {scratchPageId: 'formal123'}]) {
  await assert.rejects(verifyJoySpacePage({...base, ...options}));
}
assert.equal(calls.length, 0);
for (const markdown of ['', '  ', '![x](x)', '<IMG src="x">', '<svg/>', '<image/>']) {
  assert.throws(() => assertImageFree(markdown));
}
const cell = text => ({type: 'table-cell', children: [p(text)]});
const row = values => ({type: 'table-row', children: values.map(cell)});
content = [{type: 'table', children: [row(['metric', 'value']), row(['CPU', '12.5%'])]}];
await verifyJoySpacePage({...base, markdown: '| metric | value |\n| --- | --- |\n| CPU | 12.5% |'});
const expectedTable = '| metric | value |\n| --- | --- |\n| CPU\\|12 | 3 |';
content = [{type: 'table', children: [row(['metric', 'value']), row(['CPU|12', '3'])]}];
await verifyJoySpacePage({...base, markdown: expectedTable});
content = [{type: 'table', children: [row(['metric', 'value']), row(['CPU', '12|3'])]}];
await assert.rejects(verifyJoySpacePage({...base, markdown: expectedTable}));
content = [p('value 1 23')];
await assert.rejects(verifyJoySpacePage({...base, markdown: 'value 12 3'}));
content = [p('value 1 2')];
await verifyJoySpacePage({...base, markdown: 'value 1<br>2'});
await assert.rejects(verifyJoySpacePage({...base, markdown: 'value 12'}));
content = [p('2*3=6')];
await assert.rejects(verifyJoySpacePage({...base, markdown: '23=6'}));
content = [{type: 'p', children: [{text: 'important', bold: true}]}];
await verifyJoySpacePage({...base, markdown: '**important**'});
console.log('offline-verification-ok');
'''.replace("SCRIPT_URL", json.dumps(script.as_uri()))
        env = joy._environment(probe=True)
        env.update(HOME=str(folder), USERPROFILE=str(folder))
        result = subprocess.run([node, "--input-type=module", "-e", code], shell=False,
                                capture_output=True, text=True, timeout=20, cwd=folder, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "offline-verification-ok")

    def test_offline_image_free_cli_and_preflight(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable for offline JS test")
        folder = Path(tempfile.mkdtemp(prefix="jdperf-cli-test-"))
        report = folder / "report.md"
        script = (Path(__file__).parent / "joyspace_assets/joyspace-kit" / joy.IMPORT_SCRIPT).resolve()
        code = r'''
import assert from 'node:assert/strict';
const calls = [];
globalThis.fetch = async (url, options) => {
  calls.push([url, options.method]);
  let data;
  if (url.endsWith('/v1/pages')) {
    assert.equal(options.method, 'POST');
    const payload = JSON.parse(options.body);
    assert.equal(payload.title, 'report');
    assert.equal(payload.page_type, 13);
    assert.equal(payload.contentType, 'markdown');
    assert.deepEqual(payload.content, [{value: EXPECTED_MARKDOWN}]);
    data = {id: 'formal123'};
  } else if (url.endsWith('/v3/pages/formal123/basic?sendRecent=0')) {
    data = {title: 'report'};
  } else if (url.endsWith('/v1/pages/content')) {
    data = {content: ['report', 'CPU 12.5%'].map(text => ({type: 'p', children: [{text}]}))};
  } else { throw new Error('Unexpected network target'); }
  return {ok: true, json: async () => ({status: 'success', data})};
};
process.on('beforeExit', () => {
  assert.equal(calls.length, EXPECTED_CALLS);
});
process.argv = [process.execPath, SCRIPT_PATH, '--file', REPORT_PATH, '--title', 'report',
                '--image-free', '--source-sha256', EXPECTED_HASH];
await import(SCRIPT_URL);
'''
        for newline in ("\n", "\r\n"):
            for scenario in ("success", "changed", "image"):
                markdown = f"# report{newline}CPU 12.5%"
                # Write exact bytes so both newline formats are tested on every OS.
                report.write_bytes(markdown.encode("utf-8"))
                digest = hashlib.sha256(report.read_bytes()).hexdigest()
                if scenario == "changed":
                    report.write_text("changed", encoding="utf-8")
                elif scenario == "image":
                    report.write_text("![x](x.png)", encoding="utf-8")
                source = (code.replace("EXPECTED_CALLS", "3" if scenario == "success" else "0")
                          .replace("EXPECTED_MARKDOWN", json.dumps(markdown))
                          .replace("SCRIPT_PATH", json.dumps(str(script)))
                          .replace("SCRIPT_URL", json.dumps(script.as_uri()))
                          .replace("REPORT_PATH", json.dumps(str(report)))
                          .replace("EXPECTED_HASH", json.dumps(digest)))
                env = joy._environment(probe=True)
                env.update(HOME=str(folder), USERPROFILE=str(folder))
                if scenario == "success":
                    env["ME_TOKEN"] = "offline-mock-token"
                result = subprocess.run([node, "--input-type=module", "-e", source], shell=False,
                                        capture_output=True, text=True, timeout=20, cwd=folder, env=env)
                with self.subTest(newline=repr(newline), scenario=scenario):
                    if scenario == "success":
                        self.assertEqual(result.returncode, 0, result.stderr)
                        data = json.loads(result.stdout)
                        self.assertTrue(data["verified"])
                        self.assertTrue(data["imageFree"])
                        self.assertEqual(data["pageId"], "formal123")
                        self.assertEqual(data["sourceSha256"], digest)
                        self.assertEqual(data["images"], {"skipped": True})
                    else:
                        self.assertEqual(result.returncode, 1, result.stderr)
                        self.assertEqual(result.stdout, "")
                        expected = "Report changed after review" if scenario == "changed" else "Image-free report required"
                        self.assertIn(expected, result.stderr)
                        self.assertNotIn("AssertionError", result.stderr)

    def test_offline_image_preprocessing(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node unavailable for offline JS test")
        folder = Path(tempfile.mkdtemp(prefix="jdperf-images-test-"))
        (folder / "image.png").write_bytes(b"offline image fixture")
        script = (Path(__file__).parent / "joyspace_assets/joyspace-kit" / joy.IMPORT_SCRIPT).resolve()
        code = r'''
import assert from 'node:assert/strict';
const {preprocessLocalImages} = await import(SCRIPT_URL);
const base = {markdownFilePath: REPORT_PATH, cookieHeader: 'mock', teamHeaderId: 'root'};
let calls = [];
let failure = '';
globalThis.fetch = async (url, options) => {
  calls.push([url, options.method]);
  if (options.method === 'DELETE') {
    if (failure === 'cleanup') throw new Error('cleanup failure');
    return {ok: failure !== 'cleanup-http'};
  }
  if (url.endsWith('/uploadImage')) {
    return {ok: failure !== 'upload', status: 200,
      text: async () => JSON.stringify({status: 'success', data: {
        imgUrl: failure === 'unsafe-url' ? 'javascript:bad' : 'https://cdn.example/image.png'}})};
  }
  if (url.endsWith('/v1/pages')) return {ok: true,
    json: async () => ({status: 'success', data: {id: 'scratch'}})};
  throw new Error('Unexpected network target');
};
const result = await preprocessLocalImages({...base,
  markdown: '![a](image.png)\n![b](./image.png "title")\n![c](%69mage.png)'});
assert.equal(result.imageReport.uploaded, 1);
assert.equal((result.markdown.match(/https:\/\/cdn.example\/image.png/g) || []).length, 3);
assert.equal(calls.length, 3);
assert.equal(calls.at(-1)[1], 'DELETE');
for (const markdown of ['![x](missing.png)', '![x][ref]', '<img src="image.png">',
                        '![x](data:image/png;base64,AA)', '![x](a b.png)', '![x](a(b).png)']) {
  calls = [];
  await assert.rejects(preprocessLocalImages({...base, markdown}));
  assert.equal(calls.length, 0);
}
for (failure of ['upload', 'cleanup', 'cleanup-http', 'unsafe-url']) {
  calls = [];
  await assert.rejects(preprocessLocalImages({...base, markdown: '![x](image.png)'}));
  assert.equal(calls.at(-1)[1], 'DELETE');
}
console.log('offline-images-ok');
'''.replace("SCRIPT_URL", json.dumps(script.as_uri())).replace("REPORT_PATH", json.dumps(str(folder / "report.md")))
        env = joy._environment(probe=True)
        env.update(HOME=str(folder), USERPROFILE=str(folder))
        result = subprocess.run([node, "--input-type=module", "-e", code], shell=False,
                                capture_output=True, text=True, timeout=20, cwd=folder, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "offline-images-ok")


if __name__ == "__main__":
    unittest.main()