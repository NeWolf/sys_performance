"""Isolated offline admission reports. No authentication, publishing or store writes.

The report is produced by writing measured values straight into a JoySpace
template that was read as a Slate block tree.  Nothing is converted through
Markdown, so the tables, merged cells, column widths and colours of the
template survive into the published page unchanged.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path

import perf_slate
from perf_report_data import KDMIPS_PER_CORE, REQUIRED_PROCESSES, build_report_data
from perf_report_ui import EXCEL_BUDGETS

_SCENES = {"background", "foreground", "unknown"}
_GROUP_SCENES = {"前台": "foreground", "后台": "background"}


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
    return format(value, ".2f") if _valid(value) else ""


def _windows(store, session_id, segments, scenes):
    """Scene ranges come from 关注进程 groups; explicit scenes override a whole segment."""
    windows = {"background": [], "foreground": []}
    sources = []
    explicit = {} if scenes is None else scenes
    for segment in sorted(explicit):
        scene = explicit[segment]
        if scene in windows:
            windows[scene].append((segment, None, None))
        sources.append(f"segment {segment} 显式标注={scene}")
    for group in store.groups(session_id):
        segment = group["segment"]
        start, end = group.get("start"), group.get("end")
        label = group.get("scene", "未标注")
        scene = _GROUP_SCENES.get(label)
        if segment not in segments or segment in explicit or scene is None:
            continue
        windows[scene].append((segment, start, end))
        span = "整段" if start is None and end is None else \
            f"{'' if start is None else start}~{'' if end is None else end}ms"
        sources.append(f"segment {segment} 关注进程“{group['name']}”={label}（{span}）")
    return windows, sources


def _inside(windows, scene, segment, ts):
    for window, start, end in windows[scene]:
        if window != segment:
            continue
        if start is None and end is None:
            return True
        if not _valid(ts):
            continue
        if (start is None or ts >= start) and (end is None or ts <= end):
            return True
    return False


def _statistics(processes, schema, scene, windows, factor, plain=()):
    """Scene windows pick samples; segments in plain contribute every full cycle."""
    other = "foreground" if scene == "background" else "background"
    points = []
    for process in processes:
        for row in process["full_cycles"]:
            point = dict(zip(schema, row))
            if process["segment"] in plain or (
                    _inside(windows, scene, process["segment"], point.get("ts")) and
                    not _inside(windows, other, process["segment"], point.get("ts"))):
                points.append(point)
    cpu, memory = _values(points, "cpu1c"), _values(points, "rss_kb", 1024)
    p95, peak = _rank(cpu, 95), max(cpu) if cpu else None
    k = lambda value: value / 100 * factor if value is not None and factor is not None else None
    # Match the focus-process table: physical IO peak in MB/cycle, not MB/s.
    reads, writes = _values(points, "rd_kb", 1024), _values(points, "wr_kb", 1024)
    return [p95, k(p95), peak, k(peak),
            max(memory) if memory else None,
            max(reads) if reads else None, max(writes) if writes else None]


def _write(path, content):
    if path.is_symlink():
        raise ValueError("输出文件不能是符号链接")
    path.write_bytes(content)


def build_admission(store, session_id, template: list, output_dir: Path,
                    scenes: dict[int, str] | None = None, factor: float | None = None) -> dict:
    """Fill the template block tree and write report.json.

    Returns the filled blocks, a sandbox-safe preview and the warnings.

    Scenes are optional: foreground/background ranges are read from the session's
    关注进程 groups (scene 前台/后台 plus their millisecond range) and filled into
    section 5.2. An explicit scenes mapping still overrides a whole segment.
    Segments without any 前台/后台 label are not discarded: their full cycles are
    filed independently into each column group whose Excel requirement is Y
    (D=Y → 前台top-app, E=Y → 后台常驻服务); dual-Y rows fill both sides.
    Columns 4 and 5 transcribe those same Excel flags verbatim.
    Invalid templates, scenes or factors raise ValueError before output.
    K列采用与离线报告同一个默认系数（KDMIPS_PER_CORE），显式factor可覆盖；
    仅CPU缺失时K才留空，不以零代填。
    Group scene配置在独立快照中读取，样本仍来自同一份一致性报告数据。
    """
    if factor is not None and (not _valid(factor) or factor <= 0):
        raise ValueError("factor必须是显式正有限数值或None")
    # K列不留空：未显式给出系数时沿用离线报告同一个常量，使5.2与“当前实测”逐行可比。
    default_factor = factor is None
    if default_factor:
        factor = KDMIPS_PER_CORE
    if scenes is not None and (not isinstance(scenes, dict) or any(type(k) is not int for k in scenes) or
                               any(not isinstance(v, str) or v not in _SCENES for v in scenes.values())):
        raise ValueError("scenes可省略；给出时每段必须是background/foreground/unknown")
    names = [entry["name"] for entry in REQUIRED_PROCESSES]
    layout = perf_slate.validate_template(deepcopy(template), required_names=names)
    data = build_report_data(store, session_id, include_full_system=True)
    segments = {s["segment"] for s in data["systems"]}
    if scenes is not None and set(scenes) - segments:
        raise ValueError("scenes包含未观测的segment，不允许额外段")
    if [r["name"] for r in data["required"]] != names:
        raise ValueError("报告可靠匹配清单版本不符")
    for name in names:
        if layout.process_row(name) is None:
            raise ValueError("模板缺少进程行：" + name)
    schema = data["full_cycle_schema"]
    if not {"cpu1c", "rss_kb", "cycle", "ts"}.issubset(schema):
        raise ValueError("全量进程周期schema缺少必要字段")
    windows, sources = _windows(store, session_id, segments, scenes)
    # Segments nobody labelled still carry real samples; they are filed by the
    # Excel 前台需求/后台需求 flags instead of being dropped. An explicit
    # scenes["unknown"] is a deliberate refusal to classify, so it stays out.
    labelled = {s for entries in windows.values() for s, _, _ in entries}
    unlabelled = segments - labelled - set(scenes or ())
    warnings = [
        "CPU采用单核口径，可超过100%；P95/P99按全量有效样本最近秩重算，不平均各段分位数。",
        "缺失指标留空，不以零、整机CPU或其他字段代填；内存按1024换算，进程使用RSS。",
        "IO读/写填物理IO周期增量峰值（rd_kb/wr_kb÷1024，MB/周期），与关注进程一致；"
        "模板IO列虽标为MB/S，实测并非速率，与标准MB/s单位不同，不做红绿阈值判定。",
        "场景取自关注进程分组的前台/后台标注及其时间范围，CPU活跃不代表前后台；"
        "已标注段内未被时间范围覆盖的区间不进入前后台统计，前后台重叠区间双向剔除。",
        "第4、5列前台需求/后台需求按Excel准入清单原样填入，不做推断；模板中已人工填写的单元格保持原样。",
        "进程有有效实测时，需求非Y的一侧填-；无有效实测保持空白，适用侧缺失指标仍留空。",
        "实测值按各侧对应标准比较：超标红色、低于标准绿色、等于标准或无数值标准保持默认色。",
        "峰值原因与准入结论保留人工判断，不自动推断。",
    ]
    warnings.append("场景来源：" + "；".join(sources) if sources else
                    "未检测到前台/后台场景标注。")
    if unlabelled:
        warnings.append(
            "未标注场景的段（" + "、".join(f"segment {s}" for s in sorted(unlabelled)) +
            "）按Excel需求列归档：后台需求Y计入后台常驻服务列，前台需求Y计入前台top-app列，"
            "双Y时同一段实测分别填入两侧；该段取整段全部周期，与离线报告“当前实测”同一算法。")
    both = sorted({s for s, _, _ in windows["background"]} & {s for s, _, _ in windows["foreground"]})
    if both:
        warnings.append("前后台标注共存的段：" + "、".join(f"segment {s}" for s in both) +
                        "；重叠时间点不计入任一场景。")
    warnings.append(f"K为派生估算：CPU/100×{factor}，不是独立实测；" +
                    (f"沿用离线报告默认系数{KDMIPS_PER_CORE}，未提供自定义系数。" if default_factor
                     else "该系数来自本次请求。"))
    full = [p for system in data["systems"] for p in system["full_points"]]
    overall_rows = perf_slate.rows_of(layout.overall)
    for row, field, divisor in ((overall_rows[1], "cpu_single_core", 1),
       (overall_rows[2], "mem_used_mb", 1024)):
        values = _values(full, field, divisor)
        perf_slate.write_row(row, [_fmt(_rank(values, q)) for q in (95, 99)], 1)
        warnings.append(f"系统{field}有效样本 {len(values)}/{len(full)}。")
    by_id = {p["id"]: p for p in data["processes"]}
    matched = []
    for required in data["required"]:
        processes = [] if required["truncated"] else [by_id[pid] for pid in required["matches"]]
        matched.append(processes)
        row = layout.process_row(required["name"])
        source = EXCEL_BUDGETS[required["excel_row"]]
        # Columns 4 and 5 transcribe Excel verbatim, but never overwrite a cell a
        # human already filled in the template.
        cells = perf_slate.cells_of(row)
        for column, key in ((perf_slate.FG_NEED_COLUMN, "D"),
                            (perf_slate.BG_NEED_COLUMN, "E")):
            if not perf_slate.text_of(cells[column]).strip():
                perf_slate.write_row(row, [source.get(key, "")], column)
        statistics = {
            scene: _statistics(processes, schema, scene, windows, factor, unlabelled)
            for scene in ("background", "foreground")
        }
        has_data = any(value is not None for values in statistics.values() for value in values)
        for scene, offset, requirement, columns in (
                ("background", perf_slate.BACKGROUND_OFFSET, "E", "FGHIJKL"),
                ("foreground", perf_slate.FOREGROUND_OFFSET, "D", "MNOPQRS")):
            applicable = source.get(requirement) == "Y"
            values = statistics[scene] if applicable else [None] * len(perf_slate.METRICS)
            texts = [_fmt(value) for value in values] if applicable or not has_data else ["-"] * len(values)
            colors = []
            for value, key in zip(values, columns):
                limit = source.get(key)
                color = None
                # IO budgets are MB/s, but measured peaks are MB/cycle.
                if key not in "KLRS" and _valid(value) and _valid(limit):
                    if value > limit:
                        color = perf_slate.HIGH_COLOR
                    elif value < limit:
                        color = perf_slate.LOW_COLOR
                colors.append(color)
            perf_slate.write_row(row, texts, offset, colors=colors)
    covered = sum(any(p["p_records"] for p in group) for group in matched)
    warnings.append(f"可靠匹配采集覆盖：{covered}/49；歧义候选不参与，截断WebView始终留空。")
    missing = [r["name"] for r, group in zip(data["required"], matched) if not any(p["p_records"] for p in group)]
    if missing:
        warnings.append("未可靠采集：" + "、".join(missing))

    blocks = list(layout.blocks) + perf_slate.note_blocks(warnings)
    perf_slate.validate_tree(blocks)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / "report.json",
           json.dumps(blocks, ensure_ascii=False, indent=2).encode("utf-8"))
    return dict(blocks=blocks, html=perf_slate.to_html(blocks), warnings=warnings)