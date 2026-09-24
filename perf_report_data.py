"""Exact, offline report payloads; no runtime spreadsheet or persistent writes.

build_report_data(store, session_id) requires Store.connect and Store._sample.
Only connection-local SQLite temporary tables are written. Public payload version 1.
"""
import itertools
import json
import math
import posixpath
import time
from concurrent.futures import CancelledError
from collections import Counter, defaultdict

from perf_metrics import METHOD, metadata, to_kdmips, system_cpu_reference, cpu_reference_note


KDMIPS_PER_CORE = 28.75


EXCEL_SOURCE = {
    "file": "8255内部-资源分布策略.xlsx", "sheet": "Sheet1",
    "sha256": "fe8f5b0b2ef37604bcbe8f20b110365c032b8669d1a9da05ca353d1f6d3e4988",
    "rows": [[7, 37], [39, 56]],
    "columns": {"module": "A (merged cells expanded)", "business": "B", "name": "C"},
}
# Transcribed from the complete zip/XML workbook, including its truncated row 56.
_REQUIRED_ROWS = (
    (7, "车控初始化服务", "com.auto.beardrive"),
    (8, "车控通信服务", "com.auto.beardrive:remote"),
    (9, "HMI车控服务", "com.jd.jkc.carctl"),
    (10, "语音助手", "com.jd.jkc.joy.assistant"),
    (11, "语音助手后台常驻服务进程", "com.jd.jkc.joy.assistant:assistantservice"),
    (12, "语音算法", "com.iflytek.cutefly.speechclient.hmi"),
    (13, "编解码服务", "jkc_codec_service"),
    (14, "视频落盘", "oms_device"),
    (15, "摄像头底层服务，为OMS/DMS提供图像流", "jkc_camera_service"),
    (16, "模型管理", "com.jd.jkc.modelmanager"),
    (17, "系统埋点落盘", "com.jd.jkc.datacollector"),
    (18, "数据上传", "jkc_datacl"),
    (19, "系统监控", "sysmonitor"),
    (20, "SOS紧急呼叫", "com.jd.jkc.sos"),
    (21, "行程", "com.jd.jkc.trip"),
    (22, "行程（JD Push保活子进程）", "com.jd.jkc.trip:jdpush"),
    (23, "SR地图", "com.jd.jkc.sr"),
    (24, "健康监测", "com.jd.jkc.health"),
    (25, "音乐律动/可视化特效", "com.jd.musicrhythm"),
    (26, "无麦K歌", "com.jd.jkc.karaoke"),
    (27, "无麦K歌（子进程）", "com.jd.jkc.karaoke:remote"),
    (28, "音乐", "com.jd.jkc.music"),
    (29, "彩蛋", "com.jd.jkc.promotion"),
    (30, "塔罗", "com.jd.jkc.taro"),
    (31, "疗愈", "com.jd.jkc.heading"),
    (32, "商城", "com.jd.jkc.mall"),
    (33, "屏幕背光控制服务", "jkc_backlight_service"),
    (34, "硬件编解码进程(调用SoC硬解)", "media.hwcodec"),
    (35, "车机桌面Launcher", "com.android.car.carlauncher"),
    (36, "系统UI(状态栏/导航/通知)", "com.android.systemui"),
    (37, "时间同步服务", "com.jd.jkc.timesyncfinal"),
    (39, "艾拉比OTA升级管理(UCM)", "com.abupdate.vus.ucm"),
    (40, "艾拉比OTA升级客户端", "com.abupdate.ota"),
    (41, "路斯通音效调音(EQ/DSP音效)", "com.loostone.tuning"),
    (42, "Android系统核心进程(承载AMS/WMS/PMS等系统服务)", "system_server"),
    (43, "蓝牙协议栈应用", "com.android.bluetooth"),
    (44, "媒体框架核心服务(播放/录制会话管理)", "/system/bin/mediaserver"),
    (45, "Android音频服务(AudioFlinger/策略/混音)", "/system/bin/audioserver"),
    (46, "相机框架服务", "/system/bin/cameraserver"),
    (47, "显示合成器(图层合成/送显)", "/system/bin/surfaceflinger"),
    (48, "媒体解封装(容器解析、抽取音视频轨)", "media.extractor"),
    (49, "蓝牙HAL服务", "/vendor/bin/hw/android.hardware.bluetooth@1.0-service"),
    (50, "相机HAL服务", "/vendor/bin/hw/android.hardware.camera.provider@2.4-service_64"),
    (51, "系统日志守护进程", "/system/bin/logd"),
    (52, "ADB调试守护进程", "/apex/com.android.adbd/bin/adbd"),
    (53, "媒体库内容提供者", "com.android.providers.media.module"),
    (54, "音频HAL服务", "/vendor/bin/hw/android.hardware.audio.service"),
    (55, "Android软件编解码进程(CPU软解)", "media.swcodec"),
    (56, "webview服务", "com.android.webview:sandboxed_process0:org.chromium.content.app.SandboxedProcessServic"),
)
REQUIRED_PROCESSES = tuple(
    dict(excel_row=row, module="应用服务" if row <= 37 else "系统服务",
         business=business, name=name, truncated=row == 56)
    for row, business, name in _REQUIRED_ROWS
)
PROCESS_FIELDS = ("cpu", "cpu1c", "rss_kb", "rd_kb", "wr_kb", "rchar_kb", "wchar_kb")
SYSTEM_FIELDS = ("cpu_total", "cpu_user", "cpu_sys", "cpu_iow", "cpu_irq", "cpu_idle",
                 "mem_total_mb", "mem_avail_mb", "mem_free_mb", "mem_used_mb", "mem_percent")
