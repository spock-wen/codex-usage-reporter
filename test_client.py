import tempfile
from pathlib import Path
import unittest
import json
import os
import io
import ssl
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request

from client import NoRedirect, device_id, load_token, upload_url, safe_ack, resolve_remote, load_remote_config, upload_with_retries, upload, PinnedHTTPSConnection, PinnedHTTPSHandler, validate_connect_ip


class ClientTests(unittest.TestCase):
    def test_pin_dials_literal_ip_but_preserves_original_tls_hostname(self):
        connection = PinnedHTTPSConnection("usage.example.test:8443", connect_ip="203.0.113.65", timeout=3)
        self.assertTrue(connection._context.check_hostname)
        self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
        plain_socket, tls_socket = mock.Mock(), mock.Mock()
        context = mock.Mock()
        context.wrap_socket.return_value = tls_socket
        with mock.patch.object(connection, "_create_connection", return_value=plain_socket) as dial, mock.patch.object(connection, "_context", context):
            connection.connect()
        dial.assert_called_once_with(("203.0.113.65", 8443), 3, None)
        context.wrap_socket.assert_called_once_with(plain_socket, server_hostname="usage.example.test")
        self.assertIs(connection.sock, tls_socket)
        handler = PinnedHTTPSHandler("203.0.113.65")
        self.assertTrue(handler._context.check_hostname)
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)

    def test_pin_preserves_request_host_and_disables_environment_proxy(self):
        ack = dict(ok=True, batch_id="a" * 64, received=1, new=1, duplicates=0,
                   conflicts=0, rejected=0, quarantined=0)
        package = {"kind": "codex_usage_events", "batch_id": "a" * 64, "events": [{}]}
        with mock.patch("client.build_opener") as opener, mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.invalid:9999"}):
            opener.return_value.open.return_value = io.BytesIO(json.dumps(ack).encode())
            self.assertEqual(upload("https://usage.example.test/team", "private", package, "device", connect_ip="203.0.113.65"), ack)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.host, "usage.example.test")
            self.assertEqual(request.full_url, "https://usage.example.test/team/api/events")
            handlers = opener.call_args.args
            self.assertTrue(any(getattr(h, "proxies", None) == {} for h in handlers))
            self.assertTrue(any(isinstance(h, PinnedHTTPSHandler) and h.connect_ip == "203.0.113.65" for h in handlers))
            self.assertIn(NoRedirect, handlers)

    def test_pin_config_is_optional_literal_only_and_not_inherited_for_override(self):
        for invalid in ("server.example.test", "203.0.113.65:443", "https://203.0.113.65", "fe80::1%eth0", 123):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_connect_ip(invalid)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            token = root / "upload.txt"
            token.write_text("private-token-long-enough-12345", encoding="utf-8")
            config = root / "remote.json"
            config.write_text(json.dumps({"url": "https://old.example.test", "token_file": "upload.txt", "connect_ip": "203.0.113.65"}), encoding="utf-8")
            self.assertEqual(resolve_remote(config_path=config)["connect_ip"], "203.0.113.65")
            self.assertIsNone(resolve_remote(url="https://new.example.test", token_file=token, config_path=config)["connect_ip"])
            config.write_text(json.dumps({"url": "https://old.example.test", "token_file": "upload.txt", "connect_ip": None}), encoding="utf-8")
            self.assertIsNone(resolve_remote(config_path=config)["connect_ip"])

    def test_upload_ack_must_match_exact_batch_and_event_count(self):
        package = {"kind": "codex_usage_events", "batch_id": "a" * 64, "events": [{}]}
        correct = dict(ok=True, batch_id="a" * 64, received=1, new=1, duplicates=0,
                       conflicts=0, rejected=0, quarantined=0)
        for ack in ({**correct, "batch_id": "b" * 64}, {**correct, "received": 2, "new": 2}):
            with self.subTest(ack=ack), mock.patch("client.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(json.dumps(ack).encode())
                with self.assertRaises(ValueError):
                    upload("https://example.test", "private-upload-token", package, "device")
        with mock.patch("client.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(correct).encode())
            self.assertEqual(upload("https://example.test", "private-upload-token", package, "device"), correct)

    def test_private_config_relative_token_and_stable_device(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            token = "private-upload-token-long-enough-123"
            (root / "upload.txt").write_text(token, encoding="utf-8")
            config = root / "remote.json"
            config.write_text(json.dumps({"url": "https://example.test/team", "token_file": "upload.txt", "email": "worker@example.test"}), encoding="utf-8")
            first = resolve_remote(config_path=config)
            second = resolve_remote(config_path=config)
            self.assertEqual(first["token"], token)
            self.assertEqual(first["device_id"], second["device_id"])
            self.assertEqual(first["url"], "https://example.test/team")
            self.assertEqual(first["expected_email"], "worker@example.test")
            config.write_text(json.dumps({"url": "https://example.test", "token_file": "upload.txt", "device_id": "configured-device"}), encoding="utf-8")
            self.assertEqual(resolve_remote(config_path=config)["device_id"], "configured-device")

    def test_default_config_uses_codex_home_and_missing_does_not_guess_host(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"CODEX_HOME": folder}, clear=False):
            config, path = load_remote_config()
            self.assertEqual(config, {})
            self.assertEqual(path, Path(folder) / "codex-usage" / "remote.json")
            with self.assertRaises(ValueError):
                resolve_remote()
            with self.assertRaises(ValueError):
                load_remote_config(Path(folder) / "missing.json")

    def test_explicit_other_destination_does_not_reuse_config_token(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "upload.txt").write_text("private-upload-token-long-enough-123", encoding="utf-8")
            path = root / "remote.json"
            path.write_text(json.dumps({"url": "https://old.example.test", "token_file": "upload.txt"}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"CODEX_USAGE_UPLOAD_TOKEN": ""}):
                with self.assertRaises(ValueError):
                    resolve_remote(url="https://new.example.test", config_path=path)
                explicit = root / "explicit.txt"
                explicit.write_text("new-destination-explicit-token-123", encoding="utf-8")
                self.assertEqual(resolve_remote(url="https://new.example.test", token_file=explicit, config_path=path)["token"], "new-destination-explicit-token-123")

    def test_retry_reuses_package_at_most_three_times_and_no_retry_for_auth(self):
        package = {"kind": "codex_usage_events", "batch_id": "a" * 64}
        ack = {"ok": True}
        with mock.patch("client.upload", side_effect=[TimeoutError(), TimeoutError(), ack]) as send, mock.patch("client.time.sleep") as sleep:
            result, attempts = upload_with_retries("https://example.test", "private", package, "device")
            self.assertIs(result, ack)
            self.assertEqual(attempts, 3)
            self.assertEqual(send.call_count, 3)
            self.assertTrue(all(call.args[2] is package for call in send.call_args_list))
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])
        with mock.patch("client.upload", side_effect=HTTPError("https://example.test", 401, "secret", {}, None)) as send:
            with self.assertRaises(HTTPError):
                upload_with_retries("https://example.test", "private", package, "device")
            self.assertEqual(send.call_count, 1)
        for attempts in (0, 4, True):
            with self.assertRaises(ValueError):
                upload_with_retries("https://example.test", "private", package, "device", max_attempts=attempts)

    def test_destination_policy_and_subpath(self):
        self.assertEqual(upload_url("http://127.0.0.1:8765/"), "http://127.0.0.1:8765/api/events")
        self.assertEqual(upload_url("https://example.com/codex-usage"), "https://example.com/codex-usage/api/events")
        for value in ("http://example.com", "file:///a", "https://user:password@example.com", "https://example.com/?token=x"):
            with self.assertRaises(ValueError):
                upload_url(value)

    def test_device_id_is_persistent_and_token_is_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(device_id(folder), device_id(folder))
            path = Path(folder) / "upload.json"
            path.write_text('{"upload_token":"test-upload-token-which-is-long-enough"}', encoding="utf-8")
            self.assertEqual(load_token(path), "test-upload-token-which-is-long-enough")

    def test_no_cross_origin_redirect_of_authorization(self):
        with self.assertRaises(HTTPError):
            NoRedirect().redirect_request(Request("http://127.0.0.1:8765/api/events"),
                                          None, 302, "redirect", {}, "https://other.invalid")

    def test_ack_values_cannot_reflect_credentials_or_fake_counts(self):
        valid = dict(ok=True, batch_id="a" * 64, received=2, new=1, duplicates=1,
                     conflicts=0, rejected=0, quarantined=0, batch_repeated=False)
        self.assertEqual(safe_ack({**valid, "status": "sensitive-untrusted-text"}), valid)
        for field, value in (("new", {"secret": "token"}), ("ok", "token"),
                             ("batch_id", "token"), ("duplicates", True), ("received", 3)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                safe_ack({**valid, field: value})


if __name__ == "__main__":
    unittest.main()
