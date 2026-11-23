"""Explicit archive ownership and synchronous storage maintenance.

Call the public maintenance methods in a worker thread, not an async event loop.
Only registered archives are ever considered for removal.
"""
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
from contextlib import contextmanager
from pathlib import Path


class StorageBusy(RuntimeError):
    """Another connection or maintenance operation is active; retry later."""


class StorageInsufficientSpace(RuntimeError):
    def __init__(self, required_bytes, free_bytes):
        self.required_bytes = required_bytes
        self.free_bytes = free_bytes
        super().__init__(f"磁盘空间不足：需要 {required_bytes} 字节，可用 {free_bytes} 字节")


class _Gate:
    def __init__(self):
        self.lock = threading.RLock()
        self.active = 0
        self.owner = None
        self.depth = 0


_GATES = {}
_GATES_LOCK = threading.Lock()
_ROOTS = {"adb_logs", "top_logs", "qnx_logs"}
_UUID = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")


class StorageMixin:
    def _init_storage_gate(self):
        key = str(self.path.resolve())
        with _GATES_LOCK:
            self._storage_gate = _GATES.setdefault(key, _Gate())

    @contextmanager
    def _connection_slot(self):
        gate = self._storage_gate
        with gate.lock:
            if gate.owner is not None and gate.owner != threading.get_ident():
                raise StorageBusy("存储正在维护，请稍后重试")
            gate.active += 1
        try:
            yield
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise StorageBusy("数据库忙，请稍后重试") from exc
            raise
        finally:
            with gate.lock:
                gate.active -= 1

    @contextmanager
    def _storage_maintenance(self):
        gate = self._storage_gate
        tid = threading.get_ident()
        with gate.lock:
            if gate.owner == tid:
                gate.depth += 1
            elif gate.owner is not None or gate.active:
                raise StorageBusy("有活动数据库连接，请稍后重试")
            else:
                gate.owner, gate.depth = tid, 1
        try:
            yield
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise StorageBusy("数据库忙，请稍后重试") from exc
            raise
        finally:
            with gate.lock:
                gate.depth -= 1
                if not gate.depth:
                    gate.owner = None

    @staticmethod
    def _create_storage_schema(db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS storage_assets (
                path TEXT PRIMARY KEY,
                kind TEXT NOT NULL CHECK(kind IN ('file','directory')),
                state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','pending')),
                last_error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS session_logs (
                session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                path TEXT NOT NULL REFERENCES storage_assets(path),
                PRIMARY KEY(session,path)
            );
            CREATE INDEX IF NOT EXISTS session_logs_path ON session_logs(path);
        """)

    @staticmethod
    def _walk_error(error):
        # Never mistake an unreadable subtree for an empty/safe directory.
        raise error

    def _managed_path(self, value, *, missing_ok=False, kind=None):
        if not isinstance(value, (str, os.PathLike)):
            raise ValueError("日志路径必须是文件路径")
        path = Path(value)
        if '..' in path.parts:
            raise ValueError("日志路径不允许上级目录")
        base = self.path.parent.resolve()
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            relative = path.relative_to(base)
        except ValueError:
            try:
                relative = path.relative_to(self.path.parent.absolute())
            except ValueError as exc:
                raise ValueError("日志不在受管目录中") from exc
        path = base / relative
        if len(relative.parts) < 2 or relative.parts[0] not in _ROOTS:
            raise ValueError("只能关联受管目录内的日志，不能关联根目录")
        current = base
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("日志路径不允许符号链接")
        if not path.exists():
            if missing_ok:
                return path, kind
            raise ValueError("日志路径不存在")
        if path.is_dir():
            if len(relative.parts) != 2 or not _UUID.fullmatch(path.name):
                raise ValueError("只能关联单个采集 UUID 目录")
            for root, dirs, files in os.walk(path, followlinks=False, onerror=StorageMixin._walk_error):
                for name in dirs + files:
                    item = Path(root) / name
                    mode = item.lstat().st_mode
                    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                        raise ValueError("采集目录包含符号链接或特殊文件")
            actual_kind = 'directory'
        elif path.is_file():
            actual_kind = 'file'
        else:
            raise ValueError("日志必须是普通文件或采集目录")
        if kind is not None and kind != actual_kind:
            raise ValueError("受管资产类型发生变化")
        return path, actual_kind

    def _register_session_logs(self, db, session_id, paths):
        if db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
            raise KeyError(session_id)
        validated = [self._managed_path(path) for path in paths]
        for path, kind in validated:
            db.execute("INSERT INTO storage_assets(path,kind) VALUES (?,?) "
                       "ON CONFLICT(path) DO UPDATE SET state='active',last_error='' "
                       "WHERE storage_assets.kind=excluded.kind", (str(path), kind))
            asset = db.execute("SELECT kind FROM storage_assets WHERE path=?", (str(path),)).fetchone()
            if asset['kind'] != kind:
                raise ValueError("受管资产类型发生变化")
            db.execute("INSERT OR IGNORE INTO session_logs VALUES (?,?)", (session_id, str(path)))
        return {"session_id": session_id, "paths": sorted({str(p) for p, _ in validated})}

    def register_session_logs(self, session_id, paths):
        if isinstance(paths, (str, bytes, os.PathLike)):
            raise ValueError("paths 必须是路径列表")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._register_session_logs(db, session_id, paths)

    def _stream_log_paths(self, sources):
        paths = []
        for _, stream in sources:
            # Only capture call sites opt in; never infer ownership from source labels.
            directory = getattr(stream, '_capture_log_directory', None)
            try:
                path, kind = self._managed_path(stream.name)
                opened = os.fstat(stream.fileno())
                actual = path.stat()
                if kind != 'file' or not stat.S_ISREG(opened.st_mode) or not os.path.samestat(opened, actual):
                    raise ValueError("日志流与受管文件不一致")
            except (AttributeError, TypeError, ValueError, OSError):
                if directory is not None:
                    raise ValueError("采集日志必须是真实受管文件")
                continue  # Upload buffers/descriptors and external files are never managed.
            if directory is not None:
                directory, kind = self._managed_path(directory)
                if kind != 'directory' or directory not in path.parents:
                    raise ValueError("采集日志不属于指定 UUID 目录")
                paths.append(directory)
            else:
                paths.append(path)
        return paths

    @staticmethod
    def _path_bytes(path):
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        return sum((Path(root) / name).stat().st_size
                   for root, _, files in os.walk(path, followlinks=False, onerror=StorageMixin._walk_error) for name in files)

    @staticmethod
    def _overlaps(left, right):
        return left == right or left in right.parents or right in left.parents

    @staticmethod
    def _remove_asset(path, kind):
        if kind == 'directory':
            shutil.rmtree(path)
        else:
            path.unlink()

    def _cleanup_pending(self):
        freed, warnings = 0, []
        with self.connect() as db:
            pending = db.execute("SELECT path FROM storage_assets WHERE state='pending'").fetchall()
        for candidate in pending:
            # Serialize reference rechecks and removal across app instances too.
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                asset = db.execute("SELECT * FROM storage_assets WHERE path=? AND state='pending'",
                                   (candidate['path'],)).fetchone()
                if asset is None:
                    continue
                path = Path(asset['path'])
                live = [Path(r[0]) for r in db.execute("SELECT DISTINCT path FROM session_logs")]
                if any(self._overlaps(path, item) for item in live):
                    warnings.append(f"共享日志暂不清理：{path}")
                    continue
                try:
                    path, kind = self._managed_path(path, missing_ok=True, kind=asset['kind'])
                    before = self._path_bytes(path)
                    if path.exists():
                        self._remove_asset(path, kind)
                except (OSError, ValueError) as exc:
                    warnings.append(f"日志清理失败，已保留待重试记录：{path}：{exc}")
                    db.execute("UPDATE storage_assets SET last_error=?,attempts=attempts+1 WHERE path=?",
                               (str(exc), str(path)))
                    continue
                freed += before
                db.execute("DELETE FROM storage_assets WHERE path=? AND state='pending'", (str(path),))
        return {"logs_freed_bytes": freed, "warnings": warnings}

    def retry_pending_cleanup(self):
        with self._storage_maintenance():
            return self._cleanup_pending()

    def delete_many(self, session_ids):
        if isinstance(session_ids, (str, bytes)):
            raise ValueError("session_ids 必须是 ID 列表")
        ids = list(dict.fromkeys(session_ids))
        if any(not isinstance(sid, str) or not sid for sid in ids):
            raise ValueError("会话 ID 必须是非空字符串")
        deleted, missing = [], []
        with self._storage_maintenance():
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for sid in ids:
                    exists = db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
                    if exists is None:
                        missing.append(sid)
                        continue
                    db.execute("UPDATE storage_assets SET state='pending' WHERE path IN "
                               "(SELECT path FROM session_logs WHERE session=?)", (sid,))
                    db.execute("DELETE FROM sessions WHERE id=?", (sid,))
                    deleted.append(sid)
                db.execute("UPDATE storage_assets SET state='active' WHERE EXISTS "
                           "(SELECT 1 FROM session_logs WHERE session_logs.path=storage_assets.path)")
            result = self._cleanup_pending()
        return dict(result, deleted_ids=deleted, missing_ids=missing)

    def _database_sizes(self):
        def size(path):
            try:
                return path.stat().st_size
            except FileNotFoundError:
                return 0
        return {"database_bytes": size(self.path),
                "wal_bytes": size(Path(str(self.path) + '-wal')),
                "shm_bytes": size(Path(str(self.path) + '-shm'))}

    @staticmethod
    def _root_paths(paths):
        """Count a registered directory and its registered children only once."""
        roots = []
        for path in sorted(set(paths), key=lambda p: len(p.parts)):
            if not any(parent == path or parent in path.parents for parent in roots):
                roots.append(path)
        return roots

    def storage_usage(self):
        # Read-only statistics may overlap ordinary queries; the connection slot
        # still prevents destructive maintenance until this snapshot is closed.
        with self.connect() as db:
            db.execute('BEGIN')
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            reclaimable = db.execute('PRAGMA freelist_count').fetchone()[0] * page_size
            rows = db.execute('SELECT id,summary FROM sessions').fetchall()
            assets = db.execute('SELECT * FROM storage_assets').fetchall()
            links = {}
            for row in db.execute('SELECT session,path FROM session_logs'):
                links.setdefault(row['session'], []).append(Path(row['path']))
            warnings, sizes = [], {}
            for asset in assets:
                path = Path(asset['path'])
                try:
                    self._managed_path(path, missing_ok=True, kind=asset['kind'])
                    sizes[path] = self._path_bytes(path)
                except (OSError, ValueError) as exc:
                    sizes[path] = 0
                    warnings.append(f'无法统计受管日志：{path}：{exc}')
            # Payload lengths are exact; page and b-tree overhead is estimated.
            # In particular, never scan the complete records table.
            caches = {}
            for table in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                name = table[0]
                if name in ('records', 'session_logs'):
                    continue
                quoted = '"' + name.replace('"', '""') + '"'
                columns = db.execute(f'PRAGMA table_info({quoted})').fetchall()
                if not any(c['name'] == 'session' for c in columns):
                    continue
                values = ['COALESCE(length(CAST("' + c['name'].replace('"', '""') +
                          '" AS BLOB)),0)' for c in columns if c['name'] != 'session']
                expression = '+'.join(values) or '0'
                for cache in db.execute(f'SELECT session,SUM({expression}) FROM {quoted} GROUP BY session'):
                    caches[cache[0]] = caches.get(cache[0], 0) + (cache[1] or 0)
            sessions = []
            for row in rows:
                sid, raw = row['id'], row['summary']
                summary = json.loads(raw)
                count = sum(summary.get('counts', {}).values())
                samples = db.execute(
                    'SELECT length(CAST(data AS BLOB))+COALESCE(length(CAST(name AS BLOB)),0)'
                    '+COALESCE(length(CAST(source AS BLOB)),0) FROM records WHERE session=? LIMIT 128',
                    (sid,)).fetchall()
                average = sum(r[0] or 0 for r in samples) / len(samples) if samples else 0
                # Header/scalars plus the three session-prefixed indexes.
                estimate = int(max(count, len(samples)) * (average + 96 + 4 * len(sid.encode())))
                estimate += caches.get(sid, 0) + len(raw.encode()) + 128
                logs = sum(sizes[p] for p in self._root_paths(links.get(sid, [])))
                sessions.append(dict(id=sid, database_bytes=estimate, logs_bytes=logs,
                                     total_bytes=estimate + logs, estimated=True,
                                     cache_bytes=caches.get(sid, 0),
                                     unmanaged_logs=not bool(links.get(sid))))
            if any(s['unmanaged_logs'] for s in sessions):
                warnings.append('部分会话没有日志归属记录；外部或历史未关联日志不会自动清理。')
            physical = self._database_sizes()
            logs_bytes = sum(sizes[p] for p in self._root_paths(sizes))
            return dict(physical, free_bytes=shutil.disk_usage(self.path.parent).free,
                        reclaimable_bytes=reclaimable, sessions=sessions,
                        logs_bytes=logs_bytes,
                        total_bytes=physical['database_bytes'] + physical['wal_bytes'] + logs_bytes,
                        pending_cleanup_count=sum(a['state'] == 'pending' for a in assets),
                        unmanaged_logs=any(s['unmanaged_logs'] for s in sessions),
                        estimated=True, warnings=warnings,
                        estimate_method='每会话最多128条记录抽样×summary计数，加索引估算和精确缓存载荷；共享日志不重复计入全局总量')

    def reclaim_space(self):
        with self._storage_maintenance(), self.connect() as db:
            before = self._database_sizes()
            before_bytes = before['database_bytes'] + before['wal_bytes']
            if not db.execute('PRAGMA freelist_count').fetchone()[0]:
                return dict(before_bytes=before_bytes, after_bytes=before_bytes,
                            reclaimed_bytes=0, skipped=True)
            page_bytes = (db.execute('PRAGMA page_count').fetchone()[0]
                          * db.execute('PRAGMA page_size').fetchone()[0])
            required = 2 * max(before['database_bytes'], page_bytes) + before['wal_bytes'] + 64 * 1024 * 1024
            free = shutil.disk_usage(self.path.parent).free
            if free < required:
                raise StorageInsufficientSpace(required, free)
            checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if checkpoint[0]:
                raise StorageBusy('WAL仍被其他连接使用，请稍后重试')
            db.execute('VACUUM')
            checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if checkpoint[0]:
                raise StorageBusy('空间回收完成，但WAL仍被其他连接使用')
            after = self._database_sizes()
            after_bytes = after['database_bytes'] + after['wal_bytes']
            return dict(before_bytes=before_bytes, after_bytes=after_bytes,
                        reclaimed_bytes=max(0, before_bytes - after_bytes), skipped=False)