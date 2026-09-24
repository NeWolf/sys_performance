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
    return timestamp(value, local=True)


def prepare_display(data):
    """Use full-sample single-core CPU statistics without changing raw data."""
    from copy import deepcopy
    from perf_report_data import SYSTEM_CPU_SINGLE_FIELDS
    data = deepcopy(data)
    if data.get("display_cpu_basis") == "system-and-process-single-core":
        return data
    data["display_cpu_basis"] = "system-and-process-single-core"
    memory_field = ("mem_free_mb" if data["session"].get("summary", {}).get("source_format") == "top"
                    else "mem_avail_mb")
    labels = {"cpu_total": "CPU 总占用", "cpu_user": "用户态占用", "cpu_sys": "内核态占用",
              "cpu_iow": "iowait 占用", "cpu_irq": "irq+softirq 占用", "cpu_idle": "空闲",
              "mem_total_mb": "总内存", memory_field: "空闲内存", "mem_used_mb": "已使用内存"}
    for system in data["systems"]:
        system["metrics"] = {field: dict(system["metrics"][SYSTEM_CPU_SINGLE_FIELDS.get(field, field)], label=label)
                             for field, label in labels.items()}
        for point in system["series"]["points"]:
            for field, source in SYSTEM_CPU_SINGLE_FIELDS.items():
                point[field] = point.get(source)
                point["_run_" + field] = point.get("_run_" + source)
    data["system_fields"] = list(labels)
    data["method"]["cpu"] = "整机与进程 CPU 均采用单核口径，一个核心满载100%，整机总占用可超过100%。整机逐样本按平台满载值换算，无法换算留空。进程仍使用cpu1c，缺失不回退；固定28.75 KDMIPS/核。"
    data["method"]["system"] = "每条原始S有效字段独立统计，重复S不去重；单核口径空闲=平台满载值−单核口径总占用。各字段均先逐样本换算，再用全量有效样本计算P95/P99。超标判断仍按整机满载100%口径，严格大于90/95/99%，分母为有效S样本，非时间占比。缺失字段留空。"
    data["method"]["memory"] = "内存单位为MB。sysmonitor：已用＝MemTotal−MemAvailable，含不可回收部分，不把可回收cache算作已用；任一字段缺失则已用缺失。Top：已用直接取Mem行的used，空闲直接取free，沿用设备top口径，不等同于sysmonitor；free不等于MemAvailable，不用free冒充可用内存。Swap不计入物理内存。"
    data["method"]["chart_time"] = "趋势横轴显示小时:分钟:秒（本机时间，与日志时间表一致），提示保留完整日期时间；虚线为整段全量P95/P99，缩放不重算。"
    return data


