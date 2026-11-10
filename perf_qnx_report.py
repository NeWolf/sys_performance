"""QNX-only offline interactive report with exact statistics and bounded plots."""
import base64
import hashlib
import json
import sqlite3
from concurrent.futures import CancelledError
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from perf_qnx_parser import REQUIRED_METRICS
from perf_report_data import _progress_reporter
ASSETS = Path(__file__).resolve().parent / "report_assets"
JS = (ASSETS / "vendor/echarts.min.js").read_text(encoding="utf-8") + "\n;\n" + (ASSETS / "qnx_report.js").read_text(encoding="utf-8")
REPORT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-" +
              base64.b64encode(hashlib.sha256(JS.encode()).digest()).decode() +
              "'; base-uri 'none'; form-action 'none'")
DISPLAY_TZ = timezone(timedelta(hours=8))

MAX_SERIES = 2000
MAX_POINTS = 600
MAX_TOTAL_POINTS = 60000
CSS = """
:root{color-scheme:light;font-family:system-ui,sans-serif;color:#203047;background:#f2f5fa}
body{margin:0}main{max-width:1180px;margin:auto;padding:24px}header,section{background:white;
border:1px solid #dbe3ef;border-radius:12px;padding:22px;margin-bottom:18px}
h1{margin-top:0}h2{font-size:20px;overflow-wrap:anywhere}p,li{line-height:1.7}
nav{display:flex;flex-wrap:wrap;gap:16px;margin:20px 0}a{color:#1558b0}
.warning{background:#fff4d8;padding:14px;border-radius:8px}.muted{color:#52647a}
table{width:100%;border-collapse:collapse;table-layout:fixed}td,th{text-align:left;
padding:10px 6px;border-bottom:1px solid #e1e7ee;overflow-wrap:anywhere}
svg{width:100%;height:auto;display:block}svg text{font-size:12px;fill:#52647a}
.axis{stroke:#acb9cb;stroke-width:1}.point{fill:#2563b9;opacity:.72}.missing{stroke:#bb7000}
.compact section{padding:14px}.compact main{max-width:1040px}
.chart-panel{position:relative;padding-top:42px;display:grid}.qnx-chart{height:360px;width:100%;grid-area:1/1}
.chart-panel button{position:absolute;top:8px;right:0;border:1px solid #bfcee2;border-radius:6px;
 background:#f2f6fc;color:#1558b0;padding:6px 12px;cursor:pointer}
.chart-fallback{grid-area:1/1;align-self:start;z-index:1;background:white}
.chart-ready .chart-fallback{display:none}
@media print{.qnx-chart,.chart-panel button{display:none}.chart-panel{padding:0}
 .chart-fallback,.chart-ready .chart-fallback{display:block;position:static}}
@media(max-width:600px){main{padding:10px}section,header{padding:12px}td,th{font-size:12px}}
@media print{body{background:white}section{break-inside:avoid}nav{display:none}}
"""
CACHE_VERSION = "qnx-4-load-analysis:" + hashlib.sha256((CSS + REPORT_CSP).encode()).hexdigest()
NOTES = [
    "CPU totalcpu 已是 8 核平均，保持原值，不再除以 8，不套用 Android CPU 口径。",
    "GPU utilization 单独展示；gpubusbusy 是总线忙碌率，不能替代 GPU 利用率。",
    "DDR 与 LLCC 是平台级观测，各自展示，不与 DSP 或其他模块带宽相加。",
    "UTC 时间仅粗对齐；保留 qnx_relative_s，无精确同步保证，不强凑周期、不插值、不补零。",
    "统计使用每个时间分段内该模块/指标/原单位的全量有效样本；P95 使用 nearest-rank。",
    "时间默认显示北京时间（UTC+8），原始 UTC 时间戳及 qnx_relative_s 不变；极端日期超出范围时保留 UTC。",
    "折线为有界均匀抽样并保留极值；全流空值、时间不递增、相对时间回退或源文件切换均断线，不跨段或单位连接。",
    "折线只辅助观察趋势，不表示插值或连续采集；橙色标记仅表示抽样到的显式空值，不代表零。",
    "拖动底部滑块或在图内滚轮缩放，悬浮查看原值和源行；缩放不改变全量统计，重置按钮恢复完整时间范围。",
]


