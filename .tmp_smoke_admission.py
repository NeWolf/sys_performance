"""Throwaway smoke test: fill the real template block tree end to end."""
import json
import tempfile
from pathlib import Path

import perf_admission
import perf_slate
from perf_report_data import REQUIRED_PROCESSES

raw = json.load(open("/tmp/tpl_raw.json"))
blocks = raw["raw"]["content"]["content"]
print("blocks:", len(blocks))

layout = perf_slate.validate_template(blocks, [e["name"] for e in REQUIRED_PROCESSES])
print("names:", len(layout.names), "data_rows:", len(layout.data_rows))


class Store:
    def groups(self, session_id):
        return []


def fake_report_data(store, session_id, include_full_system=False):
    return {
        "systems": [{"segment": 1, "full_points": [
            {"cpu_single_core": 12.5, "mem_used_mb": 2048.0},
            {"cpu_single_core": 80.0, "mem_used_mb": 4096.0}]}],
        "processes": [],
        "required": [{"name": e["name"], "matches": [], "truncated": False,
                      "excel_row": e["excel_row"]} for e in REQUIRED_PROCESSES],
        "full_cycle_schema": ["ts", "cycle", "cpu1c", "rss_kb"],
    }


perf_admission.build_report_data = fake_report_data

with tempfile.TemporaryDirectory() as tmp:
    result = perf_admission.build_admission(Store(), "s1", blocks, Path(tmp))
    out = Path(tmp) / "report.json"
    print("report.json bytes:", out.stat().st_size)
    print("html chars:", len(result["html"]))
    print("warnings:", len(result["warnings"]))
    overall = perf_slate.rows_of(perf_slate.validate_template(result["blocks"]).overall)
    print("overall rows:", [perf_slate.texts_of(r) for r in overall])