def time_label(system):
    from datetime import datetime
    times = [row[1] for row in system["cycle_axis"]
             if isinstance(row[1], (int, float)) and math.isfinite(row[1])]
    if not times:
        return "开始采集时间：— · 结束采集时间：—"
    start, end = min(times), max(times)
    def formatted(value):
        try:
            return datetime.fromtimestamp(value / 1000).strftime("%Y年%m月%d日 %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return "—"
    return f"开始采集时间：{formatted(start)} · 结束采集时间：{formatted(end)}"


def metric(item, field, key="avg", active=False):
    scope = (item or {}).get("active", {}) if active else (item or {})
    return scope.get("metrics", {}).get(field, {}).get(key)


def scaled(value, divisor):
    return value / divisor if value is not None else None


def grid(headers, rows, identities=None, searchable=False, role="", search_terms=None):
    out = ['<div class="data-table" data-table="' + esc(role) + '">']
    if searchable:
        system_toggle = '<label><input type="checkbox" class="show-system-processes">显示系统进程</label>' if role == "all" else ''
        out.append('<div class="controls js-only"><label>搜索 <input type="search" class="table-search" placeholder="名称、PID、状态；点击表头排序" aria-label="搜索表格"></label>' + system_toggle + '<span class="table-count" aria-live="polite"></span></div>')
    out.append('<div class="scroll"><table><thead><tr>')
    out.extend('<th scope="col">' + esc(h) + '</th>' for h in headers)
    out.append('</tr></thead><tbody>')
    for index, row in enumerate(rows):
        identity = identities[index] if identities else None
        attrs = ' data-process="' + esc(identity) + '" tabindex="0" aria-selected="false"' if identity else ''
        if role == "all":
            name = str(row[1]).strip()
            is_system = not name.startswith('jkc') and (
                name.startswith(('android', 'com.android', '.', 'vendor',
                                 '/apex/com.android', '/system/', '/vendor/')) or
                (name.startswith('[') and name.endswith(']')) or '.' not in name)
            attrs += ' data-system-process="' + str(is_system).lower() + '"'
        if search_terms is not None:
            attrs += ' data-search="' + esc(search_terms[index]) + '"'
        out.append('<tr' + attrs + '>')
        for column, value in enumerate(row):
            if role == "all" and column == 1 and isinstance(value, str):
                value = value.lstrip()
            cls = ' class="name"' if isinstance(value, str) and len(value) > 22 else ''
            title = ' title="' + esc(value) + '"' if role == "all" and (column == 1 or headers[column] == "PID") else ''
            out.append('<td' + cls + title + ' data-sort="' + esc('' if value is None else value) + '">' + esc(number(value)) + '</td>')
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


PROCESS_HEADERS = ["序号", "原始进程名", "PID", "活跃周期", "活跃均值 %", "活跃P95 %", "活跃峰值 %", "P95K KDMIPS", "峰值K KDMIPS", "内存均值 MB", "内存 P95 MB", "内存峰值 MB", "累计读 KB", "累计写 KB"]
for _field, _label in IO_FIELDS:
    PROCESS_HEADERS += [_label + "均值 KB/周期", _label + "P95 KB/周期", _label + "峰值 KB/周期"]
    if _field not in ("rd_kb", "wr_kb"):
        PROCESS_HEADERS.append(_label + "累计 KB")
PROCESS_HEADERS += ["PID变化"]


def process_row(p, ordinal):
    p = p or {}
    cpu = lambda k, active=False: metric(p, "cpu1c", k, active)
    pid_label = ', '.join(str(v) for v in p.get("pids", [])) or None
    if p.get("pid_changes", 0) > 3:
        path = p.get("pid_path", [])
        last = ', '.join(str(v) for v in path[-1]["pids"]) if path else ""
        pid_label = f'pid{p["pid_changes"]}次变更，last：{last or "—"}'
    rows = [ordinal, p.get("name"), pid_label,
            p.get("active", {}).get("cycles"),
            *[cpu(k, True) for k in ("avg", "p95", "max")],
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


def budget_comparison(value, source, column):
    """Compare raw values only against budgets with matching units and required scenarios."""
    # IO measurements are MB/cycle, while source budgets are MB/s.
    if column in (5, 6):
        return "", ""

    def valid(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

    if source.get("D") == "Y":
        label, columns = "前台", "MNOPQRS"
    elif source.get("E") == "Y":
        label, columns = "后台", "FGHIJKL"
    else:
        return "", ""
    limit = source.get(columns[column])
    if not valid(value) or not valid(limit):
        return "", ""
    detail = label + '标准：' + number(limit)
    if value > limit:
        return "budget-high", "高于适用标准；" + detail
    if value < limit:
        return "budget-low", "低于适用标准；" + detail
    return "", ""


def required_table(data, segment):
    lookup = {p["id"]: p for p in data["processes"]}
    states = {"collected": "已匹配 / P实测", "dp_only": "DP-only / 无P", "missing": "缺失", "pending_confirmation": "候选 / 待确认"}
    headers = ["进程 / 模块 / 作用", "数据类型", "CPU P95\n%", "P95 K\nKDMIPS",
               "CPU 峰值\n%", "峰值 K\nKDMIPS", "内存峰值\nMB", "物理 IO 读\n实测峰值 MB/周期\n标准 MB/s", "物理 IO 写\n实测峰值 MB/周期\n标准 MB/s"]
    out = ['<div class="data-table" data-table="excel"><div class="controls js-only">'
           '<label>搜索 <input type="search" class="table-search" placeholder="进程、模块、作用、状态或 PID" aria-label="搜索关注进程"></label>'
           '<label><input type="checkbox" class="show-standards" checked>显示标准值</label>'
           '<span class="table-count" aria-live="polite"></span></div>'
           '<div class="scroll"><table class="focus-table"><thead><tr>']
    out.extend('<th scope="col">' + esc(h).replace('\n', '<br>') + '</th>' for h in headers)
    out.append('</tr></thead>')
    for item in data["required"]:
        status = next((s for s in item["segments"] if s["segment"] == segment), {})
        matches = status.get("process_ids", [])
        p = lookup.get(matches[0]) if matches else None
        source = EXCEL_BUDGETS[item["excel_row"]]
        state = states.get(status.get("status"), "缺失")
        search = ' '.join(str(v) for v in (item["name"], item["module"], item["business"], state,
                                          ', '.join(map(str, (p or {}).get("pids", [])))))
        out.append('<tbody class="focus-group" data-search="' + esc(search) + '">')
        measured = [metric(p, "cpu1c", "p95"), scaled(metric(p, "cpu1c", "p95"), 100 / 28.75),
                    metric(p, "cpu1c", "max"), scaled(metric(p, "cpu1c", "max"), 100 / 28.75),
                    scaled(metric(p, "rss_kb", "max"), 1024),
                    scaled(metric(p, "rd_kb", "max"), 1024), scaled(metric(p, "wr_kb", "max"), 1024)]
        # Keep one shared identity cell; the visibility control moves it between rows.
        identity = ('<strong>' + esc(item["name"]) + '</strong><span>' + esc(item["module"]) +
                    ' · ' + esc(item["business"]) + '</span><span>前台需求 ' + esc(source.get("D")) +
                    ' / 后台需求 ' + esc(source.get("E")) + '</span><span>' + esc(state) +
                    ' · Excel 行 ' + esc(item["excel_row"]) + '</span>')
        for index, (label, kind, values) in enumerate((
                ("后台标准", "standard background", [source.get(c) for c in "FGHIJKL"]),
                ("前台标准", "standard foreground", [source.get(c) for c in "MNOPQRS"]),
                ("当前实测", "measured", measured))):
            attrs = (' data-process="' + esc(p["id"]) + '" tabindex="0" aria-selected="false"'
                     if index == 2 and p else '')
            out.append('<tr class="' + kind + '"' + attrs + '>')
            if index == 0:
                out.append('<td class="focus-identity" rowspan="3">' + identity + '</td>')
            out.append('<th scope="row">' + label + '</th>')
            for column, value in enumerate(values):
                color, hint = budget_comparison(value, source, column) if index == 2 else ("", "")
                comparison = (' class="' + color + '" title="' + esc(hint) + '"') if color else ''
                out.append('<td data-sort="' + esc('' if value is None else value) + '"' + comparison + '>' + esc(number(value)) + '</td>')
            out.append('</tr>')
        out.append('</tbody>')
    return ''.join(out) + '</table></div></div>'


def chart(system, role, title, size=""):
    return '<div class="chart ' + size + '" id="' + esc(system["id"] + '-' + role) + '" data-chart="' + role + '" role="img" aria-label="' + esc(title) + '"></div>'


def segment_html(data, system):
    segment = system["segment"]
    processes = [p for p in data["processes"] if p["segment"] == segment]
    metrics = system["metrics"]
    cpu_metrics = {f: m for f, m in metrics.items() if f.startswith("cpu")}
    mem_metrics = {f: m for f, m in metrics.items() if f.startswith("mem")}
    out = ['<section class="segment" id="' + esc(system["id"]) + '" data-system="' + esc(system["id"]) + '">']
    if len(data["systems"]) > 1:
        out.append('<div class="segment-heading"><h2>采集段 ' + esc(segment) + '</h2></div>')
    out.append('<section class="panel overview"><h3>整体概览</h3>' + chart(system, "system-cpu", "单核口径整机CPU总占用、用户态、内核态、iowait、irq+softirq与空闲"))
    out.append(grid(["CPU指标", "均值", "P95", "峰值"], [[m["label"], m.get("avg"), m.get("p95"), m.get("max")] for m in cpu_metrics.values()], role="overview-cpu"))
    out.append('<h4>内存占用</h4>' + chart(system, "memory", "已使用内存、空闲内存及P95/P99"))
    out.append(grid(["内存指标", "均值", "P95", "峰值"],
                    [[m["label"], m.get("avg"),
                      m.get("p95") if field != "mem_total_mb" else None, m.get("max")]
                     for field, m in mem_metrics.items()], role="overview-memory"))
    out.append('<details class="overview-notes"><summary>查看统计口径与图表说明</summary><p class="note">单核满载为 100%，整机总占用可超过 100%；空闲 = 平台满载值 − 单核口径整机 CPU 总占用。' + esc(system.get("cpu_reference_note", "平台未知，不推定满载值。")) + '总占用曲线红色加粗。默认仅显示 CPU 总占用与已使用内存，点击图例可显示其他曲线。内存单位为 MB；sysmonitor 已使用内存 = MemTotal − MemAvailable，空闲内存指可用的 MemAvailable，已使用含不可回收部分、不把可回收 cache 算作已用。Top 已使用内存直接取 Mem 行的 used，空闲内存直接取 free，沿用设备 top 口径；free 不等于 MemAvailable，不用 free 冒充可用内存，Swap 不计入物理内存。总内存不绘图、不展示分位数。虚线为完整时段全量有效样本的 P95 / P99，缩放不重算；横轴为小时:分钟:秒（本机时间），缺失不补零。</p></details>')
    out.append('</section>')
    reference_note = '<p class="note">默认参照：系统 CPU 使用单核口径。' + esc(system.get("cpu_reference_note", "平台未知，不推定满载值。")) + '已使用内存：sysmonitor＝总内存−MemAvailable，Top＝Mem 行的 used。IO 按本周期已采集 P 进程分别汇总物理读、物理写、逻辑读、逻辑写；未采集进程不计入，各字段仅汇总有效值（可能不完整），无有效值时断线。</p>'
    out.append('<details class="panel" open><summary>全进程分析</summary><details class="report-notes"><summary>查看说明</summary><p class="note">点击行或按 Enter / 空格添加、移除叠加曲线；不改变统计。默认隐藏名称以 android、com.android、.、vendor、/apex/com.android、/system/、/vendor/ 开头、以方括号包裹或不含英文句点（.）的系统进程，但以 jkc 开头的名称不归入系统进程；可通过开关展示系统进程；筛选不移除已选曲线。列表按约6行高度展示，其余滚动查看；名称和 PID 完整展示，过长时换行。零活跃及 DP-only 身份完整保留；未启用 JavaScript 时展示全部进程。</p>' + reference_note + '</details>')
    out.append('<div class="selection-tags js-only" aria-live="polite"></div>')
    has_io = any((metric(p, field, "count") or 0) > 0
                 for p in processes for field, _ in IO_FIELDS)
    for role, title in (("overlay-cpu", "选中进程 CPU %"), ("overlay-rss", "选中进程 内存 MB"), ("overlay-io", "选中进程 IO 增量 KB/周期")):
        if role == "overlay-io" and not has_io:
            continue
        out.append('<h4>' + title + '</h4>' + chart(system, role, title))
    columns = list(range(len(PROCESS_HEADERS)))
    if not has_io:
        # IO 列从累计读开始连续排列，末尾 PID变化列仍需保留。
        columns = columns[:PROCESS_HEADERS.index("累计读 KB")] + columns[-1:]
    headers = [PROCESS_HEADERS[i] for i in columns]
    rows = [[row[i] for i in columns] for row in
            (process_row(p, n) for n, p in enumerate(processes, 1))]
    out.append(grid(headers, rows, [p["id"] for p in processes], True, "all", search_terms=[', '.join(map(str, p["pids"])) for p in processes]))
    out.append('</details>')
    out.append('<details class="panel" open><summary>关注进程</summary><details class="report-notes"><summary>查看说明</summary><p class="note">独立核对全部49项；每个进程前两行为后台标准（蓝色）、前台标准（紫色），第三行为当前实测，可通过开关隐藏标准值。标准值照录源表预算，仅比较需求为Y的场景：仅前台或仅后台需求时按对应标准，双Y时仅按前台标准比较；高于适用标准标红、低于适用标准标绿。相等或标准不足以判断时保持原色，缺失实测不着色；颜色仅表示数值对比，不判定场景达标；点击指标表头按当前实测排序，点击实测行可叠加曲线。实测为当前完整时段，日志没有前后台场景标记，不推断场景。候选与截断名称不计入实测，DP-only不等于P采集成功。实测K固定按28.75 KDMIPS/核换算；源预算K照录，不修正源表不一致。RSS峰值不等同于源表所有内存口径。IO实测取当前时段有效物理读、写周期增量各自的峰值，按1024 KB = 1 MB换算，单位MB/周期，缺失不补零；源表IO标准仍为MB/s。两者单位不同，IO不做红绿比较，不推算速率。</p></details>' + required_table(data, segment) + '</details>')
    out.append('<details class="panel" open><summary>关联进程分析</summary><details class="report-notes"><summary>查看说明</summary><p class="note">仅同段合并，按本段全部周期对齐。任一成员行或字段缺失，则对应合计缺失；全量重算最近秩分位数。不会相加分位数。可选及已选列表各显示4行，其余滚动查看。默认隐藏系统进程（与全进程分析一致：名称以 android、com.android、.、vendor、/apex/com.android、/system/、/vendor/ 开头、以方括号包裹或不含英文句点；以 jkc 开头的名称除外），可通过开关展示；搜索和隐藏不移除已选成员。</p>' + reference_note + '</details>')
    for role, title in (("merge-cpu", "合并 CPU"), ("merge-rss", "合并 内存 MB"), ("merge-io", "合并 IO 增量")):
        if role == "merge-io" and not has_io:
            continue
        out.append('<h4>' + title + '</h4>' + chart(system, role, title))
    out.append('<div class="js-only"><div class="merge-grid"><div class="merge-column"><h4>可选进程</h4><div class="controls"><label>搜索 <input class="merge-search" type="search" placeholder="名称 / PID" aria-label="搜索可合并进程"></label><label><input type="checkbox" class="merge-show-system-processes">显示系统进程</label></div><div class="candidate-list"></div></div><div class="merge-column"><h4>已选进程 <span class="merge-count">0</span></h4><button class="merge-clear" type="button">清空选择</button><div class="selected-list"></div></div></div><button class="merge-apply" type="button">计算全量合并统计</button><p class="merge-status status" aria-live="polite"></p><div class="merge-result"></div></div>')

    out.append('</details>')
    exceed = system["cpu_exceedances"]
    out.append('<details class="panel"><summary>CPU 超标分布明细 · 整机 100% 口径</summary>' + chart(system, "exceed", "CPU 超标彩色柱", "small"))
    out.append(grid(["严格阈值 · 整机 %", "样本数", "有效 S 样本占比 %"], [[t["threshold"], t["count"], t["percent"]] for t in exceed["thresholds"]]))
    limit = exceed["thresholds"][0]["threshold"]
    out.append('<details class="report-notes"><summary>查看说明</summary><p class="note">分母为有效系统CPU样本 ' + esc(exceed["valid_samples"]) + '；不是时间占比。超过 ' + esc(number(limit)) + '% 共 ' + esc(number(exceed["total"])) + ' 条，显示前 ' + esc(len(exceed["details"])) + ' 条；' + ('明细已截断。' if exceed["truncated"] else '明细未截断。') + '</p></details>')
    out.append(grid(["记录ID", "周期", "本机时间", "整机 CPU 总占用 · %", "来源", "行号"], [[r["record_id"], r["cycle"], stamp(r["ts"]), r["cpu_total"], r["source"], r["line"]] for r in exceed["details"]], searchable=True))
    out.append('</details>')
    for role, title, field, key, active in (("active-top", "活跃 CPU Top30 · 活跃均值", "cpu1c", "avg", True), ("peak-top", "CPU 峰值 Top30 · 趋势", "cpu1c", "max", False), ("rss-top", "内存均值 Top30", "rss_kb", "avg", False)):
        ranked = sorted((p for p in processes if metric(p, field, key, active) is not None), key=lambda p: (-metric(p, field, key, active), p["name"]))[:30]
        tag = "details"
        heading = '<summary>' + title + '</summary>'
        out.append('<' + tag + ' class="panel">' + heading + chart(system, role, title, "tall" if role != "peak-top" else ""))
        unit = "MB" if field == "rss_kb" else "%"
        headers = ["进程", "PID", "有效周期", *[label + " · " + unit for _, label in STATS]]
        rows = []
        for p in ranked:
            m = (p["active"] if active else p)["metrics"][field]
            rows.append([p["name"], ', '.join(map(str, p["pids"])), m["count"], *[scaled(m[k], 1024 if field == "rss_kb" else 1) for k, _ in STATS]])
        out.append(grid(headers, rows, [p["id"] for p in ranked], True, role) + '</' + tag + '>')
    out.append('<details class="panel"><summary>物理 / 逻辑 IO · 有效增量及累计</summary><details class="report-notes"><summary>查看说明</summary><p class="note">KB/周期是计数增量，不是 KB/s 或 MB/s；累计只累加有效增量，缺失不补零。选择进程查看其独立曲线，默认展示物理 IO 有效累计靠前的进程。</p></details><div class="charts">')
    for role, title in (("io-physical", "物理 IO 增量"), ("io-logical", "逻辑 IO 增量"), ("io-physical-total", "物理 IO 累计"), ("io-logical-total", "逻辑 IO 累计")):
        out.append('<div><h4>' + title + '</h4>' + chart(system, role, title) + '</div>')
    io_headers = ["进程", "PID"] + [label + suffix for _, label in IO_FIELDS for suffix in ("均值 KB/周期", "P95 KB/周期", "峰值 KB/周期", "累计 KB")]
    io_rows = [[p["name"], ', '.join(map(str, p["pids"])), *[metric(p, f, k) for f, _ in IO_FIELDS for k in ("avg", "p95", "max", "total")]] for p in processes]
    out.append('</div>' + grid(io_headers, io_rows, [p["id"] for p in processes], True, "io") + '</details>')
    return ''.join(out) + '</section>'


def render_document(data):
    data = prepare_display(data)
    payload = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
    payload = payload.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    title = str(data["session"].get("name") or "性能采集报告")
    collection_times = []
    for system in data["systems"] or [{"cycle_axis": []}]:
        label = ('<b>采集段 ' + esc(system["segment"]) + '</b>') if len(data["systems"]) > 1 else ''
        collection_times.append('<div class="collection-time">' + label + ''.join(
            '<span>' + esc(part) + '</span>' for part in time_label(system).split(' · ')) + '</div>')
    out = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="' + esc(REPORT_CSP) + '"><title>' + esc(title) + ' · 性能分析报告</title><style>' + CSS + '</style></head><body><main><header><h1>性能分析报告</h1><div class="collection-times">' + ''.join(collection_times) + '</div></header>']
    if len(data["systems"]) > 1:
        out.append('<nav aria-label="采集段导航">')
        out.extend('<a href="#' + esc(s["id"]) + '">采集段 ' + esc(s["segment"]) + '</a>' for s in data["systems"])
        out.append('</nav>')
    out.append('<noscript><p class="note">JavaScript 已禁用：静态统计、排行榜、关注进程表和全部进程仍可阅读；图表与交互不可用。</p></noscript>')
    out.extend(segment_html(data, system) for system in data["systems"])
    if not data["systems"]:
        out.append('<section class="panel empty">没有可用采集段，未生成推测数据。</section>')
    out.append('<details class="panel" id="method"><summary>统计口径与数据来源</summary><dl class="method-list">')
    for key, value in data.get("method", {}).items():
        out.append('<dt>' + esc(key) + '</dt><dd>' + esc(value) + '</dd>')
    out.append('</dl><h3>Excel 配置来源</h3><pre>' + esc(json.dumps(data.get("required_source", {}), ensure_ascii=False, indent=2)) + '</pre></details><footer>离线自包含报告 · 图表仅用于观察趋势，精确统计使用全量有效数据</footer></main><script type="application/json" id="report-data">' + payload + '</script><script>' + JS + '</script></body></html>')
    return ''.join(out)