SYSTEM_CPU_SINGLE_FIELDS = {field: "cpu_single_core" if field == "cpu_total" else field + "_single_core"
                            for field in SYSTEM_FIELDS if field.startswith("cpu")}
INCREMENTS = frozenset(PROCESS_FIELDS[3:])
FULL_CYCLE_SCHEMA = (
    "cycle", "ts", *PROCESS_FIELDS, "pids", "duplicate_pids", "p_records",
    "dp_records", "first_record_id", "last_record_id", "timestamp_conflict",
)


def _number(value):
    if type(value) not in (int, float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _sum(values):
    values = list(values)
    if not values or any(value is None for value in values):
        return None
    try:
        return _number(sum(values))
    except OverflowError:
        return None


def _percent(count, total):
    return count / total * 100 if total else None


def _metric_metadata(field):
    if field in SYSTEM_CPU_SINGLE_FIELDS.values():
        original = next(key for key, value in SYSTEM_CPU_SINGLE_FIELDS.items() if value == field)
        return dict(_metric_metadata(original), note="单核口径，逐样本按满载值换算；无法换算留空")
    if field == "cpu_idle":
        return {"label": "空闲", "unit": "%", "note": "每条 S 样本按 100 - cpu_total 计算"}
    if field == "mem_remaining_mb":
        return {"label": "剩余内存（总量−已用）", "unit": "MB", "note": "逐样本按总内存−已用内存计算；sysmonitor 对应可用内存，Top 对应其 used 的剩余量，不等同于统一的 MemFree 口径"}
    if field == "mem_used_mb":
        return {"label": "已用内存", "unit": "MB", "note": "sysmonitor：MemTotal - MemAvailable，含不可回收部分，不把可回收 cache 算作已用；Top：直接使用 Mem 行的 used，不等同于 sysmonitor 口径"}
    if field == "mem_percent":
        return {"label": "系统内存使用率", "unit": "%", "note": "已用内存 / 总内存 × 100，仅 total > 0；sysmonitor 已用为 MemTotal - MemAvailable，Top 已用为 Mem 行的 used"}
    return metadata(field)


def _insert_values(db, scope, values, active=False):
    # REAL prevents SQLite SUM integer overflow for uint64 I/O counters.
    db.executemany("INSERT INTO report_values VALUES (?,?,?,?)",
                   ((scope, field, float(value), int(active))
                    for field, value in values.items() if value is not None))


def _statistics(db, scopes, active=False):
    """One SQLite external sort across all scopes, never one full scan per name."""
    result = {scope: {field: dict(_metric_metadata(field), count=0, min=None,
                                 avg=None, p95=None, p99=None, max=None,
                                 **({"total": None} if field in INCREMENTS else {}))
                      for field in fields} for scope, fields in scopes.items()}
    where = "WHERE active=1" if active else ""
    sql = """WITH ranked AS (
        SELECT scope,field,value,
               ROW_NUMBER() OVER (PARTITION BY scope,field ORDER BY value) AS rank,
               COUNT(*) OVER (PARTITION BY scope,field) AS n
        FROM report_values %s)
        SELECT scope,field,COUNT(*),MIN(value),AVG(value),
               MAX(CASE WHEN rank=(95*n+99)/100 THEN value END),
               MAX(CASE WHEN rank=(99*n+99)/100 THEN value END),MAX(value),SUM(value)
        FROM ranked GROUP BY scope,field""" % where
    for row in db.execute(sql):
        scope, field, count, low, avg, p95, p99, high, total = row
        if scope not in result:
            continue
        metric = result[scope][field]
        metric.update(count=count, min=_number(low), avg=_number(avg),
                      p95=_number(p95), p99=_number(p99), max=_number(high))
        if field in INCREMENTS:
            metric["total"] = _number(total)
    return result


def _sample(store, points, count, fields):
    """Attach runs to the full stream BEFORE delegating to Store's sampler."""
    def marked():
        previous = None
        runs = dict.fromkeys(fields, 0)
        for index, point in enumerate(points):
            point = dict(point)
            for field in fields:
                if (previous is None or point["segment"] != previous["segment"] or
                        point["cycle"] != previous["cycle"] + 1 or
                        point.get("ts") is None or previous.get("ts") is None or
                        point["ts"] <= previous["ts"] or point.get(field) is None or
                        previous.get(field) is None):
                    runs[field] += 1
                point["_run_" + field] = runs[field]
            yield dict(id=index, segment=point["segment"], cycle=point["cycle"],
                       data=json.dumps(point, ensure_ascii=False, allow_nan=False))
            previous = point
    extra = tuple(field for field in SYSTEM_CPU_SINGLE_FIELDS.values() if field in fields)
    return store._sample(marked(), count, 600, extra_metrics=extra)


def _process_cycle(rows):
    members = defaultdict(list)
    p_count = dp_count = 0
    timestamps, ids = set(), []
    for row in rows:
        members[row["pid"]]  # DP-only members are observed, not invented P rows.
        timestamps.add(row["ts"])
        ids.append(row["id"])
        if row["kind"] == "P":
            p_count += 1
            members[row["pid"]].append(json.loads(row["data"]))
        else:
            dp_count += 1
    pids = sorted(members, key=lambda pid: (pid is None, pid or 0))
    duplicates = [pid for pid in pids if len(members[pid]) > 1]
    values = {}
    for field in PROCESS_FIELDS:
        values[field] = None if duplicates or None in members else _sum(
            _number(records[0].get(field)) if records else None
            for records in members.values())
    ts = next(iter(timestamps)) if len(timestamps) == 1 else None
    return [rows[0]["cycle"], ts, *values.values(), pids, duplicates, p_count,
            dp_count, min(ids), max(ids), len(timestamps) != 1], values


def _required(processes, segments):
    by_segment = defaultdict(dict)
    for process in processes:
        by_segment[process["segment"]][process["name"]] = process
    required = []
    for entry in REQUIRED_PROCESSES:
        item = dict(entry, matches=[], candidates=[], segments=[])
        target = entry["name"]
        for segment in segments:
            names = by_segment[segment]
            matches, candidates, mode = [], [], None
            if entry["truncated"]:
                candidates = [p for name, p in names.items() if isinstance(name, str)
                              and "webview" in name and len(name) >= 15
                              and (name.startswith(target) or target.startswith(name))]
                mode = "candidate" if candidates else None
            elif target in names:
                matches, mode = [names[target]], "exact"
            else:
                base = posixpath.basename(target) if target.startswith("/") else target
                candidates = [p for name, p in names.items() if isinstance(name, str)
                              and (target.startswith("/") or name.startswith("/"))
                              and (posixpath.basename(name) if name.startswith("/") else name) == base]
                owners = [e for e in REQUIRED_PROCESSES if not e["truncated"]
                          and (posixpath.basename(e["name"]) if e["name"].startswith("/") else e["name"]) == base]
                # Exact ownership is never stolen by another row's basename alias.
                occupied = {e["name"] for e in REQUIRED_PROCESSES if e["name"] in names}
                if len(candidates) == len(owners) == 1 and candidates[0]["name"] not in occupied:
                    matches, candidates, mode = candidates, [], "basename"
                elif candidates:
                    mode = "ambiguous"
            status = ("collected" if any(p["p_records"] for p in matches) else
                      "dp_only" if matches else "pending_confirmation" if candidates else "missing")
            matched_ids = [p["id"] for p in matches]
            candidate_ids = [p["id"] for p in candidates]
            item["segments"].append(dict(segment=segment, status=status, match=mode,
                                         process_ids=matched_ids, candidate_ids=candidate_ids))
            item["matches"].extend(matched_ids)
            item["candidates"].extend(candidate_ids)
            for process in matches:
                process["required_rows"].append(entry["excel_row"])
        states = {s["status"] for s in item["segments"]}
        item["status"] = next((s for s in ("collected", "dp_only", "pending_confirmation") if s in states), "missing")
        item["missing"] = not bool(item["matches"])
        required.append(item)
    return required


def _prepare(db, session_id):
    # One session scan; indices/sorts live only in this connection's temp database.
    db.execute("CREATE TEMP TABLE report_source AS SELECT id,segment,cycle,ts,kind,pid,name,source,line,data FROM records WHERE session=?", (session_id,))
    db.execute("CREATE INDEX temp.report_identity ON report_source(kind,segment,name,cycle,id)")
    db.execute("CREATE INDEX temp.report_segment_cycle ON report_source(segment,cycle,id)")
    db.execute("CREATE TEMP TABLE report_values(scope TEXT,field TEXT,value REAL,active INTEGER)")
    db.execute("CREATE TEMP TABLE report_points(scope TEXT,id INTEGER,segment INTEGER,cycle INTEGER,data TEXT)")
    db.execute("CREATE INDEX temp.report_point_scope ON report_points(scope,id)")


def _progress_reporter(progress=None, cancelled=None):
    last_stage, last_sent = None, 0

    def report(stage, completed=None, total=None):
        nonlocal last_stage, last_sent
        if cancelled is not None and cancelled():
            raise CancelledError()
        now = time.monotonic()
        if progress is not None and (stage != last_stage or now - last_sent >= 0.2 or
                                     (total is not None and completed == total)):
            progress(dict(stage=stage, completed=completed, total=total))
            last_stage, last_sent = stage, now
    return report


def build_report_data(store, session_id, progress=None, cancelled=None):
    """Return JSON-safe session/systems/processes/required/method, schema_version=1.

    Statistics use all valid samples, charts are bounded, and full_cycles are not.
    See returned full_cycle_schema and method.merge for offline name selection.
    Record id ranges are scope-filtered locators in the source session, not an
    assertion that every intervening id belongs to that process.
    """
    report = _progress_reporter(progress, cancelled)
    report("prepare")
    with store.connect() as db:
        if cancelled is not None:
            db.set_progress_handler(lambda: int(cancelled()), 10000)
        db.execute("PRAGMA temp_store=FILE")
        db.execute("BEGIN")  # Read-consistent session and source snapshot.
        raw = db.execute("SELECT id,name,created,summary FROM sessions WHERE id=?", (session_id,)).fetchone()
        if raw is None:
            raise KeyError(session_id)
        session = dict(raw, summary=json.loads(raw["summary"]))
        _prepare(db, session_id)
        totals = dict(db.execute("SELECT kind,COUNT(*) FROM report_source GROUP BY kind"))
        completed = {"process": 0, "system": 0}
        totals = {"process": totals.get("P", 0) + totals.get("DP", 0), "system": totals.get("S", 0)}

        def advance(stage, count):
            completed[stage] += count
            report(stage, completed[stage], totals[stage])

        report("timeline")
        cycle_counts = dict(db.execute("SELECT segment,COUNT(DISTINCT cycle) FROM report_source GROUP BY segment ORDER BY segment"))
        processes, systems, scopes = [], [], {}
        report("process", 0, totals["process"])
        cursor = db.execute("SELECT * FROM report_source WHERE kind IN ('P','DP') ORDER BY segment,name,cycle,id")
        for (segment, name), rows in itertools.groupby(cursor, key=lambda r: (r["segment"], r["name"])):
            process = _build_process(db, store, len(processes), segment, name, rows, cycle_counts[segment], advance)
            processes.append(process)
            scopes[process["id"]] = PROCESS_FIELDS
        report("system", 0, totals["system"])
        for segment, cycles in cycle_counts.items():
            system = _build_system(db, store, segment, cycles, advance)
            systems.append(system)
            scopes[system["id"]] = (*SYSTEM_FIELDS, *SYSTEM_CPU_SINGLE_FIELDS.values())
        report("statistics")
        statistics = _statistics(db, scopes)
        report("active_statistics")
        active = _statistics(db, {p["id"]: PROCESS_FIELDS for p in processes}, active=True)
        report("assemble")
        for item in systems + processes:
            item["metrics"] = statistics[item["id"]]
        for process in processes:
            process["active"]["metrics"] = active[process["id"]]
            for metrics in (process["metrics"], process["active"]["metrics"]):
                cpu = metrics["cpu1c"]
                cpu["p95k"] = to_kdmips(cpu["p95"], KDMIPS_PER_CORE)
                cpu["peakk"] = to_kdmips(cpu["max"], KDMIPS_PER_CORE)
            process["coverage"]["metrics"] = {
                field: dict(valid_cycles=m["count"], percent=_percent(m["count"], process["coverage"]["cycles"]))
                for field, m in process["metrics"].items()}
        required = _required(processes, cycle_counts)
        return _payload(session, systems, processes, required)


def _build_process(db, store, index, segment, name, rows, cycles, advance=None):
    scope = "p" + str(index)
    full, path, all_pids = [], [], set()
    p_records = dp_records = duplicate_cycles = p_cycles = active_cycles = 0
    concurrent_cycles = complete_cycles = max_members = 0
    for cycle, group in itertools.groupby(rows, key=lambda r: r["cycle"]):
        row, values = _process_cycle(list(group))
        full.append(row)
        pids, duplicates, p_count, dp_count = row[9:13]
        if advance is not None:
            advance("process", p_count + dp_count)
        all_pids.update(pids)
        p_records += p_count
        dp_records += dp_count
        p_cycles += bool(p_count)
        duplicate_cycles += bool(duplicates)
        concurrent_cycles += len(pids) > 1
        max_members = max(max_members, len(pids))
        complete_cycles += all(v is not None for v in values.values())
        is_active = values["cpu1c"] is not None and values["cpu1c"] > 0
        active_cycles += is_active
        _insert_values(db, scope, values, is_active)
        if not path or path[-1]["pids"] != pids:
            path.append(dict(cycle=cycle, ts=row[1], pids=pids))
    def points():
        for row in full:
            yield dict(segment=segment, cycle=row[0], ts=row[1],
                       **dict(zip(PROCESS_FIELDS, row[2:9])))
    return dict(
        id=scope, segment=segment, name=name, required_rows=[],
        pids=sorted(all_pids, key=lambda pid: (pid is None, pid or 0)),
        pid_path=path, pid_changes=max(0, len(path) - 1),
        concurrent=max_members > 1, concurrent_cycles=concurrent_cycles,
        max_concurrent_pids=max_members, p_records=p_records, dp_records=dp_records,
        dp_only=p_records == 0, full_cycles=full,
        coverage=dict(cycles=cycles, observed_cycles=len(full), p_cycles=p_cycles,
                      missing_cycles=cycles - len(full), missing_p_cycles=cycles - p_cycles,
                      duplicate_cycles=duplicate_cycles, complete_cycles=complete_cycles,
                      observed_percent=_percent(len(full), cycles),
                      p_percent=_percent(p_cycles, cycles), complete_percent=_percent(complete_cycles, cycles)),
        active=dict(condition="valid merged cpu1c > 0", cycles=active_cycles,
                    percent_of_segment=_percent(active_cycles, cycles),
                    note="仅完整有效单核 CPU > 0 的观察周期；非存活时长或前后台场景"),
        series=_sample(store, points(), len(full), PROCESS_FIELDS),
    )


def _build_system(db, store, segment, cycles, advance=None):
    scope = "s" + str(segment)
    counts = Counter()
    details, samples, cpu_valid = [], 0, 0
    profiles = set()
    cursor = db.execute("SELECT * FROM report_source WHERE kind='S' AND segment IS ? ORDER BY cycle,id", (segment,))
    for row in cursor:
        data = json.loads(row["data"])
        values = {field: _number(data.get(field)) for field in SYSTEM_FIELDS[:-1]}
        total, available = values["mem_total_mb"], values["mem_avail_mb"]
        if data.get("source_format") == "top":
            used = values["mem_used_mb"]
        else:
            used = total - available if total is not None and available is not None else None
        values["mem_used_mb"] = used
        cpu = values["cpu_total"]
        values["cpu_idle"] = 100 - cpu if cpu is not None else None
        values["mem_percent"] = _number(used / total * 100) if used is not None and total is not None and total > 0 else None
        reference = system_cpu_reference(data)
        capacity = reference["cpu_capacity"]
        for field, target in SYSTEM_CPU_SINGLE_FIELDS.items():
            value = values[field]
            values[target] = (reference["cpu_single_core"] if field == "cpu_total" else
                              _number(value * capacity / 100)
                              if value is not None and capacity is not None else None)
        _insert_values(db, scope, values)
        samples += 1
        if advance is not None:
            advance("system", 1)
        if cpu is not None:
            cpu_valid += 1
            for threshold in (90, 95, 99):
                counts[threshold] += cpu > threshold
            if cpu > 90 and len(details) < 200:
                details.append(dict(record_id=row["id"], cycle=row["cycle"], ts=row["ts"],
                                    source=row["source"], line=row["line"], cpu_total=cpu))
        profiles.add((reference["cpu_platform"], reference["cpu_capacity"]))
        point = dict(ts=row["ts"], record_id=row["id"], **(values | reference))
        db.execute("INSERT INTO report_points VALUES (?,?,?,?,?)",
                   (scope, samples, segment, row["cycle"], json.dumps(point, allow_nan=False)))
    def points():
        for row in db.execute("SELECT * FROM report_points WHERE scope=? ORDER BY id", (scope,)):
            yield dict(json.loads(row["data"]), segment=row["segment"], cycle=row["cycle"])
    cycle_axis = [list(row) for row in db.execute(
        "SELECT cycle,CASE WHEN MIN(ts)=MAX(ts) THEN MIN(ts) END FROM report_source WHERE segment IS ? GROUP BY cycle ORDER BY cycle", (segment,))]
    return dict(id=scope, segment=segment, cycles=cycles, samples=samples,
                cycle_axis=cycle_axis,
                series=_sample(store, points(), samples, (*SYSTEM_FIELDS, *SYSTEM_CPU_SINGLE_FIELDS.values())),
                cpu_reference_note=cpu_reference_note(profiles),
                cpu_exceedances=dict(valid_samples=cpu_valid,
                    thresholds=[dict(threshold=t, count=counts[t], percent=_percent(counts[t], cpu_valid))
                                for t in (90, 95, 99)],
                    details=details, total=counts[90], limit=200, truncated=counts[90] > len(details)))


def _payload(session, systems, processes, required):
    return dict(
        schema_version=1, session=session, systems=systems, processes=processes,
        required=required, required_source=dict(EXCEL_SOURCE),
        full_cycle_schema=list(FULL_CYCLE_SCHEMA), cycle_axis_schema=["cycle", "ts"],
        process_fields=list(PROCESS_FIELDS), system_fields=list(SYSTEM_FIELDS),
        cpu_basis=dict(primary="cpu1c", reference="cpu", kdmips_per_core=KDMIPS_PER_CORE),
        method=dict(
            cpu="进程以单核 cpu1c 为主，可超过100%；整机 cpu / cpu_total 仅参考，不替代单核缺值。P95K=单核P95/100×28.75，峰值K=单核峰值/100×28.75，单位KDMIPS；固定系数派生估算，非独立实测。",
            statistics=METHOD,
            process="同 segment+原始 name 分组；同 cycle 不同 PID 先求和，再对有效周期统计。",
            missing="仅观察到的成员参与；当周期任一 P/DP 成员缺 P 或字段缺失，该字段为 null。缺周期不补零；不能证明未观察成员不存在。",
            duplicates="同 cycle 同 PID/name 多条 P：整个名称该周期全部指标为 null，duplicate_pids 保留；DP 不作为 P 数值来源。",
            coverage="cycles 是该段全部记录类型的去重周期数；complete_cycles 要求七字段全有效。P 覆盖率不等于字段有效覆盖率。",
            active="对同名同周期合计后有效单核 cpu1c > 0 的周期另算全部指标；不是单 PID 活跃度，不代表实际运行时长。",
            pid="pid_path 按已观察周期列出 PID 集合变化（含 DP）；pid_changes 不跨段，不推断缺周期的退出/重启及同 PID 复用。",
            system="每条原始 S 有效字段独立统计，重复 S 不去重；超标严格大于 90/95/99，分母是有效 cpu_total 样本数，非时间占比。没有系统任务总量。",
            charts="每系列最多600点；全部原始点先写 _run_字段。仅同段同run且字段非null可连接；run已覆盖缺值、缺周期、非递增ts。不得用采样点计算精确统计或合并。",
            merge="使用 full_cycles（schema见 full_cycle_schema），仅选择同 segment 的不同原始 name；按该段全部 cycle 对齐。缺任一选中名称的行或字段则对应合计为null，否则求和；对全量合计有效周期重算最近秩统计。不要相加各进程p95/p99，严禁跨段求和。",
            provenance="每条 full_cycles 附 first_record_id/last_record_id，结合 session+segment+name+cycle 查询 records 可追溯所有 P/DP source/line；范围可能穿插其他记录。系统明细直接附 source/line。",
            matching="Excel原名逐字优先；仅绝对路径可退化basename，歧义仅列候选。WebView截断行始终待确认。required按每段标记，无采集为missing；dp_only不是P采集成功。",
            precision="统计临时表使用SQLite REAL；大整数可能有IEEE754舍入。完整周期数组不降采样，输出体积随名称周期数增长。",
        ),
    )



