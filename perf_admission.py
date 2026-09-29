"""Isolated offline admission reports. No authentication, publishing or store writes."""
from __future__ import annotations

import base64
import html
import io
import math
import re
from pathlib import Path
from urllib.parse import quote

from perf_report_data import REQUIRED_PROCESSES, build_report_data


_START = "- 总资源及各个进程占用"
_END = "- **准入结论**"
_SCENES = {"background", "foreground", "unknown"}


def _cells(line):
    text = line.rstrip("\r\n").strip()
    if not text.startswith("|") or not text.endswith("|"):
        raise ValueError("模板表格必须使用完整管道分隔行")
    return re.split(r"(?<!\\)\|", text)[1:-1]


def _label(text):
    text = re.sub(r"<br\s*/?>", "", text, flags=re.I)
    return re.sub(r"[\s*`（）()]+", "", text).upper()


def _template(template):
    if not isinstance(template, str):
        raise ValueError("模板必须是字符串")
    lines = template.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == _START]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == _END]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError("模板准入章节边界不唯一或顺序不符")
    start, end = starts[0], ends[0]
    body = lines[start:end]
    tables, block = [], []
    for i, line in enumerate(body + [""]):
        if line.lstrip().startswith("|"):
            block.append(i)
        elif block:
            tables.append(block)
            block = []
    if len(tables) != 2:
        raise ValueError("目标章节必须恰有总资源表和进程表")
    total, processes = tables
    headings = [line.strip() for line in body if line.strip() in {"- 总资源占用", "- 各个进程占用"}]
    if headings != ["- 总资源占用", "- 各个进程占用"]:
        raise ValueError("资源章节小标题缺失、重复或顺序不符")
    total_heading = next(i for i, line in enumerate(body) if line.strip() == "- 总资源占用")
    process_heading = next(i for i, line in enumerate(body) if line.strip() == "- 各个进程占用")
    if not total_heading < total[0] <= total[-1] < process_heading < processes[0]:
        raise ValueError("资源章节小标题与表格位置不符")
    if len(total) != 4 or any(len(_cells(body[i])) != 3 for i in total):
        raise ValueError("总资源表结构不符")
    if [_label(c) for c in _cells(body[total[0]])] != ["指标", "P95", "P99"]:
        raise ValueError("总资源表表头不符")
    if [_label(_cells(body[i])[0]) for i in total[2:]] != ["CPU%", "内存GB"]:
        raise ValueError("总资源表指标不符")
    rows = [_cells(body[i]) for i in processes]
    if len(rows) < 53 or any(len(row) != 19 for row in rows):
        raise ValueError("进程表必须为19列、49进程并保留空分隔行")
    if any(c.strip() for row in rows[53:] for c in row):
        raise ValueError("49进程后只能保留全空表格行")
    labels = [_label(c) for c in rows[0]]
    if labels[:5] != ["模块", "作用", "进程", "前台需求", "后台需求"]:
        raise ValueError("进程表前五列表头不符")
    if (labels[5] not in {"后台", "后台占用", "后台常驻服务"} or
            labels[12] not in {"前台", "前台占用", "前台TOP-APP"} or
            any(labels[6:12] + labels[13:19])):
        raise ValueError("进程表前后台列组结构不符")
    for row in (_cells(body[total[1]]), rows[1]):
        if not all(re.fullmatch(r":?-{3,}:?", c.strip()) for c in row):
            raise ValueError("Markdown表格分隔行不符")
    metrics = ["P95%", "P95K", "峰值%", "峰值K", "内存峰MB", "IO读MB/S", "IO写MB/S"]
    if [_label(c) for c in rows[2]] != [""] * 5 + metrics * 2:
        raise ValueError("进程表七项指标或顺序不符")
    if any(c.strip() for c in rows[34]):
        raise ValueError("应用31行后的空分隔行不符")
    indices = processes[3:34] + processes[35:53]
    if len(REQUIRED_PROCESSES) != 49:
        raise ValueError("准入清单版本不符")
    for i, entry in zip(indices, REQUIRED_PROCESSES):
        if _cells(body[i])[2].strip().strip("*`") != entry["name"]:
            raise ValueError("49进程名称或顺序不符：" + entry["name"])
    return lines[:start], body, lines[end:], total, indices


