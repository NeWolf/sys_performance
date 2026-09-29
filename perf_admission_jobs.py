"""Durable, bounded admission jobs independent of the business store."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading

from perf_admission import build_admission
from perf_joyspace import PUBLISH_UNKNOWN, _page_id, publish_markdown, read_template


# Shared across every manager in this interpreter, including different roots.
MAX_WORKERS = 2
MAX_OUTSTANDING = 10
_RUNNING = threading.BoundedSemaphore(MAX_WORKERS)
_CAPACITY = threading.BoundedSemaphore(MAX_OUTSTANDING)
_ID = re.compile(r"[0-9a-fA-F]{32}")


class AdmissionJobError(RuntimeError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise AdmissionJobError("会话和草稿 ID 必须是32位十六进制字符串。")
    return value.lower()


def _text(value):
    # Never forward exceptions, local paths, or credential-bearing URLs.
    value = str(value).replace("\x00", "")
    value = re.sub(r"(?:https?|file)://[^\s]+", "[链接已隐藏]", value, flags=re.I)
    return re.sub(r"(?<![\w])(?:[A-Za-z]:[\\/]|/)[^\s，；。<>\"']*",
                  "[路径已隐藏]", value)


class _Preview(HTMLParser):
    TAGS = frozenset("p table thead tbody tr th td strong em b i br h1 h2 h3 h4 ul ol li pre code hr".split())
    BLOCK = frozenset("script style iframe object embed svg math template".split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.blocked = []

    def handle_starttag(self, tag, attrs):
        if self.blocked:
            if tag in self.BLOCK:
                self.blocked.append(tag)
            return
        if tag in self.BLOCK:
            self.blocked.append(tag)
        elif tag in self.TAGS:
            self.parts.append("<" + tag + ">")
        elif tag == "img":
            src = dict(attrs).get("src", "")
            if re.fullmatch(r"data:image/png;base64,[A-Za-z0-9+/=]+", src):
                try:
                    data = base64.b64decode(src.split(",", 1)[1], validate=True)
                    if data.startswith(b"\x89PNG\r\n\x1a\n"):
                        self.parts.append('<img alt="Performance trend" src="' + src + '">')
                except ValueError:
                    pass

    def handle_endtag(self, tag):
        if self.blocked:
            if tag == self.blocked[-1]:
                self.blocked.pop()
        elif tag in self.TAGS and tag not in {"br", "hr"}:
            self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        if not self.blocked:
            # Report text is the content the user must review before publishing.
            # Escape it, but do not redact Android process paths or literal URLs.
            # Metadata/errors still use _text; active attributes are never copied.
            self.parts.append(html.escape(data.replace("\x00", "")))


def _preview(source):
    if not isinstance(source, str):
        raise ValueError("invalid preview")
    parser = _Preview()
    parser.feed(source)
    parser.close()
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'script-src \'none\'; img-src data:; style-src \'unsafe-inline\'; '
            'base-uri \'none\'; form-action \'none\'">'
            '<style>body{font:14px sans-serif;overflow-wrap:anywhere}'
            'table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px}'
            'img{max-width:100%}</style></head><body>'
            + "".join(parser.parts) + '</body></html>')


def _sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class _RootLock:
    def __init__(self, path):
        self.file = None
        try:
            if path.is_symlink():
                raise OSError("unsafe lock")
            self.file = open(path, "a+b")
            if os.name == "nt":
                import msvcrt
                self.file.seek(0, 2)
                if self.file.tell() == 0:
                    self.file.write(b"0")
                    self.file.flush()
                    os.fsync(self.file.fileno())
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            if self.file:
                self.file.close()
            raise AdmissionJobError("草稿目录不可用或已被其它进程占用。", 409) from None

    def close(self):
        # Closing the descriptor releases both Windows byte locks and flock.
        self.file.close()


class AdmissionJobs:
    def __init__(self, store, root):
        self._mutex = threading.RLock()
        self._close_mutex = threading.Lock()
        self._closed = False
        self._broken = False
        self._records = {}
        self._store = store
        self._db = None
        self._root_lock = None
        try:
            self._root = Path(root).resolve()
            self._root.mkdir(parents=True, exist_ok=True)
            self._root_lock = _RootLock(self._root / "admission.lock")
            db_path = self._root / "admission.sqlite3"
            if db_path.is_symlink():
                raise OSError("unsafe journal")
            self._db = sqlite3.connect(db_path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=PERSIST")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            self._db.commit()
            _sync_directory(self._root)
            _sync_directory(self._root.parent)
            for key, raw in self._db.execute("SELECT id, data FROM drafts"):
                record = json.loads(raw)
                if _id(key) != key or record["draft_id"] != key:
                    raise ValueError("invalid journal")
                _id(record["session_id"])
                if record["state"] not in {"generating", "ready", "publishing", "published", "unknown", "failed"}:
                    raise ValueError("invalid state")
                self._records[key] = record
            for record in tuple(self._records.values()):
                if record["state"] in {"generating", "publishing"}:
                    record = deepcopy(record)
                    publishing = record["state"] == "publishing"
                    record.update(state="unknown" if publishing else "failed",
                                  error=PUBLISH_UNKNOWN if publishing else "生成被中断；草稿不可重试。")
                    self._persist(record)
            self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="admission")
        except Exception:
            if self._db is not None:
                self._db.close()
            if self._root_lock is not None:
                self._root_lock.close()
            raise AdmissionJobError("草稿日志无法安全打开，或目录已被占用。", 503) from None

    def _persist(self, record):
        try:
            with self._db:
                self._db.execute("INSERT OR REPLACE INTO drafts VALUES (?, ?)",
                                 (record["draft_id"], json.dumps(record, ensure_ascii=True, allow_nan=False)))
        except Exception:
            self._broken = True
            raise AdmissionJobError("草稿日志写入失败；已停止新任务，请安全重启。", 503) from None
        self._records[record["draft_id"]] = record

    def _open(self, write=False):
        if self._closed or (write and self._broken):
            raise AdmissionJobError("草稿服务已关闭或日志不可用。", 503)

    @staticmethod
    def _meta(record):
        return deepcopy({key: record[key] for key in
                         ("draft_id", "state", "title", "warnings", "error", "link") if key in record})

    def _find(self, session_id, draft_id):
        record = self._records.get(draft_id)
        if record is None or record["session_id"] != session_id:
            raise AdmissionJobError("草稿不存在。", 404)
        return record

    def _unblocked(self, session_id):
        if any(r["session_id"] == session_id and r["state"] in {"unknown", "publishing"}
               for r in self._records.values()):
            raise AdmissionJobError("本会话存在发布中或结果未知的草稿；禁止新生成和其它发布。", 409)

    def list(self, session_id):
        session_id = _id(session_id)
        with self._mutex:
            self._open()
            return {"drafts": [self._meta(r) for r in self._records.values()
                               if r["session_id"] == session_id]}

    def get(self, session_id, draft_id):
        session_id, draft_id = _id(session_id), _id(draft_id)
        with self._mutex:
            self._open()
            return self._meta(self._find(session_id, draft_id))

    def create(self, session_id, draft_id, title, scenes, factor):
        session_id, draft_id = _id(session_id), _id(draft_id)
        if not isinstance(title, str) or not title.strip() or len(title) > 200 or "\x00" in title:
            raise AdmissionJobError("标题不能为空、含NUL或超过200字符。")
        if (not isinstance(scenes, dict) or any(type(k) is not int for k in scenes)
                or any(not isinstance(v, str) or v not in {"background", "foreground", "unknown"}
                       for v in scenes.values())):
            raise AdmissionJobError("场景必须是整数段号到background/foreground/unknown的映射。")
        try:
            valid = factor is None or (type(factor) in (int, float) and math.isfinite(factor) and factor > 0)
        except (OverflowError, ValueError):
            valid = False
        if not valid:
            raise AdmissionJobError("factor必须是正有限数值或None。")
        config = {"title": title, "scenes": [[k, v] for k, v in sorted(scenes.items())], "factor": factor}
        with self._mutex:
            self._open(write=True)
            existing = self._records.get(draft_id)
            if existing:
                if existing["session_id"] != session_id or existing["config"] != config:
                    raise AdmissionJobError("草稿ID已被不同会话或配置使用。", 409)
                return self._meta(existing)
            self._unblocked(session_id)
            record = dict(draft_id=draft_id, session_id=session_id, title=title,
                          config=config, state="generating", warnings=[])
            return self._enqueue(record, self._generate)

    def _enqueue(self, record, worker):
        if not _CAPACITY.acquire(blocking=False):
            raise AdmissionJobError("任务队列已满，请稍后提交新草稿。", 429)
        try:
            self._persist(record)
            # Capture the immediate response before workers can change the state.
            response = self._meta(record)
            self._executor.submit(self._run, worker, deepcopy(record))
            return response
        except AdmissionJobError:
            _CAPACITY.release()
            raise
        except Exception:
            _CAPACITY.release()
            if record["draft_id"] in self._records:
                record = deepcopy(record)
                record.update(state="failed", error="任务启动失败；草稿不可重试。")
                self._finish(record)
            raise AdmissionJobError("任务启动失败；草稿不可重试。", 503) from None

    def _run(self, worker, record):
        try:
            with _RUNNING:
                worker(record)
        finally:
            _CAPACITY.release()

    def _finish(self, record):
        with self._mutex:
            try:
                self._persist(record)
            except AdmissionJobError:
                # Keep a conservative in-memory result. Disk remains publishing
                # after remote calls, so recovery also cannot resend anything.
                if record["state"] in {"published", "unknown"}:
                    record.update(state="unknown", error=PUBLISH_UNKNOWN)
                    record.pop("link", None)
                else:
                    record.update(state="failed", error="草稿日志写入失败；请安全重启。")
                self._records[record["draft_id"]] = record

    def _directory(self, record):
        path = self._root / record["draft_id"]
        if path.is_symlink():
            raise ValueError("unsafe output")
        return path

    def _generate(self, record):
        try:
            directory = self._directory(record)
            directory.mkdir(exist_ok=True)
            result = build_admission(self._store, record["session_id"], read_template(), directory,
                                     dict(record["config"]["scenes"]), record["config"]["factor"])
            record["preview"] = _preview(result["html"])
            record["warnings"] = [_text(w) for w in result["warnings"]]
            # Flush every generated artifact before committing the ready state.
            if not (directory / "report.md").is_file():
                raise ValueError("missing report")
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_file():
                    raise ValueError("unsafe artifact")
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            _sync_directory(directory)
            _sync_directory(self._root)
            record["state"] = "ready"
        except BaseException:
            record.update(state="failed", error="草稿生成失败，请检查会话、场景配置及模板；此草稿不可重试。")
        self._finish(record)

    def preview(self, session_id, draft_id):
        session_id, draft_id = _id(session_id), _id(draft_id)
        with self._mutex:
            self._open()
            record = self._find(session_id, draft_id)
            if "preview" not in record:
                raise AdmissionJobError("草稿预览尚不可用。", 409)
            return _preview(record["preview"])

    def publish(self, session_id, draft_id, placement, parent_page, confirmed):
        session_id, draft_id = _id(session_id), _id(draft_id)
        if confirmed is not True:
            raise AdmissionJobError("发布必须显式确认。")
        if not isinstance(placement, str) or placement not in {"personal", "child", "sibling"}:
            raise AdmissionJobError("必须显式选择personal、child或sibling。")
        if placement == "personal":
            if parent_page is not None:
                raise AdmissionJobError("个人空间不接受目标页面。")
            target = None
        else:
            try:
                target = _page_id(parent_page)
            except Exception:
                raise AdmissionJobError("目标必须是有效JoySpace页面链接或页面ID。") from None
        request = {"placement": placement, "parent_page": target}
        with self._mutex:
            self._open(write=True)
            record = self._find(session_id, draft_id)
            if "publish_request" in record:
                if record["publish_request"] != request:
                    raise AdmissionJobError("该草稿已锁定发布位置，不能再次发布。", 409)
                return self._meta(record)
            self._unblocked(session_id)
            if record["state"] != "ready":
                raise AdmissionJobError("仅可发布已就绪且从未发布的草稿。", 409)
            record = deepcopy(record)
            record.update(state="publishing", publish_request=request)
            return self._enqueue(record, self._publish)

    def _publish(self, record):
        try:
            # Another worker's disk failure poisons all queued remote work too.
            with self._mutex:
                if self._broken:
                    raise AdmissionJobError("journal unavailable", 503)
            request = record["publish_request"]
            result = publish_markdown(self._directory(record) / "report.md", record["title"],
                                      request["placement"], request["parent_page"])
            page = result["pageId"]
            if not isinstance(page, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", page):
                raise ValueError("invalid result")
            record.update(state="published", link="https://joyspace.jd.com/pages/" + page)
        except BaseException:
            record.update(state="unknown", error=PUBLISH_UNKNOWN)
        self._finish(record)

    def close(self):
        with self._close_mutex:
            with self._mutex:
                if self._closed:
                    return
                self._closed = True
            # Do not hold the state mutex: workers need it to commit completion.
            self._executor.shutdown(wait=True)
            try:
                self._db.close()
            finally:
                self._root_lock.close()