def build_report_data(store, session_id, progress=None, cancelled=None):
    report = _progress_reporter(progress, cancelled)
    report("prepare")
    try:
        with store.connect() as db:
            if cancelled is not None:
                db.set_progress_handler(lambda: int(cancelled()), 10000)
            db.execute("PRAGMA temp_store=FILE")
            db.execute("BEGIN")
            raw = db.execute("SELECT id,name,created,summary FROM sessions WHERE id=?", (session_id,)).fetchone()
            if raw is None:
                raise KeyError(session_id)
            session = dict(raw, summary=json.loads(raw["summary"]))
            if session["summary"].get("source_format") != "qnx":
                raise ValueError("QNX 报告仅支持 QNX 会话")
            report("timeline")
            db.execute("""CREATE TEMP TABLE qnx_source AS SELECT id,segment,ts,source,line,
                json_extract(data,'$.module') module, json_extract(data,'$.metric') metric,
                json_extract(data,'$.unit') unit, json_extract(data,'$.value') value,
                json_extract(data,'$.qnx_relative_s') qnx_relative_s
                FROM records WHERE session=? AND kind IN ('QC','QG','QDSP','QDDR')""", (session_id,))
            db.execute("CREATE INDEX temp.qnx_time ON qnx_source(segment,module,metric,unit,id)")
            db.execute("CREATE INDEX temp.qnx_value ON qnx_source(segment,module,metric,unit,value)")
            segments = [dict(row) for row in db.execute("""SELECT segment,MIN(ts) start,MAX(ts) end,
                COUNT(*) records FROM qnx_source GROUP BY segment ORDER BY segment""")]
            report("statistics")
            groups = db.execute("""SELECT segment,module,metric,unit,COUNT(*) samples,
                COUNT(value) valid_samples, MIN(value) min,MAX(value) max, MIN(ts) start,MAX(ts) end
                FROM qnx_source GROUP BY segment,module,metric,unit
                ORDER BY segment,module,metric,unit LIMIT ?""", (MAX_SERIES + 1,)).fetchall()
            if len(groups) > MAX_SERIES:
                raise ValueError("QNX 独立时间序列超过 2000，请按文件或时间段拆分导入")
            limit = min(MAX_POINTS, MAX_TOTAL_POINTS // max(1, len(groups)))
            series = []
            present = set()
            scope = "segment=? AND module=? AND metric=? AND unit=?"
            for index, row in enumerate(groups):
                report("statistics", index, len(groups))
                item = dict(row)
                args = tuple(item[key] for key in ("segment", "module", "metric", "unit"))
                count, peak = item["valid_samples"], item["max"]
                item["missing_samples"] = item["samples"] - count
                item["avg"] = item["p95"] = None
                if count:
                    # Scale before summation so finite input near DBL_MAX cannot overflow AVG.
                    item["avg"] = (db.execute("SELECT AVG(value / ?) FROM qnx_source WHERE " + scope,
                                               (peak, *args)).fetchone()[0] * peak) if peak else 0.0
                    rank = (95 * count + 99) // 100
                    item["p95"] = db.execute("SELECT value FROM qnx_source WHERE " + scope +
                        " AND value IS NOT NULL ORDER BY value LIMIT 1 OFFSET ?", (*args, rank - 1)).fetchone()[0]
                    if item["metric"] == REQUIRED_METRICS[item["module"]]:
                        present.add((item["segment"], item["module"]))
                # Uniform sampling includes explicit NULLs; reserve two slots for min/max.
                slots = max(2, limit - 2)
                selected = {round(i * (item["samples"] - 1) / (slots - 1)) for i in range(slots)}
                points, extremes = {}, {}
                cursor = db.execute("SELECT * FROM qnx_source WHERE " + scope + " ORDER BY id", args)
                run, previous = 0, None
                for number, raw_point in enumerate(cursor):
                    point = dict(raw_point)
                    # Mark continuity BEFORE sampling; omitted NULLs still break the line.
                    if (previous is None or point["value"] is None or previous["value"] is None
                            or point["ts"] <= previous["ts"] or point["source"] != previous["source"]
                            or (point["qnx_relative_s"] is not None and previous["qnx_relative_s"] is not None
                                and point["qnx_relative_s"] < previous["qnx_relative_s"])):
                        run += 1
                    point["run"] = run
                    previous = point
                    if number % 1024 == 0:
                        report("series", index, len(groups))
                    value = point["value"]
                    if number in selected:
                        points[number] = dict(point)
                    if value is not None:
                        for key in ("min", "max"):
                            if value == item[key] and key not in extremes:
                                extremes[key] = (number, dict(point))
                points.update(extremes.values())
                item["points"] = [points[key] for key in sorted(points)]
                item["plotted_samples"] = len(item["points"])
                item["peak_point"] = extremes["max"][1] if count else None
                item["valid_ratio_pct"] = 100 * count / item["samples"]
                series.append(item)
            missing = [dict(segment=seg["segment"], module=module, metric=metric)
                       for seg in segments for module, metric in REQUIRED_METRICS.items()
                       if (seg["segment"], module) not in present]
            report("assemble")
            return dict(schema_version=1, source_format="qnx", session=session, segments=segments,
                        series=series, missing_modules=missing, notes=NOTES,
                        limits=dict(per_series=MAX_POINTS, total_points=MAX_TOTAL_POINTS, series=MAX_SERIES))
    except sqlite3.OperationalError:
        if cancelled is not None and cancelled():
            raise CancelledError() from None
        raise


def _esc(value):
    return escape(str(value), quote=True)


def _fmt(value):
    if value is None:
        return "—"
    return format(value, ".8g") if isinstance(value, float) else str(value)


def _time(ts):
    seconds, milliseconds = divmod(ts, 1000)
    utc = datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=milliseconds * 1000)
    try:
        return utc.astimezone(DISPLAY_TZ).isoformat(timespec="milliseconds")
    except OverflowError:
        return utc.isoformat(timespec="milliseconds") + "（超出北京时间可表示范围，保留 UTC）"


