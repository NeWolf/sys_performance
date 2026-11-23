"""Storage tests operate exclusively on freshly created temporary databases."""
import io
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from perf_store import Store
from perf_storage import StorageBusy, StorageInsufficientSpace


SAMPLE = b'S,946684800123,7,42.04,25.09,14.17,0.00,3.25,9326,14725,5399\n'


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='perf-storage-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root / 'test.sqlite')

    def session(self, sid='one', count=1):
        with self.store.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',
                       (sid, sid, '2026', sid, json.dumps({'counts': {'S': count}})))
            db.execute('INSERT INTO records(session,kind,data) VALUES (?,?,?)', (sid, 'S', '{}'))
        return sid

    def log(self, name='capture.log', content=b'archive'):
        path = self.root / 'adb_logs' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def merge_source(self, offset=0, **kwargs):
        sample = SAMPLE.replace(b'946684800123', str(946684800123 + offset).encode())
        return self.store.import_files([('capture.log', io.BytesIO(sample))], **kwargs)['id']

    def test_merge_preserves_samples_boundaries_and_log_ownership(self):
        first, second = self.merge_source(), self.merge_source(60000)
        archive = self.log()
        self.store.register_session_logs(first, [archive])
        with self.store.connect() as db:
            db.execute('INSERT INTO report_cache VALUES (?,?,?)', (first, 'v', b'old'))
            db.execute('INSERT INTO process_groups VALUES (?,?,?)', (first, 'g', '{}'))
            raw = [row[0] for row in db.execute('SELECT data FROM records ORDER BY ts')]
        result = self.store.merge_sessions([second, first], '当天采集')
        merged = result['id']
        self.assertEqual(result['source_count'], 2)
        self.assertEqual(len(self.store.sessions()), 3)
        summary = self.store.session(merged)['summary']
        self.assertEqual(self.store.session(merged)['name'], '当天采集')
        self.assertEqual(summary['counts'], {'S': 2})
        self.assertEqual((summary['cycles'], summary['segments']), (2, 2))
        self.assertEqual([s['id'] for s in summary['merge_sources']], [first, second])
        self.assertEqual(summary['last_ts'] - summary['first_ts'], 60000)
        self.assertEqual(summary['event_count'], 1)
        self.assertIn('不推断设备重启', summary['events'][0]['message'])
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM records WHERE session=? ORDER BY id', (merged,)).fetchall()
            self.assertEqual([r['data'] for r in rows], raw)
            self.assertEqual([(r['segment'], r['cycle']) for r in rows], [(0, 1), (1, 2)])
            for table in ('report_cache', 'process_groups'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table} WHERE session=?', (merged,)).fetchone()[0], 0)
        self.store.delete_many([first, second])
        self.assertTrue(archive.exists())
        self.assertEqual(self.store.overview(merged)['stats']['samples'], 2)
        from perf_report import render_report
        self.assertTrue(render_report(self.store, merged))
        self.assertEqual(Store(self.store.path).session(merged)['name'], '当天采集')
        self.store.delete_many([merged])
        self.assertFalse(archive.exists())

    def test_merge_top_and_qnx_reports_after_source_deletion(self):
        from test_top_parser import header, CPU, MEM, TABLE, ROW
        from test_qnx_parser import row
        from perf_report import render_report
        samples = {
            'top': [header(date=f'2026-07-01 12:0{i}:00') + CPU + MEM + TABLE + ROW for i in range(2)],
            'qnx': [row(ts=1793340807699 + i * 60000) for i in range(2)],
        }
        for source_format, texts in samples.items():
            with self.subTest(source_format=source_format):
                ids = [self.store.import_files([(f'{source_format}.log', io.BytesIO(text.encode()))])['id']
                       for text in texts]
                merged = self.store.merge_sessions(ids)['id']
                self.store.delete_many(ids)
                overview = self.store.overview(merged)
                self.assertEqual(overview['summary']['source_format'], source_format)
                self.assertEqual(overview['summary']['segments'], 2)
                self.assertEqual(overview['stats']['samples'], 2)
                self.assertTrue(render_report(self.store, merged))

    def test_merge_rejects_incompatible_formats_and_known_devices(self):
        first = self.merge_source(device_name='设备一')
        second = self.merge_source(1000, device_name='设备二')
        with self.assertRaisesRegex(ValueError, '不同设备'):
            self.store.merge_sessions([first, second])
        info = self.store.session(second)['summary']
        info.update(device_name='设备一', source_format='top')
        with self.store.connect() as db:
            db.execute('UPDATE sessions SET summary=? WHERE id=?', (json.dumps(info), second))
        with self.assertRaisesRegex(ValueError, '日志格式'):
            self.store.merge_sessions([first, second])
        self.assertEqual(len(self.store.sessions()), 2)

    def test_merge_sparse_cycles_segments_and_overlapping_times(self):
        first, second = self.merge_source(), self.merge_source(1000)
        with self.store.connect() as db:
            db.execute('UPDATE records SET segment=7,cycle=9 WHERE session=?', (first,))
            db.execute('''INSERT INTO records(session,segment,cycle,ts,kind,pid,name,source,line,data)
                SELECT session,12,2,ts,kind,pid,name,source,line,data FROM records WHERE session=?''', (first,))
            db.execute('UPDATE records SET ts=946684800123 WHERE session=?', (second,))
        merged = self.store.merge_sessions([first, second])['id']
        with self.store.connect() as db:
            pairs = db.execute('SELECT segment,cycle FROM records WHERE session=? ORDER BY segment', (merged,)).fetchall()
        self.assertEqual([tuple(row) for row in pairs], [(0, 1), (1, 2), (2, 3)])
        self.assertEqual(self.store.session(merged)['summary']['cycles'], 3)

    def test_merge_validation_lineage_and_rollback(self):
        first, second, third = [self.merge_source(n * 1000) for n in range(3)]
        for ids in ([], [first], [first, first], [first, ' '], 'invalid', [first] * 1001):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.store.merge_sessions(ids)
        with self.assertRaises(KeyError):
            self.store.merge_sessions([first, 'missing'])
        with self.assertRaises(ValueError):
            self.store.merge_sessions([first, second], 'x' * 121)
        merged = self.store.merge_sessions([first, second])['id']
        with self.assertRaisesRegex(ValueError, '重复来源'):
            self.store.merge_sessions([merged, first])
        again = self.store.merge_sessions([first, second])['id']
        self.assertNotEqual(again, merged)
        with self.assertRaisesRegex(ValueError, '重复来源'):
            self.store.merge_sessions([merged, again])
        nested = self.store.merge_sessions([merged, third])['id']
        self.assertEqual(self.store.session(nested)['summary']['counts']['S'], 3)
        self.store.delete_many([first])
        reimported = self.merge_source()
        with self.assertRaisesRegex(ValueError, '重复来源'):
            self.store.merge_sessions([merged, reimported])
        before = len(self.store.sessions())
        with self.store.connect() as db:
            db.execute("""CREATE TRIGGER refuse_merge BEFORE INSERT ON records
                WHEN new.session NOT IN (SELECT session FROM records)
                BEGIN SELECT RAISE(ABORT, 'blocked'); END""")
        with self.assertRaises(__import__('sqlite3').IntegrityError):
            self.store.merge_sessions([second, third])
        self.assertEqual(len(self.store.sessions()), before)

    def test_batch_cascade_missing_and_unmanaged(self):
        self.session('one')
        self.session('two')
        external = self.root / 'external.log'
        external.write_bytes(b'untouched')
        orphan = self.log('unregistered.log')
        with self.store.connect() as db:
            db.execute('INSERT INTO report_cache VALUES (?,?,?)', ('one', 'v', b'cache'))
            db.execute('INSERT INTO analysis_cache VALUES (?,?,?)', ('two', 'all', '{}'))
            db.execute('INSERT INTO process_groups VALUES (?,?,?)', ('one', 'g', '{}'))
        result = self.store.delete_many(['one', 'absent', 'two', 'one'])
        self.assertEqual(result['deleted_ids'], ['one', 'two'])
        self.assertEqual(result['missing_ids'], ['absent'])
        with self.store.connect() as db:
            for table in ('sessions', 'records', 'report_cache', 'analysis_cache', 'process_groups'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        self.assertTrue(external.exists())
        self.assertTrue(orphan.exists())
        self.assertFalse(self.store.delete('one'))

    def test_shared_log_only_last_owner_deletes(self):
        self.session('one')
        self.session('two')
        path = self.log()
        for sid in ('one', 'two'):
            self.store.register_session_logs(sid, [path, path])
        usage = self.store.storage_usage()
        self.assertEqual(usage['logs_bytes'], 7)
        self.assertEqual([r['logs_bytes'] for r in usage['sessions']], [7, 7])
        self.assertEqual(self.store.delete_many(['one'])['logs_freed_bytes'], 0)
        self.assertTrue(path.exists())
        self.assertEqual(self.store.delete_many(['two'])['logs_freed_bytes'], 7)
        self.assertFalse(path.exists())

    def test_directory_child_sharing_and_deduplicated_size(self):
        self.session('one')
        self.session('two')
        folder = self.root / 'qnx_logs' / uuid.uuid4().hex
        folder.mkdir(parents=True)
        child = folder / 'qnx.csv'
        child.write_bytes(b'qnx')
        sidecar = folder / 'metadata.json'
        sidecar.write_bytes(b'{}')
        self.store.register_session_logs('one', [folder, child])
        self.store.register_session_logs('two', [child])
        self.assertEqual(self.store.storage_usage()['logs_bytes'], 5)
        result = self.store.delete_many(['one'])
        self.assertTrue(result['warnings'])
        self.assertTrue(sidecar.exists())
        result = self.store.delete_many(['two'])
        self.assertEqual(result['logs_freed_bytes'], 5)
        self.assertFalse(folder.exists())

    def test_reject_external_roots_symlinks_and_atomic_registration(self):
        self.session()
        good = self.log()
        external = self.root / 'outside.log'
        external.write_bytes(b'outside')
        symlink = self.root / 'adb_logs' / 'link.log'
        symlink.symlink_to(external)
        folder = self.root / 'qnx_logs' / uuid.uuid4().hex
        folder.mkdir(parents=True)
        (folder / 'linked').symlink_to(external)
        for invalid in (external, good.parent, self.root, symlink, folder,
                        good.parent / '..' / 'outside.log'):
            with self.subTest(path=str(invalid)), self.assertRaises(ValueError):
                self.store.register_session_logs('one', [good, invalid])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM session_logs').fetchone()[0], 0)
        with self.assertRaises(KeyError):
            self.store.register_session_logs('missing', [good])
        self.assertTrue(external.exists())

    def test_cleanup_failure_persists_and_retries_after_reopen(self):
        self.session()
        path = self.log()
        self.store.register_session_logs('one', [path])
        with patch.object(self.store, '_remove_asset', side_effect=PermissionError('denied')):
            result = self.store.delete_many(['one'])
        self.assertEqual(result['deleted_ids'], ['one'])
        self.assertTrue(result['warnings'])
        self.assertTrue(path.exists())
        reopened = Store(self.store.path)
        with reopened.connect() as db:
            row = db.execute('SELECT * FROM storage_assets').fetchone()
            self.assertEqual(row['state'], 'pending')
            self.assertEqual(row['attempts'], 1)
            self.assertIn('denied', row['last_error'])
        self.assertEqual(reopened.storage_usage()['pending_cleanup_count'], 1)
        self.assertEqual(reopened.retry_pending_cleanup()['logs_freed_bytes'], 7)
        self.assertFalse(path.exists())

    def test_replaced_asset_symlink_is_not_followed(self):
        self.session()
        path = self.log()
        self.store.register_session_logs('one', [path])
        moved = path.with_suffix('.saved')
        path.rename(moved)
        external = self.root / 'outside.log'
        external.write_bytes(b'safe')
        path.symlink_to(external)
        result = self.store.delete_many(['one'])
        self.assertTrue(result['warnings'])
        self.assertEqual(external.read_bytes(), b'safe')
        self.assertEqual(self.store.storage_usage()['pending_cleanup_count'], 1)

    def test_import_duplicate_keeps_all_archive_paths(self):
        first = self.log('first.log', SAMPLE)
        second = self.log('second.log', SAMPLE)
        third = self.log('third.log', SAMPLE)
        with first.open('rb') as stream:
            initial = self.store.import_files([('first.log', stream)])
        with second.open('rb') as a, third.open('rb') as b:
            duplicate = self.store.import_files([('second.log', a), ('third.log', b)])
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(initial['id'], duplicate['id'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM session_logs').fetchone()[0], 3)
        self.assertEqual(self.store.delete_many([initial['id']])['logs_freed_bytes'], 3 * len(SAMPLE))

    def test_capture_directory_registration_is_explicit_and_duplicate_safe(self):
        folders = []
        for index, root in enumerate(('adb_logs', 'top_logs', 'qnx_logs')):
            folder = self.root / root / uuid.uuid4().hex
            folder.mkdir(parents=True)
            path = folder / 'data' / 'analysis.log'
            path.parent.mkdir()
            path.write_bytes(SAMPLE)
            (folder / 'raw.tar').write_bytes(b'raw')
            (folder / 'local.json').write_bytes(b'{}')
            with path.open('rb') as stream:
                stream._capture_log_directory = folder
                result = self.store.import_files([('old-unrelated-name.log', stream)])
            self.assertEqual(result['duplicate'], index > 0)
            folders.append(str(folder))
        with self.store.connect() as db:
            self.assertEqual(sorted(row[0] for row in db.execute('SELECT path FROM session_logs')),
                             sorted(folders))
            self.assertEqual({row[0] for row in db.execute('SELECT kind FROM storage_assets')}, {'directory'})
        self.assertEqual(self.store.storage_usage()['logs_bytes'], 3 * (len(SAMPLE) + 5))

    def test_capture_directory_invalid_binding_rolls_back_even_for_duplicates(self):
        folder = self.root / 'adb_logs' / uuid.uuid4().hex
        folder.mkdir(parents=True)
        path = folder / 'perf.log'
        path.write_bytes(SAMPLE)
        other = self.root / 'adb_logs' / uuid.uuid4().hex
        other.mkdir()
        external = self.root / uuid.uuid4().hex
        external.mkdir()
        linked = self.root / 'adb_logs' / uuid.uuid4().hex
        linked.symlink_to(folder, target_is_directory=True)
        unsafe = self.root / 'qnx_logs' / uuid.uuid4().hex
        unsafe.mkdir(parents=True)
        (unsafe / 'sidecar').symlink_to(external, target_is_directory=True)
        for duplicate in (False, True):
            if duplicate:
                self.store.import_files([('upload.log', io.BytesIO(SAMPLE))])
            for invalid in (other, external, linked, unsafe, folder.parent,
                            folder / '..' / folder.name, path):
                with self.subTest(duplicate=duplicate, directory=invalid):
                    with path.open('rb') as stream:
                        stream._capture_log_directory = invalid
                        with self.assertRaises(ValueError):
                            self.store.import_files([('perf.log', stream)])
                    with self.store.connect() as db:
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], int(duplicate))
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM session_logs').fetchone()[0], 0)
                        self.assertEqual(db.execute('SELECT COUNT(*) FROM storage_assets').fetchone()[0], 0)
        fake = io.BytesIO(SAMPLE)
        fake.name = str(path)
        fake._capture_log_directory = folder
        with self.assertRaises(ValueError):
            self.store.import_files([('perf.log', fake)])

    def test_capture_directory_parser_failure_rolls_back_registration(self):
        path = self.log(uuid.uuid4().hex + '/perf.log', SAMPLE)
        with path.open('rb') as stream:
            stream._capture_log_directory = path.parent
            with patch('perf_store.iter_records', side_effect=RuntimeError('parse failed')):
                with self.assertRaises(RuntimeError):
                    self.store.import_files([('perf.log', stream)])
        with self.store.connect() as db:
            for table in ('sessions', 'session_logs', 'storage_assets'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        self.assertTrue(path.exists())

    def test_import_upload_and_external_are_unmanaged(self):
        external = self.root / 'outside.log'
        external.write_bytes(SAMPLE)
        with external.open('rb') as stream:
            result = self.store.import_files([('outside.log', stream)])
        fake = io.BytesIO(SAMPLE)
        fake.name = str(self.log())
        self.store.import_files([('fake.log', fake)])
        usage = self.store.storage_usage()
        self.assertTrue(usage['sessions'][0]['unmanaged_logs'])
        self.store.delete_many([result['id']])
        self.assertTrue(external.exists())

    def test_usage_sample_bounded_and_cache_payload_exact(self):
        self.session(count=100000)
        with self.store.connect() as db:
            db.executemany('INSERT INTO records(session,kind,data) VALUES (?,?,?)',
                           [('one', 'S', 'x' * 100)] * 200)
            db.execute('INSERT INTO report_cache VALUES (?,?,?)', ('one', 'v', b'c' * 1000))
        statements = []
        original = __import__('sqlite3').connect
        def tracked(*args, **kwargs):
            db = original(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch('perf_store.sqlite3.connect', side_effect=tracked):
            usage = self.store.storage_usage()
        queries = [s for s in statements if 'FROM records' in s]
        self.assertEqual(len(queries), 1)
        self.assertIn('LIMIT 128', queries[0])
        row = usage['sessions'][0]
        self.assertEqual(row['cache_bytes'], 1001)
        self.assertTrue(row['estimated'])
        self.assertTrue(row['unmanaged_logs'])
        self.assertGreater(row['database_bytes'], 100000)
        self.assertEqual(usage['database_bytes'], self.store.path.stat().st_size)
        with self.store.connect() as db:
            exact = db.execute('PRAGMA freelist_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
        self.assertEqual(usage['reclaimable_bytes'], exact)

    def create_free_pages(self):
        self.session()
        with self.store.connect() as db:
            db.execute('INSERT INTO report_cache VALUES (?,?,?)', ('one', 'v', b'x' * 2_000_000))
        self.store.delete_many(['one'])
        self.assertGreater(self.store.storage_usage()['reclaimable_bytes'], 0)

    def test_reclaim_and_noop(self):
        self.assertTrue(self.store.reclaim_space()['skipped'])
        self.create_free_pages()
        result = self.store.reclaim_space()
        self.assertFalse(result['skipped'])
        self.assertGreater(result['reclaimed_bytes'], 0)
        self.assertLess(result['after_bytes'], result['before_bytes'])

    def test_reclaim_insufficient_disk_does_not_vacuum(self):
        self.create_free_pages()
        before = self.store.path.stat().st_size
        with patch('perf_storage.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaises(StorageInsufficientSpace) as caught:
                self.store.reclaim_space()
        self.assertGreaterEqual(caught.exception.required_bytes, before * 2)
        self.assertEqual(caught.exception.free_bytes, 1)
        self.assertEqual(self.store.path.stat().st_size, before)

    def test_active_connection_and_cross_instance_maintenance_protection(self):
        other = Store(self.store.path)
        with self.store.connect():
            self.assertEqual(other.storage_usage()['sessions'], [])
            for action in (other.reclaim_space, lambda: other.delete_many([])):
                with self.assertRaises(StorageBusy):
                    action()
        failures = []
        def competing():
            try:
                other.sessions()
            except StorageBusy:
                failures.append('busy')
        with self.store._storage_maintenance():
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT 1').fetchone()[0], 1)
            worker = threading.Thread(target=competing)
            worker.start()
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(failures, ['busy'])
        self.assertEqual(other.sessions(), [])

    def test_batch_failure_rolls_back_before_log_cleanup(self):
        self.session('one')
        self.session('two')
        path = self.log()
        self.store.register_session_logs('one', [path])
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON sessions "
                       "WHEN old.id='two' BEGIN SELECT RAISE(ABORT, 'blocked'); END")
        with self.assertRaises(__import__('sqlite3').IntegrityError):
            self.store.delete_many(['one', 'two'])
        self.assertEqual(len(self.store.sessions()), 2)
        self.assertTrue(path.exists())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM storage_assets').fetchone()[0], 'active')

    def test_import_failure_rolls_back_log_registration(self):
        path = self.log(content=SAMPLE)
        with path.open('rb') as stream:
            with patch('perf_store.iter_records', side_effect=RuntimeError('parse failed')):
                with self.assertRaises(RuntimeError):
                    self.store.import_files([('capture.log', stream)])
        with self.store.connect() as db:
            for table in ('sessions', 'session_logs', 'storage_assets'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        self.assertTrue(path.exists())

    def test_external_sqlite_lock_maps_to_busy(self):
        self.session()
        db = __import__('sqlite3').connect(self.store.path, timeout=0)
        try:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(StorageBusy):
                self.store.delete_many(['one'])
        finally:
            db.rollback()
            db.close()
        self.assertEqual(len(self.store.sessions()), 1)

    def test_unreadable_directory_is_not_cleaned_or_silently_counted(self):
        self.session()
        directory = self.root / 'qnx_logs' / uuid.uuid4().hex
        directory.mkdir(parents=True)
        archive = directory / 'capture.log'
        archive.write_bytes(b'keep')
        self.store.register_session_logs('one', [directory])

        def denied(path, *, followlinks, onerror):
            onerror(PermissionError('unreadable subtree'))
            return iter(())

        with patch('perf_storage.os.walk', side_effect=denied):
            with self.assertRaises(PermissionError):
                self.store.register_session_logs('one', [directory])
            usage = self.store.storage_usage()
            self.assertTrue(usage['warnings'])
            result = self.store.delete_many(['one'])
        self.assertTrue(result['warnings'])
        self.assertTrue(archive.exists())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM storage_assets').fetchone()[0], 'pending')
        self.store.retry_pending_cleanup()
        self.assertFalse(directory.exists())

    def test_only_maintenance_connections_fail_fast(self):
        with self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
        with self.store._storage_maintenance(), self.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 0)

    def test_cleanup_holds_sqlite_write_lock_during_file_removal(self):
        import sqlite3
        self.session()
        path = self.log()
        self.store.register_session_logs('one', [path])
        original = self.store._remove_asset
        def checked(path, kind):
            external = sqlite3.connect(self.store.path, timeout=0)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    external.execute('BEGIN IMMEDIATE')
            finally:
                external.close()
            original(path, kind)
        with patch.object(self.store, '_remove_asset', side_effect=checked):
            self.store.delete_many(['one'])
        self.assertFalse(path.exists())

    def test_connection_failure_releases_active_slot(self):
        with self.assertRaises(RuntimeError):
            with self.store.connect():
                raise RuntimeError('abort')
        self.assertTrue(self.store.reclaim_space()['skipped'])


class StorageApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from perf_api import create_app
        self.temp = tempfile.TemporaryDirectory(prefix='perf-storage-api-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = create_app(self.root / 'api.sqlite')
        self.store = self.app.state.store
        self.client = TestClient(self.app, base_url='http://127.0.0.1:8765')
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.headers = {'X-Session-Token': self.client.get('/api/token').json()['token']}

    def post(self, action, body=None):
        return self.client.post('/api/storage/' + action, headers=self.headers,
                                json={'confirm': True} if body is None else body)

    def upload(self):
        return self.client.post('/api/import', headers=self.headers,
                                files=[('files', ('perf.log', SAMPLE, 'text/plain'))])

    def test_merge_api_validation_and_success(self):
        url = '/api/sessions/merge'
        def merge(body):
            return self.client.post(url, headers=self.headers, json=body)
        for body in ({}, {'session_ids': ['a']}, {'session_ids': ['a', 'a']},
                     {'session_ids': [' ', 'b']}, {'session_ids': [1, 'b']},
                     {'session_ids': ['x' * 129, 'b']}, {'session_ids': ['x'] * 1001},
                     {'session_ids': ['a', 'b'], 'name': 'x' * 121},
                     {'session_ids': ['a', 'b'], 'extra': True}):
            self.assertEqual(merge(body).status_code, 422, body)
        self.assertEqual(self.client.post(url, json={'session_ids': ['a', 'b']}).status_code, 403)
        self.assertEqual(merge({'session_ids': ['a', 'b']}).status_code, 404)
        first = self.upload().json()['id']
        second = self.store.import_files([('later.log', io.BytesIO(SAMPLE.replace(b'946684800123', b'946684810123')))])['id']
        self.assertNotEqual(first, second)
        response = merge({'session_ids': [second, first], 'name': '当天汇总'})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['source_count'], 2)
        response = self.client.get('/api/sessions/' + result['id'], headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['name'], '当天汇总')
        self.assertEqual(len(self.store.sessions()), 3)
        self.assertEqual(merge({'session_ids': [result['id'], first]}).status_code, 400)
        # Both successful and failed requests release the shared operation lock.
        self.assertEqual(merge({'session_ids': [first, second]}).status_code, 200)

    def test_merge_holds_shared_lock(self):
        from concurrent.futures import ThreadPoolExecutor
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test timed out')
            raise ValueError('merge failed')
        with patch.object(self.store, 'merge_sessions', side_effect=blocked), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.client.post, '/api/sessions/merge', headers=self.headers,
                                  json={'session_ids': ['a', 'b']})
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.upload().status_code, 409)
                self.assertEqual(self.post('reclaim').status_code, 409)
                self.assertEqual(self.client.delete('/api/sessions/a', headers=self.headers).status_code, 409)
                self.assertEqual(self.client.post('/api/sessions/merge', headers=self.headers,
                                                 json={'session_ids': ['a', 'b']}).status_code, 409)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).status_code, 400)
        self.assertEqual(self.upload().status_code, 200)

    def test_confirmation_validation_and_token(self):
        for action in ('delete', 'retry-cleanup', 'reclaim'):
            base = {'session_ids': ['one']} if action == 'delete' else {}
            for value, status in ((False, 400), ('true', 422), (1, 422), (None, 422)):
                with self.subTest(action=action, value=value):
                    self.assertEqual(self.post(action, dict(base, confirm=value)).status_code, status)
            self.assertEqual(self.post(action, base).status_code, 422)
            self.assertEqual(self.post(action, dict(base, confirm=True, unexpected=1)).status_code, 422)
            self.assertEqual(self.client.post('/api/storage/' + action,
                                             json=dict(base, confirm=True)).status_code, 403)
        for ids in ([], [' '], ['x' * 129], ['x'] * 1001, [1], 'one'):
            self.assertEqual(self.post('delete', {'confirm': True, 'session_ids': ids}).status_code, 422)

    def test_usage_with_active_database_query(self):
        sid = self.upload().json()['id']
        with self.store.connect() as db:
            db.execute('BEGIN')
            self.assertEqual(db.execute('SELECT id FROM sessions').fetchone()[0], sid)
            response = self.client.get('/api/storage', headers=self.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual([row['id'] for row in response.json()['sessions']], [sid])
            # Read concurrency must not weaken destructive maintenance protection.
            self.assertEqual(self.post('reclaim').status_code, 409)
            self.assertEqual(self.post('delete', {'confirm': True, 'session_ids': [sid]}).status_code, 409)
        self.assertEqual(self.client.get('/api/storage', headers=self.headers).status_code, 200)
        self.assertEqual(self.store.session(sid)['id'], sid)

    def test_queries_during_usage_keep_maintenance_blocked(self):
        from concurrent.futures import ThreadPoolExecutor
        sid = self.upload().json()['id']
        entered, release = threading.Event(), threading.Event()
        original = self.store._database_sizes

        def blocked():
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test timed out')
            return original()

        with patch.object(self.store, '_database_sizes', side_effect=blocked), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.client.get, '/api/storage', headers=self.headers)
            try:
                self.assertTrue(entered.wait(5))
                response = self.client.get('/api/sessions', headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual([row['id'] for row in response.json()], [sid])
                self.assertEqual(self.post('reclaim').status_code, 409)
                self.assertEqual(self.upload().status_code, 409)
                with self.assertRaises(StorageBusy):
                    self.store.delete_many([sid])
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).status_code, 200)
        self.assertEqual(self.store._storage_gate.active, 0)
        self.assertEqual(self.upload().status_code, 200)

    def test_usage_batch_cleanup_and_single_delete(self):
        sid = self.upload().json()['id']
        path = self.root / 'adb_logs' / 'captured.log'
        path.parent.mkdir()
        path.write_bytes(SAMPLE)
        self.store.register_session_logs(sid, [path])
        external = self.root / 'manual.log'
        external.write_bytes(SAMPLE)
        response = self.client.get('/api/storage', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['sessions'][0]['logs_bytes'], len(SAMPLE))
        result = self.post('delete', {'confirm': True, 'session_ids': [sid, sid, 'missing']})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['deleted_ids'], [sid])
        self.assertEqual(result.json()['missing_ids'], ['missing'])
        self.assertFalse(path.exists())
        self.assertTrue(external.exists())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)
        sid = self.upload().json()['id']
        response = self.client.delete('/api/sessions/' + sid, headers=self.headers)
        self.assertTrue(response.json()['deleted'])
        self.assertEqual(response.json()['deleted_ids'], [sid])
        self.assertEqual(self.client.delete('/api/sessions/' + sid, headers=self.headers).status_code, 404)

    def test_cleanup_failure_can_be_retried(self):
        sid = self.upload().json()['id']
        path = self.root / 'adb_logs' / 'captured.log'
        path.parent.mkdir()
        path.write_bytes(SAMPLE)
        self.store.register_session_logs(sid, [path])
        with patch.object(self.store, '_remove_asset', side_effect=PermissionError('denied')):
            response = self.post('delete', {'confirm': True, 'session_ids': [sid]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['warnings'])
        self.assertTrue(path.exists())
        self.assertEqual(self.post('retry-cleanup').json()['logs_freed_bytes'], len(SAMPLE))
        self.assertFalse(path.exists())


    def test_reclaim_space_failure_and_lock_release(self):
        sid = self.upload().json()['id']
        with self.store.connect() as db:
            db.execute('INSERT INTO report_cache VALUES (?,?,?)', (sid, 'v', b'x' * 2000000))
        self.post('delete', {'confirm': True, 'session_ids': [sid]})
        with patch('perf_storage.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
            response = self.post('reclaim')
        self.assertEqual(response.status_code, 507)
        self.assertEqual(response.json()['free_bytes'], 1)
        self.assertGreater(response.json()['required_bytes'], 1)
        response = self.post('reclaim')
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json()['reclaimed_bytes'], 0)
        self.assertTrue(self.post('reclaim').json()['skipped'])

    def test_busy_during_import_and_maintenance(self):
        from concurrent.futures import ThreadPoolExecutor
        entered, release = threading.Event(), threading.Event()
        original = self.store.import_files
        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('test timed out')
            return original(*args, **kwargs)
        with patch.object(self.store, 'import_files', side_effect=blocked), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.upload)
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.client.get('/api/storage', headers=self.headers).status_code, 409)
                for action in ('retry-cleanup', 'reclaim'):
                    self.assertEqual(self.post(action).status_code, 409)
                self.assertEqual(self.post('delete', {'confirm': True, 'session_ids': ['x']}).status_code, 409)
                self.assertEqual(self.client.delete('/api/sessions/x', headers=self.headers).status_code, 409)
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=5).status_code, 200)
        with self.store._storage_maintenance():
            self.assertEqual(self.client.get('/api/storage', headers=self.headers).status_code, 409)
        self.assertEqual(self.client.get('/api/storage', headers=self.headers).status_code, 200)
        self.assertEqual(self.upload().status_code, 200)


if __name__ == '__main__':
    unittest.main()
