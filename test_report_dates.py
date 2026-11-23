"""Observed local-date selection, raw statistics, cache and API regressions."""
from concurrent.futures import CancelledError
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import perf_api
import perf_admission_jobs as jobs
import perf_qnx_report as qnx
import perf_report
from perf_admission import build_admission
from perf_report_data import build_report_data
from perf_report_dates import local_date, normalize_dates, report_dates
from perf_store import Store
from test_admission import p, s, template
from test_qnx_parser import row


def stamp(day, clock="00:00:00"):
    return int(datetime.fromisoformat(day + "T" + clock).timestamp() * 1000)


class ReportDatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "dates.sqlite3")
        self.days = ["2026-11-01", "2026-11-02", "2026-11-04"]
        self.t1 = stamp(self.days[0], "23:59:59")
        self.t2 = stamp(self.days[1])
        self.t3 = stamp(self.days[2])
        text = s(self.t1, 90) + p(self.t1, pid=99, cpu1c=90)
        text += s(self.t2, 0) + p(self.t2, pid=2, cpu1c=0)
        text += s(self.t2 + 1000, 20) + p(self.t2 + 1000, pid=2, cpu1c=20)
        text += s(self.t2 + 2000, 40) + p(self.t2 + 2000, pid=2, cpu1c=40)
        text += s(self.t2 + 3000, 60)
        text += s(self.t3, 100) + p(self.t3, pid=3, cpu1c=100)
        self.sid = self.load(text)
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.cpu1c',NULL) WHERE session=? AND ts=? AND kind='P'",
                       (self.sid, self.t2 + 2000))

    def load(self, text, name="dates.log"):
        return self.store.import_files([(name, io.BytesIO(text.encode()))])["id"]

    def dates(self, sid=None):
        with self.store.connect() as db:
            return report_dates(db, sid or self.sid)

    def test_observed_days_not_segment_min_max_range(self):
        result = self.dates()
        self.assertEqual([d["date"] for d in result["dates"]], self.days)
        self.assertEqual([d["records"] for d in result["dates"]], [2, 7, 2])
        self.assertEqual(result["undated_records"], 0)
        middle = result["dates"][1]["segments"]
        self.assertEqual(middle, [dict(segment=0, start=self.t2, end=self.t2 + 3000, records=7)])
        with self.assertRaises(KeyError):
            self.dates("missing")

    def test_invalid_timestamps_are_undated(self):
        for value in (None, "invalid", float("inf"), -float("inf"), 1e300):
            with self.subTest(value=value), self.store.connect() as db:
                db.execute("UPDATE records SET ts=? WHERE session=? AND id=(SELECT MIN(id) FROM records)",
                           (value, self.sid))
            result = self.dates()
            self.assertEqual(result["undated_records"], 1)
            self.assertEqual(sum(d["records"] for d in result["dates"]), 10)
        for value in (None, True, "123", float("nan"), float("inf"), 10 ** 500):
            self.assertIsNone(local_date(value))

    def test_strict_selection_validation(self):
        self.assertIsNone(normalize_dates(None))
        self.assertEqual(normalize_dates(self.days[::-1]), self.days)
        for dates in ([], "2026-11-01", [None], [1], [""], ["2026-2-01"],
                      ["2026-02-29"], ["0000-01-01"], ["２０２６-11-01"],
                      ["2026-11-01 "], self.days * 2):
            with self.subTest(dates=dates), self.assertRaises(ValueError):
                normalize_dates(dates)
        with self.assertRaises(ValueError):
            build_report_data(self.store, self.sid, dates=["2026-11-03"])

    @unittest.skipUnless(hasattr(time, "tzset"), "requires local timezone switching")
    def test_machine_local_date_and_dst(self):
        old = os.environ.get("TZ")
        self.addCleanup(self.restore_timezone, old)
        ts = int(datetime(2026, 11, 2, 0, 30, tzinfo=timezone.utc).timestamp() * 1000)
        for zone, expected in (("UTC", "2026-11-02"), ("America/New_York", "2026-11-01"),
                               ("Asia/Shanghai", "2026-11-02")):
            os.environ["TZ"] = zone
            time.tzset()
            self.assertEqual(local_date(ts), expected)
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        for hour in (5, 6):
            repeated = int(datetime(2026, 11, 1, hour, 30, tzinfo=timezone.utc).timestamp() * 1000)
            self.assertEqual(local_date(repeated), "2026-11-01")

    @staticmethod
    def restore_timezone(old):
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()

    def test_raw_filter_precedes_cycles_pid_and_valid_statistics(self):
        original = self.store.session(self.sid)
        data = build_report_data(self.store, self.sid, dates=[self.days[1]], include_full_system=True)
        self.assertEqual(self.store.session(self.sid), original)
        self.assertEqual(data["session"]["import_summary"], original["summary"])
        summary = data["session"]["summary"]
        self.assertEqual((summary["records"], summary["cycles"], summary["segments"]), (7, 4, 1))
        self.assertEqual((summary["first_ts"], summary["last_ts"]), (self.t2, self.t2 + 3000))
        process = data["processes"][0]
        self.assertEqual(process["pids"], [2])
        metric = process["metrics"]["cpu1c"]
        self.assertEqual((metric["count"], metric["avg"], metric["min"], metric["p95"]), (2, 10, 0, 20))
        self.assertEqual(len(process["full_cycles"]), 3)
        self.assertEqual(process["coverage"]["cycles"], 4)
        self.assertEqual(process["coverage"]["missing_cycles"], 1)
        self.assertEqual(len(data["systems"][0]["full_points"]), 4)
        self.assertTrue(all(local_date(point["ts"]) == self.days[1]
                            for point in data["systems"][0]["full_points"]))
        full = build_report_data(self.store, self.sid, include_full_system=True)
        self.assertEqual(full, build_report_data(self.store, self.sid, dates=None, include_full_system=True))
        multiple = build_report_data(self.store, self.sid, dates=[self.days[2], self.days[0]])
        self.assertEqual(multiple["processes"][0]["pids"], [3, 99])
        self.assertEqual(multiple["session"]["summary"]["records"], 4)

    def test_both_styles_cache_selection_and_cancel_preserve_previous(self):
        for style in ("default", "compact"):
            with self.subTest(style=style), patch.object(perf_report, "build_report_data", wraps=build_report_data) as builder:
                first = perf_report.render_report(self.store, self.sid, style=style, dates=[self.days[0]])
                self.assertIn("所选日期（本机本地日期）：" + self.days[0], first)
                self.assertEqual(first, perf_report.render_report(self.store, self.sid, style=style, dates=[self.days[0]]))
                self.assertEqual(builder.call_count, 1)
                second = perf_report.render_report(self.store, self.sid, style=style, dates=[self.days[1]])
                self.assertNotEqual(first, second)
                with self.assertRaises(CancelledError):
                    perf_report.render_report(self.store, self.sid, style=style, dates=[self.days[0]], cancelled=lambda: True)
                self.assertEqual(second, perf_report.render_report(self.store, self.sid, style=style, dates=[self.days[1]]))
                self.assertEqual(builder.call_count, 2)
                full = perf_report.render_report(self.store, self.sid, style=style)
                self.assertNotIn("所选日期（本机本地日期）", full)
                self.assertEqual(builder.call_count, 3)

    def test_qnx_filter_null_zero_statistics_and_styles(self):
        sid = self.load(row("99", ts=self.t1) + row("0", ts=self.t2) + row("", ts=self.t2 + 1)
                        + row("20", ts=self.t2 + 2) + row("90", ts=self.t3), "qnx.log")
        data = qnx.build_report_data(self.store, sid, dates=[self.days[1]])
        series = data["series"][0]
        self.assertEqual((series["samples"], series["valid_samples"], series["avg"], series["p95"]), (3, 2, 10, 20))
        self.assertEqual(data["session"]["summary"]["records"], 3)
        for style in ("default", "compact"):
            with patch.object(qnx, "build_report_data", wraps=qnx.build_report_data) as builder:
                selected = perf_report.render_report(self.store, sid, dates=[self.days[1]], style=style)
                self.assertIn("所选日期（本机本地日期）：" + self.days[1], selected)
                self.assertEqual(selected, perf_report.render_report(self.store, sid, dates=[self.days[1]], style=style))
                self.assertEqual(builder.call_count, 1)
                self.assertNotEqual(selected, perf_report.render_report(self.store, sid, style=style))

    def test_sorted_dates_share_cache_and_sql_cancel_keeps_cache(self):
        dates = [self.days[0], self.days[2]]
        with patch.object(perf_report, "build_report_data", wraps=build_report_data) as builder:
            first = perf_report.render_report(self.store, self.sid, dates=dates)
            self.assertEqual(first, perf_report.render_report(self.store, self.sid, dates=dates[::-1]))
            self.assertEqual(builder.call_count, 1)
        stopped = False
        def long_sort(db, scopes):
            nonlocal stopped
            stopped = True
            db.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT SUM(x) FROM n").fetchone()
        with patch("perf_report_data._statistics", long_sort), self.assertRaises(CancelledError):
            perf_report.render_report(self.store, self.sid, dates=[self.days[1]], cancelled=lambda: stopped)
        self.assertEqual(first, perf_report.render_report(self.store, self.sid, dates=dates))
        stopped = False
        saves = 0
        def stop_at_save(event):
            nonlocal stopped, saves
            if event["stage"] == "save":
                saves += 1
                stopped = True
        with self.assertRaises(CancelledError):
            perf_report.render_report(self.store, self.sid, dates=[self.days[1]],
                                      progress=stop_at_save, cancelled=lambda: stopped)
        self.assertEqual(saves, 1)
        # Cancellation during SQL and before commit must both release the lock.
        self.assertEqual(first, perf_report.render_report(self.store, self.sid, dates=dates))

    def test_admission_filters_real_samples_and_rejects_before_template(self):
        from test_admission import AdmissionTests
        result = build_admission(self.store, self.sid, template(), self.root / "output",
                                 {0: "background"}, dates=[self.days[1]])
        rows, _ = AdmissionTests.rows(self, result["blocks"])
        self.assertEqual(rows[0][5], "20.00")
        self.assertIn("所选日期（本机本地日期）：" + self.days[1], result["warnings"][0])
        with patch("perf_admission.perf_slate.validate_template") as validator:
            for dates in ([], ["2026-11-03"], ["bad"]):
                with self.assertRaises(ValueError):
                    build_admission(self.store, self.sid, [], self.root / "bad", dates=dates)
            validator.assert_not_called()
        self.assertFalse((self.root / "bad").exists())

    def wait_job(self, manager, draft, state="ready"):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = manager.get(self.sid, draft)
            if result["state"] == state:
                return result
            if result["state"] == "failed":
                self.fail(str(result))
            time.sleep(.01)
        self.fail(str(result))

    def test_jobs_persist_dates_idempotency_and_legacy(self):
        root = self.root / "jobs"
        manager = jobs.AdmissionJobs(self.store, root)
        self.addCleanup(manager.close)
        with patch.object(jobs, "read_template", return_value=template()) as reader, \
                patch.object(jobs, "build_admission", wraps=build_admission) as builder:
            draft = "a" * 32
            selected = [self.days[2], self.days[0]]
            manager.create(self.sid, draft, "date test", dates=selected)
            self.wait_job(manager, draft)
            manager.create(self.sid, draft, "date test", dates=selected[::-1])
            self.assertEqual(builder.call_count, 1)
            self.assertEqual(builder.call_args.kwargs["dates"], sorted(selected))
            for dates in (None, [self.days[1]]):
                with self.assertRaises(jobs.AdmissionJobError) as error:
                    manager.create(self.sid, draft, "date test", dates=dates)
                self.assertEqual(error.exception.status, 409)
            with self.assertRaises(jobs.AdmissionJobError):
                manager.create(self.sid, "b" * 32, "invalid", dates=["2026-11-03"])
            self.assertEqual(reader.call_count, 1)
            manager.create(self.sid, "c" * 32, "legacy")
            self.wait_job(manager, "c" * 32)
            self.assertNotIn("dates", builder.call_args.kwargs)
            manager.close()
            with sqlite3.connect(root / "admission.sqlite3") as db:
                configs = [json.loads(r[0])["config"] for r in db.execute("SELECT data FROM drafts")]
            self.assertTrue(any(c.get("dates") == sorted(selected) for c in configs))
            self.assertTrue(any("dates" not in c for c in configs))
            restored = jobs.AdmissionJobs(self.store, root)
            self.addCleanup(restored.close)
            self.assertEqual(restored.get(self.sid, draft)["state"], "ready")
            restored.create(self.sid, draft, "date test", dates=selected)
            self.assertEqual(builder.call_count, 2)

    @unittest.skipUnless(hasattr(time, "tzset"), "requires local timezone switching")
    def test_timezone_is_part_of_selected_cache_identity(self):
        self.addCleanup(self.restore_timezone, os.environ.get("TZ"))
        sid = self.load(s(stamp(self.days[1], "12:00:00")))
        with patch.object(perf_report, "build_report_data", wraps=build_report_data) as builder:
            for zone in ("UTC", "Asia/Shanghai"):
                os.environ["TZ"] = zone
                time.tzset()
                day = self.dates(sid)["dates"][0]["date"]
                perf_report.render_report(self.store, sid, dates=[day])
                perf_report.render_report(self.store, sid, dates=[day])
            self.assertEqual(builder.call_count, 2)

    def test_worker_revalidates_before_reading_remote_template(self):
        manager = jobs.AdmissionJobs(self.store, self.root / "jobs")
        self.addCleanup(manager.close)
        with patch.object(manager, "_enqueue", side_effect=lambda record, worker: record):
            record = manager.create(self.sid, "e" * 32, "revalidate", dates=[self.days[1]])
        with self.store.connect() as db:
            db.execute("UPDATE records SET ts=? WHERE session=?", (self.t1, self.sid))
        with patch.object(jobs, "read_template") as reader:
            manager._generate(record)
            reader.assert_not_called()
        self.assertEqual(manager.get(self.sid, record["draft_id"])["state"], "failed")

    def test_api_dates_reports_stream_and_rejection(self):
        app = perf_api.create_app(self.store.path)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            client.headers["X-Session-Token"] = client.get("/api/token").json()["token"]
            base = f"/api/sessions/{self.sid}"
            self.assertEqual(client.get(base + "/report-dates").json(), self.dates())
            self.assertEqual(client.get("/api/sessions/missing/report-dates").status_code, 404)
            with patch.object(perf_api, "render_report", wraps=perf_report.render_report) as render:
                for suffix in ("/report", "/report/stream"):
                    for query in ("dates=", "dates=bad", "dates=2026-11-03",
                                  "dates=2026-11-01&dates=2026-11-01"):
                        self.assertEqual(client.get(base + suffix + "?" + query).status_code, 400)
                render.assert_not_called()
            for style in ("default", "compact"):
                params = [("style", style), ("dates", self.days[2]), ("dates", self.days[0])]
                response = client.get(base + "/report/stream", params=params)
                self.assertEqual(response.status_code, 200)
                events = [json.loads(line) for line in response.text.splitlines()]
                url = next(e["data"] for e in events if e["type"] == "result")
                query = parse_qs(urlsplit(url).query)
                self.assertEqual(query["dates"], [self.days[0], self.days[2]])
                self.assertEqual(query.get("style", ["default"]), [style])
                downloaded = client.get(url)
                self.assertEqual(downloaded.status_code, 200)
                self.assertIn("所选日期（本机本地日期）：" + self.days[0] + "、" + self.days[2], downloaded.text)
                self.assertIn("attachment;", downloaded.headers["content-disposition"])
                self.assertIn("sandbox allow-scripts", downloaded.headers["content-security-policy"])
                self.assertNotIn("所选日期（本机本地日期）", client.get(base + "/report", params={"style": style}).text)

    def test_api_admission_dates_validate_before_queue_and_persist(self):
        app = perf_api.create_app(self.store.path)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client, \
                patch.object(jobs, "read_template", return_value=template()) as reader:
            client.headers["X-Session-Token"] = client.get("/api/token").json()["token"]
            url = f"/api/sessions/{self.sid}/admission/drafts"
            body = {"draft_id": "d" * 32, "title": "selected dates"}
            for dates, status in ((None, 422), ([], 422), ([""], 422), (["2026-02-29"], 422),
                                  ([self.days[0]] * 2, 422), (["2026-11-03"], 400)):
                response = client.post(url, json=dict(body, dates=dates))
                self.assertEqual(response.status_code, status, response.text)
                self.assertIs(response.json()["accepted"], False)
                self.assertIsNone(app.state.admission_jobs)
            reader.assert_not_called()
            response = client.post(url, json=dict(body, dates=[self.days[1]]))
            self.assertEqual(response.status_code, 200, response.text)
            self.wait_job(app.state.admission_jobs, body["draft_id"])
            preview = client.get(url + "/" + body["draft_id"] + "/preview")
            self.assertIn("所选日期（本机本地日期）：" + self.days[1], preview.text)
            self.assertEqual(reader.call_count, 1)