def _chart(item, report):
    start, end, peak = item["start"], item["end"], item["max"] or 1
    parts = ['<svg viewBox="0 0 1040 260" role="img" aria-label="原单位指标散点图">',
             '<line class="axis" x1="90" y1="20" x2="90" y2="210"/>',
             '<line class="axis" x1="90" y1="210" x2="1020" y2="210"/>',
             f'<text x="8" y="24">{_esc(_fmt(item["max"]))}</text>',
             f'<text x="8" y="48">{_esc(item["unit"])}</text>',
             '<text x="65" y="212">0</text>',
             f'<text x="90" y="244">{_esc(_time(start))}</text>',
             f'<text x="1020" y="244" text-anchor="end">{_esc(_time(end))}</text>']
    for index, point in enumerate(item["points"]):
        if index % 100 == 0:
            report("render")
        x = 90 + 930 * ((point["ts"] - start) / (end - start)) if end > start else 555
        value = point["value"]
        title = _esc(f'{_time(point["ts"])} | {_fmt(value)} {item["unit"]} | '
                     f'qnx_relative_s={_fmt(point["qnx_relative_s"])} | {point["source"]}:{point["line"]}')
        if value is None:
            parts.append(f'<line class="missing" x1="{x:.2f}" x2="{x:.2f}" y1="205" y2="215"><title>{title}</title></line>')
        else:
            y = 210 - 190 * (value / peak)
            parts.append(f'<circle class="point" cx="{x:.2f}" cy="{y:.2f}" r="3"><title>{title}</title></circle>')
    parts.append('</svg>')
    return ''.join(parts)


