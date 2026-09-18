"""Streaming parser for sysmonitor perf_test v1 (Python 3.9+)."""
import math
import re
from pathlib import Path
from typing import BinaryIO, Iterator, NamedTuple, Optional


SCHEMAS = {
    "S": "ts boot cpu_total cpu_user cpu_sys cpu_iow cpu_irq mem_avail_mb mem_total_mb mem_used_mb",
    "P": "ts pid cpu cpu1c dmips wait pol cpu_core state nthr rss_kb rss_delta_kb majflt rd_kb wr_kb rchar_kb wchar_kb syscr syscw name",
    "D": "ts total_mb total_objs",
    "DE": "ts exporter size_mb objs",
    "DP": "ts pid size_kb objs name",
}
FLOATS = {"cpu_total", "cpu_user", "cpu_sys", "cpu_iow", "cpu_irq", "cpu", "cpu1c", "dmips", "wait", "total_mb", "size_mb"}
TEXT = {"pol", "state", "name", "exporter"}
SIGNED = {"rss_delta_kb", "cpu_core"}
MAX_LINE_BYTES = 1024 * 1024


class ParsedLine(NamedTuple):
    number: int
    kind: Optional[str]
    data: Optional[dict]
    error: Optional[str]


def rotation_key(name: str):
    """Oldest numbered rotation first; unrelated names retain lexical ordering."""
    base = Path(name.replace("\\", "/")).name
    match = re.fullmatch(r"perf(?:\.(\d+))?\.log", base)
    return (0, -int(match.group(1) or 0), base) if match else (1, 0, base)


def parse_line(raw: str) -> tuple:
    text = raw.rstrip("\r\n")
    kind = text.split(",", 1)[0]
    if kind not in SCHEMAS:
        return None, None
    fields = SCHEMAS[kind].split()
    # Only trailing names absorb the remainder. DE.exporter is a middle column.
    values = text.split(",", len(fields)) if kind in {"P", "DP"} else text.split(",")
    if len(values) != len(fields) + 1:
        raise ValueError("字段数量错误：%s 应为 %d 列，实际 %d 列" % (kind, len(fields) + 1, len(values)))
    data = {}
    for key, value in zip(fields, values[1:]):
        if key in TEXT:
            if not value:
                raise ValueError("空文本字段：" + key)
            data[key] = value
        elif key in FLOATS:
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError("非法非负数：" + key)
            data[key] = number
        else:
            if not re.fullmatch(r"[+-]?\d+", value):
                raise ValueError("非法整数：" + key)
            number = int(value)
            if key not in SIGNED and number < 0:
                raise ValueError("负数：" + key)
            if key == "cpu_core" and number < -1:
                raise ValueError("非法 CPU 核编号")
            upper = 2 ** 64 - 1 if key in {"rd_kb", "wr_kb", "rchar_kb", "wchar_kb", "syscr", "syscw", "size_kb", "majflt"} else 2 ** 63 - 1
            if not -(2 ** 63) <= number <= upper:
                raise ValueError("整数超出存储范围：" + key)
            data[key] = number
    return kind, data


def iter_records(stream: BinaryIO) -> Iterator[ParsedLine]:
    """Bound memory even for corrupt giant lines. Incomplete final rows are rejected."""
    line_number = 0
    first = True
    while True:
        raw = stream.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        line_number += 1
        if len(raw) > MAX_LINE_BYTES:
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(MAX_LINE_BYTES + 1)
            yield ParsedLine(line_number, None, None, "单行超过 1 MiB，已跳过")
            continue
        if not raw.endswith(b"\n"):
            yield ParsedLine(line_number, None, None, "文件末尾残行，已跳过")
            continue
        try:
            text = raw.decode("utf-8-sig" if first else "utf-8")
            first = False
            if not text.strip():
                continue
            kind, data = parse_line(text)
            yield ParsedLine(line_number, kind, data, None)
        except (UnicodeError, ValueError) as exc:
            yield ParsedLine(line_number, None, None, str(exc))