"""Safe bridge to the bundled JoySpace read/import tools (no npm dependency)."""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from functools import wraps
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import perf_slate
from perf_diagnostics import correlation


def _diagnostic_emit(stage, **fields):
    # Import lazily: diagnostics are optional and must never break business work.
    try:
        from perf_diagnostics import emit
        emit(stage, **fields)
    except Exception:
        pass


def _diagnostic_clock(*, wall=False):
    try:
        return time.time() if wall else time.monotonic()
    except Exception:
        return None


def _diagnostic_elapsed(started):
    try:
        return {"elapsed_ms": max(0, min(86400000, round((time.monotonic() - started) * 1000)))}
    except Exception:
        return {}


_DIAGNOSTIC_FILE = ContextVar("joyspace_diagnostic_file", default=None)
_DIAGNOSTIC_OPERATION = ContextVar("joyspace_diagnostic_operation", default=None)
_DIAGNOSTIC_ENV = "JOYSPACE_DIAGNOSTIC_FILE"
_DIAGNOSTIC_ENUMS = {
    "stage": {"joyspace.auth", "joyspace.gateway", "joyspace.gateway_result", "joyspace.port", "joyspace.auth_process"},
    "source": {"env", "config", "tokenGrant", "legacy"},
    "operation": {"encrypt", "tokenGrant", "getWebToken", "hioffice"},
    "status": {"selected", "begin", "ok", "error", "timeout"},
    "phase": {"headers", "body"},
    "code": {"ok", "remote_error", "http_error", "missing_auth_header", "unknown_error", "timeout",
             "ECONNREFUSED", "ECONNRESET", "ETIMEDOUT", "ENOTFOUND", "EAI_AGAIN",
             "UND_ERR_CONNECT_TIMEOUT", "UND_ERR_HEADERS_TIMEOUT", "UND_ERR_BODY_TIMEOUT",
             "CERT_HAS_EXPIRED", "UNABLE_TO_VERIFY_LEAF_SIGNATURE"},
}
_DIAGNOSTIC_NUMBERS = {"elapsed_ms": (0, 86400000), "http_status": (100, 599),
                       "port": (8988, 9006), "attempt": (1, 10)}


def _import_diagnostics(path, interrupted=None):
    # Treat even our private child channel as untrusted: exact enums, bounded I/O.
    pending = {}

    def finish_pending(status):
        try:
            now = round(_diagnostic_clock(wall=True) * 1000)
        except Exception:
            now = None
        for (stage, operation, port), (fields, started) in pending.items():
            end = {key: value for key, value in fields.items()
                   if key in ("operation", "port", "attempt", "phase")}
            end.update(status=status, code="timeout" if status == "timeout" else "unknown_error")
            if now is not None and type(started) is int and 0 <= now - started <= 86400000:
                end["elapsed_ms"] = now - started
            _diagnostic_emit(stage, **end)
        pending.clear()

    try:
        with open(path, "rb") as stream:
            for _ in range(1024):
                line = stream.readline(2049)
                if not line or len(line) > 2048:
                    break
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    if record.get("stage") not in _DIAGNOSTIC_ENUMS["stage"]:
                        continue
                    safe = {}
                    for key, value in record.items():
                        if key in _DIAGNOSTIC_ENUMS:
                            if type(value) is str and value in _DIAGNOSTIC_ENUMS[key]:
                                safe[key] = value
                        elif key in _DIAGNOSTIC_NUMBERS:
                            low, high = _DIAGNOSTIC_NUMBERS[key]
                            if (type(value) is int and low <= value <= high
                                    and (key != "port" or value % 2 == 0)):
                                safe[key] = value
                        elif key == "has_auth_header" and type(value) is bool:
                            safe[key] = value
                    stage = safe.pop("stage")
                    _diagnostic_emit(stage, **safe)
                    if stage in ("joyspace.port", "joyspace.gateway", "joyspace.auth_process"):
                        key = (stage, safe.get("operation"), safe.get("port"))
                        if safe.get("status") == "begin":
                            pending[key] = (safe, record.get("started_ms"))
                        elif safe.get("phase") == "headers" and safe.get("status") == "ok":
                            if key in pending:
                                fields, started = pending[key]
                                pending[key] = (dict(fields, phase="body"), started)
                        elif safe.get("status") in ("ok", "error", "timeout"):
                            pending.pop(key, None)
                    if stage == "joyspace.auth_process" and safe.get("status") in ("error", "timeout"):
                        finish_pending(safe["status"])
                except (ValueError, TypeError, KeyError):
                    continue
    except Exception:
        pass
    # An exited child cannot complete an unfinished body, even on exit code zero.
    finish_pending(interrupted if interrupted in ("error", "timeout") else "error")


