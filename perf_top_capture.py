"""Device-owned Top jobs; explicit, bounded local archives and imports."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import tempfile
import threading
import time
import uuid

from perf_adb import AdbError

MAX_ARCHIVE_BYTES = 1_000_000_000
CHUNK_BYTES = 64 * 1024
STOP_TIMEOUT_SECONDS = 20
STOP_POLL_SECONDS = 0.5
REMOTE_ROOT = "/data/local/tmp/sysmonitor_top_capture"
ID_PATTERN = r"[0-9a-f]{32}"
SERIAL_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,199}"


class TopCapture:
    def __init__(self, adb, archive, store, import_lock):
        self.adb = adb
        self.archive_root = Path(archive).resolve()
        self.store = store
        self.import_lock = import_lock
        self._lock = threading.RLock()
        self._closed = False
        self._import_done = threading.Event()
        self._import_done.set()
        self._capture_error = None
        self._archives = {}
        self._states = {}
        self._state = self._empty()
        self.script = Path(__file__).with_name("device_top_capture.sh")

    @staticmethod
    def _empty(serial=None):
        return dict(status="idle", running=False, stopping=False,
                    importing=False, count=0, target_count=0, interval=1,
                    serial=serial, title="", error=None, archive=None,
                    id=None, session_id=None, duplicate=False, bytes=0,
                    max_bytes=MAX_ARCHIVE_BYTES, remote_path=None,
                    connected=False, connection_error=None,
                    started_at=None, ended_at=None, elapsed_seconds=None,
                    remote_cleaned=False, warning=None)

    @staticmethod
    def _serial(serial):
        if not isinstance(serial, str) or not re.fullmatch(SERIAL_PATTERN, serial):
            raise AdbError("无效的设备序列号", 400)

    @staticmethod
    def _identity(capture_id):
        if not isinstance(capture_id, str) or not re.fullmatch(ID_PATTERN, capture_id):
            raise AdbError("无效的 Top 任务标识", 400)

    def _command(self, serial, action, *args, output=None, deadline=None):
        def remaining(limit):
            if deadline is None:
                return limit
            value = min(limit, deadline - time.monotonic())
            if value <= 0:
                raise AdbError("停止确认超时；任务可能仍在运行，请查询状态或重试停止。未拉取日志。", 504)
            return value

        def shell(command):
            if deadline is None:
                return self.adb.shell(serial, command)
            return self.adb.run("-s", serial, "shell", "-T", "sh -c " + shlex.quote(command),
                                timeout=remaining(10))

        # Normalize checkout/editor line endings before hashing AND uploading.
        # Android sh treats CR in CRLF as part of tokens (e.g. "077\r").
        # Content-addressed scripts never overwrite code being read by a worker.
        script_bytes = self.script.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest = hashlib.sha256(script_bytes).hexdigest()
        remote = REMOTE_ROOT + "/control-" + digest + ".sh"
        check = (f'[ ! -L {REMOTE_ROOT} ] && mkdir -p {REMOTE_ROOT} && '
                 f'if [ -f {remote} ] && [ ! -L {remote} ]; then echo ready; fi')
        if shell(check) != "ready":
            staging = remote + "." + uuid.uuid4().hex
            with tempfile.TemporaryDirectory(prefix="jdperf-top-script-") as temporary:
                local = Path(temporary) / "control.sh"
                # Binary write avoids Windows newline translation; close before
                # adb opens the file so Windows file sharing rules are respected.
                local.write_bytes(script_bytes)
                self.adb.run("-s", serial, "push", str(local), staging, timeout=remaining(30))
            shell(f"chmod 600 {staging} && mv {staging} {remote}")
        command = "sh " + shlex.quote(remote) + " " + " ".join(
            shlex.quote(str(value)) for value in (action, *args))
        if output is not None:
            return self.adb.run("-s", serial, "shell", "-T", "sh -c " + shlex.quote(command),
                                timeout=600, output=output)
        return shell(command)

    def _decode(self, serial, raw):
        if raw == "idle":
            return dict(self._empty(serial), connected=True)
        fields = raw.splitlines()
        unconfirmed = bool(fields and fields[-1] == "unconfirmed")
        if unconfirmed:
            fields.pop()
        remote_cleaned = bool(fields and fields[-1] == "cleaned")
        if remote_cleaned:
            fields.pop()
        if len(fields) == 7:
            fields.append("")  # adb.run strips the empty hex-encoded title.
        try:
            if len(fields) not in {8, 9, 11}:
                raise ValueError()
            started_at = ended_at = elapsed_seconds = None
            if len(fields) == 11:
                if any(not re.fullmatch(r"[0-9]{1,12}", value) for value in fields[8:]):
                    raise ValueError()
                started_at, ended_at, observed_at = map(int, fields[8:])
                elapsed_seconds = max(0, (ended_at or observed_at) - started_at) if started_at else None
                started_at = started_at or None
                ended_at = ended_at or None
            identity, status, count, target, interval, size, error, title = fields[:8]
            self._identity(identity)
            if status not in {"starting", "running", "stopped", "completed", "error"}:
                raise ValueError()
            if any(not re.fullmatch(r"[0-9]{1,10}", value) for value in (count, size)):
                raise ValueError()
            if not re.fullmatch(r"-1|[0-9]{1,6}", target):
                raise ValueError()
            if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", interval) or len(interval) > 24:
                raise ValueError()
            count, target, interval, size = int(count), int(target), float(interval), int(size)
            if not (target == -1 or 1 <= target <= 100000) or not 1 <= interval <= 3600:
                raise ValueError()
            if count > 10_000_000 or size > MAX_ARCHIVE_BYTES:
                raise ValueError()
            if error not in {"none", "size_limit", "top_failed_or_timeout", "sample_limit",
                             "invalid_top", "disk_error", "detach_failed", "interrupted"}:
                raise ValueError()
            if len(title) > 960 or not re.fullmatch(r"(?:[0-9a-f]{2})*", title):
                raise ValueError()
            title = bytes.fromhex(title).decode("utf-8")
            if len(title) > 120:
                raise ValueError()
        except (ValueError, AdbError) as exc:
            raise AdbError("设备 Top 状态无效，未执行任务操作", 502) from exc
        return dict(self._empty(serial), id=identity, status=status,
                    running=unconfirmed or status in {"starting", "running"}, count=count,
                    target_count=target, interval=interval, bytes=size, title=title,
                    connected=not unconfirmed, error=None if error == "none" else error,
                    connection_error=("无法确认旧任务已停止；请重试停止或查询状态，未拉取日志。"
                                      if unconfirmed else None),
                    started_at=started_at, ended_at=ended_at, elapsed_seconds=elapsed_seconds,
                    remote_cleaned=remote_cleaned,
                    remote_path=None if remote_cleaned else f"{REMOTE_ROOT}/{identity}/raw.txt")

    def _local_path(self, state):
        identity = state["id"]
        legacy = self.archive_root / (identity + ".txt")
        if legacy.exists() or legacy.is_symlink():
            return legacy
        directory = self.archive_root / identity
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise AdbError("本地归档目录无效，拒绝写入", 409)
        # Per-task directories prevent collisions between devices started in
        # the same second, while keeping the actual filename readable.
        existing = sorted(directory.glob("Top*.txt"))
        existing = [p for p in existing if re.fullmatch(r"Top[0-9]{14}\.txt", p.name)]
        if len(existing) > 1:
            raise AdbError("本地任务存在多个归档，拒绝自动选择", 409)
        if existing:
            return existing[0]
        try:
            stamp = time.strftime("%Y%m%d%H%M%S", time.localtime(state["started_at"] or time.time()))
            if not re.fullmatch(r"[0-9]{14}", stamp):
                raise ValueError()
        except (ValueError, OverflowError, OSError):
            stamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
        return directory / ("Top" + stamp + ".txt")

    @staticmethod
    def _size_warning(size, before, after):
        return (
            f"Top 归档字节数不一致：本地实际={size} 字节，"
            f"拉取前={before} 字节，拉取后={after} 字节。"
            "本地归档已保留，可继续分析或下载；数据可能不完整，请留意分析结果。")

    def _saved_warning(self, path, size):
        marker = path.with_suffix(".warning.json")
        if marker.is_symlink() or not marker.is_file():
            return None
        try:
            with marker.open("rb") as stream:
                data = json.loads(stream.read(1024))
            if (not isinstance(data, dict) or set(data) != {"size", "before", "after"}
                    or any(type(value) is not int or not 0 <= value <= MAX_ARCHIVE_BYTES
                           for value in data.values()) or data["size"] != size):
                return None
            return self._size_warning(size, data["before"], data["after"])
        except (OSError, ValueError):
            return None

    def _query(self, serial, deadline=None):
        state = self._decode(serial, self._command(serial, "status", deadline=deadline))
        old = self._states.get(serial, {})
        if old.get("id") == state["id"] and state["id"]:
            for key in ("archive", "session_id", "duplicate", "importing", "warning"):
                state[key] = old[key]
            if old["status"] in {"imported", "importing"}:
                state["status"] = old["status"]
        if state["id"]:
            path = self._local_path(state)
            if not state["running"] and path.is_file() and not path.is_symlink():
                size = path.stat().st_size
                saved_warning = self._saved_warning(path, size)
                if (0 < size <= MAX_ARCHIVE_BYTES
                        and (size == state["bytes"] or saved_warning
                             or state["id"] in self._archives)):
                    self._archives[state["id"]] = path
                    state["archive"] = "/api/top/archive?capture_id=" + state["id"]
                    state["warning"] = state["warning"] or saved_warning
                    if size != state["bytes"] and not state["warning"]:
                        state["warning"] = (
                            f"Top 归档字节数不一致：本地实际={size} 字节，"
                            f"设备记录={state['bytes']} 字节。"
                            "本地归档已保留，可继续分析或下载；数据可能不完整，请留意分析结果。")
        if old.get("importing") and old.get("id") == state["id"]:
            old.update(state)
            state = old
        self._states[serial] = state
        # A new task (even on the same device) must not replace an import.
        if not self._state["importing"] or self._state is state:
            self._state = state
            self._capture_error = state["error"]
        return dict(state)

    def status(self, serial=None):
        with self._lock:
            if serial is None:
                return dict(self._state)
            self._serial(serial)
            try:
                with self.adb.device(serial):
                    return self._query(serial)
            except AdbError as exc:
                state = self._states.setdefault(serial, self._empty(serial))
                state.update(connected=False, connection_error=str(exc))
                if not self._state["importing"]:
                    self._state = state
                return dict(state)

    def _busy(self):
        return self._state["running"] or self._state["importing"]

    def start(self, serial, count=-1, interval=1, title=""):
        self._serial(serial)
        if type(count) is not int or not (count == -1 or 1 <= count <= 100000):
            raise ValueError("采样次数必须为 -1 或 1 至 100000 的整数")
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or not 1 <= interval <= 3600):
            raise ValueError("采样间隔必须为 1 至 3600 秒的有限数")
        if not isinstance(title, str) or len(title) > 120:
            raise ValueError("会话名称最多 120 个字符")
        with self._lock:
            self._available()
            with self.adb.device(serial):
                state = self._query(serial)
                if state["running"]:
                    raise AdbError("设备 Top 任务尚未停止", 409)
                identity = uuid.uuid4().hex
                self._command(serial, "start", identity, count, format(interval, ".15f").rstrip("0").rstrip("."),
                              title.strip().encode("utf-8").hex())
                result = self._query(serial)
                if result["id"] != identity or result["status"] in {"starting", "error"}:
                    raise AdbError("设备独立采集启动失败，请刷新设备状态", 502)
                return result

    def _available(self):
        if self._closed:
            raise AdbError("应用正在关闭", 503)
        if self._state["importing"]:
            raise AdbError("Top 导入中，请稍后重试", 409)

    def _current(self, serial, capture_id):
        state = self._query(serial)
        if state["id"] != capture_id:
            raise AdbError("Top 任务已切换，请刷新状态后重试", 409)
        return state

    def _stop_confirmed(self, serial, capture_id):
        # Send the marker before status: a legacy recovery scan may itself fail.
        # The device validates identity; never signal a guessed process/PID.
        cached = self._states.get(serial)
        if cached and cached.get("id") and cached["id"] != capture_id:
            raise AdbError("Top 任务已切换，请刷新状态后重试", 409)
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        try:
            self._command(serial, "stop", capture_id, deadline=deadline)
            while True:
                state = self._query(serial, deadline=deadline)
                if state["id"] != capture_id:
                    raise AdbError("Top 任务已切换，请刷新状态后重试", 409)
                if not state["running"]:
                    return state
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdbError("停止确认超时；任务可能仍在运行，请查询状态或重试停止。未拉取日志。", 504)
                time.sleep(min(STOP_POLL_SECONDS, remaining))
        except AdbError as exc:
            cached = self._states.get(serial)
            if cached and cached["id"] == capture_id:
                cached.update(stopping=False, connected=False,
                              connection_error="停止未确认，可重试停止或查询状态：" + str(exc))
                if cached["status"] == "stopping":
                    cached["status"] = "running"
            raise

    def stop(self, serial, capture_id):
        self._serial(serial)
        self._identity(capture_id)
        with self._lock:
            self._available()
            with self.adb.device(serial):
                return self._stop_confirmed(serial, capture_id)

    def clear(self, serial, capture_id):
        self._serial(serial)
        self._identity(capture_id)
        with self._lock:
            self._available()
            with self.adb.device(serial):
                state = self._current(serial, capture_id)
                if state["running"] or state["stopping"]:
                    raise AdbError("请先停止 Top 采集并等待结束，再清理设备日志", 409)
                self._command(serial, "clear", capture_id)
                return self._query(serial)

    def pull(self, serial, capture_id):
        self._serial(serial)
        self._identity(capture_id)
        with self._lock:
            self._available()
            with self.adb.device(serial):
                state = self._stop_confirmed(serial, capture_id)
                if state["remote_cleaned"]:
                    raise AdbError("设备 Top 日志已清理，本地已拉取的归档不受影响", 409)
                if state["bytes"] == 0:
                    raise AdbError("设备没有完整的 Top 样本", 404)
                self.archive_root.mkdir(parents=True, exist_ok=True)
                path = self._local_path(state)
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() or path.is_symlink():
                    if (capture_id not in self._archives or path.is_symlink()
                            or not path.is_file() or not 0 < path.stat().st_size <= MAX_ARCHIVE_BYTES):
                        raise AdbError("本地归档目标已存在，拒绝覆盖", 409)
                    return dict(self._state)
                # Anonymous staging is never exposed through the archive API.
                with tempfile.TemporaryFile(dir=self.archive_root) as staging:
                    self._command(serial, "pull", capture_id, output=staging)
                    # A child's writes need not advance the parent's stream position.
                    # Measure bytes on disk, including any buffered test/wrapper writes.
                    staging.flush()
                    size = os.fstat(staging.fileno()).st_size
                    if size > MAX_ARCHIVE_BYTES:
                        raise AdbError("Top 原始归档超过 1GB（1000000000 字节）", 413)
                    after = self._current(serial, capture_id)
                    if after["running"] or after["remote_cleaned"]:
                        # Only validated protocol fields and the on-disk size: never
                        # expose archive contents, host paths or command output.
                        raise AdbError(
                            "设备归档长度或状态变化，未发布本地文件；"
                            f"本地实际={size} 字节，拉取前={state['bytes']} 字节，"
                            f"拉取后={after['bytes']} 字节；"
                            f"拉取前状态={state['status']}，拉取后状态={after['status']}；"
                            f"拉取后仍运行={'是' if after['running'] else '否'}，"
                            f"拉取后已清理={'是' if after['remote_cleaned'] else '否'}。"
                            "请保留设备日志，并反馈此完整错误信息。", 502)
                    if size == 0:
                        raise AdbError("拉取到的 Top 日志为空，未发布本地文件，请重试拉取", 502)
                    warning = None
                    if size != state["bytes"] or size != after["bytes"]:
                        warning = self._size_warning(size, state["bytes"], after["bytes"])
                    os.fsync(staging.fileno())
                    staging.seek(0)
                    # Hard-link publication is atomic and never overwrites a success.
                    pending = self.archive_root / (capture_id + "." + uuid.uuid4().hex + ".part")
                    created = False
                    marker_created = False
                    published = False
                    marker = path.with_suffix(".warning.json")
                    try:
                        with pending.open("xb") as output:
                            created = True
                            for block in iter(lambda: staging.read(CHUNK_BYTES), b""):
                                self._write_all(output, block)
                            output.flush()
                            os.fsync(output.fileno())
                        if warning:
                            # Persist validated numeric diagnostics before publishing
                            # the archive, so restart recovery can identify it.
                            if marker.exists() or marker.is_symlink():
                                if self._saved_warning(path, size) != warning:
                                    raise AdbError("本地归档提示记录已存在，拒绝覆盖", 409)
                            else:
                                with marker.open("x", encoding="utf-8") as metadata:
                                    marker_created = True
                                    json.dump({"size": size, "before": state["bytes"],
                                               "after": after["bytes"]}, metadata)
                                    metadata.flush()
                                    os.fsync(metadata.fileno())
                        try:
                            os.link(pending, path)
                            published = True
                        except FileExistsError as exc:
                            raise AdbError("本地归档目标已存在，拒绝覆盖", 409) from exc
                    finally:
                        # Never remove pre-existing metadata or published archives.
                        if marker_created and not published:
                            marker.unlink(missing_ok=True)
                        if created:
                            pending.unlink(missing_ok=True)
                self._archives[capture_id] = path
                self._state["archive"] = "/api/top/archive?capture_id=" + capture_id
                self._state["warning"] = warning
                return dict(self._state)

    def close(self):
        # Device jobs deliberately survive local service shutdown.
        with self._lock:
            self._closed = True
        self._import_done.wait()

    @staticmethod
    def _write_all(stream, data):
        remaining = memoryview(data)
        while remaining:
            written = stream.write(remaining)
            if not written:
                raise OSError("归档写入未完成")
            remaining = remaining[written:]

    def archive_path(self, capture_id=None):
        with self._lock:
            return self._archive_path_locked(capture_id)

    @staticmethod
    def archive_filename(path):
        if re.fullmatch(r"Top[0-9]{14}\.txt", path.name):
            return path.name
        # Preserve legacy files in place but expose a readable name.
        return "Top" + time.strftime("%Y%m%d%H%M%S", time.localtime(path.stat().st_mtime)) + ".txt"

    def _archive_path_locked(self, capture_id=None):
        capture_id = self._state["id"] if capture_id is None else capture_id
        if capture_id is not None:
            self._identity(capture_id)
        if capture_id == self._state["id"] and self._state["running"]:
            raise AdbError("Top 采集尚未结束，请停止后拉取", 409)
        path = self._archives.get(capture_id)
        if (path is None or path.is_symlink() or not path.is_file()
                or path.parent.is_symlink()
                or path.resolve().parent not in (self.archive_root, self.archive_root / capture_id)
                or not 0 < path.stat().st_size <= MAX_ARCHIVE_BYTES):
            raise AdbError("没有可用的本地 Top 归档，请先显式拉取", 404)
        return path

    def import_capture(self, capture_id=None):
        with self._lock:
            self._available()
            if self._busy():
                raise AdbError("Top 任务运行、停止或导入中，请稍后重试", 409)
            if capture_id is not None and capture_id != self._state["id"]:
                raise AdbError("Top 任务已切换，请刷新状态后重试", 409)
            path = self._archive_path_locked(capture_id)
            if self._state["session_id"] is not None:
                return {"id": self._state["session_id"], "duplicate": True}
            if not self.import_lock.acquire(blocking=False):
                raise AdbError("已有导入正在进行，请稍后重试", 409)
            title = self._state["title"]
            serial = self._state["serial"]
            self._import_done.clear()
            self._state.update(importing=True, status="importing", error=self._capture_error)
        try:
            with path.open("rb") as stream:
                result = self.store.import_files([(self.archive_filename(path), stream)], title)
            with self._lock:
                self._state.update(session_id=result["id"], duplicate=result["duplicate"],
                                   status="imported", error=self._capture_error)
            return dict(result)
        except Exception as exc:
            if isinstance(exc, OSError):
                exc = AdbError("本地 Top 归档读取失败，请检查磁盘与权限后重试", 503)
            with self._lock:
                self._state.update(status="error", error="导入失败，可重试：" + str(exc))
            raise exc
        finally:
            with self._lock:
                self.import_lock.release()
                self._state["importing"] = False
                cached = self._states.get(serial)
                if cached is None or cached["id"] == self._state["id"]:
                    self._states[serial] = self._state
                self._import_done.set()