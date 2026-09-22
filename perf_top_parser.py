"""Bounded Android Top archive parsing; sysmonitor's parser stays unchanged."""
import json
import math
import re
import tempfile
from datetime import datetime, timezone

from perf_parser import MAX_LINE_BYTES, ParsedLine

MAX_SAMPLE_BYTES = 16 * 1024 * 1024
_HEADER = re.compile(
    r"^=+\s*Top 采集 #\d+ 时间:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r"(?:\s+(UTC|\[UTC\]))?\s*=+$")
_TOP_MARKER = re.compile(r"^=+\s*Top 采集")
_SYS_MARKER = re.compile(r"^(?:S|P|D|DE|DP),")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SIZE = re.compile(r"(\d+(?:\.\d+)?)([BKMGT]?)(?:i?B)?", re.I)
_CPU = re.compile(r"(\d+(?:\.\d+)?)%([a-z]+)", re.I)


def _lines(stream):
    """One result per physical line, with bounded reads even for corrupt input."""
    number = 0
    while True:
        raw = stream.readline(MAX_LINE_BYTES + 1)
        if not raw:
            return
        number += 1
        if len(raw) > MAX_LINE_BYTES:
            size = len(raw)
            while raw and not raw.endswith(b"\n"):
                raw = stream.readline(MAX_LINE_BYTES + 1)
                size += len(raw)
            yield number, None, "单行超过 1 MiB，已跳过", size
            continue
        size = len(raw)
        try:
            text = raw.decode("utf-8-sig" if number == 1 else "utf-8")
        except UnicodeError:
            yield number, None, "非法 UTF-8，已跳过", size
            continue
        error = None if raw.endswith(b"\n") else "文件末尾残行，已跳过"
        yield number, _ANSI.sub("", text).strip(), error, size


def detect_source_format(stream):
    """Scan the entire seekable source, rejecting mixed formats even at EOF."""
    found = set()
    stream.seek(0)
    try:
        for _, text, _, _ in _lines(stream):
            if text is None:
                continue
            if _TOP_MARKER.match(text):
                found.add("top")
            elif _SYS_MARKER.match(text):
                found.add("sysmonitor")
            if len(found) > 1:
                raise ValueError("同一导入不能混合 Top/sysmonitor（同文件混合格式）")
    finally:
        stream.seek(0)
    return next(iter(found), None)


def _number(text):
    value = float(text.rstrip("%"))
    if not math.isfinite(value) or value < 0:
        raise ValueError("非法非负数")
    return value


def _size_kb(text):
    match = _SIZE.fullmatch(text)
    if not match:
        raise ValueError("非法内存大小：" + text[:80])
    # Top's unsuffixed memory values are KB; only explicit B denotes bytes.
    factor = {"": 1, "B": 1 / 1024, "K": 1,
              "M": 1024, "G": 1024 ** 2, "T": 1024 ** 3}[match[2].upper()]
    value = float(match[1]) * factor
    if not math.isfinite(value):
        raise ValueError("内存大小超出范围")
    return value


def top_header_fields(text):
    """Split Top's bracketed sort marker, including S[%CPU]%SCPU.

    Only recognize header lines; never normalize brackets in process commands.
    Shared by capture validation and archive parsing to keep column offsets equal.
    """
    text = _ANSI.sub("", text).lstrip("\ufeff")
    fields = text.replace("[", " ").replace("]", " ").split()
    if "PID" in fields and fields[0] in (
            "PID", "USER", "PR", "NI", "VIRT", "RES", "SHR", "S", "%CPU",
            "CPU%", "ARGS", "CMD", "COMMAND", "NAME", "%MEM", "TIME+", "VSWAP", "%SCPU"):
        return fields
    return []


def _columns(text):
    fields = top_header_fields(text)
    if "PID" not in fields or "RES" not in fields:
        raise ValueError("Top 表头缺少 PID/RES")
    cpu = next((key for key in ("%CPU", "CPU%") if key in fields), None)
    command = next((key for key in ("ARGS", "CMD", "COMMAND", "NAME") if key in fields), None)
    if cpu is None or command is None:
        raise ValueError("Top 表头缺少 CPU/命令列")
    return fields, fields.index(cpu), fields.index(command)


def _system(ts, sample, zone):
    return dict(ts=ts, source_format="top", _sample=sample, timestamp_timezone=zone,
                cpu_total=None, cpu_user=None, cpu_sys=None, cpu_iow=None,
                cpu_irq=None, cpu_idle=None, cpu_capacity=None, cpu_single_core=None,
                mem_total_mb=None, mem_used_mb=None, mem_free_mb=None, mem_avail_mb=None)


def _cpu_values(text):
    values = {}
    for token in text.split():
        match = _CPU.fullmatch(token.rstrip(","))
        if not match:
            raise ValueError("Top CPU 分项格式错误")
        key = match[2].lower()
        if key in values:
            raise ValueError("Top CPU 分项重复")
        values[key] = _number(match[1])
    capacity, idle = values.get("cpu"), values.get("idle")
    if capacity is None or capacity <= 0 or idle is None or idle > capacity:
        raise ValueError("Top CPU 容量/idle 无效或缺失")
    if any(value > capacity for value in values.values()):
        raise ValueError("Top CPU 分项超过容量")
    used = capacity - idle
    result = dict(cpu_capacity=capacity, cpu_single_core=used,
                  cpu_total=used / capacity * 100, cpu_idle=idle / capacity * 100)
    for target, key in (("cpu_user", "user"), ("cpu_sys", "sys"), ("cpu_iow", "iow")):
        result[target] = values[key] / capacity * 100 if key in values else None
    irq = [values[key] for key in ("irq", "sirq", "softirq") if key in values]
    result["cpu_irq"] = sum(irq) / capacity * 100 if irq else None
    return result


def _mem_values(text):
    values = {}
    for part in text.split(":", 1)[1].split(","):
        fields = part.split()
        if len(fields) == 2 and fields[1] in ("total", "used", "free"):
            values["mem_" + fields[1] + "_mb"] = _size_kb(fields[0]) / 1024
    if len(values) != 3:
        raise ValueError("Top Mem total/used/free 无效或缺失")
    return values


def _process(text, columns):
    fields, cpu_index, command_index = columns
    # A command in a middle column consumes all text except trailing columns.
    parts = text.split(None, command_index)
    if len(parts) <= command_index:
        raise ValueError("Top 进程残行")
    tail_count = len(fields) - command_index - 1
    tail = parts.pop()
    parts.extend(tail.rsplit(None, tail_count) if tail_count else [tail])
    if len(parts) != len(fields):
        raise ValueError("Top 进程字段数量错误")
    pid_text = parts[fields.index("PID")]
    if not re.fullmatch(r"\d+", pid_text) or not 0 < int(pid_text) <= 2 ** 63 - 1:
        raise ValueError("Top PID 无效")
    name = parts[command_index].strip()
    cpu = _number(parts[cpu_index])
    rss = _size_kb(parts[fields.index("RES")])
    if name == "top" or name.startswith("top ") or name.startswith("top\t"):
        return None
    return dict(pid=int(pid_text), name=name, cpu1c=cpu, rss_kb=rss)


def iter_top_records(stream):
    """Yield S then P per header; timestamps are epoch milliseconds, no boot guess.

    Every sample has a header-line _sample identity; the Store removes it.
    Oversized samples are discarded wholesale, errors retain physical line numbers.
    """
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as spool:
        system, columns, header_line = None, None, 0
        sample_bytes, valid, overflow = 0, False, False

        def flush():
            if system is None or not valid or overflow:
                return
            yield ParsedLine(header_line, "S", system, None)
            spool.seek(0)
            for row in spool:
                number, data = json.loads(row)
                capacity = system["cpu_capacity"]
                data.update(ts=system["ts"], source_format="top", _sample=header_line,
                            cpu=data["cpu1c"] / capacity * 100 if capacity else None)
                yield ParsedLine(number, "P", data, None)

        for number, text, error, size in _lines(stream):
            if text and _SYS_MARKER.match(text):
                raise ValueError("同一导入不能混合 Top/sysmonitor（同文件混合格式）")
            is_header = text is not None and _TOP_MARKER.match(text)
            if is_header:
                yield from flush()
                spool.seek(0)
                spool.truncate()
                system, columns = None, None
                header_line, sample_bytes, valid, overflow = number, size, False, False
            elif system is not None:
                sample_bytes += size
                if sample_bytes > MAX_SAMPLE_BYTES and not overflow:
                    overflow = True
                    yield ParsedLine(number, None, None, "单个 Top 样本超过 16 MiB，已整样本跳过")
            if error:
                yield ParsedLine(number, None, None, error)
                continue
            if is_header:
                try:
                    match = _HEADER.fullmatch(text)
                    if not match:
                        raise ValueError("Top 采集头格式错误")
                    date = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S")
                    if match[2]:
                        date = date.replace(tzinfo=timezone.utc)
                    # astimezone on naive dates uses the host zone at that date (DST).
                    date = date.astimezone()
                    zone = "UTC" if match[2] else "local"
                    system = _system(int(date.timestamp() * 1000), number, zone)
                except (ValueError, OverflowError, OSError) as exc:
                    yield ParsedLine(number, None, None, str(exc))
                continue
            if not text or overflow:
                continue
            if system is None:
                yield ParsedLine(number, None, None, None)
                continue
            try:
                if text.startswith("Mem:"):
                    system.update(_mem_values(text))
                    valid = True
                elif re.match(r"\S+%cpu\b", text, re.I):
                    system.update(_cpu_values(text))
                    valid = True
                elif top_header_fields(text):
                    columns = None
                    columns = _columns(text)
                elif text.startswith(("Tasks:", "Threads:", "Swap:", "Load average:", "Load Avg:")):
                    continue
                elif columns is not None or text[0].isdigit():
                    if columns is None:
                        raise ValueError("Top 进程缺少有效表头")
                    data = _process(text, columns)
                    if data is not None:
                        spool.write(json.dumps([number, data], ensure_ascii=False) + "\n")
                        valid = True
                elif not text.startswith(("Tasks:", "Threads:", "Swap:", "Load average:", "Load Avg:")):
                    yield ParsedLine(number, None, None, None)
            except (ValueError, OverflowError) as exc:
                yield ParsedLine(number, None, None, str(exc))
        yield from flush()