@contextmanager
def _diagnostic_channel(probe):
    directory = None
    path = None
    try:
        if not probe:
            directory = tempfile.TemporaryDirectory(prefix="jdperf-joy-diag-")
            path = Path(directory.name) / "events.jsonl"
            with path.open("xb"):
                pass
    except Exception:
        path = None
    token = _DIAGNOSTIC_FILE.set(path)
    interrupted = None
    try:
        yield
    except BaseException as exc:
        interrupted = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "error"
        raise
    finally:
        _DIAGNOSTIC_FILE.reset(token)
        try:
            if path is not None:
                _import_diagnostics(path, interrupted=interrupted)
        except Exception:
            pass
        finally:
            if directory is not None:
                try:
                    directory.cleanup()
                except Exception:
                    pass


def _diagnostic_operation(stage):
    def decorate(function):
        @wraps(function)
        @correlation()
        def wrapped(*args, **kwargs):
            started = _diagnostic_clock()
            status = "error"
            state = {"timeout": False}
            token = _DIAGNOSTIC_OPERATION.set(state)
            _diagnostic_emit(stage, status="begin")
            try:
                result = function(*args, **kwargs)
                status = "error" if isinstance(result, dict) and result.get("verified") is False else "ok"
                return result
            finally:
                _DIAGNOSTIC_OPERATION.reset(token)
                if status != "ok" and state["timeout"]:
                    status = "timeout"
                _diagnostic_emit(stage, status=status,
                                 **_diagnostic_elapsed(started))
        return wrapped
    return decorate


# Read the templates in order: the first one that answers with content wins,
# the rest are only fallbacks for an unreachable or empty primary template.
TEMPLATE_URLS = (
    "https://joyspace.jd.com/pages/u4e8K20FZJF0SsDUG5fW",
    "https://joyspace.jd.com/pages/rXNrVj6wBpf16NbPkmFh",
)
TEMPLATE_URL = TEMPLATE_URLS[0]
READ_SCRIPT = "skills/joyspace-read-doc/scripts/read_joyspace_doc.js"
IMPORT_SCRIPT = "skills/markdown-to-joyspace/scripts/import_markdown_doc.js"
SLATE_SCRIPT = "skills/markdown-to-joyspace/scripts/import_slate_doc.mjs"
LIST_SCRIPT = "skills/joyspace-list/scripts/list_joyspace.mjs"
PAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")
MARKDOWN_PAGE = 13
CHARTS_UNSUPPORTED = ("含图报告发布需要可启动的 Chrome 或 Edge 浏览器，请安装后再试。"
                      "本次未认证、创建页面或上传图片；本地报告保持不变。")
PUBLISH_UNKNOWN = "发布结果未知，远端可能存在文档、临时页或图片残留；请先在 JoySpace 人工确认，不要自动重试。"
PUBLISH_RECOVERED = ("发布结果未收到确认，但在最近文档中找到同名页面；"
                     "该链接未经内容校验，请人工核对正文后再使用。")
PROBE = "console.log(JSON.stringify({version:process.version,platform:process.platform,arch:process.arch}))"


class JoySpaceError(RuntimeError):
    """An intentionally safe message suitable for display to the user."""


class _ChartsUnavailable(JoySpaceError):
    """A confirmed local preflight rejection, not an unknown publish outcome."""


def _root():
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent / "_internal"))
    return Path(__file__).resolve().parent


