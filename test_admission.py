import html
import io
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from perf_admission import build_admission, _cells, _template
from perf_parser import SCHEMAS
from perf_report_data import REQUIRED_PROCESSES, build_report_data
from perf_store import Store


def table(cells):
    return "| " + " | ".join(cells) + " |\n"


def template():
    text = "人工前言 **不改**\n- 总资源及各个进程占用\n\n- 总资源占用\n\n"
    text += table(["**指标**", "**P95**", "**P99**"]) + table(["---"] * 3)
    text += table(["CPU(%)", "", ""]) + table(["内存(GB)", "", ""])
    text += "\n【**折线图占位】各进程 CPU 占用 + 总 CPU 折线图**\n\n"
    text += "  ** 【折线图占位】内存占用折线图**\n\n- 各个进程占用\n\n"
    text += table(["**模块**", "**作用**", "**进程 **", "**前台 <br> 需求**", "**后台 <br> 需求**"] +
                  ["**后台**"] + [""] * 6 + ["**前台**"] + [""] * 6)
    text += table(["---"] * 19)
    metrics = ["P95%", "P95 K", "峰值%", "峰值K", "内存峰MB", "IO读MB/S", "IO写MB/S"]
    text += table([""] * 5 + ["**" + m + "**" for m in metrics] * 2)
    for i, item in enumerate(REQUIRED_PROCESSES):
        if i == 31:
            text += table([""] * 19)
        text += table([item["module"], item["business"], item["name"], " 人工前台 ", "人工后台"] + ["旧值"] * 14)
    return text + "\n- **准入结论**\n\n| 人工 | 峰值原因 |\n| --- | --- |\n| 不改 | 待填写 |\n"


