"""Self-contained offline HTML report entry point."""
from datetime import datetime, timezone
import hashlib
import threading
import zlib

import perf_report_compact
from perf_report_data import _progress_reporter, build_report_data
from perf_report_ui import CSS, JS, REPORT_CSP, render_document

# Bump when Python report/statistics semantics change; assets/CSP invalidate automatically.
REPORT_CACHE_VERSION = "27:" + hashlib.sha256(
    (CSS + "\0" + JS + "\0" + REPORT_CSP).encode("utf-8")).hexdigest()
# Bounded process-wide locks also coalesce requests through separate Store instances.
_REPORT_LOCKS = tuple(threading.Lock() for _ in range(64))


def timestamp(value, *, local=False):
    if value is None:
        return "—"
    try:
        date = datetime.fromtimestamp(value / 1000, timezone.utc)
        if local:
            date = date.astimezone()
        return date.isoformat(timespec="milliseconds")
    except (ValueError, OverflowError, OSError):
        return str(value) + " ms"


def render_report(store, session_id, progress=None, cancelled=None, *, style="default"):
    """Reuse a durable complete report; only successful generation is published."""
    if style not in ("default", "compact"):
        raise ValueError("未知报告样式")
    table = "report_compact_cache" if style == "compact" else "report_cache"
    version = REPORT_CACHE_VERSION + ":" + perf_report_compact.CACHE_VERSION if style == "compact" else REPORT_CACHE_VERSION
    renderer = perf_report_compact.render_document if style == "compact" else render_document
    report = _progress_reporter(progress, cancelled)
    key = (str(store.path.resolve()), session_id)
    lock = _REPORT_LOCKS[hash(key) % len(_REPORT_LOCKS)]
    report("waiting")
    while not lock.acquire(timeout=0.2):
        report("waiting")
    try:
        report("cache")
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise KeyError(session_id)
            cached = db.execute(
                f"SELECT document FROM {table} WHERE session=? AND version=?",
                (session_id, version)).fetchone()
        if cached:
            report("read_cache")
            try:
                document = zlib.decompress(cached[0]).decode("utf-8")
            except (zlib.error, UnicodeDecodeError):
                pass  # Interrupted/corrupt data is rebuilt, never served as a report.
            else:
                report("complete")
                return document
        # Both styles need the complete system stream for per-cycle overlays.
        data = build_report_data(store, session_id, progress=progress,
                                 cancelled=cancelled, include_full_system=True)
        report("render")
        document = renderer(data)
        report("compress")
        compressed = zlib.compress(document.encode("utf-8"))
        report("save")
        with store.connect() as db:
            # One atomic replacement, guarded against concurrent session deletion.
            saved = db.execute(
                f"INSERT INTO {table}(session,version,document) "
                "SELECT id,?,? FROM sessions WHERE id=? "
                "ON CONFLICT(session) DO UPDATE SET version=excluded.version,document=excluded.document",
                (version, compressed, session_id))
            if not saved.rowcount:
                raise KeyError(session_id)
            report("save")  # Cancellation before commit leaves the previous cache intact.
        report("complete")
        return document
    finally:
        lock.release()