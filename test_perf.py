import base64
import hashlib
import io
import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
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


class ReportMarkup(HTMLParser):
    def __init__(self, report):
        super().__init__()
        self.tags = []
        self.scripts = []
        self.styles = []
        self.processes = []
        self.capture = None
        self.tables = []
        self.feed(report)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if tag == "div":
            self.tables.append(attrs.get("data-table"))
        if tag == "tr" and "data-process" in attrs and "all" in self.tables:
            self.processes.append(attrs)
        if tag == "script":
            self.scripts.append([attrs, ""])
            self.capture = "script"
        elif tag == "style":
            self.capture = "style"

    def handle_endtag(self, tag):
        if tag == "div" and self.tables:
            self.tables.pop()
        if tag == self.capture:
            self.capture = None

    def handle_data(self, data):
        if self.capture == "script":
            self.scripts[-1][1] += data
        elif self.capture == "style":
            self.styles.append(data)


class ReportAssertions:
    def assert_report(self, report):
        from perf_report_ui import JS, REPORT_CSP

        markup = ReportMarkup(report)
        self.assertEqual(len(markup.scripts), 2)
        (json_attrs, raw), (js_attrs, script) = markup.scripts
        self.assertEqual(json_attrs, {"type": "application/json", "id": "report-data"})
        self.assertEqual(js_attrs, {})
        self.assertEqual(script, JS)
        digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
        directives = dict(part.strip().split(" ", 1) for part in REPORT_CSP.split(";"))
        self.assertEqual(directives["script-src"], "'sha256-" + digest + "'")
        self.assertEqual(directives["default-src"], "'none'")
        self.assertNotRegex(raw, r"[<>&]")
        data = json.loads(raw)
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["process_fields"],
                         ["cpu", "cpu1c", "rss_kb", "rd_kb", "wr_kb", "rchar_kb", "wchar_kb"])
        self.assertEqual(data["full_cycle_schema"], ["cycle", "ts", *data["process_fields"],
                         "pids", "duplicate_pids", "p_records", "dp_records",
                         "first_record_id", "last_record_id", "timestamp_conflict"])
        self.assertEqual(data["cycle_axis_schema"], ["cycle", "ts"])
        self.assertEqual(len(markup.processes), len(data["processes"]))
        self.assertEqual({p["data-process"] for p in markup.processes},
                         {p["id"] for p in data["processes"]})
        forbidden = {"link", "iframe", "img", "object", "embed", "base", "form",
                     "audio", "video", "source", "foreignobject", "use", "image", "animate", "set"}
        self.assertFalse(forbidden.intersection(tag for tag, _ in markup.tags))
        for tag, attrs in markup.tags:
            self.assertFalse(any(key.startswith("on") for key in attrs), (tag, attrs))
            self.assertFalse({"src", "srcset", "xlink:href", "srcdoc", "action"}.intersection(attrs))
            if "href" in attrs:
                self.assertTrue(attrs["href"].startswith("#"), attrs)
            if "style" in attrs:
                self.assertNotRegex(attrs["style"].lower(), r"url\s*\(|@import|expression\s*\(")
            if tag == "meta":
                self.assertNotEqual(attrs.get("http-equiv", "").lower(), "refresh")
        policies = [attrs["content"] for tag, attrs in markup.tags
                    if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy"]
        self.assertEqual(policies, [REPORT_CSP])
        self.assertNotRegex("".join(markup.styles).lower(), r"url\s*\(|@import|expression\s*\(")
        return data

    def assert_metric(self, metric, count, values):
        self.assertEqual(metric["count"], count)
        self.assertEqual([metric[key] for key in ("min", "avg", "p95", "p99", "max")], values)

    def run_report_script(self, script):
        """Send UTF-8 JavaScript via stdin to avoid Windows command-line limits."""
        import shutil
        import subprocess

        node = shutil.which("node")
        self.assertIsNotNone(node, "报告 JavaScript 回归需要 Node.js，不允许跳过")
        result = subprocess.run([node, "--input-type=commonjs", "-"], input=script,
                                text=True, encoding="utf-8", capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def report_lines(self, points, field, divisor=1, mark_runs=False):
        """Execute the shipped ECharts series builder; transport script via stdin."""
        source = (Path(__file__).parent / "report_assets/report.js").read_text(encoding="utf-8")
        prefix = source[:source.index("  const observer =")]
        script = "const input = " + json.dumps(dict(
            points=points, field=field, divisor=divisor, mark_runs=mark_runs)) + ";\n" + """
global.document = {
  getElementById: () => ({textContent: JSON.stringify({
    process_fields: [], full_cycle_schema: [], cycle_axis_schema: []
  })}),
  documentElement: {classList: {add() {}}}
};
""" + prefix + """
const points = input.mark_runs ? markRuns(input.points, [input.field]) : input.points;
console.log(JSON.stringify(lineSeries(points, input.field, input.field, '#123456', input.divisor)));
})();
"""
        groups = json.loads(self.run_report_script(script))
        for group in groups:
            self.assertEqual(group["type"], "line")
            self.assertFalse(group["connectNulls"])
            self.assertTrue(group["data"])
            self.assertTrue(all(a[0] < b[0] for a, b in zip(group["data"], group["data"][1:])))
        return [group["data"] for group in groups]

    def assert_lines(self, groups, points, edges):
        self.assertEqual(sum(len(group) for group in groups), points)
        self.assertEqual(sum(len(group) - 1 for group in groups), edges)

    def assert_report_independent(self, sid, baseline):
        from unittest.mock import patch
        from perf_report import render_report

        with patch.object(self.store, "group_analysis", side_effect=AssertionError("报告不应读取分组")), \
                patch.object(self.store, "report_settings", side_effect=AssertionError("报告不应读取手工设置")):
            report = render_report(self.store, sid)
        self.assertEqual(report, baseline)
        return self.assert_report(report)


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


class StoreTests(ReportAssertions, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite3")

    def import_sample(self, text=SAMPLE):
        return self.store.import_files([("perf.log", io.BytesIO(text.encode()))])

    def test_report_system_overview_100_percent_and_memory(self):
        from perf_report_data import build_report_data
        from perf_report_ui import prepare_display
        from perf_report import render_report

        # S-only logs must not need process ratios or inferred core counts.
        rows = [f"S,{946684800000 + i * 30000},7,{i},20,10,2,3,{1000 - i},1000,999\n"
                for i in range(1, 101)]
        sid = self.import_sample("".join(rows))["id"]
        raw = build_report_data(self.store, sid)
        display = prepare_display(raw)
        system = display["systems"][0]
        metrics = system["metrics"]
        for field, expected in (("cpu_total", [95, 99, 100]), ("cpu_idle", [94, 98, 99]),
                                ("mem_used_mb", [95, 99, 100]), ("mem_total_mb", [1000] * 3),
                                ("mem_avail_mb", [994, 998, 999])):
            self.assertEqual(metrics[field]["count"], 100)
            self.assertEqual([metrics[field][k] for k in ("p95", "p99", "max")], expected)
        for field, expected in (("cpu_user", 20), ("cpu_sys", 10), ("cpu_iow", 2), ("cpu_irq", 3)):
            self.assertEqual(metrics[field]["avg"], expected)
        self.assertEqual(metrics["cpu_total"]["unit"], "%")
        self.assertNotIn("core_evidence", system)
        self.assertEqual([t["count"] for t in system["cpu_exceedances"]["thresholds"]], [10, 5, 1])
        for point in system["series"]["points"]:
            self.assertEqual(point["cpu_idle"], 100 - point["cpu_total"])
            self.assertEqual(point["mem_used_mb"], point["mem_total_mb"] - point["mem_avail_mb"])
            self.assertIsInstance(point["mem_used_mb"], int)
        self.assertIn("mem_percent", raw["systems"][0]["metrics"])
        self.assertNotIn("mem_percent", metrics)
        self.assertEqual(prepare_display(display), display)
        html = render_report(self.store, sid)
        markup = ReportMarkup(html)
        payload = json.loads(markup.scripts[0][1])
        self.assertEqual(payload["systems"][0]["metrics"], metrics)
        for phrase in ("整机 CPU 总占用", "满载 100%", "irq+softirq", "MemTotal", "MemAvailable", "P95", "P99"):
            self.assertIn(phrase, html)
        self.assertNotIn("核数证据", html)
        from perf_report_ui import segment_html, time_label
        section = segment_html(display, system).split('</section>', 1)[0]
        from datetime import datetime
        start = datetime.fromtimestamp(946684830).strftime("%Y年%m月%d日 %H:%M:%S")
        end = datetime.fromtimestamp(946687800).strftime("%Y年%m月%d日 %H:%M:%S")
        self.assertIn("开始测试：" + start, section)
        self.assertIn("结束时间：" + end, section)
        self.assertIn("采集0小时49分钟30秒", section)
        for removed in ("独立时段 · 时间回拨隔离", "全部周期", "系统样本", "进程名称（含 DP-only）", "存在活跃周期"):
            self.assertNotIn(removed, section)
        self.assertIn("内存指标", section)
        self.assertNotIn("单位</th>", section)
        total_row = section.split('>总内存</td>', 1)[1].split('</tr>', 1)[0]
        self.assertEqual(total_row.count('>—</td>'), 2)
        self.assertIn("采集0小时0分钟0秒", time_label({"cycle_axis": [[0, 0]]}))
        self.assertIn("采集时长：—", time_label({"cycle_axis": [[0, None]]}))
        self.assertIn("采集25小时1分钟1秒", time_label({"cycle_axis": [[0, 0], [1, 90061999]]}))

    def test_report_overview_cpu_card_threshold_colors(self):
        from copy import deepcopy
        from perf_report_data import build_report_data
        from perf_report_ui import CSS, prepare_display, segment_html

        data = prepare_display(build_report_data(self.store, self.import_sample()["id"]))
        for value, level in ((0, "low"), (69.99, "low"), (70, "medium"),
                             (79.99, "medium"), (80, "high"), (80.01, "high"),
                             (100, "high"), (None, "unknown")):
            with self.subTest(value=value):
                system = deepcopy(data["systems"][0])
                system["metrics"]["cpu_total"]["avg"] = value
                html = segment_html(data, system)
                self.assertIn('class="overview-primary cpu-' + level + '"', html)
                self.assertIn('<details class="overview-notes"><summary>查看统计口径与图表说明</summary>', html)
                self.assertIn('缺失不补零。</p></details>', html)
                self.assertEqual(system["metrics"]["cpu_total"]["avg"], value)
        for level, color in (("low", "#6bcb77"), ("medium", "#ffd93d"), ("high", "#ff6b6b")):
            self.assertIn('.overview-primary.cpu-' + level + '{--cpu-color:' + color, CSS)
        self.assertIn('.overview-primary .stat-header strong{font-size:32px;color:var(--cpu-color)}', CSS)

    def test_report_primary_modules_open_before_collapsed_modules(self):
        from perf_report import render_report

        report = render_report(self.store, self.import_sample()["id"])
        self.assert_report(report)
        expanded_titles = ("Excel 49 项 · 当前时段逐项实测", "全进程 · 搜索排序与叠加趋势",
                           "关联进程 · 搜索、勾选与精确合并")

        class FoldMarkup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.modules = []
                self.notes = 0

            def handle_starttag(parser, tag, attrs):
                attrs = dict(attrs)
                if tag == "details" and attrs.get("class") != "panel":
                    self.assertNotIn("open", attrs)
                if tag == "section" and "overview" in attrs.get("class", "").split():
                    parser.overview = True
                if tag == "summary":
                    self.assertEqual(parser.stack[-1][0], "details")
                if tag == "p" and attrs.get("class") == "note":
                    # The no-JavaScript warning must remain immediately visible.
                    if not any(t == "noscript" for t, _ in parser.stack):
                        self.assertTrue(any(t == "details" and a.get("class") in
                                            ("overview-notes", "report-notes")
                                            for t, a in parser.stack))
                        parser.notes += 1
                if tag not in ("meta", "input", "br", "hr", "link", "img"):
                    parser.stack.append((tag, attrs))

            def handle_endtag(parser, tag):
                self.assertTrue(parser.stack, tag)
                self.assertEqual(parser.stack.pop()[0], tag)

            def handle_data(parser, text):
                if parser.stack and parser.stack[-1][0] in ("h4", "th", "summary"):
                    self.assertNotIn("单核", text)
                    self.assertNotIn("RSS", text)
                if parser.stack and parser.stack[-1][0] == "summary":
                    if parser.stack[-2][1].get("class") == "panel":
                        parser.modules.append(text)
                        self.assertEqual("open" in parser.stack[-2][1], text in expanded_titles)

        markup = FoldMarkup()
        markup.feed(report)
        markup.close()
        self.assertEqual(markup.stack, [])
        self.assertTrue(markup.overview)
        self.assertGreaterEqual(markup.notes, 9)
        self.assertEqual(markup.modules, [
            *expanded_titles,
            "CPU 超标分布明细 · 整机 100% 口径",
            "活跃 CPU Top30 · 活跃均值", "CPU 峰值 Top30 · 趋势", "内存均值 Top30",
            "物理 / 逻辑 IO · 有效增量及累计", "统计口径与数据来源",
        ])

    def test_report_fold_toggle_initializes_and_resizes_charts(self):
        source = Path("report_assets/report.js").read_text(encoding="utf-8")
        lifecycle = source[source.index("  function resizeVisible("):].rsplit("})();", 1)[0]
        harness = """
const assert = require('node:assert/strict');
const events = {}, callbacks = {};
const fold = {open:false, addEventListener:(event, fn)=>events[event]=fn,
  contains:node=>node===chartNode};
const chartNode = {getClientRects:()=>fold.open?[{}]:[],
  get clientWidth(){return fold.open?600:0;}};
const hiddenNode = {getClientRects:()=>[], clientWidth:0};
const instances = new Map([[chartNode,{chart:null}],[hiddenNode,{chart:null}]]);
let initialized=0, resized=0;
function activate(node,state) {
  initialized++; state.chart={resize:()=>resized++};
}
const document = {contains:()=>true, querySelectorAll:selector=>
  selector==='details'?[fold]:fold.open?[]:[fold]};
const window = {addEventListener:(event,fn)=>callbacks[event]=fn};
const requestAnimationFrame = fn=>fn(), cancelAnimationFrame = ()=>{};
"""
        checks = """
events.toggle();
assert.equal(initialized,0);
fold.open=true; events.toggle();
assert.equal(initialized,1);
assert.equal(resized,0);
fold.open=false; events.toggle();
fold.open=true; events.toggle();
assert.equal(initialized,1);
assert.equal(resized,1);
fold.open=false;
callbacks.beforeprint();
assert.equal(fold.open,true);
callbacks.afterprint();
assert.equal(fold.open,false);
assert.equal(instances.get(hiddenNode).chart,null);
"""
        self.run_report_script(harness + lifecycle + checks)

    def test_report_overview_chart_percentiles_and_time(self):
        source = Path("report_assets/report.js").read_text(encoding="utf-8")
        prefix = source[:source.index("  const observer =")]
        harness = """
const assert = require('node:assert/strict');
global.document = {
  getElementById: () => ({textContent: JSON.stringify({process_fields:[],full_cycle_schema:[],cycle_axis_schema:[]})}),
  documentElement: {classList:{add:()=>{}}}
};
"""
        checks = """
process.env.TZ = 'Asia/Shanghai';
assert.equal(timeLabel(Date.UTC(2026,8,20,13,7,59)), '21:07:59');
assert.equal(timeLabel(Date.UTC(2026,8,20,13,8,0)), '21:08:00');
assert.equal(timeLabel(Date.UTC(2026,8,20,16,0,1,999)), '00:00:01');
assert.equal(fullTimeLabel(Date.UTC(2026,8,20,16,0,1)), '2026-09-21 00:00:01');
assert.equal(timeLabel(0), '08:00:00');
process.env.TZ = 'America/New_York';
assert.equal(timeLabel(Date.UTC(2026,0,1,12)), '07:00:00');
assert.equal(timeLabel(Date.UTC(2026,6,1,12)), '08:00:00');
process.env.TZ = 'Asia/Shanghai';
assert.equal(timeLabel(null), '—');
assert.equal(timeLabel(NaN), '—');
assert.equal(timeLabel(1e20), '—');
const points = [
 {segment:0,cycle:1,ts:1000,cpu_total:10,_run_cpu_total:1},
 {segment:0,cycle:3,ts:3000,cpu_total:20,_run_cpu_total:1},
 {segment:0,cycle:4,ts:4000,cpu_total:30,_run_cpu_total:2},
 {segment:0,cycle:5,ts:null,cpu_total:40,_run_cpu_total:3}
];
const series = overviewSeries(points,'cpu_total',{p95:95,p99:99},'#ff6b6b','%',true);
assert.equal(series.length,2);
assert.deepEqual(series[0].data, [[1000,10],[3000,20]]);
assert.equal(series[0].lineStyle.width,1.5);
assert.equal(lineSeries(points,'cpu_total','CPU','red')[0].lineStyle.width,1);
assert.equal(series[0].markLine.lineStyle.width,1);
assert.equal(series[0].markLine.lineStyle.type,'dashed');
assert.deepEqual(series[0].markLine.data.map(x=>x.yAxis),[95,99]);
assert.equal(series[1].markLine,undefined);
assert.deepEqual(overviewSeries([], 'cpu_total', {p95:null,p99:null},'red','%',true),[]);
const memory = overviewSeries([{segment:0,cycle:1,ts:1000,mem_used_mb:50}],
 'mem_used_mb',{p95:900,p99:990},'green','MB',true);
assert.deepEqual(memory[0].markLine.data.map(x=>x.yAxis),[900,990]);
data.system_fields = ['cpu_total','cpu_user','cpu_sys','cpu_iow','cpu_irq','cpu_idle','mem_total_mb','mem_avail_mb','mem_used_mb'];
const overview = overviewOptions({series:{points:[{segment:0,cycle:1,ts:1000,
 ...Object.fromEntries(data.system_fields.map(f => [f,50]))}]},
 metrics:Object.fromEntries(data.system_fields.map(f => [f,{p95:80,p99:90}]))});
assert.deepEqual(Object.entries(overview.cpu.legend.selected).filter(([,v])=>v),[['整机 CPU 总占用',true]]);
assert.equal(overview.cpu.series.length,6);
assert.deepEqual(overview.memory.legend.selected,{'已使用内存':true,'空闲内存':false});
assert.deepEqual(overview.memory.series.map(s=>s.name),['已使用内存','空闲内存']);
assert.equal(overview.memory.yAxis.name,undefined);
for (const s of overview.memory.series) assert.deepEqual(s.markLine.data.map(x=>x.yAxis),[80,90]);
assert.equal(overview.cpu.series.filter(s=>s.markLine).length,1);
data.system_fields = data.system_fields.map(f => f === 'mem_avail_mb' ? 'mem_free_mb' : f);
const topMemory = overviewOptions({series:{points:[{segment:0,cycle:1,ts:1000,
 mem_used_mb:7516,mem_free_mb:7209,mem_avail_mb:null}]},
 metrics:{mem_used_mb:{p95:7516,p99:7516},mem_free_mb:{p95:7209,p99:7209}}}).memory;
assert.deepEqual(topMemory.legend.selected,{'已使用内存':true,'空闲内存':false});
assert.deepEqual(topMemory.series.map(s=>s.name),['已使用内存','空闲内存']);
assert.deepEqual(topMemory.series[1].data,[[1000,7209]]);
assert.deepEqual(topMemory.series[1].markLine.data.map(x=>x.yAxis),[7209,7209]);
})();
"""
        self.run_report_script(harness + prefix + checks)

    def test_report_process_table_and_reference_curves(self):
        from perf_report import render_report
        from perf_report_ui import PROCESS_HEADERS, process_row

        html = render_report(self.store, self.import_sample()["id"])
        payload = json.loads(ReportMarkup(html).scripts[0][1])
        self.assertLess(html.index("全进程 ·"), html.index("关联进程 ·"))
        self.assertEqual(PROCESS_HEADERS[:2], ["序号", "原始进程名"])
        self.assertEqual(PROCESS_HEADERS[-1], "PID变化")
        self.assertFalse(any("覆盖" in h or "最大并发" in h for h in PROCESS_HEADERS))
        for n, p in enumerate(payload["processes"], 1):
            row = process_row(p, n)
            self.assertEqual(len(row), len(PROCESS_HEADERS))
            self.assertEqual(row[:2], [n, p["name"]])
            self.assertEqual(row[-1], p["pid_changes"])
        for removed in ("状态", "单核均值 %", "单核P95 %", "单核P99 %", "单核峰值 %", "物理读累计 KB", "物理写累计 KB"):
            self.assertNotIn(removed, PROCESS_HEADERS)
        rss_column = PROCESS_HEADERS.index("内存峰值 MB")
        self.assertEqual(PROCESS_HEADERS[rss_column + 1:rss_column + 3], ["累计读 KB", "累计写 KB"])
        for p in payload["processes"]:
            cells = dict(zip(PROCESS_HEADERS, process_row(p, 1)))
            self.assertEqual(cells["活跃P99 %"], p["active"]["metrics"]["cpu1c"]["p99"])
            self.assertEqual(cells["活跃峰值 %"], p["active"]["metrics"]["cpu1c"]["max"])
            for header, field in (("累计读 KB", "rd_kb"), ("累计写 KB", "wr_kb"),
                                  ("逻辑读累计 KB", "rchar_kb"), ("逻辑写累计 KB", "wchar_kb")):
                self.assertEqual(cells[header], p["metrics"][field]["total"])
        self.assertNotIn("不是整机实测 IO", html)
        self.assertEqual(html.count("IO 按本周期已采集 P 进程分别汇总物理读、物理写、逻辑读、逻辑写；"), 2)
        source = Path("report_assets/report.js").read_text(encoding="utf-8")
        prefix = source[:source.index("  data.systems.forEach(setupSegment);")]
        harness = """
const assert = require('node:assert/strict');
process.env.TZ = 'Asia/Shanghai';
class Node {
  constructor() { this.children=[]; this.events={}; this.dataset={}; this.value='';
    this.classList={add(){},toggle(){}}; this.nodes=new Map(); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  setAttribute() {}
  addEventListener(type, callback) { this.events[type]=callback; }
  getClientRects() { return []; }
  querySelector(selector) {
    if (!this.nodes.has(selector)) this.nodes.set(selector,new Node());
    return this.nodes.get(selector);
  }
  querySelectorAll() { return []; }
}
const nodes = new Map();
global.document = {
  documentElement:new Node(), createElement:()=>new Node(),
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id,new Node());
    return nodes.get(id);
  }
};
global.setTimeout = callback => callback();
document.getElementById('report-data').textContent = PAYLOAD;
""".replace("PAYLOAD", json.dumps(json.dumps(payload)))
        checks = """
const system = data.systems[0], snapshot=JSON.stringify(data);
const points = collectedIO(system,data.processes);
assert.equal(points[0].rd_kb,0);
assert.equal(points[0].wr_kb,156);
assert.equal(points[0].rchar_kb,469);
assert.equal(points[0].wchar_kb,184);
const cpu=referenceSeries(system,points,['cpu1c']);
assert.equal(cpu[0].data[0][1],42.04*8);
assert.equal(referenceSeries(system,points,['rss_kb'])[0].data[0][1],5399);
assert.equal(referenceSeries(system,points,io).length,4);
assert.equal(JSON.stringify(data),snapshot);
setupSegment(system);
const option = role => instances.get(document.getElementById(system.id+'-'+role)).option;
for (const {option: chart} of instances.values()) {
  if (chart.xAxis.type !== 'time') continue;
  const ts = Date.UTC(2026,8,20,13,7,59);
  assert.equal(chart.xAxis.axisLabel.formatter(ts), '21:07:59');
  assert.equal(chart.dataZoom.find(z => z.type === 'slider').labelFormatter(ts), '21:07:59');
  assert.equal(chart.xAxis.axisPointer.label.formatter({value:ts}), '2026-09-20 21:07:59');
}
for (const mode of ['overlay','merge']) {
  assert.equal(option(mode+'-cpu').series[0].data[0][1],42.04*8);
  assert.equal(option(mode+'-rss').series[0].data[0][1],5399);
  assert.equal(option(mode+'-io').series.length,4);
}
const root=document.getElementById(system.id);
const searchWrap = new Node(), searchInput = searchWrap.querySelector('.table-search');
const searchTable = searchWrap.querySelector('table');
const searchRow = new Node();
searchRow.textContent='pid-worker pid4次变更，last：100';
searchRow.dataset.search='100, 600, 700, 800, 900';
searchTable.tBodies=[{rows:[searchRow]}];
searchTable.tHead={rows:[{cells:[]}]};
setupTable(searchWrap,()=>{});
for (const [query, hidden] of [['900',false],['100',false],['PID-WORKER',false],['999',true],['',false]]) {
  searchInput.value=query; searchInput.events.input();
  assert.equal(searchRow.hidden,hidden);
}
delete searchRow.dataset.search;
searchInput.value='pid-worker'; searchInput.events.input();
assert.equal(searchRow.hidden,false);
const candidates=root.querySelector('.candidate-list');
const chosen=candidates.children.find(label=>label.children[0].value===data.processes.find(p=>!p.dp_only).id).children[0];
chosen.checked=true; chosen.events.change();
root.querySelector('.merge-apply').events.click();
assert.equal(option('merge-cpu').series.length,2);
assert.equal(option('merge-cpu').series[1].data[0][1],116.30);
assert.equal(option('merge-io').series.length,8);
root.querySelector('.merge-clear').events.click();
assert.equal(option('merge-cpu').series.length,1);
assert.equal(option('merge-io').series.length,4);
assert.equal(JSON.stringify(data),snapshot);
const row = (cycle, ts, values, records=1) => {
  const r=Array(data.full_cycle_schema.length).fill(null);
  Object.entries({cycle,ts,p_records:records,...values}).forEach(([f,v])=>r[index[f]]=v);
  return r;
};
const segment={segment:0,cycle_axis:[[1,1000],[2,2000],[3,3000],[4,4000],[5,null]]};
const members=[
  {segment:0,full_cycles:[row(1,1000,{rd_kb:2,wr_kb:0}),row(2,2000,{rd_kb:null}),row(4,4000,{rd_kb:9})]},
  {segment:0,full_cycles:[row(1,1000,{rd_kb:3}),row(2,2000,{rd_kb:4}),row(5,5000,{rd_kb:100})]},
  {segment:1,full_cycles:[row(1,1000,{rd_kb:1000})]},
  {segment:0,full_cycles:[row(1,1000,{rd_kb:999},0),row(4,4500,{rd_kb:100})]}
];
const summed=collectedIO(segment,members);
assert.deepEqual(summed.map(p=>p.rd_kb),[5,4,null,9,null]);
assert.equal(summed[0].wr_kb,0);
assert.equal(summed[0].rchar_kb,null);
assert.notEqual(summed[1]._run_rd_kb,summed[3]._run_rd_kb);
assert.equal(lineSeries(summed,'rd_kb','IO','red').length,2);
assert.ok(collectedIO(segment,[]).every(p=>io.every(f=>p[f]===null)));
const longAxis=Array.from({length:1200},(_,i)=>[i+1,(i+1)*1000]);
const longSystem={segment:0,cycle_axis:longAxis};
const longProcess={segment:0,full_cycles:longAxis.map(([c,t])=>row(c,t,{rd_kb:c===501?null:1}))};
const sampled=collectedIO(longSystem,[longProcess]);
assert.equal(sampled.length,600);
assert.equal(lineSeries(sampled,'rd_kb','IO','red').length,2);
})();
"""
        self.run_report_script(harness + prefix + checks)

    def test_report_process_pid_summary(self):
        from perf_report import render_report
        from perf_report_ui import process_row

        cases = [
            ([[900]], "900"),
            ([[900], [800], [700], [600]], "600, 700, 800, 900"),
            ([[900], [800], [700], [600], [100]], "pid4次变更，last：100"),
            ([[900], [800], [900], [800], [900]], "pid4次变更，last：900"),
            ([[900], [800], [700], [600], [100, 200]], "pid4次变更，last：100, 200"),
        ]
        for sequence, expected in cases:
            with self.subTest(sequence=sequence):
                lines = []
                for cycle, pids in enumerate(sequence):
                    ts = 946684800123 + cycle * 1000
                    lines.append(SAMPLE.splitlines()[0].replace("946684800123", str(ts)))
                    lines.extend(f"DP,{ts},{pid},1024,1,pid-worker" for pid in pids)
                sid = self.import_sample("\n".join(lines) + "\n")["id"]
                html = render_report(self.store, sid)
                markup = ReportMarkup(html)
                payload = json.loads(markup.scripts[0][1])
                p, = payload["processes"]
                self.assertEqual(p["pid_changes"], len(sequence) - 1)
                self.assertEqual([entry["pids"] for entry in p["pid_path"]], sequence)
                self.assertEqual(process_row(p, 1)[2], expected)
                self.assertEqual(process_row(p, 1)[-1], len(sequence) - 1)
                all_table = html.split('data-table="all"', 1)[1].split('</table>', 1)[0]
                self.assertIn('>' + expected + '</td>', all_table)
                self.assertEqual(markup.processes[0]["data-search"], ', '.join(map(str, p["pids"])))

    def test_report_overview_missing_inputs_and_process_basis(self):
        from perf_report import render_report
        from perf_report_data import build_report_data
        from perf_report_ui import prepare_display
        sid = self.import_sample()["id"]
        data = prepare_display(build_report_data(self.store, sid))
        process = next(p for p in data["processes"] if not p["dp_only"])
        self.assertEqual(process["metrics"]["cpu1c"]["p95"], 116.30)
        self.assertEqual(data["systems"][0]["metrics"]["cpu_total"]["avg"], 42.04)
        for field in ("cpu_total", "cpu_user", "cpu_irq", "mem_avail_mb", "mem_total_mb"):
            with self.subTest(field=field):
                missing_sid = self.import_sample(SAMPLE.splitlines()[0] + "\n")["id"]
                with self.store.connect() as db:
                    db.execute("UPDATE records SET data=json_set(data,?,NULL) "
                               "WHERE session=? AND kind='S'", ("$." + field, missing_sid))
                    # Fixture mutation bypasses immutable imports: invalidate its prior report.
                    db.execute("DELETE FROM report_cache WHERE session=?", (missing_sid,))
                html = render_report(self.store, missing_sid)
                system = json.loads(ReportMarkup(html).scripts[0][1])["systems"][0]
                self.assertEqual(system["metrics"][field]["count"], 0)
                derived = "cpu_idle" if field == "cpu_total" else "mem_used_mb" if field.startswith("mem") else field
                self.assertIsNone(system["metrics"][derived]["p95"])
                self.assertIsNone(system["series"]["points"][0][derived])
        empty = self.import_sample(SAMPLE.splitlines()[-1] + "\n")["id"]
        self.assertIn("整体概览", render_report(self.store, empty))

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
        from perf_report_ui import esc, statistics
        from perf_report import timestamp

        rows = [SAMPLE.replace("42.04", str(cpu)).replace("946684800123", str(946684800123 + cpu))
                for cpu in range(1, 101)]
        sid = self.import_sample("".join(rows))["id"]
        for kind, fields in NUMERIC.items():
            result = self.store.statistics(sid, kind)
            self.assertEqual(set(result["metrics"]), set(fields))
            self.assertEqual(set(result["categories"]), set(CATEGORIES.get(kind, ())))
            self.assertEqual(result["samples"], 200 if kind == "DE" else 100)
            report = statistics(result["metrics"])
            for field in fields:
                metric = result["metrics"][field]
                self.assertEqual(metric["count"], result["samples"])
                for key in ("min", "avg", "p95", "p99", "max"):
                    self.assertIsNotNone(metric[key])
                if field != "cpu":
                    self.assertIn(esc(metric["label"]), report)
            for category in result["categories"].values():
                self.assertEqual(category["count"], result["samples"])
                self.assertEqual(sum(v["count"] for v in category["values"]), category["count"])
                self.assertAlmostEqual(sum(v["percent"] for v in category["values"]), 100)
            if kind == "S":
                self.assert_metric(result["metrics"]["cpu_total"], 100, [1, 50.5, 95, 99, 100])
                self.assertEqual(result["metrics"]["mem_avail_mb"]["avg"], 9326)
            self.assertEqual(timestamp(result["start"]), "2000-01-01T00:00:00.124+00:00")
            self.assertEqual(timestamp(result["end"]), "2000-01-01T00:00:00.223+00:00")

    def test_report_formatting_and_empty_metrics(self):
        from perf_report import render_report, timestamp
        from perf_report_ui import number, statistics

        for value, expected in ((None, "—"), (float('nan'), "—"),
                                (float('inf'), "—"), (-float('inf'), "—"),
                                (0, "0"), (0.5, "0.5"), (-2048, "-2,048"),
                                (12.345, "12.35"), (1234, "1,234"), ("文本", "文本")):
            with self.subTest(value=value):
                self.assertEqual(number(value), expected)
        self.assertEqual(timestamp(None), "—")
        self.assertEqual(timestamp(946684800123), "2000-01-01T00:00:00.123+00:00")
        self.assertEqual(timestamp(2 ** 63 - 1), str(2 ** 63 - 1) + " ms")
        sid = self.import_sample(SAMPLE.splitlines()[0] + "\n")["id"]
        report = render_report(self.store, sid)
        self.assertEqual(self.assert_report(report)["processes"], [])
        result = self.store.statistics(sid, "P")
        self.assertEqual(result["samples"], 0)
        self.assertIsNone(result["start"])
        self.assertIsNone(result["end"])
        for metric in result["metrics"].values():
            self.assert_metric(metric, 0, [None] * 5)
        empty = statistics(result["metrics"])
        self.assertIn('data-sort="0">0</td>', empty)
        self.assertIn('data-sort="">—</td>', empty)

    def test_report_statistics_escape_categories(self):
        from perf_report_ui import esc, grid, statistics

        sid = self.import_sample()["id"]
        result = self.store.statistics(sid, "P")
        payload = '<img src=x onerror="alert(1)">&\'quoted\''
        result["categories"]["pol"]["values"][0]["value"] = payload
        result["metrics"]["cpu1c"]["label"] = payload
        result["metrics"]["cpu1c"]["unit"] = payload
        report = statistics(result["metrics"])
        category = result["categories"]["pol"]["values"][0]["value"]
        report += grid([payload], [[category]], identities=[payload], role=payload)
        self.assertNotIn(payload, report)
        self.assertIn(esc(payload), report)
        markup = ReportMarkup(report)
        self.assertNotIn("img", [tag for tag, _ in markup.tags])
        self.assertTrue(all(not any(k.startswith("on") for k in attrs)
                            for _, attrs in markup.tags))
        self.assertEqual([attrs["data-process"] for _, attrs in markup.tags
                          if "data-process" in attrs], [payload])
        self.assertEqual(esc(None), "—")

    def test_report_compact_contract_and_system_resources(self):
        import re
        from unittest.mock import patch
        from perf_report_ui import esc
        from perf_report import render_report

        rows = [SAMPLE.replace("42.04", str(i)).replace("5399", str(i * 2))
                .replace("946684800123", str(946684800123 + i)) for i in range(1, 101)]
        sid = self.import_sample("".join(rows))["id"]
        with patch.object(self.store, "group_analysis", wraps=self.store.group_analysis) as groups, \
                patch.object(self.store, "report_settings", wraps=self.store.report_settings) as settings:
            report = render_report(self.store, sid)
        groups.assert_not_called()
        settings.assert_not_called()
        data = self.assert_report(report)
        self.assertEqual(len(data["processes"]), 2)
        self.assertEqual(len(data["systems"]), 1)
        for value in data["method"].values():
            self.assertIn(esc(value), report)
        self.assertEqual(data["display_cpu_basis"], "system-100-process-single-core")
        for removed in ("1 测试环境", "6 准入结论", "附录 A", "组同周期合计统计", "请先配置准入进程组"):
            self.assertNotIn(removed, report)
        for phrase in ("选中进程 CPU %", "选中进程 内存 MB", "全进程", "离线自包含报告"):
            self.assertIn(phrase, report)
        metrics = data["systems"][0]["metrics"]
        for field, expected in (("cpu_total", [95, 99, 100]), ("mem_used_mb", [5399] * 3)):
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

    def test_process_references_full_cycle_scope(self):
        from fastapi.testclient import TestClient
        from perf_api import create_app
        p = GroupStoreTests.p
        text = 'S,1,7,25,0,0,0,0,100,300,999\n'
        text += p(1, 1, 'selected', rd_kb=2, wr_kb=0, rchar_kb=7, wchar_kb=9)
        text += p(1, 2, 'other', rd_kb=3, wr_kb=0, rchar_kb=11, wchar_kb=13)
        text += 'S,2,7,0,0,0,0,0,100,300,999\n'
        text += p(3, 1, 'selected', rd_kb=0, wr_kb=4)
        text += 'S,0,7,100,0,0,0,0,0,300,999\n' + p(0, 9, 'other', rd_kb=999)
        sid = self.import_sample(text)['id']
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.wr_kb',NULL) WHERE session=? AND ts=1 AND pid=2", (sid,))
            db.execute("UPDATE records SET data=json_set(data,'$.mem_avail_mb',NULL) WHERE session=? AND ts=2 AND kind='S'", (sid,))
            db.execute("UPDATE records SET data=json_set(data,'$.rd_kb',NULL) WHERE session=? AND ts=3 AND kind='P'", (sid,))
        result = self.store.process_references(sid, 0)
        self.assertFalse(result['sampled'])
        self.assertEqual(result['total'], 3)
        first, absent, orphan = result['points']
        self.assertEqual(first['cpu_total'], 25)
        self.assertEqual(first['mem_used_mb'], 200)  # derive, do not trust logged used
        self.assertEqual([first[f] for f in ('rd_kb', 'wr_kb', 'rchar_kb', 'wchar_kb')], [5, 0, 18, 22])
        self.assertIsNone(absent['rd_kb'])
        self.assertIsNone(absent['mem_used_mb'])
        self.assertEqual(absent['cpu_total'], 0)
        self.assertIsNone(orphan['cpu_total'])
        self.assertIsNone(orphan['mem_used_mb'])
        self.assertIsNone(orphan['rd_kb'])
        self.assertEqual(orphan['wr_kb'], 4)
        self.assertEqual(self.store.process_references(sid, 1)['points'][0]['rd_kb'], 999)
        self.assertEqual(self.store.process_references(sid, 99)['points'], [])
        with TestClient(create_app(self.store.path), base_url='http://127.0.0.1:8765') as client:
            client.headers['X-Session-Token'] = client.get('/api/token').json()['token']
            path = f'/api/sessions/{sid}/process-references'
            response = client.get(path, params={'segment': 0})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), result)
            self.assertEqual(client.get(path).status_code, 422)
            self.assertEqual(client.get(path, params={'segment': -1}).status_code, 422)

    def test_process_references_aggregate_before_sampling(self):
        p = GroupStoreTests.p
        text = ''.join(GroupStoreTests.s(i) + p(i, 1, 'a', rd_kb=70 if i == 451 else 1)
                       + p(i, 2, 'b', rd_kb=80 if i == 451 else 2) for i in range(1000))
        sid = self.import_sample(text)['id']
        result = self.store.process_references(sid, 0, limit=64)
        self.assertTrue(result['sampled'])
        self.assertEqual(result['total'], 1000)
        self.assertLessEqual(len(result['points']), 64)
        self.assertEqual(max(point['rd_kb'] for point in result['points']), 150)

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
            for field, value in dict(ts=ts, name=name, cpu=cpu, cpu1c=cpu + 200, rss_delta_kb=delta,
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
            self.assertEqual([r[sort] for r in rows], [300, 299, 202])
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
            for table in ("process_rank_v3", "process_rank_ready_v3"):
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
        text = p(1, 111, 'same', cpu=1, cpu1c=8) + p(2, 111, 'same', cpu=2, cpu1c=16)
        text += p(3, 222, 'same', cpu=99, cpu1c=792) + p(4, 111, 'same', cpu=3, cpu1c=24)
        text += p(0, 333, 'same', cpu=100, cpu1c=800)  # rollback creates a separate segment
        text += 'DP,0,888,100,1,dma-only\n'
        sid = self.import_sample(text)['id']
        result = self.store.processes(sid, merge_names=True, with_total=True)
        self.assertEqual(result['total'], 3)
        row = next(r for r in result['items'] if r['segment'] == 0)
        self.assertEqual(row['pid_path'], [[111], [222], [111]])
        self.assertEqual(row['pid_changes'], 2)
        self.assertEqual(row['samples'], 4)
        self.assertEqual(row['cpu_avg'], 210)
        self.assertEqual(row['cpu_p95'], 792)
        self.assertEqual(row['cpu_p99'], 792)
        stats = self.store.statistics(sid, 'P', segment=0, name='same')
        self.assertEqual(stats['metrics']['cpu1c']['avg'], row['cpu_avg'])
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
            for table in ('process_name_rank_v2', 'process_name_ready_v2'):
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

    def test_process_cpu1c_ranking_and_legacy_cache(self):
        from fastapi.testclient import TestClient
        from perf_api import create_app
        p = GroupStoreTests.p
        text = ''.join(p(i, 1, 'same', cpu=1, cpu1c=i * 8) for i in range(1, 101))
        text += p(101, 2, 'same', cpu=1, cpu1c=1600)
        text += p(102, 3, 'rival', cpu=99, cpu1c=1)
        text += p(103, 4, 'zero', cpu=99, cpu1c=0)
        text += p(104, 5, 'missing', cpu=99) + 'DP,104,6,10,1,dma\n'
        text += p(1, 7, 'same', cpu=99, cpu1c=2)
        sid = self.import_sample(text)['id']
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_remove(data,'$.cpu1c') WHERE session=? AND pid=5", (sid,))
        single = self.store.processes(sid, q='1', segment=0)[0]
        self.assertEqual([single[k] for k in ('cpu_peak', 'cpu_avg', 'cpu_p95', 'cpu_p99')],
                         [800, 404, 760, 792])
        merged = self.store.processes(sid, merge_names=True, q='same', segment=0)[0]
        self.assertEqual((merged['cpu_peak'], merged['cpu_p95'], merged['cpu_p99']), (1600, 768, 800))
        self.assertAlmostEqual(merged['cpu_avg'], 42000 / 101)
        self.assertEqual(merged['samples'], 101)
        for merge in (False, True):
            for sort in ('cpu_peak', 'cpu_avg', 'cpu_p95', 'cpu_p99'):
                rows = self.store.processes(sid, merge_names=merge, sort=sort, segment=0)
                self.assertEqual(rows[0]['name'], 'same')  # cpu would incorrectly put rival first
                self.assertEqual([r['name'] for r in rows[-4:]],
                                 ['rival', 'zero'] + (['dma', 'missing'] if merge else ['missing', 'dma']))
                self.assertEqual(rows[-3][sort], 0)
                self.assertIsNone(rows[-2][sort])  # no fallback to cpu
                self.assertIsNone(rows[-1][sort])
                page = self.store.processes(sid, merge_names=merge, sort=sort, segment=0, limit=1, offset=1)
                self.assertEqual(page, rows[1:2])
        self.assertEqual(self.store.processes(sid, merge_names=True, segment=1)[0]['cpu_peak'], 2)
        # Simulate populated pre-upgrade caches, with stale cpu-based results.
        versions = (('process_rank_v3', 'process_rank_v2'),
                    ('process_rank_ready_v3', 'process_rank_ready_v2'),
                    ('process_name_rank_v2', 'process_name_rank_v1'),
                    ('process_name_ready_v2', 'process_name_ready_v1'))
        with self.store.connect() as db:
            for current, old in versions:
                db.execute(f'ALTER TABLE {current} RENAME TO {old}')
            for old in ('process_rank_v2', 'process_name_rank_v1'):
                db.execute(f'UPDATE {old} SET cpu_peak=-999,cpu_avg=-999,cpu_p95=-999,cpu_p99=-999')
        reopened = Store(self.store.path)
        self.assertEqual(reopened.processes(sid, q='1', segment=0), [single])
        self.assertEqual(reopened.processes(sid, merge_names=True, q='same', segment=0), [merged])
        with TestClient(create_app(self.store.path), base_url='http://127.0.0.1:8765') as client:
            client.headers['X-Session-Token'] = client.get('/api/token').json()['token']
            for sort in ('cpu_peak', 'cpu_avg', 'cpu_p95', 'cpu_p99'):
                response = client.get(f'/api/sessions/{sid}/processes', params=dict(
                    sort=sort, segment=0, merge_names='true', limit=1))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), [merged])

    def test_process_cache_concurrent_and_empty(self):
        from concurrent.futures import ThreadPoolExecutor
        sid = self.import_sample()["id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.processes(sid), range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM process_rank_v3").fetchone()[0], 2)
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


