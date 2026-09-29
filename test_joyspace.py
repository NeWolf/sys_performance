"""Offline JoySpace regression: no authentication, network or real publishing."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import perf_joyspace as joy


class JoySpaceBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = Path(tempfile.mkdtemp(prefix="jdperf-joyspace-test-"))
        cls.markdown = cls.folder / "report.md"
        cls.markdown.write_text("# report", encoding="utf-8")

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

    def test_read_returns_only_markdown(self):
        with patch.object(joy, "_check_runtime", return_value=(Path("/node"), self.folder)), \
                patch.object(joy, "_invoke", return_value={"content": "# template", "auth": "secret"}) as invoke:
            self.assertEqual(joy.read_template(), "# template")
            self.assertEqual(invoke.call_args.args[1][-2:], ["--url", joy.TEMPLATE_URL])

    def test_publish_mapping_and_allowlisted_result(self):
        for placement in ("personal", "child", "sibling"):
            with self.subTest(placement=placement), \
                    patch.object(joy, "_check_runtime", return_value=(Path("/node"), self.folder)), \
                    patch.object(joy, "_invoke", return_value={"verified": True, "pageId": "new123",
                                      "link": "https://evil", "auth": "secret", "images": ["local/path"]}) as invoke:
                result = joy.publish_markdown(self.markdown, "a ; $(echo test)", placement,
                                              None if placement == "personal" else "parent123")
                self.assertEqual(result, {"pageId": "new123", "link": "https://joyspace.jd.com/pages/new123"})
                args = invoke.call_args.args[1]
                self.assertNotIn("--skip-image-upload", args)
                if placement == "child":
                    self.assertEqual(args[-2:], ["--parent-page-id", "parent123"])
                if placement == "sibling":
                    self.assertEqual(args[-2:], ["--page-url", "https://joyspace.jd.com/pages/parent123"])

    def test_validation_before_subprocess(self):
        with patch.object(joy, "_check_runtime") as check:
            for args in ((self.markdown, "x", "bad"), (self.markdown, "", "personal"),
                         (self.markdown, "x", "child", "https://evil/pages/a"),
                         (self.folder, "x", "personal")):
                with self.assertRaises(joy.JoySpaceError):
                    joy.publish_markdown(*args)
            check.assert_not_called()

    def test_unverified_or_unsafe_id_is_unknown(self):
        for data in ({"pageId": "abc", "verified": False}, {"pageId": "../evil", "verified": True}):
            with patch.object(joy, "_check_runtime", return_value=(Path("/node"), self.folder)), \
                    patch.object(joy, "_invoke", return_value=data), self.assertRaisesRegex(joy.JoySpaceError, "结果未知"):
                joy.publish_markdown(self.markdown, "report")

    def test_errors_never_leak_stdout_stderr_and_do_not_retry(self):
        outcomes = [subprocess.CompletedProcess([], 1, "secret-stdout", "secret-stderr"),
                    subprocess.CompletedProcess([], 0, "secret-invalid-json", "secret-stderr"),
                    subprocess.TimeoutExpired("secret-command", 1, "secret-out", "secret-error"),
                    OSError("secret-path")]
        for outcome in outcomes:
            for publish in (False, True):
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with patch.object(joy.subprocess, "run", **kwargs) as run:
                    with self.assertRaises(joy.JoySpaceError) as raised:
                        joy._invoke(Path("/node"), ["script"], self.folder, timeout=1, publish=publish)
                    self.assertNotIn("secret", str(raised.exception))
                    self.assertEqual(run.call_count, 1)
                    self.assertFalse(run.call_args.kwargs["shell"])
                    if publish:
                        self.assertIn("结果未知", str(raised.exception))
                        self.assertIn("残留", str(raised.exception))

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


class JoySpaceImageTests(unittest.TestCase):
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