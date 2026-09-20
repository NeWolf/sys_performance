"""New offline ECharts report; full statistics remain readable without scripts."""
import base64
import hashlib
import json
import math
from html import escape
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "report_assets"
CSS = (ASSETS / "report.css").read_text(encoding="utf-8")
# One fixed trusted script, never interpolated with session/log values.
JS = (ASSETS / "vendor/echarts.min.js").read_text(encoding="utf-8") + "\n;\n" + (ASSETS / "report.js").read_text(encoding="utf-8")
REPORT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-" +
              base64.b64encode(hashlib.sha256(JS.encode("utf-8")).digest()).decode("ascii") +
              "'; base-uri 'none'; form-action 'none'")
IO_FIELDS = (("rd_kb", "物理读"), ("wr_kb", "物理写"), ("rchar_kb", "逻辑读"), ("wchar_kb", "逻辑写"))
STATS = (("min", "最小"), ("avg", "均值"), ("p95", "P95"), ("p99", "P99"), ("max", "峰值"))


def esc(value):
    return escape("—" if value is None else str(value), quote=True)


def number(value):
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return "—"
        return format(value, ",.2f").rstrip("0").rstrip(".")
    return str(value)


def stamp(value):
    from perf_report import timestamp
    return timestamp(value)


def prepare_display(data):
    """Preserve system percentages and single-core process statistics."""
    from copy import deepcopy
    data = deepcopy(data)
    data["display_cpu_basis"] = "system-100-process-single-core"
    labels = {"cpu_total": "整机 CPU 总占用", "cpu_user": "用户态占用", "cpu_sys": "内核态占用",
              "cpu_iow": "iowait 占用", "cpu_irq": "irq+softirq 占用", "cpu_idle": "空闲",
              "mem_total_mb": "总内存", "mem_avail_mb": "空闲内存", "mem_used_mb": "已使用内存"}
    for system in data["systems"]:
        system["metrics"] = {field: dict(system["metrics"][field], label=label)
                             for field, label in labels.items()}
    data["system_fields"] = list(labels)
    data["method"]["cpu"] = "整机 CPU 满载为100%，不按核数换算。进程仍使用单核cpu1c，可超过100%，缺失不回退；固定28.75 KDMIPS/核。"
    data["method"]["system"] = "每条原始S有效字段独立统计，重复S不去重；空闲=100−整机CPU总占用。各字段含空闲均用全量有效样本计算P95/P99。超标严格大于90/95/99%，分母为有效S样本，非时间占比。缺失字段留空。"
    data["method"]["memory"] = "MemTotal、MemAvailable与mem_used_mb单位为MB；每条S按MemTotal−MemAvailable计算已用内存，含不可回收部分，不把可回收cache算作已用；任一字段缺失则已用缺失。"
    data["method"]["chart_time"] = "趋势横轴显示小时:分钟（UTC，与日志时间表一致），提示保留完整日期时间；虚线为整段全量P95/P99，缩放不重算。"
    return data


def time_label(system):
    from datetime import datetime, timezone
    times = [row[1] for row in system["cycle_axis"]
             if isinstance(row[1], (int, float)) and math.isfinite(row[1])]
    if not times:
        return "开始测试：— · 结束时间：— · 采集时长：—"
    start, end = min(times), max(times)
    def formatted(value):
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y年%m月%d日 %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return "—"
    hours, remainder = divmod(int((end - start) / 1000), 3600)
    minutes, seconds = divmod(remainder, 60)
    return (f"开始测试：{formatted(start)} · 结束时间：{formatted(end)} · "
            f"采集{hours}小时{minutes}分钟{seconds}秒")


def metric(item, field, key="avg", active=False):
    scope = (item or {}).get("active", {}) if active else (item or {})
    return scope.get("metrics", {}).get(field, {}).get(key)


def scaled(value, divisor):
    return value / divisor if value is not None else None