def _metric_reference(item):
    module, unit = item["module"], item["unit"]
    meanings = {
        "cpu": "totalcpu：8 个 CPU 核负载的平均值，反映整体 CPU 忙碌程度；不是逐核百分比之和，也不是某进程负载。不再除以 8，整体均值可能掩盖单核繁忙。",
        "gpu": "utilization：厂商 GPU 利用率；v2.4.2 转换器明确输出百分比。GPU 覆盖范围需 BSP 确认，不能用 gpubusbusy 总线忙碌率替代。",
        "cdsp": "load：CDSP 实例的厂商负载指标；与 CDSP1 分开统计，不把 HVX/HMX 子单元负载相加或等同整体负载，具体关系需 BSP 确认。",
        "cdsp1": "load：CDSP1 实例的厂商负载指标；与 CDSP 分开统计，不把 HVX/HMX 子单元负载相加或等同整体负载，具体关系需 BSP 确认。",
        "ddr": "ddr_bw_total：厂商 DDR 总带宽观测值，表示平台级数据传输速率；不是内存占用量，不是单个 Guest 或应用的带宽，不能与 LLCC 或 DSP 带宽相加。",
    }
    parts = [f'<p><b>字段含义：</b>{meanings[module]}</p>']
    if module != "ddr" and unit == "%":
        parts.append('<p><b>参考值：</b>百分比标尺通常为 0–100%；0% 为该指标观测零负载，'
                     '100% 为标尺上端（CPU 表示 8 核平均满负载）。越接近上端，观测忙碌程度越高；'
                     '不预设“低/中/高”经验分档。超过 100% 时提示口径或数据需核查，不截断原值。'
                     '这不是业务准入阈值，也不等于算力容量；GPU/DSP 的分母和覆盖范围以 BSP 为准。</p>')
    elif module == "ddr" and unit in ("MB/s", "GB/s", "KB/s", "B/s", "MiB/s", "GiB/s", "KiB/s"):
        parts.append(f'<p><b>参考值：</b>0 {_esc(unit)} 表示观测带宽为零；值越大表示传输速率越高。'
                     '保留原单位，不混用十进制 MB/s 与二进制 MiB/s。上限需结合 DDR 通道、位宽、'
                     '数据率和实测可持续带宽；当前未配置，不能给出通用安全值或计算带宽饱和度。'
                     '应与同平台、同单位、同业务场景的基线比较。</p>')
    else:
        reason = '源日志未标注单位，保留 reported' if unit == "reported" else f'当前单位 {_esc(unit)} 未匹配该指标的已知参考口径'
        parts.append(f'<p class="warning"><b>参考值：</b>{reason}；不换算为百分比或带宽利用率。'
                     '需核实源日志/BSP 的量纲后再配置参考值；目前仅比较同段同单位的观测数值。</p>')
    return ''.join(parts)


