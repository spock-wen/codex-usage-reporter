"""Private local Codex event ingestion and reporting; Python standard library only."""
from __future__ import annotations
import argparse
from collections import defaultdict
from contextlib import contextmanager
import csv
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import uuid
from urllib.parse import parse_qs, urlsplit
from collector import BEIJING, TOKEN_FIELDS, estimate_cost_parts, load_prices, parse_time, usage_values

ROOT = Path(__file__).resolve().parent
MAX_BODY = 16 * 1024 * 1024
MAX_ENROLL_BODY = 8 * 1024
MAX_EVENTS = 50_000
HASH = re.compile(r'^[0-9a-f]{64}$')
DEVICE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
MODEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$')
EVENT_FIELDS = {'event_id', 'timestamp', 'model', 'total_tokens', *TOKEN_FIELDS}
COUNT_FIELDS = ('requests', *TOKEN_FIELDS, 'total_tokens')
# Cursor may return six decimal places in a cent-denominated field. Keep input
# values within a range where every one-millionth-cent step is distinguishable
# in a JavaScript number used by the dashboard.
MAX_CURSOR_CENTS = Decimal(2 ** 32)
CURSOR_CENTS_QUANTUM = Decimal('0.000001')

class Invalid(ValueError):
    pass
class AlreadyExists(ValueError):
    pass
class EnrollmentConflict(ValueError):
    pass
class CursorConflict(ValueError):
    pass

def now_iso():
    return datetime.now(BEIJING).isoformat()
def token_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()
def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
def normalize_email(value):
    if not isinstance(value, str):
        raise Invalid('invalid email')
    email = value.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", email):
        raise Invalid('invalid email')
    local, domain = email.rsplit('@', 1)
    if len(local) > 64 or local.startswith('.') or local.endswith('.') or '..' in local or '..' in domain:
        raise Invalid('invalid email')
    return email

def json_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Invalid('duplicate JSON key')
        result[key] = value
    return result

def date_or_time(value):
    if not isinstance(value, str):
        raise Invalid('invalid time')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=BEIJING)
    return parse_time(value)

def query_period(params):
    start, end = params.get('start', [''])[0], params.get('end', [''])[0]
    if bool(start) != bool(end):
        raise Invalid('both start and end required')
    if start:
        lo, hi = date_or_time(start), date_or_time(end)
    else:
        week = params.get('week', [''])[0]
        if week:
            lo = date_or_time(week)
            if lo.weekday() or any((lo.hour, lo.minute, lo.second, lo.microsecond)):
                raise Invalid('week must be Monday midnight')
        else:
            current = datetime.now(BEIJING)
            # The default report is the last completed Beijing natural week.
            lo = current.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=current.weekday() + 7)
        hi = lo + timedelta(days=7)
    if lo >= hi or hi - lo > timedelta(days=3660):
        raise Invalid('invalid period')
    return lo, hi

def validate_package(package):
    if not isinstance(package, dict) or type(package.get('schema_version')) is not int or package['schema_version'] != 1 or package.get('kind') != 'codex_usage_events':
        raise Invalid('unsupported event package')
    start, end = package.get('period_start'), package.get('period_end')
    lo, hi = parse_time(start), parse_time(end)
    if lo >= hi or hi - lo > timedelta(days=3660):
        raise Invalid('invalid package period')
    events = package.get('events')
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise Invalid('invalid event count')
    complete, status, reasons = package.get('is_complete'), package.get('data_status'), package.get('incomplete_reasons')
    if type(complete) is not bool or status not in ('incomplete', 'observed_local_usage') or (status == 'incomplete') == complete:
        raise Invalid('invalid completeness status')
    if not isinstance(reasons, list) or len(reasons) > 100 or any(not isinstance(r, str) or not re.fullmatch(r'[a-z0-9_]{1,100}', r) for r in reasons) or (complete and reasons):
        raise Invalid('invalid completeness reasons')
    diagnostics = package.get('quality')
    if not isinstance(diagnostics, dict) or len(diagnostics) > 100 or any(not isinstance(k, str) or not re.fullmatch(r'[a-z0-9_]{1,100}', k) or type(v) is not int or not 0 <= v <= 10**15 for k, v in diagnostics.items()):
        raise Invalid('invalid quality diagnostics')
    warnings = package.get('coverage_warnings', [])
    if not isinstance(warnings, list) or len(warnings) > 100 or any(not isinstance(w, str) or len(w) > 2000 for w in warnings) or not isinstance(package.get('metadata'), dict):
        raise Invalid('invalid metadata')
    seen, normalized = set(), []
    for event in events:
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise Invalid('invalid event fields')
        event_id = event.get('event_id')
        if not isinstance(event_id, str) or not HASH.fullmatch(event_id) or event_id in seen:
            raise Invalid('invalid or duplicate event ID')
        seen.add(event_id)
        stamp = parse_time(event.get('timestamp'))
        if not lo <= stamp < hi:
            raise Invalid('event outside package period')
        model = event.get('model')
        if not isinstance(model, str) or not MODEL.fullmatch(model):
            raise Invalid('invalid model')
        tokens = usage_values(event)
        if tokens is None or any(t > 10**15 for t in tokens):
            raise Invalid('invalid tokens')
        normalized.append({'event_id': event_id, 'timestamp': stamp.isoformat(), 'model': model,
                           **dict(zip(TOKEN_FIELDS, tokens)), 'total_tokens': tokens[0] + tokens[3]})
    if not events and complete:
        complete, status, reasons = False, 'incomplete', [*reasons, 'empty_observed_events']
    stable = {'schema_version': 1, 'period_start': start, 'period_end': end, 'events': sorted(events, key=lambda e: e['event_id'])}
    expected, supplied = token_hash(canonical_json(stable)), package.get('batch_id')
    if not isinstance(supplied, str) or not HASH.fullmatch(supplied) or not hmac.compare_digest(expected, supplied):
        raise Invalid('batch hash mismatch')
    return {'batch_id': supplied, 'period_start': lo.isoformat(), 'period_end': hi.isoformat(), 'events': normalized,
            'is_complete': complete, 'data_status': status, 'incomplete_reasons': reasons, 'quality': diagnostics}

