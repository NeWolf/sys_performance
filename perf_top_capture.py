"""Device-owned Top jobs; explicit, bounded local archives and imports."""
from contextlib import contextmanager
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

from perf_adb import AdbController, AdbError
from perf_diagnostics import span, measured
from perf_store import capture_naming, safe_filename
from perf_top_parser import iter_top_records

MAX_ARCHIVE_BYTES = 1_000_000_000
CHUNK_BYTES = 64 * 1024
STOP_TIMEOUT_SECONDS = 20
STOP_POLL_SECONDS = 0.5
REBOOT_WARNING = ("设备启动标识已变化，Top 任务已异常中断；实际结束时间和采集耗时未知。"
                  "日志可能包含不完整尾部，拉取时原样保留，请核对分析结果。")
RECOVERY_WARNING = ("未等待或确认采集停止，已保存读取开始时长度以内的日志副本；"
                    "这不是最终完整日志，可能含未完成样本或并发写入变化。已有本地归档不会被覆盖。")
CLEAR_WARNING = ("当前任务曾执行停止未确认的日志清理（也可能仅部分成功）；"
                 "清理不代表采集已停止，仍可能继续写入。采样次数与字节数是任务记录，不代表现存日志大小；"
                 "历史任务、任务元数据及占用标记均保留。")
