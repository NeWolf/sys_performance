import html
import io
import json
from copy import deepcopy
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import perf_slate
from perf_admission import build_admission
from perf_parser import SCHEMAS
from perf_report_data import KDMIPS_PER_CORE, REQUIRED_PROCESSES, build_report_data
from perf_report_ui import EXCEL_BUDGETS
from perf_store import Store


def paragraph(text, **marks):
    return {"type": "p", "children": [dict(text=text, **marks)]}


def heading(text, level):
    return {"type": "list", "value": "ordered", "header": level,
            "children": [{"text": text, "bold": True}]}


def row(values, header=False, shaded=False):
    cells = []
    for text in values:
        cell = {"type": "table-cell", "rowSpan": 1, "colSpan": 1,
                "children": [dict(paragraph(text, **({"bold": True} if header else {})),
                                  align="center")]}
        if header or shaded:
            cell["bgColor"] = "#F86560" if header else "rgb(242, 245, 250)"
        cells.append(cell)
    return {"type": "table-row", "height": 36, "children": cells}


def table(rows, widths, name):
    return {"type": "table", "id": name, "width": widths, "children": rows}


def template():
    """Offline synthetic Slate template; no captured page or external fixture."""
    metrics = ["P95%", "P95 K", "峰值%", "峰值K", "内存峰MB", "IO读MB/S", "IO写MB/S"]
    head = row(["模块", "作用", "进程", "前台\n需求", "后台\n需求"] +
               ["后台常驻服务"] + [""] * 6 + ["前台top-app"] + [""] * 6, header=True)
    subhead = row([""] * 5 + metrics * 2, header=True)
    for column in range(5):
        head["children"][column]["rowSpan"] = 2
        subhead["children"][column]["hidden"] = True
    for offset in (5, 12):
        head["children"][offset]["colSpan"] = 7
        for column in range(offset + 1, offset + 7):
            head["children"][column]["hidden"] = True
    process = [head, subhead]
    for i, item in enumerate(REQUIRED_PROCESSES):
        if i == 31:
            process.append(row([""] * 19, shaded=True))
        process.append(row(
            [item["module"], item["business"], item["name"], " 人工前台 ", "人工后台"] +
            ["旧值"] * 14, shaded=i % 2 == 0))
    return [
        paragraph("人工前言 不改", bold=True),
        heading("总资源及各个进程占用", 1),
        heading("总资源占用", 2),
        table([row(["指标", "P95", "P99"], header=True),
               row(["CPU(%)", "", ""]), row(["内存(GB)", "", ""])],
              [180, 120, 120], "overall"),
        paragraph("人工折线图占位说明需保留", fontColor="gray"),
        heading("各个进程占用", 2),
        table(process, [120, 180, 280, 80, 80] + [96] * 14, "process"),
        heading("准入结论", 1),
        table([row(["人工", "峰值原因"], header=True),
               row(["不改", "待填写"], shaded=True)], [180, 360], "conclusion"),
    ]


def table_named(blocks, name):
    return next(block for block in blocks if block.get("id") == name)


def put_text(cell, text):
    cell["children"][0]["children"] = [{"text": text}]


def process_rows(blocks):
    return [r for r in table_named(blocks, "process")["children"][2:]
            if any(perf_slate.text_of(c).strip() for c in r["children"])]


def p(ts, name=None, pid=1, cpu1c=10, rss_kb=1024, rd_kb=102400, wr_kb=204800):
    values = dict.fromkeys(SCHEMAS["P"].split(), 0)
    values.update(ts=ts, pid=pid, name=name or REQUIRED_PROCESSES[0]["name"],
                  cpu=99, cpu1c=cpu1c, rss_kb=rss_kb, state="S", nthr=1, pol="N",
                  rd_kb=rd_kb, wr_kb=wr_kb)
    return "P," + ",".join(str(values[k]) for k in SCHEMAS["P"].split()) + "\n"


