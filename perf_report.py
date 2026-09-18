"""Self-contained, script-free HTML reports with escaped log text."""
from html import escape
from datetime import datetime, timezone

from perf_metrics import METHOD, to_kdmips


def timestamp(value):
    if value is None:
        return "—"
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError, OSError):
        return str(value) + " ms"


def measure(value, unit, total=False):
    if value is None:
        return "—"
    if total:
        unit = unit.replace(" / 周期", "")
    if unit.startswith(("KB", "MB")):
        units = ["KB", "MB", "GB", "TB", "PB"]
        index = 1 if unit.startswith("MB") else 0
        while abs(value) >= 1024 and index < len(units) - 1:
            value /= 1024
            index += 1
        while value != 0 and abs(value) < 1 and index > 0:
            value *= 1024
            index -= 1
        unit = units[index] + (" / 周期" if "周期" in unit else "")
    number = format(value, ",.3f" if unit == "K" else ",.2f").rstrip("0").rstrip(".")
    return number + (" " + unit if unit else "")


def text(value):
    return escape("—" if value is None else str(value), quote=True)


def table(headers, rows):
    head = "".join("<th>" + text(value) + "</th>" for value in headers)
    body = "".join("<tr>" + "".join("<td>" + text(value) + "</td>" for value in row) + "</tr>" for row in rows)
    return "<div class='scroll'><table><thead><tr>" + head + "</tr></thead><tbody>" + body + "</tbody></table></div>"


def chart(series, field, title, unit):
    points = [point for point in series["points"] if point.get(field) is not None]
    if not points:
        return "<section><h3>" + text(title) + "</h3><p>无对应样本，不补零。</p></section>"
    start, end = min(p["ts"] for p in points), max(p["ts"] for p in points)
    low, high = min(p[field] for p in points), max(p[field] for p in points)
    dots, lines = [], []
    previous = None
    for point in points:
        x = 60 + (point["ts"] - start) / max(end - start, 1) * 810
        y = 190 - (point[field] - low) / max(high - low, 1) * 155
        if previous and point["segment"] == previous[0]["segment"] and point["cycle"] == previous[0]["cycle"] + 1:
            lines.append(f'<line x1="{previous[1]:.2f}" y1="{previous[2]:.2f}" x2="{x:.2f}" y2="{y:.2f}" stroke="#526ee8"/>')
        tip = text(f'{timestamp(point["ts"])}, {measure(point[field], unit)}')
        dots.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="#526ee8"><title>{tip}</title></circle>')
        previous = (point, x, y)
    svg = '<svg viewBox="0 0 920 240" role="img" aria-label="' + text(title) + '">'
    svg += '<path d="M60 25V195H880" fill="none" stroke="#8893a5"/>'
    svg += f'<text x="5" y="35">{text(measure(high, unit))}</text><text x="5" y="190">{text(measure(low, unit))}</text>'
    svg += f'<text x="60" y="225">{text(timestamp(start))}</text><text x="870" y="225" text-anchor="end">{text(timestamp(end))}</text>'
    svg += "".join(lines + dots) + "</svg>"
    note = f'显示 {len(points)} / {series["total"]} 条；横轴 UTC 时间；单位 {unit}。'
    if series["sampled"]:
        note += "图形经过降采样，在点数限额内优先保留极值，不能保证保留所有指标极值及端点，非完整原始序列。"
    return "<section><h3>" + text(title) + "</h3>" + svg + "<p>" + text(note) + "</p></section>"


def statistics_table(result, title):
    rows = []
    for field, metric in result["metrics"].items():
        rows.append([metric["label"], field, metric["count"],
                     *[measure(metric[key], metric["unit"]) for key in ("min", "avg", "p95", "p99", "max")],
                     measure(metric.get("total"), metric["unit"], total=True), metric["note"]])
    grouped = bool(result.get("scope", {}).get("group_id"))
    sample_label = "个范围周期" if grouped else "条原始记录"
    body = "<section><h3>" + text(title) + "</h3><p>" + text(
        f'{result["samples"]} {sample_label}；{timestamp(result["start"])} → {timestamp(result["end"])}') + "</p>"
    body += table(["指标", "原始字段", "有效周期" if grouped else "有效样本", "最小", "均值", "P95", "P99", "最大", "增量累计", "说明"], rows)
    for field, category in result["categories"].items():
        body += "<h4>" + text(category["label"] + " · " + field) + "</h4><p>" + text(category["note"]) + "</p>"
        body += table(["分类值", "样本数", "样本占比 %"],
                      [[item["value"], item["count"], measure(item["percent"], "")]
                       for item in category["values"]])
    return body + "</section>"