REMOTE_ROOT = "/storage/emulated/0/Download/sysmonitor_top_capture"
LEGACY_ROOT = "/data/local/tmp/sysmonitor_top_capture"
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
        self._archive_names = {}
        self._device_names = {}
        self._states = {}
        # A deployment hint only: never substitutes for execution-time guards.
        self._deployments = set()
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
                    remote_cleaned=False, state_corrupt=False, warning=None)

    @staticmethod
    def _serial(serial):
        if not isinstance(serial, str) or not re.fullmatch(SERIAL_PATTERN, serial):
            raise AdbError("无效的设备序列号", 400)

    @staticmethod
    def _identity(capture_id):
        if not isinstance(capture_id, str) or not re.fullmatch(ID_PATTERN, capture_id):
            raise AdbError("无效的 Top 任务标识", 400)

    @contextmanager
    def _device(self, serial):
        # Use the current ADB shell identity; never elevate or restart adbd.
        # Keep the shared lock and the stop-confirmation deadline unchanged.
        with self.adb.device(serial) as device:
            self._device_names[serial] = (device or {}).get("model") or serial
            yield

    @staticmethod
    def _deployment_guard(digest):
        # Error text is only interpreted for this verified Android implementation
        # and the C locale. Other builds fail closed rather than guessing ENOENT.
        legacy = [str(p) for p in reversed(Path(LEGACY_ROOT).parents) if str(p) != "/"]
        legacy.append(LEGACY_ROOT)
        parents = " ".join(shlex.quote(p) for p in legacy)
        current = shlex.quote(LEGACY_ROOT + "/current")
        return (
            'export LC_ALL=C LANG=C; '
            'fail() { echo "Top 部署安全校验失败，采集状态未确认：$*" >&2; exit 1; }; '
            'version=$(/system/bin/toybox --version 2>&1) '
            '&& [ "$version" = "toybox 0.8.9-android" ] || '
            'fail "未验证的 toybox 实现"; '
            'absent() { '
            'entry=$(/system/bin/toybox ls -ld "$1" 2>&1); rc=$?; '
            'if [ "$rc" = 0 ]; then '
            '[ ! -L "$1" ] || fail "路径是软链：$1"; return 1; fi; '
            '[ "$rc" = 1 ] && [ "$entry" = "ls: $1: No such file or directory" ] '
            '|| fail "路径无法确认：$1 ($entry)"; return 0; }; '
            'legacy_safe() { '
            f'for parent in {parents}; do '
            'if absent "$parent"; then return 0; fi; '
            '[ -d "$parent" ] && [ -x "$parent" ] || fail "旧路径不可搜索或不是目录"; '
            'done; '
            f'absent {current} || fail "旧目录存在 current，请人工确认并备份；未执行旧脚本或修改旧目录"; '
            '}; legacy_safe; '
            'probe=$(printf abc | /system/bin/toybox sha256sum 2>/dev/null) '
            '|| fail "SHA256 工具不可用"; '
            '[ "$probe" = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  -" ] '
            '|| fail "SHA256 工具输出无效"; '
            'verify_script() { '
            '[ ! -L "$1" ] && [ -f "$1" ] && [ -r "$1" ] '
            '|| fail "控制脚本不是可读普通文件"; '
            'actual=$(/system/bin/toybox sha256sum < "$1" 2>/dev/null) '
            '|| fail "控制脚本 SHA256 读取失败"; '
            f'[ "$actual" = "{digest}  -" ] || fail "控制脚本 SHA256 不匹配，未覆盖缓存"; '
            '}; '
        )

    def _command(self, serial, action, *args, output=None, deadline=None):
        try:
            return self._checked_command(serial, action, *args, output=output, deadline=deadline)
        except Exception:
            # Never replay an action after an ambiguous transport/action failure.
            # The next explicit request may recheck deployment from scratch.
            self._deployments = {key for key in self._deployments if key[0] != serial}
            raise

    def _checked_command(self, serial, action, *args, output=None, deadline=None):
        def remaining(limit):
            if deadline is None:
                return limit
            value = min(limit, deadline - time.monotonic())
            if value <= 0:
                raise AdbError("停止确认超时；任务可能仍在运行，请查询状态或重试停止。未拉取或清理日志。", 504)
            return value

        def shell(command):
            if deadline is None:
                return self.adb.shell(serial, command, via_stdin=True)
            return self.adb.run("-s", serial, "shell", "-T", "sh -s",
                                input_data=AdbController.shell_input(command), timeout=remaining(10))

        # Normalize checkout/editor line endings before hashing AND uploading.
        # Android sh treats CR in CRLF as part of tokens (e.g. "077\r").
        # Content-addressed scripts never overwrite code being read by a worker.
        script_bytes = self.script.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest = hashlib.sha256(script_bytes).hexdigest()
        remote = REMOTE_ROOT + "/control-" + digest + ".sh"
        guard = self._deployment_guard(digest)
        root, quoted_remote = shlex.quote(REMOTE_ROOT), shlex.quote(remote)
        directory = (f"[ ! -L {root} ] && ([ -d {root} ] || (umask 077; mkdir -p {root})) && "
                     f"[ -d {root} ] && [ -r {root} ] && [ -w {root} ] && [ -x {root} ] "
                     '|| fail "共享目录不可访问"; ')
        check = (guard + directory + f"if absent {quoted_remote}; then echo missing; "
                 f"else verify_script {quoted_remote}; echo ready; fi")
        deployment = (serial, remote)
        cached = "ready" if deployment in self._deployments else shell(check)
        if cached not in {"ready", "missing"}:
            raise AdbError("Top 部署检查响应无效，采集状态未确认", 502)
        if cached == "missing":
            staging = remote + "." + uuid.uuid4().hex
            with tempfile.TemporaryDirectory(prefix="jdperf-top-script-") as temporary:
                local = Path(temporary) / "control.sh"
                # Binary write avoids Windows newline translation; close before
                # adb opens the file so Windows file sharing rules are respected.
                local.write_bytes(script_bytes)
                self.adb.run("-s", serial, "push", str(local), staging, timeout=remaining(30))
            quoted_staging = shlex.quote(staging)
            # Never replace an unexpected cache entry, including one appearing
            # during upload. Shared storage still has unavoidable TOCTOU windows.
            shell(guard + directory + f'verify_script {quoted_staging}; '
                  f'if absent {quoted_remote}; then '
                  f'chmod 600 {quoted_staging} && mv -n {quoted_staging} {quoted_remote} '
                  '|| fail "控制脚本发布失败"; '
                  f'else verify_script {quoted_remote}; fi; '
                  f'verify_script {quoted_remote}')
        # Guard and hash verification run in the same shell as execution, also
        # for streamed pull and deadline-bound stop/status commands.
        command = guard + directory + f'verify_script {quoted_remote}; sh ' + quoted_remote + " " + " ".join(
            shlex.quote(str(value)) for value in (action, *args))
        if output is not None:
            result = self.adb.run("-s", serial, "shell", "-T", "sh -s",
                                  input_data=AdbController.shell_input(command),
                                  timeout=remaining(600 if deadline is None else 10), output=output)
        else:
            result = shell(command)
        self._deployments.add(deployment)
        return result

    def _decode(self, serial, raw):
        if raw == "idle":
            return dict(self._empty(serial), connected=True)
        fields = raw.splitlines()
        control_busy = bool(fields and fields[-1] == "control_busy")
        if control_busy:
            fields.pop()
        busy_error = ("已读取设备状态快照，但设备控制锁仍被占用或因中断残留；"
                      "不能据此确认控制操作已结束。未自动移除锁、停止采集或清理日志；"
                      "若持续出现，需核查锁的持有者，不能直接删锁。")
        clear_unconfirmed = bool(fields and fields[-1] == "clear_unconfirmed")
        if clear_unconfirmed:
            fields.pop()
        if fields and fields[0] == "corrupt":
            if (len(fields) != 4 or not re.fullmatch(ID_PATTERN, fields[1])
                    or fields[2] not in {"inactive", "unconfirmed"}
                    or fields[3] not in {"present", "cleaned"}):
                raise AdbError("设备 Top 损坏状态协议无效，未执行任务操作", 502)
            inactive = fields[2] == "inactive" and not control_busy
            return dict(self._empty(serial), id=fields[1], status="corrupt",
                        state_corrupt=True, running=not inactive, connected=inactive,
                        count=None, target_count=None, interval=None, bytes=None,
                        error="state_corrupt", remote_cleaned=fields[3] == "cleaned",
                        warning=CLEAR_WARNING if clear_unconfirmed else None,
                        connection_error=busy_error if control_busy else (None if inactive else
                        "采集进程仍在运行或身份无法确认；禁止新采集，可重试停止。未确认停止不会清理日志。"))
        rebooted = bool(fields and fields[-1] == "rebooted")
        if rebooted:
            fields.pop()
        unconfirmed = bool(fields and fields[-1] == "unconfirmed")
        if unconfirmed:
            fields.pop()
        remote_cleaned = bool(fields and fields[-1] == "cleaned")
        if remote_cleaned:
            fields.pop()
        archive_name = "raw.txt"
        if fields and fields[-1].startswith("archive="):
            archive_name = fields.pop()[len("archive="):]
        if len(fields) == 7:
            fields.append("")  # adb.run strips the empty hex-encoded title.
        try:
            if archive_name != "raw.txt" and not re.fullmatch(r"Top[0-9]{14}\.txt", archive_name):
                raise ValueError()
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
                             "invalid_top", "disk_error", "detach_failed", "interrupted",
                             "sampling_overrun", "clock_failed"}:
                raise ValueError()
            if len(title) > 960 or not re.fullmatch(r"(?:[0-9a-f]{2})*", title):
                raise ValueError()
            title = bytes.fromhex(title).decode("utf-8")
            if len(title) > 120:
                raise ValueError()
        except (ValueError, AdbError) as exc:
            raise AdbError("设备 Top 状态无效，未执行任务操作", 502) from exc
        if rebooted:
            if status not in {"starting", "running"} or unconfirmed or ended_at is not None:
                raise AdbError("设备 Top 重启状态协议无效，未执行任务操作", 502)
            status, error = "error", "device_rebooted"
            ended_at = elapsed_seconds = None
        return dict(self._empty(serial), id=identity, status=status,
                    warning=self._merge_warnings(REBOOT_WARNING if rebooted else None,
                                              CLEAR_WARNING if clear_unconfirmed else None),
                    running=control_busy or unconfirmed or status in {"starting", "running"}, count=count,
                    target_count=target, interval=interval, bytes=size, title=title,
                    connected=not (control_busy or unconfirmed), error=None if error == "none" else error,
                    connection_error=busy_error if control_busy else (
                        "无法确认采集任务已停止；请重试停止或查询状态，未拉取日志。"
                        if unconfirmed else None),
                    started_at=started_at, ended_at=ended_at, elapsed_seconds=elapsed_seconds,
                    remote_cleaned=remote_cleaned,
                    remote_path=None if remote_cleaned else f"{REMOTE_ROOT}/{identity}/{archive_name}")

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

    @staticmethod
    def _merge_warnings(*warnings):
        return "\n".join(dict.fromkeys(line for warning in warnings if warning
                                      for line in warning.splitlines())) or None

    def _saved_warning(self, path, size):
        marker = path.with_suffix(".warning.json")
        if marker.is_symlink() or not marker.is_file():
            return None
        try:
            with marker.open("rb") as stream:
                data = json.loads(stream.read(1024))
            if (not isinstance(data, dict)
                    or not {"size", "before", "after"} <= set(data)
                    or not set(data) <= {"size", "before", "after", "reason", "recovery", "cleared"}
                    or any(key in data and data[key] is not True for key in ("recovery", "cleared"))
                    or ("reason" in data and data["reason"] != "device_rebooted")
                    or any(type(data[key]) is not int or not 0 <= data[key] <= MAX_ARCHIVE_BYTES
                           for key in ("size", "before", "after")) or data["size"] != size):
                return None
            mismatch = size != data["before"] or size != data["after"]
            return self._merge_warnings(
                REBOOT_WARNING if data.get("reason") == "device_rebooted" else None,
                RECOVERY_WARNING if data.get("recovery") else None,
                CLEAR_WARNING if data.get("cleared") else None,
                self._size_warning(size, data["before"], data["after"]) if mismatch else None)
        except (OSError, ValueError):
            return None

    def _query(self, serial, deadline=None):
        state = self._decode(serial, self._command(serial, "status", deadline=deadline))
        old = self._states.get(serial, {})
        if old.get("id") == state["id"] and state["id"]:
            for key in ("archive", "session_id", "duplicate", "importing"):
                state[key] = old[key]
            state["warning"] = self._merge_warnings(state["warning"], old["warning"])
            if old["status"] in {"imported", "importing"}:
                state["status"] = old["status"]
        if state["id"]:
            path = self._local_path(state)
            if path.is_file() and not path.is_symlink():
                size = path.stat().st_size
                saved_warning = self._saved_warning(path, size)
                if (0 < size <= MAX_ARCHIVE_BYTES
                        and (not state["running"] or state["state_corrupt"]
                             or RECOVERY_WARNING in (saved_warning or "") or state["id"] in self._archives)
                        and (size == state["bytes"] or saved_warning or state["state_corrupt"]
                             or state["id"] in self._archives)):
                    if (path.parent.is_symlink() or path.resolve().parent not in
                            (self.archive_root, self.archive_root / state["id"])):
                        raise AdbError("本地归档路径无效", 409)
                    self._archive_naming(path, state)
                    self._archives[state["id"]] = path
                    state["archive"] = "/api/top/archive?capture_id=" + state["id"]
                    state["warning"] = self._merge_warnings(state["warning"], saved_warning)
                    if state["state_corrupt"]:
                        state["warning"] = self._merge_warnings(
                            state["warning"],
                            "设备状态损坏，无法核对日志完整性；已有本地归档已保留，可下载或导入分析。")
                    elif size != state["bytes"] and "Top 归档字节数不一致：" not in (state["warning"] or ""):
                        state["warning"] = self._merge_warnings(state["warning"],
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
                with self._device(serial):
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
            with self._device(serial):
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

    @measured("top.stop")
    def _stop_confirmed(self, serial, capture_id, *, reject_corrupt=False):
        # Request stop before status; a damaged status must not hide stop retry.
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
                if reject_corrupt and state["state_corrupt"]:
                    raise AdbError("Top 状态损坏，无法确认停止；禁止拉取设备日志。本地归档不受影响。", 409)
                if not state["running"]:
                    return state
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdbError("停止确认超时；任务可能仍在运行，请查询状态或重试停止。未拉取或清理日志。", 504)
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
            with self._device(serial):
                return self._stop_confirmed(serial, capture_id)

    def clear(self, serial, capture_id, *, allow_unconfirmed=False):
        if type(allow_unconfirmed) is not bool:
            raise ValueError("恢复操作必须显式传入布尔确认参数")
        self._serial(serial)
        self._identity(capture_id)
        with self._lock:
            self._available()
            with self._device(serial):
                if allow_unconfirmed:
                    state = self._current(serial, capture_id)
                    if state["state_corrupt"]:
                        raise AdbError("Top 状态损坏，拒绝自动清理；请保留数据人工检查", 409)
                else:
                    self._stop_confirmed(serial, capture_id)
                self._command(serial, "clear-live" if allow_unconfirmed else "clear", capture_id)
                return self._current(serial, capture_id)

    @measured("top.pull")
    def pull(self, serial, capture_id, *, allow_unconfirmed=False):
        if type(allow_unconfirmed) is not bool:
            raise ValueError("恢复操作必须显式传入布尔确认参数")
        self._serial(serial)
        self._identity(capture_id)
        with self._lock:
            self._available()
            with self._device(serial):
                state = (self._current(serial, capture_id) if allow_unconfirmed else
                         self._stop_confirmed(serial, capture_id, reject_corrupt=True))
                if state["remote_cleaned"]:
                    raise AdbError("设备 Top 日志已清理，本地已拉取的归档不受影响", 409)
                if state.get("state_corrupt"):
                    raise AdbError("Top 状态损坏，无法确认完整样本；请保留设备数据人工检查，不自动清理或恢复。本地归档不受影响。", 409)
                if not allow_unconfirmed and state["bytes"] == 0 and state["error"] != "device_rebooted":
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
                    with span("top.transfer", files=0, bytes=0) as transfer:
                        self._command(serial, "pull-live" if allow_unconfirmed else "pull",
                                      capture_id, output=staging)
                        # A child's writes need not advance the parent's stream position.
                        # Measure bytes on disk, including any buffered test/wrapper writes.
                        staging.flush()
                        size = os.fstat(staging.fileno()).st_size
                        transfer.update(files=1, bytes=size)
                        if size > MAX_ARCHIVE_BYTES:
                            raise AdbError("Top 原始归档超过 1GB（1000000000 字节）", 413)
                        after = self._current(serial, capture_id)
                        if (after["running"] and not allow_unconfirmed) or after["remote_cleaned"] or after["state_corrupt"]:
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
                        rebooted = any(s["error"] == "device_rebooted" for s in (state, after))
                        diagnostics = {"size": size, "before": state["bytes"], "after": after["bytes"]}
                        warning = REBOOT_WARNING if rebooted else None
                        if rebooted:
                            diagnostics["reason"] = "device_rebooted"
                        if allow_unconfirmed:
                            diagnostics["recovery"] = True
                            warning = self._merge_warnings(warning, RECOVERY_WARNING)
                        if any(CLEAR_WARNING in (s["warning"] or "") for s in (state, after)):
                            diagnostics["cleared"] = True
                            warning = self._merge_warnings(warning, CLEAR_WARNING)
                        if size != state["bytes"] or size != after["bytes"]:
                            warning = self._merge_warnings(
                                warning, self._size_warning(size, state["bytes"], after["bytes"]))
                    with span("top.archive", files=1, bytes=size):
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
                                        json.dump(diagnostics, metadata)
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
                self._archive_naming(path, state)
                self._state["archive"] = "/api/top/archive?capture_id=" + capture_id
                self._state["warning"] = self._merge_warnings(self._state["warning"], warning)
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

    def _archive_naming(self, path, state):
        """Persist per-archive metadata, independent of the current device task."""
        cached = self._archive_names.get(path)
        if cached:
            return dict(cached)
        marker = path.with_suffix(".name.json")
        if marker.is_symlink():
            raise AdbError("本地归档命名记录无效", 409)
        if marker.exists():
            try:
                with marker.open("rb") as stream:
                    naming = json.loads(stream.read(4096))
                if (not isinstance(naming, dict)
                        or set(naming) != {"name", "capture_method", "device_name", "capture_started_at", "naming_time_source"}
                        or naming["capture_method"] != "Top"
                        or not isinstance(naming["name"], str)
                        or not isinstance(naming["device_name"], str)
                        or naming["name"] != safe_filename(naming["name"])
                        or naming["naming_time_source"] not in ("capture", "first_record", "unknown")
                        or naming["device_name"] != safe_filename(naming["device_name"], max_bytes=180)
                        or (naming["capture_started_at"] is not None
                            and (type(naming["capture_started_at"]) not in (int, float)
                                 or not math.isfinite(naming["capture_started_at"])))
                        or ((naming["naming_time_source"] == "unknown") != (naming["capture_started_at"] is None))):
                    raise ValueError()
                match = re.fullmatch(r"Top_(?:" + re.escape(naming["device_name"]) + r"_)?([0-9]{12}|未知时间)",
                                     naming["name"])
                if not match:
                    raise ValueError()
                # Read legacy sidecars without rewriting them or recomputing local time.
                naming["name"] = "Top_" + match[1]
            except (OSError, ValueError, TypeError) as exc:
                raise AdbError("本地归档命名记录无效，原日志未改动", 409) from exc
        else:
            started = state.get("started_at")
            first_ts = None
            naming = capture_naming("Top", self._device_names.get(state.get("serial")) or state.get("serial") or "",
                                    started * 1000 if started is not None else None)
            if naming["naming_time_source"] == "unknown":
                with path.open("rb") as stream:
                    for record in iter_top_records(stream):
                        if record.kind and not record.error:
                            first_ts = record.data["ts"]
                            break
                naming = capture_naming("Top", naming["device_name"], first_ts=first_ts)
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                                 suffix=".part") as stream:
                    json.dump(naming, stream, ensure_ascii=False, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                    os.link(stream.name, marker)
            except OSError as exc:
                raise AdbError("归档已保留，但保存命名记录失败，请检查磁盘权限后重试", 503) from exc
        self._archive_names[path] = naming
        return dict(naming)

    def archive_filename(self, path):
        with self._lock:
            identity = next((key for key, value in self._archives.items() if value == path), None)
            if identity is None:
                raise AdbError("没有已登记的本地归档", 404)
            self._archive_path_locked(identity)
            state = next((value for value in self._states.values() if value["id"] == identity), {})
            return self._archive_naming(path, state)["name"] + ".txt"

    def _archive_path_locked(self, capture_id=None):
        capture_id = self._state["id"] if capture_id is None else capture_id
        if capture_id is not None:
            self._identity(capture_id)
        if (capture_id == self._state["id"] and self._state["running"]
                and capture_id not in self._archives):
            raise AdbError("Top 采集尚未结束，请停止后拉取", 409)
        path = self._archives.get(capture_id)
        if (path is None or path.is_symlink() or not path.is_file()
                or path.parent.is_symlink()
                or path.resolve().parent not in (self.archive_root, self.archive_root / capture_id)
                or not 0 < path.stat().st_size <= MAX_ARCHIVE_BYTES):
            raise AdbError("没有可用的本地 Top 归档，请先显式拉取", 404)
        return path

    @measured("top.import")
    def import_capture(self, capture_id=None):
        with self._lock:
            self._available()
            if self._state["running"] and self._state["id"] not in self._archives:
                raise AdbError("Top 任务运行或停止未确认，且没有可用本地归档", 409)
            if capture_id is not None and capture_id != self._state["id"]:
                raise AdbError("Top 任务已切换，请刷新状态后重试", 409)
            if not self.import_lock.acquire(blocking=False):
                raise AdbError("已有导入正在进行，请稍后重试", 409)
            try:
                # Deletion shares this lock: validate both archive and cached
                # session before trusting a previous import result.
                path = self._archive_path_locked(capture_id)
                session_id = self._state["session_id"]
                if session_id is not None:
                    try:
                        self.store.session(session_id)
                    except KeyError:
                        self._state.update(session_id=None, duplicate=False)
                        session_id = None
                naming = self._archive_naming(path, self._state)
            except BaseException:
                self.import_lock.release()
                raise
            if session_id is not None:
                self.import_lock.release()
                return {"id": session_id, "duplicate": True}
            serial = self._state["serial"]
            self._import_done.clear()
            self._state.update(importing=True, status="importing", error=self._capture_error)
        try:
            with path.open("rb") as stream:
                if path.parent == self.archive_root / self._state["id"]:
                    stream._capture_log_directory = path.parent
                # Legacy source labels retain their historical mtime-based name;
                # display/download names always come from the immutable snapshot.
                source_name = (path.name if re.fullmatch(r"Top[0-9]{14}\.txt", path.name)
                               else "Top" + time.strftime("%Y%m%d%H%M%S", time.localtime(path.stat().st_mtime)) + ".txt")
                result = self.store.import_files([(source_name, stream)], capture_method="Top",
                                                 device_name=naming["device_name"],
                                                 started_at=(naming["capture_started_at"]
                                                             if naming["naming_time_source"] == "capture" else None),
                                                 naming_snapshot=naming)
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