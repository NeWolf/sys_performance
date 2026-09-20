"""Self-contained offline HTML report entry point."""
from datetime import datetime, timezone

from perf_report_data import build_report_data
from perf_report_ui import REPORT_CSP, render_document


def timestamp(value):
    if value is None:
        return "—"
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError, OSError):
        return str(value) + " ms"


def render_report(store, session_id):
    """Render deterministic standalone HTML from exact report data."""
    return render_document(build_report_data(store, session_id))