def placeholder(value, default="待填写"):
    return default if value is None or not str(value).strip() else value


def manual_section(settings, field, title, default="待填写"):
    return "<h3>" + text(title) + "</h3><p class='manual'>" + text(placeholder(settings.get(field), default)) + "</p>"


def metric_value(result, field, key, unit=None, factor=None):
    metric = result["statistics"]["metrics"][field]
    if unit == "K":
        return measure(to_kdmips(metric.get(key), factor), "K")
    return measure(metric.get(key), metric["unit"] if unit is None else unit,
                   total=key == "total")


def coverage_table(result, kind):
    coverage = result["coverage"]
    cycles = coverage["cycles"]
    ratio = measure(100 * coverage["complete"] / cycles, "%") if cycles else "—"
    body = "<h4>" + text(kind + " 覆盖率") + "</h4>"
    body += table(["记录类型", "范围周期", "完整周期", "部分周期", "缺失周期", "重复周期", "完整覆盖率"],
                  [[kind, *[coverage[key] for key in ("cycles", "complete", "partial", "missing", "duplicates")], ratio]])
    if not cycles:
        body += "<p>范围内无周期，待评估。</p>"
    elif coverage["complete"] < cycles or any(
            metric["count"] < cycles for metric in result["statistics"]["metrics"].values()):
        body += "<p>覆盖不完整（低覆盖率风险）：统计仅代表有效周期，不足以证明全场景最坏情况或自动判定准入。</p>"
    body += "<p>完整周期仅代表成员记录齐全，各指标仍可能缺失；重复周期与部分周期可能重叠，不可重复相加。</p>"
    body += table(["指标", "原始字段", "有效周期", "范围周期", "指标有效覆盖率"],
                  [[m["label"], field, m["count"], cycles,
                    measure(100 * m["count"] / cycles, "%") if cycles else "—"]
                   for field, m in result["statistics"]["metrics"].items()])
    return body


def module_details(group, analyses, factor=None):
    parts = ["<section><h3>" + text(group["name"]) + "</h3>"]
    if len(group["members"]) == 1:
        parts.append("<p>单成员组：仅统计所选成员在指定段和时间范围内的有效周期；wait 和分类字段不合计，原始明细可在工具中查看。</p>")
    p = analyses["P"]
    parts.append(table(["CPU P95 %", "CPU P99 %", "CPU P95 K", "CPU P99 K", "CPU 峰值 %", "CPU 峰值 K", "RSS 峰值"],
                       [[metric_value(p, f, k, unit, factor) for f, k, unit in (
                           ("cpu1c", "p95", "%"), ("cpu1c", "p99", "%"),
                           ("cpu1c", "p95", "K"), ("cpu1c", "p99", "K"),
                           ("cpu1c", "max", "%"), ("cpu1c", "max", "K"), ("rss_kb", "max", "KB"))]]))
    parts.append("<p>I/O MB/s 无法可靠换算，仅提供周期增量；累计仅包含完整有效周期。</p>")
    parts.append(table(["I/O", "周期增量 P95", "周期增量 P99", "周期增量峰值", "有效周期增量累计"],
                       [[title] + [metric_value(p, field, key) for key in ("p95", "p99", "max", "total")]
                        for field, title in (("rd_kb", "读取 rd"), ("wr_kb", "写入 wr"))]))
    for kind in ("P", "DP"):
        result = analyses[kind]
        parts.append(coverage_table(result, kind))
        parts.append("<p>" + text(result["note"]) + "</p>")
        fields = (("cpu", "CPU（整机口径）", "%"), ("cpu1c", "CPU cpu1c（单核口径）", "%"),
                  ("rss_kb", "RSS", "KB"), ("rd_kb", "读取 rd 周期增量", "KB / 周期"),
                  ("wr_kb", "写入 wr 周期增量", "KB / 周期")) if kind == "P" else (
                      ("size_kb", "DP dmabuf", "KB"), ("objs", "DP 对象数", "个"))
        for field, title, unit in fields:
            parts.append(chart(result["series"], field, title, unit))
        parts.append(statistics_table(result["statistics"], kind + " · 组同周期合计统计"))
    return "".join(parts) + "</section>"


