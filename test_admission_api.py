"""API contract tests: mock external I/O; retain isolated temporary journals."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import perf_api
import perf_admission_jobs as jobs

S = "a" * 32
D = "b" * 32
D2 = "c" * 32
BASE = f"/api/sessions/{S}/admission/drafts"
CREATE = {"draft_id": D, "title": "准入报告", "scenes": {"0": "unknown", "1": "foreground"}, "factor": None}
PUBLISH = {"placement": "personal", "parent_page": None, "confirmed": True}


class AdmissionApiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="admission-api-test-"))
        self.patches = ExitStack()
        self.clients = ExitStack()
        self.addCleanup(self.patches.close)
        self.addCleanup(self.clients.close)
        self.store = self.patches.enter_context(patch.object(perf_api, "Store"))
        self.patches.enter_context(patch.object(perf_api, "AdbController"))
        self.top_factory = self.patches.enter_context(patch.object(perf_api, "TopCapture", side_effect=lambda *a: Mock()))
        self.template = [{"type": "p", "children": [{"text": "mock template"}]}]
        self.blocks = [{"type": "p", "children": [{"text": "mock preview"}]}]
        self.reader = self.patches.enter_context(patch.object(jobs, "read_template", return_value=self.template))
        self.builder = self.patches.enter_context(patch.object(jobs, "build_admission", side_effect=self.build))
        self.publisher = self.patches.enter_context(patch.object(jobs, "publish_slate", return_value={"pageId": "MockPage", "verified": True}))
        self.app, self.client = self.open()

    def build(self, store, session, template, directory, scenes, factor):
        self.assertIs(store, self.store.return_value)
        self.assertEqual(template, self.template)
        self.assertEqual(session, S)
        directory.joinpath("report.json").write_text(json.dumps(self.blocks), encoding="utf-8")
        return {"blocks": self.blocks, "html": "<p>untrusted alternate preview</p><script>bad()</script>", "warnings": []}

    def open(self, name="business.sqlite3", lifespan=True):
        app = perf_api.create_app(self.root / name)
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        if lifespan:
            self.clients.enter_context(client)
        else:
            self.clients.callback(client.close)
            self.clients.callback(app.state.close_services)
        token = client.get("/api/token").json()["token"]
        client.headers["X-Session-Token"] = token
        return app, client

    def wait(self, state="ready", draft=D, client=None):
        client = client or self.client
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get(f"{BASE}/{draft}")
            self.assertEqual(response.status_code, 200, response.text)
            if response.json()["state"] == state:
                return response.json()
            time.sleep(0.01)
        self.fail(response.text)

    def assert_error(self, response, status, rejected=True):
        self.assertEqual(response.status_code, status, response.text)
        self.assertIsInstance(response.json()["detail"], str)
        self.assertNotIn("SECRET", response.text)
        if rejected:
            self.assertIs(response.json().get("accepted"), False)
        else:
            self.assertNotIn("accepted", response.json())

    def ready(self):
        response = self.client.post(BASE, json=CREATE)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "generating")
        return self.wait()

    def test_five_routes_idempotency_and_html(self):
        self.assertEqual(self.client.get(BASE).json(), {"drafts": []})
        self.ready()
        self.assertEqual(self.builder.call_args.args[4], {0: "unknown", 1: "foreground"})
        self.assertEqual(self.client.post(BASE, json=CREATE).json()["state"], "ready")
        self.assertEqual(len(self.client.get(BASE).json()["drafts"]), 1)
        preview = self.client.get(f"{BASE}/{D}/preview")
        self.assertEqual(preview.status_code, 200)
        self.assertIn("text/html", preview.headers["content-type"])
        self.assertIn("script-src 'none'", preview.headers["content-security-policy"])
        self.assertIn("mock preview", preview.text)
        self.assertNotIn("bad()", preview.text)
        self.assertNotIn("untrusted alternate preview", preview.text)
        self.assertEqual(preview.headers["cache-control"], "no-store")
        self.assert_error(self.client.post(BASE, json=dict(CREATE, title="other")), 409)
        self.assertEqual(self.client.post(f"{BASE}/{D}/publish", json=PUBLISH).json()["state"], "publishing")
        value = self.wait("published")
        self.assertEqual(value["link"], "https://joyspace.jd.com/pages/MockPage")
        self.assertEqual(self.reader.call_count, 1)
        self.assertEqual(self.builder.call_count, 1)
        self.assertEqual(self.publisher.call_count, 1)
        # The same data may be published again, to another location if wanted.
        republish = self.client.post(f"{BASE}/{D}/publish",
                                     json=dict(PUBLISH, placement="child", parent_page="https://joyspace.jd.com/pages/Other"))
        self.assertEqual(republish.status_code, 200, republish.text)
        self.assertEqual(republish.json()["state"], "publishing")
        self.wait("published")
        self.assertEqual(self.publisher.call_args.args[2:], ("child", "Other"))
        self.assertEqual(self.builder.call_count, 1)
        self.assertEqual(self.publisher.call_count, 2)
        self.assert_error(self.client.get(BASE.replace(S, "d" * 32) + f"/{D}"), 404, False)

    def test_auth_all_routes_before_lazy_initialization(self):
        for method, path, body in [("GET", BASE, None), ("POST", BASE, CREATE),
                                   ("GET", f"{BASE}/{D}", None), ("GET", f"{BASE}/{D}/preview", None),
                                   ("POST", f"{BASE}/{D}/publish", PUBLISH)]:
            with self.subTest(method=method, path=path):
                self.assert_error(self.client.request(method, path, json=body, headers={"X-Session-Token": "wrong"}), 403)
        for headers in ({"Origin": "https://untrusted.invalid"}, {"Host": "untrusted.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
            self.assert_error(self.client.post(BASE, json=CREATE, headers=headers), 403)
        self.assertIsNone(self.app.state.admission_jobs)
        self.reader.assert_not_called()

    def test_strict_create_json_validation(self):
        invalid = [("draft_id", x) for x in (D.upper(), D + "\n", "x", 123)]
        invalid += [("title", x) for x in ("", " ", "x\x00", "x" * 201, 123, None)]
        invalid += [("factor", x) for x in (True, False, "1.5", 0, -1, [], {})]
        invalid += [("scenes", x) for x in ([], {"01": "unknown"}, {"-0": "unknown"}, {"+1": "unknown"}, {"1.0": "unknown"}, {" 1": "unknown"}, {"１": "unknown"}, {"-1": "unknown"}, {"0": True}, {"0": "auto"}, {"1": "unknown", "01": "foreground"})]
        invalid += [("extra", "SECRET")]
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                self.assert_error(self.client.post(BASE, json=dict(CREATE, **{key: value})), 422)
        for raw in ('{', '[]', 'null', '{"draft_id":"x","draft_id":"y"}',
                    '{"scenes":{"1":"unknown","1":"foreground"}}',
                    '{"factor":NaN}', '{"factor":Infinity}', '{"factor":1e999}'):
            self.assert_error(self.client.post(BASE, content=raw, headers={"Content-Type": "application/json"}), 422)
        self.assert_error(self.client.post(BASE, content='{}'), 422)
        self.assertIsNone(self.app.state.admission_jobs)
        response = self.client.post(BASE, json=dict(CREATE, title="中" * 200, factor=2))
        self.assertEqual(response.status_code, 200, response.text)
        self.wait()
        self.assertEqual(self.builder.call_args.args[5], 2)

    def test_create_without_scenes_passes_none_to_generator(self):
        response = self.client.post(BASE, json={"draft_id": D, "title": "自动场景"})
        self.assertEqual(response.status_code, 200, response.text)
        self.wait()
        self.assertEqual(self.builder.call_args.args[4:6], (None, None))

    def test_publish_validation_and_paths(self):
        for field, values in {"confirmed": [False, 1, 0, "true", None], "placement": ["", "auto", 1], "parent_page": [1, True], "extra": ["SECRET"]}.items():
            for value in values:
                self.assert_error(self.client.post(f"{BASE}/{D}/publish", json=dict(PUBLISH, **{field: value})), 422)
        self.assert_error(self.client.post(f"{BASE}/{D}/publish", json={"placement": "personal"}), 422)
        for bad in (S.upper(), "z" * 32, "abc"):
            self.assert_error(self.client.get(BASE.replace(S, bad)), 422)
            self.assert_error(self.client.get(f"{BASE}/{bad}"), 422)
        self.assertIsNone(self.app.state.admission_jobs)
        self.ready()
        for body in (dict(PUBLISH, parent_page="SECRET"), dict(PUBLISH, placement="child", parent_page="https://bad.invalid/SECRET")):
            self.assert_error(self.client.post(f"{BASE}/{D}/publish", json=body), 400)
        self.publisher.assert_not_called()

    def test_lazy_without_lifespan_and_database_isolation(self):
        first, first_client = self.open(lifespan=False)
        second, second_client = self.open(lifespan=False)
        self.assertIsNone(first.state.admission_jobs)
        self.assertIsNone(second.state.admission_jobs)
        self.assertFalse((self.root / "admission_jobs").exists())
        self.ready()
        self.assert_error(first_client.get(BASE), 503, False)
        other, other_client = self.open("other.sqlite3")
        self.assertEqual(other_client.get(BASE).json(), {"drafts": []})
        self.assertNotEqual(self.app.state.admission_jobs._root, other.state.admission_jobs._root)
        self.assertEqual(other_client.post(BASE, json=CREATE).status_code, 200)
        self.wait(client=other_client)
        self.app.state.close_services()
        self.assertEqual(first_client.get(f"{BASE}/{D}").json()["state"], "ready")
        self.assertIsNone(second.state.admission_jobs)

    def test_restart_restores_unknown_failed_without_remote_retry(self):
        self.ready()
        self.assertEqual(self.client.post(BASE, json=dict(CREATE, draft_id=D2)).status_code, 200)
        self.wait(draft=D2)
        manager = self.app.state.admission_jobs
        with manager._mutex:
            manager._persist(dict(manager._records[D], state="publishing",
                                  publish_request={"placement": "personal", "parent_page": None}))
            manager._persist(dict(manager._records[D2], state="generating"))
        # Model the journal left by process interruption, not a real remote call.
        self.app.state.close_services()
        app, client = self.open()
        self.assertEqual(client.get(f"{BASE}/{D}").json()["state"], "unknown")
        self.assertEqual(client.get(f"{BASE}/{D2}").json()["state"], "failed")
        self.assertEqual(len(client.get(BASE).json()["drafts"]), 2)
        # Unknown or failed drafts no longer freeze the session: generating again is allowed.
        self.assertEqual(client.post(BASE, json=dict(CREATE, draft_id="d" * 32)).json()["state"], "generating")
        self.wait(draft="d" * 32, client=client)
        self.assertEqual(client.get(f"{BASE}/{D}/preview").status_code, 200)
        self.assertEqual(self.builder.call_count, 3)
        self.publisher.assert_not_called()

    def test_queue_full_is_definitely_not_accepted(self):
        with patch.object(jobs, "_CAPACITY", Mock(acquire=Mock(return_value=False))):
            self.assert_error(self.client.post(BASE, json=CREATE), 429)
        self.assertEqual(self.client.get(BASE).json(), {"drafts": []})
        self.ready()
        with patch.object(jobs, "_CAPACITY", Mock(acquire=Mock(return_value=False))):
            self.assert_error(self.client.post(f"{BASE}/{D}/publish", json=PUBLISH), 429)
        self.assertEqual(self.client.get(f"{BASE}/{D}").json()["state"], "ready")
        self.assertNotIn("publish_request", self.app.state.admission_jobs._records[D])
        self.publisher.assert_not_called()

    def test_ambiguous_persistence_and_submit_errors_never_unlock(self):
        self.client.get(BASE)
        manager = self.app.state.admission_jobs
        original = manager._persist
        def committed_then_failed(record):
            original(record)
            raise jobs.AdmissionJobError("SECRET /private/path https://bad.invalid/token", 503)
        with patch.object(manager, "_persist", side_effect=committed_then_failed):
            self.assert_error(self.client.post(BASE, json=CREATE), 503, False)
        self.assertEqual(self.client.get(f"{BASE}/{D}").json()["state"], "generating")
        self.assertEqual(self.client.post(BASE, json=dict(CREATE, draft_id=D2)).status_code, 200)
        self.wait(draft=D2)
        with patch.object(manager._executor, "submit", side_effect=RuntimeError("SECRET")):
            self.assert_error(self.client.post(f"{BASE}/{D2}/publish", json=PUBLISH), 503, False)
        self.assertIn("publish_request", manager._records[D2])
        self.publisher.assert_not_called()

    def test_published_restart_reuses_artifacts_and_allows_republish(self):
        self.ready()
        self.client.post(f"{BASE}/{D}/publish", json=PUBLISH)
        published = self.wait("published")
        self.app.state.close_services()
        app, client = self.open()
        self.assertEqual(client.get(f"{BASE}/{D}").json(), published)
        self.assertEqual(client.post(BASE, json=CREATE).json(), published)
        # A restart alone never republishes; only an explicit new request does.
        self.assertEqual(self.publisher.call_count, 1)
        self.assertEqual(client.post(f"{BASE}/{D}/publish", json=PUBLISH).json()["state"], "publishing")
        self.wait("published", client=client)
        self.assertEqual(self.publisher.call_count, 2)
        self.assertEqual(self.reader.call_count, 1)

    def test_close_waits_for_worker_then_releases_journal(self):
        entered, release, closing, closed = (threading.Event() for _ in range(4))
        failures = []
        def blocked_build(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("mock worker timed out")
            return self.build(*args)
        self.builder.side_effect = blocked_build
        self.assertEqual(self.client.post(BASE, json=CREATE).status_code, 200)
        self.assertTrue(entered.wait(2))
        manager = self.app.state.admission_jobs
        original = manager.close
        def observed_close():
            closing.set()
            original()
        def shutdown():
            try:
                self.app.state.close_services()
            except BaseException as exc:
                failures.append(exc)
            finally:
                closed.set()
        with patch.object(manager, "close", side_effect=observed_close):
            thread = threading.Thread(target=shutdown)
            thread.start()
            try:
                self.assertTrue(closing.wait(2))
                self.assertFalse(closed.wait(0.05))
                self.app.state.top.close.assert_not_called()
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.app.state.top.close.assert_called_once()
        app, client = self.open()
        self.assertEqual(client.get(f"{BASE}/{D}").json()["state"], "ready")

    def test_top_closes_even_if_jobs_close_fails(self):
        self.client.get(BASE)
        manager = self.app.state.admission_jobs
        with patch.object(manager, "close", side_effect=RuntimeError("mock close failure")):
            with self.assertRaises(RuntimeError):
                self.app.state.close_services()
        self.app.state.top.close.assert_called_once()
        self.assert_error(self.client.get(BASE), 503, False)
        manager.close()

    def test_definite_service_errors_and_body_limits(self):
        self.client.get(BASE)
        manager = self.app.state.admission_jobs
        with patch.object(manager, "create", side_effect=jobs.AdmissionJobError("SECRET", 400)):
            self.assert_error(self.client.post(BASE, json=CREATE), 400)
        self.assert_error(self.client.post(f"{BASE}/{D}/publish", json=PUBLISH), 404)
        self.assert_error(self.client.post(BASE, content=b"\xff", headers={"Content-Type": "application/json"}), 422)
        self.assert_error(self.client.post(BASE, json=CREATE, headers={"Content-Length": "invalid"}), 400)
        with patch.object(perf_api, "MAX_BODY_BYTES", 32):
            self.assert_error(self.client.post(BASE, json=CREATE), 413)
            self.assert_error(self.client.post(BASE, json=CREATE, headers={"Content-Length": "0"}), 413)
        self.reader.assert_not_called()
        self.publisher.assert_not_called()