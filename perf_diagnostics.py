"""Bounded, allowlisted local diagnostics; never serialize business inputs/errors."""
from contextlib import contextmanager
from contextvars import ContextVar
import queue
import uuid
from datetime import datetime, timezone
from functools import wraps
import json
import math
import os
from pathlib import Path
import stat
import threading
import time

_STAGES = frozenset({
    "adb.stop", "adb.pull", "adb.transfer", "top.stop", "top.pull",
    "top.transfer", "top.archive", "top.import", "import.total",
    "import.fingerprint", "import.detect", "import.lock", "import.parse",
    "import.commit", "analysis.statistics", "analysis.scope", "analysis.aggregate",
    "analysis.process_ranks", "analysis.name_ranks", "analysis.rss_ranks",
    "analysis.process_lock", "analysis.name_lock", "analysis.rss_lock",
    "joyspace.auth", "joyspace.gateway", "joyspace.gateway_result", "joyspace.port",
    "joyspace.auth_process", "joyspace.invoke", "joyspace.template", "joyspace.publish",
})
_ENUMS = {
    "status": {"selected", "begin", "ok", "error", "timeout"},
    "source": {"env", "config", "tokenGrant", "legacy"},
    "operation": {"encrypt", "tokenGrant", "getWebToken", "hioffice", "read", "publish"},
    "phase": {"headers", "body"},
    "source_format": {"top", "sysmonitor"},
    "kind": {"S", "P", "D", "DE", "DP"},
    "code": {"ok", "remote_error", "http_error", "missing_auth_header", "unknown_error",
             "timeout", "ECONNREFUSED", "ECONNRESET", "ETIMEDOUT", "ENOTFOUND", "EAI_AGAIN",
             "UND_ERR_CONNECT_TIMEOUT", "UND_ERR_HEADERS_TIMEOUT", "UND_ERR_BODY_TIMEOUT",
             "CERT_HAS_EXPIRED", "UNABLE_TO_VERIFY_LEAF_SIGNATURE"},
}
_NUMBERS = {key: (0, 2**53 - 1) for key in
            ("files", "bytes", "records", "errors", "unknown", "samples")}
_NUMBERS.update(elapsed_ms=(0, 86400000), http_status=(100, 599),
                port=(8988, 9006), attempt=(1, 10))
_BOOLS = {"duplicate", "cache_hit", "has_auth_header"}
_MAX_BYTES = 2 * 1024 * 1024
_lock = threading.Lock()
_config_lock = threading.Lock()
_directory = None
_queue = queue.Queue(maxsize=1024)
_worker = None
_operation = ContextVar("diagnostic_operation_id", default=None)
_current_span = ContextVar("diagnostic_span_id", default=None)
_parent_span = ContextVar("diagnostic_parent_span_id", default=None)


def _drain():
    while True:
        item = _queue.get()
        try:
            if isinstance(item, threading.Event):
                item.set()
            else:
                directory, line = item
                _append(directory, line)
        except Exception:
            pass
        finally:
            _queue.task_done()


def flush(timeout=2):
    """Best-effort bounded drain for shutdown/tests, never wait indefinitely on a disk."""
    if _worker is None:
        return True
    marker = threading.Event()
    try:
        _queue.put_nowait(marker)
    except queue.Full:
        return False
    return marker.wait(timeout)


def configure(directory=None):
    """Set the application-owned writable directory, or disable logging with None."""
    global _directory, _worker
    try:
        destination = Path(directory).absolute() if directory is not None else None
        with _config_lock:
            if destination is not None and _worker is None:
                worker = threading.Thread(target=_drain, name="safe-diagnostics", daemon=True)
                worker.start()
                _worker = worker
            _directory = destination
    except Exception:
        _directory = None


def _safe_info(path, *, directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or (not directory and info.st_nlink != 1)):
        raise OSError("unsafe diagnostic destination")
    return info


def _append(directory, line):
    # Only the consumer takes this I/O lock; producers enqueue without waiting.
    with _lock:
        _safe_info(directory.parent, directory=True)
        directory.mkdir(mode=0o700, exist_ok=True)
        _safe_info(directory, directory=True)
        path = directory / "runtime.jsonl"
        backup = directory / "runtime.jsonl.1"
        info = _safe_info(path)
        _safe_info(backup)
        if info is not None and info.st_size + len(line) > _MAX_BYTES:
            os.replace(path, backup)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            current = _safe_info(path)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or current is None or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
                raise OSError("unsafe diagnostic destination")
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            # Low-frequency stage boundaries only; no per-record fsync in hot paths.
            if os.write(fd, line) != len(line):
                raise OSError("incomplete diagnostic write")
        finally:
            os.close(fd)


def emit(stage, **fields):
    """Drop unknown fields/values and suppress sink failures, without formatting errors."""
    try:
        directory = _directory
        if directory is None or type(stage) is not str or stage not in _STAGES:
            return
        record = {"time_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "stage": stage}
        for key, context in (("operation_id", _operation), ("span_id", _current_span),
                             ("parent_span_id", _parent_span)):
            value = context.get()
            if (type(value) is str and len(value) == 32
                    and all(char in "0123456789abcdef" for char in value)):
                record[key] = value
        for key, value in fields.items():
            if key in _ENUMS:
                if type(value) is str and value in _ENUMS[key]:
                    record[key] = value
            elif key in _NUMBERS:
                low, high = _NUMBERS[key]
                if (type(value) is int and low <= value <= high
                        and (key != "port" or value % 2 == 0)):
                    record[key] = value
            elif key in _BOOLS and type(value) is bool:
                record[key] = value
        line = (json.dumps(record, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
        _queue.put_nowait((directory, line))
    except Exception:
        pass


def _elapsed(started):
    value = (time.monotonic() - started) * 1000
    return max(0, min(86400000, round(value))) if math.isfinite(value) else 0


@contextmanager
def span(stage, **fields):
    """Yield mutable counters; log boundaries only, preserving result/exception identity."""
    progress = dict(fields)
    started = None
    try:
        started = time.monotonic()
    except Exception:
        pass
    with correlation():
        try:
            emit(stage, **dict(progress, status="begin"))
        except Exception:
            pass
        outcome = "error"
        try:
            yield progress
            outcome = "ok"
        finally:
            try:
                terminal = dict(progress, status=outcome)
                if started is not None:
                    terminal["elapsed_ms"] = _elapsed(started)
                emit(stage, **terminal)
            except Exception:
                pass


@contextmanager
def correlation():
    """Attach random internal IDs, independent of user/device/document identifiers."""
    tokens = []
    try:
        operation_id = _operation.get() or uuid.uuid4().hex
        span_id = uuid.uuid4().hex
        parent_id = _current_span.get()
        for context, value in ((_operation, operation_id), (_parent_span, parent_id),
                               (_current_span, span_id)):
            tokens.append((context, context.set(value)))
    except Exception:
        pass
    try:
        yield
    finally:
        for context, token in reversed(tokens):
            try:
                context.reset(token)
            except Exception:
                pass


def measured(stage):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with span(stage):
                return function(*args, **kwargs)
        return wrapped
    return decorate