def validate_cursor_snapshot(payload):
    """Validate a complete weekly aggregate from the centrally managed Cursor collector.

    Cursor's current usage-event feed has no stable request id. We therefore accept
    only full per-account week snapshots and replace that account/week on re-run.
    """
    required = {'schema_version', 'kind', 'run_id', 'period_start', 'period_end', 'captured_at', 'pagination_complete', 'accounts'}
    if not isinstance(payload, dict) or set(payload) != required or payload.get('schema_version') != 1 or payload.get('kind') != 'cursor_weekly_snapshot':
        raise Invalid('invalid Cursor snapshot')
    try:
        run_id = str(uuid.UUID(payload['run_id']))
    except (ValueError, TypeError, AttributeError):
        raise Invalid('invalid Cursor run id') from None
    lo, hi = parse_time(payload['period_start']), parse_time(payload['period_end'])
    captured = parse_time(payload['captured_at'])
    if lo.weekday() != 0 or any((lo.hour, lo.minute, lo.second, lo.microsecond)) or hi - lo != timedelta(days=7):
        raise Invalid('Cursor snapshot must cover one Beijing natural week')
    if lo.utcoffset() != BEIJING.utcoffset(lo) or hi.utcoffset() != BEIJING.utcoffset(hi) or captured.utcoffset() != BEIJING.utcoffset(captured):
        raise Invalid('Cursor snapshot timestamps must use Beijing time')
    if type(payload['pagination_complete']) is not bool or not isinstance(payload['accounts'], list) or not 1 <= len(payload['accounts']) <= 10000:
        raise Invalid('invalid Cursor snapshot accounts')
    if captured < hi:
        raise Invalid('Cursor snapshot must be collected after the reporting week')
    accounts, emails = [], set()
    for account in payload['accounts']:
        if not isinstance(account, dict) or 'email' not in account or type(account.get('ok')) is not bool or type(account.get('shared')) is not bool:
            raise Invalid('invalid Cursor account')
        email = normalize_email(account['email'])
        if email in emails:
            raise Invalid('duplicate Cursor account')
        emails.add(email)
        metadata = {}
        for key in ('name', 'department'):
            if key not in account or account[key] is None:
                continue
            value = account[key]
            if (not isinstance(value, str) or len(value) > 200 or
                    any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value)):
                raise Invalid('invalid Cursor account metadata')
            value = value.strip()
            if value:
                metadata[key] = value
        if not account['ok']:
            if not {'email', 'ok', 'shared', 'error_code'} <= set(account) or set(account) - {'email', 'ok', 'shared', 'error_code', 'name', 'department'} or not isinstance(account['error_code'], str) or not re.fullmatch(r'[a-z_]{1,60}', account['error_code']):
                raise Invalid('invalid Cursor account failure')
            accounts.append({'email': email, 'ok': False, 'shared': account['shared'], 'error_code': account['error_code'], **metadata})
            continue
        fields = {'email', 'ok', 'shared', 'usage_records', 'total_tokens', 'charged_cents', 'charge_records', 'unpriced_records', 'models', 'by_day'}
        if not fields <= set(account) or set(account) - fields - {'name', 'department'}:
            raise Invalid('invalid Cursor account fields')
        counts = {}
        for key in ('usage_records', 'total_tokens', 'charge_records', 'unpriced_records'):
            value = account[key]
            if type(value) is not int or not 0 <= value <= 10**15:
                raise Invalid('invalid Cursor account count')
            counts[key] = value
        charged = account['charged_cents']
        if charged is not None:
            if type(charged) not in (int, float):
                raise Invalid('invalid Cursor charged amount')
            amount = Decimal(str(charged))
            if not amount.is_finite() or amount < 0 or amount > MAX_CURSOR_CENTS or (amount * 1000000) != (amount * 1000000).to_integral_value():
                raise Invalid('invalid Cursor charged amount')
            try:
                charged = cursor_cents_number(amount)
            except (ValueError, OverflowError, ArithmeticError):
                raise Invalid('invalid Cursor charged amount') from None
        if not isinstance(account['models'], list) or len(account['models']) > 500 or not isinstance(account['by_day'], list) or len(account['by_day']) > 7:
            raise Invalid('invalid Cursor breakdown')
        models, model_names = [], set()
        for model in account['models']:
            if not isinstance(model, dict) or set(model) != {'model', 'total_tokens', 'usage_records'}:
                raise Invalid('invalid Cursor model row')
            name = model['model']
            if not isinstance(name, str) or not MODEL.fullmatch(name) or name in model_names:
                raise Invalid('invalid Cursor model')
            model_names.add(name)
            if type(model['total_tokens']) is not int or not 0 <= model['total_tokens'] <= 10**15 or type(model['usage_records']) is not int or not 0 <= model['usage_records'] <= 10**15:
                raise Invalid('invalid Cursor model totals')
            models.append({'model': name, 'total_tokens': model['total_tokens'], 'usage_records': model['usage_records']})
        days, day_names = [], set()
        for day in account['by_day']:
            if not isinstance(day, dict) or set(day) != {'day', 'total_tokens', 'usage_records'}:
                raise Invalid('invalid Cursor day row')
            date = day['day']
            try:
                day_start = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=BEIJING)
            except (ValueError, TypeError):
                raise Invalid('invalid Cursor day') from None
            if date in day_names or not lo <= day_start < hi:
                raise Invalid('Cursor day outside week')
            day_names.add(date)
            if type(day['total_tokens']) is not int or not 0 <= day['total_tokens'] <= 10**15 or type(day['usage_records']) is not int or not 0 <= day['usage_records'] <= 10**15:
                raise Invalid('invalid Cursor day totals')
            days.append({'day': date, 'total_tokens': day['total_tokens'], 'usage_records': day['usage_records']})
        if sum(m['total_tokens'] for m in models) != counts['total_tokens'] or sum(m['usage_records'] for m in models) != counts['usage_records']:
            raise Invalid('Cursor model totals do not reconcile')
        if sum(d['total_tokens'] for d in days) != counts['total_tokens'] or sum(d['usage_records'] for d in days) != counts['usage_records']:
            raise Invalid('Cursor daily totals do not reconcile')
        if (charged is None) != (counts['charge_records'] == 0):
            raise Invalid('Cursor charge totals do not reconcile')
        if counts['charge_records'] + counts['unpriced_records'] != counts['usage_records']:
            raise Invalid('Cursor charge coverage does not reconcile')
        accounts.append({'email': email, 'ok': True, 'shared': account['shared'], **metadata, **counts,
                         'charged_cents': charged, 'models': models, 'by_day': sorted(days, key=lambda row: row['day'])})
    snapshot_charge_total = sum((Decimal(str(row['charged_cents'])) for row in accounts
                                 if row['ok'] and row['charged_cents'] is not None), Decimal(0))
    if snapshot_charge_total > MAX_CURSOR_CENTS:
        raise Invalid('Cursor snapshot charged total exceeds safe precision range')
    complete = payload['pagination_complete'] and all(a['ok'] for a in accounts)
    normalized = {'schema_version': 1, 'kind': 'cursor_weekly_snapshot', 'run_id': run_id,
        'period_start': lo.isoformat(), 'period_end': hi.isoformat(), 'captured_at': captured.isoformat(),
        'pagination_complete': payload['pagination_complete'], 'accounts': accounts}
    return normalized, complete


def cursor_cents_number(value):
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if (not amount.is_finite() or amount < 0 or amount > MAX_CURSOR_CENTS or
            (amount * 1000000) != (amount * 1000000).to_integral_value()):
        raise ValueError('Cursor amount outside supported precision range')
    if amount == amount.to_integral_value():
        return int(amount)
    number = float(amount.quantize(CURSOR_CENTS_QUANTUM))
    if Decimal(str(number)) != amount:
        raise ValueError('Cursor amount cannot round-trip at 0.000001-cent precision')
    return number


