"""Read-only, exact two-session comparisons from original samples."""
import itertools
import json
import time
from concurrent.futures import CancelledError
from collections import defaultdict

from perf_report_data import (
    INCREMENTS, PROCESS_FIELDS, SYSTEM_FIELDS, _insert_values, _number,
    _prepare, _process_cycle, _statistics,
)


# Compare the complement of used memory without mislabelling MemAvailable as MemFree.
COMPARE_SYSTEM_FIELDS = tuple(field for field in SYSTEM_FIELDS
                              if field not in ("mem_avail_mb", "mem_free_mb")) + ("mem_remaining_mb",)


def _snapshot(store, session_id, segment=None, report=None, cancelled=None):
    report = report or (lambda *args: None)
    report("prepare")
    with store.connect() as db:
        if cancelled is not None:
            db.set_progress_handler(lambda: int(cancelled()), 10000)
        db.execute("PRAGMA temp_store=FILE")
        db.execute("BEGIN")
        raw = db.execute("SELECT id,name,created,summary FROM sessions WHERE id=?", (session_id,)).fetchone()
        if raw is None:
            raise KeyError(session_id)
        session = dict(raw, summary=json.loads(raw["summary"]))
        _prepare(db, session_id)
        if segment is not None:
            if not db.execute("SELECT 1 FROM report_source WHERE segment=? LIMIT 1", (segment,)).fetchone():
                raise ValueError("所选时段不存在")
            db.execute("DELETE FROM report_source WHERE segment<>?", (segment,))
        report("timeline")
        axes = defaultdict(list)
        for row in db.execute("SELECT segment,cycle,MIN(ts),MAX(ts) FROM report_source GROUP BY segment,cycle ORDER BY segment,cycle"):
            axes[row[0]].append(row[2] if row[2] == row[3] else None)
        intervals, duration, invalid = [], 0, 0
        for timestamps in axes.values():
            valid = [ts for ts in timestamps if ts is not None]
            duration += (max(valid) - min(valid)) / 1000 if valid else 0
            for a, b in zip(timestamps, timestamps[1:]):
                if a is not None and b is not None and b > a:
                    intervals.append((b - a) / 1000)
                else:
                    invalid += 1
        intervals.sort()
        counts = dict(db.execute("SELECT kind,COUNT(*) FROM report_source GROUP BY kind"))
        system_count = counts.get("S", 0)
        process_count = counts.get("P", 0) + counts.get("DP", 0)
        report("system", 0, system_count)
        formats = set()
        for index, row in enumerate(db.execute("SELECT data FROM report_source WHERE kind='S' ORDER BY segment,cycle,id"), 1):
            data = json.loads(row[0])
            source = data.get("source_format", "sysmonitor")
            formats.add(source)
            values = {field: _number(data.get(field)) for field in COMPARE_SYSTEM_FIELDS}
            total = values["mem_total_mb"]
            remainder = _number(data.get("mem_free_mb" if source == "top" else "mem_avail_mb"))
            used = values["mem_used_mb"]
            if total is not None and remainder is not None and (source != "top" or used is None):
                used = _number(total - remainder)
            values["mem_used_mb"] = used
            values["mem_remaining_mb"] = (
                _number(total - used) if total is not None and used is not None else None)
            values["mem_percent"] = _number(used / total * 100) if used is not None and total is not None and total > 0 else None
            cpu = values["cpu_total"]
            values["cpu_idle"] = 100 - cpu if cpu is not None else None
            _insert_values(db, "system", values)
            if index % 256 == 0:
                report("system", index, system_count)
        report("system", system_count, system_count)
        report("process", 0, process_count)
        processed = 0
        db.execute("CREATE TEMP TABLE compare_io(segment INTEGER,cycle INTEGER,field TEXT,value REAL)")
        processes = {}
        cursor = db.execute("SELECT * FROM report_source WHERE kind IN ('P','DP') ORDER BY segment,name,cycle,id")
        for (part, name), rows in itertools.groupby(cursor, key=lambda row: (row["segment"], row["name"])):
            if name not in processes:
                processes[name] = dict(scope="p" + str(len(processes)), name=name, pids=set(), formats=set(), observed=0)
            process = processes[name]
            for cycle, members in itertools.groupby(rows, key=lambda row: row["cycle"]):
                members = list(members)
                process["formats"].update(json.loads(row["data"]).get("source_format", "sysmonitor")
                            for row in members if row["kind"] == "P")
                full, values = _process_cycle(members)
                process["pids"].update(pid for pid in full[9] if pid is not None)
                process["observed"] += 1
                _insert_values(db, process["scope"], values)
                db.executemany("INSERT INTO compare_io VALUES (?,?,?,?)",
                               ((part, cycle, field, values[field]) for field in sorted(INCREMENTS)))
                processed += len(members)
                report("process", processed, process_count)
        report("process", process_count, process_count)
        report("io")
        # Only sum observed names when every observed member has a valid value.
        # Absent names are never invented, and this is not whole-device disk I/O.
        db.execute("""INSERT INTO report_values
            SELECT 'io',field,SUM(value),0 FROM compare_io
            GROUP BY segment,cycle,field HAVING COUNT(value)=COUNT(*)""")
        scopes = {"system": COMPARE_SYSTEM_FIELDS, "io": sorted(INCREMENTS)}
        scopes.update({p["scope"]: PROCESS_FIELDS for p in processes.values()})
        report("statistics")
        metrics = _statistics(db, scopes)
        for process in processes.values():
            process["metrics"] = metrics[process.pop("scope")]
            process["pids"] = sorted(process["pids"])
            process["formats"] = sorted(process["formats"])
        return dict(session=session, segment=segment, cycles=sum(map(len, axes.values())),
                    segments=len(axes), duration_seconds=duration, formats=sorted(formats),
                    interval=dict(count=len(intervals), min=intervals[0] if intervals else None,
                                  max=intervals[-1] if intervals else None,
                                  median=(intervals[(len(intervals)-1)//2] + intervals[len(intervals)//2]) / 2 if intervals else None,
                                  invalid=invalid),
                    system=metrics["system"], io=metrics["io"], processes=processes)


def _difference(before, after):
    delta = _number(after - before) if before is not None and after is not None else None
    percent = _number(delta / abs(before) * 100) if delta is not None and before else None
    return dict(baseline=before, target=after, delta=delta, percent=percent)


def _compare_metrics(before, after, blocked=()):
    result = {}
    for field in sorted(before.keys() | after.keys()):
        a, b = before.get(field, {}), after.get(field, {})
        meta = a or b
        statistics = {}
        for stat in ("avg", "p95", "p99", "max", "total"):
            if stat not in a and stat not in b:
                continue
            difference = _difference(a.get(stat), b.get(stat))
            if field in blocked:
                difference.update(delta=None, percent=None)
            statistics[stat] = difference
        result[field] = dict(label=meta["label"], unit=meta["unit"],
                             baseline_count=a.get("count", 0), target_count=b.get("count", 0),
                             comparable=field not in blocked, statistics=statistics)
    return result


def compare_sessions(store, baseline_id, target_id, baseline_segment=None, target_segment=None,
                     progress=None, cancelled=None):
    last_stage, last_sent = None, 0

    def report(side, stage, completed=None, total=None):
        nonlocal last_stage, last_sent
        if cancelled is not None and cancelled():
            raise CancelledError()
        now = time.monotonic()
        key = (side, stage)
        if progress is not None and (key != last_stage or now - last_sent >= 0.2 or
                                     (total is not None and completed == total)):
            progress(dict(side=side, stage=stage, completed=completed, total=total))
            last_stage, last_sent = key, now

    report(None, "validate")
    if baseline_id == target_id:
        raise ValueError("请选择两次不同的采集")
    # Validate both sessions before doing expensive sample aggregation.
    store.session(baseline_id)
    store.session(target_id)
    baseline = _snapshot(store, baseline_id, baseline_segment,
                         lambda *args: report("baseline", *args), cancelled)
    target = _snapshot(store, target_id, target_segment,
                       lambda *args: report("target", *args), cancelled)
    report(None, "difference")
    warnings = [
        "差值=对比采集−基准采集；CPU差值单位为百分点，相对变化以基准绝对值为分母。基准为0时相对变化不计算。",
        "进程按原始名称精确匹配，不按PID或路径简称；同段同周期同名PID先合计，再对全部有效周期计算均值和最近秩分位数。缺失不补零。",
        "整机CPU满载100%；进程CPU保留单核算力口径，可超过100%，两者不能直接相加比较。",
        "IO为已采集进程的物理/逻辑读写增量，不是整机磁盘IO；整体IO按周期合计已观察名称，任一成员字段缺失则该周期该字段无效。",
        "IO单位为KB/周期和有效累计KB，不是KB/s。采样间隔、有效覆盖率或时长不同会影响比较；未观察到不代表进程新增或退出。",
        "进程内存：sysmonitor RSS前20进程可能含GPU回填，Top RES不含该回填；来源不同或混合来源时仍计算数值差异并纳入内存排名，差值仅供参考，不可直接判定内存退化；缺失数据不补零。",
        "全会话按各段有效样本汇总，不跨段合并周期、不平均各段分位数；时长为各段时间跨度之和，不含段间间隔。",
    ]
    warnings.append("系统剩余内存逐样本按总内存−已用内存计算，再统计均值、分位数和峰值，不使用汇总值相减。sysmonitor 对应可用内存，Top 对应其 used 的剩余量，不统一称为 Linux 空闲内存。")
    if baseline["formats"] != target["formats"] or len(baseline["formats"]) > 1:
        warnings.append("两侧系统内存来源不同或包含混合来源：已用、剩余内存和使用率仍提供数值差异，但 Top used 与 sysmonitor MemTotal−MemAvailable 的缓存口径不同，差值仅供参考，不可直接判定内存退化。")
    if baseline["interval"] != target["interval"] or baseline["duration_seconds"] != target["duration_seconds"]:
        warnings.append("两次采集的时长或采样间隔分布不同，请优先对比相同场景和采样设置；IO累计不能单独用于判断退化。")
    processes = []
    left, right = baseline.pop("processes"), target.pop("processes")
    names = sorted(left.keys() | right.keys())
    for index, name in enumerate(names, 1):
        report(None, "difference", index - 1, len(names))
        a, b = left.get(name), right.get(name)
        processes.append(dict(name=name, status="matched" if a and b else "baseline_only" if a else "target_only",
                              baseline=a, target=b,
                              metrics=_compare_metrics(a["metrics"] if a else {}, b["metrics"] if b else {})))
    system = _compare_metrics(baseline.pop("system"), target.pop("system"))
    io = _compare_metrics(baseline.pop("io"), target.pop("io"))
    report(None, "difference", len(names), len(names))
    return dict(baseline=baseline, target=target, system=system, io=io, processes=processes, warnings=warnings)