def _current_load_analysis(item):
    unit = _esc(item["unit"])
    low, peak, avg, p95 = (item[key] for key in ("min", "max", "avg", "p95"))
    parts = ['<p><b>当前负载分析（本次采集全量有效样本，非实时）：</b>']
    if item["module"] != "ddr" and item["unit"] == "%":
        if peak > 100:
            parts.append('存在超过百分比通常标尺 100% 的观测值，需核查源数据、单位和 BSP 口径；'
                         '暂停相对百分比标尺的负载解读，不据此判断超标或真实过载。')
        else:
            parts.append(f'平均负载 {_esc(_fmt(avg))}%，P95 为 {_esc(_fmt(p95))}%，'
                         f'观测峰值 {_esc(_fmt(peak))}%；P95 距 100% 标尺上端 '
                         f'{_esc(_fmt(100 - p95))} 个百分点。'
                         '此差值仅用于读数，不是可用算力余量；整体指标未到 100% 也不能排除局部瓶颈。')
    elif item["module"] == "ddr":
        parts.append(f'平均带宽读数 {_esc(_fmt(avg))} {unit}，P95 为 {_esc(_fmt(p95))} {unit}；'
                     '缺少平台容量及场景基线，无法判定带宽偏高、偏低或饱和。')
    else:
        parts.append('单位口径尚未确认，只解读原值分布，不划分负载等级。')
    if peak > 0 and item["valid_samples"] > 1 and low != peak:
        parts.append(f' 均值为本次观测峰值的 {avg / peak * 100:.2f}%，'
                     f'P95 为本次观测峰值的 {p95 / peak * 100:.2f}%；'
                     f'峰均差为 {_esc(_fmt(peak - avg))} {"个百分点" if item["unit"] == "%" else unit}。'
                     '这些比例仅比较样本分布，不是硬件利用率。')
        if p95 < peak:
            parts.append(' 峰值高于 P95，优先核对上尾样本及峰值附近业务；不能据此认定突发或持续时长。')
        else:
            parts.append(' P95 与峰值相同，上尾分位已触及观测最大值；小样本也可出现此现象，不代表持续满载。')
    parts.append('</p>')
    return ''.join(parts)


