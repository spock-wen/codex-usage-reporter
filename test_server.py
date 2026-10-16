"""Synthetic backend invariants; never reads real Codex authentication or logs."""
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from collector import TOKEN_FIELDS, parse_time
from server import AlreadyExists, EnrollmentConflict, Invalid, Store, canonical_json, csv_safe, export_csv, make_server, query_period, validate_package

ADMIN = 'synthetic-admin-key-at-least-24-characters'
START = '2026-10-12T00:00:00+08:00'
END = '2026-10-19T00:00:00+08:00'
PRICES = {'currency': 'USD', 'models': {'test-model': {'input': 2, 'cached_input': .1, 'cache_write': 2.5, 'output': 10}}}


def event(name, stamp='2026-10-12T09:00:00+08:00', model='test-model', inp=100):
    return {'event_id': hashlib.sha256(('openai:codex:' + name).encode()).hexdigest(), 'timestamp': stamp,
            'model': model, 'input_tokens': inp, 'cached_input_tokens': 20,
            'cache_write_input_tokens': 10, 'output_tokens': 10, 'reasoning_output_tokens': 2, 'total_tokens': inp + 10}


def package(events, start=START, end=END, complete=True):
    stable = {'schema_version': 1, 'period_start': start, 'period_end': end, 'events': sorted(events, key=lambda e: e['event_id'])}
    return {**stable, 'kind': 'codex_usage_events', 'batch_id': hashlib.sha256(canonical_json(stable).encode()).hexdigest(),
            'is_complete': complete, 'data_status': 'observed_local_usage' if complete else 'incomplete',
            'incomplete_reasons': [] if complete else ['source_starts_after_period'], 'quality': {},
            'coverage_warnings': ['Local observed subset'], 'metadata': {'account_binding': 'unknown'}}


class ServerStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.prices = self.root / 'prices.json'
        self.prices.write_text(json.dumps(PRICES), encoding='utf-8')
        self.db = self.root / 'usage.sqlite3'
        self.store = Store(self.db, ADMIN, self.prices)
        self.user = self.store.create_user(' Person@Example.COM ')

    def tearDown(self):
        self.tmp.cleanup()

    def ingest(self, events, device='pc-one', email='person@example.com', **kwargs):
        return self.store.ingest(email, {'device_id': device, 'package': package(events, **kwargs)})

    def summary(self, email=None):
        return self.store.summary(parse_time(START), parse_time(END), email)

    def test_registration_hash_only_and_role_isolation(self):
        self.assertEqual(self.user['email'], 'person@example.com')
        self.assertEqual(self.store.authenticate(ADMIN), ('admin', None))
        self.assertEqual(self.store.authenticate(self.user['upload_token']), ('uploader', 'person@example.com'))
        self.assertEqual(self.store.authenticate('bad'), (None, None))
        with self.store.connect() as conn:
            row = conn.execute('SELECT token_hash FROM users').fetchone()
            self.assertNotEqual(row[0], self.user['upload_token'])
            self.assertEqual(len(row[0]), 64)
        self.assertNotIn(self.user['upload_token'], json.dumps(self.store.users()))
        with self.assertRaises(AlreadyExists):
            self.store.create_user('person@example.com')
        self.assertEqual(self.store.authenticate(self.user['upload_token'])[0], 'uploader')

    def enrollment(self, email='auto@example.com', device='automatic-pc', key='a' * 48):
        return self.store.enroll({'email': email, 'device_id': device, 'registration_key': key})

    def test_enrollment_repeat_reopen_and_normalized_mailbox(self):
        first = self.enrollment(email=' Auto@Example.COM ')
        second = self.enrollment()
        self.assertEqual(first['email'], 'auto@example.com')
        self.assertFalse(first['enrollment_repeated'])
        self.assertTrue(second['enrollment_repeated'])
        self.assertEqual(first['upload_token'], second['upload_token'])
        self.assertEqual(first['identity_assurance'], 'self_reported')
        self.store = Store(self.db, ADMIN, self.prices)
        self.assertEqual(self.enrollment()['upload_token'], first['upload_token'])
        self.assertEqual(self.store.authenticate(first['upload_token']), ('uploader', 'auto@example.com'))

    def test_enrollment_multimachine_credentials_independent_and_usage_union(self):
        one = self.enrollment(email='person@example.com', device='auto-one', key='b' * 48)
        two = self.enrollment(email='person@example.com', device='auto-two', key='c' * 48)
        self.assertNotEqual(one['upload_token'], two['upload_token'])
        for credential in (one, two):
            self.assertEqual(self.store.authenticate(credential['upload_token']), ('uploader', 'person@example.com'))
        self.assertEqual(self.store.authenticate(self.user['upload_token']), ('uploader', 'person@example.com'))
        self.ingest([event('shared-auto'), event('one-auto')], device='auto-one')
        self.ingest([event('shared-auto'), event('two-auto')], device='auto-two')
        self.assertEqual(self.summary()['totals']['requests'], 3)

    def test_enrollment_registration_secret_cannot_move_email_or_device(self):
        first = self.enrollment()
        for overrides in ({'email': 'another@example.com'}, {'device': 'other-pc'}):
            with self.subTest(overrides=overrides), self.assertRaises(EnrollmentConflict):
                self.enrollment(**overrides)
        self.assertEqual(self.enrollment()['upload_token'], first['upload_token'])
        self.assertNotIn('another@example.com', {u['email'] for u in self.store.users()})

    def test_disabled_mailbox_cannot_enroll_reactivate_or_authenticate(self):
        first = self.enrollment(email='person@example.com')
        with self.store.connect() as conn:
            conn.execute('UPDATE users SET active=0 WHERE email=?', ('person@example.com',))
        for key in ('a' * 48, 'd' * 48):
            with self.assertRaises(PermissionError):
                self.enrollment(email='person@example.com', key=key)
        self.assertEqual(self.store.authenticate(first['upload_token']), (None, None))
        self.assertEqual(self.store.authenticate(self.user['upload_token']), (None, None))

    def test_enrollment_migrates_legacy_db_without_changing_token(self):
        self.ingest([event('legacy-before-migration')])
        with self.store.connect() as conn:
            old_hash = conn.execute('SELECT token_hash FROM users WHERE email=?', ('person@example.com',)).fetchone()[0]
            conn.execute('DROP TABLE upload_credentials')
        upgraded = Store(self.db, ADMIN, self.prices)
        credential = upgraded.enroll({'email': 'person@example.com', 'device_id': 'upgraded', 'registration_key': 'm' * 48})
        self.assertEqual(upgraded.authenticate(self.user['upload_token']), ('uploader', 'person@example.com'))
        self.assertEqual(upgraded.authenticate(credential['upload_token']), ('uploader', 'person@example.com'))
        with upgraded.connect() as conn:
            self.assertEqual(conn.execute('SELECT token_hash FROM users WHERE email=?', ('person@example.com',)).fetchone()[0], old_hash)
        self.assertEqual(upgraded.summary(parse_time(START), parse_time(END))['totals']['requests'], 1)

    def test_enrollment_database_and_users_never_persist_plaintext_secret(self):
        key = 'PRIVATE_registration_key_which_is_random_0123456789'
        credential = self.enrollment(key=key)
        with self.store.connect() as conn:
            persisted = '\n'.join(conn.iterdump())
            rows = conn.execute('SELECT registration_key_hash,token_hash FROM upload_credentials').fetchall()
        for secret in (key, credential['upload_token'], ADMIN):
            self.assertNotIn(secret, persisted)
            self.assertNotIn(secret, json.dumps(self.store.users()))
            self.assertNotIn(secret, json.dumps(self.summary()))
        self.assertTrue(all(len(v) == 64 for row in rows for v in row))

    def test_enrollment_concurrent_repeat_is_atomic(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.enrollment(), range(16)))
        self.assertEqual(len({r['upload_token'] for r in results}), 1)
        self.assertEqual(sum(not r['enrollment_repeated'] for r in results), 1)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM upload_credentials').fetchone()[0], 1)

    def test_enrollment_rejects_unknown_fields_invalid_secret_and_invalid_device(self):
        valid = {'email': 'person@example.com', 'device_id': 'pc', 'registration_key': 'v' * 48}
        for body in ({**valid, 'admin': True}, {**valid, 'registration_key': 'x' * 31},
                     {**valid, 'registration_key': 'x' * 129}, {**valid, 'registration_key': '!' * 48},
                     {**valid, 'device_id': '../pc'}, {**valid, 'email': 'invalid'}, []):
            with self.subTest(body=body), self.assertRaises(Invalid):
                self.store.enroll(body)

    def test_enrollment_http_public_limited_and_not_admin_authority(self):
        server = make_server('127.0.0.1', 0, self.store)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            body = {'email': 'http-auto@example.com', 'device_id': 'http-device', 'registration_key': 'q' * 48}
            request = Request(base + '/api/enroll', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(request, timeout=3) as response:
                result = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            for path in ('/api/users', '/api/summary', '/api/export.csv'):
                req = Request(base + path, headers={'Authorization': 'Bearer ' + result['upload_token']})
                with self.assertRaises(HTTPError) as raised:
                    urlopen(req, timeout=3)
                self.assertEqual(raised.exception.code, 403)
                raised.exception.close()
            too_large = Request(base + '/api/enroll', data=b'{}', headers={'Content-Type': 'application/json', 'Content-Length': '8193'}, method='POST')
            with self.assertRaises(HTTPError) as raised:
                urlopen(too_large, timeout=3)
            self.assertEqual(raised.exception.code, 413)
            raised.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_repeat_overlap_clone_and_new_machine_accumulate_union(self):
        first = self.ingest([event('shared'), event('first')])
        repeated = self.ingest([event('shared'), event('first')])
        cloned = self.ingest([event('shared'), event('first')], device='pc-clone')
        added = self.ingest([event('shared'), event('second')], device='pc-two', start='2026-10-11T00:00:00+08:00')
        self.assertEqual(first['new'], 2)
        self.assertEqual(repeated['duplicates'], 2)
        self.assertTrue(repeated['batch_repeated'])
        self.assertEqual(cloned['duplicates'], 2)
        self.assertEqual(added['new'], 1)
        self.assertEqual(added['duplicates'], 1)
        summary = self.summary()
        self.assertEqual(summary['totals']['requests'], 3)
        self.assertEqual(summary['totals']['input_tokens'], 300)
        self.assertEqual(len(summary['by_user'][0]['devices']), 3)
        self.assertEqual(len(summary['batches']), 4)

    def test_atomic_concurrent_duplicate_inserts(self):
        data = {'device_id': 'parallel', 'package': package([event('same')])}
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.ingest('person@example.com', data), range(16)))
        self.assertEqual(sum(r['new'] for r in results), 1)
        self.assertEqual(sum(r['duplicates'] for r in results), 15)
        self.assertEqual(self.summary()['totals']['requests'], 1)

    def test_content_conflict_quarantines_without_overwriting_first(self):
        original = event('bad')
        self.ingest([original, event('good')])
        ack = self.ingest([event('bad', inp=200)], device='other')
        self.assertEqual(ack['conflicts'], 1)
        summary = self.summary()
        self.assertEqual(summary['totals']['requests'], 1)
        self.assertEqual(summary['conflicts'][0]['reason'], 'content')
        self.assertIsNone(summary['totals']['estimated_cost_usd'])
        with self.store.connect() as conn:
            row = conn.execute('SELECT canonical,quarantined FROM usage_events WHERE event_id=?', (original['event_id'],)).fetchone()
        self.assertEqual(json.loads(row['canonical']), original)
        self.assertEqual(row['quarantined'], 1)
        self.assertEqual(self.ingest([original])['conflicts'], 1)

    def test_owner_conflict_even_identical_batch_quarantines_all_users(self):
        self.store.create_user('second@example.com')
        self.ingest([event('shared')])
        ack = self.ingest([event('shared')], email='second@example.com')
        self.assertEqual(ack['conflicts'], 1)
        self.assertEqual(self.summary()['totals']['requests'], 0)
        for email in ('person@example.com', 'second@example.com'):
            summary = self.summary(email)
            self.assertEqual(len(summary['conflicts']), 1)
            self.assertFalse(summary['totals']['is_complete'])

    def test_invalid_batch_has_no_partial_insert_device_or_batch(self):
        invalid = event('invalid')
        invalid['output_tokens'] = True
        with self.assertRaises(Invalid):
            self.ingest([event('valid'), invalid])
        with self.store.connect() as conn:
            for table in ('usage_events', 'devices', 'batches'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        duplicate = event('duplicate')
        with self.assertRaises(Invalid):
            self.ingest([duplicate, duplicate])
        bad_hash = package([event('bad-hash')])
        bad_hash['batch_id'] = '0' * 64
        with self.assertRaises(Invalid):
            self.store.ingest('person@example.com', {'device_id': 'pc', 'package': bad_hash})

    def test_payload_owner_cannot_override_upload_token(self):
        with self.assertRaises(PermissionError):
            self.store.ingest('person@example.com', {'device_id': 'pc', 'email': 'other@example.com', 'package': package([event('one')])})

    def test_boundary_timezone_normalization_and_half_open(self):
        self.ingest([event('at-start', '2026-10-11T16:00:00Z'), event('before-end', '2026-10-18T15:59:59.999999Z')])
        summary = self.summary()
        self.assertEqual(summary['totals']['requests'], 2)
        self.assertEqual([r['day'] for r in summary['by_day']], ['2026-10-12', '2026-10-18'])
        with self.assertRaises(Invalid):
            self.ingest([event('at-end', '2026-10-18T16:00:00Z')])
        with self.assertRaises(ValueError):
            self.ingest([event('naive', '2026-10-12T00:00:00')])
        lo, hi = query_period({'start': ['2026-10-12'], 'end': ['2026-10-19']})
        self.assertEqual(lo.isoformat(), START)
        self.assertEqual(hi - lo, timedelta(days=7))

    def test_unknown_model_and_incomplete_sources_not_free(self):
        self.ingest([event('priced'), event('unpriced', model='unknown')])
        summary = self.summary()
        self.assertIsNone(summary['totals']['estimated_cost_usd'])
        self.assertGreater(summary['totals']['priced_cost_usd'], 0)
        self.assertEqual(summary['totals']['unpriced_requests'], 1)
        self.ingest([event('incomplete')], complete=False)
        self.assertFalse(self.summary()['totals']['is_complete'])

    def test_persistent_reopen_retains_auth_and_idempotency(self):
        self.ingest([event('persist')])
        self.store = Store(self.db, ADMIN, self.prices)
        self.assertEqual(self.store.authenticate(self.user['upload_token'])[1], 'person@example.com')
        self.assertEqual(self.ingest([event('persist')])['duplicates'], 1)
        self.assertEqual(self.summary()['totals']['requests'], 1)

    def test_large_package_over_six_megabytes_is_accepted(self):
        events = [event('large-' + str(i)) for i in range(23_000)]
        payload = {'device_id': 'large-device', 'package': package(events)}
        self.assertGreater(len(json.dumps(payload).encode()), 6 * 1024 * 1024)
        ack = self.store.ingest('person@example.com', payload)
        self.assertEqual(ack['new'], 23_000)
        self.assertEqual(self.summary()['totals']['requests'], 23_000)

    def test_incomplete_empty_and_missing_users_not_verified_zero(self):
        self.assertFalse(self.summary()['totals']['is_complete'])
        ack = self.ingest([], complete=True)
        self.assertFalse(ack['is_complete'])
        self.assertFalse(self.summary()['totals']['is_complete'])

    def test_csv_formula_protection(self):
        self.assertEqual(csv_safe('=cmd'), "'=cmd")
        self.assertEqual(csv_safe(' +cmd'), "' +cmd")
        self.assertTrue(export_csv(self.summary()).startswith(b'\xef\xbb\xbf'))


if __name__ == '__main__':
    unittest.main()