def member_resources_table(resources):
    group = resources["group"]
    body = '<section><h3>' + text(group["name"] + ' · 成员实测资源表') + '</h3>'
    body += '<p>' + text(resources["note"]) + '</p>'
    body += '<p>仅填写组标注的场景；前台/后台运行标记不用于推断场景。未标注场景单列。K 单位为 KDMIPS。</p>'
    for category in ("应用服务", "系统服务", "未分类"):
        rows = []
        for row in resources["rows"]:
            member = row["member"]
            if member["service_category"] != category:
                continue
            for field, title, unit in (("cpu1c", "CPU 单核", "%"), ("kdmips", "CPU 算力", "K"),
                                       ("rss_kb", "RSS", "KB"), ("rd_kb", "读取", "KB / 周期"),
                                       ("wr_kb", "写入", "KB / 周期")):
                metric = row["kdmips"] if field == "kdmips" else row["statistics"][field]
                count = row["statistics"]["cpu1c"]["count"] if field == "kdmips" else metric["count"]
                values = " / ".join(measure(metric[key], unit) for key in ("min", "avg", "p95", "p99", "max"))
                if "total" in metric:
                    values += "；累计 " + measure(metric["total"], unit, total=True)
                rows.append([member["pid"], member["name"], member["related_service"], title, count,
                             *[values if group["scene"] == scene else "—" for scene in ("前台", "后台", "未标注")]])
        if rows:
            body += '<h4>' + text(category) + '</h4>'
            body += table(["PID", "进程", "关联服务说明", "指标", "有效样本",
                           "前台：最小 / 均值 / P95 / P99 / 峰值",
                           "后台：最小 / 均值 / P95 / P99 / 峰值",
                           "未标注：最小 / 均值 / P95 / P99 / 峰值"], rows)
    return body + '</section>'


def report_chart(traces, title, unit):
    """Shared axes; connect sampled points only inside pre-sampling metric runs."""
    valid = [(series, field, label, [p for p in series["points"] if p.get(field) is not None])
             for series, field, label in traces]
    points = [p for _, _, _, samples in valid for p in samples]
    heading = '<section><h3>' + text(title) + '</h3>'
    if not points:
        return heading + '<p>无对应样本，不补零。</p></section>'
    start, end = min(p["ts"] for p in points), max(p["ts"] for p in points)
    high = max(p[field] for _, field, _, samples in valid for p in samples)
    low = min(0, min(p[field] for _, field, _, samples in valid for p in samples))
    svg = ['<svg viewBox="0 0 1000 280" role="img" aria-label="' + text(title) + '">',
           '<path d="M110 45V230H970" fill="none" stroke="#8893a5"/>',
           f'<text x="5" y="55">{text(measure(high, unit))}</text>',
           f'<text x="5" y="230">{text(measure(low, unit))}</text>',
           f'<text x="110" y="265">{text(timestamp(start))}</text>',
           f'<text x="970" y="265" text-anchor="end">{text(timestamp(end))}</text>']
    notes = []
    for index, (series, field, label, samples) in enumerate(valid):
        color = ("#526ee8", "#b45309")[index % 2]
        dash = ' stroke-dasharray="6 3"' if index % 2 else ''
        svg.append(f'<text x="{110 + index * 280}" y="22" fill="{color}">{text(label)}</text>')
        previous = None
        for point in samples:
            x = 110 + (point["ts"] - start) / max(end - start, 1) * 850
            y = 230 - (point[field] - low) / max(high - low, 1) * 175
            run = point.get("_run_" + field)
            if (previous is not None and run is not None and run == previous[0].get("_run_" + field)
                    and point["segment"] == previous[0]["segment"] and point["ts"] > previous[0]["ts"]):
                svg.append(f'<line x1="{previous[1]:.2f}" y1="{previous[2]:.2f}" x2="{x:.2f}" y2="{y:.2f}" stroke="{color}"{dash}/>')
            tip = text(f'{label} · {timestamp(point["ts"])} · {measure(point[field], unit)}')
            svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="{color}"><title>{tip}</title></circle>')
            previous = (point, x, y)
        note = f'{label}：显示 {len(samples)} 个有效点 / {series["total"]} 条原始记录'
        if not samples:
            note += '；无对应样本，不补零'
        if series["sampled"]:
            note += '；已降采样，限额内优先保留极值，不保证保留所有极值及端点'
        notes.append(note)
    return heading + ''.join(svg) + '</svg><p>' + text('；'.join(notes)) + '</p></section>'