def p(ts, name=None, pid=1, cpu1c=10, rss_kb=1024):
    values = dict.fromkeys(SCHEMAS["P"].split(), 0)
    values.update(ts=ts, pid=pid, name=name or REQUIRED_PROCESSES[0]["name"],
                  cpu=99, cpu1c=cpu1c, rss_kb=rss_kb, state="S", nthr=1, pol="N",
                  rd_kb=102400, wr_kb=204800)
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
        return build_admission(self.store, self.load(text), source or template(), self.output,
                               scenes if scenes is not None else {0: "background"}, factor)

    def rows(self, markdown):
        _, body, _, total, indices = _template(markdown)
        return [[c.strip() for c in _cells(body[i])] for i in indices], [
            [c.strip() for c in _cells(body[i])] for i in total[2:]]

    def test_template_preservation_and_scene_isolation(self):
        original = template().replace("人工后台", "人工折线图占位说明").replace("\n", "\r\n")
        text = s(100) + p(100, cpu1c=0) + s(101) + p(101, cpu1c=50)
        text += s(1) + p(1, cpu1c=200) + s(0) + p(0, cpu1c=999)
        result = self.build(text, {0: "background", 1: "foreground", 2: "unknown"}, 20, original)
        markdown = result["markdown"]
        before, body, after, _, indices = _template(original)
        out_before, out_body, out_after, _, out_indices = _template(markdown)
        self.assertEqual(before, out_before)
        self.assertEqual(after, out_after)
        for i, j in zip(indices, out_indices):
            self.assertEqual(_cells(body[i])[:5], _cells(out_body[j])[:5])
        self.assertIn(table([""] * 19).replace("\n", "\r\n"), markdown)
        rows, total = self.rows(markdown)
        self.assertEqual(rows[0][5:12], ["50", "10", "50", "10", "1", "", ""])
        self.assertEqual(rows[0][12:19], ["200", "40", "200", "40", "1", "", ""])
        self.assertTrue(all(row[5:] == [""] * 14 for row in rows[1:]))
        self.assertEqual(total, [["CPU(%)", "80", "80"], ["内存(GB)", "4", "4"]])
        self.assertEqual((self.output / "report.md").read_bytes(), markdown.encode())

    def test_live_template_headers_and_trailing_empty_rows(self):
        for count in (1, 2):
            with self.subTest(trailing_rows=count):
                original = template().replace("**后台**", "**后台常驻服务**").replace(
                    "**前台**", "**前台top-app**")
                original = original.replace("\n- **准入结论**", table([""] * 19) * count + "\n- **准入结论**")
                result = self.build(s(1) + p(1), source=original)
                before, body, after, _, indices = _template(original)
                out_before, out_body, out_after, _, out_indices = _template(result["markdown"])
                self.assertEqual(len(out_indices), 49)
                self.assertEqual(before, out_before)
                self.assertEqual(after, out_after)
                empty_rows = lambda lines: [line for line in lines if line.startswith("|") and not any(c.strip() for c in _cells(line))]
                self.assertEqual(len(empty_rows(body)), count + 1)
                self.assertEqual(empty_rows(body), empty_rows(out_body))
                for i, j in zip(indices, out_indices):
                    self.assertEqual(_cells(body[i])[:5], _cells(out_body[j])[:5])
                self.assertIn("**后台常驻服务**", result["markdown"])
                self.assertIn("**前台top-app**", result["markdown"])
                malformed = original.replace("\n- **准入结论**", table([""] * 18 + ["unexpected"]) + "\n- **准入结论**")
                with self.assertRaises(ValueError):
                    _template(malformed)

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
        rows, totals = self.rows(result["markdown"])
        self.assertEqual(rows[0][5:10], ["1050", "", "99999", "", "1099"])
        self.assertEqual(totals[0][1:], ["8", "8"])

    def test_missing_ambiguous_truncated_and_pid_merge(self):
        webview = REQUIRED_PROCESSES[-1]["name"]
        text = p(1, pid=1, cpu1c=3) + p(1, pid=2, cpu1c=7)
        text += p(2, pid=1, cpu1c=999) * 2
        text += p(3, pid=1, cpu1c=999) + "DP,3,2,100,1," + REQUIRED_PROCESSES[0]["name"] + "\n"
        text += p(4, name="/one/audioserver") + p(4, name="/two/audioserver")
        text += p(5, name=webview + "e") + p(6, name="mediaserver", cpu1c=40)
        result = self.build(text)
        rows, totals = self.rows(result["markdown"])
        self.assertEqual(rows[0][5:12], ["10", "", "10", "", "2", "", ""])
        names = [r["name"] for r in REQUIRED_PROCESSES]
        self.assertEqual(rows[names.index("/system/bin/audioserver")][5:], [""] * 14)
        self.assertEqual(rows[-1][5:], [""] * 14)
        self.assertEqual(rows[names.index("/system/bin/mediaserver")][5], "40")
        self.assertEqual([row[1:] for row in totals], [["", ""], ["", ""]])
        self.assertEqual(len(list(self.output.glob("*.png"))), 0)
        self.assertTrue(any("IO" in warning for warning in result["warnings"]))

    def test_factor_and_scene_validation(self):
        sid = self.load(s(1) + p(1))
        for factor in [0, -1, float("nan"), float("inf"), True, "20"]:
            with self.subTest(factor=factor), self.assertRaises(ValueError):
                build_admission(self.store, sid, template(), self.output, {0: "unknown"}, factor)
        for scenes in [{}, {0: "active"}, {0: "background", 1: "foreground"}, {False: "background"}]:
            with self.subTest(scenes=scenes), self.assertRaises(ValueError):
                build_admission(self.store, sid, template(), self.output, scenes, None)
        self.assertFalse(self.output.exists())
        result = self.build(s(1) + p(1), {0: "unknown"})
        self.assertEqual(self.rows(result["markdown"])[0][0][5:], [""] * 14)
        self.assertEqual(len(list(self.output.glob("*.png"))), 0)

    def test_structure_changes_rejected_before_read(self):
        original = template()
        variants = [original.replace("- 总资源占用", "- 总资源汇总"),
                    original.replace("- 各个进程占用", "- 总资源占用"),
                    original.replace("- **准入结论**", "结论"), original + "- **准入结论**\n",
                    original.replace("CPU(%)", "CPU参考"), original.replace("**IO读MB/S**", "**IO写MB/S**", 1),
                    original.replace("**后台**", "**前台**", 1),
                    original.replace(table([""] * 19), ""), original.replace("| 旧值 |", "| 旧值 | extra |", 1),
                    original.replace(REQUIRED_PROCESSES[0]["name"] + " |", "wrong |", 1)]
        for source in variants:
            with self.subTest(source=source[:30]), patch("perf_admission.build_report_data") as builder:
                with self.assertRaises(ValueError):
                    build_admission(None, "none", source, self.output, {}, None)
                builder.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_safe_image_free_preview(self):
        attack = '<script>alert(1)</script><img src="https://bad/x" onerror="bad()">\n'
        attack += '![remote](https://bad/image.png)\n![local](/etc/passwd)\n[x](javascript:bad)\n'
        result = self.build(s(1) + p(1) + s(2) + p(2), source=attack + template())
        preview = result["html"]
        tags = Tags(preview).tags
        self.assertNotIn("script", [tag for tag, _ in tags])
        self.assertNotIn("a", [tag for tag, _ in tags])
        self.assertIn(html.escape("<script>"), preview)
        self.assertNotIn(str(self.output), preview)
        images = [attrs["src"] for tag, attrs in tags if tag == "img"]
        self.assertEqual(images, [])
        self.assertEqual([p.name for p in self.output.iterdir()], ["report.md"])
        self.assertNotIn("![", result["markdown"])
        self.assertNotIn("<img", result["markdown"])
        self.assertNotIn("折线图占位", result["markdown"])
        self.assertTrue(any("default-src 'none'" in attrs.get("content", "") for _, attrs in tags))

    def test_template_images_removed_everywhere_and_text_preserved(self):
        images = ('![nested](a(b).png)![ref][figure]![short]'
                  '<IMG src="remote.png" title="a>b">'
                  '<picture><source srcset="x"><img src="x"></picture>'
                  '<svg><path d="x"/></svg>')
        source = template().replace("人工前言", "前" + images + "后人工前言")
        source = source.replace("人工后台", "左" + images + "右人工后台")
        source += "尾前" + images + "尾后\n[figure]: https://example/image.png\n[short]: short.png\n"
        source = source.replace("- 各个进程占用", "人工折线图占位说明需保留\n- 各个进程占用")
        result = self.build(s(1) + p(1), source=source)
        markdown = result["markdown"]
        self.assertIn("前后人工前言", markdown)
        self.assertIn("尾前尾后", markdown)
        self.assertIn("人工折线图占位说明需保留", markdown)
        self.assertEqual(self.rows(markdown)[0][0][4], "左右人工后台")
        for marker in ("![", "<IMG", "<picture", "<svg", "[figure]:"):
            self.assertNotIn(marker, markdown)
        self.assertEqual([item.name for item in self.output.iterdir()], ["report.md"])

    def test_image_only_references_removed_shared_links_preserved(self):
        from perf_admission import _without_images
        source = ('前![a][shared]后 [文档][shared]\n![only][]\n'
                  '[shared]: https://example/shared\n[only]: https://example/only\n'
                  '<image href="x" title="a>b"/>尾')
        cleaned = _without_images(source)
        self.assertIn('前后 [文档][shared]', cleaned)
        self.assertIn('[shared]: https://example/shared', cleaned)
        self.assertNotIn('https://example/only', cleaned)
        self.assertNotIn('<image', cleaned)
        self.assertTrue(cleaned.endswith('尾'))

    def test_literal_image_syntax_is_not_deleted(self):
        from perf_admission import _without_images
        for source in ('注意 ![未通过] 请复查', '注意 ![a][missing] 后',
                       '注意 ![missing][] 后', r'转义 \![a](x.png)',
                       '`![a](x.png)`', '``literal ` ![a](x.png)``',
                       '```markdown\n![a](x.png)\n```',
                       '~~~markdown\n<img src="x">\n~~~',
                       '    ![a](x.png)\n', '`![x][a]`\n[a]: image.png\n'):
            with self.subTest(source=source):
                self.assertEqual(_without_images(source), source)
        source = '```\n![literal](x)\n```\n![real][a]\n[a]: image.png\n'
        self.assertEqual(_without_images(source), '```\n![literal](x)\n```\n\n')

    def test_malformed_images_fail_before_store_read(self):
        for image in ("![broken", "![image](broken", "![image][broken",
                      '<img src="broken>', '<picture>unfinished', '<svg/>',
                      '<svg><svg></svg></svg>', '</image>'):
            with patch("perf_admission.build_report_data") as builder:
                with self.assertRaises(ValueError):
                    build_admission(None, "none", image + template(),self.output, {}, None)
                builder.assert_not_called()
        self.assertFalse(self.output.exists())

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
        rows, totals = self.rows(result["markdown"])
        self.assertEqual(rows[0][5:12], ["", "", "", "", "1", "", ""])
        self.assertEqual([r[1:] for r in totals], [["", ""], ["", ""]])
        self.assertFalse(list(self.output.glob("*.png")))


if __name__ == "__main__":
    unittest.main()
