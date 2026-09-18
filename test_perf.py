import io
import tempfile
import unittest
from pathlib import Path

from perf_parser import iter_records, parse_line, rotation_key
from perf_store import Store

SAMPLE = """S,946684800123,7,42.04,25.09,14.17,0.00,3.25,9326,14725,5399
P,946684800123,5589,14.54,116.30,14.54,0.52,CFS,2,S,45,247120,4292,0,0,156,469,184,246,585,com.desaysv.engmode
D,946684800123,2349.90,2939
DE,946684800123,qcom_hgsl,1201.10,2394
DE,946684800123,qcom system,1082.20,412
DP,946684800123,1361,413664,78,/system/bin/surfaceflinger
"""


class ParserTests(unittest.TestCase):
    def test_protocol(self):
        records = list(iter_records(io.BytesIO(SAMPLE.encode())))
        self.assertEqual(len(records), 6)
        self.assertTrue(all(r.error is None for r in records))
        self.assertEqual(records[0].data["mem_used_mb"], 5399)
        self.assertEqual(records[1].data["wr_kb"], 156)
        self.assertEqual(records[4].data["exporter"], "qcom system")

    def test_bad_unknown_and_partial(self):
        rows = list(iter_records(io.BytesIO(b"X,123\nS,12\nD,1,nan,2\nD,1,2,3")))
        self.assertIsNone(rows[0].kind)
        self.assertTrue(all(row.error for row in rows[1:]))

    def test_name_and_signed_delta(self):
        text = SAMPLE.splitlines()[1].replace("4292", "-4292") + " extra arg"
        _, data = parse_line(text)
        self.assertEqual(data["rss_delta_kb"], -4292)
        self.assertTrue(data["name"].endswith("extra arg"))
        self.assertEqual(parse_line("DP,1,2,18446744073709551615,1,test")[1]["size_kb"], 2 ** 64 - 1)

    def test_rotation(self):
        self.assertEqual(sorted(["perf.log", "perf.1.log", "perf.4.log"], key=rotation_key),
                         ["perf.4.log", "perf.1.log", "perf.log"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite3")

    def import_sample(self, text=SAMPLE):
        return self.store.import_files([("perf.log", io.BytesIO(text.encode()))])

    def test_group_api_contract(self):
        from fastapi.testclient import TestClient
        from perf_api import create_app

        app = create_app(Path(self.temp.name) / "api.sqlite3")
        store = app.state.store
        sid = store.import_files([("perf.log", io.BytesIO(SAMPLE.encode()))])["id"]
        other = store.import_files([("other.log", io.BytesIO(SAMPLE.replace("42.04", "43.04").encode()))])["id"]
        base = f"/api/sessions/{sid}"
        body = {"name": "H5", "segment": 0, "members": [{"pid": 5589, "name": "com.desaysv.engmode"}]}
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            self.assertEqual(client.get(base + "/groups").status_code, 403)
            headers = {"X-Session-Token": client.get("/api/token").json()["token"]}
            client.headers.update(headers)
            self.assertEqual(client.get(base + "/groups").json(), [])
            response = client.post(base + "/groups", json=body)
            self.assertEqual(response.status_code, 201, response.text)
            gid = response.json()["id"]
            path = base + "/groups/" + gid
            self.assertEqual(client.get(path).json()["name"], "H5")
            result = client.get(path + "/analysis?kind=P&limit=64").json()
            self.assertEqual(result["statistics"]["metrics"]["cpu1c"]["p95"], 116.30)
            self.assertEqual(result["coverage"]["complete"], 1)
            self.assertEqual(client.get(path + "/analysis?kind=DP").status_code, 200)
            for query in ("kind=S", "limit=0", "limit=5001"):
                self.assertEqual(client.get(path + "/analysis?" + query).status_code, 422)
            for changes in ({"unknown": 1}, {"segment": True}, {"members": []},
                            {"start": "123"}, {"name": "a" * 121}, {"scene": "自动"},
                            {"members": [{"pid": 5589, "name": "x", "unknown": 1}]}):
                with self.subTest(changes=changes):
                    self.assertEqual(client.post(base + "/groups", json={**body, **changes}).status_code, 422)
            for changes in ({"name": " "}, {"start": 2, "end": 1},
                            {"members": body["members"] * 2},
                            {"members": [{"pid": 999, "name": "missing"}]}):
                self.assertEqual(client.post(base + "/groups", json={**body, **changes}).status_code, 400)
            self.assertEqual(client.put(path, json={**body, "name": "已更新"}).status_code, 200)
            self.assertEqual(client.get(path).json()["name"], "已更新")
            foreign = f"/api/sessions/{other}/groups/{gid}"
            for method, kwargs in (("get", {}), ("put", {"json": body}), ("delete", {})):
                self.assertEqual(getattr(client, method)(foreign, **kwargs).status_code, 404)
            settings = base + "/report-settings"
            self.assertEqual(client.get(settings).json(), {})
            resources = client.get(path + "/resources")
            self.assertEqual(resources.status_code, 200)
            self.assertEqual(set(resources.json()), {"group", "settings", "rows", "note"})
            row = resources.json()["rows"][0]
            self.assertEqual(set(row), {"member", "statistics", "kdmips"})
            self.assertEqual(set(row["statistics"]), {"cpu1c", "rss_kb", "rd_kb", "wr_kb"})
            self.assertEqual(set(row["statistics"]["cpu1c"]), {"count", "min", "avg", "max", "p95", "p99"})
            self.assertEqual(set(row["statistics"]["rd_kb"]), {"count", "min", "avg", "max", "p95", "p99", "total"})
            self.assertTrue(all(v is None for v in row["kdmips"].values()))
            self.assertEqual(row["member"]["service_category"], "未分类")
            self.assertEqual(row["member"]["related_service"], "")
            self.assertEqual(client.get(foreign + "/resources").status_code, 404)
            for invalid in ({"cpu_platform": "test", "kdmips_per_core": v}
                            for v in (True, 0, -1, "28.75")):
                self.assertEqual(client.put(settings, json=invalid).status_code, 422)
            for invalid in ({"kdmips_per_core": 1}, {"cpu_platform": " ", "kdmips_per_core": 1},
                            {"cpu_platform": "x" * 121}, {"cpu_platform":None}):
                self.assertEqual(client.put(settings, json=invalid).status_code, 422)
            for changes in ({"service_category": "invalid"}, {"related_service": "x" * 2001}):
                invalid = {**body, "members": [{**body["members"][0], **changes}]}
                self.assertEqual(client.put(path, json=invalid).status_code, 422)
            calibration = dict(cpu_platform="test", kdmips_per_core=28.75)
            self.assertEqual(client.put(settings, json=calibration).status_code, 200)
            calibrated = client.get(path + "/resources").json()
            self.assertEqual(calibrated["settings"], calibration)
            self.assertAlmostEqual(calibrated["rows"][0]["kdmips"]["avg"], 33.43625)
            self.assertEqual(client.get(base + "/processes?segment=99&with_total=true").json(),
                             dict(items=[], total=0, all_total=0))
            for query in ("segment=-1", "segment=true", "segment=9223372036854775808"):
                self.assertEqual(client.get(base + "/processes?" + query).status_code, 422)
            self.assertEqual(client.put(settings, json={"hardware": "设备", "conclusion": "待评估"}).status_code, 200)
            self.assertEqual(client.get(settings).json()["hardware"], "设备")
            for invalid in ({"unknown": 1}, {"conclusion": "自动准入"}, {"tester": "x" * 201}):
                self.assertEqual(client.put(settings, json=invalid).status_code, 422)
            self.assertEqual(client.get(f"/api/sessions/{other}/report-settings").json(), {})
            self.assertEqual(client.delete(path).status_code, 200)
            self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.delete(path).status_code, 404)
            self.assertEqual(client.get("/api/sessions/missing/groups").status_code, 404)

    def test_statistics_helper_all_fields_and_percentiles(self):
        from perf_metrics import CATEGORIES, NUMERIC
        from perf_report import statistics_table

        rows = [SAMPLE.replace("42.04", str(cpu)).replace("946684800123", str(946684800123 + cpu))
                for cpu in range(1, 101)]
        sid = self.import_sample("".join(rows))["id"]
        for kind, fields in NUMERIC.items():
            result = self.store.statistics(sid, kind)
            report = statistics_table(result, kind + " · 原始统计")
            self.assertIn(kind + " · ", report)
            for field in fields:
                self.assertIn("<td>" + field + "</td>", report)
            for field in CATEGORIES.get(kind, ()):
                self.assertIn(" · " + field + "</h4>", report)
            if kind == "S":
                self.assertIn("<td>1 %</td><td>50.5 %</td><td>95 %</td><td>99 %</td><td>100 %</td>", report)
                self.assertIn("9.11 GB", report)
            self.assertIn("2000-01-01T00:00:00.124+00:00", report)
            self.assertIn("2000-01-01T00:00:00.223+00:00", report)

    def test_report_formatting_and_empty_metrics(self):
        from perf_report import measure, render_report, timestamp

        for value, unit, total, expected in (
                (None, "KB", False, "—"), (0, "MB", False, "0 MB"),
                (1024, "KB", False, "1 MB"), (0.5, "MB", False, "512 KB"),
                (-2048, "KB / 周期", False, "-2 MB / 周期"),
                (-2048, "KB / 周期", True, "-2 MB"),
                (12.345, "%", False, "12.35 %"), (1234, "", False, "1,234")):
            with self.subTest(value=value, unit=unit, total=total):
                self.assertEqual(measure(value, unit, total), expected)
        self.assertEqual(timestamp(None), "—")
        self.assertEqual(timestamp(946684800123), "2000-01-01T00:00:00.123+00:00")
        self.assertEqual(timestamp(2 ** 63 - 1), str(2 ** 63 - 1) + " ms")
        sid = self.import_sample(SAMPLE.splitlines()[0] + "\n")["id"]
        report = render_report(self.store, sid)
        self.assertIn("无进程记录", report)
        self.assertNotIn('<details', report)
        from perf_report import statistics_table
        empty = statistics_table(self.store.statistics(sid, "P"), "P")
        self.assertIn("0 条原始记录；— → —", empty)
        self.assertIn("<td>cpu1c</td><td>0</td><td>—</td>", empty)

    def test_report_statistics_escape_categories(self):
        from perf_report import statistics_table, text

        sid = self.import_sample()["id"]
        result = self.store.statistics(sid, "P")
        payload = '<img src=x onerror="alert(1)">'
        result["categories"]["pol"]["values"][0]["value"] = payload
        result["metrics"]["cpu"]["note"] = payload
        report = statistics_table(result, payload)
        self.assertNotIn(payload, report)
        self.assertEqual(report.count(text(payload)), 3)

    def test_report_compact_contract_and_system_resources(self):
        import re
        from unittest.mock import patch
        from perf_metrics import METHOD
        from perf_report import render_report, text

        rows = [SAMPLE.replace("42.04", str(i)).replace("5399", str(i * 2))
                .replace("946684800123", str(946684800123 + i)) for i in range(1, 101)]
        sid = self.import_sample("".join(rows))["id"]
        with patch.object(self.store, "group_analysis", wraps=self.store.group_analysis) as groups, \
                patch.object(self.store, "report_settings", wraps=self.store.report_settings) as settings:
            report = render_report(self.store, sid)
        groups.assert_not_called()
        settings.assert_not_called()
        self.assertEqual(re.findall(r"<h2>(.*?)</h2>", report), ["全部进程资源统计"])
        self.assertEqual(report.count('<details class="process">'), 2)
        self.assertEqual(report.count('<summary>'), 2)
        self.assertIn(text(METHOD), report)
        for removed in ("1 测试环境", "6 准入结论", "附录 A", "组同周期合计统计", "请先配置准入进程组"):
            self.assertNotIn(removed, report)
        for phrase in ("进程 CPU + 总 CPU", "内存占用（RSS）", "cpu / cpu_total", "离线查看"):
            self.assertIn(phrase, report)
        self.assertNotRegex(report.lower(), r"<script|<link|<iframe|<img|<object|<embed")
        # 精简展示不删除系统资源底层统计的验证。
        metrics = self.store.statistics(sid, "S")["metrics"]
        for field, expected in (("cpu_total", [95, 99, 100]), ("mem_used_mb", [190, 198, 200])):
            self.assertEqual(metrics[field]["count"], 100)
            self.assertEqual([metrics[field][key] for key in ("p95", "p99", "max")], expected)

    def test_import_duplicate_query_delete(self):
        result = self.import_sample()
        self.assertEqual(self.import_sample(), {"id": result["id"], "duplicate": True})
        overview = self.store.overview(result["id"])
        self.assertEqual(overview["summary"]["counts"]["DE"], 2)
        self.assertEqual(overview["stats"]["cpu_peak"], 42.04)
        self.assertEqual(self.store.series(result["id"], "P")["points"][0]["cpu1c"], 116.30)
        self.assertEqual(self.store.processes(result["id"])[0]["write_kb"], 156)
        self.assertTrue(self.store.delete(result["id"]))
        self.assertEqual(self.store.sessions(), [])

    def test_reboot_and_clock_rollback(self):
        text = SAMPLE + SAMPLE.replace("946684800123", "946684799123")
        text += SAMPLE.replace(",7,42.04", ",8,42.04")
        result = self.import_sample(text)
        self.assertEqual(self.store.session(result["id"])["summary"]["segments"], 3)

    def test_rollback_empty_import(self):
        with self.assertRaises(ValueError):
            self.import_sample("unknown\n")
        self.assertEqual(self.store.sessions(), [])

    def test_peak_preserving_sampling(self):
        text = "".join("S,%d,7,%d,0,0,0,0,100,200,100\n" % (i, 99 if i == 451 else 1) for i in range(1000))
        result = self.import_sample(text)
        series = self.store.series(result["id"], "S", limit=60)
        self.assertTrue(series["sampled"])
        self.assertLessEqual(len(series["points"]), 60)
        self.assertEqual(max(point["cpu_total"] for point in series["points"]), 99)

    def test_small_series_is_not_sampled(self):
        result = self.import_sample("".join("S,%d,7,1,0,0,0,0,100,200,100\n" % i for i in range(100)))
        series = self.store.series(result["id"], "S", limit=100)
        self.assertFalse(series["sampled"])
        self.assertEqual(len(series["points"]), 100)

    def test_large_io_sum_does_not_overflow(self):
        from perf_parser import SCHEMAS
        row = SAMPLE.splitlines()[1].split(",")
        for field in ("rd_kb", "wr_kb"):
            row[SCHEMAS["P"].split().index(field) + 1] = str(2**63 - 1)
        result = self.import_sample((",".join(row) + "\n") * 3)
        process = self.store.processes(result["id"])[0]
        for field in ("read_kb", "write_kb"):
            self.assertEqual(process[field], float(3 * (2**63 - 1)))

    def test_all_fields_statistics_and_cache(self):
        from perf_parser import SCHEMAS
        from perf_metrics import NUMERIC, CATEGORIES, IDENTIFIERS, INCREMENTS
        sid = self.import_sample()["id"]
        for kind, schema in SCHEMAS.items():
            result = self.store.statistics(sid, kind, segment=0)
            self.assertEqual(set(result["metrics"]), set(schema.split()) - IDENTIFIERS)
            self.assertEqual(set(result["categories"]), set(CATEGORIES.get(kind, ())))
            for field in NUMERIC[kind]:
                metric = result["metrics"][field]
                self.assertGreater(metric["count"], 0)
                self.assertIsNotNone(metric["p95"])
                self.assertIsNotNone(metric["p99"])
                self.assertEqual("total" in metric, field in INCREMENTS)
            self.assertEqual(result, self.store.statistics(sid, kind, segment=0))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_cache").fetchone()[0], 5)
        self.assertTrue(self.store.delete(sid))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_cache").fetchone()[0], 0)
        with self.assertRaises(KeyError):
            self.store.statistics(sid, "S")

    def test_exact_percentiles_independent_of_sampling(self):
        text = "".join(f"S,{i},7,{i},0,0,0,0,100,200,100\n" for i in range(1, 101))
        sid = self.import_sample(text)["id"]
        self.store.series(sid, "S", limit=64)
        metric = self.store.statistics(sid, "S")["metrics"]["cpu_total"]
        self.assertEqual([metric[k] for k in ("count", "min", "max", "avg", "p95", "p99")],
                         [100, 1, 100, 50.5, 95, 99])
        empty = self.store.statistics(sid, "DP")["metrics"]["size_kb"]
        self.assertEqual(empty["count"], 0)
        self.assertIsNone(empty["p99"])
        single = self.import_sample("D,1,2,3\n")["id"]
        self.assertEqual(self.store.statistics(single, "D")["metrics"]["total_mb"]["p95"], 2)

    def test_statistics_identity_categories_and_sort(self):
        from perf_parser import SCHEMAS
        fields = SCHEMAS["P"].split()
        template = SAMPLE.splitlines()[1].split(",")
        def row(ts, name, cpu, delta, policy, core):
            values = template.copy()
            for field, value in dict(ts=ts, name=name, cpu=cpu, rss_delta_kb=delta,
                                     pol=policy, cpu_core=core, rd_kb=2**63-1).items():
                values[fields.index(field) + 1] = str(value)
            return ",".join(values) + "\n"
        text = row(1, "a", 1, -10, "?", -1) + row(2, "a", 2, 5, "CFS", 2)
        text += row(3, "b", 99, 0, "FF", 0) + row(0, "a", 100, 0, "RR", 1)
        sid = self.import_sample(text)["id"]
        stats = self.store.statistics(sid, "P", pid=5589, segment=0, name="a")
        self.assertEqual(stats["samples"], 2)
        self.assertEqual(stats["metrics"]["cpu"]["p99"], 2)
        self.assertEqual(stats["metrics"]["rss_delta_kb"]["total"], -5)
        self.assertGreater(stats["metrics"]["cpu1c"]["p95"], 100)
        self.assertEqual(stats["metrics"]["rd_kb"]["total"], float(2*(2**63-1)))
        self.assertEqual(stats["categories"]["cpu_core"]["values"][0]["value"], -1)
        self.assertEqual(stats["categories"]["pol"]["values"][0]["percent"], 50)
        self.assertEqual(self.store.statistics(sid, "P", pid=1)["samples"], 0)
        for sort in ("cpu_p95", "cpu_p99"):
            rows = self.store.processes(sid, sort=sort)
            self.assertEqual([r[sort] for r in rows], [100, 99, 2])
            self.assertEqual(self.store.processes(sid, sort=sort, limit=1, offset=1)[0]["name"], "b")

    def test_process_search_pagination_and_persistent_cache(self):
        import sqlite3
        from contextlib import contextmanager
        from unittest.mock import patch
        text = "".join(SAMPLE.splitlines()[1].replace("5589", str(1000 + i)).replace(
            "com.desaysv.engmode", f"App.worker{i:03}") + "\n" for i in range(125))
        text += "DP,946684800123,9000,512,1,special%_name\n"
        sid = self.import_sample(text)["id"]
        result = self.store.processes(sid, with_total=True)
        self.assertEqual((result["total"], result["all_total"]), (126, 126))
        rows = [row for offset in range(0, 126, 20)
                for row in self.store.processes(sid, limit=20, offset=offset)]
        self.assertEqual(len({(r["segment"], r["pid"], r["name"]) for r in rows}), 126)
        self.assertEqual(rows[-1]["samples"], 0)
        self.assertIsNone(rows[-1]["cpu_p95"])
        self.assertEqual(self.store.processes(sid, q=" APP.WORKER124 ")[0]["pid"], 1124)
        self.assertEqual(self.store.processes(sid, q="1124")[0]["name"], "App.worker124")
        self.assertEqual(self.store.processes(sid, q="%_")[0]["pid"], 9000)
        empty = self.store.processes(sid, q="' OR 1=1 --", with_total=True)
        self.assertEqual((empty["items"], empty["total"], empty["all_total"]), ([], 0, 126))
        beyond = self.store.processes(sid, offset=999, q="worker", with_total=True)
        self.assertEqual((beyond["items"], beyond["total"]), ([], 125))
        reopened = Store(self.store.path)
        connect = reopened.connect
        @contextmanager
        def cached_only():
            with connect() as db:
                db.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY
                                  if action == sqlite3.SQLITE_READ and table == "records"
                                  else sqlite3.SQLITE_OK)
                yield db
        with patch.object(reopened, "connect", cached_only):
            for sort in ("cpu_peak", "cpu_avg", "cpu_p95", "cpu_p99", "rss_peak_kb",
                         "read_kb", "write_kb", "wait_peak", "dmabuf_peak_kb"):
                self.assertTrue(reopened.processes(sid, sort=sort, q="worker", offset=100))
        other = self.import_sample()["id"]
        self.assertEqual(self.store.processes(other, with_total=True)["total"], 2)
        self.store.delete(sid)
        with self.store.connect() as db:
            for table in ("process_rank_v2", "process_rank_ready_v2"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE session=?", (sid,)).fetchone()[0], 0)
        with self.assertRaises(KeyError):
            reopened.processes(sid)

    def test_merged_process_identity_statistics_and_pid_changes(self):
        p = GroupStoreTests.p
        for changes in range(5):
            sequence = [111, 222, 333, 444, 555][:changes + 1]
            text = ''.join(p(i + 1, pid, 'same', cpu=i + 1) +
                           f'DP,{i + 1},{pid},100,1,same\n' for i, pid in enumerate(sequence))
            sid = self.import_sample(text)['id']
            row = self.store.processes(sid, merge_names=True)[0]
            self.assertEqual(row['pid_changes'], changes)
            self.assertEqual(row['pid_path'], [[pid] for pid in sequence[:4]])
            self.assertEqual(row['pids'], sequence)
            self.assertFalse(row['concurrent_pids'])
        text = p(1, 111, 'same', cpu=1) + p(2, 111, 'same', cpu=2)
        text += p(3, 222, 'same', cpu=99) + p(4, 111, 'same', cpu=3)
        text += p(0, 333, 'same', cpu=100)  # rollback creates a separate segment
        text += 'DP,0,888,100,1,dma-only\n'
        sid = self.import_sample(text)['id']
        result = self.store.processes(sid, merge_names=True, with_total=True)
        self.assertEqual(result['total'], 3)
        row = next(r for r in result['items'] if r['segment'] == 0)
        self.assertEqual(row['pid_path'], [[111], [222], [111]])
        self.assertEqual(row['pid_changes'], 2)
        self.assertEqual(row['samples'], 4)
        self.assertEqual(row['cpu_avg'], 26.25)
        self.assertEqual(row['cpu_p95'], 99)
        self.assertEqual(row['cpu_p99'], 99)
        stats = self.store.statistics(sid, 'P', segment=0, name='same')
        self.assertEqual(stats['metrics']['cpu']['avg'], row['cpu_avg'])
        self.assertEqual(stats['metrics']['rd_kb']['total'], row['read_kb'])
        series = self.store.series(sid, 'P', segment=0, name='same')
        self.assertEqual([r['pid'] for r in series['points']], [111, 111, 222, 111])
        self.assertEqual(len(self.store.processes(sid)), 4)  # legacy identity unchanged
        self.assertEqual(self.store.processes(sid, q='222', merge_names=True), [row])
        dma = self.store.processes(sid, q='dma-only', merge_names=True)[0]
        self.assertEqual(dma['samples'], 0)
        self.assertIsNone(dma['cpu_p95'])
        self.assertEqual(dma['pid_path'], [[888]])

    def test_merged_process_cycles_cache_and_api(self):
        import sqlite3
        from contextlib import contextmanager
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from perf_api import create_app
        p = GroupStoreTests.p
        text = (p(1, 111, 'same') + p(1, 222, 'same') + p(1, 111, 'same') +
                'DP,1,111,100,1,same\n' + p(2, 222, 'same') + p(2, 111, 'same') +
                p(3, 333, 'same') + p(3, 444, 'other%_'))
        sid = self.import_sample(text)['id']
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.processes(sid, merge_names=True), range(8)))
        self.assertTrue(all(r == results[0] for r in results))
        row = next(r for r in results[0] if r['name'] == 'same')
        self.assertEqual(row['pid_path'], [[111, 222], [333]])
        self.assertEqual(row['pid_changes'], 1)
        self.assertTrue(row['concurrent_pids'])
        reopened = Store(self.store.path)
        connect = reopened.connect
        @contextmanager
        def cached_only():
            with connect() as db:
                db.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY
                                  if action == sqlite3.SQLITE_READ and table == 'records'
                                  else sqlite3.SQLITE_OK)
                yield db
        with patch.object(reopened, 'connect', cached_only):
            for sort in ('cpu_peak', 'cpu_avg', 'cpu_p95', 'cpu_p99', 'rss_peak_kb',
                         'read_kb', 'write_kb', 'wait_peak', 'dmabuf_peak_kb'):
                self.assertEqual(reopened.processes(sid, sort=sort, q='222', merge_names=True), [row])
            self.assertEqual(len(reopened.processes(sid, q='%_', merge_names=True)), 1)
            self.assertEqual(reopened.processes(sid, q=',', merge_names=True), [])
            pages = [reopened.processes(sid, limit=1, offset=i, merge_names=True)[0] for i in range(2)]
            self.assertEqual(len({r['name'] for r in pages}), 2)
        with TestClient(create_app(self.store.path), base_url='http://127.0.0.1:8765') as client:
            client.headers['X-Session-Token'] = client.get('/api/token').json()['token']
            response = client.get(f'/api/sessions/{sid}/processes?merge_names=true&with_total=true&q=222')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'items': [row], 'total': 1, 'all_total': 2})
        empty = self.import_sample('D,1,2,3\n')['id']
        self.assertEqual(self.store.processes(empty, merge_names=True, with_total=True),
                         {'items': [], 'total': 0, 'all_total': 0})
        self.store.delete(sid)
        with self.store.connect() as db:
            for table in ('process_name_rank_v1', 'process_name_ready_v1'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table} WHERE session=?', (sid,)).fetchone()[0], 0)

    def test_rss_raw_distribution_segment_and_legacy_cache(self):
        import sqlite3
        from contextlib import contextmanager
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from perf_api import create_app
        p = GroupStoreTests.p
        text = ''.join(p(i, 1, 'same', rss_kb=i) for i in range(1, 101))
        text += p(100, 2, 'same', rss_kb=1000) + p(101, 2, 'same', rss_kb=2000)
        text += p(102, 2, 'same', rss_kb=9999) + 'DP,102,3,10,1,dma\n'
        text += p(1, 4, 'same', rss_kb=3000)
        sid = self.import_sample(text)['id']
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.rss_kb',NULL) WHERE session=? AND ts=102 AND kind='P'", (sid,))
        single = self.store.processes(sid, segment=0, q='1')[0]
        self.assertEqual([single[k] for k in ('rss_avg_kb', 'rss_p95_kb', 'rss_p99_kb')], [50.5, 95, 99])
        merged = self.store.processes(sid, segment=0, merge_names=True, q='same')[0]
        self.assertAlmostEqual(merged['rss_avg_kb'], 8050 / 102)
        self.assertEqual((merged['rss_p95_kb'], merged['rss_p99_kb']), (97, 1000))
        self.assertEqual(merged['samples'], 103)
        dma = self.store.processes(sid, q='dma')[0]
        self.assertTrue(all(dma[k] is None for k in ('rss_avg_kb', 'rss_p95_kb', 'rss_p99_kb')))
        for invalid in (True, -1, '0', 2**63):
            with self.assertRaises(ValueError):
                self.store.processes(sid, segment=invalid)
        # Simulate a database whose original rank caches are already populated.
        with self.store.connect() as db:
            db.execute('DROP TABLE process_rss_rank_v1')
            db.execute('DROP TABLE process_rss_ready_v1')
        reopened = Store(self.store.path)
        self.assertEqual(reopened.processes(sid, segment=0, merge_names=True, q='same'), [merged])
        connect = reopened.connect
        @contextmanager
        def cached_only():
            with connect() as db:
                db.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY
                                  if action == sqlite3.SQLITE_READ and table == 'records'
                                  else sqlite3.SQLITE_OK)
                yield db
        with patch.object(reopened, 'connect', cached_only):
            for sort in ('rss_avg_kb', 'rss_p95_kb', 'rss_p99_kb'):
                self.assertEqual([r['pid'] for r in reopened.processes(sid, sort=sort, segment=0)], [2, 1, 3])
                result = reopened.processes(sid, sort=sort, segment=0, merge_names=True, q='same', with_total=True)
                self.assertEqual(result, dict(items=[merged], total=1, all_total=2))
                self.assertEqual(reopened.processes(sid, segment=1)[0]['rss_avg_kb'], 3000)
        with TestClient(create_app(self.store.path), base_url='http://127.0.0.1:8765') as client:
            client.headers['X-Session-Token'] = client.get('/api/token').json()['token']
            for sort in ('rss_avg_kb', 'rss_p95_kb', 'rss_p99_kb'):
                response = client.get(f'/api/sessions/{sid}/processes', params=dict(
                    sort=sort, segment=0, merge_names='true', with_total='true', q='same'))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), dict(items=[merged], total=1, all_total=2))
        self.store.delete(sid)
        with self.store.connect() as db:
            for table in ('process_rss_rank_v1', 'process_rss_ready_v1'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table} WHERE session=?', (sid,)).fetchone()[0], 0)

    def test_process_cache_concurrent_and_empty(self):
        from concurrent.futures import ThreadPoolExecutor
        sid = self.import_sample()["id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.processes(sid), range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM process_rank_v2").fetchone()[0], 2)
        empty = self.import_sample("D,1,2,3\n")["id"]
        for _ in range(2):
            self.assertEqual(self.store.processes(empty, with_total=True),
                             {"items": [], "total": 0, "all_total": 0})

    def test_rotation_split_cycle_and_duplicate_file(self):
        lines = SAMPLE.splitlines(keepends=True)
        result = self.store.import_files([
            ("perf.log", io.BytesIO("".join(lines[1:]).encode())),
            ("perf.1.log", io.BytesIO(lines[0].encode())),
            ("copy.log", io.BytesIO(lines[0].encode())),
        ])
        summary = self.store.session(result["id"])["summary"]
        self.assertEqual(summary["cycles"], 1)
        self.assertEqual(summary["orphan_cycles"], 0)
        self.assertEqual(summary["duplicate_files"], 1)


class GroupStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "groups.sqlite3")

    @staticmethod
    def p(ts, pid=1, name="a", **changes):
        from perf_parser import SCHEMAS
        fields = SCHEMAS["P"].split()
        values = SAMPLE.splitlines()[1].split(",")
        for field, value in dict(ts=ts, pid=pid, name=name, **changes).items():
            values[fields.index(field) + 1] = str(value)
        return ",".join(values) + "\n"

    @staticmethod
    def s(ts):
        return f"S,{ts},7,1,0,0,0,0,100,200,100\n"

    def load(self, text):
        return self.store.import_files([("perf.log", io.BytesIO(text.encode()))])["id"]

    @staticmethod
    def config(**changes):
        return dict(dict(name="组合", segment=0, members=[dict(pid=1, name="a"),
                        dict(pid=2, name="b")]), **changes)

    def test_report_all_identities_including_dp_beyond_top_100(self):
        import re
        from unittest.mock import patch
        from perf_report import render_report

        rows = self.s(1) + "".join(self.p(1, pid, f"worker-{pid}") for pid in range(1, 106))
        rows += "DP,1,1,999999,1,worker-1\nDP,1,999,123456,1,dma-only\n"
        sid = self.load(rows)
        with patch.object(self.store, "processes", side_effect=AssertionError("不得使用排行截断")):
            report = render_report(self.store, sid)
        summaries = re.findall(r"<summary>(.*?)</summary>", report)
        self.assertEqual(len(summaries), 106)
        self.assertEqual(len(set(summaries)), 106)
        for pid in range(1, 106):
            self.assertTrue(any(f"段 0 · PID {pid} · worker-{pid} ·" in item for item in summaries))
        self.assertIn("共 106 个进程身份，全部列出。", report)
        dma = next(block for block in re.findall(r"<details.*?</details>", report)
                   if "PID 999 · dma-only" in block)
        for metric in ("CPU（整机）", "RSS"):
            self.assertIn(f"<td>{metric}</td><td>0</td>" + "<td>—</td>" * 5, dma)
        self.assertIn("仅有 DP 记录，无 P 样本", dma)
        self.assertNotIn("进程 CPU ·", dma)
        self.assertNotIn("RSS ·", dma)
        self.assertIn("总 CPU ·", dma)

    def test_report_exact_identity_and_system_series_reuse(self):
        import re
        from unittest.mock import patch
        from perf_report import render_report

        rows = self.s(100) + self.p(100, cpu=11, rss_kb=101)
        rows += self.p(100, name="renamed", cpu=22, rss_kb=202)
        rows += self.p(100, pid=2, cpu=33, rss_kb=303)
        rows += self.s(1) + self.p(1, cpu=44, rss_kb=404)
        sid = self.load(rows)
        with patch.object(self.store, "report_series", wraps=self.store.report_series) as series:
            report = render_report(self.store, sid)
        blocks = re.findall(r"<details.*?</details>", report)
        expected = [(0, 1, "a", 11, 101), (0, 1, "renamed", 22, 202),
                    (0, 2, "a", 33, 303), (1, 1, "a", 44, 404)]
        self.assertEqual(len(blocks), len(expected))
        for block, (segment, pid, name, cpu, rss) in zip(blocks, expected):
            self.assertIn(f"段 {segment} · PID {pid} · {name} ·", block)
            self.assertIn("<td>CPU（整机）</td><td>1</td>" + f"<td>{cpu} %</td>" * 5, block)
            self.assertIn("<td>RSS</td><td>1</td>" + f"<td>{rss} KB</td>" * 5, block)
            self.assertIn(f" · {cpu} %</title>", block)
            self.assertIn(f" · {rss} KB</title>", block)
        self.assertEqual([call.kwargs for call in series.call_args_list if call.args[1] == "S"],
                         [dict(segment=0), dict(segment=1)])
        self.assertEqual([call.kwargs for call in series.call_args_list if call.args[1] == "P"],
                         [dict(segment=segment, pid=pid, name=name) for segment, pid, name, _, _ in expected])

    def test_report_zero_null_and_actual_cpu_plot_coordinates(self):
        import re
        import xml.etree.ElementTree as ET
        from perf_report import render_report

        rows = self.s(1).replace(",7,1,", ",7,40,") + self.p(1, cpu=20, cpu1c=960, rss_kb=0)
        rows += self.s(2).replace(",7,1,", ",7,40,") + self.p(2, cpu=0, cpu1c=800, rss_kb=0)
        rows += self.s(3).replace(",7,1,", ",7,40,") + self.p(3, cpu=999, rss_kb=999)
        sid = self.load(rows)
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.cpu',NULL,'$.rss_kb',NULL) "
                       "WHERE session=? AND kind='P' AND ts=3", (sid,))
        report = render_report(self.store, sid)
        self.assertIn("<td>CPU（整机）</td><td>2</td><td>0 %</td><td>10 %</td>" +
                      "<td>20 %</td>" * 3, report)
        self.assertIn("<td>RSS</td><td>2</td>" + "<td>0 KB</td>" * 5, report)
        cpu_svg, rss_svg = [ET.fromstring(svg) for svg in re.findall(r"<svg.*?</svg>", report)]
        process = [dot for dot in cpu_svg.findall("circle") if dot.findtext("title").startswith("进程 CPU ·")]
        system = [dot for dot in cpu_svg.findall("circle") if dot.findtext("title").startswith("总 CPU ·")]
        self.assertEqual([(dot.get("cx"), dot.get("cy")) for dot in process],
                         [("110.00", "142.50"), ("535.00", "230.00")])
        self.assertEqual([dot.get("cy") for dot in system], ["55.00"] * 3)
        self.assertEqual([dot.get("cy") for dot in rss_svg.findall("circle")], ["230.00"] * 2)
        self.assertEqual(len(cpu_svg.findall("line")), 3)
        self.assertEqual(len(rss_svg.findall("line")), 1)

    def test_report_series_metric_gaps_and_cycle_boundaries(self):
        import xml.etree.ElementTree as ET
        from perf_report import report_chart

        mutations = {
            "cpu_null": ("data=json_set(data,'$.cpu',NULL)", 3, 3, 5),
            "rss_null": ("data=json_set(data,'$.rss_kb',NULL)", 3, 5, 3),
            "missing_cycle": ("cycle=30", 3, 3, 3),
            "duplicate_cycle": ("cycle=2", 3, 3, 3),
            "equal_time": ("data=json_set(data,'$.ts',2)", 3, 4, 4),
            "backward_time": ("data=json_set(data,'$.ts',1)", 3, 4, 4),
        }
        for case, (assignment, ts, cpu_lines, rss_lines) in mutations.items():
            with self.subTest(case=case):
                sid = self.load("".join(self.s(i) + self.p(i, name=case, cpu=i, rss_kb=i)
                                       for i in range(1, 7)))
                with self.store.connect() as db:
                    db.execute("UPDATE records SET " + assignment +
                               " WHERE session=? AND kind='P' AND ts=?", (sid, ts))
                series = self.store.report_series(sid, "P", 0, pid=1, name=case)
                self.assertFalse(series["sampled"])
                self.assertEqual(series["total"], 6)
                points = series["points"]
                for field, expected in (("cpu", cpu_lines), ("rss_kb", rss_lines)):
                    chart = ET.fromstring(report_chart([(series, field, field)], field, ""))
                    self.assertEqual(len(chart.findall(".//line")), expected)
                    self.assertEqual(len(chart.findall(".//circle")),
                                     5 if case == field.replace("rss_kb", "rss") + "_null" else 6)
                if case in ("cpu_null", "rss_null"):
                    broken = "cpu" if case == "cpu_null" else "rss_kb"
                    intact = "rss_kb" if broken == "cpu" else "cpu"
                    self.assertNotEqual(points[1]["_run_" + broken], points[3]["_run_" + broken])
                    self.assertEqual(points[1]["_run_" + intact], points[3]["_run_" + intact])

    def test_report_downsampling_preserves_hidden_gaps_not_artificial_gaps(self):
        import re
        import xml.etree.ElementTree as ET
        from perf_report import render_report, report_chart

        sid = self.load("".join(self.s(i) + self.p(i, cpu=i, rss_kb=i) for i in range(1, 1002)))
        continuous = self.store.report_series(sid, "P", 0, pid=1, name="a", limit=4)
        self.assertTrue(continuous["sampled"])
        self.assertEqual(continuous["total"], 1001)
        self.assertGreaterEqual(len(continuous["points"]), 2)
        self.assertLessEqual(len(continuous["points"]), 4)
        self.assertTrue(any(b["cycle"] > a["cycle"] + 1
                            for a, b in zip(continuous["points"], continuous["points"][1:])))
        chart = ET.fromstring(report_chart([(continuous, "cpu", "CPU")], "CPU", "%"))
        self.assertEqual(len(chart.findall(".//line")), len(continuous["points"]) - 1)
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.cpu',NULL) "
                       "WHERE session=? AND kind='P' AND ts=501", (sid,))
            db.execute("UPDATE records SET data=json_set(data,'$.cpu_total',NULL) "
                       "WHERE session=? AND kind='S' AND ts=501", (sid,))
        gapped = self.store.report_series(sid, "P", 0, pid=1, name="a", limit=4)
        points = gapped["points"]
        self.assertTrue(all(point["cpu"] is not None for point in points))
        self.assertLess(points[0]["ts"], 501)
        self.assertGreater(points[-1]["ts"], 501)
        self.assertNotEqual(points[0]["_run_cpu"], points[-1]["_run_cpu"])
        self.assertEqual(points[0]["_run_rss_kb"], points[-1]["_run_rss_kb"])
        for field, count in (("cpu", len(points) - 2), ("rss_kb", len(points) - 1)):
            chart = ET.fromstring(report_chart([(gapped, field, field)], field, ""))
            self.assertEqual(len(chart.findall(".//line")), count)
        system = self.store.report_series(sid, "S", 0, limit=4)
        self.assertNotEqual(system["points"][0]["_run_cpu_total"],
                            system["points"][-1]["_run_cpu_total"])
        report = render_report(self.store, sid)
        self.assertIn("已降采样", report)
        self.assertIn("<td>CPU（整机）</td><td>1000</td><td>1 %</td><td>501 %</td>"
                      "<td>951 %</td><td>991 %</td><td>1,001 %</td>", report)
        for svg in re.findall(r"<svg.*?</svg>", report):
            root = ET.fromstring(svg)
            for label in ("进程 CPU", "总 CPU", "RSS"):
                dots = [dot for dot in root.findall("circle")
                        if dot.findtext("title").startswith(label + " ·")]
                self.assertLessEqual(len(dots), 600)
            # 缺失点位于时间轴中央；CPU 两条曲线都不能跨越它。
            if root.get("aria-label") == "进程 CPU + 总 CPU":
                for line in root.findall("line"):
                    self.assertFalse(float(line.get("x1")) < 535 < float(line.get("x2")))

    def test_report_chart_requires_same_segment_known_run_and_increasing_time(self):
        import xml.etree.ElementTree as ET
        from perf_report import report_chart

        first = dict(ts=1, cpu=10, segment=0, _run_cpu=1)
        cases = [(dict(ts=1000000, segment=0, _run_cpu=1), 1),
                 (dict(ts=2, segment=1, _run_cpu=1), 0),
                 (dict(ts=2, segment=0, _run_cpu=2), 0),
                 (dict(ts=2, segment=0, _run_cpu=None), 0),
                 (dict(ts=1, segment=0, _run_cpu=1), 0),
                 (dict(ts=0, segment=0, _run_cpu=1), 0)]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                series = dict(points=[first, dict(first, **changes)], total=2, sampled=False)
                chart = ET.fromstring(report_chart([(series, "cpu", "CPU")], "CPU", "%"))
                self.assertEqual(len(chart.findall(".//circle")), 2)
                self.assertEqual(len(chart.findall(".//line")), expected)

    def test_report_offline_markup_and_untrusted_text(self):
        from html import escape
        from html.parser import HTMLParser
        from perf_report import render_report

        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tags = []
                self.styles = []
                self.in_style = False

            def handle_starttag(self, tag, attrs):
                self.tags.append((tag, dict(attrs)))
                if tag == "style":
                    self.in_style = True

            def handle_endtag(self, tag):
                if tag == "style":
                    self.in_style = False

            def handle_data(self, data):
                if self.in_style:
                    self.styles.append(data)

        payload = '</summary><script>alert("x")</script><img src="https://example.invalid/x" onerror="alert(1)">&'
        sid = self.load(self.p(1, name=payload))
        with self.store.connect() as db:
            db.execute("UPDATE sessions SET name=? WHERE id=?", (payload, sid))
        report = render_report(self.store, sid)
        self.assertNotIn(payload, report)
        self.assertGreaterEqual(report.count(escape(payload, quote=True)), 3)
        markup = Markup()
        markup.feed(report)
        tags = [tag for tag, _ in markup.tags]
        self.assertEqual(tags.count("details"), 1)
        self.assertEqual(tags.count("summary"), 1)
        self.assertIn("svg", tags)
        forbidden = {"script", "link", "iframe", "img", "object", "embed", "base", "form",
                     "audio", "video", "source", "foreignobject", "use", "image", "animate", "set"}
        self.assertFalse(forbidden.intersection(tags))
        for tag, attrs in markup.tags:
            self.assertFalse(any(key.startswith("on") for key in attrs), (tag, attrs))
            self.assertFalse({"src", "srcset", "href", "xlink:href", "srcdoc", "action"}.intersection(attrs))
            if tag == "meta":
                self.assertNotEqual(attrs.get("http-equiv", "").lower(), "refresh")
        policies = [attrs["content"] for tag, attrs in markup.tags
                    if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy"]
        self.assertEqual(policies, ["default-src 'none'; style-src 'unsafe-inline'"])
        self.assertNotRegex("".join(markup.styles).lower(), r"url\s*\(|@import|expression\s*\(")

    def test_report_only_dp_without_system_does_not_fabricate_samples(self):
        from perf_report import render_report

        sid = self.load("DP,1,8,1024,1,dma\n")
        report = render_report(self.store, sid)
        self.assertIn("共 1 个进程身份，全部列出。", report)
        self.assertIn("仅有 DP 记录，无 P 样本", report)
        self.assertNotIn("<svg", report)
        for metric in ("CPU（整机）", "RSS"):
            self.assertIn(f"<td>{metric}</td><td>0</td>" + "<td>—</td>" * 5, report)

    def test_member_resources_independent_exact_scope_and_nulls(self):
        rows = self.s(1000) + self.p(1000, cpu1c=50, rss_kb=10, rd_kb=2)
        rows += self.p(1000, 2, "b", cpu1c=20)
        rows += self.s(1001) + self.p(1001, cpu1c=100, rss_kb=30, rd_kb=4)
        rows += self.s(1002) + self.p(1002, cpu1c=999) * 2
        rows += self.p(1002, 2, "b", cpu1c=40)
        rows += self.s(1003) + self.p(1003, name="renamed", cpu1c=999)
        rows += self.p(1003, 2, "b", cpu1c=60) + "DP,1003,3,10,1,dma\n"
        rows += self.s(1004) + self.p(1004, cpu1c=999)
        rows += self.s(1000) + self.p(1000, cpu1c=999)
        sid = self.load(rows)
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.rss_kb',NULL) "
                       "WHERE session=? AND pid=2", (sid,))
        group = self.store.save_group(sid, self.config(start=1000, end=1003,
            members=[dict(pid=1, name="a"), dict(pid=2, name="b"), dict(pid=3, name="dma")]))
        self.store.save_report_settings(sid, dict(cpu_platform="test", kdmips_per_core=28.75))
        result = self.store.group_resources(sid, group["id"])
        a, b, dma = result["rows"]
        self.assertEqual(a["statistics"]["cpu1c"],
                         dict(count=2, min=50, avg=75, max=100, p95=100, p99=100))
        self.assertEqual(a["kdmips"], dict(min=14.375, avg=21.5625, p95=28.75, p99=28.75, max=28.75))
        self.assertEqual(a["statistics"]["rd_kb"]["total"], 6)
        self.assertEqual(b["statistics"]["cpu1c"]["count"], 3)
        self.assertEqual(b["statistics"]["cpu1c"]["avg"], 40)
        self.assertEqual(b["statistics"]["rss_kb"],
                         dict(count=0, min=None, avg=None, max=None, p95=None, p99=None))
        self.assertEqual(dma["statistics"]["rd_kb"]["count"], 0)
        self.assertIsNone(dma["statistics"]["rd_kb"]["total"])
        self.assertTrue(all(v is None for v in dma["kdmips"].values()))
        self.assertEqual(self.store.group_analysis(sid, group["id"])["coverage"]["complete"], 0)
        self.store.save_report_settings(sid, {})
        self.assertTrue(all(v is None for v in self.store.group_resources(sid, group["id"])["rows"][0]["kdmips"].values()))
        self.store.save_group(sid, self.config(start=1001, end=1001), group["id"])
        self.assertEqual(self.store.group_resources(sid, group["id"])["rows"][0]["statistics"]["cpu1c"]["min"], 100)
        self.store.save_group(sid, self.config(start=2000, end=2001), group["id"])
        self.assertEqual(self.store.group_resources(sid, group["id"])["rows"][0]["statistics"]["cpu1c"]["count"], 0)

    def test_member_resources_nearest_rank_and_report_scenes(self):
        from perf_report import member_resources_table, table
        sid = self.load("".join(self.p(i, cpu1c=i, rss_kb=i) for i in range(1, 101)))
        group = self.store.save_group(sid, self.config(scene="后台", members=[dict(
            pid=1, name="a", service_category="应用服务", related_service="<unsafe>&", foreground="Y")]))
        self.store.save_report_settings(sid, dict(cpu_platform="<platform>", kdmips_per_core=28.75))
        resources = self.store.group_resources(sid, group["id"])
        self.assertEqual(resources["rows"][0]["statistics"]["cpu1c"],
                         dict(count=100, min=1, avg=50.5, max=100, p95=95, p99=99))
        report = member_resources_table(resources)
        self.assertEqual(resources["settings"]["cpu_platform"], "<platform>")
        self.assertIn("&lt;platform&gt;", table(["平台"], [[resources["settings"]["cpu_platform"]]]))
        self.assertIn("&lt;unsafe&gt;&amp;", report)
        self.assertNotIn("<unsafe>", report)
        self.assertIn("应用服务", report)
        self.assertIn("28.462 K", report)
        for scene, index in (("前台", 0), ("后台", 1), ("未标注", 2)):
            resources["group"]["scene"] = scene
            cells = ["—"] * 3
            cells[index] = "1 % / 50.5 % / 95 % / 99 % / 100 %"
            self.assertIn("<td>100</td>" + "".join("<td>" + c + "</td>" for c in cells),
                          member_resources_table(resources))

    def test_resource_settings_validation_and_legacy_members(self):
        import json
        from perf_metrics import to_kdmips
        sid = self.load(self.p(1))
        for factor in (0, -1, True, "28.75", float("nan"), float("inf"), 10**1000):
            with self.subTest(factor=repr(factor)), self.assertRaises(ValueError):
                self.store.save_report_settings(sid, dict(cpu_platform="test", kdmips_per_core=factor))
        for settings in ({"kdmips_per_core": 1}, {"cpu_platform": " ", "kdmips_per_core": 1},
                         {"cpu_platform": "a" * 121}, {"cpu_platform": None}, []):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.store.save_report_settings(sid, settings)
        self.assertEqual(to_kdmips(50, 28.75), 14.375)
        self.assertIsNone(to_kdmips(50, None))
        self.assertIsNone(to_kdmips(1e308, 1e308))
        for changes in (dict(service_category="invalid"), dict(service_category=[]),
                        dict(related_service="x" * 2001), dict(related_service=None)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.save_group(sid, self.config(members=[dict(pid=1, name="a", **changes)]))
        group = self.store.save_group(sid, self.config(members=[dict(pid=1, name="a")]))
        with self.store.connect() as db:
            legacy = json.loads(db.execute("SELECT config FROM process_groups WHERE id=?", (group["id"],)).fetchone()[0])
            for member in legacy["members"]:
                member.pop("service_category")
                member.pop("related_service")
            encoded = json.dumps(legacy)
            db.execute("UPDATE process_groups SET config=? WHERE id=?", (encoded, group["id"]))
        old_settings = dict(hardware="旧设备")
        self.store.save_report_settings(sid, old_settings)
        reopened = Store(self.store.path)
        self.assertEqual(reopened.report_settings(sid), old_settings)
        self.assertEqual(reopened.group(sid, group["id"]), group)
        self.assertEqual(reopened.groups(sid), [group])
        result = reopened.group_resources(sid, group["id"])
        self.assertEqual(result["settings"], dict(cpu_platform="", kdmips_per_core=None))
        self.assertEqual(result["rows"][0]["member"]["service_category"], "未分类")
        with reopened.connect() as db:
            self.assertEqual(db.execute("SELECT config FROM process_groups WHERE id=?", (group["id"],)).fetchone()[0], encoded)

    def test_report_manual_fields_escaped_and_single_member(self):
        from perf_report import manual_section, module_details, render_report, table, text

        fields = ("hardware", "software", "tester", "test_notes", "worst_scenarios",
                  "analysis", "criteria", "conclusion", "conclusion_notes")
        payload = '<script>alert("unsafe & \'quoted\'")</script>'
        sid = self.load(self.s(1) + self.p(1, name=payload))
        baseline = render_report(self.store, sid)
        settings = {field: field + payload for field in fields}
        self.store.save_report_settings(sid, settings)
        group = self.store.save_group(sid, self.config(name="group" + payload, description="desc" + payload,
                              scene="前台", start=1, end=1, members=[dict(pid=1, name=payload,
                              process_type="type" + payload, purpose="purpose" + payload,
                              foreground="Y", background="N")]))
        self.assertEqual(self.store.report_settings(sid), settings)
        self.assertEqual(self.store.group(sid, group["id"]), group)
        manual = "".join(manual_section(settings, field, field) for field in fields)
        for value in settings.values():
            self.assertIn(text(value), manual)
            self.assertNotIn(value, manual)
        member = group["members"][0]
        metadata = table(["组", "说明", "类型", "用途", "前台", "后台"], [[
            group["name"], group["description"], member["process_type"], member["purpose"],
            member["foreground"], member["background"]]])
        for prefix in ("group", "desc", "type", "purpose"):
            self.assertIn(text(prefix + payload), metadata)
            self.assertNotIn(prefix + payload, metadata)
        self.assertIn("<td>Y</td><td>N</td>", metadata)
        analyses = {kind: self.store.group_analysis(sid, group["id"], kind) for kind in ("P", "DP")}
        self.assertIn("单成员组", module_details(group, analyses))
        self.assertEqual(analyses["P"]["statistics"]["samples"], 1)
        self.assertIn(text(payload), baseline)
        self.assertNotIn("<script", baseline.lower())
        self.assertEqual(render_report(self.store, sid), baseline)
        for conclusion in ("待评估", "准入", "有条件准入", "不准入"):
            saved = self.store.save_report_settings(sid, dict(conclusion=conclusion))
            self.assertEqual(self.store.report_settings(sid), saved)
            self.assertIn("<p class='manual'>" + conclusion + "</p>",
                          manual_section(saved, "conclusion", "结论"))
            self.assertEqual(render_report(self.store, sid), baseline)

    def test_report_group_offset_peaks_k_and_analysis_reuse(self):
        from unittest.mock import patch
        from perf_report import module_details, metric_value

        rows = "".join(self.s(i) + self.p(i, cpu=i, cpu1c=8*i, rss_kb=i*1024) +
                       self.p(i, 2, "b", cpu=101-i, cpu1c=8*(101-i), rss_kb=(101-i)*1024) +
                       f"DP,{i},1,{i*1024},1,a\nDP,{i},2,{(101-i)*1024},2,b\n"
                       for i in range(1, 101))
        sid = self.load(rows)
        group = self.store.save_group(sid, self.config())
        self.store.save_report_settings(sid, dict(cpu_platform="测试平台", kdmips_per_core=28.75))
        analyses = {kind: self.store.group_analysis(sid, group["id"], kind) for kind in ("P", "DP")}
        factor = self.store.report_settings(sid)["kdmips_per_core"]
        with patch.object(self.store, "group_analysis", wraps=self.store.group_analysis) as analysis:
            module = module_details(group, analyses, factor)
        analysis.assert_not_called()
        report = module
        self.assertIn("<td>808 %</td><td>808 %</td><td>232.3 K</td><td>232.3 K</td>"
                      "<td>808 %</td><td>232.3 K</td><td>101 MB</td>", module)
        self.assertNotIn("808 K", report)
        self.assertNotIn("200 %", module)
        self.assertIn("100 个范围周期", module)
        self.assertNotIn("条原始记录", module)
        for phrase in ("CPU cpu1c", "读取 rd 周期增量", "写入 wr 周期增量", "DP dmabuf", "DP 对象数",
                       "I/O MB/s 无法可靠换算"):
            self.assertIn(phrase, report)
        overlap = self.store.save_group(sid, self.config(name="重叠组"))
        self.assertEqual(self.store.group_analysis(sid, overlap["id"])["statistics"]["metrics"],
                         analyses["P"]["statistics"]["metrics"])
        self.assertEqual(metric_value(analyses["P"], "cpu1c", "max", "%"), "808 %")
        self.assertEqual(metric_value(analyses["P"], "cpu1c", "max", "K", factor), "232.3 K")
        self.assertEqual(metric_value(analyses["P"], "rss_kb", "max"), "101 MB")
        self.assertEqual(metric_value(analyses["DP"], "size_kb", "max"), "101 MB")

    def test_report_low_coverage_null_duplicate_and_empty_scope(self):
        from perf_report import module_details

        rows = self.s(1) + self.p(1) + self.p(1, 2, "b") + "DP,1,1,100,2,a\nDP,1,2,200,3,b\n"
        rows += self.s(2) + self.p(2) + self.s(3)
        rows += self.s(4) + self.p(4) + self.p(4) + self.p(4, 2, "b") + "DP,4,1,50,1,a\n"
        rows += self.s(5) + self.p(5) + self.p(5, 2, "b")
        sid = self.load(rows)
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.cpu',NULL) "
                       "WHERE session=? AND ts=5 AND pid=2", (sid,))
        group = self.store.save_group(sid, self.config())
        analyses = {kind: self.store.group_analysis(sid, group["id"], kind) for kind in ("P", "DP")}
        module = module_details(group, analyses)
        self.assertIn("<td>P</td><td>5</td><td>2</td><td>2</td><td>1</td><td>1</td><td>40 %</td>", module)
        self.assertIn("<td>DP</td><td>5</td><td>1</td><td>1</td><td>3</td><td>0</td><td>20 %</td>", module)
        self.assertIn("<td>cpu</td><td>1</td><td>5</td><td>20 %</td>", module)
        self.assertIn("<td>rss_kb</td><td>2</td><td>5</td><td>40 %</td>", module)
        self.assertIn("低覆盖率风险", module)
        self.assertIn("完整周期仅代表成员记录齐全", module)
        self.assertIn("5 个范围周期", module)
        self.assertNotIn("条原始记录", module)
        group = self.store.save_group(sid, self.config(start=10, end=20), group["id"])
        analyses = {kind: self.store.group_analysis(sid, group["id"], kind) for kind in ("P", "DP")}
        empty = module_details(group, analyses)
        self.assertIn("范围内无周期，待评估", empty)
        self.assertIn("0 个范围周期；— → —", empty)
        self.assertIn("无对应样本，不补零", empty)

    def test_cycle_sum_before_peak_percentiles_and_increments(self):
        text = "".join(self.s(i) + self.p(i, cpu=i, cpu1c=8*i, rss_delta_kb=-10) +
                       self.p(i, 2, "b", cpu=101-i, cpu1c=8*(101-i), rss_delta_kb=3)
                       for i in range(1, 101))
        sid = self.load(text)
        group = self.store.save_group(sid, self.config())
        result = self.store.group_analysis(sid, group["id"])
        stats = result["statistics"]
        self.assertEqual(stats["samples"], 100)
        cpu = stats["metrics"]["cpu"]
        self.assertEqual([cpu[k] for k in ("count", "min", "avg", "max", "p95", "p99")],
                         [100, 101, 101, 101, 101, 101])
        self.assertEqual(stats["metrics"]["cpu1c"]["max"], 808)
        self.assertEqual(stats["metrics"]["rss_delta_kb"]["total"], -700)
        self.assertNotIn("wait", stats["metrics"])
        self.assertEqual(stats["categories"], {})
        self.assertNotIn("wait", result["series"]["points"][0])
        self.assertEqual(result["coverage"], dict(cycles=100, complete=100, partial=0, missing=0, duplicates=0))
        group = self.store.save_group(sid, self.config(members=[dict(pid=1, name="a")]), group["id"])
        cpu = self.store.group_analysis(sid, group["id"])["statistics"]["metrics"]["cpu"]
        self.assertEqual((cpu["avg"], cpu["p95"], cpu["p99"]), (50.5, 95, 99))
        self.assertEqual(self.store.statistics(sid, "P", pid=1)["metrics"]["wait"]["count"], 100)

    def test_missing_duplicate_null_and_low_frequency_dp(self):
        text = self.s(1) + self.p(1) + self.p(1, 2, "b")
        text += "DP,1,1,100,2,a\nDP,1,2,200,3,b\n"
        text += self.s(2) + self.p(2)
        text += self.s(3)
        text += self.s(4) + self.p(4) + self.p(4) + self.p(4, 2, "b")
        text += "DP,4,1,50,1,a\n"
        text += self.s(5) + self.p(5) + self.p(5, 2, "b")
        sid = self.load(text)
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.cpu',NULL) "
                       "WHERE session=? AND ts=5 AND pid=2", (sid,))
        group = self.store.save_group(sid, self.config())
        result = self.store.group_analysis(sid, group["id"])
        self.assertEqual(result["coverage"], dict(cycles=5, complete=2, partial=2, missing=1, duplicates=1))
        self.assertEqual(result["statistics"]["metrics"]["cpu"]["count"], 1)
        self.assertEqual(result["statistics"]["metrics"]["rss_kb"]["count"], 2)
        for point in result["series"]["points"][1:]:
            self.assertIsNone(point["cpu"])
        dp = self.store.group_analysis(sid, group["id"], "DP")
        self.assertEqual(dp["coverage"], dict(cycles=5, complete=1, partial=1, missing=3, duplicates=0))
        self.assertEqual(dp["statistics"]["metrics"]["size_kb"]["avg"], 300)
        self.assertEqual(dp["statistics"]["metrics"]["objs"]["max"], 5)
        self.assertIsNone(dp["series"]["points"][3]["size_kb"])


    def test_time_segment_and_exact_identity(self):
        text = self.p(10) + self.p(10, 2, "b") + self.s(20) + self.p(20, 1, "renamed")
        text += self.p(20, 2, "b") + self.s(30) + self.p(30) + self.p(30, 2, "b")
        text += self.s(10) + self.p(10, cpu=999) + self.p(10, 2, "b", cpu=999)
        sid = self.load(text)
        group = self.store.save_group(sid, self.config(start=10, end=20))
        result = self.store.group_analysis(sid, group["id"])
        self.assertEqual(result["coverage"], dict(cycles=2, complete=1, partial=1, missing=0, duplicates=0))
        self.assertEqual((result["statistics"]["start"], result["statistics"]["end"]), (10, 20))
        self.assertLess(result["statistics"]["metrics"]["cpu"]["max"], 999)
        self.store.save_group(sid, self.config(start=21, end=29), group["id"])
        empty = self.store.group_analysis(sid, group["id"])
        self.assertEqual(empty["coverage"]["cycles"], 0)
        self.assertEqual(empty["series"], dict(points=[], total=0, sampled=False))
        self.assertIsNone(empty["statistics"]["metrics"]["rss_delta_kb"]["total"])
        dp_sid = self.load("DP,1,1,100,2,a\nDP,1,2,200,3,b\n")
        dp_group = self.store.save_group(dp_sid, self.config())
        self.assertEqual(self.store.group_analysis(dp_sid, dp_group["id"])["coverage"]["missing"], 1)

    def test_crud_settings_persistence_and_cascade(self):
        import sqlite3
        import uuid
        sid = self.load(self.p(1) + self.p(1, 2, "b"))
        self.assertEqual(self.store.groups(sid), [])
        self.assertEqual(self.store.report_settings(sid), {})
        group = self.store.save_group(sid, self.config())
        uuid.UUID(group["id"])
        self.assertEqual(group["members"][0]["foreground"], "待填写")
        self.assertEqual(self.store.group(sid, group["id"]), group)
        settings = dict(title="报告", nested=dict(groups=[group["id"]]))
        self.assertEqual(self.store.save_report_settings(sid, settings), settings)
        reopened = Store(self.store.path)
        self.assertEqual(reopened.report_settings(sid), settings)
        self.assertEqual(reopened.groups(sid), [group])
        replacement = self.store.save_group(sid, self.config(name="修改", scene="前台"), group["id"])
        self.assertEqual(replacement["id"], group["id"])
        self.assertEqual(reopened.group(sid, group["id"])["name"], "修改")
        self.assertTrue(self.store.delete_group(sid, group["id"]))
        self.assertFalse(self.store.delete_group(sid, group["id"]))
        with self.assertRaises(KeyError):
            self.store.group(sid, group["id"])
        self.store.save_group(sid, self.config())
        self.assertTrue(self.store.delete(sid))
        with self.store.connect() as db:
            for table in ("process_groups", "report_settings"):
                self.assertEqual(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO process_groups VALUES ('missing','id','{}')")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO report_settings VALUES ('missing','{}')")
        operations = [lambda: self.store.groups(sid), lambda: self.store.group(sid, "x"),
                      lambda: self.store.save_group(sid, self.config()),
                      lambda: self.store.delete_group(sid, "x"), lambda: self.store.report_settings(sid),
                      lambda: self.store.save_report_settings(sid, {}),
                      lambda: self.store.group_analysis(sid, "x")]
        for operation in operations:
            with self.assertRaises(KeyError):
                operation()


    def test_validation_and_session_group_limit(self):
        sid = self.load(self.p(1) + self.p(1, 2, "b"))
        invalid = [dict(name=""), dict(segment=1), dict(segment=True), dict(members=[]),
                   dict(members=[dict(pid=1, name="a")] * 51),
                   dict(members=[dict(pid=1, name="a")] * 2),
                   dict(members=[dict(pid=1, name="b")]), dict(start=3, end=2),
                   dict(start=True), dict(scene="未知"), dict(description=[]),
                   dict(members=[dict(pid=1, name="a", foreground="yes")]),
                   dict(members=[dict(pid=1, name="a", purpose=3)]),
                   dict(members=[dict(pid=1, name="a", background=[])]),
                   dict(members=[dict(pid=True, name="a")])]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.save_group(sid, self.config(**changes))
        with self.assertRaises(KeyError):
            self.store.save_group(sid, self.config(), "unknown")
        groups = [self.store.save_group(sid, self.config(name=str(i))) for i in range(20)]
        with self.assertRaises(ValueError):
            self.store.save_group(sid, self.config())
        self.store.save_group(sid, self.config(name="replace at cap"), groups[0]["id"])
        self.assertEqual(len(self.store.groups(sid)), 20)
        other = self.load(self.p(2) + self.p(2, 2, "b"))
        with self.assertRaises(KeyError):
            self.store.save_group(other, self.config(), groups[0]["id"])
        self.assertFalse(self.store.delete_group(other, groups[0]["id"]))
        for kind, limit in (("S", 600), ("P", 0), ("P", True)):
            with self.assertRaises(ValueError):
                self.store.group_analysis(sid, groups[0]["id"], kind, limit)

    def test_sample_nulls_extrema_and_hard_limit(self):
        text = "".join(self.s(i) + (self.p(i, cpu=99 if i == 451 else 1) +
                       self.p(i, 2, "b", cpu=1) if i % 5 else "") for i in range(1000))
        sid = self.load(text)
        group = self.store.save_group(sid, self.config())
        for limit in (1, 2, 10, 60, 600):
            series = self.store.group_analysis(sid, group["id"], limit=limit)["series"]
            self.assertTrue(series["sampled"])
            self.assertLessEqual(len(series["points"]), limit)
            self.assertEqual(series["total"],1000)
            self.assertEqual(max(p["cpu"] for p in series["points"] if p["cpu"] is not None), 100)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analysis_cache").fetchone()[0], 0)

    def test_duplicate_does_not_replace_member_or_join_same_timestamp(self):
        sid = self.load("DP,1,1,100,2,a\nDP,1,1,200,3,a\nDP,2,2,300,4,b\nD,3,1,1\n")
        # Model separate cycles sharing a timestamp; cycle, not ts, is the join key.
        with self.store.connect() as db:
            db.execute("UPDATE records SET ts=1,data=json_set(data,'$.ts',1) "
                       "WHERE session=? AND cycle=2", (sid,))
        group = self.store.save_group(sid, self.config())
        result = self.store.group_analysis(sid, group["id"], "DP")
        self.assertEqual(result["coverage"], dict(cycles=3, complete=0, partial=2, missing=1, duplicates=1))
        self.assertEqual(result["statistics"]["metrics"]["size_kb"]["count"], 0)
        self.assertTrue(all(p["size_kb"] is None for p in result["series"]["points"]))
        with self.store.connect() as db:
            db.execute("UPDATE records SET cycle=1 WHERE session=? AND cycle=2", (sid,))
        result = self.store.group_analysis(sid, group["id"], "DP")
        self.assertEqual(result["coverage"], dict(cycles=2, complete=0, partial=1, missing=1, duplicates=1))
        self.assertIsNone(result["statistics"]["metrics"]["size_kb"]["max"])

    def test_fifty_members_and_explicit_identity_only(self):
        text = "".join(self.p(1, i, "shared-name", cpu=1) for i in range(50))
        text += self.p(1, 1, "renamed", cpu=1)
        sid = self.load(text)
        members = [dict(pid=i, name="shared-name") for i in range(50)]
        group = self.store.save_group(sid, self.config(members=members))
        result = self.store.group_analysis(sid, group["id"])
        self.assertEqual(result["statistics"]["metrics"]["cpu"]["max"], 50)
        group = self.store.save_group(sid, self.config(members=[dict(pid=1, name="shared-name"),
                                     dict(pid=1, name="renamed")]), group["id"])
        self.assertEqual(self.store.group_analysis(sid, group["id"])["coverage"]["complete"], 1)

    def test_large_group_totals_do_not_overflow(self):
        text = "".join(self.p(i, rd_kb=2**64-1) + self.p(i, 2, "b", rd_kb=2**64-1)
                       for i in (1, 2))
        sid = self.load(text)
        group = self.store.save_group(sid, self.config())
        metric = self.store.group_analysis(sid, group["id"])["statistics"]["metrics"]["rd_kb"]
        self.assertEqual(metric["total"], float(4 * (2**64-1)))


class ApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from perf_api import create_app
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(Path(self.temp.name) / "api.sqlite3")
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8765")
        self.addCleanup(self.client.close)
        self.headers = {"X-Session-Token": self.client.get("/api/token").json()["token"]}

    def upload(self, text=SAMPLE, title="接口测试"):
        return self.client.post("/api/import", headers=self.headers,
                                files=[("files", ("perf.log", text.encode(), "text/plain"))], data={"title": title})

    def test_end_to_end(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        sid = response.json()["id"]
        self.assertTrue(self.upload().json()["duplicate"])
        base = "/api/sessions/" + sid
        self.assertEqual(len(self.client.get("/api/sessions", headers=self.headers).json()), 1)
        self.assertEqual(self.client.get(base, headers=self.headers).json()["stats"]["cpu_peak"], 42.04)
        for kind, count in (("S", 1), ("P", 1), ("D", 1), ("DE", 2), ("DP", 1)):
            result = self.client.get(base + "/series", params={"kind": kind, "segment": 0}, headers=self.headers)
            self.assertEqual(result.json()["total"], count)
            stats = self.client.get(base + "/statistics", params={"kind": kind, "segment": 0}, headers=self.headers)
            self.assertEqual(stats.status_code, 200, stats.text)
            self.assertEqual(stats.json()["samples"], count)
            self.assertIn("p99", next(iter(stats.json()["metrics"].values())))
        for sort in ("cpu_p95", "cpu_p99"):
            self.assertEqual(self.client.get(base + "/processes", params={"sort": sort}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(base + "/processes", headers=self.headers).status_code, 200)
        filtered = self.client.get(base + "/processes", params={"q": "ENGMODE", "with_total": True}, headers=self.headers)
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual((filtered.json()["total"], filtered.json()["all_total"]), (1, 2))
        self.assertEqual(filtered.json()["items"][0]["pid"], 5589)
        self.assertEqual(self.client.get(base + "/processes", params={"q": "x" * 257}, headers=self.headers).status_code, 422)
        report = self.client.get(base + "/report", headers=self.headers)
        self.assertEqual(report.status_code, 200)
        self.assertIn("<svg", report.text)
        self.assertIn("attachment", report.headers["content-disposition"])
        self.assertEqual(self.client.delete(base, headers=self.headers).json(), {"deleted": True})
        self.assertEqual(self.client.get(base, headers=self.headers).status_code, 404)

    def test_security_boundary(self):
        self.assertEqual(self.client.get("/api/sessions").status_code, 403)
        for headers in ({"Host": "evil.example:8765"}, {"Origin": "https://evil.example"},
                        {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Origin": "http://localhost:5173"}):
            self.assertEqual(self.client.get("/api/token", headers=headers).status_code, 403)
        response = self.client.get("/api/token", headers={"Origin": "http://127.0.0.1:8765"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_upload_validation(self):
        from unittest.mock import patch
        self.assertEqual(self.upload("garbage\n").status_code, 400)
        self.assertEqual(self.upload(title="x" * 121).status_code, 400)
        files = [("files", (f"perf.{i}.log", SAMPLE.encode())) for i in range(6)]
        self.assertEqual(self.client.post("/api/import", files=files, headers=self.headers).status_code, 400)
        with patch("perf_api.MAX_FILE_BYTES", 10):
            self.assertEqual(self.upload().status_code, 413)
        with patch("perf_api.MAX_BODY_BYTES", 10):
            self.assertEqual(self.upload().status_code, 413)
        self.assertEqual(self.client.get("/api/sessions", headers=self.headers).json(), [])

    def test_local_upload_size_boundary(self):
        from unittest.mock import patch
        from perf_api import MAX_FILE_BYTES, MAX_BODY_BYTES
        from perf_adb import MAX_FILE_BYTES as ADB_FILE_BYTES

        self.assertEqual(MAX_FILE_BYTES, 1024 ** 3)
        self.assertEqual(MAX_BODY_BYTES, 5 * MAX_FILE_BYTES + 1024 ** 2)
        self.assertEqual(ADB_FILE_BYTES, 50 * 1024 ** 2)
        # 缩小阈值验证真实 multipart 边界，不分配 GB 级测试数据。
        size = len(SAMPLE.encode())
        with patch("perf_api.MAX_FILE_BYTES", size - 1):
            response = self.upload()
            self.assertEqual(response.status_code, 413)
            self.assertIn("1 GB", response.json()["detail"])
        self.assertEqual(self.client.get("/api/sessions", headers=self.headers).json(), [])
        with patch("perf_api.MAX_FILE_BYTES", size):
            response = self.upload()
            self.assertEqual(response.status_code, 200, response.text)

    def test_report_escapes_untrusted_text(self):
        payload = '<script>alert("unsafe")</script>'
        result = self.upload(SAMPLE.replace("com.desaysv.engmode", payload), title=payload)
        report = self.client.get("/api/sessions/" + result.json()["id"] + "/report", headers=self.headers)
        self.assertNotIn(payload, report.text)
        self.assertIn("&lt;script&gt;", report.text)
        self.assertNotIn("<script", report.text)

    def test_chunked_limit_closes_files_and_releases_lock(self):
        import asyncio
        from unittest.mock import patch
        opened = []
        factory = tempfile.SpooledTemporaryFile

        def tracked_file(*args, **kwargs):
            file = factory(*args, **kwargs)
            opened.append(file)
            return file

        async def upload():
            chunks = iter([
                b'--test\r\nContent-Disposition: form-data; name="files"; filename="perf.log"\r\n\r\n',
                b'x' * 512,
            ])
            messages = []

            async def receive():
                return {"type": "http.request", "body": next(chunks), "more_body": True}

            async def send(message):
                messages.append(message)

            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": "POST", "scheme": "http", "path": "/api/import",
                     "raw_path": b"/api/import", "query_string": b"",
                     "headers": [(b"host", b"127.0.0.1:8765"),
                                 (b"content-type", b"multipart/form-data; boundary=test"),
                                 (b"x-session-token", self.headers["X-Session-Token"].encode())],
                     "client": ("127.0.0.1", 10000), "server": ("127.0.0.1", 8765)}
            await self.app(scope, receive, send)
            return messages

        with patch("perf_api.MAX_BODY_BYTES", 256), patch("starlette.formparsers.SpooledTemporaryFile", tracked_file):
            messages = asyncio.run(upload())
        self.assertEqual(messages[0]["status"], 413)
        self.assertTrue(opened)
        self.assertTrue(all(file.closed for file in opened))
        self.assertEqual(self.app.state.store.sessions(), [])
        self.assertEqual(self.upload().status_code, 200)

    def test_import_concurrency(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        entered, release = threading.Event(), threading.Event()
        original = self.app.state.store.import_files

        def blocked(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("import test timed out")
            return original(*args)

        with patch.object(self.app.state.store, "import_files", blocked), ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.upload)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.upload().status_code, 409)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).status_code, 200)

    def test_development_origin_and_restart_token(self):
        from fastapi.testclient import TestClient
        from perf_api import create_app
        app = create_app(Path(self.temp.name) / "api.sqlite3", development=True)
        with TestClient(app, base_url="http://localhost:5173") as client:
            self.assertEqual(client.get("/api/token", headers={"Origin": "http://localhost:5173"}).status_code, 200)
            self.assertEqual(client.get("/api/token", headers={"Origin": "http://evil.example"}).status_code, 403)
            self.assertEqual(client.get("/api/sessions", headers=self.headers).status_code, 403)

    def test_real_http_cli_and_static_assets(self):
        import os
        import re
        import socket
        import subprocess
        import sys
        import time
        import httpx
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "main.py", "--port", str(port), "--database",
             str(Path(self.temp.name) / "smoke.sqlite3")],
            cwd=Path(__file__).parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252:strict"})
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        root = client.get("/")
                        break
                    except httpx.ConnectError:
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail("本地服务启动失败")
                        time.sleep(0.1)
                self.assertEqual(root.status_code, 200)
                assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', root.text)
                self.assertTrue(assets)
                for asset in assets:
                    self.assertEqual(client.get(asset).status_code, 200)
                self.assertEqual(client.get("/api/sessions").status_code, 403)
                client.headers["X-Session-Token"] = client.get("/api/token").json()["token"]
                imported = client.post("/api/import", files={"files": ("perf.log", SAMPLE.encode())})
                self.assertEqual(imported.status_code, 200)
                base = "/api/sessions/" + imported.json()["id"]
                self.assertEqual(client.get(base).json()["stats"]["cpu_peak"], 42.04)
                for kind in ("S", "P", "D", "DE", "DP"):
                    self.assertGreater(client.get(base + "/series", params={"kind": kind}).json()["total"], 0)
                self.assertTrue(client.get(base + "/processes").json())
                report = client.get(base + "/report")
                self.assertEqual(report.status_code, 200)
                self.assertIn("<svg", report.text)
                self.assertEqual(client.delete(base).status_code, 200)
                self.assertEqual(client.get("/api/sessions").json(), [])
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def test_query_validation(self):
        sid = self.upload().json()["id"]
        base = "/api/sessions/" + sid
        for path in ("/series?kind=Z", "/series?kind=S&limit=0", "/series?kind=S&pid=999999999999999999999",
                     "/processes?sort=bad", "/processes?offset=-1", "/statistics?kind=Z",
                     "/statistics?kind=P&segment=-1", "/statistics?kind=P&pid=999999999999999999999"):
            self.assertEqual(self.client.get(base + path, headers=self.headers).status_code, 422)

