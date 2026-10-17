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

class Invalid(ValueError):
    pass
class AlreadyExists(ValueError):
    pass
class EnrollmentConflict(ValueError):
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
            ''')
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
            if parsed.path in ('/api/users', '/api/summary', '/api/export.csv') and role != 'admin':
                self.close_connection = True
                return self.reply(403, {'error': 'admin required'})
            if parsed.path == '/api/events' and role != 'uploader':
                self.close_connection = True
                return self.reply(403, {'error': 'uploader token required'})
            if method == 'GET' and parsed.path == '/api/users':
                return self.reply(200, {'users': self.server.store.users()})
            if method == 'GET' and parsed.path in ('/api/summary', '/api/export.csv'):
                params = parse_qs(parsed.query, keep_blank_values=True)
                if any(len(v) != 1 for v in params.values()):
                    raise Invalid('repeated query parameter')
                lo, hi = query_period(params)
                result = self.server.store.summary(lo, hi, params.get('email', [None])[0])
                return self.reply(200, export_csv(result), 'text/csv; charset=utf-8') if parsed.path.endswith('.csv') else self.reply(200, result)
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
