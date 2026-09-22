"""Rootless, bounded Top capture with explicit import and durable raw archives."""
from datetime import datetime
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid

from perf_adb import AdbError
from perf_top_parser import top_header_fields

MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_SAMPLE_BYTES = 16 * 1024 * 1024
TOP_TIMEOUT = 10
CHUNK_BYTES = 64 * 1024


class TopCapture:
    def __init__(self, adb, archive, store, import_lock):
        self.adb = adb
        self.archive_root = Path(archive).resolve()
        self.store = store
        self.import_lock = import_lock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._closed = False
        self._import_done = threading.Event()
        self._import_done.set()
        self._capture_error = None
        self._archives = {}
        self._state = dict(status="idle", running=False, stopping=False,
                           importing=False, count=0, target_count=0, interval=1,
                           serial=None, title="", error=None, archive=None,
                           id=None, session_id=None, duplicate=False, bytes=0)

    def status(self):
        with self._lock:
            return dict(self._state)

    def _busy(self):
        return (self._state["running"] or self._state["importing"]
                or (self._thread is not None and self._thread.is_alive()))

    def start(self, serial, count=-1, interval=1, title=""):
        if type(count) is not int or not (count == -1 or 1 <= count <= 100000):
            raise ValueError("采样次数必须为 -1 或 1 至 100000 的整数")
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not math.isfinite(interval) or not 1 <= interval <= 3600):
            raise ValueError("采样间隔必须为 1 至 3600 秒的有限数")
        if not isinstance(title, str) or len(title) > 120:
            raise ValueError("会话名称最多 120 个字符")
        if not isinstance(serial, str):
            raise ValueError("无效的设备序列号")
        ready, startup_errors = threading.Event(), []
        with self._lock:
            if self._closed:
                raise AdbError("应用正在关闭", 503)
            if self._busy():
                raise AdbError("Top 任务运行、停止或导入中，请稍后重试", 409)
            self._stop.clear()
            self._capture_error = None
            self._state.update(status="starting", running=True, stopping=False,
                               importing=False, count=0, target_count=count,
                               interval=interval, serial=serial, title=title.strip(),
                               error=None, archive=None, id=uuid.uuid4().hex,
                               session_id=None, duplicate=False, bytes=0)
            self._thread = threading.Thread(target=self._capture,
                                            args=(ready, startup_errors),
                                            name="top-capture", daemon=False)
            try:
                self._thread.start()
            except Exception as exc:
                self._state.update(status="error", running=False, error=str(exc))
                raise ValueError("无法启动 Top 采集线程") from exc
        # Device validation errors are returned to the start caller, not hidden.
        ready.wait()
        if startup_errors:
            raise startup_errors[0]
        return self.status()

    def stop(self):
        with self._lock:
            if self._state["running"]:
                self._stop.set()
                self._state.update(status="stopping", stopping=True)
            return dict(self._state)

    def close(self):
        with self._lock:
            self._closed = True
            self._stop.set()
            if self._state["running"]:
                self._state.update(status="stopping", stopping=True)
            thread = self._thread
        if thread is not None and thread.ident is not None:
            thread.join()
        self._import_done.wait()

    def _sample(self, serial, output):
        """Bound both stdout and stderr, including when adb stalls mid-line."""
        if not self.adb.executable:
            raise AdbError("未找到 adb，请安装 Android platform-tools", 503)
        command = [self.adb.executable, "-s", serial, "shell", "-T",
                   "top", "-b", "-n", "1", "-d", "1"]
        expired = threading.Event()
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       shell=False)
        except OSError as exc:
            raise AdbError("无法执行 ADB，请检查安装及本机文件权限", 503) from exc

        def timeout():
            if process.poll() is None:
                expired.set()
                try:
                    process.kill()
                except OSError:
                    pass

        timer = threading.Timer(TOP_TIMEOUT, timeout)
        size = 0
        try:
            timer.start()
            while True:
                block = process.stdout.read1(CHUNK_BYTES)
                if not block:
                    break
                size += len(block)
                if size > MAX_SAMPLE_BYTES:
                    raise ValueError("单次 Top 输出超过 16 MiB，已停止；已有样本保留")
                output.write(block)
            process.wait()
            if expired.is_set():
                raise AdbError("Top 采集超时（10 秒）；已有样本保留", 504)
            if process.returncode:
                output.seek(0)
                message = output.read(600).decode("utf-8", "replace").strip()
                raise AdbError("Top 执行失败：" + (message or "请检查设备连接与 top 支持"), 502)
            output.seek(0)
            if not self._valid_top(output):
                raise ValueError("Top 输出为空或不是有效进程列表；已有样本保留")
            output.seek(0)
            return size
        finally:
            timer.cancel()
            if timer.ident is not None:
                timer.join()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()

    @staticmethod
    def _valid_top(output):
        columns = None
        while True:
            line = output.readline(CHUNK_BYTES)
            if not line:
                return False
            text = line.decode("utf-8", "replace")
            fields = text.split()
            upper = top_header_fields(text)
            if upper:
                columns = None
                if (any(item in upper for item in ("%CPU", "CPU%"))
                        and any(item in upper for item in ("ARGS", "CMD", "COMMAND", "NAME"))):
                    cpu_column = next(i for i, name in enumerate(upper) if name in ("%CPU", "CPU%"))
                    columns = upper.index("PID"), cpu_column, len(upper)
            elif columns is not None:
                pid_column, cpu_column, width = columns
                if len(fields) < width or not fields[pid_column].isdigit():
                    continue
                try:
                    cpu = float(fields[cpu_column].rstrip("%"))
                except ValueError:
                    continue
                if math.isfinite(cpu) and cpu >= 0:
                    return True

    def _capture(self, ready, startup_errors):
        """Own the device lock until the archive is closed, including on errors."""
        state = self.status()
        path = self.archive_root / (state["id"] + ".txt")
        error = None
        try:
            with self.adb.device(state["serial"]):
                self.archive_root.mkdir(parents=True, exist_ok=True)
                with path.open("x+b", buffering=0) as archive:
                    with self._lock:
                        if not self._stop.is_set():
                            self._state["status"] = "running"
                    ready.set()
                    count, size = 0, 0
                    while not self._stop.is_set():
                        started = time.monotonic()
                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        with tempfile.TemporaryFile(mode="w+b") as sample:
                            sample_size = self._sample(state["serial"], sample)
                            header = (f"========== Top 采集 #{count + 1} 时间: "
                                      f"{timestamp} ==========\n").encode("utf-8")
                            added = len(header) + sample_size + 2
                            if size + added > MAX_ARCHIVE_BYTES:
                                raise AdbError("Top 归档超过 1 GiB，已停止；已有样本保留", 413)
                            try:
                                self._write_all(archive, header)
                                sample.seek(0)
                                for block in iter(lambda: sample.read(CHUNK_BYTES), b""):
                                    self._write_all(archive, block)
                                self._write_all(archive, b"\n\n")
                                archive.flush()
                                os.fsync(archive.fileno())
                            except Exception:
                                # A failed write must not leave half a sample after good ones.
                                archive.seek(size)
                                archive.truncate()
                                os.fsync(archive.fileno())
                                raise
                        count += 1
                        size += added
                        with self._lock:
                            self._state.update(count=count, bytes=size)
                        if state["target_count"] != -1 and count >= state["target_count"]:
                            break
                        self._stop.wait(max(0, state["interval"] - (time.monotonic() - started)))
        except Exception as exc:
            if isinstance(exc, OSError):
                exc = AdbError("本地 Top 归档写入失败，请检查磁盘空间与权限；已有样本保留", 503)
            error = str(exc)
            if not ready.is_set():
                startup_errors.append(exc)
        finally:
            with self._lock:
                self._capture_error = error
                if self._state["count"]:
                    self._archives[state["id"]] = path
                    self._state["archive"] = "/api/top/archive?capture_id=" + state["id"]
                self._state.update(status="error" if error else "stopped" if self._stop.is_set()
                                   else "completed", running=False, stopping=False, error=error)
            ready.set()

    @staticmethod
    def _write_all(stream, data):
        remaining = memoryview(data)
        while remaining:
            written = stream.write(remaining)
            if not written:
                raise OSError("归档写入未完成")
            remaining = remaining[written:]

    def archive_path(self, capture_id=None):
        """Resolve a completed immutable archive, never a caller-supplied path."""
        with self._lock:
            return self._archive_path_locked(capture_id)

    def _archive_path_locked(self, capture_id=None):
        capture_id = self._state["id"] if capture_id is None else capture_id
        if capture_id == self._state["id"] and self._state["running"]:
            raise AdbError("Top 采集尚未结束，请停止后下载或导入", 409)
        path = self._archives.get(capture_id)
        if path is None or not path.is_file():
            raise AdbError("没有可用的已完成 Top 归档", 404)
        return path

    def import_capture(self, capture_id=None):
        """Explicit synchronous import; reserve both locks before opening the file."""
        with self._lock:
            if self._closed:
                raise AdbError("应用正在关闭", 503)
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
            self._import_done.clear()
            self._state.update(importing=True, status="importing", error=self._capture_error)
        try:
            with path.open("rb") as stream:
                result = self.store.import_files([(path.name, stream)], title)
            with self._lock:
                self._state.update(session_id=result["id"], duplicate=result["duplicate"],
                                   status="imported", error=self._capture_error)
            return dict(result)
        except Exception as exc:
            if isinstance(exc, OSError):
                exc = AdbError("本地 Top 归档读取失败，请检查磁盘与权限后重试", 503)
            with self._lock:
                message = "导入失败，可重试：" + str(exc)
                if self._capture_error:
                    message = self._capture_error + "；" + message
                self._state.update(status="error", error=message)
            raise exc
        finally:
            with self._lock:
                self.import_lock.release()
                self._state["importing"] = False
                self._import_done.set()