def _analysis_overview(data, report):
    labels = {"cpu": "CPU 整体负载", "gpu": "GPU 利用率", "cdsp": "CDSP 负载",
              "cdsp1": "CDSP1 负载", "ddr": "DDR 总带宽"}
    parts = ['<section id="analysis-overview"><h2>关键负载分析总览</h2>',
             '<p>按时间分段和原单位分别分析，不跨段合并。均值与 P95 基于全量有效样本，'
             '不是时间加权统计；峰值只表示观测最大值，不代表硬件容量。</p>',
             '<p class="warning">尚未配置平台容量和业务阈值，不判定达标、超标或瓶颈。'
             '仅凭这些指标不能归因到具体进程，也不能推断内存泄漏、温度或降频原因。</p>']
    fields = (
        ('模块 / 指标 / 原单位', '标识测量对象与量纲；只有同一分段、模块、指标、单位的记录参与同一组统计。',
         '各指标参考见下文；reported 表示单位未知，不自动换算。'),
        ('分段 / 记录时间范围', '分段为导入时划分的时间段编号；范围为本序列已导入记录的最早至最晚时间（含显式空值）。',
         '编号无负载含义；范围跨度不等于有效采集时长，不跨段合并。'),
        ('有效记录 / 总记录 / 有效比例', '有效记录为数值非空的记录（含零）；总记录包含显式空值；有效比例＝有效记录÷总记录。',
         '已导入记录的完整性参考为 100%；不是采集覆盖率，不能发现未记录的时间点或替代解析错误诊断。'),
        ('显式空值数', '已导入但数值为空的记录数量；不参与均值、分位及极值统计，不按零处理。',
         '完整性期望为 0 条；大于 0 需核查采集诊断，并注意有效样本可能有偏。'),
        ('平均值', '全量有效样本的算术平均，描述样本整体水平；不是按采样间隔加权的时间均值。',
         '单位与原指标相同；应落在最小值与峰值之间，业务参考随模块和场景确定。'),
        ('P95', '有效值从小到大排序，取向上取整后的第 95%×有效数 个值（nearest-rank）；至少 95% 的有效样本不大于它。',
         '单位与原指标相同；小样本可能等于峰值，不代表 95% 的时间或连续高负载时长。'),
        ('最小值 / 峰值 / 样本变化范围', '全量有效样本中的最小值、最大值及两者构成的区间；不是图表抽样后的极值。',
         '按指标原单位解读；峰值不是硬件容量，不能仅凭极值认定突发或持续瓶颈。'),
        ('峰值时间 / qnx_relative_s / 源文件:行号', '峰值显示北京时间 UTC+8；qnx_relative_s 为原始 QNX 相对秒数；文件及行号定位导入记录。同峰值取记录顺序首条。',
         '仅用于定位，无负载阈值；相对时间缺失显示 —，UTC 为粗对齐，不能用于精确跨模块因果判断。'),
    )
    parts.append('<h3>字段说明与参考口径</h3><table><thead><tr><th>字段</th><th>含义 / 计算方式</th>'
                 '<th>参考值 / 解读边界</th></tr></thead><tbody>')
    for field in fields:
        parts.append('<tr>' + ''.join(f'<td>{_esc(value)}</td>' for value in field) + '</tr>')
    parts.append('</tbody></table>')
    found = False
    for index, item in enumerate(data["series"]):
        if item["metric"] != REQUIRED_METRICS[item["module"]]:
            continue
        report("render")
        found = True
        label = f'{labels[item["module"]]} · 分段 {item["segment"]} · {item["unit"]}'
        parts.append(f'<h3><a href="#series-{index}">{_esc(label)}</a></h3>')
        parts.append(_metric_reference(item))
        parts.append(f'<p>记录时间范围：{_esc(_time(item["start"]))} 至 '
                     f'{_esc(_time(item["end"]))}（含显式空值记录）。</p>')
        parts.append(f'<p>有效记录 {item["valid_samples"]} / {item["samples"]} '
                     f'（{item["valid_ratio_pct"]:.2f}%）；显式空值 {item["missing_samples"]} 条。</p>')
        if not item["valid_samples"]:
            parts.append('<p class="warning">没有有效负载数据，不能评估；空值不按零负载处理。</p>')
            continue
        if item["missing_samples"]:
            parts.append('<p class="warning">当前存在显式空值，以下结论仅代表有效记录，可能存在样本偏差；'
                         '请先核查空值附近的采集状态，不能推广到缺失时段。</p>')
        unit = _esc(item["unit"])
        parts.append(_current_load_analysis(item))
        parts.append(f'<p>最小值 <b>{_esc(_fmt(item["min"]))}</b> {unit} · 平均值 <b>{_esc(_fmt(item["avg"]))}</b> {unit} · '
                     f'P95 <b>{_esc(_fmt(item["p95"]))}</b> {unit} · '
                     f'峰值 <b>{_esc(_fmt(item["max"]))}</b> {unit}。</p>')
        peak = item["peak_point"]
        parts.append(f'<p>峰值定位：{_esc(_time(peak["ts"]))} · '
                     f'qnx_relative_s={_esc(_fmt(peak["qnx_relative_s"]))} · '
                     f'{_esc(peak["source"])}:{peak["line"]}。同峰值取记录顺序首条。</p>')
        if item["valid_samples"] == 1:
            parts.append('<p class="warning">仅一个有效样本，不能评估负载变化。</p>')
        elif item["max"] == 0:
            parts.append('<p>全部有效样本为零；仅说明已记录的观测值，不代表缺失时段空闲。</p>')
        elif item["min"] == item["max"]:
            parts.append('<p>全部有效样本数值相同；不能据此区分稳定负载与采样分辨率限制。</p>')
        else:
            parts.append(f'<p>样本变化范围：{_esc(_fmt(item["min"]))}–'
                         f'{_esc(_fmt(item["max"]))} {unit}；'
                         '需结合下方时间图核对峰值附近场景，不能由 P95 推断连续高负载时长。</p>')
    if not found:
        parts.append('<p class="warning">没有关键负载序列；频率、GPU 总线忙碌率等辅助指标不能替代负载。</p>')
    parts.extend(('<h3>数据质量与解读建议</h3>',
                  '<ul><li>有效比例只统计已导入记录，不是采集覆盖率；未记录的时间点无法据此统计。</li>',
                  '<li>CPU 负载应结合各核与频率观察；GPU 利用率与总线忙碌率分别解读；'
                  'CDSP 和 CDSP1 独立分析，频率变化本身不能证明降频。</li>',
                  '<li>DDR 和 LLCC 保持平台原始口径；缺少理论带宽时不计算饱和度，不归因到 Guest 或应用。</li>',
                  '<li>跨模块只有粗时间对齐，不计算精确同步相关性或因果关系；缺失模块不能判为空闲。</li></ul>',
                  '</section>'))
    return ''.join(parts)