def grid(headers, rows, identities=None, searchable=False, role=""):
    out = ['<div class="data-table" data-table="' + esc(role) + '">']
    if searchable:
        out.append('<div class="controls js-only"><label>搜索 <input type="search" class="table-search" placeholder="名称、PID、状态；点击表头排序" aria-label="搜索表格"></label><span class="table-count" aria-live="polite"></span></div>')
    out.append('<div class="scroll"><table><thead><tr>')
    out.extend('<th scope="col">' + esc(h) + '</th>' for h in headers)
    out.append('</tr></thead><tbody>')
    for index, row in enumerate(rows):
        identity = identities[index] if identities else None
        attrs = ' data-process="' + esc(identity) + '" tabindex="0" aria-selected="false"' if identity else ''
        out.append('<tr' + attrs + '>')
        for value in row:
            cls = ' class="name"' if isinstance(value, str) and len(value) > 22 else ''
            out.append('<td' + cls + ' data-sort="' + esc('' if value is None else value) + '">' + esc(number(value)) + '</td>')
        out.append('</tr>')
    return ''.join(out) + '</tbody></table></div></div>'


def statistics(metrics, cycles=None):
    groups = []
    for category in ("cpu", "memory", "io"):
        rows = []
        headers = ["指标", "单位", "有效样本", "最小", "均值", "P95", "P99", "峰值"]
        if category == "io":
            headers += ["有效增量累计 KB"]
        elif category == "cpu":
            headers += ["P95K KDMIPS", "峰值K KDMIPS"]
        for field, m in metrics.items():
            if field == "cpu":
                continue
            kind = "io" if field in dict(IO_FIELDS) else "cpu" if field.startswith("cpu") else "memory"
            if kind != category:
                continue
            divisor = 1024 if field == "rss_kb" else 1
            unit = "MB" if field == "rss_kb" else "KB/周期" if kind == "io" else m.get("unit", "%")
            row = [m.get("label", field), unit, m.get("count", 0), *[scaled(m.get(k), divisor) for k, _ in STATS]]
            if kind == "io":
                row += [m.get("total")]
            elif kind == "cpu":
                row += [scaled(m.get(k), 100 / 28.75) for k in ("p95", "max")]
            rows.append(row)
        if rows:
            groups.append(grid(headers, rows))
    return ''.join(groups)


def stat_cards(metrics):
    out = ['<div class="stat-grid">']
    for field, m in metrics.items():
        out.append('<div class="stat-item"><div class="stat-header"><span>' + esc(m.get("label", field)) + ' · ' + esc(m.get("unit", "%")) + '</span><strong>' + esc(number(m.get("avg"))) + '</strong></div><div class="stat-details">')
        for key, label in (("max", "最大"), ("min", "最小"), ("p95", "P95"), ("p99", "P99"), ("count", "有效样本")):
            out.append('<div><span>' + label + '</span><b class="' + key + '">' + esc(number(m.get(key))) + '</b></div>')
        out.append('</div></div>')
    return ''.join(out) + '</div>'


PROCESS_HEADERS = ["序号", "原始进程名", "PID", "活跃周期", "活跃均值 %", "活跃P95 %", "活跃P99 %", "活跃峰值 %", "P95K KDMIPS", "峰值K KDMIPS", "RSS均值 MB", "RSS P95 MB", "RSS峰值 MB", "累计读 KB", "累计写 KB"]
for _field, _label in IO_FIELDS:
    PROCESS_HEADERS += [_label + "均值 KB/周期", _label + "P95 KB/周期", _label + "峰值 KB/周期"]
    if _field not in ("rd_kb", "wr_kb"):
        PROCESS_HEADERS.append(_label + "累计 KB")
PROCESS_HEADERS += ["PID变化"]


def process_row(p, ordinal):
    p = p or {}
    cpu = lambda k, active=False: metric(p, "cpu1c", k, active)
    rows = [ordinal, p.get("name"), ', '.join(str(v) for v in p.get("pids", [])) or None,
            p.get("active", {}).get("cycles"),
            *[cpu(k, True) for k in ("avg", "p95", "p99", "max")],
            scaled(cpu("p95"), 100 / 28.75), scaled(cpu("max"), 100 / 28.75),
            *[scaled(metric(p, "rss_kb", k), 1024) for k in ("avg", "p95", "max")],
            metric(p, "rd_kb", "total"), metric(p, "wr_kb", "total")]
    for field, _ in IO_FIELDS:
        rows.extend(metric(p, field, k) for k in ("avg", "p95", "max"))
        if field not in ("rd_kb", "wr_kb"):
            rows.append(metric(p, field, "total"))
    return rows + [p.get("pid_changes")]