def _runtime():
    root = _root()
    skill = root / "joyspace_assets/joyspace-kit"
    for name in ("SKILL.md", READ_SCRIPT, IMPORT_SCRIPT, SLATE_SCRIPT, LIST_SCRIPT,
                "skills/markdown-to-joyspace/scripts/chart_browser.mjs",
                "skills/shared-auth.mjs", "skills/safe-diagnostics.mjs",
                "skills/hioffice-auth/scripts/hioffice-auth.mjs"):
        if not (skill / name).is_file():
            raise JoySpaceError("内置 JoySpace 资源不完整，请重新安装应用。")
    node = root / "joyspace_assets/node" / ("node.exe" if os.name == "nt" else "node")
    if not node.is_file():
        if getattr(sys, "frozen", False):
            raise JoySpaceError("内置 Node 运行时缺失，请重新安装应用。")
        found = shutil.which("node")
        if not found:
            raise JoySpaceError("开发环境需要 Node 22.7 或更高版本。")
        node = Path(found)
    if os.name != "nt" and not os.access(node, os.X_OK):
        raise JoySpaceError("Node 运行时不可执行，请重新安装应用。")
    return node.resolve(), skill.resolve()


def _environment(probe=False):
    if probe:
        # No auth helper is invoked, and no credentials are inherited by probes.
        env = {k: v for k, v in os.environ.items()
               if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    else:
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("NODE_", "DYLD_", "LD_"))}
    env = {k: v for k, v in env.items() if k.upper() != _DIAGNOSTIC_ENV}
    if not probe and _DIAGNOSTIC_FILE.get() is not None:
        env[_DIAGNOSTIC_ENV] = str(_DIAGNOSTIC_FILE.get())
    env["PATH"] = ""
    return env


# Never run the publishing module before Windows has assigned the gated Node
# process to its private, non-breakaway Job. No browser/auth code runs in the gate.
_WINDOWS_PUBLISH_GATE = r"""
process.on('SIGBREAK', () => {
  if (!process.emit('SIGTERM')) process.exit(1);
});
process.stdin.once('data', async chunk => {
  process.stdin.removeAllListeners('end');
  process.stdin.destroy();
  if (chunk.toString() !== '1') process.exit(1);
  try {
    const {pathToFileURL} = await import('node:url');
    await import(pathToFileURL(process.argv[1]).href);
  } catch { process.exitCode = 1; }
});
process.stdin.once('end', () => process.exit(1));
"""
_PUBLISH_GRACE = 2.0
_PUBLISH_OUTPUT_LIMIT = 8 * 1024 * 1024