def _valid(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _values(points, field, divisor=1):
    return sorted(p[field] / divisor for p in points if _valid(p.get(field)))


def _rank(values, percent):
    return values[(len(values) * percent + 99) // 100 - 1] if values else None


def _fmt(value):
    return format(value, ".6f").rstrip("0").rstrip(".") if _valid(value) else ""


def _row(line, values, offset):
    # Keep every byte of the first columns, including original whitespace.
    pipes = list(re.finditer(r"(?<!\\)\|", line))
    left, right = pipes[offset].end(), pipes[offset + len(values)].start()
    return line[:left] + " " + " | ".join(str(v) if v is not None else "" for v in values) + " " + line[right:]


def _statistics(processes, schema, scene, scenes, factor):
    points = [dict(zip(schema, row)) for p in processes
              if scenes[p["segment"]] == scene for row in p["full_cycles"]]
    cpu, memory = _values(points, "cpu1c"), _values(points, "rss_kb", 1024)
    p95, peak = _rank(cpu, 95), max(cpu) if cpu else None
    k = lambda value: value / 100 * factor if value is not None and factor is not None else None
    return [_fmt(v) for v in (p95, k(p95), peak, k(peak), max(memory) if memory else None, None, None)]


def _inline(text):
    # Do not interpret user HTML, links or images; only a tiny emphasis subset.
    text = re.sub(r"!\[[^\]]*\]\([^\n]*?\)", "[图片已阻止]", text)
    text = re.sub(r"\[([^\]]*)\]\([^\n]*?\)", r"\1 [链接已禁用]", text)
    escaped = html.escape(text, quote=True)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)


def _preview(markdown, images):
    result, table = [], False
    for line in markdown.splitlines():
        if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
            if not table:
                result.append("<table>")
                table = True
            cells = _cells(line)
            if all(re.fullmatch(r":?-{3,}:?", c.strip()) for c in cells):
                continue
            result.append("<tr>" + "".join("<td>" + _inline(c) + "</td>" for c in cells) + "</tr>")
            continue
        if table:
            result.append("</table>")
            table = False
        if line in images:
            encoded = base64.b64encode(images[line]).decode("ascii")
            result.append('<img alt="Performance trend" src="data:image/png;base64,' + encoded + '">')
        elif line.strip():
            result.append("<p>" + _inline(line) + "</p>")
    if table:
        result.append("</table>")
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'img-src data:; style-src \'unsafe-inline\';">'
            '<style>body{font:14px sans-serif;margin:20px;overflow-wrap:anywhere}'
            'table{border-collapse:collapse;width:100%;margin:16px 0}'
            'td{border:1px solid #ccc;padding:6px}img{max-width:100%;height:auto}</style>'
            '</head><body>' + "\n".join(result) + '</body></html>')


def _write(path, content):
    if path.is_symlink():
        raise ValueError("输出文件不能是符号链接")
    path.write_bytes(content)


def _trend_runs(points, field, divisor=1):
    """Sampled cycle gaps may connect only when upstream run IDs prove continuity."""
    runs, previous = [], None
    for point in points:
        if not _valid(point.get("ts")) or not _valid(point.get(field)):
            previous = None
            continue
        run = point.get("_run_" + field)
        connected = (previous is not None and run is not None and
                     run == previous.get("_run_" + field) and
                     point.get("segment") == previous.get("segment") and
                     point["ts"] > previous["ts"] and
                     point["cycle"] > previous["cycle"])
        if not connected:
            runs.append([])
        runs[-1].append((point["ts"], point[field] / divisor))
        previous = point
    return runs