def render_document(data, *, style="default", progress=None, cancelled=None):
    if style not in ("default", "compact"):
        raise ValueError("未知 QNX 报告样式")
    report = _progress_reporter(progress, cancelled)
    report("render")
    session = data["session"]
    summary = session["summary"]
    parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             f'<meta http-equiv="Content-Security-Policy" content="{_esc(REPORT_CSP)}">',
             '<title>QNX 独立性能报告</title>', f'<style>{CSS}</style></head>',
             f'<body class="{style}"><main><header><h1>QNX 独立性能报告</h1>',
             f'<p>{_esc(session["name"])}</p>',
             f'<p>独立序列：{len(data["series"])} · 时间分段：{len(data["segments"])} · '
             f'解析错误：{_esc(summary.get("errors", 0))}</p>', '<ul>']
    parts.extend(f'<li>{_esc(note)}</li>' for note in data["notes"])
    parts.append('</ul>')
    if data["missing_modules"]:
        missing = '；'.join(f'分段 {m["segment"]}：{m["module"]}/{m["metric"]}' for m in data["missing_modules"])
        parts.append(f'<p class="warning">缺少有效关键指标（未采集或全部为空）：{_esc(missing)}</p>')
    if summary.get("errors"):
        parts.append('<p class="warning">非法记录已跳过，未补零；请在导入诊断中检查源文件及行号。</p>')
    parts.append('<nav aria-label="指标导航">')
    for index, item in enumerate(data["series"]):
        label = f'{item["module"]}/{item["metric"]} ({item["unit"]}) · 分段 {item["segment"]}'
        parts.append(f'<a href="#series-{index}">{_esc(label)}</a>')
    parts.append('</nav></header>')
    parts.append(_analysis_overview(data, report))
    for index, item in enumerate(data["series"]):
        report("render", index, len(data["series"]))
        label = f'{item["module"]}/{item["metric"]} ({item["unit"]}) · 分段 {item["segment"]}'
        parts.append(f'<section id="series-{index}"><h2>{_esc(label)}</h2><table><thead><tr>')
        parts.extend(f'<th>{heading}</th>' for heading in ('有效数', '空值数', '最小值', '最大值', '平均值', 'P95'))
        parts.append('</tr></thead><tbody><tr>')
        parts.extend(f'<td>{_esc(_fmt(item[key]))}</td>' for key in ('valid_samples', 'missing_samples', 'min', 'max', 'avg', 'p95'))
        parts.append('</tr></tbody></table>')
        parts.append(f'<p class="muted">显示 {item["plotted_samples"]} / {item["samples"]} 个记录点；统计未抽样。</p>')
        if not item["valid_samples"]:
            parts.append('<p class="warning">该序列没有有效数值，统计为空。</p>')
        parts.append(f'<div class="chart-panel"><button type="button">重置缩放</button>'
                     f'<div class="qnx-chart" data-data="qnx-data-{index}" role="img" '
                     f'aria-label="{_esc(label)}，北京时间交互折线图"></div>'
                     '<div class="chart-fallback">')
        parts.extend((_chart(item, report), '</div></div>'))
        # One bounded series per inert JSON block; never concatenate log data into JS.
        payload = json.dumps(item, ensure_ascii=True, allow_nan=False, separators=(',', ':')).replace(
            '<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        parts.append(f'<script type="application/json" id="qnx-data-{index}">{payload}</script></section>')
    parts.append('<noscript><p class="warning">JavaScript 未启用：显示静态采样图和全量统计。</p></noscript>')
    parts.append('</main><script>' + JS + '</script></body></html>')
    report("render", len(data["series"]), len(data["series"]))
    return ''.join(parts)
