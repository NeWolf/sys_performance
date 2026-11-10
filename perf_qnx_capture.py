"""Local, durable QNX capture orchestration; only explicit operations connect."""
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid

from perf_adb import AdbError
import perf_qnx_device as device
from perf_qnx_convert import has_clock_anchor

ID_PATTERN = r"[0-9a-f]{32}"
SERIAL_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,199}"
MAX_STATE_BYTES = 1024 * 1024
STATUSES = {"starting", "recovering", "running", "stopped", "pulled", "unknown", "imported"}


def validate_target(serial, address):
    if not isinstance(serial, str) or not re.fullmatch(SERIAL_PATTERN, serial):
        raise AdbError("无效的设备序列号", 400)
    if not isinstance(address, str) or not 1 <= len(address) <= 253 or '%' in address:
        raise AdbError("无效的 QNX 地址", 400)
    try:
        ipaddress.ip_address(address)
    except ValueError:
        if (not re.fullmatch(r"[A-Za-z0-9.-]+", address)
                or all(c in '0123456789.' for c in address)
                or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", p)
                       for p in address.split('.'))):
            raise AdbError("无效的 QNX 地址", 400)


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERN, value):
        raise AdbError("无效的 QNX 任务标识", 400)
    return value


class QnxCapture:
    def __init__(self, adb, archive, store, import_lock):
        self.adb, self.store, self.import_lock = adb, store, import_lock
        self.root = Path(archive).absolute()
        self._lock = threading.Lock()
        self._closed = threading.Event()

    def close(self):
        # No remote signal, no worker join, no unbounded shutdown wait.
        self._closed.set()

    @contextmanager
    def _guard(self):
        if self._closed.is_set():
            raise AdbError("QNX 服务已关闭；远端采集不受影响", 503)
        if not self._lock.acquire(blocking=False):
            raise AdbError("已有 QNX 操作正在执行", 409)
        try:
            yield
        except AdbError:
            raise
        except (OSError, ValueError, device.CaptureError) as exc:
            raise AdbError("QNX 本地状态或文件无效，未确认远端状态", 409) from exc
        finally:
            self._lock.release()

    def _safe(self, path, base=None):
        path = Path(path).absolute()
        base = self.root if base is None else Path(base).absolute()
        if '..' in path.parts or not path.is_relative_to(base) or not path.resolve().is_relative_to(base.resolve()):
            raise AdbError("QNX 文件超出采集目录", 409)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise AdbError("QNX 文件路径含符号链接", 409)
        if path.exists() and not (path.is_file() or path.is_dir()):
            raise AdbError("QNX 文件类型无效", 409)
        return path

    def _folder(self, capture_id):
        return self._safe(self.root / identity(capture_id))

    def _json(self, path):
        self._safe(path)
        if path.stat().st_size > MAX_STATE_BYTES:
            raise ValueError("State too large")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate state field")
                result[key] = value
            return result
        try:
            value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs)
        except RecursionError as exc:
            raise ValueError('State nesting limit exceeded') from exc
        if not isinstance(value, dict):
            raise ValueError("Invalid state")
        return value

    def _save(self, state, deadline=None):
        folder = self._folder(state['capture_id'])
        self._safe(folder / 'local.tmp')
        self._safe(folder / 'local.json')
        device.atomic_json(folder / 'local.json', state, deadline)

    def _load(self, capture_id):
        folder = self._folder(capture_id)
        if not folder.exists():
            raise AdbError("QNX 本地任务不存在", 404)
        state = self._json(folder / 'local.json')
        if state.get('capture_id') != capture_id or not isinstance(state.get('status'), str) or state['status'] not in STATUSES:
            raise ValueError("Invalid local state")
        validate_target(state.get('serial'), state.get('qnx_address'))
        if type(state.get('title')) is not str or len(state['title']) > 120:
            raise ValueError("Invalid title")
        if state.get('remote_session_id') is not None:
            identity(state['remote_session_id'])
        if state.get('session_id') is not None:
            identity(state['session_id'])
        for key, low, high in (('interval_ms', 100, 60000), ('max_duration_s', 1, 3600)):
            value = state.get(key)
            if value is not None and (type(value) is not int or not low <= value <= high):
                raise ValueError("Invalid capture parameter")
        # Persisted errors are codes, never arbitrary strings from remote state.
        if state.get('error') not in (None, 'operation_failed', 'timeout', 'import_failed'):
            raise ValueError("Invalid error code")
        return state

    def _upstream(self, state, deadline=None):
        folder = self._folder(state['capture_id']) / 'data'
        self._safe(folder)
        # Validate each generation independently, retaining all historical paths.
        counts = {}
        if folder.exists():
            for parent, dirs, files in os.walk(folder, followlinks=False):
                device.check_deadline(deadline)
                for name in dirs + files:
                    device.check_deadline(deadline)
                    artifact = Path(parent) / name
                    relative = artifact.relative_to(folder)
                    if len(relative.parts) > 1:
                        generation = relative.parts[0]
                        counts[generation] = counts.get(generation, 0) + 1
                        if counts[generation] > device.MAX_MEMBERS:
                            raise ValueError("Too many artifacts in generation")
                    self._safe(artifact, folder)
        device.check_deadline(deadline)
        path = folder / 'session.json'
        if not path.exists():
            return folder, None
        raw = self._json(path)
        device.validate_state(raw)
        if (raw.get('serial') != state['serial'] or raw.get('qnx_address') != state['qnx_address']
                or state.get('remote_session_id') not in (None, raw['session_id'])):
            raise ValueError("Session mismatch")
        if not isinstance(raw.get('status'), str) or raw['status'] not in {'starting', 'running', 'stopped', 'pulled', 'recovery', 'unknown', 'start_failed'}:
            raise ValueError('Invalid upstream status')
        for key in ('archive', 'raw_dir'):
            if raw.get(key) is not None:
                if not isinstance(raw[key], str):
                    raise ValueError('Invalid artifact path')
                artifact = self._safe(raw[key], folder)
                pattern = r'capture_\d{8}_\d{6}_(?:[0-9a-f]{6}|[0-9a-f]{32})\.tar' if key == 'archive' else r'raw_capture_\d{8}_\d{6}_(?:[0-9a-f]{6}|[0-9a-f]{32})'
                if artifact.parent != folder or not re.fullmatch(pattern, artifact.name):
                    raise ValueError('Invalid artifact name')
        return folder, raw

    def _bound_perf(self, folder, raw):
        # Legacy unbound CSVs remain on disk, but cannot identify the current archive.
        report = raw.get('conversion')
        generation, checksum = raw.get('generation'), raw.get('archive_sha256')
        if (not isinstance(report, dict) or not isinstance(generation, str)
                or not re.fullmatch(r'capture_\d{8}_\d{6}_[0-9a-f]{32}', generation)
                or not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum)
                or report.get('generation') != generation
                or report.get('archive_sha256') != checksum
                or raw.get('archive') != str(folder / (generation + '.tar'))
                or raw.get('raw_dir') != str(folder / ('raw_' + generation))
                or report.get('perf_file') != str(folder / ('perf_' + generation + '.log'))):
            return None
        perf = self._safe(report['perf_file'], folder)
        if (not self._safe(raw['archive'], folder).is_file()
                or not self._safe(raw['raw_dir'], folder).is_dir()
                or not perf.is_file() or perf.stat().st_size == 0):
            return None
        return perf

    def _view(self, state, deadline=None):
        folder, raw = self._upstream(state, deadline)
        raw = raw or {}
        anchor = has_clock_anchor(raw)
        perf = self._bound_perf(folder, raw)
        archive = raw.get('archive')
        available = bool(archive and self._safe(archive, folder).is_file())
        report = raw.get('conversion') if perf is not None else None
        missing = []
        if isinstance(report, dict) and isinstance(report.get('missing_modules'), list):
            missing = sorted({x for x in report['missing_modules'] if isinstance(x, str)
                              and x in {'cpu', 'gpu', 'cdsp', 'cdsp1', 'ddr'}})
        warning = '缺少有效时钟锚，仅保留原始数据，不能导入 UTC 数据' if not anchor and available else None
        if missing:
            warning = '部分模块无可用样本：' + ', '.join(missing) + '；已保留可用数据'
        result = {key: state.get(key) for key in ('capture_id', 'serial', 'qnx_address', 'title',
                  'remote_session_id', 'interval_ms', 'max_duration_s', 'session_id', 'error')}
        status = state['status']
        if status in {'starting', 'recovering'}:
            status = 'unknown'
        result.update(status=status, running=True if status == 'running' else
                      False if status in {'stopped', 'pulled', 'imported'} else None,
                      connected=False, state_corrupt=False, warning=warning,
                      raw_only=not anchor, can_import=anchor and perf is not None,
                      archive_available=available, stop_includes_pull=True,
                      alignment_error_ms=raw.get('alignment_error_ms') if anchor else None,
                      missing_modules=missing)
        return result

    def captures(self):
        with self._guard():
            self._safe(self.root)
            rows = []
            if self.root.exists():
                for path in sorted(self.root.iterdir()):
                    if not re.fullmatch(ID_PATTERN, path.name):
                        continue
                    try:
                        rows.append(self._view(self._load(path.name)))
                    except (AdbError, OSError, ValueError, device.CaptureError):
                        rows.append({'capture_id': path.name, 'status': 'corrupt', 'state_corrupt': True,
                                     'running': None, 'connected': False, 'can_import': False,
                                     'archive_available': False, 'error': 'state_corrupt'})
            return {'captures': rows}

    def _args(self, state, stop=False):
        return SimpleNamespace(adb=self.adb.executable, serial=state['serial'],
                               qnx=state['qnx_address'], interval=state.get('interval_ms'),
                               max_duration=state.get('max_duration_s'),
                               session=state.get('remote_session_id'), stop=stop,
                               deadline=time.monotonic() + 900)

    def _sync(self, state, deadline=None):
        _, raw = self._upstream(state, deadline)
        if raw:
            state['remote_session_id'] = raw['session_id']
            if raw.get('status') in {'running', 'stopped', 'pulled'}:
                state['status'] = raw['status']
        return raw

    def _execute(self, state, action, stop=False):
        args = self._args(state, stop)
        try:
            folder, raw = self._upstream(state, args.deadline)
            if action not in {'start', 'recover'} and raw is None:
                raise AdbError('缺少远端任务元数据，请按远端 ID 显式恢复', 409)
            with self.adb.device(state['serial']):
                try:
                    if action == 'status':
                        state['status'] = device.status(args, folder, quiet=True)
                    else:
                        operation = device.start_capture if action == 'start' else getattr(device, action)
                        operation(args, folder)
                        self._sync(state, args.deadline)
                except device.MissingModulesError:
                    self._sync(state, args.deadline)
                    if state['status'] != 'pulled':
                        raise
            state['error'] = None
            self._save(state, args.deadline)
            view = self._view(state, args.deadline)
            device.check_deadline(args.deadline)
            view['connected'] = True
            return view
        except Exception as exc:
            # Preserve identity and any committed artifact, but never infer stopped on failure.
            try:
                self._sync(state)
            except (AdbError, OSError, ValueError, device.CaptureError):
                pass
            state['status'] = 'unknown'
            state['error'] = 'timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'operation_failed'
            self._save(state)
            code = exc.status if isinstance(exc, AdbError) else 504 if state['error'] == 'timeout' else 502
            raise AdbError('QNX 操作未完成，远端状态未确认；本地任务 ' + state['capture_id']
                           + ' 已保留，请查看状态或显式刷新', code) from exc

    def _new(self, serial, qnx_address, title, interval_ms=None, max_duration_s=None, remote_session_id=None):
        validate_target(serial, qnx_address)
        if type(title) is not str or len(title) > 120:
            raise AdbError('标题最多 120 字符', 400)
        state = dict(capture_id=uuid.uuid4().hex, serial=serial, qnx_address=qnx_address,
                     title=title.strip(), interval_ms=interval_ms, max_duration_s=max_duration_s,
                     remote_session_id=remote_session_id, session_id=None, error=None,
                     status='recovering' if remote_session_id else 'starting')
        self._folder(state['capture_id']).mkdir(parents=True, exist_ok=False)
        self._save(state)
        return state

    def start(self, serial, qnx_address, interval_ms=1000, max_duration_s=60, title=''):
        for value, low, high in ((interval_ms, 100, 60000), (max_duration_s, 1, 3600)):
            if type(value) is not int or not low <= value <= high:
                raise AdbError('QNX 采样间隔或时长超出范围', 400)
        with self._guard():
            state = self._new(serial, qnx_address, title, interval_ms, max_duration_s)
            return self._execute(state, 'start')

    def recover(self, serial, qnx_address, remote_session_id, stop=False, title=''):
        identity(remote_session_id)
        if type(stop) is not bool:
            raise AdbError('stop 必须为布尔值', 400)
        with self._guard():
            state = self._new(serial, qnx_address, title, remote_session_id=remote_session_id)
            return self._execute(state, 'recover', stop)

    def status(self, capture_id, refresh=False):
        with self._guard():
            state = self._load(capture_id)
            return self._execute(state, 'status') if refresh else self._view(state)

    def stop(self, capture_id):
        with self._guard():
            return self._execute(self._load(capture_id), 'stop')

    def pull(self, capture_id):
        with self._guard():
            return self._execute(self._load(capture_id), 'pull')

    def sessions(self, serial, qnx_address):
        validate_target(serial, qnx_address)
        with self._guard():
            try:
                with self.adb.device(serial):
                    rows = device.sessions(self._args({'serial': serial, 'qnx_address': qnx_address}))
                return {'sessions': rows, 'connected': True}
            except AdbError:
                raise
            except Exception as exc:
                raise AdbError('QNX 远端任务发现失败，未确认设备状态',
                               504 if isinstance(exc, subprocess.TimeoutExpired) else 502) from exc

    def import_capture(self, capture_id):
        with self._guard():
            state = self._load(capture_id)
            if not self._view(state)['can_import']:
                raise AdbError('无可导入的 UTC 数据；raw-only 任务仅支持原始归档下载', 409)
            if not self.import_lock.acquire(blocking=False):
                raise AdbError('已有导入正在执行', 409)
            try:
                folder, raw = self._upstream(state)
                perf = self._bound_perf(folder, raw)
                if perf is None:
                    raise ValueError('Current generation is not importable')
                with perf.open('rb') as stream:
                    imported = self.store.import_files([('perf_qnx.log', stream)], state['title'],
                                                       capture_method='QNX', device_name=state['serial'])
                state['session_id'] = imported['id']
                state['error'] = None
                self._save(state)
                return {'capture': self._view(state), 'session_id': imported['id'],
                        'duplicate': bool(imported.get('duplicate'))}
            except Exception as exc:
                state['error'] = 'import_failed'
                self._save(state)
                raise AdbError('QNX 数据导入失败，原始文件已保留', 422) from exc
            finally:
                self.import_lock.release()

    def archive_path(self, capture_id):
        with self._guard():
            state = self._load(capture_id)
            if not self._view(state)['archive_available']:
                raise AdbError('QNX 归档尚不可用', 404)
            folder, raw = self._upstream(state)
            return self._safe(raw['archive'], folder)