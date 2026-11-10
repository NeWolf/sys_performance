"""Convert sysprofiler text tables to typed, headerless perf CSV records."""
import csv
import math
from pathlib import Path
import re
import subprocess
import time


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired('QNX operation', 0)

MAX_LINE_CHARS = 65536
MAX_COLUMNS = 256
MAX_RECORDS = 1000000
MAX_DIAGNOSTICS = 1000

MODULES = {'cpuprofiler': ('QC', 'cpu'), 'gpuprofiler': ('QG', 'gpu'),
           'cdspprofiler': ('QDSP', 'cdsp'), 'cdsp1profiler': ('QDSP', 'cdsp1'),
           'ddrprofiler': ('QDDR', 'ddr')}


def metric(column):
    unit = ''
    match = re.search(r'\(([^)]+)\)', column)
    if match:
        unit = match.group(1)
    elif column.endswith('%'):
        unit = '%'
    name = re.sub(r'\([^)]*\)', '', column).rstrip('%')
    name = re.sub(r'[^a-zA-Z0-9]+', '_', name).strip('_').lower()
    return name, unit or 'reported'


def read_table(path, deadline=None):
    header = None
    with path.open(errors='replace') as stream:
        lines = iter(lambda: stream.readline(MAX_LINE_CHARS + 1), '')
        for line_no, line in enumerate(lines, 1):
            check_deadline(deadline)
            if len(line) > MAX_LINE_CHARS:
                raise ValueError('Conversion resource limit exceeded')
            parts = line.split()
            if len(parts) > MAX_COLUMNS:
                raise ValueError('Conversion column limit exceeded')
            if not parts:
                continue
            if parts[0] == 'SystemTime':
                header = parts
                continue
            if header is None:
                continue
            try:
                relative = float(parts[0])
            except ValueError:
                continue
            if not math.isfinite(relative) or relative < 0:
                continue
            if len(parts) != len(header):
                yield relative, [], f'{path.name}:{line_no}: column count mismatch ({len(parts)} != {len(header)})'
                continue
            values = []
            for column, token in zip(header[1:], parts[1:]):
                try:
                    value = float(token)
                except ValueError:
                    values.append((column, '', f'invalid value {token!r}'))
                    continue
                if not math.isfinite(value) or value < 0:
                    values.append((column, '', f'unavailable value {token!r}'))
                else:
                    values.append((column, token, None))
            yield relative, values, None


def has_clock_anchor(state):
    anchor = state.get('estimated_start_utc_ms')
    error = state.get('alignment_error_ms')
    return (type(anchor) in (int, float) and 0 < anchor < 253402300799000
            and math.isfinite(anchor) and type(error) in (int, float)
            and 0 <= error <= 253402300799000 and math.isfinite(error))


def deadline_sorted(items, deadline, key=None):
    # Bound each uninterruptible sort; check every merge item as well.
    import heapq
    chunks, chunk = [], []
    for item in items:
        check_deadline(deadline)
        chunk.append(item)
        if len(chunk) == 4096:
            chunks.append(sorted(chunk, key=key))
            check_deadline(deadline)
            chunk = []
    if chunk:
        chunks.append(sorted(chunk, key=key))
    check_deadline(deadline)
    for item in heapq.merge(*chunks, key=key):
        check_deadline(deadline)
        yield item


def profiler_files(raw, deadline):
    import os
    for parent, dirs, files in os.walk(raw, followlinks=False):
        check_deadline(deadline)
        for names, is_file in ((dirs, False), (files, True)):
            for name in names:
                check_deadline(deadline)
                path = Path(parent) / name
                if path.is_symlink():
                    raise ValueError('Unsafe raw artifact')
                if is_file and path.match('*profiler_*.txt'):
                    yield path
    check_deadline(deadline)


def convert(raw, output, state, deadline=None):
    local_deadline = time.monotonic() + 120
    deadline = local_deadline if deadline is None else min(deadline, local_deadline)
    check_deadline(deadline)
    if not has_clock_anchor(state):
        raise ValueError('No valid clock anchor; raw-only, UTC conversion forbidden')
    raw, output = Path(raw), Path(output)
    targets = (raw, output, Path(str(output) + '.tmp'), Path(str(output) + '.diagnostics.txt'))
    if any(p.is_symlink() for target in targets for p in (target, *target.parents)):
        raise ValueError('Unsafe conversion path')
    records, diagnostics, found = [], [], set()
    required_metrics = {'cpu': 'totalcpu', 'gpu': 'utilization', 'cdsp': 'load',
                        'cdsp1': 'load', 'ddr': 'ddr_bw_total'}
    load_found = set()
    samples = {name: 0 for _, name in MODULES.values()}
    for path in deadline_sorted(profiler_files(raw, deadline), deadline):
        module = next((key for key in MODULES if path.name.startswith(key + '_')), None)
        if module is None:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(raw.resolve()):
            raise ValueError('Unsafe raw artifact')
        typ, source = MODULES[module]
        for relative, values, error in read_table(path, deadline):
            new_diagnostics = sum(bool(item[2]) for item in values) + bool(error)
            if (len(records) + len(values) > MAX_RECORDS
                    or len(diagnostics) + new_diagnostics > MAX_DIAGNOSTICS):
                raise ValueError('Conversion resource limit exceeded')
            if relative > (253402300799000 - state['estimated_start_utc_ms']) / 1000:
                raise ValueError('Timestamp outside UTC range')
            if error:
                diagnostics.append(error)
                continue
            valid = False
            ts = round(state['estimated_start_utc_ms'] + relative * 1000)
            for column, value, error in values:
                name, unit = metric(column)
                # v2.4.2 GPU labels utilization without a unit; it is a percentage.
                if source == 'gpu' and name == 'utilization':
                    unit = '%'
                if not error and name == required_metrics[source]:
                    load_found.add(source)
                # Preserve the vendor column semantics; do not silently normalize CPU or infer GPU load.
                records.append([typ, ts, source, name, value, unit, f'{relative:.6f}'])
                if error:
                    diagnostics.append(f'{path.name}: {relative}: {column}: {error}')
                else:
                    valid = True
            if valid:
                found.add(source)
                samples[source] += 1
    ordered = deadline_sorted(records, deadline, key=lambda row: (row[1], row[2], row[3]))
    temp = Path(str(output) + '.tmp')
    with temp.open('w', newline='') as stream:
        csv.writer(stream, lineterminator='\n').writerows(ordered)
        check_deadline(deadline)
    check_deadline(deadline)
    report = {'rows': len(records), 'samples': samples,
              'missing_modules': sorted(set(samples) - load_found),
              'modules_without_any_values': sorted(set(samples) - found), 'diagnostics': diagnostics,
              'alignment_error_ms': state['alignment_error_ms']}
    Path(str(output) + '.diagnostics.txt').write_text('\n'.join(diagnostics) + ('\n' if diagnostics else ''))
    check_deadline(deadline)
    temp.replace(output)
    check_deadline(deadline)
    return report