class GroupStoreTests(ReportAssertions, unittest.TestCase):
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

    def test_report_cache_persists_and_is_session_scoped(self):
        from unittest.mock import patch
        from perf_report import render_report

        sid = self.load(self.s(1) + self.p(1))
        report = render_report(self.store, sid)
        reopened = Store(self.store.path)
        with patch("perf_report.build_report_data", side_effect=AssertionError("不得重复分析")), \
                patch("perf_report.render_document", side_effect=AssertionError("不得重复渲染")):
            self.assertEqual(render_report(reopened, sid), report)
            self.store.save_group(sid, self.config(members=[dict(pid=1, name="a")]))
            self.assertEqual(render_report(reopened, sid), report)
        other = self.load(self.s(2) + self.p(2, name="other"))
        self.assertNotEqual(render_report(reopened, other), report)
        self.assertTrue(reopened.delete(sid))
        with self.assertRaises(KeyError):
            render_report(reopened, sid)
        with reopened.connect() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT session FROM report_cache")], [other])

    def test_report_cache_version_and_corruption_rebuild(self):
        from unittest.mock import patch
        from perf_report import build_report_data, render_report

        sid = self.load(self.s(1) + self.p(1))
        report = render_report(self.store, sid)
        with patch("perf_report.REPORT_CACHE_VERSION", "next-version"), \
                patch("perf_report.build_report_data", wraps=build_report_data) as build:
            self.assertEqual(render_report(self.store, sid), report)
            self.assertEqual(render_report(self.store, sid), report)
            self.assertEqual(build.call_count, 1)
            with self.store.connect() as db:
                self.assertEqual(db.execute("SELECT version FROM report_cache").fetchone()[0], "next-version")
                db.execute("UPDATE report_cache SET document=?", (b"broken",))
            self.assertEqual(render_report(self.store, sid), report)
            self.assertEqual(build.call_count, 2)

    def test_report_cache_failure_retries_and_deletion_during_build(self):
        from unittest.mock import patch
        from perf_report import build_report_data, render_report

        sid = self.load(self.s(1) + self.p(1))
        for target in ("build_report_data", "render_document"):
            with patch("perf_report." + target, side_effect=RuntimeError("generation failed")):
                with self.assertRaises(RuntimeError):
                    render_report(self.store, sid)
            with self.store.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM report_cache").fetchone()[0], 0)
        self.assert_report(render_report(self.store, sid))
        other = self.load(self.s(2) + self.p(2))

        def build_then_delete(store, session_id):
            data = build_report_data(store, session_id)
            store.delete(session_id)
            return data

        with patch("perf_report.build_report_data", side_effect=build_then_delete):
            with self.assertRaises(KeyError):
                render_report(self.store, other)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM report_cache WHERE session=?", (other,)).fetchone())

    def test_report_cache_coalesces_concurrent_requests(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        from unittest.mock import patch
        from perf_report import build_report_data, render_report

        sid = self.load(self.s(1) + self.p(1))
        reopened = Store(self.store.path)
        barrier = threading.Barrier(2)

        def request(store):
            barrier.wait(timeout=10)
            return render_report(store, sid)

        with patch("perf_report.build_report_data", wraps=build_report_data) as build:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(request, self.store)
                second = executor.submit(request, reopened)
                self.assertEqual(first.result(timeout=30), second.result(timeout=30))
            self.assertEqual(build.call_count, 1)

    def test_report_all_identities_including_dp_beyond_top_100(self):
        import re
        from unittest.mock import patch
        from perf_report import render_report

        rows = self.s(1) + "".join(self.p(1, pid, f"worker-{pid}") for pid in range(1, 106))
        rows += "DP,1,1,999999,1,worker-1\nDP,1,999,123456,1,dma-only\n"
        sid = self.load(rows)
        with patch.object(self.store, "processes", side_effect=AssertionError("不得使用排行截断")):
            report = render_report(self.store, sid)
        data = self.assert_report(report)
        processes = {p["name"]: p for p in data["processes"]}
        self.assertEqual(len(processes), 106)
        self.assertEqual(set(processes), {f"worker-{pid}" for pid in range(1, 106)} | {"dma-only"})
        for pid in range(1, 106):
            self.assertEqual(processes[f"worker-{pid}"]["pids"], [pid])
            self.assertEqual(processes[f"worker-{pid}"]["metrics"]["cpu"]["count"], 1)
        self.assertEqual(processes["worker-1"]["dp_records"], 1)
        dma = processes["dma-only"]
        self.assertEqual(dma["pids"], [999])
        self.assertTrue(dma["dp_only"])
        for metric in dma["metrics"].values():
            self.assert_metric(metric, 0, [None] * 5)
        self.assertIn("DP-only", report)
        self.assertIn("整机 CPU 总占用", report)

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
        data = self.assert_report(report)
        expected = {(0, "a"): ([1, 2], 44, 404),
                    (0, "renamed"): ([1], 22, 202), (1, "a"): ([1], 44, 404)}
        self.assertEqual(len(data["processes"]), 3)
        self.assertEqual([s["segment"] for s in data["systems"]], [0, 1])
        self.assertEqual([s["samples"] for s in data["systems"]], [1, 1])
        for process in data["processes"]:
            pids, cpu, rss = expected[(process["segment"], process["name"])]
            self.assertEqual(process["pids"], pids)
            self.assert_metric(process["metrics"]["cpu"], 1, [cpu] * 5)
            self.assert_metric(process["metrics"]["rss_kb"], 1, [rss] * 5)
            self.assertEqual(len(process["full_cycles"]), 1)
            cycle = dict(zip(data["full_cycle_schema"], process["full_cycles"][0]))
            self.assertEqual((cycle["cpu"], cycle["rss_kb"], cycle["pids"]), (cpu, rss, pids))
            self.assertEqual(process["concurrent"], len(pids) > 1)
        # 完整载荷一次生成；不再按PID调用旧采样接口。
        series.assert_not_called()

    def test_report_zero_null_and_actual_cpu_plot_coordinates(self):
        from perf_report import render_report

        rows = self.s(1).replace(",7,1,", ",7,40,") + self.p(1, cpu=20, cpu1c=960, rss_kb=0)
        rows += self.s(2).replace(",7,1,", ",7,40,") + self.p(2, cpu=0, cpu1c=800, rss_kb=0)
        rows += self.s(3).replace(",7,1,", ",7,40,") + self.p(3, cpu=999, rss_kb=999)
        sid = self.load(rows)
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.cpu1c',NULL,'$.rss_kb',NULL) "
                       "WHERE session=? AND kind='P' AND ts=3", (sid,))
        data = self.assert_report(render_report(self.store, sid))
        process = data["processes"][0]
        self.assert_metric(process["metrics"]["cpu"], 3, [0, 1019 / 3, 999, 999, 999])
        self.assert_metric(process["metrics"]["cpu1c"], 2, [800, 880, 960, 960, 960])
        self.assert_metric(process["metrics"]["rss_kb"], 2, [0] * 5)
        self.assertEqual([row[2:4] for row in process["full_cycles"]],
                         [[20, 960], [0, 800], [999, None]])
        self.assertEqual(process["active"]["cycles"], 2)
        cpu = self.report_lines(process["series"]["points"], "cpu1c")
        rss = self.report_lines(process["series"]["points"], "rss_kb", 1024)
        system = self.report_lines(data["systems"][0]["series"]["points"], "cpu_total", 1 / 8)
        self.assertEqual(cpu, [[[1, 960], [2, 800]]])
        self.assertEqual(rss, [[[1, 0], [2, 0]]])
        self.assertEqual(system, [[[1, 320], [2, 320], [3, 320]]])
        self.assert_lines(cpu, 2, 1)
        self.assert_lines(rss, 2, 1)
        self.assert_lines(system, 3, 2)

    def test_report_series_metric_gaps_and_cycle_boundaries(self):
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
                sid = self.load("".join(self.s(i) + self.p(i, name=case, cpu=i, cpu1c=i, rss_kb=i)
                                       for i in range(1, 7)))
                with self.store.connect() as db:
                    db.execute("UPDATE records SET " + assignment +
                               " WHERE session=? AND kind='P' AND ts=?", (sid, ts))
                series = self.store.report_series(sid, "P", 0, pid=1, name=case)
                self.assertFalse(series["sampled"])
                self.assertEqual(series["total"], 6)
                points = series["points"]
                for field, expected in (("cpu", cpu_lines), ("rss_kb", rss_lines)):
                    count = 5 if case == field.replace("rss_kb", "rss") + "_null" else 6
                    self.assert_lines(self.report_lines(points, field), count, expected)
                    self.assert_lines(self.report_lines(points, field, mark_runs=True), count, expected)
                if case in ("cpu_null", "rss_null"):
                    broken = "cpu" if case == "cpu_null" else "rss_kb"
                    intact = "rss_kb" if broken == "cpu" else "cpu"
                    self.assertNotEqual(points[1]["_run_" + broken], points[3]["_run_" + broken])
                    self.assertEqual(points[1]["_run_" + intact], points[3]["_run_" + intact])

    def test_report_downsampling_preserves_hidden_gaps_not_artificial_gaps(self):
        from perf_report import render_report

        sid = self.load("".join(self.s(i) + self.p(i, cpu=i, cpu1c=i, rss_kb=i) for i in range(1, 1002)))
        continuous = self.store.report_series(sid, "P", 0, pid=1, name="a", limit=4)
        self.assertTrue(continuous["sampled"])
        self.assertEqual(continuous["total"], 1001)
        self.assertGreaterEqual(len(continuous["points"]), 2)
        self.assertLessEqual(len(continuous["points"]), 4)
        self.assertTrue(any(b["cycle"] > a["cycle"] + 1
                            for a, b in zip(continuous["points"], continuous["points"][1:])))
        self.assert_lines(self.report_lines(continuous["points"], "cpu"),
                          len(continuous["points"]), len(continuous["points"]) - 1)
        full_report = render_report(self.store, sid)
        full_data = self.assert_report(full_report)
        self.assertEqual(len(full_data["processes"][0]["full_cycles"]), 1001)
        self.assert_metric(full_data["processes"][0]["metrics"]["cpu1c"],
                           1001, [1, 501, 951, 991, 1001])
        full_points = full_data["processes"][0]["series"]["points"]
        self.assertGreater(len(full_points), 2)
        self.assertLessEqual(len(full_points), 600)
        self.assert_lines(self.report_lines(full_points, "cpu1c"), len(full_points), len(full_points) - 1)
        raw_points = [dict(zip(full_data["full_cycle_schema"], row), segment=0)
                      for row in full_data["processes"][0]["full_cycles"]]
        self.assert_lines(self.report_lines(raw_points, "cpu1c", mark_runs=True), 600, 599)
        with self.store.connect() as db:
            db.execute("UPDATE records SET data=json_set(data,'$.cpu',NULL,'$.cpu1c',NULL) "
                       "WHERE session=? AND kind='P' AND ts=501", (sid,))
            db.execute("UPDATE records SET data=json_set(data,'$.cpu_total',NULL) "
                       "WHERE session=? AND kind='S' AND ts=501", (sid,))
            # This test deliberately edits an already analyzed source snapshot.
            db.execute("DELETE FROM report_cache WHERE session=?", (sid,))
        gapped = self.store.report_series(sid, "P", 0, pid=1, name="a", limit=4)
        points = gapped["points"]
        self.assertTrue(all(point["cpu"] is not None for point in points))
        self.assertLess(points[0]["ts"], 501)
        self.assertGreater(points[-1]["ts"], 501)
        self.assertNotEqual(points[0]["_run_cpu"], points[-1]["_run_cpu"])
        self.assertEqual(points[0]["_run_rss_kb"], points[-1]["_run_rss_kb"])
        for field, count in (("cpu", len(points) - 2), ("rss_kb", len(points) - 1)):
            self.assert_lines(self.report_lines(points, field), len(points), count)
        system = self.store.report_series(sid, "S", 0, limit=4)
        self.assertNotEqual(system["points"][0]["_run_cpu_total"],
                            system["points"][-1]["_run_cpu_total"])
        report = render_report(self.store, sid)
        data = self.assert_report(report)
        process = data["processes"][0]
        self.assertEqual(len(process["full_cycles"]), 1001)
        self.assert_metric(process["metrics"]["cpu1c"], 1000, [1, 501, 951, 991, 1001])
        self.assert_metric(process["metrics"]["rss_kb"], 1001, [1, 501, 951, 991, 1001])
        self.assertEqual(process["full_cycles"][500][3], None)
        for series, field, label in ((process["series"], "cpu1c", "进程 CPU"),
                                      (data["systems"][0]["series"], "cpu_total", "系统 CPU")):
            self.assertTrue(series["sampled"])
            self.assertEqual(series["total"], 1001)
            self.assertLessEqual(len(series["points"]), 600)
            self.assertNotIn(501, [p["ts"] for p in series["points"]], "真实null缺口应隐藏在采样点之间")
            valid = [p for p in series["points"] if p[field] is not None]
            self.assertGreater(len(valid), 2)
            self.assertLess(valid[0]["ts"], 501)
            self.assertGreater(valid[-1]["ts"], 501)
            self.assertNotEqual(valid[0]["_run_" + field], valid[-1]["_run_" + field])
            # 选择真实缺口两侧相邻采样点；即使null没有入选也不能连线。
            left = max((p for p in valid if p["ts"] < 501), key=lambda p: p["ts"])
            right = min((p for p in valid if p["ts"] > 501), key=lambda p: p["ts"])
            self.assertNotEqual(left["_run_" + field], right["_run_" + field])
            groups = self.report_lines(series["points"], field)
            self.assert_lines(groups, len(valid), len(valid) - 2)
            self.assertTrue(all(not (g[0][0] < 501 < g[-1][0]) for g in groups))
        rss_points = process["series"]["points"]
        self.assert_lines(self.report_lines(rss_points, "rss_kb"), len(rss_points), len(rss_points) - 1)
        raw_points = [dict(zip(data["full_cycle_schema"], row), segment=0)
                      for row in process["full_cycles"]]
        self.assert_lines(self.report_lines(raw_points, "cpu1c", mark_runs=True), 600, 598)

    def test_report_chart_requires_same_segment_known_run_and_increasing_time(self):
        first = dict(ts=1, cpu1c=10, segment=0, _run_cpu1c=1)
        cases = [(dict(ts=1000000, segment=0, _run_cpu1c=1), 1),
                 (dict(ts=2, segment=1, _run_cpu1c=1), 0),
                 (dict(ts=2, segment=0, _run_cpu1c=2), 0),
                 (dict(ts=2, segment=0, _run_cpu1c=None), 0),
                 (dict(ts=1, segment=0, _run_cpu1c=1), 0),
                 (dict(ts=0, segment=0, _run_cpu1c=1), 0)]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assert_lines(self.report_lines([first, dict(first, **changes)], "cpu1c"), 2, expected)
        for invalid in (None, "1"):
            with self.subTest(invalid=invalid):
                self.assert_lines(self.report_lines([first, dict(first, ts=invalid)], "cpu1c"), 1, 0)
                self.assert_lines(self.report_lines([first, dict(first, ts=2, cpu1c=invalid)], "cpu1c"), 1, 0)

    def test_report_offline_markup_and_untrusted_text(self):
        from html import escape
        from html.parser import HTMLParser
        from perf_report import render_report

        payload = '</summary><script>alert("x")</script><img src="https://example.invalid/x" onerror="alert(1)">&'
        sid = self.load(self.p(1, name=payload))
        with self.store.connect() as db:
            db.execute("UPDATE sessions SET name=? WHERE id=?", (payload, sid))
        report = render_report(self.store, sid)
        self.assertNotIn(payload, report)
        self.assertGreaterEqual(report.count(escape(payload, quote=True)), 3)
        data = self.assert_report(report)
        self.assertEqual(data["session"]["name"], payload)
        self.assertEqual(data["processes"][0]["name"], payload)
        self.assertEqual(len(ReportMarkup(report).processes), 1)
        self.assertIn('data-chart="system-cpu"', report)

    def test_report_only_dp_without_system_does_not_fabricate_samples(self):
        from perf_report import render_report

        sid = self.load("DP,1,8,1024,1,dma\n")
        report = render_report(self.store, sid)
        data = self.assert_report(report)
        self.assertEqual(len(data["processes"]), 1)
        process = data["processes"][0]
        self.assertTrue(process["dp_only"])
        self.assertEqual((process["p_records"], process["dp_records"]), (0, 1))
        self.assertEqual(process["full_cycles"][0][2:9], [None] * 7)
        self.assertEqual(data["systems"][0]["samples"], 0)
        self.assertEqual(data["systems"][0]["series"]["points"], [])
        self.assertIn("DP-only", report)
        for field in data["process_fields"]:
            self.assertEqual(self.report_lines(process["series"]["points"], field), [])
        self.assertEqual(self.report_lines(data["systems"][0]["series"]["points"], "cpu_total"), [])
        for metrics in (process["metrics"], data["systems"][0]["metrics"]):
            for metric in metrics.values():
                self.assert_metric(metric, 0, [None] * 5)

    def test_report_merged_cycles_reject_duplicate_and_dp_missing_members(self):
        from perf_report import render_report

        fields = ["cpu", "cpu1c", "rss_kb", "rd_kb", "wr_kb", "rchar_kb", "wchar_kb"]
        def member(ts, pid, value):
            return self.p(ts, pid, **dict.fromkeys(fields, value))

        rows = self.s(100) + member(100, 1, 2) + member(100, 2, 3)
        rows += "DP,100,1,999999,1,a\n"  # 同一成员的DP不是额外数值来源。
        rows += self.s(101) + member(101, 1, 10) * 2 + member(101, 2, 20)
        rows += self.s(102) + member(102, 1, 10) + "DP,102,2,999999,1,a\n"
        rows += self.s(103) + member(103, 1, 0) + self.s(104)
        rows += self.s(1) + member(1, 1, 9)  # 回拨开启新段，不能合并统计。
        data = self.assert_report(render_report(self.store, self.load(rows)))
        self.assertEqual(len(data["processes"]), 2)
        first, second = sorted(data["processes"], key=lambda p: p["segment"])
        self.assertEqual((first["segment"], second["segment"]), (0, 1))
        cycles = [dict(zip(data["full_cycle_schema"], row)) for row in first["full_cycles"]]
        self.assertEqual([c["ts"] for c in cycles], [100, 101, 102, 103])
        self.assertEqual([c["pids"] for c in cycles], [[1, 2], [1, 2], [1, 2], [1]])
        self.assertEqual([c["duplicate_pids"] for c in cycles], [[], [1], [], []])
        self.assertEqual([c["p_records"] for c in cycles], [2, 3, 1, 1])
        self.assertEqual([c["dp_records"] for c in cycles], [1, 0, 1, 0])
        self.assertEqual((first["p_records"], first["dp_records"]), (7, 2))
        self.assertEqual((first["concurrent_cycles"], first["max_concurrent_pids"]), (3, 2))
        self.assertEqual([p["pids"] for p in first["pid_path"]], [[1, 2], [1]])
        self.assertEqual(first["pid_changes"], 1)
        coverage = first["coverage"]
        for key, value in dict(cycles=5, observed_cycles=4, p_cycles=4,
                               missing_cycles=1, missing_p_cycles=1, duplicate_cycles=1,
                               complete_cycles=2, observed_percent=80, complete_percent=40).items():
            self.assertEqual(coverage[key], value, key)
        self.assertEqual(first["active"]["cycles"], 1)
        self.assertEqual(first["active"]["percent_of_segment"], 20)
        for field in fields:
            with self.subTest(field=field):
                self.assertEqual([c[field] for c in cycles], [5, None, None, 0])
                self.assert_metric(first["metrics"][field], 2, [0, 2.5, 5, 5, 5])
                self.assert_metric(first["active"]["metrics"][field], 1, [5] * 5)
                self.assertEqual(coverage["metrics"][field], dict(valid_cycles=2, percent=40))
                self.assert_metric(second["metrics"][field], 1, [9] * 5)
        self.assertEqual(second["pid_changes"], 0)

    def test_report_merged_fields_are_independently_nullable(self):
        from perf_report import render_report

        fields = ["cpu", "cpu1c", "rss_kb", "rd_kb", "wr_kb", "rchar_kb", "wchar_kb"]
        for missing in fields:
            with self.subTest(missing=missing):
                rows = self.s(1) + self.p(1, 1, missing, **dict.fromkeys(fields, 2))
                rows += self.p(1, 2, missing, **dict.fromkeys(fields, 3))
                rows += self.s(2) + self.p(2, 1, missing, **dict.fromkeys(fields, 0))
                sid = self.load(rows)
                with self.store.connect() as db:
                    db.execute("UPDATE records SET data=json_set(data,?,NULL) "
                               "WHERE session=? AND kind='P' AND pid=2", ("$." + missing, sid))
                data = self.assert_report(render_report(self.store, sid))
                self.assertEqual(len(data["processes"]), 1)
                process = data["processes"][0]
                self.assertEqual(process["coverage"]["complete_cycles"], 1)
                self.assertEqual(process["coverage"]["duplicate_cycles"], 0)
                self.assertEqual(process["active"]["cycles"], int(missing != "cpu1c"))
                cycle = dict(zip(data["full_cycle_schema"], process["full_cycles"][0]))
                for field in fields:
                    absent = field == missing
                    self.assertEqual(cycle[field], None if absent else 5)
                    self.assert_metric(process["metrics"][field], 1 if absent else 2,
                                       [0] * 5 if absent else [0, 2.5, 5, 5, 5])
                    self.assertEqual(process["coverage"]["metrics"][field],
                                     dict(valid_cycles=1 if absent else 2, percent=50 if absent else 100))
                    active = missing != "cpu1c" and not absent
                    self.assert_metric(process["active"]["metrics"][field], int(active),
                                       [5] * 5 if active else [None] * 5)

    def test_report_excel49_missing_entries_and_source(self):
        from perf_report import render_report

        data = self.assert_report(render_report(self.store, self.load(self.s(1))))
        required = data["required"]
        self.assertEqual(len(required), 49)
        self.assertEqual([item["excel_row"] for item in required],
                         list(range(7, 38)) + list(range(39, 57)))
        self.assertEqual(data["processes"], [])
        from unittest.mock import patch
        from perf_report_ui import required_table
        with patch("perf_report_ui.grid", return_value="") as table:
            required_table(data, 0)
        headers, rows = table.call_args_list[0].args[:2]
        self.assertNotIn("候选（不计实测）", headers)
        self.assertEqual(len(headers), 13)
        self.assertEqual(len(rows), 49)
        self.assertTrue(all(len(row) == len(headers) for row in rows))
        self.assertEqual(data["required_source"]["file"], "8255内部-资源分布策略.xlsx")
        self.assertEqual(data["required_source"]["sheet"], "Sheet1")
        self.assertEqual(data["required_source"]["rows"], [[7, 37], [39, 56]])
        self.assertEqual(data["required_source"]["sha256"],
                         "fe8f5b0b2ef37604bcbe8f20b110365c032b8669d1a9da05ca353d1f6d3e4988")
        for item in required:
            with self.subTest(row=item["excel_row"]):
                self.assertEqual(item["status"], "missing")
                self.assertTrue(item["missing"])
                self.assertEqual(item["matches"], [])
                self.assertEqual(item["candidates"], [])
                self.assertEqual(item["segments"], [dict(segment=0, status="missing", match=None,
                                                       process_ids=[], candidate_ids=[])])
                self.assertEqual(item["module"], "应用服务" if item["excel_row"] <= 37 else "系统服务")
                self.assertTrue(item["business"])
                self.assertTrue(item["name"])
                self.assertEqual(item["truncated"], item["excel_row"] == 56)

    def test_report_excel_matching_is_segment_local_and_ambiguity_safe(self):
        from perf_report import render_report

        webview = "com.android.webview:sandboxed_process0:org.chromium.content.app.SandboxedProcessServic"
        names = ["/system/bin/mediaserver", "mediaserver", "audioserver",
                 "/vendor/bin/logd", "/other/bin/logd", webview, webview + "e:0"]
        rows = self.s(100) + "".join(self.p(100, pid, name) for pid, name in enumerate(names, 1))
        rows += "DP,100,20,99999,1,/system/bin/surfaceflinger\n"
        rows += self.s(1) + self.p(1, name="/system/bin/logd")
        data = self.assert_report(render_report(self.store, self.load(rows)))
        required = {item["excel_row"]: item for item in data["required"]}
        processes = {(p["segment"], p["name"]): p for p in data["processes"]}
        self.assertEqual(len(required), 49)
        self.assertEqual(len(processes), 9)
        def pid(segment, name):
            return processes[(segment, name)]["id"]

        expected = {
            44: ("collected", "exact", [pid(0, names[0])], []),
            45: ("collected", "basename", [pid(0, "audioserver")], []),
            47: ("dp_only", "exact", [pid(0, "/system/bin/surfaceflinger")], []),
            51: ("pending_confirmation", "ambiguous", [],
                 [pid(0, "/vendor/bin/logd"), pid(0, "/other/bin/logd")]),
            56: ("pending_confirmation", "candidate", [], [pid(0, webview), pid(0, webview + "e:0")]),
        }
        for row, item in required.items():
            with self.subTest(row=row):
                self.assertEqual([s["segment"] for s in item["segments"]], [0, 1])
                first, second = item["segments"]
                status, mode, matches, candidates = expected.get(row, ("missing", None, [], []))
                self.assertEqual((first["status"], first["match"]), (status, mode))
                self.assertEqual(first["process_ids"], matches)
                self.assertCountEqual(first["candidate_ids"], candidates)
                later = [pid(1, "/system/bin/logd")] if row == 51 else []
                self.assertEqual(second, dict(segment=1, status="collected" if later else "missing",
                                             match="exact" if later else None,
                                             process_ids=later, candidate_ids=[]))
                self.assertEqual(item["matches"], matches + later)
                self.assertCountEqual(item["candidates"], candidates)
                self.assertEqual(item["missing"], not bool(matches + later))
                self.assertEqual(item["status"], "collected" if later else status)
        assigned = {(0, names[0]): [44], (0, "audioserver"): [45],
                    (0, "/system/bin/surfaceflinger"): [47], (1, "/system/bin/logd"): [51]}
        for key, process in processes.items():
            self.assertEqual(process["required_rows"], assigned.get(key, []), key)

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
        from perf_report import render_report
        from perf_report_ui import grid, number, statistics

        sid = self.load("".join(self.p(i, cpu1c=i, rss_kb=i) for i in range(1, 101)))
        baseline = render_report(self.store, sid)
        member = dict(pid=1, name="a", service_category="应用服务",
                      related_service="<unsafe>&", foreground="Y")
        group = self.store.save_group(sid, self.config(scene="后台", members=[member]))
        self.store.save_report_settings(sid, dict(cpu_platform="<platform>", kdmips_per_core=28.75))
        resources = self.store.group_resources(sid, group["id"])
        row = resources["rows"][0]
        self.assertEqual(row["statistics"]["cpu1c"],
                         dict(count=100, min=1, avg=50.5, max=100, p95=95, p99=99))
        for key, expected in dict(min=0.2875, avg=14.51875, p95=27.3125,
                                  p99=28.4625, max=28.75).items():
            self.assertAlmostEqual(row["kdmips"][key], expected)
        self.assertEqual(number(row["kdmips"]["p99"]), "28.46")
        self.assertEqual(resources["settings"]["cpu_platform"], "<platform>")
        report = grid(["平台", "关联服务", "分类"], [[resources["settings"]["cpu_platform"],
                      row["member"]["related_service"], row["member"]["service_category"]]])
        self.assertIn("&lt;platform&gt;", report)
        self.assertIn("&lt;unsafe&gt;&amp;", report)
        self.assertNotIn("<unsafe>", report)
        self.assertIn("应用服务", report)
        self.assertIn('>27.31</td>', statistics(row["statistics"]))
        self.assertIn('>28.75</td>', statistics(row["statistics"]))
        for scene in ("前台", "后台", "未标注"):
            with self.subTest(scene=scene):
                saved = self.store.save_group(sid, self.config(scene=scene, members=[member]), group["id"])
                updated = self.store.group_resources(sid, group["id"])
                self.assertEqual(updated["group"]["scene"], scene)
                self.assertEqual(updated["group"], saved)
                self.assertEqual(updated["rows"], resources["rows"])
                for key, value in member.items():
                    self.assertEqual(updated["rows"][0]["member"][key], value)
                self.assert_report_independent(sid, baseline)

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
        from perf_report import render_report
        from perf_report_ui import esc, grid

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
        manual = grid(fields, [[settings[field] for field in fields]])
        for value in settings.values():
            self.assertIn(esc(value), manual)
            self.assertNotIn(value, manual)
        member = group["members"][0]
        metadata = grid(["组", "说明", "类型", "用途", "前台", "后台"], [[
            group["name"], group["description"], member["process_type"], member["purpose"],
            member["foreground"], member["background"]]])
        for prefix in ("group", "desc", "type", "purpose"):
            self.assertIn(esc(prefix + payload), metadata)
            self.assertNotIn(prefix + payload, metadata)
        self.assertIn('data-sort="Y">Y</td><td data-sort="N">N</td>', metadata)
        analyses = {kind: self.store.group_analysis(sid, group["id"], kind) for kind in ("P", "DP")}
        self.assertEqual(len(group["members"]), 1)
        self.assertEqual(analyses["DP"]["coverage"]["missing"], 1)
        for metric in analyses["DP"]["statistics"]["metrics"].values():
            self.assert_metric(metric, 0, [None] * 5)
        self.assertEqual(analyses["P"]["statistics"]["samples"], 1)
        self.assertIn(esc(payload), baseline)
        self.assertEqual(self.assert_report(baseline)["processes"][0]["name"], payload)
        self.assert_report_independent(sid, baseline)
        for conclusion in ("待评估", "准入", "有条件准入", "不准入"):
            saved = self.store.save_report_settings(sid, dict(conclusion=conclusion))
            self.assertEqual(self.store.report_settings(sid), saved)
            self.assertEqual(saved["conclusion"], conclusion)
            self.assert_report_independent(sid, baseline)

    def test_report_group_offset_peaks_k_and_analysis_reuse(self):
        from unittest.mock import patch
        from perf_report import render_report
        from perf_report_ui import number, scaled, statistics

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
            module = statistics(analyses["P"]["statistics"]["metrics"])
        analysis.assert_not_called()
        metrics = analyses["P"]["statistics"]["metrics"]
        self.assert_metric(metrics["cpu1c"], 100, [808] * 5)
        self.assert_metric(metrics["rss_kb"], 100, [101 * 1024] * 5)
        self.assert_metric(analyses["DP"]["statistics"]["metrics"]["size_kb"], 100, [101 * 1024] * 5)
        self.assert_metric(analyses["DP"]["statistics"]["metrics"]["objs"], 100, [3] * 5)
        self.assertEqual(analyses["P"]["coverage"],
                         dict(cycles=100, complete=100, partial=0, missing=0, duplicates=0))
        for key in ("p95", "p99", "max"):
            self.assertEqual(number(scaled(metrics["cpu1c"][key], 100 / factor)), "232.3")
            self.assertEqual(scaled(metrics["rss_kb"][key], 1024), 101)
        self.assertIn('>232.3</td>', module)
        self.assertIn('>101</td>', module)
        for field in ("rd_kb", "wr_kb", "rchar_kb", "wchar_kb"):
            m = metrics[field]
            self.assertEqual(m["count"], 100)
            self.assertEqual(m["total"], m["avg"] * 100)
        baseline = render_report(self.store, sid)
        self.assert_report_independent(sid, baseline)
        overlap = self.store.save_group(sid, self.config(name="重叠组"))
        self.assertEqual(self.store.group_analysis(sid, overlap["id"])["statistics"]["metrics"],
                         analyses["P"]["statistics"]["metrics"])
        self.assertEqual(scaled(analyses["DP"]["statistics"]["metrics"]["size_kb"]["max"], 1024), 101)
        self.assert_report_independent(sid, baseline)

    def test_report_low_coverage_null_duplicate_and_empty_scope(self):
        from perf_report import render_report
        from perf_report_ui import statistics

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
        self.assertEqual(analyses["P"]["coverage"],
                         dict(cycles=5, complete=2, partial=2, missing=1, duplicates=1))
        self.assertEqual(analyses["DP"]["coverage"],
                         dict(cycles=5, complete=1, partial=1, missing=3, duplicates=0))
        metrics = analyses["P"]["statistics"]["metrics"]
        self.assertEqual(metrics["cpu"]["count"], 1)
        self.assertEqual(metrics["rss_kb"]["count"], 2)
        for field, percent in (("cpu", 20), ("rss_kb", 40)):
            self.assertEqual(metrics[field]["count"] / analyses["P"]["statistics"]["samples"] * 100, percent)
        # 完整周期只说明成员记录齐全，字段缺失不应补零。
        self.assertGreater(analyses["P"]["coverage"]["complete"], metrics["cpu"]["count"])
        baseline = render_report(self.store, sid)
        self.assert_report_independent(sid, baseline)
        group = self.store.save_group(sid, self.config(start=10, end=20), group["id"])
        analyses = {kind: self.store.group_analysis(sid, group["id"], kind) for kind in ("P", "DP")}
        for analysis in analyses.values():
            self.assertEqual(analysis["series"], dict(points=[], total=0, sampled=False))
            self.assertEqual(analysis["coverage"],
                             dict(cycles=0, complete=0, partial=0, missing=0, duplicates=0))
            result = analysis["statistics"]
            self.assertEqual(result["samples"], 0)
            self.assertIsNone(result["start"])
            self.assertIsNone(result["end"])
            for metric in result["metrics"].values():
                self.assert_metric(metric, 0, [None] * 5)
            self.assertIn('data-sort="">—</td>', statistics(result["metrics"]))
        self.assert_report_independent(sid, baseline)

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


