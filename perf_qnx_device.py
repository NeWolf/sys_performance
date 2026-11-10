#!/usr/bin/env python3
"""Host-side QNX sysprofiler capture through adb. See README.md."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import threading
import time
import uuid

from perf_qnx_convert import check_deadline, convert, has_clock_anchor


MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 10000


def emit(*args, **kwargs):
    """Embedded mode: no process-wide stdout changes or raw device output."""


class CaptureError(RuntimeError):
    pass


class MissingModulesError(CaptureError):
    """The archive and converted partial data have already been persisted."""


def atomic_json(path, value, deadline=None):
    check_deadline(deadline)
    temp = path.with_suffix('.tmp')
    if any(p.is_symlink() for target in (path, temp) for p in (target, *target.parents)):
        raise CaptureError('Unsafe state path')
    with temp.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    check_deadline(deadline)
    temp.replace(path)


class Connection:
    def __init__(self, adb, serial, address, deadline=None):
        self.adb = [adb, '-s', serial]
        self.address = address
        self.root_prefix = None
        self.deadline = time.monotonic() + 900 if deadline is None else deadline

    def call(self, args, check=True, timeout=90):
        remaining = min(timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            raise subprocess.TimeoutExpired('QNX operation', timeout)
        p = subprocess.run(self.adb + args, capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=remaining, shell=False)
        if check and p.returncode:
            raise CaptureError(p.stderr.decode(errors='replace') + p.stdout.decode(errors='replace'))
        return p

    def android(self, command, check=True, timeout=90):
        if self.root_prefix is None:
            uid = self.call(['shell', 'id -u']).stdout.decode().strip()
            if uid == '0':
                self.root_prefix = ''
            else:
                failures = []
                for prefix in ('su 0 sh -c ', 'su -c '):
                    p = self.call(['shell', prefix + shlex.quote('id -u')], check=False)
                    if not p.returncode and p.stdout.decode().strip() == '0':
                        self.root_prefix = prefix
                        break
                    failures.append(prefix.strip() + ': ' +
                                    (p.stderr + p.stdout).decode(errors='replace').strip())
                else:
                    raise CaptureError('Android shell needs root or working su; adb root is not run automatically.\n' +
                                       '\n'.join(failures))
        cmd = self.root_prefix + shlex.quote(command) if self.root_prefix else command
        return self.call(['shell', cmd], check=check, timeout=timeout)

    def ssh(self, script, check=True, timeout=90):
        command = 'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new ' + shlex.quote('root@' + self.address)
        command += ' ' + shlex.quote('sh -c ' + shlex.quote(script))
        return self.android(command, check=check, timeout=timeout)

    def clock(self):
        t0 = time.monotonic_ns()
        p = self.android('date +%s; cat /proc/uptime; cat /proc/sys/kernel/random/boot_id')
        t1 = time.monotonic_ns()
        lines = p.stdout.decode().strip().splitlines()
        try:
            return {'epoch_s': int(lines[0]), 'uptime_s': float(lines[1].split()[0]),
                    'boot_id': lines[2], 'host_mid_ns': (t0+t1)//2, 'rtt_ms': (t1-t0)/1e6}
        except (ValueError, IndexError) as e:
            raise CaptureError('Cannot read Android clock: ' + p.stdout.decode()) from e


def select_device(adb, requested):
    if requested:
        return requested
    try:
        p = subprocess.run([adb, 'devices'], capture_output=True, check=True, stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.CalledProcessError) as e:
        raise CaptureError('Cannot execute adb: ' + str(e)) from e
    devices = [row.split()[0] for row in p.stdout.decode().splitlines()[1:]
               if len(row.split()) >= 2 and row.split()[1] == 'device']
    if len(devices) != 1:
        raise CaptureError('Exactly one online device required, or use -s. Online devices: ' + ', '.join(devices))
    return devices[0]


def save(folder, state, deadline=None):
    atomic_json(folder / 'session.json', state, deadline)


def load(folder):
    try:
        state = json.loads((folder / 'session.json').read_text())
    except (OSError, ValueError) as e:
        raise CaptureError('No valid session.json in ' + str(folder)) from e
    validate_state(state)
    return state


def validate_state(state):
    if not isinstance(state, dict):
        raise CaptureError('Invalid session metadata')
    if not isinstance(state.get('session_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', state['session_id']):
        raise CaptureError('Invalid session identifier')
    expected = '/tmp/qnx_perf_' + state['session_id']
    if state.get('qnx_dir') != expected or state.get('android_dir') != '/data/local/tmp/qnx_perf_' + state['session_id']:
        raise CaptureError('Invalid remote session paths')


def remote_save(conn, state):
    remote = state['qnx_dir']
    payload = shlex.quote(json.dumps(state, ensure_ascii=True))
    conn.ssh(f"printf '%s\\n' {payload} > {remote}/session.json.tmp && "
             f'mv {remote}/session.json.tmp {remote}/session.json')


def sessions(args, folder=None):
    conn = make_connection(args)
    result = conn.ssh('''for d in /tmp/qnx_perf_*; do
 sid=${d#/tmp/qnx_perf_}
 case "$sid" in ''|*[!0-9a-f]*) continue;; esac
 [ "${#sid}" = 32 ] || continue
 [ -d "$d" ] || continue
 state=unknown
 if [ -f "$d/exit_code" ]; then state=stopped
 elif [ -f "$d/profiler.pid" ]; then
  p=$(cat "$d/profiler.pid")
  case "$p" in ''|*[!0-9]*) ;; *)
   info=$(pidin -p "$p" ar)
   case "$info" in *sysprofiler_app*"$d/results"*) state=running;; esac;; esac
 fi
 metadata=no; [ ! -f "$d/session.json" ] || metadata=yes
 printf '%s %s metadata=%s\\n' "$sid" "$state" "$metadata"
done''')
    rows = []
    for line in result.stdout.decode(errors='replace').splitlines():
        match = re.fullmatch(r'([0-9a-f]{32}) (running|stopped|unknown) metadata=(yes|no)', line)
        if not match or len(rows) >= MAX_MEMBERS:
            raise CaptureError('Invalid remote session listing')
        rows.append({'remote_session_id': match[1], 'status': match[2],
                     'has_metadata': match[3] == 'yes'})
    return rows


def recover(args, folder):
    if folder.exists() and any(folder.iterdir()):
        raise CaptureError('Recovery output directory must be empty: ' + str(folder))
    conn = make_connection(args)
    sid = args.session
    if not sid:
        sid = conn.ssh('cat /tmp/qnx_perf_capture.lock/owner').stdout.decode().strip()
    if not re.fullmatch(r'[0-9a-f]{32}', sid):
        raise CaptureError('Valid --session required; use sessions to list remote captures')
    remote = '/tmp/qnx_perf_' + sid
    p = conn.ssh(f'test -d {remote} || exit 1; if [ -f {remote}/session.json ]; then cat {remote}/session.json; fi')
    if p.stdout.strip():
        state = json.loads(p.stdout)
        validate_state(state)
        if state['session_id'] != sid:
            raise CaptureError('Remote metadata session mismatch')
    else:
        state = {'version': 2, 'session_id': sid, 'qnx_dir': remote,
                 'android_dir': '/data/local/tmp/qnx_perf_' + sid,
                 'alignment_error_ms': None, 'status': 'recovery'}
    # Remote metadata is untrusted. Never adopt remote-supplied local artifacts.
    allowed = {'version', 'session_id', 'qnx_dir', 'android_dir', 'interval_ms',
               'max_duration_s', 'estimated_start_utc_ms', 'alignment_error_ms'}
    state = {key: value for key, value in state.items() if key in allowed}
    if not has_clock_anchor(state):
        state['estimated_start_utc_ms'] = None
        state['alignment_error_ms'] = None
    state['status'] = 'recovery'
    state['serial'] = conn.adb[-1]
    state['qnx_address'] = conn.address
    folder.mkdir(parents=True, exist_ok=True)
    save(folder, state)
    value = status(args, folder, quiet=True)
    state['status'] = value
    save(folder, state)
    if value == 'running':
        if not args.stop:
            emit('Capture is running. Metadata recovered to ' + str(folder))
            emit('Use stop -o ' + shlex.quote(str(folder)) + ', or recover --stop with an empty directory.')
            return
        return stop(args, folder)
    if value == 'stopped':
        return stop(args, folder)
    raise CaptureError('Remote capture state unknown; metadata saved, no signal or lock removal performed')


def make_connection(args, state=None):
    if getattr(args, 'deadline', None) is None:
        args.deadline = time.monotonic() + 900
    return Connection(args.adb, state['serial'] if state else select_device(args.adb, args.serial),
                      state['qnx_address'] if state else args.qnx,
                      deadline=getattr(args, 'deadline', None))


def prepare_restart(conn):
    # Only replace the lock-owned profiler whose command identifies this session.
    result = conn.ssh('''if [ ! -d /tmp/qnx_perf_capture.lock ]; then exit 0; fi
 sid=$(cat /tmp/qnx_perf_capture.lock/owner) || exit 1
 case "$sid" in ''|*[!0-9a-f]*) echo 'Invalid lock owner; refusing restart' >&2; exit 1;; esac
 [ "${#sid}" = 32 ] || exit 1
 d=/tmp/qnx_perf_$sid
 if [ ! -f "$d/exit_code" ]; then
  p=$(cat "$d/profiler.pid") || exit 1
  case "$p" in ''|*[!0-9]*) exit 1;; esac
  info=$(pidin -p "$p" ar)
  case "$info" in *sysprofiler_app*"$d/results"*)
   echo "Finalizing previous capture: $sid"
   kill -INT "$p" || exit 1;;
  *) echo 'Previous capture identity unknown; refusing restart' >&2; exit 1;; esac
  n=0
  while [ ! -f "$d/exit_code" ]; do
   n=$((n+1))
   [ "$n" -le 60 ] || { echo 'Previous capture has not finalized; refusing restart' >&2; exit 1; }
   sleep 1
  done
 fi
 if [ -d /tmp/qnx_perf_capture.lock ]; then
  [ "$(cat /tmp/qnx_perf_capture.lock/owner 2>/dev/null)" = "$sid" ] || {
   echo 'Lock owner changed; refusing restart' >&2; exit 1;
  }
  rm -r /tmp/qnx_perf_capture.lock || exit 1
 fi
 echo "Previous data retained: $d"
''', check=False)
    if result.returncode:
        raise CaptureError((result.stderr + result.stdout).decode(errors='replace'))
    if result.stdout:
        emit(result.stdout.decode(errors='replace').strip())


def start(args, folder):
    if folder.exists() and any(folder.iterdir()):
        raise CaptureError('Output directory must be empty: ' + str(folder))
    try:
        return start_capture(args, folder)
    except (Exception, KeyboardInterrupt) as exc:
        # The launch may have succeeded even when its reply was lost.
        if (folder / 'session.json').exists():
            state = load(folder)
            if state.get('status') == 'starting':
                state['status'] = 'start_failed'
                state['last_error'] = str(exc) or 'Interrupted during startup'
                save(folder, state)
        raise


def start_capture(args, folder):
    if folder.exists() and any(folder.iterdir()):
        raise CaptureError('Output directory must be empty: ' + str(folder))
    folder.mkdir(parents=True, exist_ok=True)
    conn = make_connection(args)
    sid = uuid.uuid4().hex
    remote = '/tmp/qnx_perf_' + sid
    state = {'version': 2, 'session_id': sid, 'serial': conn.adb[-1], 'qnx_address': args.qnx,
             'qnx_dir': remote, 'android_dir': '/data/local/tmp/qnx_perf_' + sid,
             'interval_ms': args.interval, 'max_duration_s': args.max_duration,
             'status': 'starting', 'created_host_utc_ms': time.time_ns()//1000000}
    save(folder, state)
    preflight = conn.ssh('command -v sysprofiler_app && command -v tar && command -v pidin && command -v on && sysprofiler_app --version')
    (folder / 'preflight.txt').write_bytes(preflight.stdout + preflight.stderr)
    if 'sysprofiler_app' not in preflight.stdout.decode(errors='replace'):
        raise CaptureError('QNX sysprofiler_app not found')
    prepare_restart(conn)
    before = conn.clock()
    state['clock_before'] = before
    state['estimated_start_utc_ms'] = before['epoch_s'] * 1000
    state['alignment_error_ms'] = None
    save(folder, state)
    interval = args.interval
    command = ('sysprofiler_app --cpu --gpu --cdsp --cdsp1 --ddr '
               f'--sample-time {interval} --cpu-sample-time {interval} --gpu-sample-time {interval} '
               f'--cdsp-sample-time {interval} --cdsp1-sample-time {interval} --ddr-sample-time {interval} '
               f'--total-time {args.max_duration} --dir {remote}/results --log 1')
    wrapper = f'''trap '' HUP
cd {remote} || exit 1
{command} > profiler.log 2>&1 &
p=$!
echo "$p" > profiler.pid
wait "$p"
rc=$?
printf '%s\\n' "$rc" > exit_code.tmp
if [ "$(cat /tmp/qnx_perf_capture.lock/owner 2>/dev/null)" = {sid} ]; then
 rm -r /tmp/qnx_perf_capture.lock
fi
mv exit_code.tmp exit_code
'''
    # A global lock avoids competing access to global hardware profilers.
    state['command'] = command
    metadata = shlex.quote(json.dumps(state, ensure_ascii=True))
    script = f'''command -v sysprofiler_app >/dev/null || exit 1
mkdir /tmp/qnx_perf_capture.lock || {{
 echo 'Another capture or unresolved lock exists. Use sessions, then recover --session ID.' >&2
 cat /tmp/qnx_perf_capture.lock/owner >&2
 exit 1
}}
echo {sid} > /tmp/qnx_perf_capture.lock/owner
launch_attempted=0
trap 'if [ "$launch_attempted" = 0 ] && [ "$(cat /tmp/qnx_perf_capture.lock/owner 2>/dev/null)" = {sid} ]; then rm -r /tmp/qnx_perf_capture.lock; fi' 0
mkdir {remote} || exit 1
printf '%s\\n' {metadata} > {remote}/session.json || exit 1
printf '%s' {shlex.quote(wrapper)} > {remote}/runner.sh || exit 1
launch_attempted=1
on -d sh {remote}/runner.sh </dev/null >{remote}/runner.log 2>&1 || exit 1
sleep 1
cat {remote}/profiler.pid
'''
    try:
        p = conn.ssh(script, check=False)
        (folder / 'start.log').write_bytes(p.stdout + p.stderr)
        if p.returncode:
            raise CaptureError((p.stderr + p.stdout).decode(errors='replace') +
                               '\nSee start.log; use sessions to discover the existing capture.')
        matches = re.findall(r'^\s*(\d+)\s*$', p.stdout.decode(), re.M)
        if not matches:
            raise CaptureError('QNX did not return profiler PID; see start.log')
        state['pid'] = int(matches[-1])
        save(folder, state)
        after = conn.clock()
        state['clock_before'] = before
        state['clock_after'] = after
        # Android epoch resolution is one second. This is an approximate anchor, not exact synchronization.
        elapsed_ms = (after['host_mid_ns'] - before['host_mid_ns'])/1e6
        state['estimated_start_utc_ms'] = round(before['epoch_s']*1000 + elapsed_ms/2)
        state['alignment_error_ms'] = round(1000 + elapsed_ms/2 + max(before['rtt_ms'],after['rtt_ms'])/2)
        state['command'] = command
        state['status'] = 'running'
        save(folder, state)
        remote_save(conn, state)
        remote_status = status(args, folder, quiet=True)
        if remote_status == 'stopped':
            state['status'] = 'stopped'
            save(folder, state)
        elif remote_status != 'running':
            raise CaptureError('Profiler startup state unknown. Use status/recover; no process is killed automatically.')
    except Exception:
        state['status'] = 'start_failed'
        save(folder, state)
        raise
    emit(f'Capture {state["status"]} on {state["serial"]}, PID={state["pid"]}, interval={interval} ms')
    emit('Session: ' + str(folder))
    return state


def status(args, folder, quiet=False):
    state = load(folder)
    conn = make_connection(args, state)
    remote = state['qnx_dir']
    result = conn.ssh(f'''if [ -f {remote}/exit_code ]; then
 echo stopped; cat {remote}/exit_code
elif [ -f {remote}/profiler.pid ]; then
 p=$(cat {remote}/profiler.pid)
 case "$p" in ''|*[!0-9]*) exit 1;; esac
 pidin -p "$p" ar
else echo unknown; fi''')
    text = result.stdout.decode(errors='replace')
    if text.startswith('stopped'):
        value = 'stopped'
    elif remote + '/results' in text and 'sysprofiler_app' in text:
        value = 'running'
    else:
        value = 'unknown'
    if not quiet:
        emit(value)
        emit(text.strip())
    return value


def stop(args, folder):
    state = load(folder)
    conn = make_connection(args, state)
    remote = state['qnx_dir']
    result = conn.ssh(f'''if [ ! -f {remote}/exit_code ]; then
 p=$(cat {remote}/profiler.pid) || exit 1
 case "$p" in ''|*[!0-9]*) exit 1;; esac
 args=$(pidin -p "$p" ar)
 case "$args" in *sysprofiler_app*{remote}/results*) kill -INT "$p" || exit 1;;
 *) echo 'PID identity mismatch; refusing to signal' >&2; exit 1;; esac
fi
n=0
while [ ! -f {remote}/exit_code ]; do
 n=$((n+1)); [ "$n" -le 60 ] || {{ echo 'Profiler has not finalized; files retained' >&2; exit 1; }}
 sleep 1
done
cat {remote}/exit_code
if [ "$(cat /tmp/qnx_perf_capture.lock/owner 2>/dev/null)" = {state['session_id']} ]; then
 rm -r /tmp/qnx_perf_capture.lock
fi''')
    (folder / 'stop.log').write_bytes(result.stdout + result.stderr)
    state['status'] = 'stopped'
    state['stop_host_utc_ms'] = time.time_ns()//1000000
    state['profiler_exit_code'] = result.stdout.decode().strip().splitlines()[0]
    save(folder, state)
    emit('Profiler finalized. Pulling data...')
    return pull(args, folder)


def extract_archive(archive, raw, deadline=None):
    check_deadline(deadline)
    if archive.stat().st_size > MAX_ARCHIVE_BYTES or raw.is_symlink():
        raise CaptureError('Archive exceeds size limit or unsafe destination')
    members, total, names, entries = [], 0, set(), set()
    with tarfile.open(archive) as tar:
        for member in tar:
            check_deadline(deadline)
            path = Path(member.name)
            total += member.size
            if (len(members) >= MAX_MEMBERS or total > MAX_ARCHIVE_BYTES
                    or member.size < 0 or len(member.name) > 512
                    or '\\' in member.name or ':' in member.name
                    or path.is_absolute() or '..' in path.parts
                    or not path.parts or str(path) in names
                    or not (member.isfile() or member.isdir())):
                raise CaptureError('Unsafe archive or extraction limit exceeded')
            names.add(str(path))
            entries.update(str(p) for p in (path, *path.parents) if p != Path('.'))
            if len(entries) > MAX_MEMBERS:
                raise CaptureError('Extraction entry limit exceeded')
            members.append(member)
        for member in members:
            check_deadline(deadline)
            destination = raw / member.name
            if any(p.is_symlink() for p in (destination, *destination.parents)):
                raise CaptureError('Symlink extraction destination')
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise CaptureError('Cannot extract archive member')
                with source, destination.open('xb') as target:
                    for chunk in iter(lambda: source.read(1024*1024), b''):
                        check_deadline(deadline)
                        target.write(chunk)
                        check_deadline(deadline)


def pull(args, folder):
    state = load(folder)
    conn = make_connection(args, state)
    remote, android = state['qnx_dir'], state['android_dir']
    # Do not archive files that are still being written.
    p = conn.ssh(f'test -f {remote}/exit_code && cd {remote} && tar -cf capture.tar results profiler.log runner.log profiler.pid exit_code')
    (folder / 'archive.log').write_bytes(p.stdout + p.stderr)
    address = 'root@' + state['qnx_address'] + ':' + remote + '/capture.tar'
    conn.android(f'mkdir -p {android} && scp -o BatchMode=yes -o StrictHostKeyChecking=accept-new '
                 + shlex.quote(address) + ' ' + android + '/capture.tar', timeout=600)
    checksum = conn.android('sha256sum ' + android + '/capture.tar').stdout.decode().split()[0]
    deadline = args.deadline
    check_deadline(deadline)
    generation = 'capture_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex
    archive = folder / (generation + '.tar')
    partial = folder / (generation + '.tar.partial')
    raw = folder / ('raw_' + generation)
    # Reserve a fresh generation; never overwrite a returned download path.
    raw.mkdir()
    if partial.exists() or partial.is_symlink() or archive.exists() or archive.is_symlink():
        raise CaptureError('Artifact generation already exists')
    conn.call(['pull', android + '/capture.tar', str(partial)], timeout=600)
    check_deadline(deadline)
    if partial.stat().st_size > MAX_ARCHIVE_BYTES:
        raise CaptureError('Archive exceeds size limit')
    with partial.open('rb') as stream:
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            check_deadline(deadline)
            digest.update(chunk)
            check_deadline(deadline)
    if digest.hexdigest() != checksum:
        raise CaptureError('Archive checksum mismatch; retained for retry')
    extract_archive(partial, raw, deadline=deadline)
    check_deadline(deadline)
    # Invalidate the old binding before any new archive is published. If the
    # deadline expires between link and metadata commit, stale CSV stays disabled.
    state.pop('conversion', None)
    save(folder, state, deadline)
    check_deadline(deadline)
    # Hard-link publication is atomic and fails rather than replacing a collision.
    # Keep the staging link too: no history or in-flight artifact is deleted.
    os.link(partial, archive)
    state.update(archive=str(archive), raw_dir=str(raw), archive_sha256=checksum,
                 generation=generation, status='pulled')
    state.pop('conversion', None)
    save(folder, state, deadline)  # New archive invalidates any older CSV binding.
    if not has_clock_anchor(state):
        return {'raw_only': True, 'raw_dir': str(raw)}
    output = folder / ('perf_' + generation + '.log')
    conversion_deadline = min(deadline, time.monotonic() + 120)
    report = convert(raw, output, state, deadline=conversion_deadline)
    check_deadline(conversion_deadline)
    state['conversion'] = dict(report, generation=generation, archive_sha256=checksum,
                               perf_file=str(output))
    save(folder, state, conversion_deadline)
    check_deadline(deadline)
    emit('Raw data: ' + str(raw))
    emit('Perf data: ' + str(output))
    emit(json.dumps(report, ensure_ascii=False, indent=2))
    missing_required = set(report['missing_modules']) & {'cpu', 'gpu', 'cdsp', 'ddr'}
    if missing_required:
        raise MissingModulesError('Required modules produced no usable samples: ' + ', '.join(sorted(missing_required)))
    if 'cdsp1' in report['missing_modules']:
        emit('Warning: optional CDSP1 has no usable samples; it is not reported as zero.', file=sys.stderr)
    return report


def run(args, folder):
    start(args, folder)
    stop_event = threading.Event()
    def requested(signum, frame):
        stop_event.set()
    old = {s: signal.signal(s, requested) for s in (signal.SIGINT, signal.SIGTERM)}
    if sys.stdin.isatty():
        def enter_to_stop():
            try:
                input()
                stop_event.set()
            except EOFError:
                pass
        threading.Thread(target=enter_to_stop, daemon=True).start()
    emit('Press Ctrl+C or Enter to stop and pull. Do not disconnect the device.')
    try:
        while not stop_event.wait(5):
            if status(args, folder, quiet=True) == 'stopped':
                emit('Profiler exited by itself; retrieving diagnostics.')
                break
        # Ignore a second Ctrl+C during finalization/slow data transfer.
        for s in old:
            signal.signal(s, signal.SIG_IGN)
        stop(args, folder)
    finally:
        for s, handler in old.items():
            signal.signal(s, handler)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['run','start','stop','status','pull','sessions','recover'])
    p.add_argument('-s', '--serial', default=os.environ.get('ANDROID_SERIAL'))
    p.add_argument('-o', '--output', help='Host session directory; empty for run/start/recover')
    p.add_argument('--session', help='Remote session ID for recover; defaults to capture lock owner')
    p.add_argument('--stop', action='store_true', help='Explicitly stop a running capture during recover')
    p.add_argument('--max-duration', type=int, default=600, help='Maximum capture seconds (default 600; 1..3600)')
    p.add_argument('--adb', default='adb', help='adb executable (not a shell command)')
    p.add_argument('--qnx', default='192.168.1.1')
    p.add_argument('-i', '--interval', type=int, default=1000, help='Sampling interval in ms (default 1000)')
    return p


def main():
    args = parser().parse_args()
    if not 100 <= args.interval <= 60000:
        raise CaptureError('Interval must be between 100 and 60000 ms')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*', args.qnx):
        raise CaptureError('Invalid QNX address')
    if not 1 <= args.max_duration <= 3600:
        raise CaptureError('Maximum duration must be between 1 and 3600 seconds')
    if args.action != 'sessions' and not args.output:
        raise CaptureError('-o/--output required except for sessions')
    if args.stop and args.action != 'recover':
        raise CaptureError('--stop is only valid with recover')
    if args.session and args.action != 'recover':
        raise CaptureError('--session is only valid with recover')
    folder = Path(args.output).expanduser().resolve() if args.output else None
    {'run':run, 'start':start, 'stop':stop, 'status':status, 'pull':pull,
     'sessions':sessions, 'recover':recover}[args.action](args, folder)


if __name__ == '__main__':
    try:
        main()
    except (CaptureError, OSError, ValueError, tarfile.TarError) as exc:
        emit('qnx_perf: ' + str(exc), file=sys.stderr)
        emit('Remote files are retained. Use status/stop/pull with the same -o directory. '
              'If it is lost, use sessions and recover --session ID -o NEW_DIR; '
              'add --stop only to explicitly stop an active capture.', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        emit('Interrupted. Session files retained; use stop/pull with the same -o directory.', file=sys.stderr)
        sys.exit(130)
