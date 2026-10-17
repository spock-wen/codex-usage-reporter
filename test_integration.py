"""Independent black-box HTTP acceptance tests; synthetic identities and usage only.

Run after server.py is ready: python -m unittest test_integration -v
No Codex logs, login state, auth files, external server or user credentials are read.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
          "output_tokens", "reasoning_output_tokens", "total_tokens")
MODEL = "gpt-6.1-sol"
EMAIL_A = "integration-alpha@example.test"
EMAIL_B = "integration-beta@example.test"
START, END = "2026-10-05T00:00:00+08:00", "2026-10-19T00:00:00+08:00"


def synthetic_event(tag, inp=100, output=10, timestamp="2026-10-12T09:00:00+08:00",
                    cached=20, write=0, reasoning=2):
    return {"event_id": hashlib.sha256(("openai:codex:" + tag).encode()).hexdigest(),
            "timestamp": timestamp, "model": MODEL, "input_tokens": inp,
            "cached_input_tokens": cached, "cache_write_input_tokens": write,
            "output_tokens": output, "reasoning_output_tokens": reasoning,
            "total_tokens": inp + output}


def package(events, start=START, end=END):
    canonical = {"schema_version": 1, "period_start": start, "period_end": end,
                 "events": sorted(events, key=lambda row: row["event_id"])}
    batch_id = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=True,
                                         separators=(",", ":")).encode()).hexdigest()
    return {**canonical, "kind": "codex_usage_events", "batch_id": batch_id,
            "generated_at": datetime.now(timezone.utc).isoformat(), "timezone": "Asia/Shanghai",
            "data_status": "observed_local_usage", "is_complete": True,
            "incomplete_reasons": [], "quality": {}, "coverage_warnings": [],
            "metadata": {"source": "local_codex_token_usage_record",
                         "account_binding": "unknown; synthetic integration fixture"}}


class HttpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-http-integration-")
        self.root = Path(self.temp.name)
        self.admin = secrets.token_urlsafe(32)
        self.db = self.root / "usage.sqlite3"
        self.children = []
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stop_server)
        self.start_server()

    def start_server(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        env = os.environ.copy()
        env["CODEX_USAGE_ADMIN_KEY"] = self.admin
        env["PYTHONUTF8"] = "1"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py"), "--host", "127.0.0.1",
             "--port", str(self.port), "--db", str(self.db), "--prices", str(ROOT / "prices.json")],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, shell=False, creationflags=flags)
        self.children.append(self.process)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail("Synthetic server process exited before HTTP readiness")
            try:
                status, _ = self.http("GET", "/api/summary?start=2026-10-05&end=2026-10-19", self.admin)
                if status == 200:
                    return
            except (URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(.04)
        self.fail("Synthetic server did not become HTTP-ready within 12 seconds")

    def stop_server(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def http(self, method, path, token=None, body=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            return error.code, json.loads(error.read())

    def user(self, email):
        status, result = self.http("POST", "/api/users", self.admin, {"email": email})
        self.assertEqual(status, 201)
        self.assertEqual(result["email"], email)
        token = result["upload_token"]
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(len(token), 24)
        return token

    def upload(self, token, events, device="device-a", **bounds):
        status, result = self.http("POST", "/api/events", token,
                                   {"device_id": device, "package": package(events, **bounds)})
        self.assertEqual(status, 200)
        return result

    def summary(self, start="2026-10-05", end="2026-10-19", email=None):
        from urllib.parse import urlencode
        query = {"start": start, "end": end}
        if email is not None:
            query["email"] = email
        status, result = self.http("GET", "/api/summary?" + urlencode(query), self.admin)
        self.assertEqual(status, 200)
        return result

    def assert_totals(self, result, events):
        self.assertEqual(result["totals"]["requests"], len(events))
        for field in FIELDS:
            self.assertEqual(result["totals"][field], sum(row[field] for row in events), field)

    def test_same_device_retry_counts_once_and_token_is_not_listed(self):
        token = self.user(EMAIL_A)
        events = [synthetic_event("one", write=10), synthetic_event("two", 200, 20)]
        first, second = self.upload(token, events), self.upload(token, events)
        self.assertEqual(first["new"], 2)
        self.assertEqual(second["new"], 0)
        self.assertTrue(second["batch_repeated"])
        self.assert_totals(self.summary(), events)
        status, listing = self.http("GET", "/api/users", self.admin)
        self.assertEqual(status, 200)
        self.assertNotIn(token, json.dumps(listing))
        status, duplicate_user = self.http("POST", "/api/users", self.admin, {"email": EMAIL_A})
        self.assertEqual(status, 409)
        self.assertNotIn(token, json.dumps(duplicate_user))
        self.upload(token, events)
        self.assert_totals(self.summary(), events)

    def test_cursor_snapshot_endpoint_is_admin_only_and_idempotent(self):
        start, end = "2026-10-05T00:00:00+08:00", "2026-10-12T00:00:00+08:00"
        row = {"email": "cursor-alpha@example.test", "ok": True, "shared": False,
               "usage_records": 1, "total_tokens": 321, "charged_cents": 12.840812,
               "charge_records": 1, "unpriced_records": 0,
               "models": [{"model": "cursor-model", "total_tokens": 321, "usage_records": 1}],
               "by_day": [{"day": "2026-10-06", "total_tokens": 321, "usage_records": 1}]}
        snapshot = {"schema_version": 1, "kind": "cursor_weekly_snapshot", "run_id": str(uuid.uuid4()),
                    "period_start": start, "period_end": end, "captured_at": "2026-10-17T12:00:00+08:00",
                    "pagination_complete": True, "accounts": [row]}
        status, first = self.http("POST", "/api/cursor/snapshots", self.admin, snapshot)
        self.assertEqual(status, 200)
        self.assertFalse(first["repeated"])
        status, repeated = self.http("POST", "/api/cursor/snapshots", self.admin, snapshot)
        self.assertEqual(status, 200)
        self.assertTrue(repeated["repeated"])
        status, _ = self.http("GET", "/api/cursor/summary?start=2026-10-05&end=2026-10-12")
        self.assertEqual(status, 401)
        status, summary = self.http("GET", "/api/cursor/summary?start=2026-10-05&end=2026-10-12", self.admin)
        self.assertEqual(status, 200)
        self.assertEqual(summary["totals"]["total_tokens"], 321)
        self.assertEqual(summary["totals"]["charged_cents"], 12.840812)

    def test_two_devices_union_for_same_email(self):
        token = self.user(EMAIL_A)
        shared = synthetic_event("same-request")
        one, two = synthetic_event("machine-one", 200), synthetic_event("machine-two", 300)
        self.upload(token, [shared, one], "desktop")
        self.upload(token, [shared, two], "laptop")
        self.assert_totals(self.summary(email=EMAIL_A), [shared, one, two])
        self.assert_totals(self.summary(), [shared, one, two])

    def test_different_emails_isolated_and_do_not_accept_body_email_override(self):
        token_a, token_b = self.user(EMAIL_A), self.user(EMAIL_B)
        a, b = synthetic_event("owner-a", 100), synthetic_event("owner-b", 200)
        self.upload(token_a, [a])
        status, _ = self.http("POST", "/api/events", token_b,
                              {"device_id": "device-b", "email": EMAIL_A, "package": package([b])})
        self.assertIn(status, (200, 400, 403))
        if status in (400, 403):
            self.upload(token_b, [b], "device-b")
        self.assert_totals(self.summary(email=EMAIL_A), [a])
        self.assert_totals(self.summary(email=EMAIL_B), [b])
        self.assert_totals(self.summary(), [a, b])

    def test_cross_email_clone_is_quarantined_and_not_double_counted(self):
        token_a, token_b = self.user(EMAIL_A), self.user(EMAIL_B)
        shared = synthetic_event("owner-conflict")
        own_a, own_b = synthetic_event("independent-a", 200), synthetic_event("independent-b", 300)
        self.upload(token_a, [shared, own_a])
        ack = self.upload(token_b, [shared, own_b], "other-device")
        self.assertGreaterEqual(ack["conflicts"], 1)
        result = self.summary()
        self.assert_totals(result, [own_a, own_b])
        self.assertTrue(result["conflicts"])
        self.assert_totals(self.summary(email=EMAIL_A), [own_a])
        self.assert_totals(self.summary(email=EMAIL_B), [own_b])
        self.upload(token_a, [shared])
        self.assert_totals(self.summary(), [own_a, own_b])

    def test_same_id_content_conflict_excludes_original_instead_of_overwriting(self):
        token = self.user(EMAIL_A)
        original = synthetic_event("content-conflict")
        changed = {**original, "input_tokens": 999, "total_tokens": 1009}
        self.upload(token, [original])
        ack = self.upload(token, [changed], "other-device")
        self.assertGreaterEqual(ack["conflicts"], 1)
        result = self.summary()
        self.assert_totals(result, [])
        self.assertTrue(result["conflicts"])
        self.assertIsNone(result["totals"]["estimated_cost_usd"])

    def test_exact_batch_copied_to_another_email_cannot_bypass_ownership_conflict(self):
        token_a, token_b = self.user(EMAIL_A), self.user(EMAIL_B)
        event = synthetic_event("exact-batch-owner-conflict")
        self.upload(token_a, [event])
        ack = self.upload(token_b, [event], "copied-device")
        self.assertGreaterEqual(ack["conflicts"], 1)
        self.assert_totals(self.summary(), [])
        self.assertTrue(self.summary()["conflicts"])

    def test_concurrent_uploads_preserve_global_uniqueness(self):
        token = self.user(EMAIL_A)
        event = synthetic_event("concurrent-same-request")
        def send(index):
            return self.upload(token, [event], f"device-{index}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(send, range(8)))
        self.assertEqual(sum(row["new"] for row in results), 1)
        self.assert_totals(self.summary(), [event])

    def test_overlapping_packages_and_beijing_week_boundaries(self):
        token = self.user(EMAIL_A)
        sunday = synthetic_event("sunday", 111, timestamp="2026-10-11T23:59:59.999+08:00")
        monday = synthetic_event("monday", 222, timestamp="2026-10-12T00:00:00+08:00")
        self.upload(token, [sunday, monday], start="2026-10-10T00:00:00+08:00", end="2026-10-13T00:00:00+08:00")
        self.upload(token, [sunday, monday], "second", start="2026-10-11T00:00:00+08:00", end="2026-10-14T00:00:00+08:00")
        self.assert_totals(self.summary("2026-10-05", "2026-10-12"), [sunday])
        self.assert_totals(self.summary("2026-10-12", "2026-10-19"), [monday])
        self.assert_totals(self.summary("2026-10-11T16:00:00Z", "2026-10-12T16:00:00Z"), [monday])
        result = self.summary()
        self.assert_totals(result, [sunday, monday])
        self.assertEqual({row["day"]: row["requests"] for row in result["by_day"]},
                         {"2026-10-11": 1, "2026-10-12": 1})

    def test_authorization_errors_do_not_disclose_email_or_tokens(self):
        token = self.user(EMAIL_A)
        cases = [("GET", "/api/users", None, None, 401),
                 ("GET", "/api/summary", "not-valid-upload-secret", None, 401),
                 ("GET", "/api/summary", token, None, 403),
                 ("POST", "/api/users", token, {"email": EMAIL_B}, 403),
                 ("POST", "/api/events", self.admin,
                  {"device_id": "x", "package": package([synthetic_event("admin-denied")])}, 403)]
        for method, path, bearer, body, expected in cases:
            with self.subTest(method=method, path=path, role=expected):
                status, result = self.http(method, path, bearer, body)
                self.assertEqual(status, expected)
                serialized = json.dumps(result)
                for private in [self.admin, token, EMAIL_A, EMAIL_B, "not-valid-upload-secret"]:
                    self.assertNotIn(private, serialized)
        self.assert_totals(self.summary(), [])

    def test_invalid_batch_is_atomic(self):
        token = self.user(EMAIL_A)
        good, bad = synthetic_event("would-be-valid"), synthetic_event("invalid")
        bad["total_tokens"] += 1
        status, _ = self.http("POST", "/api/events", token,
                              {"device_id": "atomic", "package": package([good, bad])})
        self.assertEqual(status, 400)
        self.assert_totals(self.summary(), [])

    def test_oversized_declared_body_is_rejected_before_read(self):
        token = self.user(EMAIL_A)
        request = Request(self.url + "/api/events", data=b"{}", method="POST",
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/json",
                                   "Content-Length": str(17 * 1024 * 1024)})
        with self.assertRaises(HTTPError) as raised:
            urlopen(request, timeout=3)
        self.assertEqual(raised.exception.code, 413)
        safe_error = raised.exception.read().decode()
        self.assertNotIn(token, safe_error)
        self.assert_totals(self.summary(), [])

    def test_restart_persists_users_events_and_retry_identity(self):
        token = self.user(EMAIL_A)
        event = synthetic_event("persistent")
        self.upload(token, [event])
        self.stop_server()
        self.start_server()
        self.assert_totals(self.summary(), [event])
        ack = self.upload(token, [event])
        self.assertEqual(ack["new"], 0)
        self.assertTrue(ack["batch_repeated"])
        self.assert_totals(self.summary(), [event])


if __name__ == "__main__":
    unittest.main()
