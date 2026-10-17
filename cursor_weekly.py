#!/usr/bin/env python3
"""Collect and publish one complete Cursor natural-week snapshot.

Cursor's current account event feed does not provide a stable response ID.
This command therefore sends account/week aggregates which the server replaces
by account and week, making repeated collection reports idempotent.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
from datetime import datetime, timedelta
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

from collector import BEIJING, parse_time
from server import Invalid, normalize_email, validate_cursor_snapshot

BASE = 'https://api2.cursor.sh'
PAGE_SIZE = 500
MAX_PAGES = 200
HTTP_TIMEOUT = 45
MODEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$')


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Requests carry Cursor or local admin bearer credentials. Never forward
        # those headers to a redirect target, even when it appears trustworthy.
        return None


HTTP_OPENER = urllib.request.build_opener(NoRedirectHandler())


def week_bounds(now=None):
    current = (now or datetime.now(BEIJING)).astimezone(BEIJING)
    monday = current.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=current.weekday())
    return monday - timedelta(days=7), monday


def read_accounts(path):
    accounts = {}
    with open(path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream)
        next(reader, None)
        for line_no, row in enumerate(reader, 2):
            if not row or not any(part.strip() for part in row):
                continue
            if len(row) < 4:
                raise ValueError(f'invalid account CSV row {line_no}')
            email, key, label = row[1].strip(), row[2].strip(), row[3].strip()
            email = normalize_email(email)
            if not key or email in accounts:
                raise ValueError(f'invalid or duplicate account row {line_no}')
            accounts[email] = {'email': email, 'key': key, 'shared': '/' in label}
    if not accounts:
        raise ValueError('account CSV is empty')
    return list(accounts.values())


def post_json(url, headers, value=None):
    body = b'{}' if value is None else json.dumps(value, separators=(',', ':')).encode('utf-8')
    request = urllib.request.Request(url, data=body, headers={**headers, 'Content-Type': 'application/json'}, method='POST')
    with HTTP_OPENER.open(request, timeout=HTTP_TIMEOUT) as response:
        result = json.loads(response.read().decode('utf-8'))
    if not isinstance(result, dict):
        raise ValueError('invalid JSON response')
    return result


def _nonnegative_integer(value):
    return type(value) is int and 0 <= value <= 10**15


def _account_failure(account, reason):
    return {'email': account['email'], 'ok': False, 'shared': account['shared'], 'error_code': reason}


def collect_account(account, start, end, sender=post_json):
    """Collect one account; sender(url, headers, optional_json_body) is injectable."""
    email = account['email']
    try:
        token_response = sender(BASE + '/auth/exchange_user_api_key', {'Authorization': 'Bearer ' + account['key']})
    except Exception:
        return _account_failure(account, 'auth_failed')
    token = token_response.get('accessToken')
    if not isinstance(token, str) or not token or len(token) > 8192:
        return _account_failure(account, 'auth_failed')
    headers = {'Authorization': 'Bearer ' + token, 'Connect-Protocol-Version': '1'}
    aggregate = {'usage_records': 0, 'total_tokens': 0, 'charged_cents': 0, 'charge_records': 0,
                 'unpriced_records': 0, 'models': {}, 'by_day': {}}
    page = 1
    try:
        while page <= MAX_PAGES:
            response = sender(BASE + '/aiserver.v1.DashboardService/GetFilteredUsageEvents', headers,
                {'page': page, 'pageSize': PAGE_SIZE, 'startDate': str(int(start.timestamp() * 1000))})
            events = response.get('usageEventsDisplay')
            if not isinstance(events, list):
                return _account_failure(account, 'invalid_response')
            if not events:
                break
            for event in events:
                if not isinstance(event, dict):
                    return _account_failure(account, 'invalid_response')
                stamp = event.get('timestamp')
                if type(stamp) is not int or stamp < 0:
                    return _account_failure(account, 'invalid_response')
                moment = datetime.fromtimestamp(stamp / 1000, BEIJING)
                if not start <= moment < end:
                    continue
                raw_model = event.get('model')
                model = raw_model if isinstance(raw_model, str) and MODEL.fullmatch(raw_model) else 'unknown'
                token_usage = event.get('tokenUsage') or {}
                if not isinstance(token_usage, dict):
                    return _account_failure(account, 'invalid_response')
                values = [token_usage.get(name, 0) or 0 for name in ('inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens')]
                if any(not _nonnegative_integer(value) for value in values):
                    return _account_failure(account, 'invalid_response')
                tokens = sum(values)
                charge = event.get('chargedCents')
                if charge is not None and not _nonnegative_integer(charge):
                    return _account_failure(account, 'invalid_response')
                # Preserve source charges even when an event has zero/missing
                # token counts or an unavailable model name. Ignore only rows
                # that contain neither billable tokens nor source amount.
                if tokens <= 0 and charge is None:
                    continue
                aggregate['usage_records'] += 1
                aggregate['total_tokens'] += tokens
                model_row = aggregate['models'].setdefault(model, {'model': model, 'total_tokens': 0, 'usage_records': 0})
                model_row['total_tokens'] += tokens
                model_row['usage_records'] += 1
                day = moment.date().isoformat()
                day_row = aggregate['by_day'].setdefault(day, {'day': day, 'total_tokens': 0, 'usage_records': 0})
                day_row['total_tokens'] += tokens
                day_row['usage_records'] += 1
                if charge is not None:
                    aggregate['charged_cents'] += charge
                    aggregate['charge_records'] += 1
                else:
                    aggregate['unpriced_records'] += 1
            if len(events) < PAGE_SIZE:
                break
            if page == MAX_PAGES:
                return _account_failure(account, 'pagination_truncated')
            page += 1
    except (urllib.error.URLError, TimeoutError, OSError):
        return _account_failure(account, 'events_failed')
    except Exception:
        return _account_failure(account, 'invalid_response')
    aggregate['charged_cents'] = aggregate['charged_cents'] if aggregate['charge_records'] else None
    return {'email': email, 'ok': True, 'shared': account['shared'], **aggregate,
            'models': sorted(aggregate['models'].values(), key=lambda row: (-row['total_tokens'], row['model'])),
            'by_day': sorted(aggregate['by_day'].values(), key=lambda row: row['day'])}


def collect(accounts, start, end, captured_at=None, sender=post_json, max_workers=8):
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_account, account, start, end, sender): account for account in accounts}
        for future in concurrent.futures.as_completed(futures):
            try:
                results.append(future.result())
            except Exception:
                # Never serialize the source row: it contains the Cursor API key.
                results.append(_account_failure(futures[future], 'collection_failed'))
    results.sort(key=lambda row: row['email'])
    payload = {'schema_version': 1, 'kind': 'cursor_weekly_snapshot', 'run_id': str(uuid.uuid4()),
        'period_start': start.isoformat(), 'period_end': end.isoformat(),
        'captured_at': (captured_at or datetime.now(BEIJING)).astimezone(BEIJING).isoformat(),
        'pagination_complete': all(row.get('error_code') != 'pagination_truncated' for row in results), 'accounts': results}
    validate_cursor_snapshot(payload)
    return payload


def atomic_save(path, payload):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix='.cursor-weekly-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_local_endpoint(endpoint):
    try:
        parts = urlsplit(endpoint)
        address = ipaddress.ip_address(parts.hostname or '')
        port = parts.port
    except (ValueError, TypeError):
        raise ValueError('snapshot endpoint must be a loopback HTTP URL') from None
    if (parts.scheme != 'http' or not address.is_loopback or port is None or
            parts.username is not None or parts.password is not None or
            parts.path != '/api/cursor/snapshots' or parts.query or parts.fragment):
        raise ValueError('snapshot endpoint must be a loopback HTTP URL')
    return endpoint


def upload(payload, endpoint, config_path, sender=post_json):
    endpoint = validate_local_endpoint(endpoint)
    config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    key = config.get('admin_key') if isinstance(config, dict) else None
    if not isinstance(key, str) or len(key) < 24:
        raise ValueError('invalid local service configuration')
    last_error = None
    for attempt in range(3):
        try:
            return sender(endpoint, {'Authorization': 'Bearer ' + key}, payload)
        except Exception:
            last_error = True
            if attempt < 2:
                time.sleep(2 ** attempt)
    if last_error:
        raise RuntimeError('Cursor snapshot upload failed; retry the saved snapshot')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--accounts', default='/srv/www/cursor-report/accounts.csv')
    parser.add_argument('--server-config', default='/etc/codex-usage/server.json')
    parser.add_argument('--endpoint', default='http://127.0.0.1:18765/api/cursor/snapshots')
    parser.add_argument('--output-dir', default='/srv/www/cursor-report/data/weekly')
    parser.add_argument('--now', help='ISO timestamp for a reproducible period selection')
    parser.add_argument('--retry-file', help='Upload an already saved snapshot without recollecting')
    args = parser.parse_args(argv)
    if args.retry_file:
        payload = json.loads(Path(args.retry_file).read_text(encoding='utf-8'))
        validate_cursor_snapshot(payload)
    else:
        now = parse_time(args.now) if args.now else datetime.now(BEIJING)
        start, end = week_bounds(now)
        accounts = read_accounts(args.accounts)
        began = time.monotonic()
        payload = collect(accounts, start, end, now)
        stamp_start, stamp_end = start.strftime('%Y%m%d'), end.strftime('%Y%m%d')
        atomic_save(Path(args.output_dir) / f'cursor-week-{stamp_start}-{stamp_end}-{payload["run_id"]}.json', payload)
        print(f'Collected {len(accounts)} Cursor accounts for {stamp_start}..{stamp_end}; {time.monotonic()-began:.0f}s.', flush=True)
    ack = upload(payload, args.endpoint, args.server_config)
    print(json.dumps({k: ack.get(k) for k in ('ok', 'period_start', 'period_end', 'expected', 'success', 'failed', 'repeated', 'stale', 'applied', 'complete')}, ensure_ascii=False))
    return 0 if ack.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