EXCEL_BUDGETS = {
    7: {'D': 'N', 'E': 'Y', 'F': 2.0, 'G': 0.6, 'H': 5.0, 'I': 1.4, 'J': 150.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    8: {'D': 'N', 'E': 'Y', 'F': 40.0, 'G': 11.5, 'H': 50.0, 'I': 14.4, 'J': 300.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    9: {'D': 'Y', 'E': 'Y', 'F': 3.0, 'G': 0.9, 'H': 5.0, 'I': 1.4, 'J': 150.0, 'K': 0.0, 'L': 0.0, 'M': 20.0, 'O': 50.0, 'Q': 200.0, 'R': 0.0, 'S': 0.0},
    10: {'D': 'Y', 'E': 'Y', 'F': 10.0, 'G': 2.9, 'H': 15.0, 'I': 4.3, 'J': 1000.0, 'K': 0.0, 'L': 0.0, 'M': 70.0, 'N': 20.1, 'O': 100.0, 'P': 28.8, 'Q': 1500.0, 'R': 0.0, 'S': 0.0},
    11: {'D': 'N', 'E': 'Y', 'F': 3.0, 'G': 0.9, 'H': 7.0, 'I': 2.0, 'J': 140.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    12: {'D': 'Y', 'E': 'Y', 'F': 10.0, 'G': 2.9, 'H': 20.0, 'I': 5.8, 'J': 1000.0, 'K': 0.0, 'L': 0.0, 'M': 30.0, 'N': 8.6, 'O': 40.0, 'P': 11.5, 'Q': 1000.0, 'R': 0.0, 'S': 0.0},
    13: {'D': 'N', 'E': 'Y', 'F': 30.0, 'G': 8.6, 'H': 30.0, 'I': 8.6, 'J': 500.0, 'K': 12.0, 'L': 8.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    14: {'D': 'N', 'E': 'Y', 'F': 10.0, 'G': 2.9, 'H': 15.0, 'I': 4.3, 'J': 100.0, 'K': 1.0, 'L': 2.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    15: {'D': 'N', 'E': 'Y', 'F': 25.0, 'G': 7.2, 'H': 20.0, 'I': 5.8, 'J': 100.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    16: {'D': 'N', 'E': 'Y', 'F': 20.0, 'G': 5.8, 'H': 40.0, 'I': 11.5, 'J': 500.0, 'K': 1.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    17: {'D': 'N', 'E': 'Y', 'F': 5.0, 'G': 1.4, 'H': 10.0, 'I': 2.9, 'J': 150.0, 'K': 0.0, 'L': 1.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    18: {'D': 'N', 'E': 'Y', 'F': 4.0, 'G': 1.2, 'H': 15.0, 'I': 4.3, 'J': 80.0, 'K': 0.0, 'L': 1.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    19: {'D': 'N', 'E': 'Y', 'F': 10.0, 'G': 2.9, 'H': 20.0, 'I': 5.8, 'J': 50.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    20: {'D': 'Y', 'E': 'Y', 'F': 0.0, 'G': 0.0, 'H': 0.0, 'I': 0.0, 'J': 150.0, 'K': 0.0, 'L': 0.0, 'M': 5.0, 'N': 1.4, 'O': 60.0, 'P': 17.3, 'Q': 300.0, 'R': 0.0, 'S': 0.0},
    21: {'D': 'Y', 'E': 'Y', 'F': 2.0, 'G': 0.6, 'H': 10.0, 'I': 2.9, 'J': 600.0, 'K': 0.0, 'L': 0.0, 'M': 40.0, 'N': 11.5, 'O': 100.0, 'P': 28.8, 'Q': 900.0, 'R': 4.0, 'S': 1.0},
    22: {'D': 'N', 'E': 'Y', 'F': 2.0, 'G': 0.6, 'H': 10.0, 'I': 2.9, 'J': 150.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    23: {'D': 'Y', 'E': 'N', 'F': '—', 'G': '—', 'H': '—', 'I': '—', 'J': '—', 'K': '—', 'L': '—', 'M': 50.0, 'N': 14.4, 'O': 100.0, 'P': 28.8, 'Q': 800.0, 'R': 5.0, 'S': 1.0},
    24: {'D': 'Y', 'E': 'Y', 'F': 3.0, 'G': 0.9, 'H': 5.0, 'I': 1.4, 'J': 150.0, 'K': 0.0, 'L': 0.0, 'M': 50.0, 'N': 14.4, 'O': 100.0, 'P': 28.8, 'Q': 600.0, 'R': 0.0, 'S': 0.0},
    25: {'D': 'Y', 'E': 'N', 'F': '—', 'G': '—', 'H': '—', 'I': '—', 'J': '—', 'K': '—', 'L': '—', 'M': 2.0, 'N': 0.6, 'O': 4.0, 'P': 1.2, 'Q': 130.0, 'R': 0.0, 'S': 0.0},
    26: {'D': 'Y', 'E': 'N', 'F': '—', 'G': '—', 'H': '—', 'I': '—', 'J': '—', 'K': '—', 'L': '—', 'M': 50.0, 'N': 14.4, 'O': 80.0, 'P': 23.0, 'Q': 500.0, 'R': 4.0, 'S': 2.0},
    27: {'D': 'N', 'E': 'Y', 'F': 4.0, 'G': 1.2, 'H': 6.0, 'I': 1.7, 'J': 200.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    28: {'D': 'Y', 'E': 'Y', 'F': 2.0, 'G': 0.6, 'H': 5.0, 'I': 1.4, 'J': 200.0, 'K': 0.0, 'L': 0.0, 'M': 20.0, 'N': 5.8, 'O': 80.0, 'P': 23.0, 'Q': 250.0, 'R': 3.0, 'S': 0.0},
    29: {'D': 'Y', 'E': 'N', 'F': '—', 'G': '—', 'H': '—', 'I': '—', 'J': '—', 'K': '—', 'L': '—', 'M': 30.0, 'N': 8.6, 'O': 50.0, 'P': 14.4, 'Q': 300.0, 'R': 0.0, 'S': 0.0},
    30: {'D': 'Y', 'E': 'N', 'F': '—', 'G': '—', 'H': '—', 'I': '—', 'J': '—', 'K': '—', 'L': '—', 'M': 60.0, 'N': 17.3, 'O': 70.0, 'P': 20.1, 'Q': 1000.0, 'R': 0.0, 'S': 0.0},
    31: {'D': 'Y', 'E': 'N', 'F': '—', 'G': '—', 'H': '—', 'I': '—', 'J': '—', 'M': 60.0, 'N': 17.3, 'O': 70.0, 'P': 20.1},
    32: {'D': 'Y', 'E': 'N', 'F': '—', 'G': '—', 'H': '—', 'I': '—', 'J': '—', 'K': '—', 'L': '—', 'M': 30.0, 'N': 8.6, 'O': 50.0, 'P': 14.4, 'Q': 150.0, 'R': 0.0, 'S': 0.0},
    33: {'D': 'N', 'E': 'Y', 'F': 1.0, 'G': 0.3, 'H': 2.0, 'I': 0.6, 'J': 20.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    34: {'D': 'N', 'E': 'Y', 'F': 28.0, 'G': 8.1, 'H': 40.0, 'I': 11.5, 'J': 100.0, 'K': 20.0, 'L': 2.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    35: {'D': 'Y', 'E': 'Y', 'F': 2.0, 'G': 0.6, 'H': 0.0, 'I': 0.0, 'J': 200.0, 'K': 0.0, 'L': 0.0, 'M': 2.0, 'N': 0.6, 'O': 15.0, 'P': 4.3, 'Q': 200.0, 'R': 0.0, 'S': 0.0},
    36: {'D': 'Y', 'E': 'Y', 'F': 2.0, 'G': 0.6, 'H': 0.0, 'I': 0.0, 'J': 250.0, 'K': 0.0, 'L': 0.0, 'M': 2.0, 'N': 0.6, 'O': 15.0, 'P': 4.3, 'Q': 300.0, 'R': 0.0, 'S': 0.0},
    37: {'D': 'N', 'E': 'Y', 'F': 2.0, 'G': 0.6, 'H': 5.0, 'I': 1.4, 'J': 150.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    39: {'D': 'N', 'E': 'Y', 'F': 1.0, 'G': 0.3, 'H': 7.0, 'I': 2.0, 'J': 400.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    40: {'D': 'N', 'E': 'Y', 'F': 1.0, 'G': 0.3, 'H': 4.0, 'I': 1.2, 'J': 400.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    41: {'D': 'N', 'E': 'Y', 'F': 1.0, 'G': 0.3, 'H': 2.0, 'I': 0.6, 'J': 200.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    42: {'D': 'N', 'E': 'Y', 'F': 30.0, 'G': 8.6, 'H': 197.0, 'I': 56.6, 'J': 400.0, 'K': 0.0, 'L': 1.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    43: {'D': 'N', 'E': 'Y', 'F': 2.0, 'G': 0.6, 'H': 5.0, 'I': 1.4, 'J': 150.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    44: {'D': 'N', 'E': 'Y', 'F': 8.0, 'G': 2.3, 'H': 17.0, 'I': 4.9, 'J': 40.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    45: {'D': 'N', 'E': 'Y', 'F': 6.0, 'G': 1.7, 'H': 19.0, 'I': 5.5, 'J': 40.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    46: {'D': 'N', 'E': 'Y', 'F': 12.0, 'G': 3.5, 'H': 14.2, 'I': 4.1, 'J': 40.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    47: {'D': 'N', 'E': 'Y', 'F': 17.0, 'G': 4.9, 'H': 24.0, 'I': 6.9, 'J': 120.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    48: {'D': 'N', 'E': 'Y', 'F': 1.0, 'G': 0.3, 'H': 5.0, 'I': 1.4, 'J': 100.0, 'K': 10.0, 'L': 1.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    49: {'D': 'N', 'E': 'Y', 'F': 0.0, 'G': 0.0, 'H': 3.8, 'I': 1.1, 'J': 5.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    50: {'D': 'N', 'E': 'Y', 'F': 7.0, 'G': 2.0, 'H': 10.7, 'I': 3.1, 'J': 21.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    51: {'D': 'N', 'E': 'Y', 'F': 8.0, 'G': 2.3, 'H': 22.0, 'I': 6.3, 'J': 51.0, 'K': 0.0, 'L': 2.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    52: {'D': 'N', 'E': 'Y', 'F': 7.0, 'G': 2.0, 'H': 43.0, 'I': 12.4, 'J': 52.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    53: {'D': 'N', 'E': 'Y', 'F': 10.0, 'G': 2.9, 'H': 20.0, 'I': 5.8, 'J': 180.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    54: {'D': 'N', 'E': 'Y', 'F': 20.0, 'G': 5.8, 'H': 30.0, 'I': 8.6, 'J': 50.0, 'K': 0.0, 'L': 0.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    55: {'D': 'N', 'E': 'Y', 'F': 10.0, 'G': 2.9, 'H': 20.0, 'I': 5.8, 'J': 50.0, 'K': 15.0, 'L': 2.0, 'M': '—', 'N': '—', 'O': '—', 'P': '—', 'Q': '—', 'R': '—', 'S': '—'},
    56: {'D': 'N', 'E': 'Y', 'F': '—', 'G': '—', 'H': '—', 'I': '—', 'J': '—', 'K': 0.0, 'L': 0.0, 'M': 60.0, 'N': 17.3, 'O': 70.0, 'P': 20.1, 'Q': 300.0, 'R': '—', 'S': '—'},
}


def required_table(data, segment):
    lookup = {p["id"]: p for p in data["processes"]}
    states = {"collected": "已匹配 / P实测", "dp_only": "DP-only / 无P", "missing": "缺失", "pending_confirmation": "候选 / 待确认"}
    rows, ids = [], []
    for item in data["required"]:
        status = next((s for s in item["segments"] if s["segment"] == segment), {})
        matches = status.get("process_ids", [])
        p = lookup.get(matches[0]) if matches else None
        candidates = ', '.join(str(lookup[i]["name"]) for i in status.get("candidate_ids", [])) or None
        source = EXCEL_BUDGETS[item["excel_row"]]
        rows.append([item["module"], item["business"], item["name"], source.get("D"), source.get("E"),
                     states.get(status.get("status"), "缺失"), candidates,
                     metric(p, "cpu1c", "p95"), scaled(metric(p, "cpu1c", "p95"), 100 / 28.75),
                     metric(p, "cpu1c", "max"), scaled(metric(p, "cpu1c", "max"), 100 / 28.75),
                     scaled(metric(p, "rss_kb", "max"), 1024), None, None])
        ids.append(p["id"] if p else None)
    headers = ["模块", "作用", "进程(cmdline)", "前台需求", "后台需求", "匹配状态", "候选（不计实测）",
               "P95% · 单核", "P95 K · KDMIPS", "峰值% · 单核", "峰值K · KDMIPS", "内存峰MB · RSS", "IO读MB/S", "IO写MB/S"]
    budgets = []
    for item in data["required"]:
        source = EXCEL_BUDGETS[item["excel_row"]]
        for scene, cols in (("后台常驻服务", "FGHIJKL"), ("前台top-app", "MNOPQRS")):
            budgets.append([item["excel_row"], item["name"], scene, *[source.get(c) for c in cols]])
    return (grid(headers, rows, ids, True, "excel") +
            '<details class="excel-source"><summary>查看真实源表预算（非实测）</summary>' +
            grid(["Excel行", "进程(cmdline)", "源表分组", "P95%", "P95 K", "峰值%", "峰值K", "内存峰MB", "IO读MB/S", "IO写MB/S"], budgets, searchable=True) + '</details>')


def chart(system, role, title, size=""):
    return '<div class="chart ' + size + '" id="' + esc(system["id"] + '-' + role) + '" data-chart="' + role + '" role="img" aria-label="' + esc(title) + '"></div>'


def segment_html(data, system):
    segment = system["segment"]
    processes = [p for p in data["processes"] if p["segment"] == segment]
    metrics = system["metrics"]
    cpu_metrics = {f: m for f, m in metrics.items() if f.startswith("cpu")}
    mem_metrics = {f: m for f, m in metrics.items() if f.startswith("mem")}
    out = ['<section class="segment" id="' + esc(system["id"]) + '" data-system="' + esc(system["id"]) + '"><div class="segment-heading"><h2>' + ''.join('<span>' + esc(part) + '</span>' for part in time_label(system).split(' · ')) + '</h2></div>']
    average = metrics["cpu_total"].get("avg")
    cpu_level = "unknown"
    if isinstance(average, (int, float)) and math.isfinite(average):
        cpu_level = "low" if average < 70 else "medium" if average < 80 else "high"
    out.append('<section class="panel overview"><h3>整体概览</h3><div class="overview-primary cpu-' + cpu_level + '">' + stat_cards({"cpu_total": metrics["cpu_total"]}) + '</div><div class="charts"><div><h4>整机 CPU 总占用 · 满载 100%</h4>' + chart(system, "system-cpu", "整机CPU总占用、用户态、内核态、iowait、irq+softirq与空闲") + '</div><div><h4>内存占用</h4>' + chart(system, "memory", "已使用内存、空闲内存及P95/P99") + '</div></div>')
    out.append('<p class="note">整机 CPU 满载为 100%，不按核数换算；空闲 = 100% − 整机 CPU 总占用%。均值卡：低于70%绿色，70%至不足80%黄色，80%及以上红色。总占用曲线红色加粗。默认仅显示整机 CPU 总占用与已使用内存，点击图例可显示其他曲线。内存单位为 MB；已使用内存 = MemTotal − MemAvailable，空闲内存指可用的 MemAvailable；已使用含不可回收部分，不把可回收 cache 算作已用。总内存不绘图、不展示分位数。虚线为完整时段全量有效样本的 P95 / P99，缩放不重算；横轴为小时:分钟（UTC），缺失不补零。</p>')
    out.append(grid(["CPU指标 · %", "最小", "最大", "均值", "P95", "P99", "有效样本"], [[m["label"], m.get("min"), m.get("max"), m.get("avg"), m.get("p95"), m.get("p99"), m.get("count")] for m in cpu_metrics.values()]))
    out.append(grid(["内存指标", "最小", "最大", "均值", "P95", "P99", "有效样本"],
                    [[m["label"], m.get("min"), m.get("max"), m.get("avg"),
                      m.get("p95") if field != "mem_total_mb" else None,
                      m.get("p99") if field != "mem_total_mb" else None, m.get("count")]
                     for field, m in mem_metrics.items()]))
    out.append('</section>')
    exceed = system["cpu_exceedances"]
    out.append('<details class="panel"><summary>CPU 超标分布明细 · 整机 100% 口径</summary>' + chart(system, "exceed", "CPU 超标彩色柱", "small"))
    out.append(grid(["严格阈值 · 整机 %", "样本数", "有效 S 样本占比 %"], [[t["threshold"], t["count"], t["percent"]] for t in exceed["thresholds"]]))
    limit = exceed["thresholds"][0]["threshold"]
    out.append('<p class="note">分母为有效系统CPU样本 ' + esc(exceed["valid_samples"]) + '；不是时间占比。超过 ' + esc(number(limit)) + '% 共 ' + esc(number(exceed["total"])) + ' 条，显示前 ' + esc(len(exceed["details"])) + ' 条；' + ('明细已截断。' if exceed["truncated"] else '明细未截断。') + '</p>')
    out.append(grid(["记录ID", "周期", "UTC时间", "整机 CPU 总占用 · %", "来源", "行号"], [[r["record_id"], r["cycle"], stamp(r["ts"]), r["cpu_total"], r["source"], r["line"]] for r in exceed["details"]], searchable=True))
    out.append('</details>')
    for role, title, field, key, active in (("active-top", "活跃 CPU Top30 · 单核活跃均值", "cpu1c", "avg", True), ("peak-top", "CPU 峰值 Top30 · 趋势", "cpu1c", "max", False), ("rss-top", "RSS 均值 Top30", "rss_kb", "avg", False)):
        ranked = sorted((p for p in processes if metric(p, field, key, active) is not None), key=lambda p: (-metric(p, field, key, active), p["name"]))[:30]
        tag = "details"
        heading = '<summary>' + title + '</summary>'
        out.append('<' + tag + ' class="panel">' + heading + chart(system, role, title, "tall" if role != "peak-top" else ""))
        unit = "MB" if field == "rss_kb" else "%" if role == "peak-top" else "单核 %"
        headers = ["进程", "PID", "有效周期", *[label + " · " + unit for _, label in STATS]]
        rows = []
        for p in ranked:
            m = (p["active"] if active else p)["metrics"][field]
            rows.append([p["name"], ', '.join(map(str, p["pids"])), m["count"], *[scaled(m[k], 1024 if field == "rss_kb" else 1) for k, _ in STATS]])
        out.append(grid(headers, rows, [p["id"] for p in ranked], True, role) + '</' + tag + '>')
    out.append('<details class="panel"><summary>物理 / 逻辑 IO · 有效增量及累计</summary><p class="note">KB/周期是计数增量，不是 KB/s 或 MB/s；累计只累加有效增量，缺失不补零。选择进程查看其独立曲线，默认展示物理 IO 有效累计靠前的进程。</p><div class="charts">')
    for role, title in (("io-physical", "物理 IO 增量"), ("io-logical", "逻辑 IO 增量"), ("io-physical-total", "物理 IO 累计"), ("io-logical-total", "逻辑 IO 累计")):
        out.append('<div><h4>' + title + '</h4>' + chart(system, role, title) + '</div>')
    io_headers = ["进程", "PID"] + [label + suffix for _, label in IO_FIELDS for suffix in ("均值 KB/周期", "P95 KB/周期", "峰值 KB/周期", "累计 KB")]
    io_rows = [[p["name"], ', '.join(map(str, p["pids"])), *[metric(p, f, k) for f, _ in IO_FIELDS for k in ("avg", "p95", "max", "total")]] for p in processes]
    out.append('</div>' + grid(io_headers, io_rows, [p["id"] for p in processes], True, "io") + '</details>')
    out.append('<section class="panel"><h3>Excel 49 项 · 当前时段逐项实测</h3><p class="note">独立核对全部49项；实测为当前完整时段，日志没有前后台场景标记，不推断场景。候选与截断名称不计入实测，DP-only不等于P采集成功。实测K固定按28.75 KDMIPS/核换算；源预算K照录，不修正源表不一致。RSS峰值不等同于源表所有内存口径。日志仅有IO周期增量，无法给出源表MB/S速率，因此留空。</p>' + required_table(data, segment) + '</section>')
    reference_note = '<p class="note">默认参照：系统 CPU＝整机总占用×8（满载800%）；已使用内存＝总内存−MemAvailable。IO 按本周期已采集 P 进程分别汇总物理读、物理写、逻辑读、逻辑写；未采集进程不计入，各字段仅汇总有效值（可能不完整），无有效值时断线。</p>'
    out.append('<section class="panel"><h3>全进程 · 搜索排序与叠加趋势</h3><p class="note">点击行或按 Enter / 空格添加、移除叠加曲线；不改变统计。零活跃及 DP-only 身份完整保留。</p>' + reference_note + grid(PROCESS_HEADERS, [process_row(p, n) for n, p in enumerate(processes, 1)], [p["id"] for p in processes], True, "all"))
    out.append('<div class="selection-tags js-only" aria-live="polite"></div>')
    for role, title in (("overlay-cpu", "选中进程单核 CPU %"), ("overlay-rss", "选中进程 RSS MB"), ("overlay-io", "选中进程 IO 增量 KB/周期")):
        out.append('<h4>' + title + '</h4>' + chart(system, role, title))
    out.append('</section>')
    out.append('<section class="panel"><h3>关联进程 · 搜索、勾选与精确合并</h3><p class="note">仅同段合并，按本段全部周期对齐。任一成员行或字段缺失，则对应合计缺失；全量重算最近秩分位数。不会相加分位数。</p>' + reference_note + '<div class="js-only"><div class="merge-grid"><div class="merge-column"><h4>可选进程</h4><label>搜索 <input class="merge-search" type="search" placeholder="名称 / PID" aria-label="搜索可合并进程"></label><div class="candidate-list"></div></div><div class="merge-column"><h4>已选进程 <span class="merge-count">0</span></h4><button class="merge-clear" type="button">清空选择</button><div class="selected-list"></div></div></div><button class="merge-apply" type="button">计算全量合并统计</button><p class="merge-status status" aria-live="polite"></p><div class="merge-result"></div></div>')
    for role, title in (("merge-cpu", "合并单核 CPU"), ("merge-rss", "合并 RSS MB"), ("merge-io", "合并 IO 增量")):
        out.append(chart(system, role, title))

    return ''.join(out) + '</section></section>'


def render_document(data):
    data = prepare_display(data)
    payload = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    payload = payload.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    title = str(data["session"].get("name") or "性能采集报告")
    out = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="' + esc(REPORT_CSP) + '"><title>' + esc(title) + ' · 性能分析报告</title><style>' + CSS + '</style></head><body><main><header><div class="eyebrow">SYSMONITOR · OFFLINE PERFORMANCE</div><h1>性能分析报告</h1><p class="subtitle">' + esc(title) + '</p><p class="note">进程主口径：单核 CPU · 固定 28.75 KDMIPS/核 · RSS MB · IO KB/周期</p></header><nav>']
    out.extend('<a href="#' + esc(s["id"]) + '">' + esc(time_label(s)) + '</a>' for s in data["systems"])
    out.append('<a href="#method">统计口径</a></nav><noscript><p class="note">JavaScript 已禁用：静态统计、排行榜、Excel逐项表和全部进程仍可阅读；图表与交互不可用。</p></noscript>')
    out.extend(segment_html(data, system) for system in data["systems"])
    if not data["systems"]:
        out.append('<section class="panel empty">没有可用采集段，未生成推测数据。</section>')
    out.append('<details class="panel" id="method"><summary>统计口径与数据来源</summary><dl class="method-list">')
    for key, value in data.get("method", {}).items():
        out.append('<dt>' + esc(key) + '</dt><dd>' + esc(value) + '</dd>')
    out.append('</dl><h3>Excel 配置来源</h3><pre>' + esc(json.dumps(data.get("required_source", {}), ensure_ascii=False, indent=2)) + '</pre></details><footer>离线自包含报告 · 图表仅用于观察趋势，精确统计使用全量有效数据</footer></main><script type="application/json" id="report-data">' + payload + '</script><script>' + JS + '</script></body></html>')
    return ''.join(out)

