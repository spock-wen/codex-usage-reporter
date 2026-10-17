import json
import sqlite3
import tempfile
import unittest
import urllib.request
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from collector import BEIJING
from cursor_weekly import NoRedirectHandler, collect_account, validate_local_endpoint, week_bounds
from server import Invalid, Store, cursor_cents_number, cursor_cents_output, validate_cursor_snapshot


class CursorWeeklyTests(unittest.TestCase):
    def test_previous_natural_week_uses_beijing_midnight_bounds(self):
        start, end = week_bounds(datetime(2026, 10, 17, 12, 0, tzinfo=BEIJING))
        self.assertEqual(start.isoformat(), '2026-10-05T00:00:00+08:00')
        self.assertEqual(end.isoformat(), '2026-10-12T00:00:00+08:00')

    def test_collector_aggregates_model_day_and_source_amount(self):
        start = datetime(2026, 10, 5, tzinfo=BEIJING)
        end = start + timedelta(days=7)
        event = {'timestamp': str(int(datetime(2026, 10, 6, 3, tzinfo=BEIJING).timestamp() * 1000)),
            'model': 'cursor-model', 'tokenUsage': {'inputTokens': 100, 'outputTokens': 20, 'cacheReadTokens': 30, 'cacheWriteTokens': 5},
            'chargedCents': 12.8408}
        calls = []
        def sender(url, headers, body=None):
            calls.append((url, body))
            if '/auth/' in url:
                return {'accessToken': 'temporary-access-token'}
            return {'usageEventsDisplay': [event] if body['page'] == 1 else []}
        account = {'email': 'a@example.com', 'key': 'must-not-leak', 'shared': False}
        result = collect_account(account, start, end, sender)
        self.assertTrue(result['ok'])
        self.assertEqual(result['total_tokens'], 155)
        self.assertEqual(result['usage_records'], 1)
        self.assertEqual(result['charged_cents'], 12.8408)
        self.assertEqual(result['models'], [{'model': 'cursor-model', 'total_tokens': 155, 'usage_records': 1}])
        self.assertEqual(result['by_day'][0]['day'], '2026-10-06')
        self.assertTrue(all('must-not-leak' not in json.dumps(call) for call in calls))

    def test_collector_preserves_charged_records_without_model_or_tokens(self):
        start = datetime(2026, 10, 5, tzinfo=BEIJING)
        end = start + timedelta(days=7)
        event = {'timestamp': int(datetime(2026, 10, 6, 3, tzinfo=BEIJING).timestamp() * 1000),
            'tokenUsage': {}, 'chargedCents': 4}
        def sender(url, headers, body=None):
            if '/auth/' in url:
                return {'accessToken': 'temporary-access-token'}
            return {'usageEventsDisplay': [event] if body['page'] == 1 else []}
        result = collect_account({'email': 'a@example.com', 'key': 'secret', 'shared': False}, start, end, sender)
        self.assertTrue(result['ok'])
        self.assertEqual(result['usage_records'], 1)
        self.assertEqual(result['total_tokens'], 0)
        self.assertEqual(result['charged_cents'], 4)
        self.assertEqual(result['models'][0]['model'], 'unknown')

    def test_same_week_replaces_snapshot_and_different_accounts_accumulate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'usage.sqlite3', 'test-admin-key-with-at-least-24-chars')
            start = datetime(2026, 10, 5, tzinfo=BEIJING)
            end = start + timedelta(days=7)
            payload = self.payload(start, end, [self.account('a@example.com', 100)])
            first = store.ingest_cursor_snapshot(payload)
            second = store.ingest_cursor_snapshot(payload)
            self.assertEqual(first['replaced_accounts'], 1)
            self.assertTrue(second['repeated'])
            summary = store.cursor_summary(start, end)
            self.assertEqual(summary['totals']['total_tokens'], 100)

            rerun = self.payload(start, end, [self.account('a@example.com', 120), self.account('b@example.com', 60)], datetime(2026, 10, 18, tzinfo=BEIJING))
            store.ingest_cursor_snapshot(rerun)
            summary = store.cursor_summary(start, end)
            self.assertEqual(summary['totals']['total_tokens'], 180)
            self.assertEqual(len(summary['by_user']), 2)
            self.assertEqual(summary['by_model'][0]['total_tokens'], 180)

            failed_run = self.payload(start, end, [self.failure('a@example.com')], datetime(2026, 10, 19, tzinfo=BEIJING))
            store.ingest_cursor_snapshot(failed_run)
            summary = store.cursor_summary(start, end)
            self.assertEqual(summary['totals']['total_tokens'], 180)
            self.assertFalse(summary['quality']['is_complete'])
            self.assertEqual(summary['quality']['failed_count'], 1)
            self.assertEqual({row['email'] for row in summary['by_user']}, {'a@example.com', 'b@example.com'})
            self.assertEqual(next(row for row in summary['by_user'] if row['email'] == 'b@example.com')['totals']['total_tokens'], 60)

    def test_same_capture_time_late_complete_run_cannot_change_selected_week(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'usage.sqlite3', 'test-admin-key-with-at-least-24-chars')
            start = datetime(2026, 10, 5, tzinfo=BEIJING)
            end = start + timedelta(days=7)
            captured = datetime(2026, 10, 20, tzinfo=BEIJING)
            first = self.payload(start, end, [self.account('a@example.com', 100), self.account('b@example.com', 50)], captured)
            store.ingest_cursor_snapshot(first)
            same_time_late = self.payload(start, end, [self.account('a@example.com', 999)], captured)
            ack = store.ingest_cursor_snapshot(same_time_late)
            self.assertTrue(ack['stale'])
            summary = store.cursor_summary(start, end)
            self.assertEqual(summary['totals']['total_tokens'], 150)
            self.assertEqual({row['email'] for row in summary['by_user']}, {'a@example.com', 'b@example.com'})

    def test_first_failed_account_is_listed_without_fake_zero_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'usage.sqlite3', 'test-admin-key-with-at-least-24-chars')
            start = datetime(2026, 10, 5, tzinfo=BEIJING)
            end = start + timedelta(days=7)
            run = self.payload(start, end, [self.failure('failed@example.com')])
            store.ingest_cursor_snapshot(run)
            summary = store.cursor_summary(start, end)
            row = summary['by_user'][0]
            self.assertEqual(row['email'], 'failed@example.com')
            self.assertFalse(row['available'])
            self.assertEqual(row['error_code'], 'auth_failed')
            self.assertIsNone(row['totals']['total_tokens'])

    def test_existing_cursor_schema_adds_and_backfills_capture_watermarks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'usage.sqlite3'
            conn = sqlite3.connect(path)
            conn.executescript('''
                CREATE TABLE cursor_runs (run_id TEXT PRIMARY KEY, period_start REAL NOT NULL, period_end REAL NOT NULL,
                    captured_at TEXT NOT NULL, payload_hash TEXT NOT NULL, pagination_complete INTEGER NOT NULL,
                    expected_count INTEGER NOT NULL, success_count INTEGER NOT NULL, failed_count INTEGER NOT NULL, complete INTEGER NOT NULL);
                CREATE TABLE cursor_run_accounts (run_id TEXT NOT NULL, email TEXT NOT NULL, ok INTEGER NOT NULL,
                    shared INTEGER NOT NULL, error_code TEXT, PRIMARY KEY(run_id,email));
                CREATE TABLE cursor_weekly_accounts (period_start REAL NOT NULL, period_end REAL NOT NULL, email TEXT NOT NULL,
                    shared INTEGER NOT NULL, total_tokens INTEGER NOT NULL, usage_records INTEGER NOT NULL, charged_cents INTEGER,
                    charge_records INTEGER NOT NULL, unpriced_records INTEGER NOT NULL, models TEXT NOT NULL, by_day TEXT NOT NULL,
                    last_success_at TEXT NOT NULL, source_run_id TEXT NOT NULL, PRIMARY KEY(period_start,period_end,email));
            ''')
            start = datetime(2026, 10, 5, tzinfo=BEIJING)
            end = start + timedelta(days=7)
            captured = datetime(2026, 10, 17, 12, tzinfo=BEIJING)
            conn.execute('INSERT INTO cursor_runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('old-run', start.timestamp(), end.timestamp(), captured.isoformat(), 'hash', 1, 1, 1, 0, 1))
            conn.execute('INSERT INTO cursor_weekly_accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (start.timestamp(), end.timestamp(), 'a@example.com', 0, 10, 1, None, 0, 1, '[]', '[]', captured.isoformat(), 'old-run'))
            conn.commit()
            conn.close()

            Store(path, 'test-admin-key-with-at-least-24-chars')
            conn = sqlite3.connect(path)
            run_epoch, applied = conn.execute('SELECT captured_epoch,applied FROM cursor_runs WHERE run_id=?', ('old-run',)).fetchone()
            account_epoch = conn.execute('SELECT last_success_epoch FROM cursor_weekly_accounts WHERE email=?', ('a@example.com',)).fetchone()[0]
            conn.close()
            self.assertAlmostEqual(run_epoch, captured.timestamp())
            self.assertAlmostEqual(account_epoch, captured.timestamp())
            self.assertEqual(applied, 1)

    def test_older_complete_retry_cannot_overwrite_or_resurrect_retired_account(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'usage.sqlite3', 'test-admin-key-with-at-least-24-chars')
            start = datetime(2026, 10, 5, tzinfo=BEIJING)
            end = start + timedelta(days=7)
            original = self.payload(start, end, [self.account('a@example.com', 100), self.account('b@example.com', 50)], datetime(2026, 10, 17, tzinfo=BEIJING))
            store.ingest_cursor_snapshot(original)
            current = self.payload(start, end, [self.account('a@example.com', 200)], datetime(2026, 10, 18, tzinfo=BEIJING))
            store.ingest_cursor_snapshot(current)

            delayed = self.payload(start, end, [self.account('a@example.com', 110), self.account('b@example.com', 55)], datetime(2026, 10, 17, 12, tzinfo=BEIJING))
            ack = store.ingest_cursor_snapshot(delayed)
            self.assertTrue(ack['stale'])
            self.assertFalse(ack['applied'])
            self.assertEqual(ack['replaced_accounts'], 0)
            summary = store.cursor_summary(start, end)
            self.assertEqual(summary['totals']['total_tokens'], 200)
            self.assertEqual([row['email'] for row in summary['by_user']], ['a@example.com'])
            retry_ack = store.ingest_cursor_snapshot(delayed)
            self.assertTrue(retry_ack['repeated'])

    def test_cost_coverage_must_account_for_each_usage_record(self):
        start = datetime(2026, 10, 5, tzinfo=BEIJING)
        end = start + timedelta(days=7)
        account = self.account('a@example.com', 10)
        account['unpriced_records'] = 0
        with self.assertRaises(Invalid):
            validate_cursor_snapshot(self.payload(start, end, [account]))

    def test_source_charge_rejects_precision_below_one_ten_thousandth_cent(self):
        start = datetime(2026, 10, 5, tzinfo=BEIJING)
        end = start + timedelta(days=7)
        account = self.account_cost('a@example.com', 100)
        account['charged_cents'] = 12.84081
        with self.assertRaises(Invalid):
            validate_cursor_snapshot(self.payload(start, end, [account]))

    def test_cursor_amount_output_rejects_values_above_exact_browser_range(self):
        self.assertEqual(cursor_cents_number(Decimal(2 ** 36)), 2 ** 36)
        with self.assertRaises(ValueError):
            cursor_cents_number(Decimal('1000000000000.0003'))
        self.assertEqual(cursor_cents_output(Decimal('1000000000000.0003')), '1000000000000.0003')

    def test_cursor_snapshot_rejects_aggregate_amount_above_exact_browser_range(self):
        start = datetime(2026, 10, 5, tzinfo=BEIJING)
        end = start + timedelta(days=7)
        per_account = float(Decimal(2 ** 35) + Decimal('0.0001'))
        self.assertEqual(Decimal(str(per_account)), Decimal(2 ** 35) + Decimal('0.0001'))
        first = self.account_cost('a@example.com', 100)
        second = self.account_cost('b@example.com', 100)
        first['charged_cents'] = per_account
        second['charged_cents'] = per_account
        with self.assertRaises(Invalid):
            validate_cursor_snapshot(self.payload(start, end, [first, second]))

    def test_cursor_summary_preserves_exact_amount_across_multiple_weeks(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'usage.sqlite3', 'test-admin-key-with-at-least-24-chars')
            start = datetime(2026, 10, 5, tzinfo=BEIJING)
            last_amount = float(Decimal(2 ** 36) - Decimal('0.0001'))
            for index in range(16):
                week_start = start + timedelta(days=7 * index)
                week_end = week_start + timedelta(days=7)
                account = self.account_cost('a@example.com', 100)
                account['charged_cents'] = last_amount if index == 15 else 2 ** 36
                account['charge_records'] = 2
                account['unpriced_records'] = 0
                account['by_day'][0]['day'] = (week_start + timedelta(days=1)).date().isoformat()
                captured = week_end + timedelta(hours=1)
                store.ingest_cursor_snapshot(self.payload(week_start, week_end, [account], captured))

            summary = store.cursor_summary(start, start + timedelta(days=112))
            expected = Decimal(2 ** 36) * 16 - Decimal('0.0001')
            self.assertEqual(summary['totals']['total_tokens'], 1600)
            self.assertEqual(summary['totals']['charged_cents'], format(expected, 'f'))

    def test_partial_snapshot_retained_values_keep_summary_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'usage.sqlite3', 'test-admin-key-with-at-least-24-chars')
            start = datetime(2026, 10, 5, tzinfo=BEIJING)
            end = start + timedelta(days=7)
            first = self.account_cost('a@example.com', 100)
            second = self.account_cost('b@example.com', 100)
            first['charged_cents'] = 0.0001
            second['charged_cents'] = float(Decimal(2 ** 36) - Decimal('0.0001'))
            first['charge_records'] = second['charge_records'] = 2
            first['unpriced_records'] = second['unpriced_records'] = 0
            store.ingest_cursor_snapshot(self.payload(start, end, [first, second]))

            updated = self.account_cost('a@example.com', 200)
            updated['charged_cents'] = float(Decimal(2 ** 36) - Decimal('0.0001'))
            updated['charge_records'] = 2
            updated['unpriced_records'] = 0
            store.ingest_cursor_snapshot(self.payload(start, end, [updated, self.failure('b@example.com')],
                datetime(2026, 10, 20, tzinfo=BEIJING)))

            summary = store.cursor_summary(start, end)
            self.assertEqual(summary['totals']['total_tokens'], 300)
            self.assertEqual(summary['totals']['charge_subtotal_cents'], '137438953471.9998')
            self.assertFalse(summary['totals']['charge_complete'])

    def test_cursor_summary_keeps_per_user_priced_subtotal_when_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'usage.sqlite3', 'test-admin-key-with-at-least-24-chars')
            start = datetime(2026, 10, 5, tzinfo=BEIJING)
            end = start + timedelta(days=7)
            store.ingest_cursor_snapshot(self.payload(start, end, [self.account_cost('a@example.com', 100)]))
            user = store.cursor_summary(start, end)['by_user'][0]
            self.assertIsNone(user['totals']['charged_cents'])
            self.assertEqual(user['totals']['charge_subtotal_cents'], 12.8408)
            self.assertEqual(store.cursor_summary(start, end)['totals']['charge_subtotal_cents'], 12.8408)
            self.assertFalse(user['totals']['charge_complete'])

    def test_bearer_requests_do_not_follow_redirects(self):
        handler = NoRedirectHandler()
        request = urllib.request.Request('https://example.test')
        result = handler.redirect_request(request, None, 302, 'Found', {}, 'https://other.test')
        self.assertIsNone(result)

    def test_admin_upload_endpoint_must_be_loopback_only(self):
        allowed = 'http://127.0.0.1:18765/api/cursor/snapshots'
        self.assertEqual(validate_local_endpoint(allowed), allowed)
        self.assertEqual(validate_local_endpoint('http://[::1]:18765/api/cursor/snapshots'), 'http://[::1]:18765/api/cursor/snapshots')
        for endpoint in ('https://example.test/api/cursor/snapshots', 'http://evil.test:18765/api/cursor/snapshots',
                         'http://127.0.0.1:18765/other', 'http://user:pass@127.0.0.1:18765/api/cursor/snapshots',
                         'http://127.0.0.1:18765/api/cursor/snapshots?to=evil'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_local_endpoint(endpoint)

    @staticmethod
    def account(email, tokens):
        return {'email': email, 'ok': True, 'shared': False, 'usage_records': 1, 'total_tokens': tokens,
            'charged_cents': None, 'charge_records': 0, 'unpriced_records': 1,
            'models': [{'model': 'cursor-model', 'total_tokens': tokens, 'usage_records': 1}],
            'by_day': [{'day': '2026-10-06', 'total_tokens': tokens, 'usage_records': 1}]}

    @staticmethod
    def failure(email):
        return {'email': email, 'ok': False, 'shared': False, 'error_code': 'auth_failed'}

    @staticmethod
    def account_cost(email, tokens):
        return {'email': email, 'ok': True, 'shared': False, 'usage_records': 2, 'total_tokens': tokens,
            'charged_cents': 12.8408, 'charge_records': 1, 'unpriced_records': 1,
            'models': [{'model': 'cursor-model', 'total_tokens': tokens, 'usage_records': 2}],
            'by_day': [{'day': '2026-10-06', 'total_tokens': tokens, 'usage_records': 2}]}

    @classmethod
    def payload(cls, start, end, accounts, captured_at=None):
        return {'schema_version': 1, 'kind': 'cursor_weekly_snapshot', 'run_id': str(uuid.uuid4()),
            'period_start': start.isoformat(), 'period_end': end.isoformat(), 'captured_at': (captured_at or datetime(2026, 10, 17, tzinfo=BEIJING)).isoformat(),
            'pagination_complete': True, 'accounts': accounts}


if __name__ == '__main__':
    unittest.main()
