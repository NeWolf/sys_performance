import io
import json
import math
import tempfile
import threading
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from perf_parser import MAX_LINE_BYTES
from perf_qnx_parser import HEADER, iter_qnx_records, parse_qnx_line
import perf_qnx_report as qnx
from perf_report import render_report
from perf_qnx_report import REPORT_CSP
from perf_store import Store
from perf_top_parser import detect_source_format


def row(value="12.5", ts=1793340807699, kind="QC", module="cpu", metric="totalcpu", unit="%", relative="1"):
    return f"{kind},{ts},{module},{metric},{value},{unit},{relative}\n"


class QnxParserTests(unittest.TestCase):
    def test_mapping_null_and_header(self):
        text = '\ufeff' + ','.join(HEADER) + '\r\n'
        for kind, module in (("QC", "cpu"), ("QG", "gpu"), ("QDSP", "cdsp"),
                             ("QDSP", "cdsp1"), ("QDDR", "ddr")):
            text += row(kind=kind, module=module, value="", relative="")
        parsed = list(iter_qnx_records(io.BytesIO(text.encode())))
        self.assertEqual(len(parsed), 5)
        self.assertTrue(all(p.error is None and p.data["value"] is None and
                            p.data["qnx_relative_s"] is None for p in parsed))
        self.assertEqual(parse_qnx_line(row(value="0"))[1]["value"], 0)
        self.assertEqual(parse_qnx_line(row(ts=253402300799999))[1]["ts"], 253402300799999)

    def test_invalid_fields_and_no_zero_repair(self):
        cases = [row(value=v) for v in ("-1", "-0", "NaN", "Inf", "1e999", "1_000", " 2")]
        cases += [row(ts=t) for t in ("-1", "1.5", "1e3", "", "253402300800000")]
        cases += [row(module="gpu"), row(metric="bad metric"), row(unit=""), row(unit="a\x1bb"),
                  row(relative="-1"), row(relative="nan"), row().rstrip() + ',extra\n']
        for text in cases:
            with self.subTest(text=text):
                parsed = list(iter_qnx_records(io.BytesIO(text.encode())))
                self.assertEqual(len(parsed), 1)
                self.assertIsNotNone(parsed[0].error)
                self.assertIsNone(parsed[0].data)

    def test_bounded_corrupt_and_partial(self):
        class Bounded(io.BytesIO):
            def readline(self, size=-1):
                if size < 0 or size > MAX_LINE_BYTES + 1:
                    raise AssertionError("unbounded read")
                return super().readline(size)
        stream = Bounded(b'x' * (MAX_LINE_BYTES + 20) + b'\n\xff\n' + row().encode() + row().rstrip().encode())
        parsed = list(iter_qnx_records(stream))
        self.assertEqual([p.number for p in parsed], [1, 2, 3, 4])
        self.assertEqual(sum(p.error is not None for p in parsed), 3)
        self.assertEqual(parsed[2].data["value"], 12.5)

    def test_detection_full_file_and_quoted_header(self):
        text = ','.join('"' + f + '"' for f in HEADER) + '\n'
        self.assertEqual(detect_source_format(io.BytesIO(text.encode())), "qnx")
        for other in ('S,1,2\n', '========== Top 采集 #1 时间: invalid ==========\n'):
            for text in (row() * 3000 + other, other + row(), row() + other.rstrip()):
                stream = io.BytesIO(text.encode())
                with self.assertRaisesRegex(ValueError, "混合"):
                    detect_source_format(stream)
                self.assertEqual(stream.tell(), 0)


class QnxStoreReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "qnx.sqlite3")

    def load(self, text, name="perf_qnx.log"):
        return self.store.import_files([(name, io.BytesIO(text.encode()))], capture_method="QNX")["id"]

    def test_import_overview_counts_and_duplicates(self):
        text = row() + row(value="") + row(value="-1") + row(ts=1000)
        sid = self.load(text)
        overview = self.store.overview(sid)
        summary = overview["summary"]
        self.assertEqual(summary["counts"], dict(QC=3, QG=0, QDSP=0, QDDR=0))
        self.assertEqual(summary["capture_method"], "QNX")
        self.assertEqual(summary["source_format"], "qnx")
        self.assertEqual(summary["orphan_cycles"], 0)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(len(overview["segments"]), 2)
        self.assertEqual(overview["stats"], dict(samples=3, valid_samples=2))
        self.assertEqual(self.load(text), sid)
        with self.store.connect() as db:
            values = [json.loads(r[0])["value"] for r in db.execute('SELECT data FROM records ORDER BY id')]
        self.assertEqual(values, [12.5, None, 12.5])

    def test_mixed_files_rollback(self):
        for sources in ([row(), "S,1,2\n"], [row() + "S,1,2\n"], [row(value="bad")]):
            with self.assertRaises(ValueError):
                self.store.import_files([(str(i), io.BytesIO(s.encode())) for i, s in enumerate(sources)])
            self.assertEqual(self.store.sessions(), [])

    def test_exact_statistics_units_null_segments_and_bound(self):
        text = ''.join(row(value=str(i), ts=1000+i) for i in range(1, 2002))
        text += row(value="", ts=3002) + row(value="10", unit="MHz", ts=3002)
        text += row(value="", kind="QG", module="gpu", metric="utilization", ts=3002)
        text += row(value="99", ts=1)
        data = qnx.build_report_data(self.store, self.load(text))
        primary = next(s for s in data["series"] if s["segment"] == 0 and s["unit"] == "%" and s["module"] == "cpu")
        self.assertEqual(primary["samples"], 2002)
        self.assertEqual(primary["valid_samples"], 2001)
        self.assertEqual(primary["p95"], 1901)
        self.assertAlmostEqual(primary["avg"], 1001)
        self.assertEqual(primary["min"], 1)
        self.assertEqual(primary["max"], 2001)
        self.assertLessEqual(len(primary["points"]), qnx.MAX_POINTS)
        self.assertEqual(len(data["series"]), 4)
        self.assertIn(dict(segment=0, module="gpu", metric="utilization"), data["missing_modules"])
        empty = next(s for s in data["series"] if s["module"] == "gpu")
        self.assertIsNone(empty["avg"])
        self.assertIsNone(empty["p95"])
        with patch.object(qnx, "MAX_TOTAL_POINTS", 40):
            bounded = qnx.build_report_data(self.store, data["session"]["id"])
        self.assertLessEqual(sum(len(s["points"]) for s in bounded["series"]), 40)

    def test_analysis_overview_full_statistics_and_peak_location(self):
        text = ''.join(row(value=str(value), ts=1000+i, relative=str(i))
                       for i, value in enumerate((0, 80, 20, 80)))
        text += row(value="", ts=1004)
        text += row(value="5", kind="QDDR", module="ddr", metric="ddr_bw_total",
                    unit="reported", ts=1004)
        data = qnx.build_report_data(self.store, self.load(text, name='peak&source.log'))
        cpu = next(s for s in data["series"] if s["module"] == "cpu")
        self.assertEqual((cpu["avg"], cpu["p95"], cpu["max"]), (45, 80, 80))
        self.assertEqual(cpu["valid_ratio_pct"], 80)
        self.assertEqual(cpu["peak_point"]["ts"], 1001)
        self.assertEqual(cpu["peak_point"]["qnx_relative_s"], 1)
        self.assertEqual(cpu["peak_point"]["line"], 2)
        for style in ("default", "compact"):
            doc = qnx.render_document(data, style=style)
            self.assertIn('关键负载分析总览', doc)
            self.assertIn('CPU 整体负载', doc)
            self.assertIn('DDR 总带宽', doc)
            self.assertIn('有效记录 4 / 5 （80.00%）', doc)
            self.assertIn('peak&amp;source.log:2', doc)
            self.assertIn('1970-01-01T08:00:01.001+08:00', doc)
            self.assertIn('源日志未标注单位', doc)
            self.assertIn('不是时间加权统计', doc)
            self.assertIn('不判定达标、超标或瓶颈', doc)
            self.assertIn('不是采集覆盖率', doc)
        with patch.object(qnx, "MAX_POINTS", 4):
            bounded = qnx.build_report_data(self.store, data["session"]["id"])
        other = next(s for s in bounded["series"] if s["module"] == "cpu")
        self.assertEqual(other["peak_point"], cpu["peak_point"])
        self.assertEqual(other["avg"], cpu["avg"])

    def test_analysis_missing_zero_auxiliary_and_separate_scopes(self):
        text = row(value="0", ts=1000) + row(value="0", ts=1001)
        text += row(value="", kind="QG", module="gpu", metric="utilization", ts=1001)
        text += row(value="99", kind="QG", module="gpu", metric="gpubusbusy", ts=1001)
        text += row(value="10", unit="reported", ts=1001)
        text += row(value="70", ts=1)
        data = qnx.build_report_data(self.store, self.load(text))
        gpu = next(s for s in data["series"] if s["metric"] == "utilization")
        self.assertIsNone(gpu["peak_point"])
        self.assertEqual(gpu["valid_ratio_pct"], 0)
        doc = qnx.render_document(data)
        self.assertIn('全部有效样本为零', doc)
        self.assertIn('没有有效负载数据，不能评估', doc)
        self.assertIn('仅一个有效样本', doc)
        self.assertIn('CPU 整体负载 · 分段 0 · %', doc)
        self.assertIn('CPU 整体负载 · 分段 0 · reported', doc)
        self.assertIn('CPU 整体负载 · 分段 1 · %', doc)
        data = qnx.build_report_data(self.store, self.load(row(metric="frequency", unit="KHz")))
        self.assertIn('没有关键负载序列', qnx.render_document(data))
        self.assertEqual(len(data["missing_modules"]), 5)

    def test_analysis_field_guide_references_and_current_distribution(self):
        text = ''.join(row(value=str(value), ts=1000+i) for i, value in enumerate([20] * 19 + [80]))
        text += row(value="", ts=1020)
        for kind, module, metric, unit, value in (
            ('QG', 'gpu', 'utilization', '%', '35'),
            ('QDSP', 'cdsp', 'load', '%', '0'),
            ('QDSP', 'cdsp1', 'load', '%', '100'),
            ('QDDR', 'ddr', 'ddr_bw_total', 'MB/s', '4466'),
        ):
            text += row(kind=kind, module=module, metric=metric, unit=unit, value=value, ts=1020)
        data = qnx.build_report_data(self.store, self.load(text))
        for style in ('default', 'compact'):
            doc = qnx.render_document(data, style=style)
            overview = doc.split('<section id="analysis-overview">')[1].split('</section>')[0]
            for expected in ('字段说明与参考口径', '有效比例＝有效记录÷总记录', 'nearest-rank',
                             '至少 95%', 'qnx_relative_s 为原始 QNX 相对秒数', '记录时间范围：',
                             '8 个 CPU 核负载的平均值', 'gpubusbusy', 'HVX/HMX', 'CDSP1 实例',
                             '不是内存占用量', '0–100%', '0 MB/s', '实测可持续带宽',
                             '本次采集全量有效样本，非实时', '平均负载 23%', 'P95 为 20%',
                             'P95 距 100% 标尺上端 80 个百分点', '28.75%', '25.00%',
                             '峰均差为 57 个百分点', '峰值高于 P95', '可能存在样本偏差',
                             '平均带宽读数 4466 MB/s', '无法判定带宽偏高、偏低或饱和'):
                self.assertIn(expected, overview)
        with patch.object(qnx, 'MAX_POINTS', 4):
            bounded = qnx.build_report_data(self.store, data['session']['id'])
        self.assertEqual(qnx._analysis_overview(data, lambda *args: None),
                         qnx._analysis_overview(bounded, lambda *args: None))

    def test_analysis_unknown_units_empty_constant_and_percentage_anomaly(self):
        text = row(value='', unit='reported', ts=1000)
        text += row(value='120', ts=1000) + row(value='150', ts=1001)
        text += row(value='40', unit='MHz', ts=1001)
        text += row(value='10', kind='QDSP', module='cdsp', metric='load', ts=1001)
        text += row(value='10', kind='QDSP', module='cdsp', metric='load', ts=1002)
        text += row(value='', kind='QDDR', module='ddr', metric='ddr_bw_total', unit='MB/s', ts=1002)
        data = qnx.build_report_data(self.store, self.load(text))
        overview = qnx._analysis_overview(data, lambda *args: None)
        self.assertIn('源日志未标注单位，保留 reported', overview)
        self.assertIn('当前单位 MHz 未匹配该指标的已知参考口径', overview)
        self.assertIn('单位口径尚未确认，只解读原值分布', overview)
        self.assertIn('存在超过百分比通常标尺 100%', overview)
        self.assertIn('暂停相对百分比标尺的负载解读', overview)
        self.assertNotIn('P95 距 100% 标尺上端 -', overview)
        self.assertIn('全部有效样本数值相同', overview)
        self.assertIn('没有有效负载数据，不能评估', overview)
        self.assertIn('0 MB/s', overview)
        cpu = next(s for s in data['series'] if s['module'] == 'cpu' and s['unit'] == '%')
        self.assertEqual(cpu['max'], 150)

    def test_analysis_extreme_unknown_and_ddr_units_remain_finite_and_escaped(self):
        text = row(value='1.7e308', ts=1000) + row(value='1e308', ts=1001)
        text += row(value='1', kind='QDDR', module='ddr', metric='ddr_bw_total', unit='%', ts=1001)
        text += row(value='20', kind='QG', module='gpu', metric='utilization', unit='<b>unknown</b>', ts=1001)
        data = qnx.build_report_data(self.store, self.load(text))
        overview = qnx._analysis_overview(data, lambda *args: None)
        self.assertNotIn('inf', overview.lower())
        self.assertNotIn('nan', overview.lower())
        self.assertNotIn('<b>unknown</b>', overview)
        self.assertIn('&lt;b&gt;unknown&lt;/b&gt;', overview)
        ddr = next(s for s in data['series'] if s['module'] == 'ddr')
        self.assertNotIn('0–100%', qnx._metric_reference(ddr))
        self.assertNotIn('标尺上端', qnx._current_load_analysis(ddr))
        self.assertTrue(qnx.CACHE_VERSION.startswith('qnx-4-load-analysis:'))

    def test_beijing_time_does_not_mutate_timestamps(self):
        sid = self.load(row(ts=0, relative="1.25"))
        data = qnx.build_report_data(self.store, sid)
        for style in ('default', 'compact'):
            doc = qnx.render_document(data, style=style)
            self.assertIn('1970-01-01T08:00:00.000+08:00', doc)
            self.assertIn('北京时间（UTC+8）', doc)
        self.assertEqual(data['series'][0]['points'][0]['ts'], 0)
        self.assertEqual(data['series'][0]['points'][0]['qnx_relative_s'], 1.25)
        self.assertEqual(qnx._time(16 * 3600 * 1000), '1970-01-02T00:00:00.000+08:00')
        self.assertIn('保留 UTC', qnx._time(253402300799999))

    def test_unsampled_null_and_relative_reset_break_lines(self):
        text = ''.join(row(value='' if i == 3 else '10', ts=1000+i,
                           relative=str(i if i < 7 else i-7)) for i in range(12))
        sid = self.load(text)
        full = qnx.build_report_data(self.store, sid)['series'][0]
        with patch.object(qnx, 'MAX_POINTS', 5):
            sampled = qnx.build_report_data(self.store, sid)['series'][0]
        self.assertTrue(all(p['value'] is not None for p in sampled['points']))
        self.assertEqual(len(set(p['run'] for p in sampled['points'])), 3)
        for key in ('samples', 'valid_samples', 'missing_samples', 'avg', 'p95', 'max', 'min'):
            self.assertEqual(sampled[key], full[key])
        sid = self.load(row(ts=1) + row(ts=1) + row(ts=2))
        points = qnx.build_report_data(self.store, sid)['series'][0]['points']
        self.assertNotEqual(points[0]['run'], points[1]['run'])
        self.assertEqual(points[1]['run'], points[2]['run'])

    def test_finite_extreme_average_and_svg(self):
        sid = self.load(row(value="1.7e308") + row(value="1.7e308"))
        data = qnx.build_report_data(self.store, sid)
        self.assertTrue(math.isfinite(data["series"][0]["avg"]))
        self.assertNotIn('cy="nan"', qnx.render_document(data))

    def test_time_bounds_zero_null_and_series_limit(self):
        sid = self.load(row(value="0", ts=0) + row(value="", ts=253402300799999))
        data = qnx.build_report_data(self.store, sid)
        self.assertEqual(data["series"][0]["avg"], 0)
        document = qnx.render_document(data)
        self.assertIn("9999-12-31T23:59:59.999+00:00", document)
        self.assertIn('class="missing"', document)
        sid = self.load(row() + row(metric="cpu0"))
        with patch.object(qnx, "MAX_SERIES", 1):
            with self.assertRaisesRegex(ValueError, "拆分导入"):
                qnx.build_report_data(self.store, sid)

    def test_sql_cancellation_during_projection(self):
        sid = self.load(''.join(row(ts=1000+i) for i in range(2000)))
        state = {"timeline": False, "calls": 0}
        def progress(event):
            if event["stage"] == "timeline":
                state["timeline"] = True
        def cancelled():
            if state["timeline"]:
                state["calls"] += 1
            return state["calls"] >= 2
        with self.assertRaises(CancelledError):
            render_report(self.store, sid, progress=progress, cancelled=cancelled)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM report_cache').fetchone()[0], 0)
        self.assertIn("QNX 独立性能报告", render_report(self.store, sid))

    def test_dual_style_safe_offline_cache_version_and_rebuild(self):
        sid = self.load(row(unit='"<script>alert(1)</script>"'), name='<img onerror=bad>.log')
        with patch('perf_report.build_report_data', side_effect=AssertionError("Android builder")):
            for style, table in (("default", "report_cache"), ("compact", "report_compact_cache")):
                doc = render_report(self.store, sid, style=style)
                self.assertIn("QNX 独立性能报告", doc)
                self.assertIn("cdsp1/load", doc)
                import base64
                import hashlib
                import re
                scripts = re.findall(r'<script([^>]*)>(.*?)</script>', doc, re.S)
                executable = [body for attrs, body in scripts if 'application/json' not in attrs]
                self.assertEqual(executable, [qnx.JS])
                digest = base64.b64encode(hashlib.sha256(executable[0].encode()).digest()).decode()
                self.assertIn("'sha256-" + digest + "'", REPORT_CSP)
                payloads = [body for attrs, body in scripts if 'application/json' in attrs]
                self.assertTrue(payloads)
                for payload in payloads:
                    self.assertNotIn('<', payload)
                    self.assertEqual(json.loads(payload)['unit'], '<script>alert(1)</script>')
                from html.parser import HTMLParser
                class TagCollector(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.tags = []
                    def handle_starttag(self, tag, attrs):
                        self.tags.append((tag, dict(attrs)))
                parsed = TagCollector()
                parsed.feed(doc)
                self.assertFalse(any(tag == 'img' for tag, _ in parsed.tags))
                self.assertFalse(any('src' in attrs or any(key.startswith('on') for key in attrs)
                                     for _, attrs in parsed.tags))
                from html import escape
                self.assertIn(escape("<script>"), doc)
                self.assertIn(escape(REPORT_CSP, quote=True), doc)
                with patch.object(qnx, 'build_report_data', side_effect=AssertionError("cache miss")):
                    self.assertEqual(render_report(self.store, sid, style=style), doc)
                with self.store.connect() as db:
                    version = db.execute(f'SELECT version FROM {table}').fetchone()[0]
                    self.assertIn(qnx.CACHE_VERSION, version)
                    db.execute(f'UPDATE {table} SET document=?', (b'corrupt',))
                self.assertEqual(render_report(self.store, sid, style=style), doc)
                with patch.object(qnx, 'CACHE_VERSION', 'changed'), patch.object(qnx, 'build_report_data', wraps=qnx.build_report_data) as build:
                    render_report(self.store, sid, style=style)
                    self.assertEqual(build.call_count, 1)

    def test_cancel_stages_dont_publish_and_lock_recovers(self):
        sid = self.load(row())
        for stage in ("waiting", "timeline", "statistics", "render", "save"):
            flag = threading.Event()
            def progress(event):
                if event["stage"] == stage:
                    flag.set()
            with self.assertRaises(CancelledError):
                render_report(self.store, sid, progress=progress, cancelled=flag.is_set)
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM report_cache').fetchone()[0], 0)
        self.assertIn("QNX 独立性能报告", render_report(self.store, sid))

    def test_concurrent_reports_coalesce_and_delete_guard(self):
        sid = self.load(row())
        with patch.object(qnx, 'build_report_data', wraps=qnx.build_report_data) as build:
            with ThreadPoolExecutor(max_workers=2) as pool:
                docs = list(pool.map(lambda _: render_report(self.store, sid), range(2)))
            self.assertEqual(docs[0], docs[1])
            self.assertEqual(build.call_count, 1)
        sid = self.load(row(value="13"))
        def progress(event):
            if event["stage"] == "compress":
                self.store.delete(sid)
        with self.assertRaises(KeyError):
            render_report(self.store, sid, progress=progress)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM report_cache WHERE session=?', (sid,)).fetchone()[0], 0)


class QnxIsolationTests(unittest.TestCase):
    setUp = QnxStoreReportTests.setUp
    load = QnxStoreReportTests.load

    def snapshot(self):
        with self.store.connect() as db:
            return list(db.iterdump())

    def test_android_store_entries_reject_without_writes(self):
        from perf_groups import QnxIsolationError
        sid = self.load(row())
        before = self.snapshot()
        calls = [
            lambda: self.store.groups(sid),
            lambda: self.store.group(sid, "missing"),
            lambda: self.store.save_group(sid, {}),
            lambda: self.store.save_group(sid, {}, "missing"),
            lambda: self.store.delete_group(sid, "missing"),
            lambda: self.store.report_settings(sid),
            lambda: self.store.save_report_settings(sid, {}),
            lambda: self.store.group_resources(sid, "missing"),
            lambda: self.store.group_analysis(sid, "missing"),
            lambda: self.store.series(sid, "S"),
            lambda: self.store.process_references(sid, 0),
            lambda: self.store.statistics(sid, "P"),
            lambda: self.store.processes(sid),
            lambda: self.store.processes(sid, merge_names=True),
        ]
        for index, call in enumerate(calls):
            with self.subTest(entry=index), self.assertRaises(QnxIsolationError):
                call()
            self.assertEqual(before, self.snapshot())
        with self.store.connect() as db, self.assertRaises(QnxIsolationError):
            self.store._ensure_process_rss_ranks(db, sid)
        self.assertEqual(before, self.snapshot())

    def test_builders_reject_before_snapshots_templates_or_output(self):
        from perf_admission import build_admission
        from perf_compare import compare_sessions
        from perf_groups import QnxIsolationError
        from perf_report_data import build_report_data
        from test_perf import SAMPLE
        sid = self.load(row())
        android = self.store.import_files([("android.log", io.BytesIO(SAMPLE.encode()))])["id"]
        before = self.snapshot()
        with patch("perf_compare._snapshot", side_effect=AssertionError("snapshot")) as snapshot:
            for left, right in ((sid, android), (android, sid)):
                with self.assertRaises(QnxIsolationError):
                    compare_sessions(self.store, left, right)
            snapshot.assert_not_called()
        with patch("perf_report_data._prepare", side_effect=AssertionError("prepare")) as prepare:
            with self.assertRaises(QnxIsolationError):
                build_report_data(self.store, sid)
            prepare.assert_not_called()
        output = Path(self.temp.name) / "output"
        with patch("perf_slate.validate_template", side_effect=AssertionError("template")) as template:
            with self.assertRaises(QnxIsolationError):
                build_admission(self.store, sid, [], output)
            template.assert_not_called()
        self.assertFalse(output.exists())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(build_report_data(self.store, android)["session"]["id"], android)
        for style in ("default", "compact"):
            self.assertIn("QNX 独立性能报告", render_report(self.store, sid, style=style))
            self.assertNotIn("QNX 独立性能报告", render_report(self.store, android, style=style))

    def test_jobs_reject_before_queue_and_journal_writes(self):
        from perf_admission_jobs import AdmissionJobs
        from perf_groups import QnxIsolationError
        sid = self.load(row())
        jobs = AdmissionJobs(self.store, Path(self.temp.name) / "jobs")
        self.addCleanup(jobs.close)
        with patch.object(jobs, "_open", side_effect=AssertionError("journal")) as opened, \
             patch.object(jobs, "_enqueue", side_effect=AssertionError("enqueue")) as enqueue:
            with self.assertRaises(QnxIsolationError):
                jobs.create(sid, "b" * 32, "QNX")
            opened.assert_not_called()
            enqueue.assert_not_called()
        self.assertEqual(jobs._records, {})
        self.assertEqual(jobs._db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)

    def test_api_rejects_before_lazy_init_and_preserves_reports(self):
        from contextlib import ExitStack
        from fastapi.testclient import TestClient
        import perf_api
        sid = self.load(row())
        prefix = f"/api/sessions/{sid}"
        base = prefix + "/admission/drafts"
        draft = "b" * 32
        before = self.snapshot()
        with ExitStack() as stack:
            for name in ("AdbController", "TopCapture", "QnxCapture"):
                stack.enter_context(patch.object(perf_api, name))
            stack.enter_context(patch.object(perf_api, "Store", return_value=self.store))
            factory = stack.enter_context(patch("perf_admission_jobs.AdmissionJobs",
                                                side_effect=AssertionError("lazy init")))
            app = perf_api.create_app(self.store.path)
            client = stack.enter_context(TestClient(app, base_url="http://127.0.0.1:8765"))
            create = {"draft_id": draft, "title": "QNX"}
            self.assertEqual(client.post(base, json=create).status_code, 403)
            client.headers["X-Session-Token"] = client.get("/api/token").json()["token"]
            requests = [
                ("get", base, None), ("post", base, create),
                ("get", f"{base}/{draft}", None),
                ("get", f"{base}/{draft}/preview", None),
                ("post", f"{base}/{draft}/publish", {"placement": "personal", "confirmed": True}),
            ]
            for method, url, body in requests:
                with self.subTest(url=url, method=method):
                    response = client.request(method, url, **({"json": body} if body else {}))
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIs(response.json()["accepted"], False)
                    self.assertIn("QNX", response.json()["detail"])
            factory.assert_not_called()
            self.assertIsNone(app.state.admission_jobs)
            self.assertFalse((Path(self.temp.name) / "admission_jobs").exists())
            for suffix in ("groups", "groups/missing", "groups/missing/analysis",
                           "groups/missing/resources", "report-settings", "series?kind=S",
                           "statistics?kind=P", "process-references?segment=0", "processes"):
                with self.subTest(entry=suffix):
                    response = client.get(prefix + "/" + suffix)
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("QNX", response.json()["detail"])
            self.assertEqual(client.delete(prefix + "/groups/missing").status_code, 400)
            self.assertEqual(client.put(prefix + "/report-settings", json={}).status_code, 400)
            self.assertEqual(before, self.snapshot())
            self.assertEqual(client.get(prefix).status_code, 200)
            for style in ("default", "compact"):
                response = client.get(prefix + "/report", params={"style": style})
                self.assertEqual(response.status_code, 200, response.text[:200])
                self.assertIn("QNX 独立性能报告", response.text)
                self.assertEqual(REPORT_CSP + "; sandbox allow-scripts", response.headers["content-security-policy"])
                from html import escape
                self.assertIn(escape(REPORT_CSP, quote=True), response.text)

    def test_deleted_session_draft_routes_and_create_idempotency(self):
        from test_admission_api import AdmissionApiTests, BASE, CREATE, D, PUBLISH
        case = AdmissionApiTests()
        self.addCleanup(case.doCleanups)
        case.setUp()
        case.ready()
        case.store.return_value.session.side_effect = KeyError("deleted")
        for url in (BASE, f"{BASE}/{D}", f"{BASE}/{D}/preview"):
            with self.subTest(url=url):
                response = case.client.get(url)
                self.assertEqual(response.status_code, 200, response.text)
        response = case.client.post(BASE, json=CREATE)
        self.assertEqual(response.status_code, 200, response.text)
        case.builder.assert_called_once()
        response = case.client.post(f"{BASE}/{D}/publish", json=PUBLISH)
        self.assertEqual(response.status_code, 200, response.text)
        case.wait("published")
        case.publisher.assert_called_once()


if __name__ == '__main__':
    unittest.main()
