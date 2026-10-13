"""Reference-inspired light report, sharing the original full-sample statistics."""
import base64
import hashlib

from perf_report_ui import (
    ASSETS, JS as BASE_JS, esc, grid, metric, number, prepare_display,
    report_data_scripts, integer, pid_label, time_label,
)

CSS = (ASSETS / "report_compact.css").read_text(encoding="utf-8")
JS = (ASSETS / "report_compact.js").read_text(encoding="utf-8") + "\n;\n" + BASE_JS
REPORT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-" +
              base64.b64encode(hashlib.sha256(JS.encode("utf-8")).digest()).decode("ascii") +
              "'; base-uri 'none'; form-action 'none'")
CACHE_VERSION = "4:" + hashlib.sha256((CSS + "\0" + JS + "\0" + REPORT_CSP).encode()).hexdigest()


def chart(system, role, title):
    return '<div class="chart" id="' + esc(system["id"] + '-' + role) + '" role="img" aria-label="' + esc(title) + '"></div>'


def segment_html(data, system):
    processes = [p for p in data["processes"] if p["segment"] == system["segment"]]
    out = ['<section class="segment" id="' + esc(system["id"]) + '"><h2>采集段 ' + esc(system["segment"]) + '</h2><p>' + esc(time_label(system)) + '</p>']
    out.append('<section class="panel"><h2>总览 · 整机 CPU 与内存</h2><div class="cards">')
    out.append('<div class="card"><span>采样周期</span><strong>' + esc(integer(system["cycles"])) + '</strong></div>')
    for field, label, unit in (("cpu_total", "CPU", "%"), ("mem_used_mb", "已用内存", "MB")):
        for key, title in (("avg", "均值"), ("max", "峰值"), ("p95", "P95"), ("p99", "P99")):
            out.append('<div class="card"><span>' + label + ' ' + title + '</span><strong>' + esc(number(metric(system, field, key))) + '<small> ' + unit + '</small></strong></div>')
    out.append('</div><p class="note">CPU 单核满载 100%，整机可超过 100%；统计为全量有效样本，缺失不补零。保留全部采样点，每秒采集即可逐秒查看；可拖动底部滑块或滚轮缩放。时间异常、缺失周期处断线。</p><div class="charts">')
    out.append(chart(system, "system-cpu", "整机 CPU") + chart(system, "memory", "整机已用内存") + '</div></section>')
    for role, title in (("combined-cpu", "CPU"), ("combined-memory", "内存")):
        out.append('<section class="panel"><h2>组合图 · 整机 vs 各进程 ' + title + '</h2><p class="note">仅按方括号名称过滤内核进程；其他已采集进程均保留。默认整机与本指标均值前 15 个进程，各曲线独立展示，不叠加求和。进程 RSS 不等于整机已用内存。</p>')
        out.append('<div class="combination" data-role="' + role + '"><div class="legend-controls js-only"></div>' + chart(system, role, title + '组合图') + '</div></section>')
    out.append('<section class="panel"><h2>进程详情 · 点击查看折线图</h2><p class="note">点击行或按 Enter / 空格查看详情，点击表头排序。CPU、内存均使用全部有效观察周期（含有效零值），不是原报告全进程表的 CPU 活跃周期口径；同段同名进程按周期合计，不跨段合并。内存逐观察周期将 RSS 按 1024 KB = 1 MB 换算并舍入后统计。</p>')
    headers = ["进程", "PID", "CPU有效周期", "CPU均值 %", "CPU峰值 %", "CPU P95 %", "CPU P99 %", "内存有效周期", "内存均值 MB", "内存峰值 MB", "内存 P95 MB", "内存 P99 MB"]
    rows = [[p["name"], pid_label(p), metric(p, "cpu1c", "count"),
             *[metric(p, "cpu1c", k) for k in ("avg", "max", "p95", "p99")],
             metric(p, "rss_mb", "count"),
             *[metric(p, "rss_mb", k) for k in ("avg", "max", "p95", "p99")]] for p in processes]
    out.append('<div class="process-detail" tabindex="-1"><h3 class="detail-title" aria-live="polite">请选择一个进程</h3><div class="charts">' + chart(system, "detail-cpu", "进程 CPU 及 P95/P99") + chart(system, "detail-memory", "进程内存及 P95/P99") + '</div></div>')
    out.append(grid(headers, rows, [p["id"] for p in processes], True, "compact-processes", search_terms=[', '.join(map(str, p["pids"])) for p in processes]))
    out.append('</section></section>')
    return ''.join(out)


def render_document(data):
    data = prepare_display(data)
    data["method"]["charts"] = "CPU与内存曲线保留全部采样点，不降采样；缺失周期、无效值和非递增时间处断线，可缩放至逐秒查看。CPU提示按采集段、周期及精确时间匹配，不使用邻近值；组合图列出全部非内核进程（不受勾选影响），详情图列出当前进程与整机。"
    title = str(data["session"].get("name") or "性能采集报告")
    # Store only the four columns needed by full-resolution charts/tooltips.
    # full_points retains the original CPU basis, unlike prepared series.points.
    for system in data["systems"]:
        full = system.pop("full_points", None)
        points = full if full is not None else system["series"]["points"]
        if full is None and system["samples"] > len(points):
            raise ValueError("简洁报告需要完整整机采样数据，请启用 include_full_system")
        cpu = "cpu_single_core" if full is not None else "cpu_total"
        system["chart_samples"] = [[p.get("cycle"), p.get("ts"), p.get(cpu), p.get("mem_used_mb")]
                                   for p in points]
    schema = {name: i for i, name in enumerate(data["full_cycle_schema"])}
    for process in data["processes"]:
        rows = process.pop("full_cycles", [])
        process["chart_samples"] = [[row[schema[f]] for f in ("cycle", "ts", "cpu1c", "rss_kb")]
                                    for row in rows]
    out = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="' + esc(REPORT_CSP) + '"><title>' + esc(title) + ' · 简洁组合图</title><style>' + CSS + '</style></head><body data-report-style="compact"><header><h1>性能分析 · ' + esc(title) + '</h1><p>简洁组合图 · 离线交互报告</p></header><main>']
    if len(data["systems"]) > 1:
        out.append('<nav aria-label="采集段导航">' + ''.join('<a href="#' + esc(s["id"]) + '">采集段 ' + esc(s["segment"]) + '</a>' for s in data["systems"]) + '</nav>')
    out.append('<noscript><p>JavaScript 已禁用：统计卡和完整进程表仍可阅读，图表及筛选不可用。</p></noscript>')
    out.extend(segment_html(data, system) for system in data["systems"])
    if not data["systems"]:
        out.append('<section class="panel">没有可用采集段，未生成推测数据。</section>')
    out.append('<details class="panel"><summary>统计口径与数据来源</summary><dl>')
    for key, value in data.get("method", {}).items():
        out.append('<dt>' + esc(key) + '</dt><dd>' + esc(value) + '</dd>')
    out.append('</dl></details><footer>离线自包含 · 全量统计 · 缺失不补零 · 各采集段独立分析</footer></main>')
    out.extend(report_data_scripts(data))
    out.append('<script>' + JS + '</script></body></html>')
    return ''.join(out)