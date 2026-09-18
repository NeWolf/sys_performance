"""Persistent, explicit process groups; analysis uses disk-backed SQLite scopes."""
import json
import uuid

from perf_metrics import INCREMENTS, METHOD, NUMERIC, metadata, resource_settings, to_kdmips


def normalize_group(config, group_id):
    return dict(config, id=group_id, members=[dict(
        service_category="未分类", related_service="") | member
        for member in config["members"]])


class GroupStore:
    @staticmethod
    def _require_session(db, session_id):
        if db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
            raise KeyError(session_id)

    @staticmethod
    def _read_group(db, session_id, group_id):
        row = db.execute("SELECT config FROM process_groups WHERE session=? AND id=?",
                         (session_id, group_id)).fetchone()
        if row is None:
            raise KeyError(group_id)
        return normalize_group(json.loads(row[0]), group_id)

    def groups(self, session_id):
        with self.connect() as db:
            self._require_session(db, session_id)
            return [normalize_group(json.loads(row["config"]), row["id"]) for row in db.execute(
                "SELECT id,config FROM process_groups WHERE session=? ORDER BY rowid", (session_id,))]

    def group(self, session_id, group_id):
        with self.connect() as db:
            self._require_session(db, session_id)
            return self._read_group(db, session_id, group_id)

    @staticmethod
    def _validate_group(db, session_id, config):
        if not isinstance(config, dict):
            raise ValueError("进程组配置必须是对象")
        if not isinstance(config.get("name"), str) or not config["name"].strip():
            raise ValueError("进程组名称不能为空")
        segment = config.get("segment")
        if type(segment) is not int or not 0 <= segment <= 2**63 - 1:
            raise ValueError("segment 必须是非负整数")
        members = config.get("members")
        if not isinstance(members, list) or not 1 <= len(members) <= 50:
            raise ValueError("进程组必须包含 1..50 个成员")
        normalized, seen = [], set()
        for member in members:
            if not isinstance(member, dict):
                raise ValueError("成员必须是对象")
            pid, name = member.get("pid"), member.get("name")
            if type(pid) is not int or not 0 <= pid <= 2**63 - 1 or not isinstance(name, str) or not name:
                raise ValueError("成员 pid/name 无效")
            if (pid, name) in seen:
                raise ValueError("成员 pid/name 重复")
            seen.add((pid, name))
            item = dict(pid=pid, name=name)
            item["service_category"] = member.get("service_category", "未分类")
            if item["service_category"] not in ("未分类", "应用服务", "系统服务"):
                raise ValueError("service_category 必须是未分类、应用服务或系统服务")
            item["related_service"] = member.get("related_service", "")
            if not isinstance(item["related_service"], str) or len(item["related_service"]) > 2000:
                raise ValueError("related_service 必须是最多 2000 字的文本")
            for field in ("process_type", "purpose"):
                item[field] = member.get(field, "")
                if not isinstance(item[field], str):
                    raise ValueError(field + " 必须是文本")
            for field in ("foreground", "background"):
                item[field] = member.get(field, "待填写")
                if item[field] not in ("待填写", "Y", "N"):
                    raise ValueError(field + " 必须是待填写、Y 或 N")
            if db.execute("""SELECT 1 FROM records WHERE session=? AND segment=?
                    AND pid=? AND name=? AND kind IN ('P','DP') LIMIT 1""",
                          (session_id, segment, pid, name)).fetchone() is None:
                raise ValueError("成员在指定 segment 内没有 P 或 DP 记录")
            normalized.append(item)
        start, end = config.get("start"), config.get("end")
        for value in (start, end):
            if value is not None and (type(value) is not int or not -(2**63) <= value <= 2**63 - 1):
                raise ValueError("时间范围必须是毫秒整数或 null")
        if start is not None and end is not None and start > end:
            raise ValueError("start 不能晚于 end")
        scene = config.get("scene", "未标注")
        description = config.get("description", "")
        if scene not in ("未标注", "前台", "后台") or not isinstance(description, str):
            raise ValueError("场景或描述无效")
        return dict(name=config["name"], segment=segment, members=normalized,
                    scene=scene, start=start, end=end, description=description)

    def save_group(self, session_id, config, group_id=None):
        """Create a UUID group, or replace an existing group in this session."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_session(db, session_id)
            if group_id is not None:
                self._read_group(db, session_id, group_id)
            elif db.execute("SELECT COUNT(*) FROM process_groups WHERE session=?",
                            (session_id,)).fetchone()[0] >= 20:
                raise ValueError("每个会话最多 20 个进程组")
            value = self._validate_group(db, session_id, config)
            encoded = json.dumps(value, ensure_ascii=False)
            if group_id is None:
                group_id = uuid.uuid4().hex
                db.execute("INSERT INTO process_groups(session,id,config) VALUES (?,?,?)",
                           (session_id, group_id, encoded))
            else:
                db.execute("UPDATE process_groups SET config=? WHERE session=? AND id=?",
                           (encoded, session_id, group_id))
            return dict(value, id=group_id)

    def delete_group(self, session_id, group_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_session(db, session_id)
            return db.execute("DELETE FROM process_groups WHERE session=? AND id=?",
                              (session_id, group_id)).rowcount > 0

    def report_settings(self, session_id):
        with self.connect() as db:
            self._require_session(db, session_id)
            row = db.execute("SELECT config FROM report_settings WHERE session=?",
                             (session_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def save_report_settings(self, session_id, config):
        resource_settings(config)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._require_session(db, session_id)
            encoded = json.dumps(config, ensure_ascii=False)
            db.execute("""INSERT INTO report_settings(session,config) VALUES (?,?)
                ON CONFLICT(session) DO UPDATE SET config=excluded.config""", (session_id, encoded))
            return json.loads(encoded)

    def group_resources(self, session_id, group_id):
        """Independent member P samples, not strict complete-group cycle totals."""
        fields = ("cpu1c", "rss_kb", "rd_kb", "wr_kb")
        with self.connect() as db:
            db.execute("PRAGMA temp_store=FILE")
            db.execute("PRAGMA temp.cache_size=-8192")
            db.execute("BEGIN")
            self._require_session(db, session_id)
            group = self._read_group(db, session_id, group_id)
            row = db.execute("SELECT config FROM report_settings WHERE session=?", (session_id,)).fetchone()
            settings = resource_settings(json.loads(row[0]) if row else {})
            where, params = "session=? AND segment=? AND kind='P'", [session_id, group["segment"]]
            for field, op in (("start", ">="), ("end", "<=")):
                if group[field] is not None:
                    where += " AND ts" + op + "?"
                    params.append(group[field])
            db.execute("CREATE TEMP TABLE resource_members (pid INTEGER,name TEXT,PRIMARY KEY(pid,name))")
            db.executemany("INSERT INTO resource_members VALUES (?,?)",
                           ((m["pid"], m["name"]) for m in group["members"]))
            columns = ",".join("MAX(json_extract(data,'$.%s')) AS %s" % (f, f) for f in fields)
            db.execute("CREATE TEMP TABLE resource_cycles AS SELECT r.pid,r.name,cycle," + columns +
                       " FROM records r JOIN resource_members m ON r.pid=m.pid AND r.name=m.name WHERE " +
                       where + " GROUP BY r.pid,r.name,cycle HAVING COUNT(*)=1", params)
            db.execute("CREATE INDEX temp.resource_identity ON resource_cycles(pid,name)")
            rows = []
            for member in group["members"]:
                statistics = {}
                for field in fields:
                    metric = dict(db.execute(f"""WITH ranked AS (
                        SELECT {field} value,ROW_NUMBER() OVER (ORDER BY {field}) rn,
                        COUNT(*) OVER () n FROM resource_cycles
                        WHERE pid=? AND name=? AND {field} IS NOT NULL
                        ) SELECT COUNT(*) count,MIN(value) min,AVG(value) avg,MAX(value) max,
                        MAX(CASE WHEN rn=(95*n+99)/100 THEN value END) p95,
                        MAX(CASE WHEN rn=(99*n+99)/100 THEN value END) p99,
                        SUM(CAST(value AS REAL)) total FROM ranked""",
                        (member["pid"], member["name"])).fetchone())
                    if field not in INCREMENTS:
                        metric.pop("total")
                    statistics[field] = metric
                kdmips = {key: to_kdmips(statistics["cpu1c"][key], settings["kdmips_per_core"])
                          for key in ("min", "avg", "p95", "p99", "max")}
                rows.append(dict(member=member, statistics=statistics, kdmips=kdmips))
            note = ("成员独立统计：按 segment、毫秒闭区间及精确 PID/name 匹配 P 记录；"
                    "仅保留该成员同周期恰好一条记录，不要求其他成员齐全。"
                    "不同于全部成员齐全后先相加的严格组总量；不得相加成员分位数或峰值。"
                    "CPU 使用 cpu1c 单核百分比，可超过 100%；K = cpu1c / 100 × kdmips_per_core，"
                    "未配置系数时为 null；RSS 单位 KB，rd/wr 为 KB 周期增量，total 为有效周期累计，非 KB/s。" + METHOD)
            return dict(group=group, settings=settings, rows=rows, note=note)

    def group_analysis(self, session_id, group_id, kind="P", limit=600):
        """Sum only exact member/cycle matches, then rank valid cycle totals on disk."""
        if kind not in ("P", "DP"):
            raise ValueError("进程组只支持 P 或 DP")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit 必须是正整数")
        fields = tuple(field for field in NUMERIC[kind] if field != "wait")
        with self.connect() as db:
            db.execute("PRAGMA temp_store=FILE")
            db.execute("PRAGMA temp.cache_size=-8192")
            db.execute("BEGIN")  # Configuration and records share one read snapshot.
            self._require_session(db, session_id)
            group = self._read_group(db, session_id, group_id)
            where, params = "session=? AND segment=?", [session_id, group["segment"]]
            for field, op in (("start", ">="), ("end", "<=")):
                if group[field] is not None:
                    where += " AND ts" + op + "?"
                    params.append(group[field])
            db.execute("CREATE TEMP TABLE group_scope AS SELECT cycle,ts,kind,pid,name,data "
                       "FROM records WHERE " + where, params)
            db.execute("CREATE TEMP TABLE group_members (pid INTEGER, name TEXT, PRIMARY KEY(pid,name))")
            db.executemany("INSERT INTO group_members VALUES (?,?)",
                           ((m["pid"], m["name"]) for m in group["members"]))
            columns = ",".join("MAX(json_extract(r.data,'$.%s')) AS %s" % (f, f) for f in fields)
            db.execute("""CREATE TEMP TABLE member_cycles AS SELECT r.cycle,r.pid,r.name,
                COUNT(*) n,""" + columns + """ FROM group_scope r JOIN group_members m
                ON r.pid=m.pid AND r.name=m.name WHERE r.kind=?
                GROUP BY r.cycle,r.pid,r.name""", (kind,))
            db.execute("CREATE INDEX temp.member_cycle_index ON member_cycles(cycle)")
            member_count = len(group["members"])
            complete = f"COUNT(m.pid)={member_count} AND MAX(m.n)=1"
            columns = ",".join(
                f"CASE WHEN {complete} AND COUNT(m.{f})={member_count} "
                f"THEN SUM(CAST(m.{f} AS REAL)) END AS {f}" for f in fields)
            db.execute("""CREATE TEMP TABLE group_totals AS
                SELECT c.cycle,c.ts,COUNT(m.pid) present,
                COALESCE(MAX(m.n)>1,0) duplicate,""" + complete + " AS complete," + columns + """
                FROM (SELECT cycle,MIN(ts) ts FROM group_scope GROUP BY cycle) c
                LEFT JOIN member_cycles m ON c.cycle=m.cycle GROUP BY c.cycle""")
            coverage = dict(db.execute("""SELECT COUNT(*) cycles,
                COALESCE(SUM(complete),0) complete,
                COALESCE(SUM(present>0 AND NOT complete),0) partial,
                COALESCE(SUM(present=0),0) missing,
                COALESCE(SUM(duplicate),0) duplicates FROM group_totals""").fetchone())
            note = ("样本数为所选 segment 和闭区间毫秒时间范围内全部记录周期数；"
                    "每项指标仅在全部成员各有且仅有一条同类记录且指标非空时合计，缺失不补零。"
                    "重复周期剔除；complete 仅表示记录齐全，不保证每项指标非空。"
                    "先按周期相加再统计，增量仅累计完整有效周期；wait 和分类字段不合计，"
                    "可查看单成员原始明细。不推断 PID 生命周期或自动识别 WebView。"
                    "DP 为低频采样，跨进程可能共享，不等同于系统 D 总量。")
            statistics = self._group_statistics(db, fields, kind, group, note)
            pairs = ",".join("'%s',%s" % (f, f) for f in ("ts",) + fields)
            cursor = db.execute("SELECT cycle AS id,cycle,? AS segment,json_object(" + pairs +
                                ") AS data FROM group_totals ORDER BY cycle", (group["segment"],))
            series = self._sample(cursor, coverage["cycles"], limit)
            return dict(group=group, statistics=statistics, series=series, coverage=coverage, note=note)

    @staticmethod
    def _group_statistics(db, fields, kind, group, note):
        bounds = dict(db.execute(
            "SELECT COUNT(*) samples,MIN(ts) start,MAX(ts) end FROM group_totals").fetchone())
        result = dict(bounds, method=note + METHOD, metrics={}, categories={},
                      scope=dict(kind=kind, pid=None, segment=group["segment"],
                                 name=group["name"], group_id=group["id"]))
        for field in fields:
            metric = dict(db.execute(f"""WITH ranked AS (
                SELECT {field} value,ROW_NUMBER() OVER (ORDER BY {field}) rn,
                COUNT(*) OVER () n FROM group_totals WHERE {field} IS NOT NULL
                ) SELECT COUNT(*) count,MIN(value) min,MAX(value) max,AVG(value) avg,
                MAX(CASE WHEN rn=(95*n+99)/100 THEN value END) p95,
                MAX(CASE WHEN rn=(99*n+99)/100 THEN value END) p99,
                SUM(CAST(value AS REAL)) total FROM ranked""").fetchone())
            if field not in INCREMENTS:
                metric.pop("total")
            result["metrics"][field] = dict(metric, **metadata(field))
        return result