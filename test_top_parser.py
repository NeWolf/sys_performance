import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from perf_parser import MAX_LINE_BYTES, iter_records
from perf_store import Store
from perf_top_parser import MAX_SAMPLE_BYTES, detect_source_format, iter_top_records
from test_perf import ReportAssertions, SAMPLE


CPU = "800%cpu 160%user 0%nice 80%sys 520%idle 16%iow 8%irq 16%sirq\n"
MEM = "Mem: 14725M total, 5399M used, 9326M free\n"
TABLE = "PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ ARGS\n"
ROW = "42 root 20 0 2G 1.5M 0 S 160 0.1 0:01 app  --flag value\n"


def header(index=1, date="2026-07-01 12:00:00", zone="UTC"):
    return f"========== Top 采集 #{index} 时间: {date} {zone} ==========\n"


def parse(text):
    return list(iter_top_records(io.BytesIO(text.encode() if isinstance(text, str) else text)))


class TopParserTests(unittest.TestCase):
    def test_cpu_memory_and_source_lines(self):
        records = parse(header() + TABLE + ROW + CPU + MEM)
        self.assertEqual([r.kind for r in records], ["S", "P"])
        system, process = [r.data for r in records]
        self.assertEqual([r.number for r in records], [1, 3])
        self.assertEqual(system["cpu_capacity"], 800)
        self.assertEqual(system["cpu_single_core"], 280)
        self.assertEqual(system["cpu_total"], 35)
        self.assertEqual(system["cpu_irq"], 3)
        self.assertEqual(system["cpu_idle"], 65)
        self.assertEqual(system["mem_used_mb"], 5399)
        self.assertEqual(system["mem_free_mb"], 9326)
        self.assertEqual(system["mem_total_mb"], 14725)
        self.assertIsNone(system["mem_avail_mb"])
        self.assertNotIn("boot", system)
        self.assertEqual(process["name"], "app  --flag value")
        self.assertEqual(process["cpu1c"], 160)
        self.assertEqual(process["cpu"], 20)
        self.assertEqual(process["rss_kb"], 1536)
        self.assertTrue(all(r.data["source_format"] == "top" for r in records))
        self.assertEqual(system["_sample"], process["_sample"])

    def test_memory_units_default_to_kb(self):
        for total, used, free in (("8192", "3072", "5120"),
                                  ("8192K", "3072kB", "5120KiB"),
                                  ("8M", "3MiB", "5MB"),
                                  ("8388608B", "3145728B", "5242880B")):
            with self.subTest(total=total):
                records = parse(header() +
                                f"Mem: {total} total, {used} used, {free} free\n")
                self.assertFalse(any(r.error for r in records))
                system = records[0].data
                self.assertEqual(system["mem_total_mb"], 8)
                self.assertEqual(system["mem_used_mb"], 3)
                self.assertEqual(system["mem_free_mb"], 5)

    def test_vswap_scpu_and_command_aliases(self):
        for name in ("ARGS", "CMD", "COMMAND", "NAME"):
            with self.subTest(name=name):
                table = f"PID USER PR NI VIRT RES SHR VSWAP S %CPU %SCPU %MEM TIME+ {name}\n"
                row = "42 root 20 0 2G 512K 0 1G S 120 15 2 0:01 app --x\n"
                records = parse(header() + CPU + table + row)
                self.assertEqual(records[-1].data["cpu1c"], 120)
                self.assertEqual(records[-1].data["cpu"], 15)
                self.assertEqual(records[-1].data["name"], "app --x")
        records = parse(header() + "USER CMD CPU% PID RES\nroot app  --x 0 12 1024\n")
        self.assertEqual(records[-1].data["pid"], 12)
        self.assertEqual(records[-1].data["name"], "app  --x")
        self.assertEqual(records[-1].data["rss_kb"], 1024)
        self.assertIsNone(records[-1].data["cpu"])

    def test_device_compact_sorted_header(self):
        for columns in ("S[%CPU]%SCPU", "S %CPU[%SCPU]", "S %CPU %SCPU"):
            with self.subTest(columns=columns):
                table = ("PID USER PR NI VIRT RES VSWAP SHR " + columns +
                         " %MEM TIME+ ARGS\n")
                rows = ("23212 shell 20 0 10G 5.2M 0 4.1M R 11.1 7.4 0.0 0:00.03 top -b -n 1 -d 1\n"
                        "8647 system 18 -2 19G 413M 0 318M S 3.7 0.0 2.8 3:32.23 system_server\n"
                        "22956 root 0 -20 0 0 0 0 I 3.7 3.7 0.0 0:10.64 [kworker/u17:3-asm330lhh workqueue]\n")
                records = parse(header() +
                                "800%cpu 52%user 0%nice 119%sys 619%idle 0%iow 7%irq 4%sirq 0%host\n" +
                                "Mem: 14725M total, 7385M used, 7340M free, 56M buffers\n" + table + rows)
                self.assertFalse([r.error for r in records if r.error])
                self.assertEqual([r.kind for r in records], ["S", "P", "P"])
                system, process, worker = [r.data for r in records]
                self.assertEqual(system["cpu_total"], 22.625)
                self.assertEqual(system["mem_used_mb"], 7385)
                self.assertEqual(process["cpu1c"], 3.7)
                self.assertAlmostEqual(process["cpu"], 0.4625)
                self.assertEqual(process["rss_kb"], 413 * 1024)
                self.assertEqual(process["name"], "system_server")
                self.assertEqual(worker["name"], "[kworker/u17:3-asm330lhh workqueue]")

    def test_command_pid_token_does_not_reset_table(self):
        records = parse(header() + CPU + TABLE + ROW.replace("--flag", "PID") + ROW)
        self.assertFalse(any(r.error for r in records))
        self.assertEqual([r.data["name"] for r in records if r.kind == "P"],
                         ["app  PID value", "app  --flag value"])

    def test_res_units_zero_and_top_filter(self):
        for unit, expected in (("2048", 2048), ("512B", .5), ("1.5K", 1.5),
                               ("1.5M", 1536), ("1.5G", 1572864), ("1MiB", 1024)):
            with self.subTest(unit=unit):
                records = parse(header() + "PID RES CPU% NAME\n" +
                                f"1 {unit} 0 zero\n2 1K 0 top\n3 1K 0 top -b\n4 1K 0 topcat\n")
                processes = [r.data for r in records if r.kind == "P"]
                self.assertEqual([p["name"] for p in processes], ["zero", "topcat"])
                self.assertEqual(processes[0]["rss_kb"], expected)
                self.assertEqual(processes[0]["cpu1c"], 0)

    def test_missing_values_and_empty_headers(self):
        records = parse(header() + header(2) + MEM + header(3) + TABLE + ROW)
        systems = [r.data for r in records if r.kind == "S"]
        self.assertEqual(len(systems), 2)
        self.assertIsNone(systems[0]["cpu_total"])
        self.assertIsNone(systems[1]["mem_used_mb"])
        self.assertIsNone(records[-1].data["cpu"])
        self.assertEqual(parse(header() + TABLE), [])

    def test_invalid_numbers_are_not_repaired(self):
        for cpu in ("800%cpu -1%idle", "800%cpu 801%idle", "0%cpu 0%idle",
                    "800%cpu 1%idle 2%idle", "800%cpu nan%idle"):
            with self.subTest(cpu=cpu):
                records = parse(header() + cpu + "\n" + TABLE + ROW)
                self.assertEqual(sum(bool(r.error) for r in records), 1)
                self.assertIsNone(records[-1].data["cpu"])
        for row in ("0 1K 1 app", "-1 1K 1 app", "1 -1M 1 app", "1 1K nan app",
                    "1 1K inf app", "1 1K 1", "9223372036854775808 1K 1 app"):
            with self.subTest(row=row):
                records = parse(header() + "PID RES CPU% NAME\n" + row + "\n")
                self.assertEqual(sum(bool(r.error) for r in records), 1)
                self.assertFalse(any(r.kind for r in records))

    def test_utc_and_bracket_utc(self):
        expected = int(datetime(2026, 7, 1, 12, tzinfo=timezone.utc).timestamp() * 1000)
        for zone in ("UTC", "[UTC]"):
            record = parse(header(zone=zone) + CPU)[0]
            self.assertEqual(record.data["ts"], expected)
            self.assertEqual(record.data["timestamp_timezone"], "UTC")

    def test_local_timezone_uses_date_dst(self):
        if not hasattr(time, "tzset"):
            date = datetime(2026, 7, 1, 12)
            self.assertEqual(parse(header(zone="") + CPU)[0].data["ts"],
                             int(date.timestamp() * 1000))
            return
        original = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/New_York"
            time.tzset()
            for month, utc_hour in ((1, 17), (7, 16)):
                text = header(date=f"2026-{month:02d}-01 12:00:00", zone="") + CPU
                system = parse(text)[0].data
                expected = datetime(2026, month, 1, utc_hour, tzinfo=timezone.utc)
                self.assertEqual(system["ts"], int(expected.timestamp() * 1000))
                self.assertEqual(system["timestamp_timezone"], "local")
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()

    def test_corrupt_lines_and_recovery(self):
        text = (header() + CPU).encode() + b"\xff\n" + b"x" * (MAX_LINE_BYTES + 1)
        text += b"\n" + (TABLE + ROW).encode() + b"42 1K 1 partial"
        records = parse(text)
        self.assertEqual([r.number for r in records if r.error], [3, 4, 7])
        self.assertEqual([(r.kind, r.number) for r in records if r.kind], [("S", 1), ("P", 6)])
        text = header(date="2026-02-30 12:00:00") + CPU + header(2) + MEM
        records = parse(text)
        self.assertEqual(sum(bool(r.error) for r in records), 1)
        self.assertEqual([r.number for r in records if r.kind], [3])
        records = parse(header() + CPU + header(2).rstrip())
        self.assertEqual(sum(bool(r.error) for r in records), 1)
        self.assertEqual(sum(r.kind == "S" for r in records), 1)

    def test_sample_bound_and_bounded_reads(self):
        class BoundedStream(io.BytesIO):
            def read(self, size=-1):
                raise AssertionError("parser must not read whole file")

            def readline(self, size=-1):
                if not 0 < size <= MAX_LINE_BYTES + 1:
                    raise AssertionError("unbounded line read")
                return super().readline(size)

        text = (header() + CPU).encode() + b"x" * (MAX_SAMPLE_BYTES + 1) + b"\n"
        text += (header(2) + MEM).encode()
        stream = BoundedStream(text)
        self.assertEqual(detect_source_format(stream), "top")
        self.assertEqual(stream.tell(), 0)
        records = list(iter_top_records(stream))
        self.assertEqual(sum(bool(r.error) for r in records), 2)
        self.assertEqual([r.number for r in records if r.kind], [4])
        with patch("perf_top_parser.MAX_SAMPLE_BYTES", 300):
            records = parse(header() + CPU + TABLE + ROW * 5 + header(2) + MEM)
        self.assertEqual(sum(r.kind == "S" for r in records), 1)
        self.assertFalse(any(r.kind == "P" for r in records))

    def test_detection_mixed_and_ansi(self):
        for text in (header() + CPU + SAMPLE, SAMPLE + header() + CPU,
                     header() + CPU + SAMPLE.rstrip()):
            with self.subTest(text=text[:20]):
                stream = io.BytesIO(text.encode())
                with self.assertRaisesRegex(ValueError, "混合"):
                    detect_source_format(stream)
                self.assertEqual(stream.tell(), 0)
                with self.assertRaisesRegex(ValueError, "混合"):
                    parse(text)
        text = b"\xef\xbb\xbf\x1b[H\x1b[2J" + (header() + CPU).encode()
        self.assertEqual(detect_source_format(io.BytesIO(text)), "top")
        self.assertEqual(parse(text)[0].data["cpu_total"], 35)
        self.assertEqual(detect_source_format(io.BytesIO(SAMPLE.encode())), "sysmonitor")