def s(ts, cpu=10, avail=4096, total=8192):
    return f"S,{ts},7,{cpu},0,0,0,0,{avail},{total},0\n"


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "synthetic.sqlite3")
        self.output = self.root / "output space"

    def load(self, text):
        return self.store.import_files([("perf.log", io.BytesIO(text.encode()))])["id"]

    def build(self, text, scenes=None, factor=None, source=None):
        return build_admission(self.store, self.load(text), template() if source is None else source, self.output,
                               scenes if scenes is not None else {0: "background"}, factor)

    def rows(self, blocks):
        total = table_named(blocks, "overall")["children"][1:]
        texts = lambda r: [perf_slate.text_of(c).strip() for c in r["children"]]
        return [texts(r) for r in process_rows(blocks)], [texts(r) for r in total]

    def assert_preserved(self, before, blocks):
        # Compare every property except the measured text leaves we may replace.
        restored = deepcopy(blocks[:len(before)])
        for name in ("overall", "process"):
            old_table = table_named(before, name)
            new_table = table_named(restored, name)
            self.assertEqual(len(old_table["children"]), len(new_table["children"]))
            for index, (old, new) in enumerate(zip(old_table["children"], new_table["children"])):
                if index < (1 if name == "overall" else 2):
                    continue
                if not any(perf_slate.text_of(c).strip() for c in old["children"]):
                    continue
                for column in range(1 if name == "overall" else 5, len(old["children"])):
                    old_p = old["children"][column]["children"][0]
                    new_p = new["children"][column]["children"][0]
                    new_p["children"] = deepcopy(old_p["children"])
        self.assertEqual(restored, before)

    def test_template_preservation_and_scene_isolation(self):
        original = template()
        before = deepcopy(original)
        text = s(100) + p(100, cpu1c=0) + s(101) + p(101, cpu1c=50)
        text += s(1) + p(1, cpu1c=200) + s(0) + p(0, cpu1c=999)
        result = self.build(text, {0: "background", 1: "foreground", 2: "unknown"}, 20, original)
        blocks = result["blocks"]
        self.assert_preserved(before, blocks)
        rows, total = self.rows(blocks)
        self.assertEqual(rows[0][5:12], ["50.00", "10.00", "50.00", "10.00", "1.00", "100.00", "200.00"])
        self.assertEqual(rows[0][12:19], ["-"] * 7)
        self.assertTrue(all(row[5:] == [""] * 14 for row in rows[1:]))
        self.assertEqual(total, [["CPU(%)", "80.00", "80.00"], ["内存(GB)", "4.00", "4.00"]])
        self.assertEqual(json.loads((self.output / "report.json").read_text()), blocks)
        self.assertEqual([item.name for item in self.output.iterdir()], ["report.json"])
        self.assertEqual(len(rows), 49)
        self.assertTrue(all(len(r) == 19 for r in rows))
        tags = Tags(result["html"]).tags
        self.assertEqual(sum(attrs.get("colspan") == "7" for _, attrs in tags), 2)
        self.assertEqual(sum(attrs.get("rowspan") == "2" for _, attrs in tags), 5)
        self.assertTrue(any(tag == "th" and "head" in attrs.get("class", "") for tag, attrs in tags))
        self.assertTrue(any("alt" in attrs.get("class", "") for _, attrs in tags))
        expected_widths = [w for b in before if b.get("type") == "table" for w in b["width"]]
        self.assertEqual([attrs["style"] for tag, attrs in tags if tag == "col"],
                         [f"width:{w}px" for w in expected_widths])

    def test_live_template_headers_and_trailing_empty_rows(self):
        for count in (1, 2):
            with self.subTest(trailing_rows=count):
                original = template()
                table_named(original, "process")["children"].extend(
                    row([""] * 19, shaded=True) for _ in range(count))
                before = deepcopy(original)
                result = self.build(s(1) + p(1), source=original)
                self.assert_preserved(before, result["blocks"])
                self.assertEqual(len(self.rows(result["blocks"])[0]), 49)
                blank_rows = [r for r in table_named(result["blocks"], "process")["children"]
                              if not any(perf_slate.text_of(c).strip() for c in r["children"])]
                self.assertEqual(len(blank_rows), count + 1)
                self.assertIn("后台常驻服务", result["html"])
                self.assertIn("前台top-app", result["html"])

    def test_full_samples_nearest_rank_and_schema_not_positions(self):
        text = "".join(s(i, cpu=1) + p(i, cpu1c=i, rss_kb=i * 1024) for i in range(100, 1100))
        text += s(1, cpu=90) + p(1, cpu1c=99999, rss_kb=99999)
        sid = self.load(text)
        regular = build_report_data(self.store, sid)
        full = build_report_data(self.store, sid, include_full_system=True)
        self.assertNotIn("full_points", regular["systems"][0])
        self.assertEqual(len(full["systems"][0]["full_points"]), 1000)
        self.assertLess(len(full["systems"][0]["series"]["points"]), 1000)
        for system in full["systems"]:
            system.pop("full_points")
        self.assertEqual(regular, full)
        full = build_report_data(self.store, sid, include_full_system=True)
        full["full_cycle_schema"].reverse()
        for process in full["processes"]:
            for row in process["full_cycles"]:
                row.reverse()
            process["series"]["points"] = []
        with patch("perf_admission.build_report_data", return_value=full) as builder:
            result = build_admission(self.store, sid, template(), self.output,
                                     {0: "background", 1: "background"}, None)
        builder.assert_called_once_with(self.store, sid, include_full_system=True)
        rows, totals = self.rows(result["blocks"])
        self.assertEqual(rows[0][5:10], ["1050.00", "301.88", "99999.00", "28749.71", "1099.00"])
        self.assertEqual(totals[0][1:], ["8.00", "8.00"])

    def test_missing_ambiguous_truncated_and_pid_merge(self):
        webview = REQUIRED_PROCESSES[-1]["name"]
        text = p(1, pid=1, cpu1c=3) + p(1, pid=2, cpu1c=7)
        text += p(2, pid=1, cpu1c=999) * 2
        text += p(3, pid=1, cpu1c=999) + "DP,3,2,100,1," + REQUIRED_PROCESSES[0]["name"] + "\n"
        text += p(4, name="/one/audioserver") + p(4, name="/two/audioserver")
        text += p(5, name=webview + "e") + p(6, name="mediaserver", cpu1c=40)
        result = self.build(text)
        rows, totals = self.rows(result["blocks"])
        self.assertEqual(rows[0][5:12], ["10.00", "2.88", "10.00", "2.88", "2.00", "200.00", "400.00"])
        names = [r["name"] for r in REQUIRED_PROCESSES]
        self.assertEqual(rows[names.index("/system/bin/audioserver")][5:], [""] * 14)
        self.assertEqual(rows[-1][5:], [""] * 14)
        self.assertEqual(rows[names.index("/system/bin/mediaserver")][5], "40.00")
        self.assertEqual([row[1:] for row in totals], [["", ""], ["", ""]])
        self.assertEqual(len(list(self.output.glob("*.png"))), 0)
        self.assertTrue(any("IO" in warning for warning in result["warnings"]))

    def test_factor_and_scene_validation(self):
        sid = self.load(s(1) + p(1))
        for factor in [0, -1, float("nan"), float("inf"), True, "20"]:
            with self.subTest(factor=factor), self.assertRaises(ValueError):
                build_admission(self.store, sid, template(), self.output, {0: "unknown"}, factor)
        for scenes in [{0: "active"}, {0: "background", 1: "foreground"}, {False: "background"}, []]:
            with self.subTest(scenes=scenes), self.assertRaises(ValueError):
                build_admission(self.store, sid, template(), self.output, scenes, None)
        self.assertFalse(self.output.exists())
        result = self.build(s(1) + p(1), {0: "unknown"})
        self.assertEqual(self.rows(result["blocks"])[0][0][5:], [""] * 14)
        self.assertEqual(len(list(self.output.glob("*.png"))), 0)

    def test_kdmips_columns_use_offline_default_without_explicit_factor(self):
        text = s(1) + p(1, cpu1c=80)
        default = self.build(text)
        rows, _ = self.rows(default["blocks"])
        expected = format(80 / 100 * KDMIPS_PER_CORE, ".2f")
        self.assertEqual(rows[0][5:9], ["80.00", expected, "80.00", expected])
        self.assertTrue(any(f"CPU/100×{KDMIPS_PER_CORE}" in w and "沿用离线报告默认系数" in w
                            for w in default["warnings"]))
        self.assertFalse(any("K列留空" in w for w in default["warnings"]))
        explicit = self.build(text, factor=20)
        self.assertEqual(self.rows(explicit["blocks"])[0][0][5:9], ["80.00", "16.00", "80.00", "16.00"])
        self.assertTrue(any("CPU/100×20" in w and "来自本次请求" in w for w in explicit["warnings"]))

    def test_scenes_optional_and_derived_from_groups(self):
        name = REQUIRED_PROCESSES[2]["name"]
        sid = self.load(s(1) + p(1, name=name, cpu1c=50, rd_kb=1024, wr_kb=2048) +
                        s(2) + p(2, name=name, cpu1c=200, rss_kb=2048))
        members = [dict(pid=1, name=name)]
        group = dict(name="前台组", segment=0, members=members, scene="前台", start=1, end=1)
        self.store.save_group(sid, group)
        self.store.save_group(sid, dict(group, name="后台组", scene="后台", start=2, end=2))
        self.store.save_group(sid, dict(group, name="未标注组", scene="未标注", start=None, end=None))
        result = build_admission(self.store, sid, template(), self.output)
        rows, _ = self.rows(result["blocks"])
        self.assertEqual(rows[2][5:12], ["200.00", "57.50", "200.00", "57.50", "2.00", "100.00", "200.00"])
        self.assertEqual(rows[2][12:19], ["50.00", "14.38", "50.00", "14.38", "1.00", "1.00", "2.00"])
        self.assertTrue(any("关注进程" in w and "前台组" in w and "后台组" in w for w in result["warnings"]))
        self.assertFalse(any("未标注组" in w for w in result["warnings"]))
        # An explicit scene still overrides every group window of that segment.
        override = build_admission(self.store, sid, template(), self.output, {0: "unknown"}, None)
        self.assertEqual(self.rows(override["blocks"])[0][2][5:], [""] * 14)

    def test_without_groups_rows_follow_excel_requirement(self):
        result = build_admission(self.store, self.load(s(1) + p(1)), template(), self.output)
        rows, totals = self.rows(result["blocks"])
        # Excel行7仅适用后台；有实测时非适用前台侧填写短横线。
        self.assertEqual(rows[0][5:12], ["10.00", "2.88", "10.00", "2.88", "1.00", "100.00", "200.00"])
        self.assertEqual(rows[0][12:19], ["-"] * 7)
        self.assertEqual(rows[0][3:5], ["人工前台", "人工后台"])
        self.assertTrue(all(row[5:] == [""] * 14 for row in rows[1:]))
        self.assertEqual(totals[0][1:], ["80.00", "80.00"])
        self.assertTrue(any("未检测到前台/后台场景标注" in w for w in result["warnings"]))

    def test_applicable_sides_and_scene_specific_budget_colors(self):
        selected = [item for item in REQUIRED_PROCESSES if item["excel_row"] in (7, 9, 23)]
        text = s(1) + "".join(p(1, name=item["name"], pid=i + 1, cpu1c=10)
                             for i, item in enumerate(selected))
        result = self.build(text, scenes={})
        rows = {perf_slate.text_of(r["children"][2]): r for r in process_rows(result["blocks"])}
        for item in selected:
            with self.subTest(excel_row=item["excel_row"]):
                cells = rows[item["name"]]["children"]
                budget = EXCEL_BUDGETS[item["excel_row"]]
                for offset, requirement, columns in ((5, "E", "FGHIJKL"), (12, "D", "MNOPQRS")):
                    leaves = [c["children"][0]["children"][0] for c in cells[offset:offset + 7]]
                    if budget[requirement] != "Y":
                        self.assertEqual(leaves, [{"text": "-"}] * 7)
                        continue
                    self.assertEqual([v["text"] for v in leaves],
                                     ["10.00", "2.88", "10.00", "2.88", "1.00", "100.00", "200.00"])
                    # IO has measured text but never compares against MB/s budgets.
                    for value, key, leaf in zip((10, 2.875, 10, 2.875, 1, 100, 200), columns, leaves):
                        limit = budget.get(key)
                        expected = None
                        if key not in "KLRS" and isinstance(limit, (int, float)):
                            expected = perf_slate.HIGH_COLOR if value > limit else perf_slate.LOW_COLOR
                        self.assertEqual(leaf.get("fontColor"), expected)
        for name, r in rows.items():
            if name not in {item["name"] for item in selected}:
                self.assertEqual([c["children"][0]["children"][0] for c in r["children"][5:]],
                                 [{"text": ""}] * 14)
        preview = perf_slate.wrap_preview(result["html"])
        self.assertIn('<span class="note">10.00</span>', preview)
        self.assertIn('<span class="budget-low">10.00</span>', preview)
        self.assertIn('.budget-low{color:#389E0D}', preview)
        self.assertEqual(json.loads((self.output / "report.json").read_text()), result["blocks"])

    def test_equal_raw_threshold_and_missing_metrics_colors(self):
        for cpu, expected in ((2, None), (2.0000001, perf_slate.HIGH_COLOR),
                              (1.9999999, perf_slate.LOW_COLOR), (0, perf_slate.LOW_COLOR)):
            with self.subTest(cpu=cpu):
                result = self.build(s(1) + p(1, cpu1c=cpu))
                cell = process_rows(result["blocks"])[0]["children"][5]
                self.assertEqual(cell["children"][0]["children"][0]["text"],
                                 "0.00" if cpu == 0 else "2.00")
                self.assertEqual(cell["children"][0]["children"][0].get("fontColor"), expected)
        for cpu, rss in ((None, 1024), (10, None), (None, None),
                         (float("nan"), 1024), (10, float("nan"))):
            with self.subTest(cpu=cpu, rss=rss):
                sid = self.load(s(1) + p(1))
                data = build_report_data(self.store, sid, include_full_system=True)
                cycle = data["processes"][0]["full_cycles"][0]
                cycle[data["full_cycle_schema"].index("cpu1c")] = cpu
                cycle[data["full_cycle_schema"].index("rss_kb")] = rss
                for field in ("rd_kb", "wr_kb"):
                    cycle[data["full_cycle_schema"].index(field)] = None
                with patch("perf_admission.build_report_data", return_value=data):
                    result = build_admission(self.store, sid, template(), self.output,
                                             {0: "background"}, 28.75)
                leaves = [c["children"][0]["children"][0]
                          for c in process_rows(result["blocks"])[0]["children"][5:]]
                if cpu is None or cpu != cpu:
                    self.assertEqual(leaves[:4], [{"text": ""}] * 4)
                if rss is None or rss != rss:
                    self.assertEqual(leaves[4], {"text": ""})
                self.assertEqual(leaves[5:7], [{"text": ""}] * 2)
                expected = "" if cpu is None and rss is None else "-"
                self.assertEqual(leaves[7:], [{"text": expected}] * 7)

    def test_io_peaks_use_cycles_not_rate_average_or_total(self):
        text = (s(1) + p(1, rd_kb=1280, wr_kb=512) +
                s(5) + p(5, rd_kb=2560, wr_kb=4096) +
                s(30) + p(30, rd_kb=1024, wr_kb=2048))
        result = self.build(text, scenes={})
        cells = process_rows(result["blocks"])[0]["children"]
        self.assertEqual([c["children"][0]["children"][0] for c in cells[10:12]],
                         [{"text": "2.50"}, {"text": "4.00"}])
        self.assertTrue(any("MB/周期" in w and "不做红绿" in w for w in result["warnings"]))
        self.assertIn("2.50", result["html"])
        self.assertEqual(json.loads((self.output / "report.json").read_text()), result["blocks"])

    def test_io_windows_exclude_overlap_and_uncovered_cycles(self):
        name = REQUIRED_PROCESSES[2]["name"]
        text = "".join(s(ts) + p(ts, name=name, rd_kb=kb, wr_kb=kb * 2)
                       for ts, kb in ((1, 1024), (2, 999999), (3, 2048), (4, 9999999)))
        sid = self.load(text)
        for scene, start, end in (("后台", 1, 2), ("前台", 2, 3)):
            self.store.save_group(sid, dict(name=scene, scene=scene, segment=0,
                                           members=[dict(pid=1, name=name)], start=start, end=end))
        result = build_admission(self.store, sid, template(), self.output)
        rows, _ = self.rows(result["blocks"])
        self.assertEqual(rows[2][10:12], ["1.00", "2.00"])
        self.assertEqual(rows[2][17:19], ["2.00", "4.00"])

    def test_io_only_zero_invalid_and_absent_fields(self):
        sid = self.load(s(1) + p(1))
        original = build_report_data(self.store, sid, include_full_system=True)
        for read, write, expected in (
                (0, 0, ["0.00", "0.00"]),
                (None, 1536, ["", "1.50"]),
                (float("nan"), float("inf"), ["", ""]),
                (True, "2048", ["", ""]),
                ("absent", "absent", ["", ""])):
            with self.subTest(read=read, write=write):
                data = deepcopy(original)
                schema = data["full_cycle_schema"]
                cycles = data["processes"][0]["full_cycles"]
                for field, value in (("cpu1c", None), ("rss_kb", None),
                                     ("rd_kb", read), ("wr_kb", write)):
                    index = schema.index(field)
                    for cycle in cycles:
                        if value == "absent":
                            cycle.pop(index)
                        else:
                            cycle[index] = value
                    if value == "absent":
                        schema.pop(index)
                with patch("perf_admission.build_report_data", return_value=data):
                    result = build_admission(self.store, sid, template(), self.output)
                leaves = [c["children"][0]["children"][0]
                          for c in process_rows(result["blocks"])[0]["children"][5:]]
                self.assertEqual(leaves[:5], [{"text": ""}] * 5)
                self.assertEqual(leaves[5:7], [{"text": v} for v in expected])
                self.assertEqual(leaves[7:], [{"text": "-" if any(expected) else ""}] * 7)

    def test_requirement_columns_filled_when_template_leaves_them_empty(self):
        source = template()
        for r in process_rows(source):
            for cell in r["children"][3:5]:
                put_text(cell, "")
        result = build_admission(self.store, self.load(s(1) + p(1)), source, self.output)
        rows, _ = self.rows(result["blocks"])
        budgets = [EXCEL_BUDGETS[item["excel_row"]] for item in REQUIRED_PROCESSES]
        self.assertEqual([row[3:5] for row in rows],
                         [[budget["D"], budget["E"]] for budget in budgets])

    def assert_rejected_before_read(self, source):
        with patch("perf_admission.build_report_data") as builder:
            with self.assertRaises(ValueError):
                build_admission(None, "none", source, self.output, {}, None)
            builder.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_structure_changes_rejected_before_read(self):
        variants = []
        for index, text in ((2, "总资源汇总"), (5, "总资源占用"), (7, "结论")):
            source = template()
            source[index]["children"] = [{"text": text}]
            variants.append(source)
        source = template()
        source.append(deepcopy(source[7]))
        variants.append(source)
        for name, r, c, text in (("overall", 1, 0, "CPU参考"),
                                  ("process", 1, 10, "IO写MB/S"),
                                  ("process", 0, 5, "前台top-app"),
                                  ("process", 2, 2, "wrong")):
            source = template()
            put_text(table_named(source, name)["children"][r]["children"][c], text)
            variants.append(source)
        for change in ("extra_column", "merge", "missing_process", "duplicate_process", "width"):
            source = template()
            process = table_named(source, "process")
            if change == "extra_column":
                process["children"][2]["children"].append(row(["extra"])["children"][0])
            elif change == "merge":
                process["children"][0]["children"][5]["colSpan"] = 6
            elif change == "missing_process":
                process["children"].pop(2)
            elif change == "duplicate_process":
                process["children"].append(deepcopy(process["children"][2]))
            else:
                process["width"][0] = '1px; background:url(https://bad/x)'
            variants.append(source)
        for index, source in enumerate(variants):
            with self.subTest(variant=index):
                self.assert_rejected_before_read(source)

    def test_safe_image_free_preview(self):
        attack = '<script>alert(1)</script><img src="https://bad/x" onerror="bad()">\n'
        attack += '![remote](https://bad/image.png)\n![local](/etc/passwd)\n[x](javascript:bad)\n'
        source = [paragraph(attack)] + template()
        result = self.build(s(1) + p(1) + s(2) + p(2), source=source)
        preview = perf_slate.wrap_preview(result["html"])
        tags = Tags(preview).tags
        self.assertNotIn("script", [tag for tag, _ in tags])
        self.assertNotIn("a", [tag for tag, _ in tags])
        self.assertIn(html.escape(attack, quote=True).replace("\n", "<br>"), preview)
        self.assertNotIn(str(self.output), preview)
        self.assertFalse(any(tag == "img" for tag, _ in tags))
        self.assertFalse(any(key.lower().startswith("on") for _, attrs in tags for key in attrs))
        self.assertEqual([p.name for p in self.output.iterdir()], ["report.json"])
        self.assertEqual(perf_slate.text_of(result["blocks"][0]), attack)
        self.assertIn("人工折线图占位说明需保留", preview)
        self.assertTrue(any("default-src 'none'" in attrs.get("content", "") and
                            "img-src 'none'" in attrs.get("content", "") for _, attrs in tags))

    def test_image_and_embed_blocks_rejected_everywhere(self):
        for kind in ("image", "img", "picture", "svg", "file", "embed", "iframe", "unknown"):
            for location in ("prefix", "paragraph", "cell", "conclusion", "suffix"):
                with self.subTest(kind=kind, location=location):
                    source = template()
                    unsafe = {"type": kind, "children": [{"text": ""}]}
                    if location == "prefix":
                        source.insert(0, unsafe)
                    elif location == "paragraph":
                        source[0]["children"].append(unsafe)
                    elif location == "cell":
                        process_rows(source)[0]["children"][4]["children"][0]["children"].append(unsafe)
                    elif location == "conclusion":
                        table_named(source, "conclusion")["children"][1]["children"][1]["children"].append(unsafe)
                    else:
                        source.append(unsafe)
                    self.assert_rejected_before_read(source)

    def test_reference_and_image_syntax_preserved_as_safe_text(self):
        text = ('前![a][shared]后 [文档][shared]\n![only][]\n'
                '[shared]: https://example/shared\n[only]: https://example/only\n'
                '<image href="x" title="a>b"/>尾')
        source = template()
        source.insert(0, paragraph(text))
        put_text(process_rows(source)[0]["children"][4], text)
        put_text(table_named(source, "conclusion")["children"][1]["children"][1], text)
        source.append(paragraph(text))
        before = deepcopy(source)
        result = self.build(s(1) + p(1), source=source)
        self.assert_preserved(before, result["blocks"])
        self.assertEqual(perf_slate.text_of(result["blocks"][0]), text)
        self.assertEqual(self.rows(result["blocks"])[0][0][4], text)
        self.assertEqual(result["html"].count(html.escape(text, quote=True).replace("\n", "<br>")), 4)
        self.assertFalse(any(tag in ("img", "image", "a") for tag, _ in Tags(result["html"]).tags))

    def test_literal_image_syntax_is_not_deleted(self):
        literals = ('注意 ![未通过] 请复查', '注意 ![a][missing] 后',
                    '注意 ![missing][] 后', r'转义 \![a](x.png)',
                    '`![a](x.png)`', '``literal ` ![a](x.png)``',
                    '```markdown\n![a](x.png)\n```',
                    '~~~markdown\n<img src="x">\n~~~',
                    '    ![a](x.png)\n', '`![x][a]`\n[a]: image.png\n',
                    '```\n![literal](x)\n```\n![real][a]\n[a]: image.png\n',
                    '![broken', '![image](broken', '![image][broken',
                    '<img src="broken>', '<picture>unfinished', '<svg/>',
                    '<svg><svg></svg></svg>', '</image>')
        source = [paragraph(text) for text in literals] + template()
        before = deepcopy(source)
        result = self.build(s(1) + p(1), source=source)
        self.assert_preserved(before, result["blocks"])
        for index, text in enumerate(literals):
            with self.subTest(text=text):
                self.assertEqual(perf_slate.text_of(result["blocks"][index]), text)
                self.assertIn(html.escape(text, quote=True).replace("\n", "<br>"), result["html"])
        self.assertFalse(any(tag in ("img", "image", "picture", "svg", "a")
                             for tag, _ in Tags(result["html"]).tags))

    def test_malformed_slate_rejected_before_store_read(self):
        invalid_nodes = [
            None, "not a node", {}, {"text": 1},
            {"text": "x", "src": "https://bad/image"},
            {"text": "x", "onerror": "bad()"},
            {"type": "p", "children": []},
            {"type": "p", "children": "not a list"},
            dict(paragraph("x"), src="https://bad/image"),
            dict(paragraph("x"), style="background:url(https://bad/image)"),
            dict(paragraph("x"), id=123),
            heading("x", 3),
            dict(heading("x", 1), value="unsafe"),
            table([paragraph("wrong child")], [120], "invalid"),
            {"type": "table-row", "children": [paragraph("wrong child")]},
        ]
        for index, node in enumerate(invalid_nodes):
            with self.subTest(node=index):
                self.assert_rejected_before_read([node] + template())
        for source in (None, [], {}, "# Markdown is not Slate"):
            with self.subTest(root=source):
                self.assert_rejected_before_read(source)
        for field, values in (("width", [[], [0], [-1], [4001], [True], [1.5]]),
                              ("rowSpan", [0, -1, 1001, True, "2"]),
                              ("colSpan", [0, -1, 1001, True, "7"]),
                              ("hidden", [0, "true"])):
            for value in values:
                with self.subTest(field=field, value=value):
                    source = template()
                    process = table_named(source, "process")
                    target = process if field == "width" else process["children"][2]["children"][5]
                    target[field] = value
                    self.assert_rejected_before_read(source)

    def test_input_template_unchanged(self):
        source = template()
        before = deepcopy(source)
        self.build(s(1) + p(1), source=source)
        self.assertEqual(source, before, "build_admission must not mutate its input template")

    def test_missing_field_never_falls_back(self):
        sid = self.load(s(1) + p(1))
        data = build_report_data(self.store, sid, include_full_system=True)
        system = data["systems"][0]
        system["full_points"][0]["cpu_single_core"] = None
        system["full_points"][0]["mem_used_mb"] = None
        system["series"]["points"] = []
        process = data["processes"][0]
        process["full_cycles"][0][data["full_cycle_schema"].index("cpu1c")] = None
        process["series"]["points"] = []
        with patch("perf_admission.build_report_data", return_value=data):
            result = build_admission(self.store, sid, template(), self.output, {0: "background"}, 28.75)
        rows, totals = self.rows(result["blocks"])
        self.assertEqual(rows[0][5:12], ["", "", "", "", "1.00", "100.00", "200.00"])
        self.assertEqual([r[1:] for r in totals], [["", ""], ["", ""]])
        self.assertFalse(list(self.output.glob("*.png")))


if __name__ == "__main__":
    unittest.main()
