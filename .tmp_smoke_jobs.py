"""Throwaway smoke test: AdmissionJobs end to end on the block tree."""
import json
import tempfile
from pathlib import Path

import perf_admission
import perf_admission_jobs
import perf_slate
from perf_report_data import REQUIRED_PROCESSES

raw = json.load(open("/tmp/tpl_raw.json"))
blocks = raw["raw"]["content"]["content"]


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
perf_admission_jobs.read_template = lambda: perf_slate.validate_tree(blocks)

sent = []


class Jobs(perf_admission_jobs.AdmissionJobs):
    def _publish(self, record):
        request = record["publish_request"]
        self._checked_slate(record)
        sent.append((request["placement"], request["parent_page"]))
        record.update(state="published", link="https://joyspace.jd.com/pages/Page123")
        self._finish(record)


with tempfile.TemporaryDirectory() as tmp:
    jobs = Jobs(Store(), tmp)
    draft = "a" * 32
    jobs.create("b" * 32, draft, "标题")
    jobs._executor.shutdown(wait=True)
    jobs._executor = perf_admission_jobs.ThreadPoolExecutor(max_workers=1)
    state = jobs.get("b" * 32, draft)
    print("state:", state["state"], "warnings:", len(state["warnings"]))
    body = jobs.preview("b" * 32, draft)
    print("preview chars:", len(body), "has table:", "<table class=\"tbl\">" in body)
    print("result:", jobs.publish("b" * 32, draft, "personal", None, True))
    jobs._executor.shutdown(wait=True)
    print("sent:", sent)
    jobs.close()