def render_report(store, session_id):
    session = store.session(session_id)
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
             '<title>' + text(session["name"]) + ' · 性能报告</title>',
             '<style>body{font:15px system-ui,sans-serif;color:#243148;background:#f5f7fb;margin:24px auto;padding:0 20px;max-width:1200px}details,section{background:white;padding:16px;margin:12px 0;border-radius:8px}summary{cursor:pointer;line-height:1.8;overflow-wrap:anywhere}summary:focus-visible{outline:2px solid #526ee8}table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left;overflow-wrap:anywhere}svg{width:100%;max-height:320px}svg text{font-size:12px}.scroll{overflow:auto}p{line-height:1.7}h1{overflow-wrap:anywhere}</style></head><body>',
             '<h1>' + text(session["name"]) + ' · 进程性能报告</h1>',
             '<p>点击进程行展开统计、CPU 对比与内存曲线；文件可离线查看，无脚本、无外部资源。</p>',
             '<p>按时间段、PID、名称精确区分，覆盖全部进程（含仅 DP 记录）。同名进程不合并；同段同 PID 同名的复用仍可能混合生命周期。</p>',
             '<p>CPU 比较使用整机口径 cpu / cpu_total，不混用单核 cpu1c；总 CPU 来自系统实测，非进程求和。内存为 RSS，不与 dmabuf 相加。容量按 1024 进位。</p>',
             '<p>' + text(METHOD) + ' 缺失显示 —，真实零保留；不进行准入判定。统计与曲线覆盖所在整段，不受进程组或人工配置影响。</p>',
             '<p>曲线每类最多 600 点，降采样不改变统计；仅在原始连续周期内连接采样点，缺失值、缺周期、重复周期及时间回拨断线。不推断仅时间间隔异常的采集缺口。横轴 UTC，悬停圆点查看数值。</p>',
             '<h2>全部进程资源统计</h2>']
    total, system_segment, system = 0, None, None
    with store.connect() as db:
        identities = db.execute("SELECT segment,pid,name FROM records WHERE session=? AND kind IN ('P','DP') "
                                "GROUP BY segment,pid,name ORDER BY segment,pid,name", (session_id,))
        for segment, pid, name in identities:
            scope = dict(segment=segment, pid=pid, name=name)
            stats = store.statistics(session_id, "P", **scope)
            metrics = stats["metrics"]
            process = store.report_series(session_id, "P", **scope)
            if segment != system_segment or system is None:
                system = store.report_series(session_id, "S", segment=segment)
                system_segment = segment
            label = f'段 {segment} · PID {pid} · {name}'
            parts.append('<details class="process"><summary>' + text(label) +
                         ' · CPU 均值 ' + text(measure(metrics["cpu"]["avg"], "%")) +
                         ' · RSS 峰值 ' + text(measure(metrics["rss_kb"]["max"], "KB")) + '</summary>')
            parts.append(table(["指标", "有效样本", "最小值", "均值", "P95", "P99", "峰值"],
                               [[title, metrics[field]["count"], *[measure(metrics[field][key], unit)
                                  for key in ("min", "avg", "p95", "p99", "max")]]
                                for field, title, unit in (("cpu", "CPU（整机）", "%"), ("rss_kb", "RSS", "KB"))]))
            if not stats["samples"]:
                parts.append('<p>仅有 DP 记录，无 P 样本：CPU / RSS 无对应样本，不补零。</p>')
            parts.append(report_chart([(process, "cpu", "进程 CPU"), (system, "cpu_total", "总 CPU")],
                                      "进程 CPU + 总 CPU", "%"))
            parts.append(report_chart([(process, "rss_kb", "RSS")], "内存占用（RSS）", "KB"))
            parts.append('</details>')
            total += 1
    parts.append('<p>共 ' + str(total) + ' 个进程身份，全部列出。</p>' if total else '<p>无进程记录。</p>')
    return ''.join(parts) + '</body></html>'