class TopStoreTests(ReportAssertions, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "top.sqlite3")

    def load(self, text):
        return self.store.import_files([("top.txt", io.BytesIO(text.encode()))])["id"]

    def test_cycles_equal_time_rollback_sources_and_deduplication(self):
        text = header() + CPU + TABLE + ROW
        text += header(2) + MEM + header(3, "2026-07-01 11:59:59") + CPU
        sources = [("top.1.log", io.BytesIO(text.encode())),
                   ("duplicate.log", io.BytesIO(text.encode())),
                   ("top.log", io.BytesIO((header(4, "2026-07-01 11:59:59") + MEM).encode()))]
        result = self.store.import_files(sources)
        summary = self.store.session(result["id"])["summary"]
        self.assertEqual(summary["source_format"], "top")
        self.assertEqual(summary["counts"], dict(S=4, P=1, D=0, DE=0, DP=0))
        self.assertEqual(summary["cycles"], 4)
        self.assertEqual(summary["segments"], 2)
        self.assertEqual(summary["duplicate_files"], 1)
        self.assertEqual(summary["orphan_cycles"], 0)
        self.assertEqual(summary["timestamp_timezones"], dict(UTC=4, local=0))
        self.assertIn("nearest-rank", summary["statistics_policy"])
        self.assertNotIn("重启", summary["events"][0]["message"])
        self.assertEqual(self.store.import_files(sources), dict(id=result["id"], duplicate=True))
        with self.store.connect() as db:
            rows = db.execute("SELECT cycle,segment,line,data FROM records ORDER BY id").fetchall()
        self.assertEqual([(r["cycle"], r["segment"], r["line"]) for r in rows],
                         [(1, 0, 1), (1, 0, 4), (2, 0, 5), (3, 1, 7), (4, 1, 1)])
        for row in rows:
            self.assertNotIn("_sample", json.loads(row["data"]))
            self.assertNotIn("boot", json.loads(row["data"]))

    def test_mixed_and_empty_imports_leave_no_rows(self):
        top = header() + CPU
        cases = [[top + SAMPLE], [SAMPLE + top], [top, SAMPLE], [SAMPLE, top],
                 [header()], [header() + TABLE], ["unknown\n"]]
        for sources in cases:
            with self.subTest(sources=sources):
                with self.assertRaises(ValueError):
                    self.store.import_files([(f"input{i}.txt", io.BytesIO(s.encode()))
                                             for i, s in enumerate(sources)])
                with self.store.connect() as db:
                    for table in ("sessions", "records", "analysis_cache"):
                        self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_import_error_accounting_and_sysmonitor_unchanged(self):
        text = header(date="bad") + CPU + header(2) + CPU
        text += "PID RES CPU% NAME\n1 1M nan invalid\n2 1M 0 valid\n"
        sid = self.load(text)
        summary = self.store.session(sid)["summary"]
        self.assertEqual(summary["errors"], 2)
        self.assertEqual([issue["line"] for issue in summary["issues"]], [1, 6])
        self.assertEqual(summary["counts"]["P"], 1)
        baseline = list(iter_records(io.BytesIO(SAMPLE.encode())))
        old_sid = self.load(SAMPLE)
        self.assertEqual(self.store.session(old_sid)["summary"]["source_format"], "sysmonitor")
        with self.store.connect() as db:
            stored = db.execute("SELECT kind,data FROM records WHERE session=? ORDER BY id", (old_sid,)).fetchall()
        self.assertEqual([(r["kind"], json.loads(r["data"])) for r in stored],
                         [(r.kind, r.data) for r in baseline])

    def test_references_formats_missing_fields_and_io_null(self):
        sid = self.load(header() + CPU + MEM + TABLE + ROW +
                        header(2, "2026-07-01 12:00:01") + MEM +
                        header(3, "2026-07-01 12:00:02") + TABLE + ROW)
        points = self.store.process_references(sid, 0)["points"]
        self.assertEqual([p["cpu_total"] for p in points], [35, None, None])
        self.assertEqual([p["cpu_single_core"] for p in points], [280, None, None])
        self.assertEqual([p["mem_used_mb"] for p in points], [5399, 5399, None])
        for point in points:
            for field in ("rd_kb", "wr_kb", "rchar_kb", "wchar_kb"):
                self.assertIsNone(point[field])
        old_sid = self.load(SAMPLE)
        point = self.store.process_references(old_sid, 0)["points"][0]
        self.assertAlmostEqual(point["cpu_single_core"], 42.04 * 8)
        self.assertEqual(point["mem_used_mb"], 5399)
        self.assertEqual(point["wr_kb"], 156)
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_remove(data,'$.mem_avail_mb') "
                       "WHERE session=? AND kind='S'", (old_sid,))
        self.assertIsNone(self.store.process_references(old_sid, 0)["points"][0]["mem_used_mb"])

    def test_explicit_platform_overrides_memory_in_report(self):
        from perf_report import render_report

        for capacity, memory, platform in ((700, 14725, "8295"), (800, 20617, "8255")):
            with self.subTest(capacity=capacity):
                text = header() + f"{capacity}%cpu {capacity / 2}%idle\n"
                text += MEM.replace("14725", str(memory)) + TABLE + ROW
                text += header(2, "2026-07-01 12:00:01") + MEM.replace("14725", str(memory))
                sid = self.load(text)
                reference = self.store.process_references(sid, 0)
                self.assertEqual([p["cpu_single_core"] for p in reference["points"]], [capacity / 2, None])
                payload = self.assert_report(render_report(self.store, sid))
                system = payload["systems"][0]
                points = system["series"]["points"]
                self.assertEqual(points[0]["cpu_platform"], platform)
                self.assertEqual(points[0]["cpu_detection"], "explicit")
                self.assertEqual([p["cpu_single_core"] for p in points], [capacity / 2, None])
                self.assertEqual(system["metrics"]["cpu_total"]["avg"], capacity / 2)
                self.assertEqual([p["cpu_total"] for p in points], [capacity / 2, None])
                self.assertEqual(system["metrics"]["cpu_total"]["count"], 1)
                self.assertEqual(system["cpu_reference_note"], reference["cpu_reference_note"])
                self.assert_lines(self.report_lines(points, "cpu_single_core"), 1, 0)

    def test_single_core_peak_survives_reference_sampling(self):
        text = ""
        for i in range(200):
            capacity = 1600 if i == 97 else 800
            text += header(i, f"2026-07-01 12:{i // 60:02d}:{i % 60:02d}")
            text += f"{capacity}%cpu {capacity / 2}%idle\n"
        sid = self.load(text)
        for limit in (1, 64):
            result = self.store.process_references(sid, 0, limit=limit)
            self.assertTrue(result["sampled"])
            self.assertLessEqual(len(result["points"]), limit)
            self.assertEqual(max(p["cpu_single_core"] for p in result["points"]), 800)
            self.assertEqual({p["cpu_total"] for p in result["points"]}, {50})

    def test_invalid_header_resets_columns_and_recovers(self):
        records = parse(header() + CPU + "PID RES CPU% NAME\n1 1M 1 good\n"
                        "PID CPU% NAME\n2 2 bad\nPID RES CPU% NAME\n3 1M 3 recovered\n")
        self.assertEqual([r.number for r in records if r.error], [5, 6])
        self.assertEqual([r.data["pid"] for r in records if r.kind == "P"], [1, 3])

    def test_real_report_missing_cycles_and_metrics_in_node(self):
        from perf_report import render_report

        text = ""
        for i in range(1, 7):
            text += header(i, f"2026-07-01 12:00:{i:02d}")
            text += MEM if i in (3, 4) else CPU
            if i != 3:
                text += TABLE + ROW
        sid = self.load(text)
        payload = self.assert_report(render_report(self.store, sid))
        self.assertEqual(payload["session"]["summary"]["source_format"], "top")
        system, process = payload["systems"][0], payload["processes"][0]
        self.assert_metric(system["metrics"]["cpu_total"], 4, [280] * 5)
        self.assert_metric(process["metrics"]["cpu1c"], 5, [160] * 5)
        self.assert_metric(process["metrics"]["cpu"], 4, [20] * 5)
        self.assert_metric(process["metrics"]["rss_kb"], 5, [1536] * 5)
        self.assertEqual(process["name"], "app  --flag value")
        self.assertEqual(process["coverage"]["missing_cycles"], 1)
        self.assertEqual(process["coverage"]["complete_cycles"], 0)
        for field in ("rd_kb", "wr_kb", "rchar_kb", "wchar_kb"):
            self.assert_metric(process["metrics"][field], 0, [None] * 5)
        full = [dict(zip(payload["full_cycle_schema"], row)) for row in process["full_cycles"]]
        self.assertEqual([row["cycle"] for row in full], [1, 2, 4, 5, 6])
        self.assertIsNone(full[2]["cpu"])
        self.assertEqual(full[2]["cpu1c"], 160)
        self.assert_lines(self.report_lines(system["series"]["points"], "cpu_total"), 4, 2)
        self.assert_lines(self.report_lines(process["series"]["points"], "cpu1c"), 5, 3)
        self.assert_lines(self.report_lines(process["series"]["points"], "cpu"), 4, 2)

    def test_unsuffixed_memory_import_and_report(self):
        from perf_report import render_report
        from perf_report_ui import scaled

        text = (header() + CPU +
                "Mem: 8388608 total, 3145728 used, 5242880 free\n" +
                "PID RES CPU% NAME\n42 2048 0 app\n")
        sid = self.load(text)
        with self.store.connect() as db:
            rows = db.execute("SELECT kind,data FROM records WHERE session=? ORDER BY id",
                              (sid,)).fetchall()
        stored = {r["kind"]: json.loads(r["data"]) for r in rows}
        self.assertEqual(stored["P"]["rss_kb"], 2048)
        self.assertEqual(stored["S"]["mem_used_mb"], 3072)
        payload = self.assert_report(render_report(self.store, sid))
        system, process = payload["systems"][0], payload["processes"][0]
        for field, expected in (("mem_total_mb", 8192), ("mem_used_mb", 3072),
                                ("mem_free_mb", 5120)):
            self.assert_metric(system["metrics"][field], 1, [expected] * 5)
        self.assert_metric(process["metrics"]["rss_kb"], 1, [2048] * 5)
        self.assertEqual(scaled(process["metrics"]["rss_kb"]["avg"], 1024), 2)
        lines = self.report_lines(process["series"]["points"], "rss_kb", 1024)
        self.assertEqual([point[1] for line in lines for point in line], [2])
        references = self.store.process_references(sid, 0)["points"]
        self.assertEqual(references[0]["mem_used_mb"], 3072)

    def test_report_device_memory_and_old_cache_rebuild(self):
        import zlib
        from perf_report import REPORT_CACHE_VERSION, render_report
        from perf_report_data import build_report_data

        text = (header(date="2026-09-22 03:01:28") +
                "Tasks: 499 total, 1 running, 498 sleeping, 0 stopped, 0 zombie\n"
                "  Mem: 14725M total, 7516M used, 7209M free, 61M buffers\n"
                " Swap: 4095M total, 0M used, 4095M free, 2504M cached\n"
                "800%cpu 26%user 4%nice 30%sys 730%idle 0%iow 7%irq 4%sirq 0%host\n")
        sid = self.load(text)
        raw = build_report_data(self.store, sid)["systems"][0]
        self.assert_metric(raw["metrics"]["mem_used_mb"], 1, [7516] * 5)
        self.assertAlmostEqual(raw["metrics"]["mem_percent"]["avg"], 7516 / 14725 * 100)
        old_version = "1:" + REPORT_CACHE_VERSION.split(":", 1)[1]
        with self.store.connect() as db:
            db.execute("INSERT INTO report_cache(session,version,document) VALUES (?,?,?)",
                       (sid, old_version, zlib.compress(b"old missing memory report")))
        html = render_report(self.store, sid)
        payload = self.assert_report(html)
        system = payload["systems"][0]
        self.assert_metric(system["metrics"]["mem_used_mb"], 1, [7516] * 5)
        point = system["series"]["points"][0]
        self.assertEqual(point["mem_used_mb"], 7516)
        self.assertEqual(point["mem_total_mb"], 14725)
        self.assertEqual(point["mem_free_mb"], 7209)
        self.assert_metric(system["metrics"]["mem_free_mb"], 1, [7209] * 5)
        self.assertIn("mem_free_mb", payload["system_fields"])
        self.assertNotIn("mem_avail_mb", system["metrics"])
        self.assertIsNone(point["mem_avail_mb"])
        self.assertIn("Top", payload["method"]["memory"])
        self.assertIn("used", payload["method"]["memory"])
        self.assertEqual(render_report(Store(self.store.path), sid), html)

    @unittest.skipUnless(hasattr(time, "tzset"), "需要可切换时区")
    def test_local_report_time_and_legacy_utc(self):
        from perf_report import render_report
        from perf_report_ui import stamp

        self.addCleanup(time.tzset)
        with patch.dict(os.environ, {"TZ": "Asia/Shanghai"}):
            time.tzset()
            for zone, hour in (("", "12"), ("UTC", "20")):
                sid = self.load(header(zone=zone) + "100%cpu 1%idle\n" + MEM)
                html = render_report(self.store, sid)
                payload = self.assert_report(html)
                ts = payload["systems"][0]["cycle_axis"][0][1]
                self.assertIn(f"2026年07月01日 {hour}:00:00", html)
                self.assertEqual(stamp(ts), f"2026-07-01T{hour}:00:00.000+08:00")
                self.assertIn("本机时间", html)
                self.assertNotIn("UTC时间", html)
        time.tzset()

    def test_report_top_missing_used_does_not_fall_back(self):
        from perf_report_data import build_report_data

        sid = self.load(header() + MEM + header(2, "2026-07-01 12:00:01") + MEM)
        for field in ("mem_used_mb", "mem_free_mb"):
            with self.subTest(field=field):
                with self.store.connect() as db:
                    db.execute("UPDATE records SET data=json_set(data,?,0) "
                               "WHERE session=? AND cycle=1", ("$." + field, sid))
                    db.execute("UPDATE records SET data=json_remove(data,?) "
                               "WHERE session=? AND cycle=2", ("$." + field, sid))
                system = build_report_data(self.store, sid)["systems"][0]
                self.assertEqual([p[field] for p in system["series"]["points"]], [0, None])
                self.assert_metric(system["metrics"][field], 1, [0] * 5)

    def test_real_report_nearest_rank_uses_all_observed_values(self):
        from perf_report import render_report

        text = ""
        for i in range(1, 101):
            text += header(i, f"2026-07-01 12:{i // 60:02d}:{i % 60:02d}")
            text += f"100%cpu {100 - i}%idle\nPID RES CPU% NAME\n42 1M {i} app\n"
        sid = self.load(text)
        payload = self.assert_report(render_report(self.store, sid))
        system, process = payload["systems"][0], payload["processes"][0]
        for metrics, field in ((system["metrics"], "cpu_total"),
                               (process["metrics"], "cpu1c"), (process["metrics"], "cpu")):
            self.assert_metric(metrics[field], 100, [1, 50.5, 95, 99, 100])
        self.assert_metric(process["active"]["metrics"]["cpu1c"], 100, [1, 50.5, 95, 99, 100])
        self.assertEqual(len(process["full_cycles"]), 100)
        self.assert_lines(self.report_lines(process["series"]["points"], "cpu1c"), 100, 99)


if __name__ == "__main__":
    unittest.main()


