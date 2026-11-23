"""Shared local-calendar date selection over observed raw records."""
from datetime import date, datetime
import math
import re


def local_date(value):
    if type(value) not in (int, float):
        return None
    try:
        if not math.isfinite(value):
            return None
        return datetime.fromtimestamp(value / 1000).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def normalize_dates(dates):
    """None means legacy all records; an explicit empty selection is invalid."""
    if dates is None:
        return None
    if not isinstance(dates, (list, tuple)) or not dates:
        raise ValueError("请选择至少一个有效日期")
    for value in dates:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError("日期必须为 YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError("日期不存在") from None
    if len(set(dates)) != len(dates):
        raise ValueError("日期不能重复")
    return sorted(dates)


def report_dates(db, session_id):
    if db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
        raise KeyError(session_id)
    db.create_function("report_local_date", 1, local_date, deterministic=True)
    result, undated = {}, 0
    for row in db.execute("""SELECT report_local_date(ts) date,segment,
            MIN(ts) start,MAX(ts) end,COUNT(*) records FROM records
            WHERE session=? GROUP BY report_local_date(ts),segment
            ORDER BY date,segment""", (session_id,)):
        day, segment, start, end, count = row
        if day is None:
            undated += count
            continue
        item = result.setdefault(day, dict(date=day, records=0, segments=[]))
        item["records"] += count
        item["segments"].append(dict(segment=segment, start=start, end=end, records=count))
    return dict(dates=list(result.values()), undated_records=undated)


def validate_dates(store, session_id, dates):
    selected = normalize_dates(dates)
    if selected is not None:
        with store.connect() as db:
            date_filter(db, session_id, selected)
    return selected


def date_filter(db, session_id, dates):
    selected = normalize_dates(dates)
    if selected is None:
        return ""
    observed = {item["date"] for item in report_dates(db, session_id)["dates"]}
    if not set(selected).issubset(observed):
        raise ValueError("所选日期没有采集记录")
    selected = frozenset(selected)
    db.create_function("report_selected_date", 1, lambda ts: int(local_date(ts) in selected),
                       deterministic=True)
    return " AND report_selected_date(ts)=1"


def selected_session(db, session, table, dates):
    """Only change the in-memory report summary, never the stored session."""
    if dates is None:
        return session
    if table not in {"report_source", "qnx_source"}:
        raise ValueError("未知报告数据源")
    original = session["summary"]
    summary = {k: v for k, v in original.items() if k.endswith("_policy") or k == "source_format"}
    counts = dict.fromkeys(original.get("counts", {}), 0)
    counts.update(dict(db.execute(f"SELECT kind,COUNT(*) FROM {table} GROUP BY kind")))
    first, last, records = db.execute(f"SELECT MIN(ts),MAX(ts),COUNT(*) FROM {table}").fetchone()
    cycles = db.execute(f"SELECT COUNT(*) FROM (SELECT segment,cycle FROM {table} GROUP BY segment,cycle)").fetchone()[0]
    segments = db.execute(f"SELECT COUNT(DISTINCT segment) FROM {table}").fetchone()[0]
    files = [row[0] for row in db.execute(f"SELECT DISTINCT source FROM {table} ORDER BY source")]
    summary.update(counts=counts, records=records, segments=segments, cycles=cycles,
                   first_ts=first, last_ts=last, files=files, selected_dates=normalize_dates(dates))
    return dict(session, summary=summary, import_summary=original,
                selected_dates=normalize_dates(dates),
                date_selection_note="所选日期（本机本地日期）：" + "、".join(normalize_dates(dates)) +
                "；仅统计所选日期的实测记录，无有效日期的记录不纳入。导入诊断仍保留完整会话范围。")