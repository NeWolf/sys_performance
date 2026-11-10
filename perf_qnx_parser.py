"""Bounded seven-column QNX CSV parsing, without Android metric conversion."""
import csv
import math
import re

from perf_parser import ParsedLine
from perf_top_parser import _lines

MODULES = {"QC": ("cpu",), "QG": ("gpu",), "QDSP": ("cdsp", "cdsp1"), "QDDR": ("ddr",)}
HEADER = ["kind", "ts", "module", "metric", "value", "unit", "qnx_relative_s"]
REQUIRED_METRICS = {"cpu": "totalcpu", "gpu": "utilization", "cdsp": "load",
                    "cdsp1": "load", "ddr": "ddr_bw_total"}
_NUMBER = re.compile(r"\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _number(text, field):
    if not text:
        return None
    if not _NUMBER.fullmatch(text):
        raise ValueError("非法非负数：" + field)
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise ValueError("非法有限非负数：" + field)
    return value


def parse_qnx_line(text):
    fields = next(csv.reader([text], strict=True))
    if fields == HEADER:
        return None, None
    if not fields or fields[0] not in MODULES:
        raise ValueError("未知 QNX kind")
    if len(fields) != 7:
        raise ValueError("QNX 应为七列")
    kind, ts, module, metric, value, unit, relative = fields
    if not re.fullmatch(r"[0-9]{1,15}", ts) or int(ts) > 253402300799999:
        raise ValueError("非法 QNX UTC 毫秒时间戳")
    if module not in MODULES[kind]:
        raise ValueError("QNX kind 与 module 不匹配")
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}", metric):
        raise ValueError("非法 QNX metric")
    if not unit or len(unit) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in unit):
        raise ValueError("非法 QNX unit")
    return kind, dict(ts=int(ts), module=module, metric=metric,
                      value=_number(value, "value"), unit=unit,
                      qnx_relative_s=_number(relative, "qnx_relative_s"), source_format="qnx")


def iter_qnx_records(stream):
    """Preserve explicit empty numeric cells as NULL; reject invalid rows, not zero-fill."""
    for number, text, error, _ in _lines(stream, strip_ansi=False):
        if error:
            yield ParsedLine(number, None, None, error)
            continue
        if not text:
            continue
        try:
            kind, data = parse_qnx_line(text)
            if kind is not None:
                yield ParsedLine(number, kind, data, None)
        except (ValueError, csv.Error) as exc:
            yield ParsedLine(number, None, None, str(exc))