def cursor_cents_output(value):
    amount = value if isinstance(value, Decimal) else Decimal(str(value))
    if (not amount.is_finite() or amount < 0 or
            (amount * 1000000) != (amount * 1000000).to_integral_value()):
        raise ValueError('invalid Cursor amount for summary')
    if amount <= MAX_CURSOR_CENTS:
        return cursor_cents_number(amount)
    if amount == amount.to_integral_value():
        integer = int(amount)
        return integer if integer <= 2 ** 53 - 1 else str(integer)
    # A wide aggregate may no longer fit a JSON/browser float at 0.000001-cent
    # precision. Keep the exact decimal text so a charge never breaks token data.
    return format(amount.quantize(CURSOR_CENTS_QUANTUM), 'f')


class UsageTotal:
    def __init__(self):
        self.counts = dict.fromkeys(COUNT_FIELDS, 0)
        self.parts = defaultdict(Decimal)
        self.unpriced = 0
    def add(self, event, prices):
        for key in TOKEN_FIELDS:
            self.counts[key] += event[key]
        self.counts['requests'] += 1
        self.counts['total_tokens'] += event['total_tokens']
        parts, _ = estimate_cost_parts(tuple(event[k] for k in TOKEN_FIELDS), event['model'], prices)
        if parts is None:
            self.unpriced += 1
        else:
            for key, value in parts.items():
                self.parts[key] += value
    def output(self, incomplete=False):
        cost = round(float(sum(self.parts.values(), Decimal(0))), 8)
        return {**self.counts, 'estimated_cost_usd': None if self.unpriced or incomplete else cost,
                'priced_cost_usd': cost, 'unpriced_requests': self.unpriced,
                'priced_cost_breakdown_usd': {k: round(float(self.parts[k]), 8) for k in ('input', 'cached_input', 'cache_write', 'output')},
                'is_complete': not incomplete}

