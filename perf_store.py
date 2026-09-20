"""Local SQLite sessions. Imported sources are streamed, never retained in RAM."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from perf_parser import iter_records, rotation_key
from perf_groups import GroupStore
from perf_metrics import CATEGORIES, INCREMENTS, LABELS, METHOD, NOTES, NUMERIC, metadata


class Store(GroupStore):
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, name TEXT, created TEXT,
                    fingerprint TEXT UNIQUE, summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    segment INTEGER, cycle INTEGER, ts INTEGER, kind TEXT,
                    pid INTEGER, name TEXT, source TEXT, line INTEGER, data TEXT
                );
                CREATE INDEX IF NOT EXISTS records_series ON records(session, kind, id);
                CREATE INDEX IF NOT EXISTS records_process ON records(session, pid, kind);
                CREATE INDEX IF NOT EXISTS records_group_scope ON records(session, segment, cycle);
                CREATE TABLE IF NOT EXISTS process_groups (
                    session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    id TEXT NOT NULL, config TEXT NOT NULL, PRIMARY KEY(session, id)
                );
                CREATE TABLE IF NOT EXISTS report_settings (
                    session TEXT PRIMARY KEY NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    config TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analysis_cache (
                    session TEXT REFERENCES sessions(id) ON DELETE CASCADE,
                    scope TEXT, result TEXT NOT NULL, PRIMARY KEY(session, scope)
                );
                CREATE TABLE IF NOT EXISTS process_rss_rank_v1 (
                    session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    merged INTEGER NOT NULL, segment INTEGER, pid INTEGER, name TEXT,
                    rss_avg_kb REAL, rss_p95_kb REAL, rss_p99_kb REAL,
                    PRIMARY KEY(session,merged,segment,pid,name)
                );
                CREATE TABLE IF NOT EXISTS process_rss_ready_v1 (
                    session TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS process_rank_v3 (
                    session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    segment INTEGER, pid INTEGER, name TEXT, cpu_p95 REAL, cpu_p99 REAL,
                    samples INTEGER, cpu_peak REAL, cpu_avg REAL, rss_peak_kb REAL,
                    wait_peak REAL, read_kb REAL, write_kb REAL, dmabuf_peak_kb REAL,
                    PRIMARY KEY(session, segment, pid, name)
                );
                CREATE TABLE IF NOT EXISTS process_rank_ready_v3 (
                    session TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    total INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS process_name_rank_v2 (
                    session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    segment INTEGER, name TEXT, cpu_p95 REAL, cpu_p99 REAL,
                    samples INTEGER, cpu_peak REAL, cpu_avg REAL, rss_peak_kb REAL,
                    wait_peak REAL, read_kb REAL, write_kb REAL, dmabuf_peak_kb REAL,
                    pids TEXT NOT NULL DEFAULT '[]', pid_path TEXT NOT NULL DEFAULT '[]',
                    pid_changes INTEGER NOT NULL DEFAULT 0,
                    concurrent_pids INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(session, segment, name)
                );
                CREATE TABLE IF NOT EXISTS process_name_ready_v2 (
                    session TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                    total INTEGER NOT NULL
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def sessions(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,name,created,summary FROM sessions ORDER BY created DESC").fetchall()
            return [dict(row, summary=json.loads(row["summary"])) for row in rows]

    def session(self, session_id):
        with self.connect() as db:
            row = db.execute("SELECT id,name,created,summary FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            return dict(row, summary=json.loads(row["summary"]))

    def delete(self, session_id):
        with self.connect() as db:
            return db.execute("DELETE FROM sessions WHERE id=?", (session_id,)).rowcount > 0

    def import_files(self, sources, title=""):
        sources = sorted(sources, key=lambda source: rotation_key(source[0]))
        fingerprint = hashlib.sha256()
        unique_sources, seen = [], set()
        for name, stream in sources:
            digest = hashlib.sha256()
            stream.seek(0)
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            value = digest.digest()
            if value in seen:
                continue
            seen.add(value)
            fingerprint.update(value)
            stream.seek(0)
            unique_sources.append((Path(name.replace("\\", "/")).name, stream))
        signature = fingerprint.hexdigest()
        with self.connect() as db:
            # Serialize imports before checking duplicates.
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM sessions WHERE fingerprint=?", (signature,)).fetchone()
            if existing:
                return {"id": existing["id"], "duplicate": True}
            session_id = uuid.uuid4().hex
            summary = {"counts": {k: 0 for k in ("S", "P", "D", "DE", "DP")},
                       "errors": 0, "unknown": 0, "issues": [], "events": [], "event_count": 0,
                       "files": [name for name, _ in unique_sources], "segments": 0,
                       "orphan_cycles": 0, "duplicate_files": len(sources) - len(unique_sources)}
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (
                session_id, title[:120] or "离线采集 " + datetime.now().strftime("%m-%d %H:%M"),
                datetime.now(timezone.utc).isoformat(), signature, "{}"))
            last_ts = None
            boot = None
            segment = 0
            cycle = 0
            cycle_has_s = False
            batch = []
            first_ts = None
            for source, stream in unique_sources:
                for record in iter_records(stream):
                    if record.error:
                        summary["errors"] += 1
                        if len(summary["issues"]) < 100:
                            summary["issues"].append({"source": source, "line": record.number, "message": record.error})
                        continue
                    if record.kind is None:
                        summary["unknown"] += 1
                        continue
                    kind, data = record.kind, record.data
                    ts = data["ts"]
                    changed_boot = kind == "S" and boot is not None and data["boot"] != boot
                    backwards = last_ts is not None and ts < last_ts
                    if changed_boot or backwards:
                        segment += 1
                        summary["event_count"] += 1
                        if len(summary["events"]) < 100:
                            summary["events"].append({"source": source, "line": record.number, "ts": ts,
                                                      "message": "设备重启" if changed_boot else "时钟回拨或数据乱序"})
                    if ts != last_ts or changed_boot or backwards:
                        if last_ts is not None and not cycle_has_s:
                            summary["orphan_cycles"] += 1
                        cycle += 1
                        cycle_has_s = False
                    if kind == "S":
                        boot = data["boot"]
                        cycle_has_s = True
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                    summary["counts"][kind] += 1
                    batch.append((session_id, segment, cycle, ts, kind, data.get("pid"),
                                  data.get("name", data.get("exporter")), source, record.number,
                                  json.dumps(data, ensure_ascii=False)))
                    if len(batch) >= 1000:
                        self._insert(db, batch)
                        batch.clear()
            self._insert(db, batch)
            if not any(summary["counts"].values()):
                raise ValueError("没有有效的 sysmonitor 记录，请检查日志格式及末尾换行")
            if last_ts is not None and not cycle_has_s:
                summary["orphan_cycles"] += 1
            summary.update(segments=segment + 1, cycles=cycle, first_ts=first_ts, last_ts=last_ts)
            db.execute("UPDATE sessions SET summary=? WHERE id=?", (json.dumps(summary, ensure_ascii=False), session_id))
        return {"id": session_id, "duplicate": False}

    @staticmethod
    def _insert(db, rows):
        db.executemany("""INSERT INTO records
            (session,segment,cycle,ts,kind,pid,name,source,line,data) VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)

    def statistics(self, session_id, kind, pid=None, segment=None, name=None):
        """Exact nearest-rank statistics, disk-backed sorting and immutable-scope cache."""
        if kind not in NUMERIC:
            raise ValueError("不支持的记录类型")
        self.session(session_id)
        scope = json.dumps([1, kind, pid, segment, name], ensure_ascii=False)
        where, params = "session=? AND kind=?", [session_id, kind]
        for key, value in (("pid", pid), ("segment", segment), ("name", name)):
            if value is not None:
                where += " AND " + key + "=?"
                params.append(value)
        with self.connect() as db:
            cached = db.execute("SELECT result FROM analysis_cache WHERE session=? AND scope=?",
                                (session_id, scope)).fetchone()
            if cached:
                return json.loads(cached[0])
            # Keep raw JSON extraction to one scan. SQLite spills metric sorts to disk.
            db.execute("PRAGMA temp_store=FILE")
            db.execute("PRAGMA temp.cache_size=-8192")
            fields = NUMERIC[kind] + CATEGORIES.get(kind, ())
            columns = ",".join("json_extract(data,'$.%s') AS %s" % (f, f) for f in fields)
            db.execute("CREATE TEMP TABLE metric_scope AS SELECT ts," + columns +
                       " FROM records WHERE " + where, params)
            bounds = dict(db.execute("SELECT COUNT(*) samples, MIN(ts) start, MAX(ts) end FROM metric_scope").fetchone())
            result = dict(bounds, method=METHOD, metrics={}, categories={},
                          scope={"kind": kind, "pid": pid, "segment": segment, "name": name})
            for field in NUMERIC[kind]:
                row = dict(db.execute(f"""WITH ranked AS (
                    SELECT {field} value, ROW_NUMBER() OVER (ORDER BY {field}) rn,
                           COUNT(*) OVER () n FROM metric_scope WHERE {field} IS NOT NULL
                    ) SELECT COUNT(*) count, MIN(value) min, MAX(value) max, AVG(value) avg,
                    MAX(CASE WHEN rn=(95*n+99)/100 THEN value END) p95,
                    MAX(CASE WHEN rn=(99*n+99)/100 THEN value END) p99,
                    SUM(CAST(value AS REAL)) total FROM ranked""").fetchone())
                if field not in INCREMENTS:
                    row.pop("total")
                result["metrics"][field] = dict(row, **metadata(field))
            for field in CATEGORIES.get(kind, ()):
                rows = db.execute(f"SELECT {field} value, COUNT(*) count FROM metric_scope "
                                  f"WHERE {field} IS NOT NULL GROUP BY {field} ORDER BY count DESC, value").fetchall()
                count = sum(row["count"] for row in rows)
                result["categories"][field] = {
                    "label": LABELS[field], "note": NOTES.get(field, ""), "count": count,
                    "values": [dict(row, percent=100 * row["count"] / count) for row in rows]}
            # A concurrent deletion must not resurrect a cached session.
            db.execute("INSERT OR IGNORE INTO analysis_cache SELECT id,?,? FROM sessions WHERE id=?",
                       (scope, json.dumps(result, ensure_ascii=False), session_id))
            return result

    def series(self, session_id, kind, limit=1500, pid=None, segment=None, name=None):
        self.session(session_id)
        where = "session=? AND kind=?"
        params = [session_id, kind]
        for key, value in (("pid", pid), ("segment", segment), ("name", name)):
            if value is not None:
                where += " AND " + key + "=?"
                params.append(value)
        # Buckets preserve min/max for each numeric metric (not simple stride sampling).
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM records WHERE " + where, params).fetchone()[0]
            cursor = db.execute("SELECT id,segment,cycle,data FROM records WHERE " + where + " ORDER BY id", params)
            return self._sample(cursor, count, limit)

    def process_references(self, session_id, segment, limit=1500):
        """Aggregate full-scope P IO before sampling; never inherit process filters."""
        self.session(session_id)
        io_fields = ("rd_kb", "wr_kb", "rchar_kb", "wchar_kb")
        sums = ",".join(
            f"SUM(CASE WHEN kind='P' THEN CAST(json_extract(data,'$.{field}') AS REAL) END) AS {field}"
            for field in io_fields)
        with self.connect() as db:
            # Include S-only cycles so absent IO remains NULL, not a fabricated zero.
            # Spill the full cycle aggregation to disk rather than loading all P rows.
            db.execute("PRAGMA temp_store=FILE")
            db.execute("""CREATE TEMP TABLE process_reference_cycles AS
                SELECT MIN(id) id, segment, cycle, MIN(ts) ts,
                MAX(CASE WHEN kind='S' THEN json_extract(data,'$.cpu_total') END) cpu_total,
                MAX(CASE WHEN kind='S' THEN json_extract(data,'$.mem_total_mb')
                    - json_extract(data,'$.mem_avail_mb') END) mem_used_mb,
                """ + sums + """ FROM records
                WHERE session=? AND segment=? AND kind IN ('S','P')
                GROUP BY segment,cycle""", (session_id, segment))
            count = db.execute("SELECT COUNT(*) FROM process_reference_cycles").fetchone()[0]
            cursor = db.execute("SELECT * FROM process_reference_cycles ORDER BY id")

            def rows():
                for row in cursor:
                    point = {field: row[field] for field in ("ts", "cpu_total", "mem_used_mb", *io_fields)}
                    yield {"id": row["id"], "segment": row["segment"], "cycle": row["cycle"],
                           "data": json.dumps(point)}

            return self._sample(rows(), count, limit)

    def report_series(self, session_id, kind, segment, pid=None, name=None, limit=600):
        """Mark real metric gaps before bounded report downsampling."""
        if kind not in ("S", "P"):
            raise ValueError("报告趋势仅支持 S / P")
        self.session(session_id)
        fields = ("cpu_total",) if kind == "S" else ("cpu", "rss_kb")
        where, params = "session=? AND kind=? AND segment=?", [session_id, kind, segment]
        for key, value in (("pid", pid), ("name", name)):
            if value is not None:
                where += " AND " + key + "=?"
                params.append(value)

        def marked_rows(cursor):
            previous = None
            runs = dict.fromkeys(fields, 0)
            for row in cursor:
                data = json.loads(row["data"])
                for field in fields:
                    if (previous is None or data.get(field) is None or
                            previous["data"].get(field) is None or
                            row["cycle"] != previous["cycle"] + 1 or
                            data["ts"] <= previous["data"]["ts"]):
                        runs[field] += 1
                point = {key: data.get(key) for key in ("ts", *fields)}
                point.update({"_run_" + field: runs[field] for field in fields})
                yield dict(row, data=json.dumps(point))
                previous = {"cycle": row["cycle"], "data": data}

        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM records WHERE " + where, params).fetchone()[0]
            cursor = db.execute("SELECT id,segment,cycle,data FROM records WHERE " + where + " ORDER BY id", params)
            return self._sample(marked_rows(cursor), count, limit)

    @staticmethod
    def _sample(cursor, count, limit):
        import math
        if type(limit) is not int or limit < 1:
            raise ValueError("limit 必须是正整数")
        if count <= limit:
            return {"points": [dict(json.loads(row["data"]), segment=row["segment"], cycle=row["cycle"])
                               for row in cursor], "total": count, "sampled": False}
        metrics = tuple(dict.fromkeys(field for fields in NUMERIC.values() for field in fields))
        width = max(1, math.ceil(count / max(1, limit // (2 * len(metrics) + 2))))
        result, selected, extrema = [], {}, {}
        for index, row in enumerate(cursor):
            point = dict(json.loads(row["data"]), segment=row["segment"], cycle=row["cycle"])
            identity = row["id"]
            if index % width == 0:
                result.extend(value for _, value in sorted(selected.items()))
                selected, extrema = {identity: point}, {}
            for key in metrics:
                if point.get(key) is None:
                    continue
                for mode in ("min", "max"):
                    old = extrema.get((key, mode))
                    if old is None or (point[key] < old[1][key] if mode == "min" else point[key] > old[1][key]):
                        extrema[key, mode] = (identity, point)
            # Keep only first/last and extrema; memory does not depend on bucket size.
            first = min(selected)
            selected = {first: selected[first], identity: point}
            selected.update({i: p for i, p in extrema.values()})
        result.extend(value for _, value in sorted(selected.items()))
        if len(result) > limit:
            # Very small budgets cannot retain every metric's extrema. Prefer
            # global extrema in metric order, then endpoints, without exceeding limit.
            keep = {}
            for key in metrics:
                valid = [i for i, point in enumerate(result) if point.get(key) is not None]
                if not valid:
                    continue
                for pick in (max, min):
                    index = pick(valid, key=lambda i: result[i][key])
                    if len(keep) < limit:
                        keep[index] = result[index]
            for index in (0, len(result) - 1):
                if len(keep) < limit:
                    keep[index] = result[index]
            for index in range(len(result)):
                if len(keep) >= limit:
                    break
                keep[index] = result[index]
            result = [point for _, point in sorted(keep.items())]
        return {"points": result, "total": count, "sampled": len(result) < count}

    def overview(self, session_id):
        session = self.session(session_id)
        with self.connect() as db:
            stats = db.execute("""SELECT COUNT(*) samples,
                AVG(json_extract(data,'$.cpu_total')) cpu_avg,
                MAX(json_extract(data,'$.cpu_total')) cpu_peak,
                MIN(json_extract(data,'$.mem_avail_mb')) mem_available_min,
                MAX(json_extract(data,'$.mem_used_mb')) mem_used_peak
                FROM records WHERE session=? AND kind='S'""", (session_id,)).fetchone()
            segments = db.execute("""SELECT segment,MIN(ts) start,MAX(ts) end,COUNT(*) records
                FROM records WHERE session=? GROUP BY segment ORDER BY segment""", (session_id,)).fetchall()
            exporters = db.execute("""SELECT name,MAX(json_extract(data,'$.size_mb')) peak_mb,
                COUNT(*) samples FROM records WHERE session=? AND kind='DE'
                GROUP BY name ORDER BY peak_mb DESC LIMIT 100""", (session_id,)).fetchall()
        return dict(session, stats=dict(stats), segments=[dict(r) for r in segments],
                    exporters=[dict(r) for r in exporters])

    def _ensure_process_ranks(self, db, session_id):
        # Versioned caches exclude old cpu-based ranks; missing cpu1c stays NULL.
        # Public cpu_* fields represent single-core CPU for both values and sorting.
        if db.execute("SELECT 1 FROM process_rank_ready_v3 WHERE session=?", (session_id,)).fetchone():
            return
        # Recheck under SQLite's writer lock: concurrent first requests build only once.
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            raise KeyError(session_id)
        if db.execute("SELECT 1 FROM process_rank_ready_v3 WHERE session=?", (session_id,)).fetchone():
            db.commit()
            return
        db.execute("PRAGMA temp_store=FILE")
        db.execute("PRAGMA cache_size=-8192")
        db.execute("""INSERT INTO process_rank_v3
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY segment,pid,name,kind
                    ORDER BY json_extract(data,'$.cpu1c') IS NULL,json_extract(data,'$.cpu1c')) rn,
       COUNT(json_extract(data,'$.cpu1c')) OVER (PARTITION BY segment,pid,name,kind) n
                FROM records WHERE session=? AND kind IN ('P','DP')
            ) SELECT ?,segment,pid,name,
                MAX(CASE WHEN kind='P' AND rn=(95*n+99)/100 THEN json_extract(data,'$.cpu1c') END),
                MAX(CASE WHEN kind='P' AND rn=(99*n+99)/100 THEN json_extract(data,'$.cpu1c') END),
                SUM(kind='P'),
                MAX(json_extract(data,'$.cpu1c')), AVG(json_extract(data,'$.cpu1c')),
                MAX(json_extract(data,'$.rss_kb')), MAX(json_extract(data,'$.wait')),
                SUM(CAST(json_extract(data,'$.rd_kb') AS REAL)),
                SUM(CAST(json_extract(data,'$.wr_kb') AS REAL)),
                MAX(CASE WHEN kind='DP' THEN json_extract(data,'$.size_kb') END)
            FROM ranked GROUP BY segment,pid,name""", (session_id, session_id))
        db.execute("""INSERT INTO process_rank_ready_v3 SELECT ?,COUNT(*)
            FROM process_rank_v3 WHERE session=?""", (session_id, session_id))
        db.commit()

    def _ensure_process_name_ranks(self, db, session_id):
        if db.execute("SELECT 1 FROM process_name_ready_v2 WHERE session=?", (session_id,)).fetchone():
            return
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            raise KeyError(session_id)
        if db.execute("SELECT 1 FROM process_name_ready_v2 WHERE session=?", (session_id,)).fetchone():
            db.commit()
            return
        db.execute("PRAGMA temp_store=FILE")
        db.execute("PRAGMA cache_size=-8192")
        # Recompute from raw samples, never average per-PID percentiles/averages.
        db.execute("""INSERT INTO process_name_rank_v2
            (session,segment,name,cpu_p95,cpu_p99,samples,cpu_peak,cpu_avg,
             rss_peak_kb,wait_peak,read_kb,write_kb,dmabuf_peak_kb)
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY segment,name,kind
                    ORDER BY json_extract(data,'$.cpu1c') IS NULL,json_extract(data,'$.cpu1c')) rn,
                    COUNT(json_extract(data,'$.cpu1c')) OVER (PARTITION BY segment,name,kind) n
                FROM records WHERE session=? AND kind IN ('P','DP')
            ) SELECT ?,segment,name,
                MAX(CASE WHEN kind='P' AND rn=(95*n+99)/100 THEN json_extract(data,'$.cpu1c') END),
                MAX(CASE WHEN kind='P' AND rn=(99*n+99)/100 THEN json_extract(data,'$.cpu1c') END),
                SUM(kind='P'), MAX(json_extract(data,'$.cpu1c')), AVG(json_extract(data,'$.cpu1c')),
                MAX(json_extract(data,'$.rss_kb')), MAX(json_extract(data,'$.wait')),
                SUM(CAST(json_extract(data,'$.rd_kb') AS REAL)),
                SUM(CAST(json_extract(data,'$.wr_kb') AS REAL)),
                MAX(CASE WHEN kind='DP' THEN json_extract(data,'$.size_kb') END)
            FROM ranked GROUP BY segment,name""", (session_id, session_id))
        from itertools import groupby
        # P/DP repeats and duplicate rows within one cycle do not imply a restart.
        cursor = db.execute("""SELECT DISTINCT segment,name,cycle,pid FROM records
            WHERE session=? AND kind IN ('P','DP') ORDER BY segment,name,cycle,pid""", (session_id,))
        for (segment, name), records in groupby(cursor, key=lambda row: (row['segment'], row['name'])):
            pids, path, previous, changes, concurrent = {}, [], None, 0, False
            for _, cycle in groupby(records, key=lambda row: row['cycle']):
                current = [row['pid'] for row in cycle]
                pids.update(dict.fromkeys(current))
                concurrent |= len(current) > 1
                if current != previous:
                    changes += previous is not None
                    if len(path) < 4:
                        path.append(current)
                    previous = current
            db.execute("""UPDATE process_name_rank_v2 SET pids=?,pid_path=?,pid_changes=?,concurrent_pids=?
                WHERE session=? AND segment=? AND name=?""",
                       (json.dumps(list(pids)), json.dumps(path), changes, concurrent, session_id, segment, name))
        db.execute("""INSERT INTO process_name_ready_v2 SELECT ?,COUNT(*)
            FROM process_name_rank_v2 WHERE session=?""", (session_id, session_id))
        db.commit()

    def _ensure_process_rss_ranks(self, db, session_id):
        if db.execute("SELECT 1 FROM process_rss_ready_v1 WHERE session=?", (session_id,)).fetchone():
            return
        db.execute("BEGIN IMMEDIATE")
        self._require_session(db, session_id)
        if db.execute("SELECT 1 FROM process_rss_ready_v1 WHERE session=?", (session_id,)).fetchone():
            db.commit()
            return
        db.execute("PRAGMA temp_store=FILE")
        db.execute("PRAGMA cache_size=-8192")
        for merged in (0, 1):
            identity = "segment,name" if merged else "segment,pid,name"
            pid = "-1" if merged else "pid"
            db.execute(f"""INSERT INTO process_rss_rank_v1
                WITH ranked AS (
                    SELECT segment,pid,name,json_extract(data,'$.rss_kb') value,
                    ROW_NUMBER() OVER (PARTITION BY {identity}
                        ORDER BY json_extract(data,'$.rss_kb')) rn,
                    COUNT(*) OVER (PARTITION BY {identity}) n
                    FROM records WHERE session=? AND kind='P'
                    AND json_extract(data,'$.rss_kb') IS NOT NULL
                ) SELECT ?,?,segment,{pid},name,AVG(value),
                    MAX(CASE WHEN rn=(95*n+99)/100 THEN value END),
                    MAX(CASE WHEN rn=(99*n+99)/100 THEN value END)
                    FROM ranked GROUP BY {identity}""", (session_id, session_id, merged))
        db.execute("INSERT INTO process_rss_ready_v1 VALUES (?)", (session_id,))
        db.commit()

    def processes(self, session_id, sort="cpu_peak", limit=100, offset=0, q="", with_total=False, merge_names=False, segment=None):
        self.session(session_id)
        if sort not in {"cpu_peak", "cpu_avg", "cpu_p95", "cpu_p99", "rss_peak_kb", "rss_avg_kb", "rss_p95_kb", "rss_p99_kb", "read_kb", "write_kb", "wait_peak", "dmabuf_peak_kb"}:
            raise ValueError("不支持的排序字段")
        if type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
            raise ValueError("分页参数无效")
        if segment is not None and (type(segment) is not int or not 0 <= segment <= 2**63 - 1):
            raise ValueError("segment 必须是非负整数或 null")
        query = q.strip().lower()
        with self.connect() as db:
            rank_table = "process_name_rank_v2" if merge_names else "process_rank_v3"
            ready_table = "process_name_ready_v2" if merge_names else "process_rank_ready_v3"
            ensure = self._ensure_process_name_ranks if merge_names else self._ensure_process_ranks
            ensure(db, session_id)
            self._ensure_process_rss_ranks(db, session_id)
            db.execute("BEGIN")
            ready = db.execute(f"SELECT total FROM {ready_table} WHERE session=?", (session_id,)).fetchone()
            if ready is None:
                raise KeyError(session_id)
            where = "session=?"
            params = [session_id]
            if segment is not None:
                where += " AND segment=?"
                params.append(segment)
            all_total = db.execute(f"SELECT COUNT(*) FROM {rank_table} WHERE " + where, params).fetchone()[0]
            if query:
                # Search individual PIDs, not JSON punctuation or boundaries.
                pid_match = (f"EXISTS (SELECT 1 FROM json_each({rank_table}.pids) "
                             "WHERE instr(CAST(value AS TEXT),?)>0)" if merge_names
                             else "instr(CAST(pid AS TEXT),?)>0")
                where += " AND (instr(lower(name),?)>0 OR " + pid_match + ")"
                params.extend([query, query])
            total = db.execute(f"SELECT COUNT(*) FROM {rank_table} WHERE " + where, params).fetchone()[0] if query else all_total
            tie_break = "segment,name" if merge_names else "segment,pid,name"
            rss_pid = "-1" if merge_names else "r.pid"
            # Correlated cached lookups preserve the existing rank/search columns.
            extras = ",".join(f"(SELECT s.{field} FROM process_rss_rank_v1 s WHERE "
                              f"s.session=r.session AND s.merged={int(bool(merge_names))} "
                              f"AND s.segment=r.segment AND s.pid={rss_pid} AND s.name=r.name) AS {field}"
                               for field in ("rss_avg_kb", "rss_p95_kb", "rss_p99_kb"))
            rows = db.execute(f"SELECT * FROM (SELECT r.*,{extras} FROM {rank_table} r) AS {rank_table} WHERE " + where +
                              " ORDER BY " + sort + " DESC," + tie_break + " LIMIT ? OFFSET ?",
                              (*params, limit, offset)).fetchall()
            items = [{key: row[key] for key in row.keys() if key != "session"} for row in rows]
            if merge_names:
                for item in items:
                    item["pids"] = json.loads(item["pids"])
                    item["pid_path"] = json.loads(item["pid_path"])
                    item["concurrent_pids"] = bool(item["concurrent_pids"])
                    item["pid"] = item["pids"][0]
            return {"items": items, "total": total, "all_total": all_total} if with_total else items