def _png(points, field, title, divisor=1):
    from PIL import Image, ImageDraw

    runs = _trend_runs(points, field, divisor)
    if not runs:
        return None
    all_points = [point for run in runs for point in run]
    low_x, high_x = min(p[0] for p in all_points), max(p[0] for p in all_points)
    low_y, high_y = min(0, min(p[1] for p in all_points)), max(p[1] for p in all_points)
    width, height = 960, 320
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.text((64, 14), title, fill="#222222")
    draw.line([(64, 48), (64, 270), (928, 270)], fill="#666666")
    draw.text((6, 48), format(high_y, ".4g"), fill="#444444")
    draw.text((6, 258), format(low_y, ".4g"), fill="#444444")
    draw.text((64, 290), "Timestamp (ms): " + str(low_x) + " to " + str(high_x), fill="#444444")
    for run in runs:
        pixels = [(64 + (x - low_x) / (high_x - low_x or 1) * 864,
                   270 - (y - low_y) / (high_y - low_y or 1) * 222) for x, y in run]
        if len(pixels) > 1:
            draw.line(pixels, fill="#2463a6", width=2)
        for x, y in pixels:
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill="#2463a6")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def build_admission(store, session_id, template: str, output_dir: Path,
                    scenes: dict[int, str], factor: float | None) -> dict:
    """Write report.md and PNGs; return markdown, sandbox-safe html and warnings.

    Every observed segment needs an explicit scene. Invalid templates, scenes or
    factors raise ValueError before output. Only one read-consistent payload is used.
    """
    prefix, body, suffix, total, indices = _template(template)
    if factor is not None and (not _valid(factor) or factor <= 0):
        raise ValueError("factor必须是显式正有限数值或None")
    if (not isinstance(scenes, dict) or any(type(k) is not int for k in scenes) or
            any(not isinstance(v, str) or v not in _SCENES for v in scenes.values())):
        raise ValueError("每段场景必须显式指定background/foreground/unknown")
    data = build_report_data(store, session_id, include_full_system=True)
    segments = {s["segment"] for s in data["systems"]}
    if set(scenes) != segments:
        raise ValueError("scenes必须恰好覆盖所有segment，不允许遗漏或额外段")
    if [r["name"] for r in data["required"]] != [r["name"] for r in REQUIRED_PROCESSES]:
        raise ValueError("报告可靠匹配清单版本不符")
    schema = data["full_cycle_schema"]
    if not {"cpu1c", "rss_kb", "cycle", "ts"}.issubset(schema):
        raise ValueError("全量进程周期schema缺少必要字段")
    warnings = [
        "CPU采用单核口径，可超过100%；P95/P99按全量有效样本最近秩重算，不平均各段分位数。",
        "缺失指标留空，不以零、整机CPU或其他字段代填；内存按1024换算，进程使用RSS。",
        "IO读/写MB/S留空：rd_kb/wr_kb不是可靠速率，不能直接换算。",
        "场景仅使用用户显式segment标注，CPU活跃不代表前后台；unknown不进入前后台统计。",
        "峰值原因与准入结论保留人工判断，不自动推断。",
    ]
    warnings.append("场景标注：" + "，".join(f"segment {s}={scenes[s]}" for s in sorted(segments)))
    warnings.append("未提供factor，K列留空。" if factor is None else
                    f"K为派生估算：CPU/100×{factor}，不是独立实测。")
    full = [p for system in data["systems"] for p in system["full_points"]]
    for index, field, divisor in ((total[2], "cpu_single_core", 1), (total[3], "mem_used_mb", 1024)):
        values = _values(full, field, divisor)
        body[index] = _row(body[index], [_fmt(_rank(values, q)) for q in (95, 99)], 1)
        warnings.append(f"系统{field}有效样本 {len(values)}/{len(full)}。")
    by_id = {p["id"]: p for p in data["processes"]}
    matched = []
    for index, required in zip(indices, data["required"]):
        processes = [] if required["truncated"] else [by_id[pid] for pid in required["matches"]]
        matched.append(processes)
        for scene, offset in (("background", 5), ("foreground", 12)):
            body[index] = _row(body[index], _statistics(processes, schema, scene, scenes, factor), offset)
    covered = sum(any(p["p_records"] for p in group) for group in matched)
    warnings.append(f"可靠匹配采集覆盖：{covered}/49；歧义候选不参与，截断WebView始终留空。")
    missing = [r["name"] for r, group in zip(data["required"], matched) if not any(p["p_records"] for p in group)]
    if missing:
        warnings.append("未可靠采集：" + "、".join(missing))
    return _finish(output_dir, prefix, body, suffix, data, matched, warnings)


def _finish(output_dir, prefix, body, suffix, data, matched, warnings):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    images, charts = {}, []
    newline = "\r\n" if body[0].endswith("\r\n") else "\n"

    def chart(item, field, divisor, name, title, english):
        content = _png(item["series"]["points"], field, english, divisor)
        if content is None:
            return
        path = output_dir / (name + ".png")
        _write(path, content)
        # Resolve beside report.md on every platform; never expose the host path.
        markdown = "![趋势图](" + quote(path.name, safe="") + ")"
        images[markdown] = content
        charts.extend(["", "### " + title, "", markdown])

    for system in data["systems"]:
        segment = system["segment"]
        for field, divisor, key, title, unit in (
                ("cpu_single_core", 1, "cpu", "整机CPU趋势", "CPU single-core (%)"),
                ("mem_used_mb", 1024, "memory", "整机内存趋势", "Memory (GB)")):
            chart(system, field, divisor, f"system_s{segment}_{key}",
                  f"段{segment} {title}", f"System segment {segment} - {unit}")
    for number, (required, group) in enumerate(zip(data["required"], matched), 1):
        for process in group:
            segment = process["segment"]
            for field, divisor, key, title, unit in (
                    ("cpu1c", 1, "cpu", "CPU趋势", "CPU single-core (%)"),
                    ("rss_kb", 1024, "memory", "内存趋势", "RSS (MB)")):
                chart(process, field, divisor, f"process_{number:02d}_s{segment}_{key}",
                      f"段{segment} {required['name']} {title}",
                      f"Process {number} segment {segment} - {unit}")
    if not images:
        warnings.append("无有效时间与指标点，未生成趋势PNG。")
    body = [line for line in body
            if line.lstrip().startswith("|") or "折线图占位" not in line]
    additions = ["", "### 统计口径与覆盖说明", ""] + ["- " + warning for warning in warnings] + charts + ["", ""]
    markdown = "".join(prefix + body) + newline.join(additions) + "".join(suffix)
    preview = _preview(markdown, images)
    _write(output_dir / "report.md", markdown.encode("utf-8"))
    return dict(markdown=markdown, html=preview, warnings=warnings)