class _WindowsPublishJob:
    """Own only this launch and descendants, never enumerate/kill browser names."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active_limit", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD),
                        ("scheduling", w.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read", "write", "other")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in
                        ("user", "kernel", "period_user", "period_kernel")] + [
                            (name, w.DWORD) for name in
                            ("faults", "total", "active", "terminated")]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = argtypes, restype
        self.api, self.ctypes, self.accounting = api, ctypes, Accounting
        self.handle = api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("Cannot create private publish job")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        try:
            self._check(api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _check(ok):
        if not ok:
            raise OSError("Private publish job operation failed")

    def assign(self, process):
        self._check(self.api.AssignProcessToJobObject(self.handle, int(process._handle)))

    def active(self):
        info = self.accounting()
        self._check(self.api.QueryInformationJobObject(
            self.handle, 1, self.ctypes.byref(info), self.ctypes.sizeof(info), None))
        return info.active

    def terminate(self):
        self._check(self.api.TerminateJobObject(self.handle, 1))

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            self._check(self.api.CloseHandle(handle))


def _macos_publish_group_exited(pgid):
    """Prove the pinned private group contains no live members, or fail closed."""
    if sys.platform != "darwin":
        return False
    import ctypes

    # Unlike a system-wide process scan, PROC_PGRP_ONLY selects only our group.
    api = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    api.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                 ctypes.c_void_p, ctypes.c_int]
    api.proc_listpids.restype = ctypes.c_int
    api.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                ctypes.c_void_p, ctypes.c_int]
    api.proc_pidinfo.restype = ctypes.c_int

    class BsdShortInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in
                    ("pid", "ppid", "pgid", "status")] + [
                        ("comm", ctypes.c_char * 16)] + [
                            (name, ctypes.c_uint32) for name in
                            ("flags", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]

    size = api.proc_listpids(2, pgid, None, 0)  # PROC_PGRP_ONLY
    if size <= 0:
        return False  # The unreaped leader must still be present.
    for _ in range(3):
        members = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 32))()
        capacity = ctypes.sizeof(members)
        used = api.proc_listpids(2, pgid, members, capacity)
        if used <= 0 or used % ctypes.sizeof(ctypes.c_int):
            return False
        if used < capacity:
            break
        size = capacity * 2  # Never accept a potentially truncated snapshot.
    else:
        return False
    pids = list(members)[:used // ctypes.sizeof(ctypes.c_int)]
    if pgid not in pids or any(pid <= 0 for pid in pids):
        return False
    for pid in pids:
        info = BsdShortInfo()
        ctypes.set_errno(0)
        used = api.proc_pidinfo(pid, 13, 0, ctypes.byref(info), ctypes.sizeof(info))
        if used == 0 and ctypes.get_errno() == errno.ESRCH:
            # Darwin proc_pidinfo excludes zombies; a reaped member is also gone.
            # Never use these snapshot PIDs as signal targets (they can recycle).
            continue
        if (used != ctypes.sizeof(info) or info.pid != pid or info.pgid != pgid
                or info.status != 5):  # SZOMB; EPERM and unknown states fail closed.
            return False
    # A live member could have forked and exited while we inspected the first
    # snapshot. Accept only unchanged membership after every member proved dead;
    # any new/missing PID or truncation is uncertainty, not permission to ignore.
    used = api.proc_listpids(2, pgid, members, capacity)
    return (0 < used < capacity and used % ctypes.sizeof(ctypes.c_int) == 0
            and set(list(members)[:used // ctypes.sizeof(ctypes.c_int)]) == set(pids))


def _signal_publish_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS killpg can return EPERM when every member is already a zombie.
        # Keep the leader unreaped during verification, pinning the group ID.
        if not _macos_publish_group_exited(process.pid):
            raise
        return False
    return True


def _stop_publish_group(process):
    # start_new_session makes this PID the exclusive process group leader.
    # Never signal after reaping it, or after observing the group's removal.
    if process.returncode is not None or process.pid <= 1 or process.pid == os.getpgrp():
        raise OSError("Invalid private publish group")
    if not _signal_publish_group(process, signal.SIGTERM):
        return
    deadline = time.monotonic() + _PUBLISH_GRACE
    while time.monotonic() < deadline:
        if not _signal_publish_group(process, 0):
            return
        time.sleep(0.05)
    _signal_publish_group(process, signal.SIGKILL)


def _stop_publish_tree(process, job=None):
    """Bounded cleanup, including descendants left behind by a successful Node."""
    try:
        if job is not None:
            try:
                if job.active():
                    if process.returncode is None:
                        try:
                            process.send_signal(signal.CTRL_BREAK_EVENT)
                        except OSError:
                            pass  # No console: Job termination remains authoritative.
                    deadline = time.monotonic() + _PUBLISH_GRACE
                    while job.active() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    job.terminate()
                    deadline = time.monotonic() + _PUBLISH_GRACE
                    while job.active() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    if job.active():
                        raise OSError("Private publish job did not terminate")
            finally:
                job.close()  # KILL_ON_JOB_CLOSE also covers exceptions in cleanup.
        else:
            _stop_publish_group(process)
    finally:
        # Even a genuine permission/query/Job error must not strand the leader.
        # All group operations are over before wait/kill can reap its pinned PID.
        try:
            process.wait(timeout=_PUBLISH_GRACE)
        except subprocess.TimeoutExpired:
            try:
                process.kill()  # Only our direct Popen child; never a snapshot PID.
            finally:
                process.wait(timeout=_PUBLISH_GRACE)


def _wait_publish(process, timeout):
    if os.name == "nt":
        process.wait(timeout=timeout)
        return
    # Keep the session leader unreaped until group cleanup. This pins its PID,
    # preventing a recycled group ID from ever targeting an unrelated process.
    if not all(hasattr(os, name) for name in ("waitid", "P_PID", "WNOWAIT")):
        # macOS Python may not expose waitid; kqueue observes exit without reaping.
        if not hasattr(select, "kqueue"):
            raise OSError("Private publish exit observation unavailable")
        with closing(select.kqueue()) as queue:
            event = select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                                 flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                 fflags=select.KQ_NOTE_EXIT)
            try:
                events = queue.control([event], 1, timeout)
            except ProcessLookupError:
                return  # Already exited before registration; still unreaped.
            if not events:
                raise subprocess.TimeoutExpired(process.args, timeout)
            for event in events:
                if event.flags & select.KQ_EV_ERROR:
                    if event.data == errno.ESRCH:
                        return
                    raise OSError("Private publish exit observation failed")
        return
    deadline = time.monotonic() + timeout
    while True:
        if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)
        time.sleep(min(0.05, remaining))


def _run_publish(node, args, skill, *, timeout, probe=False):
    """Use files, not inherited output pipes: EOF cannot stall the timeout path."""
    command = [str(node), *args]
    job = None
    process = None
    assigned = False
    # Only these handles are ours. Do not scan/remove browser profile directories.
    with tempfile.TemporaryFile(mode="w+b") as stdout:
        try:
            options = {"start_new_session": True}
            if os.name == "nt":
                job = _WindowsPublishJob()
                options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                command = [str(node), "--input-type=module", "-e", _WINDOWS_PUBLISH_GATE, *args]
            process = subprocess.Popen(
                command, cwd=str(skill), env=_environment(probe), shell=False,
                stdin=subprocess.PIPE if job else subprocess.DEVNULL,
                stdout=stdout, stderr=subprocess.DEVNULL, close_fds=True, **options,
            )
            if job is not None:
                job.assign(process)
                assigned = True
                process.stdin.write(b"1")
                process.stdin.flush()
                process.stdin.close()
            _wait_publish(process, timeout)
        finally:
            try:
                if process is not None:
                    if job is not None and not assigned:
                        # Gate never opened: no publisher/browser has executed.
                        try:
                            process.kill()
                        finally:
                            process.wait(timeout=_PUBLISH_GRACE)
                    else:
                        _stop_publish_tree(process, job)
            finally:
                try:
                    if job is not None:
                        job.close()
                finally:
                    if process is not None and process.stdin is not None:
                        process.stdin.close()
        stdout.seek(0)
        output = stdout.read(_PUBLISH_OUTPUT_LIMIT + 1)
        if len(output) > _PUBLISH_OUTPUT_LIMIT:
            raise ValueError("Publish output too large")
        # Strict decode happens only after all owned processes have been cleaned.
        return subprocess.CompletedProcess(command, process.returncode, output.decode("utf-8"), "")


def _invoke(node, args, skill, *, timeout, publish=False, probe=False, shape=dict):
    started = _diagnostic_clock()
    operation = "publish" if publish else "read"
    outcome = "error"
    if not probe:
        _diagnostic_emit("joyspace.invoke", operation=operation, status="begin")
    try:
        with _diagnostic_channel(probe):
            if publish:
                result = _run_publish(node, args, skill, timeout=timeout, probe=probe)
            else:
                result = subprocess.run(
                    [str(node), *args], cwd=str(skill), env=_environment(probe),
                    shell=False, capture_output=True, text=True, encoding="utf-8",
                    timeout=timeout, check=False,
                )
            if result.returncode:
                raise ValueError("script failed")
            data = json.loads(result.stdout)
            if not isinstance(data, shape):
                raise ValueError("invalid result")
            outcome = "ok"
            return data
    except subprocess.TimeoutExpired:
        outcome = "timeout"
        state = _DIAGNOSTIC_OPERATION.get()
        if not probe and state is not None:
            state["timeout"] = True
        raise JoySpaceError(PUBLISH_UNKNOWN if publish else "JoySpace 读取超时，请检查内网连接和 HiOffice 登录状态。") from None
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        raise JoySpaceError(PUBLISH_UNKNOWN if publish else "JoySpace 操作失败，请检查内网连接和 HiOffice 登录状态。") from None
    finally:
        if not probe:
            _diagnostic_emit("joyspace.invoke", operation=operation, status=outcome,
                             **_diagnostic_elapsed(started))


def _check_runtime():
    node, skill = _runtime()
    data = _invoke(node, ["-e", PROBE], skill, timeout=10, probe=True)
    version = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", str(data.get("version", "")))
    arches = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}
    systems = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
    if (not version or tuple(map(int, version.groups())) < (22, 7, 0)
            or data.get("platform") != systems.get(platform.system())
            or data.get("arch") != arches.get(platform.machine().lower())):
        raise JoySpaceError("Node 运行时版本、平台或架构不兼容，请重新安装应用。")
    return node, skill


def status():
    """Only readiness, never credentials or an implicit authentication attempt."""
    try:
        _check_runtime()
    except JoySpaceError as exc:
        return {"ready": False, "message": str(exc)}
    return {"ready": True, "message": "JoySpace 运行时就绪；尚未验证认证，请保持 HiOffice 登录及内网连接。"}


def _page_id(value):
    if not isinstance(value, str) or value != value.strip():
        raise JoySpaceError("请选择有效的 JoySpace 页面链接或页面 ID。")
    if PAGE_ID.fullmatch(value):
        return value
    try:
        url = urlsplit(value)
        match = re.fullmatch(r"/pages/([A-Za-z0-9_-]{1,128})/?", url.path)
        if (url.scheme != "https" or url.netloc != "joyspace.jd.com"
                or url.query or url.fragment or not match):
            raise ValueError("untrusted page URL")
        return match.group(1)
    except ValueError:
        raise JoySpaceError("仅支持 https://joyspace.jd.com/pages/ 页面链接或页面 ID。") from None


@_diagnostic_operation("joyspace.template")
def read_template():
    """Read the template as a Slate block tree, not as Markdown.

    The report is written straight into this tree and uploaded unchanged, so
    the published page keeps the template's own tables, merges and colours.
    """
    node, skill = _check_runtime()
    failure = None
    for url in TEMPLATE_URLS:
        try:
            data = _invoke(node, [str(skill / READ_SCRIPT), "--url", url, "--raw"],
                           skill, timeout=90)
            raw = data.get("raw")
            content = raw.get("content") if isinstance(raw, dict) else None
            blocks = content.get("content") if isinstance(content, dict) else None
            try:
                return perf_slate.validate_tree(blocks)
            except perf_slate.SlateError as exc:
                raise JoySpaceError(f"JoySpace 模板结构不受支持：{exc}") from None
        except JoySpaceError as exc:
            # Never expose which template answered; only try the next fallback.
            failure = exc
    raise JoySpaceError(str(failure) if failure else "JoySpace 模板内容为空或格式无效。")


def _has_charts(blocks):
    detector = getattr(perf_slate, "has_charts", None)
    if callable(detector):
        return detector(blocks)
    return isinstance(blocks, list) and any(
        isinstance(block, dict) and block.get("type") == "image" for block in blocks)


def report_snapshot(file_path):
    """Validate a draft and hash its original bytes without changing it or doing I/O remotely."""
    try:
        content = Path(file_path).read_bytes()
        blocks = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise JoySpaceError("无法读取报告，请重新生成并预览。") from None
    try:
        perf_slate.validate_tree(blocks, allow_images=True)
    except perf_slate.SlateError as exc:
        raise JoySpaceError(f"报告内容不受支持，暂不支持发布：{exc}"
                            "请检查完整正文后重新生成并预览。") from None
    return blocks, hashlib.sha256(content).hexdigest()


def report_sha256(file_path):
    return report_snapshot(file_path)[1]


def find_published(title, *, limit=100):
    """Look up recent documents by exact title; read-only, never writes remotely.

    Returns every markdown page whose title matches, so the caller can tell a
    single candidate apart from duplicates instead of guessing.
    """
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or "\x00" in title:
        raise JoySpaceError("文档标题不能为空且不能超过 200 个字符。")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise JoySpaceError("查询条数必须是 1 到 200 之间的整数。")
    wanted = title.strip()
    node, skill = _check_runtime()
    pages = _invoke(node, [str(skill / LIST_SCRIPT), "--json", "--limit", str(limit),
                           "--grep", wanted], skill, timeout=90, shape=list)
    found = []
    for page in pages:
        if not isinstance(page, dict) or page.get("page_type") != MARKDOWN_PAGE:
            continue
        name, link = page.get("title"), page.get("link")
        if not isinstance(name, str) or name.strip() != wanted:
            continue
        try:
            page_id = _page_id(link)
        except JoySpaceError:
            continue
        # The link must agree with the record's own identifier; never trust one alone.
        if page_id != page.get("id"):
            continue
        created = page.get("created_at")
        found.append({
            "pageId": page_id,
            "link": "https://joyspace.jd.com/pages/" + page_id,
            "createdAt": created if isinstance(created, str) and TIMESTAMP.fullmatch(created) else None,
        })
    return found


def _recover(title):
    """Never republish: only report what a read-only lookup can prove."""
    try:
        found = find_published(title)
    except JoySpaceError:
        raise JoySpaceError(PUBLISH_UNKNOWN) from None
    if not found:
        raise JoySpaceError(PUBLISH_UNKNOWN + "已查询最近文档，未发现同名页面。")
    if len(found) > 1:
        links = "、".join(item["link"] for item in found)
        raise JoySpaceError(PUBLISH_UNKNOWN +
                            f"已查询最近文档，发现 {len(found)} 个同名页面：{links}。")
    return dict(found[0], verified=False, message=PUBLISH_RECOVERED)


@_diagnostic_operation("joyspace.publish")
def publish_slate(file_path, title, placement="personal", parent_page=None, *, expected_sha256=None):
    """Upload the draft block tree as-is, so the page matches the template."""
    if placement not in ("personal", "child", "sibling"):
        raise JoySpaceError("发布位置必须是 personal、child 或 sibling。")
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or "\x00" in title:
        raise JoySpaceError("文档标题不能为空且不能超过 200 个字符。")
    try:
        path = Path(file_path).resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".json":
            raise ValueError("invalid report file")
    except (OSError, ValueError, TypeError):
        raise JoySpaceError("请选择存在的报告文件。") from None
    target = None
    if placement != "personal":
        target = _page_id(parent_page)
    elif parent_page:
        raise JoySpaceError("个人空间发布不接受目标页面。")
    blocks, source_sha256 = report_snapshot(path)
    if expected_sha256 is not None and source_sha256 != expected_sha256:
        raise JoySpaceError("报告内容已变化，请重新生成并预览后再发布。")
    with_charts = _has_charts(blocks)
    node, skill = _check_runtime()
    args = [str(skill / SLATE_SCRIPT), "--file", str(path), "--title", title.strip(),
            "--inline-charts" if with_charts else "--image-free", "--source-sha256", source_sha256]
    if placement == "child":
        args.extend(["--parent-page-id", target])
    elif placement == "sibling":
        args.extend(["--page-url", "https://joyspace.jd.com/pages/" + target])
    try:
        data = _invoke(node, args, skill, timeout=180, publish=True)
        # This exact response is emitted only before auth or any remote action.
        if with_charts and data == {"status": "unsupported", "reason": "chart-browser-unavailable",
                                   "remoteAttempted": False}:
            raise _ChartsUnavailable(CHARTS_UNSUPPORTED)
        page_id = data.get("pageId")
        marker = "chartsVerified" if with_charts else "imageFree"
        if (data.get("verified") is not True or data.get(marker) is not True
                or data.get("title") != title.strip() or data.get("sourceSha256") != source_sha256
                or not isinstance(page_id, str) or not PAGE_ID.fullmatch(page_id)):
            raise JoySpaceError(PUBLISH_UNKNOWN)
    except _ChartsUnavailable:
        raise JoySpaceError(CHARTS_UNSUPPORTED) from None
    except JoySpaceError:
        # The document may exist despite a lost confirmation; look it up instead of
        # republishing, and mark the link as not content-verified.
        return _recover(title.strip())
    # Do not forward arbitrary links, authentication metadata or local paths.
    return {"pageId": page_id, "link": "https://joyspace.jd.com/pages/" + page_id, "verified": True}