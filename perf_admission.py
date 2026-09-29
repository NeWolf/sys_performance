"""Isolated offline admission reports. No authentication, publishing or store writes."""
from __future__ import annotations

import html
import math
import re
from pathlib import Path

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


def _without_images(markdown):
    """Remove template images, preserving surrounding text and table columns."""
    if not isinstance(markdown, str):
        raise ValueError("模板必须是字符串")

    def closing(text, start, left, right):
        depth, escaped = 0, False
        for index in range(start, len(text)):
            char = text[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif char == left:
                depth += 1
            elif char == right:
                depth -= 1
                if depth == 0:
                    return index + 1
        raise ValueError("模板图片语法不完整，请检查模板")

    # Protect literal code and escaped punctuation before scanning image syntax.
    # Tokens cannot collide with the input and are restored byte-for-byte.
    token_prefix = "\x00literal"
    while token_prefix in markdown:
        token_prefix += "_"
    literals = []

    def protect(value):
        token = f"{token_prefix}{len(literals)}\x00"
        if value.endswith("\n"):
            token += "\n"
        literals.append((token, value))
        return token

    lines, fence, block = [], None, []
    for line in markdown.splitlines(keepends=True):
        if fence:
            block.append(line)
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*(?:\r?\n)?", line):
                lines.append(protect("".join(block)))
                fence, block = None, []
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if opening:
            fence, block = opening[1], [line]
        elif line.startswith(("    ", "\t")):
            lines.append(protect(line))
        else:
            lines.append(line)
    if block:
        lines.append(protect("".join(block)))
    markdown = "".join(lines)
    markdown = re.sub(r"\\[!`<\\]", lambda m: protect(m[0]), markdown)
    markdown = re.sub(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)",
                      lambda m: protect(m[0]), markdown, flags=re.S)

    definitions = re.compile(r"^ {0,3}\[([^]\n]+)\]:[^\n]*(?:\n|$)", re.M)
    normalize_ref = lambda value: " ".join(value.split()).casefold()
    defined_refs = {normalize_ref(m[1]) for m in definitions.finditer(markdown)}
    result, cursor, image_refs = [], 0, set()
    while True:
        start = markdown.find("![", cursor)
        if start < 0:
            result.append(markdown[cursor:])
            break
        result.append(markdown[cursor:start])
        end = closing(markdown, start + 1, "[", "]")
        label = markdown[start + 2:end - 1]
        if markdown[end:end + 1] in ("(", "["):
            left, ref_start = markdown[end], end
            end = closing(markdown, end, left, ")" if left == "(" else "]")
            if left == "[":
                ref = normalize_ref(markdown[ref_start + 1:end - 1] or label)
                if ref in defined_refs:
                    image_refs.add(ref)
                else:
                    result.append(markdown[start:end])
        else:
            ref = normalize_ref(label)
            if ref in defined_refs:
                image_refs.add(ref)
            else:
                result.append(markdown[start:end])
        cursor = end
    text = "".join(result)
    # Drop definitions used only by removed images; preserve shared text links.
    definitions = re.compile(r"^ {0,3}\[([^]\n]+)\]:[^\n]*(?:\n|$)", re.M)
    body = definitions.sub("", text)
    remaining_refs = {normalize_ref(m) for m in re.findall(r"\[([^]\n]+)\]", body)}
    text = definitions.sub(lambda m: "" if normalize_ref(m[1]) in image_refs - remaining_refs else m[0], text)
    text = re.sub(r"<(picture|svg)\b[^>]*>.*?</\1\s*>", "", text, flags=re.I | re.S)
    text = re.sub(r"<(?:img|image)\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", "", text, flags=re.I)
    # Do not silently accept malformed containers that could hide report text.
    if re.search(r"<\s*/?\s*(?:img|image|picture|svg)\b", text, re.I):
        raise ValueError("模板图片 HTML 不完整，请检查模板")
    for token, value in reversed(literals):
        text = text.replace(token, value)
    return text


def _preview(markdown):
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
        if line.strip():
            result.append("<p>" + _inline(line) + "</p>")
    if table:
        result.append("</table>")
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'img-src \'none\'; style-src \'unsafe-inline\';">'
            '<style>body{font:14px sans-serif;margin:20px;overflow-wrap:anywhere}'
            'table{border-collapse:collapse;width:100%;margin:16px 0}'
            'td{border:1px solid #ccc;padding:6px}</style>'
            '</head><body>' + "\n".join(result) + '</body></html>')


def _write(path, content):
    if path.is_symlink():
        raise ValueError("输出文件不能是符号链接")
    path.write_bytes(content)


def build_admission(store, session_id, template: str, output_dir: Path,
                    scenes: dict[int, str], factor: float | None) -> dict:
    """Write an image-free report.md; return markdown, sandbox-safe html and warnings.

    Every observed segment needs an explicit scene. Invalid templates, scenes or
    factors raise ValueError before output. Only one read-consistent payload is used.
    """
    prefix, body, suffix, total, indices = _template(_without_images(template))
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
    return _finish(output_dir, prefix, body, suffix, warnings)


def _finish(output_dir, prefix, body, suffix, warnings):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    newline = "\r\n" if body[0].endswith("\r\n") else "\n"
    body = [line for line in body if not re.fullmatch(
        r"\s*[\s*]*【[\s*]*折线图占位[\s*]*】[^\r\n]*\s*", line)]
    additions = ["", "### 统计口径与覆盖说明", ""] + ["- " + warning for warning in warnings] + ["", ""]
    markdown = "".join(prefix + body) + newline.join(additions) + "".join(suffix)
    preview = _preview(markdown)
    _write(output_dir / "report.md", markdown.encode("utf-8"))
    return dict(markdown=markdown, html=preview, warnings=warnings)

