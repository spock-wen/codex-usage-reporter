"""Synthetic backend invariants; never reads real Codex authentication or logs."""
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from collector import TOKEN_FIELDS, parse_time
from server import AlreadyExists, Invalid, Store, canonical_json, csv_safe, export_csv, query_period, validate_package

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