class Store:
    def __init__(self, db, admin_key, prices_path=None):
        if not isinstance(admin_key, str) or not 24 <= len(admin_key) <= 512:
            raise ValueError('admin key must have 24..512 characters')
        self.db, self.admin_hash = str(db), token_hash(admin_key)
        # Separate this purpose from admin authentication and request hashing.
        self.enrollment_key = hmac.new(admin_key.encode('utf-8'), b'codex-usage:enrollment:v1', hashlib.sha256).digest()
        self.prices_path = Path(prices_path or ROOT / 'prices.json')
        with self.connect() as conn:
            conn.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS upload_credentials (email TEXT NOT NULL REFERENCES users(email), device_id TEXT NOT NULL,
              registration_key_hash TEXT UNIQUE NOT NULL, token_hash TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS devices (email TEXT NOT NULL REFERENCES users(email), device_id TEXT NOT NULL, last_seen TEXT NOT NULL, received_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(email,device_id));
            CREATE TABLE IF NOT EXISTS usage_events (event_id TEXT PRIMARY KEY, email TEXT NOT NULL REFERENCES users(email), stamp REAL NOT NULL, canonical TEXT NOT NULL, quarantined INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS usage_events_period ON usage_events(stamp,email);
            CREATE TABLE IF NOT EXISTS event_seen (event_id TEXT NOT NULL REFERENCES usage_events(event_id), email TEXT NOT NULL, device_id TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY(event_id,email,device_id));
            CREATE TABLE IF NOT EXISTS conflicts (event_id TEXT PRIMARY KEY REFERENCES usage_events(event_id), ownership INTEGER NOT NULL, content INTEGER NOT NULL, detected_at TEXT NOT NULL, last_detected_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conflict_observations (event_id TEXT NOT NULL REFERENCES usage_events(event_id), email TEXT NOT NULL, stamp REAL NOT NULL, PRIMARY KEY(event_id,email,stamp));
            CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL, email TEXT NOT NULL REFERENCES users(email), device_id TEXT NOT NULL, period_start REAL NOT NULL, period_end REAL NOT NULL, received_at TEXT NOT NULL, received INTEGER NOT NULL, new_count INTEGER NOT NULL, duplicates INTEGER NOT NULL, conflicts INTEGER NOT NULL, rejected INTEGER NOT NULL DEFAULT 0, is_complete INTEGER NOT NULL, data_status TEXT NOT NULL, quality TEXT NOT NULL, incomplete_reasons TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS batches_period ON batches(period_start,period_end,email);
            CREATE TABLE IF NOT EXISTS cursor_runs (run_id TEXT PRIMARY KEY, period_start REAL NOT NULL, period_end REAL NOT NULL, captured_at TEXT NOT NULL, captured_epoch REAL NOT NULL, payload_hash TEXT NOT NULL, pagination_complete INTEGER NOT NULL, expected_count INTEGER NOT NULL, success_count INTEGER NOT NULL, failed_count INTEGER NOT NULL, complete INTEGER NOT NULL, applied INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS cursor_run_accounts (run_id TEXT NOT NULL REFERENCES cursor_runs(run_id), email TEXT NOT NULL, ok INTEGER NOT NULL, shared INTEGER NOT NULL, name TEXT, department TEXT, error_code TEXT, PRIMARY KEY(run_id,email));
            CREATE TABLE IF NOT EXISTS cursor_weekly_accounts (period_start REAL NOT NULL, period_end REAL NOT NULL, email TEXT NOT NULL, shared INTEGER NOT NULL, name TEXT, department TEXT, total_tokens INTEGER NOT NULL, usage_records INTEGER NOT NULL, charged_cents INTEGER, charge_records INTEGER NOT NULL, unpriced_records INTEGER NOT NULL, models TEXT NOT NULL, by_day TEXT NOT NULL, last_success_at TEXT NOT NULL, last_success_epoch REAL NOT NULL, source_run_id TEXT NOT NULL REFERENCES cursor_runs(run_id), PRIMARY KEY(period_start,period_end,email));
            ''')
            # Keep upgrades additive for databases created by an earlier unified
            # dashboard release; timestamps compare numerically across ISO forms.
            for table, column, definition in (
                ('cursor_runs', 'captured_epoch', 'REAL NOT NULL DEFAULT 0'),
                ('cursor_runs', 'applied', 'INTEGER NOT NULL DEFAULT 1'),
                ('cursor_run_accounts', 'name', 'TEXT'),
                ('cursor_run_accounts', 'department', 'TEXT'),
                ('cursor_weekly_accounts', 'name', 'TEXT'),
                ('cursor_weekly_accounts', 'department', 'TEXT'),
                ('cursor_weekly_accounts', 'last_success_epoch', 'REAL NOT NULL DEFAULT 0'),
            ):
                if column not in {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}:
                    conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
            # Backfill rows created before numeric capture epochs were added.
            for row in conn.execute("SELECT run_id,captured_at FROM cursor_runs WHERE captured_epoch=0").fetchall():
                conn.execute('UPDATE cursor_runs SET captured_epoch=? WHERE run_id=?',
                             (parse_time(row['captured_at']).timestamp(), row['run_id']))
            for row in conn.execute("SELECT period_start,period_end,email,last_success_at FROM cursor_weekly_accounts WHERE last_success_epoch=0").fetchall():
                conn.execute('UPDATE cursor_weekly_accounts SET last_success_epoch=? WHERE period_start=? AND period_end=? AND email=?',
                             (parse_time(row['last_success_at']).timestamp(), row['period_start'], row['period_end'], row['email']))
            conn.execute('CREATE INDEX IF NOT EXISTS cursor_runs_period ON cursor_runs(period_start,period_end,applied,captured_epoch)')
            conn.execute('CREATE INDEX IF NOT EXISTS cursor_accounts_period ON cursor_weekly_accounts(period_start,period_end,email)')
    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            with conn:
                yield conn
        finally:
            conn.close()
    def authenticate(self, token):
        if not isinstance(token, str) or len(token) > 512:
            return None, None
        hashed = token_hash(token)
        if hmac.compare_digest(hashed, self.admin_hash):
            return 'admin', None
        with self.connect() as conn:
            row = conn.execute('SELECT email FROM users WHERE token_hash=? AND active=1', (hashed,)).fetchone()
            if row is None:
                row = conn.execute('SELECT u.email FROM upload_credentials c JOIN users u ON u.email=c.email '
                                   'WHERE c.token_hash=? AND u.active=1', (hashed,)).fetchone()
        return ('uploader', row['email']) if row else (None, None)
    def enroll(self, body):
        if not isinstance(body, dict) or set(body) != {'email', 'device_id', 'registration_key'}:
            raise Invalid('invalid enrollment object')
        email, device, registration_key = normalize_email(body['email']), body['device_id'], body['registration_key']
        if not isinstance(device, str) or not DEVICE.fullmatch(device):
            raise Invalid('invalid device_id')
        if not isinstance(registration_key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', registration_key):
            raise Invalid('invalid registration key')
        key_hash = token_hash('codex-usage:registration-key:v1:' + registration_key)
        message = canonical_json({'email': email, 'device_id': device, 'registration_key': registration_key})
        upload_token = 'cu_' + hmac.new(self.enrollment_key, message.encode('utf-8'), hashlib.sha256).hexdigest()
        hashed = token_hash(upload_token)
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT email,device_id,token_hash FROM upload_credentials WHERE registration_key_hash=?', (key_hash,)).fetchone()
            if existing and (existing['email'] != email or existing['device_id'] != device):
                raise EnrollmentConflict('registration key already bound')
            user = conn.execute('SELECT active FROM users WHERE email=?', (email,)).fetchone()
            if user is not None and not user['active']:
                raise PermissionError('disabled mailbox')
            if existing:
                if not hmac.compare_digest(existing['token_hash'], hashed):
                    # A changed persistent admin secret cannot reproduce the old
                    # one-way credential; do not silently rotate an active key.
                    raise EnrollmentConflict('enrollment key changed')
                repeated = True
            else:
                if user is None:
                    # Legacy column remains compatible, without issuing or
                    # replacing a legacy token for this automatic enrollment.
                    placeholder = token_hash(secrets.token_urlsafe(48))
                    conn.execute('INSERT INTO users(email,token_hash,created_at) VALUES(?,?,?)', (email, placeholder, now_iso()))
                conn.execute('INSERT INTO upload_credentials VALUES(?,?,?,?,?)', (email, device, key_hash, hashed, now_iso()))
                repeated = False
        return {'ok': True, 'email': email, 'device_id': device, 'upload_token': upload_token,
                'identity_assurance': 'self_reported', 'enrollment_repeated': repeated}
    def create_user(self, email):
        email, upload_token = normalize_email(email), secrets.token_urlsafe(32)
        try:
            with self.connect() as conn:
                conn.execute('INSERT INTO users(email,token_hash,created_at) VALUES(?,?,?)', (email, token_hash(upload_token), now_iso()))
        except sqlite3.IntegrityError:
            raise AlreadyExists('user already exists') from None
        return {'email': email, 'upload_token': upload_token}
    def users(self):
        with self.connect() as conn:
            users = [dict(r) for r in conn.execute('SELECT email,active,created_at FROM users ORDER BY email')]
            devices = [dict(r) for r in conn.execute('SELECT email,device_id,last_seen,received_count FROM devices ORDER BY device_id')]
        for row in users:
            row['active'] = bool(row['active'])
            row['identity_assurance'] = 'self_reported'
            row['devices'] = [{k: v for k, v in d.items() if k != 'email'} for d in devices if d['email'] == row['email']]
        return users
    def ingest(self, email, body):
        email = normalize_email(email)
        if not isinstance(body, dict) or set(body) - {'device_id', 'package', 'email'}:
            raise Invalid('invalid upload object')
        if 'email' in body and normalize_email(body['email']) != email:
            raise PermissionError('email does not match uploader token')
        device = body.get('device_id')
        if not isinstance(device, str) or not DEVICE.fullmatch(device):
            raise Invalid('invalid device_id')
        package, received_at = validate_package(body.get('package')), now_iso()
        counts = {'received': len(package['events']), 'new': 0, 'duplicates': 0, 'conflicts': 0, 'rejected': 0}
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM users WHERE email=? AND active=1', (email,)).fetchone():
                raise PermissionError('uploader disabled')
            repeated = bool(conn.execute('SELECT 1 FROM batches WHERE email=? AND batch_id=? LIMIT 1', (email, package['batch_id'])).fetchone())
            for event in package['events']:
                event_id, serialized = event['event_id'], canonical_json(event)
                stamp = parse_time(event['timestamp']).timestamp()
                inserted = conn.execute('INSERT INTO usage_events(event_id,email,stamp,canonical) VALUES(?,?,?,?) ON CONFLICT(event_id) DO NOTHING', (event_id, email, stamp, serialized)).rowcount
                row = conn.execute('SELECT * FROM usage_events WHERE event_id=?', (event_id,)).fetchone()
                ownership, content = row['email'] != email, row['canonical'] != serialized
                if ownership or content or row['quarantined']:
                    counts['conflicts'] += 1
                    conn.execute('UPDATE usage_events SET quarantined=1 WHERE event_id=?', (event_id,))
                    conn.execute('INSERT INTO conflicts VALUES(?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET ownership=MAX(ownership,excluded.ownership),content=MAX(content,excluded.content),last_detected_at=excluded.last_detected_at', (event_id, int(ownership), int(content), received_at, received_at))
                    for observed_email, observed_stamp in ((row['email'], row['stamp']), (email, stamp)):
                        conn.execute('INSERT OR IGNORE INTO conflict_observations VALUES(?,?,?)', (event_id, observed_email, observed_stamp))
                elif inserted:
                    counts['new'] += 1
                else:
                    counts['duplicates'] += 1
                conn.execute('INSERT INTO event_seen VALUES(?,?,?,?,?) ON CONFLICT(event_id,email,device_id) DO UPDATE SET last_seen=excluded.last_seen', (event_id, email, device, received_at, received_at))
            conn.execute('INSERT INTO devices VALUES(?,?,?,?) ON CONFLICT(email,device_id) DO UPDATE SET last_seen=excluded.last_seen,received_count=received_count+excluded.received_count', (email, device, received_at, counts['received']))
            conn.execute('INSERT INTO batches(batch_id,email,device_id,period_start,period_end,received_at,received,new_count,duplicates,conflicts,rejected,is_complete,data_status,quality,incomplete_reasons) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (package['batch_id'], email, device, parse_time(package['period_start']).timestamp(), parse_time(package['period_end']).timestamp(), received_at, counts['received'], counts['new'], counts['duplicates'], counts['conflicts'], 0, int(package['is_complete']), package['data_status'], canonical_json(package['quality']), canonical_json(package['incomplete_reasons'])))
        return {'ok': True, 'batch_id': package['batch_id'], **counts, 'batch_repeated': repeated, 'quarantined': counts['conflicts'], 'is_complete': package['is_complete'] and not counts['conflicts']}
    def summary(self, lo, hi, email=None):
        if email:
            email = normalize_email(email)
        prices, lower, upper = load_prices(self.prices_path), lo.timestamp(), hi.timestamp()
        clause, values = (' AND email=?', [email]) if email else ('', [])
        with self.connect() as conn:
            conn.execute('BEGIN')  # All totals, batches and quarantine flags share one snapshot.
            users = [dict(r) for r in conn.execute('SELECT email,active FROM users' + (' WHERE email=?' if email else '') + ' ORDER BY email', values)]
            events = [dict(r) for r in conn.execute('SELECT * FROM usage_events WHERE stamp>=? AND stamp<? AND quarantined=0' + clause, [lower, upper, *values])]
            devices = [dict(r) for r in conn.execute('SELECT * FROM devices' + (' WHERE email=?' if email else '') + ' ORDER BY device_id', values)]
            batch_rows = [dict(r) for r in conn.execute('SELECT * FROM batches WHERE period_start<? AND period_end>?' + clause + ' ORDER BY id DESC', [upper, lower, *values])]
            conflict_rows = [dict(r) for r in conn.execute('SELECT DISTINCT c.* FROM conflicts c JOIN conflict_observations o ON c.event_id=o.event_id WHERE o.stamp>=? AND o.stamp<?' + (' AND o.email=?' if email else '') + ' ORDER BY c.last_detected_at DESC', [lower, upper, *values])]
            affected_emails = set()
            for conflict in conflict_rows:
                affected_emails.update(r['email'] for r in conn.execute('SELECT DISTINCT email FROM conflict_observations WHERE event_id=?', (conflict['event_id'],)))
        latest = {}
        for row in batch_rows:
            latest.setdefault((row['email'], row['device_id'], row['period_start'], row['period_end']), row)
        # A complete batch for one day cannot establish coverage for a whole week.
        # Merge declared windows per reporting device; do not infer missing activity
        # for known devices that submitted no batch in the selected interval.
        windows = defaultdict(list)
        for row in latest.values():
            windows[(row['email'], row['device_id'])].append((max(lower, row['period_start']), min(upper, row['period_end'])))
        partial_devices = set()
        for device, intervals in windows.items():
            covered_until = lower
            for start, end in sorted(intervals):
                if start > covered_until:
                    break
                covered_until = max(covered_until, end)
            if covered_until < upper:
                partial_devices.add(device)
        incomplete_emails = {r['email'] for r in latest.values() if not r['is_complete']}
        incomplete_emails.update(email for email, _ in partial_devices)
        submitted_emails = {r['email'] for r in batch_rows}
        incomplete_emails.update(u['email'] for u in users if u['active'] and u['email'] not in submitted_emails)
        overall, by_user, by_day, by_model = UsageTotal(), defaultdict(UsageTotal), defaultdict(UsageTotal), defaultdict(UsageTotal)
        for row in events:
            event = json.loads(row['canonical'])
            day = parse_time(event['timestamp']).date().isoformat()
            for target in (overall, by_user[row['email']], by_day[day], by_model[event['model']]):
                target.add(event, prices)
        incomplete = not batch_rows or bool(incomplete_emails or conflict_rows)
        user_rows = []
        for user in users:
            ds = [{**{k: v for k, v in row.items() if k != 'email'}, 'partial_window_coverage': (row['email'], row['device_id']) in partial_devices} for row in devices if row['email'] == user['email']]
            user_rows.append({'email': user['email'], 'active': bool(user['active']), 'totals': by_user[user['email']].output(user['email'] in incomplete_emails or user['email'] in affected_emails), 'devices': ds, 'last_seen': max((d['last_seen'] for d in ds), default=None), 'submitted': any(r['email'] == user['email'] for r in batch_rows), 'incomplete_batches': sum(not r['is_complete'] for r in latest.values() if r['email'] == user['email'])})
        conflicts = [{'event_id': r['event_id'], 'reason': '+'.join(n for n in ('ownership' if r['ownership'] else '', 'content' if r['content'] else '') if n) or 'quarantined', 'detected_at': r['detected_at'], 'last_detected_at': r['last_detected_at'], 'status': 'quarantined_excluded'} for r in conflict_rows[:100]]
        batches = [{'received_at': r['received_at'], 'email': r['email'], 'device_id': r['device_id'], 'received': r['received'], 'new': r['new_count'], 'duplicates': r['duplicates'], 'conflicts': r['conflicts'], 'rejected': r['rejected'], 'is_complete': bool(r['is_complete']), 'data_status': r['data_status'], 'incomplete_reasons': json.loads(r['incomplete_reasons'])} for r in batch_rows[:100]]
        return {'period_start': lo.isoformat(), 'period_end': hi.isoformat(), 'timezone': 'Asia/Shanghai', 'identity_assurance': 'self_reported', 'totals': overall.output(incomplete), 'by_user': user_rows, 'by_day': [{'day': k, **v.output(incomplete)} for k, v in sorted(by_day.items())], 'by_model': [{'model': k, **v.output(incomplete)} for k, v in sorted(by_model.items())], 'conflicts': conflicts, 'batches': batches, 'quality': {'conflicted_events_excluded': len(conflict_rows), 'incomplete_device_periods': sum(not r['is_complete'] for r in latest.values()), 'partial_coverage_devices': len(partial_devices), 'missing_users': [u['email'] for u in user_rows if u['active'] and not u['submitted']], 'is_complete': not incomplete, 'account_coverage': 'available uploaded local events only; a submission does not prove full historical coverage'}, 'pricing': {'currency': prices.get('currency', 'USD'), 'basis': prices.get('basis', 'standard_api_equivalent'), 'verified_at': prices.get('verified_at'), 'source': prices.get('source'), 'sha256': token_hash(json.dumps(prices, sort_keys=True, ensure_ascii=True)), 'notes': ['Server recomputes configured API-equivalent costs; subscription billing and quota are separate.']}}

    def ingest_cursor_snapshot(self, payload):
        snapshot, complete = validate_cursor_snapshot(payload)
        lo, hi = parse_time(snapshot['period_start']).timestamp(), parse_time(snapshot['period_end']).timestamp()
        captured = snapshot['captured_at']
        captured_epoch = parse_time(captured).timestamp()
        digest = token_hash(canonical_json(snapshot))
        good = [row for row in snapshot['accounts'] if row['ok']]
        bad = [row for row in snapshot['accounts'] if not row['ok']]
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT payload_hash,applied FROM cursor_runs WHERE run_id=?', (snapshot['run_id'],)).fetchone()
            if previous:
                if not hmac.compare_digest(previous['payload_hash'], digest):
                    raise CursorConflict('run id reused with different snapshot')
                return {'ok': True, 'run_id': snapshot['run_id'], 'period_start': snapshot['period_start'], 'period_end': snapshot['period_end'], 'expected': len(snapshot['accounts']), 'success': len(good), 'failed': len(bad), 'repeated': True, 'stale': not bool(previous['applied']), 'applied': bool(previous['applied']), 'replaced_accounts': 0, 'complete': complete}
            latest = conn.execute('SELECT captured_epoch FROM cursor_runs WHERE period_start=? AND period_end=? AND applied=1 ORDER BY captured_epoch DESC,rowid DESC LIMIT 1', (lo, hi)).fetchone()
            # A delayed retry is still recorded for audit, but cannot mutate the
            # newest snapshot or bring back accounts retired by a later full run.
            stale = latest is not None and captured_epoch <= latest['captured_epoch']
            conn.execute('''INSERT INTO cursor_runs
                (run_id,period_start,period_end,captured_at,captured_epoch,payload_hash,pagination_complete,expected_count,success_count,failed_count,complete,applied)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (snapshot['run_id'], lo, hi, captured, captured_epoch, digest, int(snapshot['pagination_complete']), len(snapshot['accounts']), len(good), len(bad), int(complete), int(not stale)))
            for account in snapshot['accounts']:
                conn.execute('''INSERT INTO cursor_run_accounts
                    (run_id,email,ok,shared,name,department,error_code) VALUES(?,?,?,?,?,?,?)''',
                    (snapshot['run_id'], account['email'], int(account['ok']), int(account['shared']),
                     account.get('name'), account.get('department'), account.get('error_code')))
            for account in (() if stale else good):
                conn.execute('''INSERT INTO cursor_weekly_accounts
                    (period_start,period_end,email,shared,name,department,total_tokens,usage_records,charged_cents,charge_records,unpriced_records,models,by_day,last_success_at,last_success_epoch,source_run_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(period_start,period_end,email) DO UPDATE SET
                    shared=excluded.shared,name=COALESCE(excluded.name,cursor_weekly_accounts.name),
                    department=COALESCE(excluded.department,cursor_weekly_accounts.department),
                    total_tokens=excluded.total_tokens,usage_records=excluded.usage_records,
                    charged_cents=excluded.charged_cents,charge_records=excluded.charge_records,
                    unpriced_records=excluded.unpriced_records,models=excluded.models,by_day=excluded.by_day,
                    last_success_at=excluded.last_success_at,last_success_epoch=excluded.last_success_epoch,source_run_id=excluded.source_run_id
                    WHERE excluded.last_success_epoch > cursor_weekly_accounts.last_success_epoch''',
                    (lo, hi, account['email'], int(account['shared']), account.get('name'), account.get('department'),
                     account['total_tokens'], account['usage_records'],
                     account['charged_cents'], account['charge_records'], account['unpriced_records'],
                     canonical_json(account['models']), canonical_json(account['by_day']), captured, captured_epoch, snapshot['run_id']))
            # Only an all-account successful run can retire accounts removed from
            # the central account list. Partial runs preserve the prior good value.
            if complete and not stale:
                current = [row['email'] for row in snapshot['accounts']]
                if current:
                    marks = ','.join('?' for _ in current)
                    conn.execute(f'DELETE FROM cursor_weekly_accounts WHERE period_start=? AND period_end=? AND email NOT IN ({marks}) AND last_success_epoch<=?', (lo, hi, *current, captured_epoch))
                else:
                    conn.execute('DELETE FROM cursor_weekly_accounts WHERE period_start=? AND period_end=? AND last_success_epoch<=?', (lo, hi, captured_epoch))
        applied = not stale
        return {'ok': True, 'run_id': snapshot['run_id'], 'period_start': snapshot['period_start'], 'period_end': snapshot['period_end'], 'expected': len(snapshot['accounts']), 'success': len(good), 'failed': len(bad), 'repeated': False, 'stale': stale, 'applied': applied, 'replaced_accounts': len(good) if applied else 0, 'complete': complete}

    def cursor_summary(self, lo, hi):
        midnight = lambda value: value.weekday() == 0 and not any((value.hour, value.minute, value.second, value.microsecond))
        if not midnight(lo) or not midnight(hi) or lo >= hi or (hi - lo).days % 7:
            raise Invalid('Cursor summaries require complete Beijing natural weeks')
        lower, upper = lo.timestamp(), hi.timestamp()
        with self.connect() as conn:
            conn.execute('BEGIN')
            runs = [dict(row) for row in conn.execute('SELECT rowid AS sequence,* FROM cursor_runs WHERE period_start>=? AND period_end<=? AND applied=1 ORDER BY captured_epoch DESC,sequence DESC', (lower, upper))]
            account_rows = [dict(row) for row in conn.execute('SELECT * FROM cursor_weekly_accounts WHERE period_start>=? AND period_end<=? ORDER BY period_start,email', (lower, upper))]
            statuses = [dict(row) for row in conn.execute('''SELECT a.run_id,a.email,a.ok,a.shared,a.name,a.department,a.error_code,r.period_start,r.period_end,r.captured_at,r.captured_epoch
                FROM cursor_run_accounts a JOIN cursor_runs r ON r.run_id=a.run_id
                WHERE r.period_start>=? AND r.period_end<=? AND r.applied=1 ORDER BY r.period_start,r.captured_epoch DESC,r.rowid DESC''', (lower, upper))]
        weeks = int((hi - lo).days / 7)
        newest_by_week = {}
        for run in runs:
            newest_by_week.setdefault(run['period_start'], run)
        run_account_latest = {}
        for row in statuses:
            run = newest_by_week.get(row['period_start'])
            if run and row['run_id'] == run['run_id']:
                run_account_latest[(row['period_start'], row['email'])] = row
        daily = defaultdict(lambda: {'total_tokens': 0, 'usage_records': 0})
        models = defaultdict(lambda: {'total_tokens': 0, 'usage_records': 0})
        people = defaultdict(lambda: {'email': '', 'shared': False, 'total_tokens': 0, 'usage_records': 0,
            'charged_cents': Decimal(0), 'has_charges': False, 'charge_complete': True, 'last_success_at': None,
            'name': None, 'department': None, '_metadata_epoch': -1,
            'models': defaultdict(lambda: {'total_tokens': 0, 'usage_records': 0})})
        def apply_metadata(person, row, epoch):
            if epoch >= person['_metadata_epoch'] and (row.get('name') or row.get('department')):
                person['name'] = row.get('name') or person['name']
                person['department'] = row.get('department') or person['department']
                person['_metadata_epoch'] = epoch
        charge_subtotal, charge_missing, total_tokens, total_records = Decimal(0), 0, 0, 0
        user_status = {}
        for row in account_rows:
            person = people[row['email']]
            person['email'] = row['email']
            person['_has_snapshot'] = True
            person['shared'] = person['shared'] or bool(row['shared'])
            apply_metadata(person, row, row['last_success_epoch'])
            person['total_tokens'] += row['total_tokens']
            person['usage_records'] += row['usage_records']
            person['last_success_at'] = max(person['last_success_at'] or '', row['last_success_at']) or None
            if row['charged_cents'] is not None:
                amount = Decimal(str(row['charged_cents']))
                person['charged_cents'] += amount
                person['has_charges'] = True
                charge_subtotal += amount
            if row['unpriced_records']:
                person['charge_complete'] = False
                charge_missing += row['unpriced_records']
            total_tokens += row['total_tokens']
            total_records += row['usage_records']
            for model in json.loads(row['models']):
                aggregate = person['models'][model['model']]
                aggregate['total_tokens'] += model['total_tokens']
                aggregate['usage_records'] += model['usage_records']
                models[model['model']]['total_tokens'] += model['total_tokens']
                models[model['model']]['usage_records'] += model['usage_records']
            for day in json.loads(row['by_day']):
                aggregate = daily[day['day']]
                aggregate['total_tokens'] += day['total_tokens']
                aggregate['usage_records'] += day['usage_records']
        for (week_start, email), status in run_account_latest.items():
            person = people[email]
            person['email'] = email
            person['shared'] = person['shared'] or bool(status['shared'])
            apply_metadata(person, status, status['captured_epoch'])
            previous = user_status.get(email)
            if previous is None or week_start > previous['period_start']:
                user_status[email] = {**status, 'period_start': week_start}
        run_summaries = []
        for run in sorted(newest_by_week.values(), key=lambda row: row['period_start']):
            start = datetime.fromtimestamp(run['period_start'], BEIJING).date().isoformat()
            end = datetime.fromtimestamp(run['period_end'], BEIJING).date().isoformat()
            run_summaries.append({'run_id': run['run_id'], 'period_start': start, 'period_end': end,
                'captured_at': run['captured_at'], 'expected': run['expected_count'], 'success': run['success_count'],
                'failed': run['failed_count'], 'pagination_complete': bool(run['pagination_complete']), 'complete': bool(run['complete'])})
        week_starts = [lower + 7 * 86400 * i for i in range(weeks)]
        missing_weeks = [datetime.fromtimestamp(stamp, BEIJING).date().isoformat() for stamp in week_starts if stamp not in newest_by_week]
        incomplete_weeks = [datetime.fromtimestamp(stamp, BEIJING).date().isoformat() for stamp, run in newest_by_week.items() if not run['complete']]
        latest = max(newest_by_week.values(), key=lambda row: row['captured_epoch']) if newest_by_week else None
        all_complete = len(newest_by_week) == weeks and all(run['complete'] for run in newest_by_week.values())
        coverage_end = hi.isoformat() if all_complete else None
        if not all_complete:
            covered = lower
            for stamp in week_starts:
                run = newest_by_week.get(stamp)
                if not run or not run['complete']:
                    break
                covered = run['period_end']
            coverage_end = datetime.fromtimestamp(covered, BEIJING).isoformat()
        expected_current = latest['expected_count'] if latest else 0
        latest_accounts = [row for (week, _), row in run_account_latest.items() if latest and week == latest['period_start']]
        summary_charge_complete = charge_missing == 0 and all(p['charge_complete'] for p in people.values()) and all_complete
        def person_charge_complete(person):
            return bool(person.get('_has_snapshot')) and person['charge_complete'] and all_complete and all(
                run_account_latest.get((stamp, person['email']), {}).get('ok', True)
                for stamp in week_starts if stamp in newest_by_week)
        return {'period_start': lo.isoformat(), 'period_end': hi.isoformat(), 'timezone': 'Asia/Shanghai', 'source': 'cursor',
            'totals': {'requests': total_records, 'usage_records': total_records, 'total_tokens': total_tokens,
                'charged_cents': cursor_cents_output(charge_subtotal) if summary_charge_complete else None,
                'charge_subtotal_cents': cursor_cents_output(charge_subtotal), 'unpriced_records': charge_missing,
                'charge_complete': summary_charge_complete,
                'is_complete': all_complete},
            'by_user': [{'email': person['email'], 'name': person['name'], 'department': person['department'], 'shared': person['shared'], 'available': bool(person.get('_has_snapshot')), 'error_code': user_status.get(person['email'], {}).get('error_code'), 'last_success_at': person['last_success_at'],
                'totals': {'requests': person['usage_records'] if person.get('_has_snapshot') else None, 'usage_records': person['usage_records'] if person.get('_has_snapshot') else None, 'total_tokens': person['total_tokens'] if person.get('_has_snapshot') else None,
                    'charged_cents': cursor_cents_output(person['charged_cents']) if person_charge_complete(person) else None,
                    'charge_subtotal_cents': cursor_cents_output(person['charged_cents']) if person.get('_has_snapshot') else None,
                    'charge_complete': person_charge_complete(person), 'is_complete': bool(person.get('_has_snapshot')) and all_complete and all(
                        run_account_latest.get((stamp, person['email']), {}).get('ok', True)
                        for stamp in week_starts if stamp in newest_by_week)},
                'models': [{'model': name, **values} for name, values in sorted(person['models'].items(), key=lambda item: -item[1]['total_tokens'])]}
                for person in sorted(people.values(), key=lambda item: (-item['total_tokens'], item['email']))],
            'by_day': [{'day': day, **values} for day, values in sorted(daily.items())],
            'by_model': [{'model': name, **values} for name, values in sorted(models.items(), key=lambda item: -item[1]['total_tokens'])],
            'quality': {'is_complete': all_complete, 'missing_weeks': missing_weeks, 'incomplete_weeks': incomplete_weeks,
                'account_count': expected_current, 'success_count': len([row for row in latest_accounts if row['ok']]),
                'failed_count': len([row for row in latest_accounts if not row['ok']]), 'last_success_at': max((r['last_success_at'] for r in account_rows), default=None),
                'coverage_end': coverage_end}, 'sync_runs': run_summaries,
            'pricing': {'currency': 'USD', 'basis': 'cursor_charged_cents', 'notes': ['Cursor amount comes from the source usage records; it is not combined with Codex API-equivalent estimates or presented as an invoice.']}}

def csv_safe(value):
    text = str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text

def export_csv(summary):
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['周期开始', '周期结束(不含)', '邮箱', '已提交', '设备数', *COUNT_FIELDS, '估算USD', '已定价小计USD', '未定价请求', '数据完整'])
    for row in summary['by_user']:
        t = row['totals']
        writer.writerow([csv_safe(v) for v in [summary['period_start'], summary['period_end'], row['email'], row['submitted'], len(row['devices']), *[t[k] for k in COUNT_FIELDS], t['estimated_cost_usd'] if t['estimated_cost_usd'] is not None else '未定价或不完整', t['priced_cost_usd'], t['unpriced_requests'], t['is_complete']]])
    return ('\ufeff' + output.getvalue()).encode('utf-8')

class Handler(BaseHTTPRequestHandler):
    protocol_version, server_version, sys_version = 'HTTP/1.1', 'CodexUsage', ''
    def setup(self):
        super().setup()
        self.connection.settimeout(30)
    def log_message(self, fmt, *args):
        pass
    def reply(self, status, value, content_type='application/json; charset=utf-8'):
        body = json.dumps(value, ensure_ascii=False).encode('utf-8') if isinstance(value, (dict, list)) else value
        self.send_response(status)
        for key, val in [('Content-Type', content_type), ('Content-Length', str(len(body))), ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'), ('X-Frame-Options', 'DENY'), ('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'")]:
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        self.dispatch('GET')
    def do_POST(self):
        self.dispatch('POST')
    def read_json(self, maximum):
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
            raise Invalid('one Content-Length required')
        length = int(self.headers['Content-Length'])
        if not 0 < length <= maximum:
            self.close_connection = True
            self.reply(413, {'error': 'request body exceeds bounds'})
            return None
        if self.headers.get_content_type() != 'application/json':
            raise Invalid('application/json required')
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise Invalid('truncated body')
        data = json.loads(raw, object_pairs_hook=json_unique, parse_constant=lambda _: (_ for _ in ()).throw(Invalid('non-finite JSON')))
        if not isinstance(data, dict):
            raise Invalid('object required')
        return data
    def dispatch(self, method):
        try:
            parsed = urlsplit(self.path)
            if method == 'GET' and parsed.path == '/health':
                return self.reply(200, {'ok': True})
            static = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
            if method == 'GET' and parsed.path in static:
                name, mime = static[parsed.path]
                path = ROOT / 'web' / name
                return self.reply(200, path.read_bytes(), mime) if path.is_file() else self.reply(404, {'error': 'not found'})
            if method == 'POST' and parsed.path == '/api/enroll':
                data = self.read_json(MAX_ENROLL_BODY)
                if data is None:
                    return
                return self.reply(200, self.server.store.enroll(data))
            authorization = self.headers.get('Authorization', '')
            role, email = self.server.store.authenticate(authorization[7:]) if authorization.startswith('Bearer ') else (None, None)
            if not role:
                self.close_connection = True
                return self.reply(401, {'error': 'unauthorized'})
            if parsed.path in ('/api/users', '/api/summary', '/api/export.csv', '/api/cursor/summary', '/api/cursor/snapshots') and role != 'admin':
                self.close_connection = True
                return self.reply(403, {'error': 'admin required'})
            if parsed.path == '/api/events' and role != 'uploader':
                self.close_connection = True
                return self.reply(403, {'error': 'uploader token required'})
            if method == 'GET' and parsed.path == '/api/users':
                return self.reply(200, {'users': self.server.store.users()})
            if method == 'GET' and parsed.path == '/api/cursor/summary':
                params = parse_qs(parsed.query, keep_blank_values=True)
                if any(len(v) != 1 for v in params.values()):
                    raise Invalid('repeated query parameter')
                lo, hi = query_period(params)
                return self.reply(200, self.server.store.cursor_summary(lo, hi))
            if method == 'GET' and parsed.path in ('/api/summary', '/api/export.csv'):
                params = parse_qs(parsed.query, keep_blank_values=True)
                if any(len(v) != 1 for v in params.values()):
                    raise Invalid('repeated query parameter')
                lo, hi = query_period(params)
                result = self.server.store.summary(lo, hi, params.get('email', [None])[0])
                return self.reply(200, export_csv(result), 'text/csv; charset=utf-8') if parsed.path.endswith('.csv') else self.reply(200, result)
            if method == 'POST' and parsed.path == '/api/cursor/snapshots':
                data = self.read_json(MAX_BODY)
                if data is None:
                    return
                return self.reply(200, self.server.store.ingest_cursor_snapshot(data))
            if method != 'POST' or parsed.path not in ('/api/users', '/api/events'):
                self.close_connection = True
                return self.reply(404, {'error': 'not found'})
            data = self.read_json(MAX_BODY)
            if data is None:
                return
            if parsed.path == '/api/users':
                if set(data) != {'email'}:
                    raise Invalid('email required')
                return self.reply(201, self.server.store.create_user(data['email']))
            return self.reply(200, self.server.store.ingest(email, data))
        except EnrollmentConflict:
            self.close_connection = True
            self.reply(409, {'error': 'registration conflict; no credentials changed'})
        except AlreadyExists:
            self.close_connection = True
            self.reply(409, {'error': 'user already exists; existing upload token was not changed'})
        except CursorConflict:
            self.close_connection = True
            self.reply(409, {'error': 'Cursor run id reused with different snapshot'})
        except PermissionError:
            self.close_connection = True
            self.reply(403, {'error': 'uploader identity mismatch or disabled'})
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, RecursionError):
            self.close_connection = True
            self.reply(400, {'error': 'invalid request'})
        except (OSError, sqlite3.Error, ArithmeticError):
            self.close_connection = True
            self.reply(503, {'error': 'service unavailable'})

def make_server(host='127.0.0.1', port=8765, store=None):
    if store is None:
        raise ValueError('store required')
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads, server.store = True, store
    return server

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', default=str(ROOT / 'usage.sqlite3'))
    parser.add_argument('--prices', default=str(ROOT / 'prices.json'))
    parser.add_argument('--config', help='Private JSON file containing admin_key')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig')) if args.config else {}
    key = os.environ.get('CODEX_USAGE_ADMIN_KEY') or config.get('admin_key')
    if not key:
        parser.error('set CODEX_USAGE_ADMIN_KEY or --config containing admin_key (24+ characters)')
    try:
        server = make_server(args.host, args.port, Store(args.db, key, args.prices))
    except (ValueError, OSError, sqlite3.Error):
        parser.error('cannot initialize service; check configuration and database')
    print('Codex usage service listening at http://%s:%s' % (args.host, server.server_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == '__main__':
    main()