class ApiTests(ReportAssertions, unittest.TestCase):
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
        self.assertIn('data-chart="system-cpu"', report.text)
        self.assertIn("attachment", report.headers["content-disposition"])
        self.assertEqual(self.client.delete(base, headers=self.headers).json(), {"deleted": True})
        self.assertEqual(self.client.get(base, headers=self.headers).status_code, 404)

    def test_reopened_analysis_uses_persistent_report_and_overview(self):
        from contextlib import contextmanager
        from fastapi.testclient import TestClient
        from unittest.mock import patch
        import sqlite3
        from perf_api import create_app

        sid = self.upload().json()["id"]
        base = "/api/sessions/" + sid
        overview = self.client.get(base, headers=self.headers)
        report = self.client.get(base + "/report", headers=self.headers)
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(report.status_code, 200)
        app = create_app(self.app.state.store.path)
        connect = app.state.store.connect

        @contextmanager
        def cached_only():
            with connect() as db:
                db.set_authorizer(lambda action, table, *_:
                                  sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table == "records"
                                  else sqlite3.SQLITE_OK)
                yield db

        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = {"X-Session-Token": client.get("/api/token").json()["token"]}
            with patch.object(app.state.store, "connect", cached_only), \
                    patch("perf_report.build_report_data", side_effect=AssertionError("不应重新分析")), \
                    patch("perf_report.render_document", side_effect=AssertionError("不应重新生成")):
                self.assertEqual(client.get(base, headers=headers).json(), overview.json())
                cached = client.get(base + "/report", headers=headers)
                self.assertEqual(cached.status_code, 200)
                self.assertEqual(cached.text, report.text)
                self.assertEqual(cached.headers["content-security-policy"], report.headers["content-security-policy"])
            self.assertEqual(client.delete(base, headers=headers).status_code, 200)
            self.assertEqual(client.get(base + "/report", headers=headers).status_code, 404)

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
        self.assertEqual(ADB_FILE_BYTES, 1024 ** 3)
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
        from perf_report_ui import REPORT_CSP

        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.headers["content-security-policy"], REPORT_CSP + "; sandbox allow-scripts")
        data = self.assert_report(report.text)
        self.assertEqual(data["session"]["name"], payload)
        self.assertIn(payload, [p["name"] for p in data["processes"]])

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
                self.assertIn('data-chart="system-cpu"', report.text)
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

