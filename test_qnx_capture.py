"""Offline QNX regressions. All device execution is replaced with mocks."""
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from perf_adb import AdbController, AdbError
from perf_api import create_app
from perf_qnx_capture import QnxCapture, validate_target
from perf_qnx_convert import convert, has_clock_anchor
import perf_qnx_device as device

SID = 'a' * 32
SERIAL = 'offline-device'
ADDRESS = '192.0.2.10'


class QnxTests(unittest.TestCase):
    def setUp(self):
        # Keep isolated artifacts for inspection; no cleanup/deletion or actual commands.
        self.root = Path(tempfile.mkdtemp(prefix='qnx-offline-')).resolve()
        self.addCleanup(patch.stopall)
        self.process = patch('subprocess.run', side_effect=AssertionError('Device command forbidden')).start()
        patch('socket.create_connection', side_effect=AssertionError('Network forbidden')).start()
        self.adb = AdbController(self.root / 'adb', executable='mock-adb')
        self.devices = patch.object(self.adb, 'devices', return_value=[{'serial': SERIAL, 'state': 'device'}]).start()
        self.store = Mock()
        self.store.import_files.return_value = {'id': 'b' * 32, 'duplicate': False}
        self.import_lock = threading.Lock()
        self.capture = QnxCapture(self.adb, self.root / 'captures', self.store, self.import_lock)

    def upstream(self, args, folder, status='running', anchor=True):
        self.assertTrue(self.adb.lock.locked())
        folder.mkdir(parents=True, exist_ok=True)
        state = dict(session_id=args.session or SID, serial=args.serial, qnx_address=args.qnx,
                     qnx_dir='/tmp/qnx_perf_' + (args.session or SID),
                     android_dir='/data/local/tmp/qnx_perf_' + (args.session or SID), status=status)
        if anchor:
            state.update(estimated_start_utc_ms=1700000000000, alignment_error_ms=1000)
        device.save(folder, state)
        return state

    def start(self):
        with patch.object(device, 'start_capture', side_effect=self.upstream):
            return self.capture.start(SERIAL, ADDRESS)

    def pulled(self, args, folder, anchor=True, missing=False):
        state = self.upstream(args, folder, 'pulled', anchor)
        generation = 'capture_20261030_120000_' + 'f' * 32
        archive = folder / (generation + '.tar')
        archive.write_bytes(b'offline archive')
        raw = folder / ('raw_' + generation)
        raw.mkdir(exist_ok=True)
        checksum = device.hashlib.sha256(archive.read_bytes()).hexdigest()
        state.update(archive=str(archive), raw_dir=str(raw), generation=generation,
                     archive_sha256=checksum)
        if anchor:
            perf = folder / ('perf_' + generation + '.log')
            perf.write_text('QC,1700000000000,cpu,totalcpu,12,%,0\n')
            state['conversion'] = dict(generation=generation, archive_sha256=checksum,
                                       perf_file=str(perf),
                                       missing_modules=['gpu', 'cdsp', 'ddr'] if missing else [])
        device.save(folder, state)
        if missing:
            raise device.MissingModulesError('partial data')

    def test_local_default_and_restart_never_connect(self):
        started = self.start()
        self.devices.reset_mock()
        restarted = QnxCapture(self.adb, self.capture.root, self.store, self.import_lock)
        self.assertEqual(restarted.status(started['capture_id'])['remote_session_id'], SID)
        self.assertEqual(len(restarted.captures()['captures']), 1)
        self.devices.assert_not_called()
        self.process.assert_not_called()

    def test_ids_are_local_independent_and_unique(self):
        first, second = self.start(), self.start()
        self.assertNotEqual(first['capture_id'], SID)
        self.assertNotEqual(first['capture_id'], second['capture_id'])
        self.assertEqual(first['remote_session_id'], SID)

    def test_shared_device_lock_blocks_without_command(self):
        self.adb.lock.acquire()
        try:
            with self.assertRaises(AdbError) as caught:
                self.capture.start(SERIAL, ADDRESS)
            self.assertEqual(caught.exception.status, 409)
            self.devices.assert_not_called()
        finally:
            self.adb.lock.release()
        state = self.capture.captures()['captures'][0]
        self.assertEqual(state['status'], 'unknown')
        self.assertIsNone(state['running'])

    def test_recover_requires_id_and_defaults_no_stop(self):
        with self.assertRaises(AdbError):
            self.capture.recover(SERIAL, ADDRESS, '')
        def recovery(args, folder):
            self.assertEqual(args.session, SID)
            self.assertIs(args.stop, False)
            self.upstream(args, folder)
        with patch.object(device, 'recover', side_effect=recovery):
            result = self.capture.recover(SERIAL, ADDRESS, SID)
        self.assertEqual(result['status'], 'running')

    def test_explicit_refresh_persists_state(self):
        result = self.start()
        with patch.object(device, 'status', return_value='stopped') as status:
            refreshed = self.capture.status(result['capture_id'], True)
        self.assertEqual(refreshed['status'], 'stopped')
        status.assert_called_once()
        self.assertEqual(self.capture.status(result['capture_id'])['status'], 'stopped')

    def test_timeout_persists_unknown_and_sanitizes_error(self):
        result = self.start()
        with patch.object(device, 'stop', side_effect=subprocess.TimeoutExpired('sensitive command', 1)):
            with self.assertRaises(AdbError) as caught:
                self.capture.stop(result['capture_id'])
        self.assertEqual(caught.exception.status, 504)
        self.assertNotIn('sensitive', str(caught.exception))
        state = self.capture.status(result['capture_id'])
        self.assertEqual(state['status'], 'unknown')
        self.assertEqual(state['error'], 'timeout')
        self.assertIsNone(state['running'])

    def test_partial_conversion_stays_importable_and_downloadable(self):
        result = self.start()
        with patch.object(device, 'stop', side_effect=lambda args, folder: self.pulled(args, folder, missing=True)):
            state = self.capture.stop(result['capture_id'])
        self.assertTrue(state['can_import'])
        self.assertTrue(state['archive_available'])
        self.assertTrue(state['stop_includes_pull'])
        self.assertIn('gpu', state['warning'])
        self.devices.reset_mock()
        imported = self.capture.import_capture(result['capture_id'])
        self.assertEqual(imported['session_id'], 'b' * 32)
        self.assertEqual(self.store.import_files.call_args.kwargs['capture_method'], 'QNX')
        self.assertTrue(self.capture.archive_path(result['capture_id']).is_file())
        self.devices.assert_not_called()

    def test_import_lock_is_shared(self):
        result = self.start()
        with patch.object(device, 'pull', side_effect=self.pulled):
            self.capture.pull(result['capture_id'])
        self.import_lock.acquire()
        try:
            with self.assertRaises(AdbError) as caught:
                self.capture.import_capture(result['capture_id'])
            self.assertEqual(caught.exception.status, 409)
        finally:
            self.import_lock.release()
        self.store.import_files.assert_not_called()

    def test_raw_only_never_imports_even_if_old_csv_exists(self):
        result = self.start()
        with patch.object(device, 'pull', side_effect=lambda args, folder: self.pulled(args, folder, anchor=False)):
            state = self.capture.pull(result['capture_id'])
        (self.capture.root / result['capture_id'] / 'data' / 'perf_qnx.log').write_text('old')
        self.assertTrue(state['raw_only'])
        self.assertTrue(state['archive_available'])
        with self.assertRaises(AdbError):
            self.capture.import_capture(result['capture_id'])
        self.store.import_files.assert_not_called()

    def test_corrupt_states_are_listed_without_remote_calls(self):
        for value in ('{broken', '{"status":[]}', '{"capture_id":1,"capture_id":2}',
                   '{"nested":' + '[' * 2000 + '0' + ']' * 2000 + '}'):
            folder = self.capture.root / ('c' * 31 + str(len(list(self.capture.root.glob('*')))))
            folder.mkdir(parents=True)
            (folder / 'local.json').write_text(value)
        rows = self.capture.captures()['captures']
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row['state_corrupt'] for row in rows))
        self.devices.assert_not_called()

    def test_artifact_escape_and_symbolic_links_rejected(self):
        result = self.start()
        folder = self.capture.root / result['capture_id'] / 'data'
        raw = device.load(folder)
        raw['archive'] = str(folder / '..' / '..' / 'outside.tar')
        device.save(folder, raw)
        with self.assertRaises(AdbError):
            self.capture.status(result['capture_id'])
        raw.pop('archive')
        device.save(folder, raw)
        (folder / 'perf_qnx.log').symlink_to(self.root / 'outside')
        with self.assertRaises(AdbError):
            self.capture.pull(result['capture_id'])
        self.process.assert_not_called()

    def test_upstream_fields_not_leaked(self):
        result = self.start()
        folder = self.capture.root / result['capture_id'] / 'data'
        state = device.load(folder)
        state.update(private='private-output', command='remote-command')
        device.save(folder, state)
        response = json.dumps(self.capture.status(result['capture_id']))
        self.assertNotIn('private-output', response)
        self.assertNotIn('remote-command', response)

    def test_close_does_not_wait_or_signal(self):
        self.capture._lock.acquire()
        try:
            self.capture.close()
        finally:
            self.capture._lock.release()
        with self.assertRaises(AdbError) as caught:
            self.capture.captures()
        self.assertEqual(caught.exception.status, 503)
        self.process.assert_not_called()

    def test_target_validation(self):
        for target in ('-option', 'user@host', 'host;id', 'a/b', '999.999.1.1', 'fe80::1%eth0'):
            with self.subTest(target=target), self.assertRaises(AdbError):
                validate_target(SERIAL, target)
        for serial in ('-bad', 'a b', 'a;id'):
            with self.assertRaises(AdbError):
                validate_target(serial, ADDRESS)
        validate_target(SERIAL, 'qnx.example.test')
        for interval, duration in ((True, 1), (99, 1), (60001, 1), (100, 3601)):
            with self.assertRaises(AdbError):
                self.capture.start(SERIAL, ADDRESS, interval, duration)

    def test_upstream_recover_filters_remote_paths_without_stopping(self):
        state = dict(session_id=SID, qnx_dir='/tmp/qnx_perf_' + SID,
                     android_dir='/data/local/tmp/qnx_perf_' + SID, archive='/outside/secret',
                     estimated_start_utc_ms=float('nan'), alignment_error_ms=20)
        conn = Mock(adb=['mock-adb', '-s', SERIAL], address=ADDRESS)
        conn.ssh.return_value.stdout = json.dumps(state).encode()
        args = SimpleNamespace(session=SID, stop=False)
        with patch.object(device, 'make_connection', return_value=conn), patch.object(device, 'status', return_value='running'), patch.object(device, 'stop') as stop:
            device.recover(args, self.root / 'recovery')
        stop.assert_not_called()
        self.assertNotIn('archive', device.load(self.root / 'recovery'))

    def test_sessions_returns_structured_validated_rows(self):
        conn = Mock()
        conn.ssh.return_value.stdout = (SID + ' running metadata=yes\n').encode()
        with patch.object(device, 'make_connection', return_value=conn):
            result = self.capture.sessions(SERIAL, ADDRESS)
            self.assertEqual(result, {'sessions': [{'remote_session_id': SID,
                'status': 'running', 'has_metadata': True}], 'connected': True})
            conn.ssh.return_value.stdout = b'arbitrary device output'
            with self.assertRaises(AdbError):
                self.capture.sessions(SERIAL, ADDRESS)

    def test_connection_has_finite_timeout_and_no_shell(self):
        with patch('subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            device.Connection('mock', SERIAL, ADDRESS).call(['mock-only'])
        self.assertGreater(run.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(run.call_args.kwargs['timeout'], 90)
        self.assertIs(run.call_args.kwargs['shell'], False)

    def test_tar_rejects_links_traversal_and_duplicate_members(self):
        for index, name in enumerate(('../escape', '/absolute', 'safe', 'a\\b', 'a:b')):
            archive = self.root / ('unsafe' + str(index) + '.tar')
            with tarfile.open(archive, 'w') as tar:
                member = tarfile.TarInfo(name)
                if name == 'safe':
                    member.type = tarfile.SYMTYPE
                    member.linkname = '/outside'
                tar.addfile(member)
            with self.assertRaises(device.CaptureError):
                device.extract_archive(archive, self.root / ('raw' + str(index)))
        archive = self.root / 'duplicate.tar'
        with tarfile.open(archive, 'w') as tar:
            tar.addfile(tarfile.TarInfo('same'))
            tar.addfile(tarfile.TarInfo('same'))
        with self.assertRaises(device.CaptureError):
            device.extract_archive(archive, self.root / 'duplicates')

    def test_converter_clock_and_module_semantics(self):
        raw = self.root / 'raw'
        raw.mkdir()
        (raw / 'cpuprofiler_1.txt').write_text('SystemTime TotalCPU(%)\n0 125\n1 -1\n')
        (raw / 'gpuprofiler_1.txt').write_text('SystemTime utilization\n0 40\n')
        state = dict(estimated_start_utc_ms=1700000000000, alignment_error_ms=20)
        report = convert(raw, self.root / 'perf.log', state)
        text = (self.root / 'perf.log').read_text()
        self.assertIn('QC,1700000000000,cpu,totalcpu,125,%', text)
        self.assertIn('QG,1700000000000,gpu,utilization,40,%', text)
        self.assertIn('cdsp', report['missing_modules'])
        self.assertTrue(report['diagnostics'])
        for anchor in (None, True, -1, float('nan'), float('inf'), 10**400):
            invalid = dict(state, estimated_start_utc_ms=anchor)
            self.assertFalse(has_clock_anchor(invalid))
            with self.assertRaises(ValueError):
                convert(raw, self.root / 'invalid.log', invalid)

    def test_start_failure_retains_remote_identity(self):
        def failure(args, folder):
            self.upstream(args, folder, 'start_failed')
            raise device.CaptureError('device-private-detail')
        with patch.object(device, 'start_capture', side_effect=failure):
            with self.assertRaises(AdbError):
                self.capture.start(SERIAL, ADDRESS)
        state = self.capture.captures()['captures'][0]
        self.assertEqual(state['remote_session_id'], SID)
        self.assertEqual(state['status'], 'unknown')

    def test_conversion_rejects_resource_exhaustion(self):
        raw = self.root / 'raw'
        raw.mkdir()
        source = raw / 'cpuprofiler_1.txt'
        state = dict(estimated_start_utc_ms=1700000000000, alignment_error_ms=20)
        for text in ('x' * 65537, 'SystemTime ' + ' '.join(['value'] * 256),
                     'SystemTime TotalCPU(%)\n' + '0 -1\n' * 1001):
            source.write_text(text)
            with self.assertRaises(ValueError):
                convert(raw, self.root / 'perf.log', state)
        source.write_text('SystemTime TotalCPU(%)\n0 10\n')
        with patch('perf_qnx_convert.time.monotonic', side_effect=[0, 121]):
            with self.assertRaises(subprocess.TimeoutExpired):
                convert(raw, self.root / 'perf.log', state)

    def pull_connection(self, value=12, members=None):
        import io
        payload = io.BytesIO()
        if members is None:
            members = {'results/' + module + '_1.txt':
                       f'SystemTime {column}\n0 {value}\n'.encode()
                       for module, column in [('cpuprofiler', 'TotalCPU(%)'),
                       ('gpuprofiler', 'Utilization'), ('cdspprofiler', 'Load(%)'),
                       ('ddrprofiler', 'DDR_BW_Total(MB/s)')]}
        with tarfile.open(fileobj=payload, mode='w') as tar:
            for name, content in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        content = payload.getvalue()
        checksum = device.hashlib.sha256(content).hexdigest().encode()
        conn = Mock()
        conn.ssh.return_value = SimpleNamespace(stdout=b'', stderr=b'')
        conn.android.return_value = SimpleNamespace(stdout=checksum + b'  capture.tar\n')
        conn.call.side_effect = lambda args, **kwargs: Path(args[-1]).write_bytes(content)
        return conn, content

    def test_repeated_pull_retains_generations_and_delayed_download(self):
        cid = self.start()['capture_id']
        paths = []
        # Each generation has five extraction entries; total exceeds old 5+100.
        with patch.object(device, 'MAX_MEMBERS', 5):
            for index in range(12):
                conn, payload = self.pull_connection(index)
                with patch.object(device, 'make_connection', return_value=conn):
                    self.assertTrue(self.capture.pull(cid)['can_import'])
                paths.append((self.capture.archive_path(cid), payload))
            self.assertTrue(self.capture.status(cid)['can_import'])
            self.assertFalse(self.capture.captures()['captures'][0]['state_corrupt'])
            self.capture.import_capture(cid)
        self.assertEqual(len({path for path, _ in paths}), 12)
        for path, payload in paths:
            self.assertEqual(path.read_bytes(), payload)
        folder = self.capture.root / cid / 'data'
        self.assertGreater(len(list(folder.rglob('*'))), 105)
        self.assertEqual(len(list(folder.glob('*.partial'))), 12)
        self.process.assert_not_called()

    def test_failed_new_conversion_never_imports_old_generation(self):
        for failure in (ValueError('bad table'), subprocess.TimeoutExpired('convert', 0)):
            with self.subTest(failure=type(failure).__name__):
                cid = self.start()['capture_id']
                with patch.object(device, 'pull', side_effect=self.pulled):
                    self.capture.pull(cid)
                old_archive = self.capture.archive_path(cid)
                folder = self.capture.root / cid / 'data'
                old_csv = Path(device.load(folder)['conversion']['perf_file'])
                conn, _ = self.pull_connection()
                with patch.object(device, 'make_connection', return_value=conn), patch.object(device, 'convert', side_effect=failure):
                    with self.assertRaises(AdbError) as caught:
                        self.capture.pull(cid)
                self.assertEqual(caught.exception.status, 504 if isinstance(failure, subprocess.TimeoutExpired) else 502)
                state = self.capture.status(cid)
                self.assertFalse(state['can_import'])
                self.assertEqual(state['missing_modules'], [])
                self.assertTrue(state['archive_available'])
                self.assertNotEqual(old_archive, self.capture.archive_path(cid))
                self.assertTrue(old_csv.is_file())
                self.assertEqual(old_archive.read_bytes(), b'offline archive')
                self.assertNotIn('conversion', device.load(folder))
                with self.assertRaises(AdbError):
                    self.capture.import_capture(cid)
        self.store.import_files.assert_not_called()

    def test_unbound_or_mismatched_metadata_disables_import(self):
        cid = self.start()['capture_id']
        with patch.object(device, 'pull', side_effect=self.pulled):
            self.capture.pull(cid)
        folder = self.capture.root / cid / 'data'
        original = device.load(folder)
        cases = [('generation', 'capture_20261030_120000_' + 'e' * 32),
                 ('archive_sha256', '0' * 64), ('perf_file', str(folder / 'perf_qnx.log'))]
        for key, value in cases + [('generation', None)]:
            state = json.loads(json.dumps(original))
            state['conversion'][key] = value
            device.save(folder, state)
            self.assertFalse(self.capture.status(cid)['can_import'])
            self.assertEqual(self.capture.status(cid)['missing_modules'], [])
            self.assertTrue(self.capture.archive_path(cid).is_file())
            with self.assertRaises(AdbError):
                self.capture.import_capture(cid)
        original.pop('generation')
        device.save(folder, original)
        self.assertFalse(self.capture.status(cid)['can_import'])
        self.store.import_files.assert_not_called()

    def test_implicit_directory_quota_rejects_before_publication(self):
        cid = self.start()['capture_id']
        conn, _ = self.pull_connection(members={'a/b/c/file.txt': b'1'})
        with patch.object(device, 'MAX_MEMBERS', 3), patch.object(device, 'make_connection', return_value=conn):
            with self.assertRaises(AdbError):
                self.capture.pull(cid)
            self.assertFalse(self.capture.status(cid)['state_corrupt'])
        folder = self.capture.root / cid / 'data'
        self.assertNotIn('archive', device.load(folder))
        self.assertEqual(len(list(folder.glob('*.tar'))), 0)
        self.assertEqual(len(list(folder.glob('*.partial'))), 1)

    def test_hash_and_extraction_expiry_return_504_without_publication(self):
        for stage in ('hash', 'extract'):
            with self.subTest(stage=stage):
                cid = self.start()['capture_id']
                conn, _ = self.pull_connection()
                clock = [0]
                if stage == 'hash':
                    digest = device.hashlib.sha256()
                    def update(chunk):
                        digest.update(chunk)
                        clock[0] = 901
                    hook = patch.object(device.hashlib, 'sha256', return_value=Mock(update=update, hexdigest=digest.hexdigest))
                else:
                    original = tarfile.TarFile.extractfile
                    def extract(tar, member):
                        source = original(tar, member)
                        clock[0] = 901
                        return source
                    hook = patch.object(tarfile.TarFile, 'extractfile', extract)
                with patch.object(device, 'make_connection', return_value=conn), patch('time.monotonic', side_effect=lambda: clock[0]), hook:
                    with self.assertRaises(AdbError) as caught:
                        self.capture.pull(cid)
                self.assertEqual(caught.exception.status, 504)
                folder = self.capture.root / cid / 'data'
                self.assertNotIn('archive', device.load(folder))
                self.assertEqual(len(list(folder.glob('*.tar'))), 0)
                self.assertEqual(len(list(folder.glob('*.partial'))), 1)

    def test_archive_publication_failure_never_revives_old_csv(self):
        stages = ('before_invalidation', 'after_invalidation', 'after_link', 'metadata_failure')
        for stage in stages:
            with self.subTest(stage=stage):
                cid = self.start()['capture_id']
                with patch.object(device, 'pull', side_effect=self.pulled):
                    self.capture.pull(cid)
                folder = self.capture.root / cid / 'data'
                old_state = device.load(folder)
                old_archive = self.capture.archive_path(cid)
                old_csv = Path(old_state['conversion']['perf_file'])
                old_csv_content = old_csv.read_bytes()
                conn, payload = self.pull_connection()
                clock = [0]
                original_save, original_link = device.save, device.os.link

                def save(target, state, deadline=None):
                    if state.get('generation') == old_state['generation']:
                        if stage == 'before_invalidation':
                            clock[0] = 901
                        original_save(target, state, deadline)
                        if stage == 'after_invalidation':
                            clock[0] = 901
                    elif stage == 'metadata_failure':
                        raise OSError('offline state commit failure')
                    else:
                        original_save(target, state, deadline)

                def link(source, target):
                    # Invalidation must be durable before the archive appears.
                    self.assertNotIn('conversion', device.load(folder))
                    original_link(source, target)
                    if stage == 'after_link':
                        clock[0] = 901

                with patch.object(device, 'make_connection', return_value=conn), patch('time.monotonic', side_effect=lambda: clock[0]), patch.object(device, 'save', side_effect=save), patch.object(device.os, 'link', side_effect=link), patch.object(device, 'convert') as conversion:
                    with self.assertRaises(AdbError) as caught:
                        self.capture.pull(cid)
                self.assertEqual(caught.exception.status, 502 if stage == 'metadata_failure' else 504)
                conversion.assert_not_called()
                view = self.capture.status(cid)
                self.assertFalse(view['state_corrupt'])
                self.assertTrue(view['archive_available'])
                self.assertEqual(old_archive.read_bytes(), b'offline archive')
                self.assertEqual(old_csv.read_bytes(), old_csv_content)
                self.assertEqual(self.capture.archive_path(cid), old_archive)
                self.assertEqual(len(list(folder.glob('*.partial'))), 1)
                published = stage in ('after_link', 'metadata_failure')
                archives = list(folder.glob('*.tar'))
                self.assertEqual(len(archives), 2 if published else 1)
                if published:
                    self.assertEqual(next(path for path in archives if path != old_archive).read_bytes(), payload)
                if stage == 'before_invalidation':
                    # No new archive exists and old metadata was never changed.
                    self.assertTrue(view['can_import'])
                    self.assertEqual(device.load(folder)['conversion'], old_state['conversion'])
                else:
                    self.assertFalse(view['can_import'])
                    self.assertEqual(view['missing_modules'], [])
                    self.assertNotIn('conversion', device.load(folder))
                    with self.assertRaises(AdbError):
                        self.capture.import_capture(cid)
        self.store.import_files.assert_not_called()

    def test_conversion_binding_publish_checks_remaining_local_budget(self):
        cid = self.start()['capture_id']
        conn, _ = self.pull_connection()
        clock, conversion_started = [0], [False]
        original_convert = device.convert
        original_fsync = device.os.fsync
        def convert_at_deadline(*args, **kwargs):
            self.assertEqual(kwargs['deadline'], 120)
            result = original_convert(*args, **kwargs)
            conversion_started[0] = True
            return result
        def fsync(fd):
            original_fsync(fd)
            if conversion_started[0]:
                clock[0] = 121
        with patch.object(device, 'make_connection', return_value=conn), patch('time.monotonic', side_effect=lambda: clock[0]), patch.object(device, 'convert', side_effect=convert_at_deadline), patch.object(device.os, 'fsync', side_effect=fsync):
            with self.assertRaises(AdbError) as caught:
                self.capture.pull(cid)
        self.assertEqual(caught.exception.status, 504)
        folder = self.capture.root / cid / 'data'
        self.assertNotIn('conversion', device.load(folder))
        self.assertFalse(self.capture.status(cid)['can_import'])
        self.assertTrue(self.capture.archive_path(cid).is_file())

    def test_conversion_deadline_covers_sort_write_and_publish(self):
        import perf_qnx_convert as converter
        raw = self.root / 'raw'
        raw.mkdir()
        (raw / 'cpuprofiler_1.txt').write_text('SystemTime TotalCPU(%)\n0 10\n')
        state = dict(estimated_start_utc_ms=1700000000000, alignment_error_ms=20)
        for stage in ('scan', 'sort', 'write', 'publish'):
            with self.subTest(stage=stage):
                output = self.root / (stage + '.log')
                clock = [0]
                if stage == 'scan':
                    def walk(*args, **kwargs):
                        clock[0] = 6
                        yield str(raw), [], ['unmatched.txt']
                    hook = patch('os.walk', side_effect=walk)
                elif stage == 'sort':
                    def slow_sorted(items, **kwargs):
                        result = sorted(items, **kwargs)
                        if result and isinstance(result[0], list):
                            clock[0] = 6
                        return result
                    hook = patch.object(converter, 'sorted', side_effect=slow_sorted, create=True)
                elif stage == 'write':
                    original_writer = converter.csv.writer
                    def writer(*args, **kwargs):
                        real = original_writer(*args, **kwargs)
                        def rows(items):
                            real.writerows(items)
                            clock[0] = 6
                        return SimpleNamespace(writerows=rows)
                    hook = patch.object(converter.csv, 'writer', side_effect=writer)
                else:
                    original_write = Path.write_text
                    def write(path, *args, **kwargs):
                        result = original_write(path, *args, **kwargs)
                        if path.name.endswith('.diagnostics.txt'):
                            clock[0] = 6
                        return result
                    hook = patch.object(Path, 'write_text', write)
                with patch('time.monotonic', side_effect=lambda: clock[0]), hook:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        convert(raw, output, state, deadline=5)
                self.assertFalse(output.exists())

    def test_sort_merge_deadline_and_order(self):
        from perf_qnx_convert import deadline_sorted
        values = list(range(9000, 0, -1))
        clock = [0]
        with patch('time.monotonic', side_effect=lambda: clock[0]):
            iterator = deadline_sorted(values, 5)
            self.assertEqual(next(iterator), 1)
            clock[0] = 5
            with self.assertRaises(subprocess.TimeoutExpired):
                next(iterator)
        self.assertEqual(list(deadline_sorted(values, None)), sorted(values))

    def test_connection_keeps_explicit_expired_deadline(self):
        with patch('time.monotonic', return_value=0):
            conn = device.Connection('mock-adb', SERIAL, ADDRESS, deadline=0)
            with self.assertRaises(subprocess.TimeoutExpired):
                conn.call(['shell', 'true'])
        self.process.assert_not_called()

    def test_api_validation_security_and_local_workflow(self):
        with patch('perf_api.Store', return_value=self.store), patch('perf_api.AdbController', return_value=self.adb), patch('perf_diagnostics.configure'), patch('perf_diagnostics.flush'):
            app = create_app(self.root / 'api.db')
            with TestClient(app, base_url='http://127.0.0.1:8765') as client:
                base = {'confirm': True, 'serial': SERIAL, 'qnx_address': ADDRESS}
                self.assertEqual(client.post('/api/qnx/start', json=base).status_code, 403)
                token = client.get('/api/token').json()['token']
                client.headers['x-session-token'] = token
                for headers in ({'host': 'evil.test'}, {'origin': 'https://evil.test'}):
                    self.assertEqual(client.post('/api/qnx/start', json=base, headers=headers).status_code, 403)
                invalid = [dict(base, confirm=value) for value in (1, 'true', False)]
                invalid += [{key: value for key, value in base.items() if key != 'confirm'}]
                invalid += [dict(base, **extra) for extra in ({'adb': '/unsafe'}, {'output': '/unsafe'},
                    {'interval_ms': 99}, {'interval_ms': True}, {'max_duration_s': 3601},
                    {'qnx_address': 'host;id'}, {'serial': '-option'})]
                for payload in invalid:
                    with self.subTest(payload=payload):
                        self.assertEqual(client.post('/api/qnx/start', json=payload).status_code, 422)
                self.assertEqual(client.post('/api/qnx/recover', json=base).status_code, 422)
                self.assertEqual(client.post('/api/qnx/recover', json=dict(base, remote_session_id=SID, stop=1)).status_code, 422)
                for action in ('stop', 'pull', 'import'):
                    self.assertEqual(client.post('/api/qnx/' + action, json={'confirm': 1, 'capture_id': SID}).status_code, 422)
                    self.assertEqual(client.post('/api/qnx/' + action, json={'confirm': True, 'capture_id': '../bad'}).status_code, 422)
                self.devices.assert_not_called()
                with patch.object(device, 'start_capture', side_effect=self.upstream):
                    response = client.post('/api/qnx/start', json=base)
                self.assertEqual(response.status_code, 200, response.text)
                cid = response.json()['capture_id']
                with patch.object(device, 'stop', side_effect=self.pulled):
                    stopped = client.post('/api/qnx/stop', json={'confirm': True, 'capture_id': cid})
                self.assertEqual(stopped.status_code, 200, stopped.text)
                self.assertTrue(stopped.json()['can_import'])
                self.devices.reset_mock()
                self.assertEqual(client.get('/api/qnx/captures').status_code, 200)
                self.assertEqual(client.get('/api/qnx/status', params={'capture_id': cid}).status_code, 200)
                imported = client.post('/api/qnx/import', json={'confirm': True, 'capture_id': cid})
                self.assertEqual(imported.status_code, 200, imported.text)
                self.assertEqual(imported.json()['session_id'], 'b' * 32)
                archive = client.get('/api/qnx/archive', params={'capture_id': cid})
                self.assertEqual(archive.status_code, 200)
                self.assertEqual(archive.content, b'offline archive')
                self.devices.assert_not_called()
                self.process.assert_not_called()


if __name__ == '__main__':
    unittest.main()

