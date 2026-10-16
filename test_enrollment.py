import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock
from urllib.error import URLError

import client
import configure
import enrollment
import report
from test_identity import FAKE_RPC


def account(email="worker@example.test"):
    return {"status": "ok", "login_type": "chatgpt", "email": email}


def receiver(base, email, device, registration_key, connect_ip=None, timeout=15):
    token = "cu_" + hashlib.sha256((email + device + registration_key).encode()).hexdigest()
    return {"email": email, "device_id": device, "upload_token": token,
            "identity_assurance": "self_reported", "enrollment_repeated": False}


class EnrollmentTests(unittest.TestCase):
    def test_explicit_legacy_migration_uses_real_rpc_and_preserves_old_credentials(self):
        self.config.parent.mkdir(parents=True)
        token_path = self.config.parent / "legacy-upload.txt"
        old_token = "PRIVATE-LEGACY-UPLOAD-TOKEN-LONG-ENOUGH"
        token_path.write_text(old_token)
        self.config.write_text(json.dumps({"url": self.args["url"], "email": "worker@example.test", "device_id": "device-preserved", "token_file": token_path.name}))
        fake = self.root / "fake-account-rpc.py"
        fake.write_text(FAKE_RPC, encoding="utf-8")
        trace = self.root / "rpc-trace.jsonl"
        original_read = enrollment.identity.read_account
        def real_rpc(**kwargs):
            kwargs.pop("executable", None)
            return original_read(**kwargs, _command=[sys.executable, str(fake), "success", str(trace)])
        with mock.patch("enrollment.identity.read_account", side_effect=real_rpc) as read, mock.patch("enrollment.request_enrollment", side_effect=receiver) as enroll:
            remote = enrollment.ensure_remote(**self.args, allow_account_switch=True)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(enroll.call_count, 1)
        self.assertEqual(remote["enrollment_mode"], "self_reported")
        self.assertEqual(remote["device_id"], "device-preserved")
        self.assertNotEqual(remote["token"], old_token)
        self.assertEqual(token_path.read_text(), old_token)
        methods = [json.loads(line)["method"] for line in trace.read_text().splitlines()]
        self.assertEqual(methods, ["initialize", "initialized", "account/read"])
        self.assertEqual(json.loads(trace.read_text().splitlines()[-1])["params"], {"refreshToken": False})

    def test_failed_explicit_legacy_migration_leaves_old_pair_usable(self):
        self.config.parent.mkdir(parents=True)
        token_path = self.config.parent / "legacy-upload.txt"
        old_token = "PRIVATE-LEGACY-UPLOAD-TOKEN-LONG-ENOUGH"
        token_path.write_text(old_token)
        self.config.write_text(json.dumps({"url": self.args["url"], "email": "worker@example.test", "device_id": "device-preserved", "token_file": token_path.name}))
        old_config = self.config.read_bytes()
        actual_write = configure.atomic_private_json
        def fail_switch(path, value):
            if Path(path) == self.config:
                raise OSError("PRIVATE-SAVE-ERROR")
            actual_write(path, value)
        with mock.patch("enrollment.identity.read_account", return_value=account()), mock.patch("enrollment.request_enrollment", side_effect=receiver), mock.patch("configure.atomic_private_json", side_effect=fail_switch):
            with self.assertRaises(OSError):
                enrollment.ensure_remote(**self.args, allow_account_switch=True)
        self.assertEqual(self.config.read_bytes(), old_config)
        self.assertEqual(token_path.read_text(), old_token)
        self.assertEqual(client.resolve_remote(config_path=self.config)["token"], old_token)
        self.assertTrue(list(self.config.parent.glob("enrollment-pending-*.json")))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex_home = self.root / "login-and-logs"
        self.config = self.root / "separate-private-state" / "remote.json"
        self.args = {"config_path": self.config, "codex_home": self.codex_home,
                     "url": "https://example.test/team", "codex_executable": "test-native.exe"}

    def tearDown(self):
        self.temp.cleanup()

    def test_first_auto_enrollment_saves_before_send_and_reuses_existing_credential(self):
        def inspect_saved(*args):
            pending = list(self.config.parent.glob("enrollment-pending-*.json"))
            self.assertEqual(len(pending), 1)
            payload = json.loads(pending[0].read_text())
            self.assertEqual(payload["registration_key"], args[3])
            self.assertEqual(payload["email"], "worker@example.test")
            return receiver(*args)
        with mock.patch("enrollment.identity.read_account", return_value=account()) as read, mock.patch("enrollment.request_enrollment", side_effect=inspect_saved) as enroll:
            first = enrollment.ensure_remote(**self.args)
            second = enrollment.ensure_remote(**self.args)
        self.assertEqual(first["token"], second["token"])
        self.assertEqual(first["device_id"], second["device_id"])
        self.assertEqual(enroll.call_count, 1)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args.kwargs["codex_home"], self.codex_home)
        self.assertTrue(read.call_args.kwargs["include_email"])
        self.assertFalse((self.codex_home / "codex-usage").exists())
        config = json.loads(self.config.read_text())
        self.assertEqual(config["enrollment_mode"], "self_reported")
        self.assertEqual(config["identity_assurance"], "self_reported")
        output = json.dumps(enrollment.safe_result(first))
        self.assertNotIn(first["token"], output)
        self.assertNotIn("worker@example.test", output)
        self.assertNotIn("registration_key", output)

    def test_failed_response_and_rerun_use_exact_same_persisted_key(self):
        keys = []
        def failed(*args):
            keys.append(args[3])
            raise URLError("PRIVATE-ERROR")
        with mock.patch("enrollment.identity.read_account", return_value=account()), mock.patch("enrollment.request_enrollment", side_effect=failed), mock.patch("client.time.sleep"):
            with self.assertRaises(URLError):
                enrollment.ensure_remote(**self.args)
        self.assertEqual(len(keys), 3)
        self.assertEqual(len(set(keys)), 1)
        self.assertFalse(self.config.exists())
        with mock.patch("enrollment.identity.read_account", return_value=account()), mock.patch("enrollment.request_enrollment", side_effect=receiver) as enroll:
            enrollment.ensure_remote(**self.args)
        self.assertEqual(enroll.call_args.args[3], keys[0])

    def test_multiple_machines_same_mailbox_have_distinct_device_key_and_token(self):
        with mock.patch("enrollment.identity.read_account", return_value=account()), mock.patch("enrollment.request_enrollment", side_effect=receiver) as enroll:
            first = enrollment.ensure_remote(**self.args)
            second = enrollment.ensure_remote(**{**self.args, "config_path": self.root / "machine-b" / "remote.json"})
        self.assertEqual(first["expected_email"], second["expected_email"])
        self.assertNotEqual(first["device_id"], second["device_id"])
        self.assertNotEqual(first["token"], second["token"])
        self.assertNotEqual(enroll.call_args_list[0].args[3], enroll.call_args_list[1].args[3])

    def test_api_key_no_mailbox_or_unavailable_identity_never_enroll(self):
        for identity in ({"status": "api_key", "login_type": "apiKey"}, {"status": "no_account"},
                         {"status": "timeout"}, account(None)):
            with self.subTest(identity=identity), mock.patch("enrollment.identity.read_account", return_value=identity), mock.patch("enrollment.request_enrollment") as send:
                with self.assertRaises(enrollment.EnrollmentError):
                    enrollment.ensure_remote(**self.args)
                send.assert_not_called()
                self.assertFalse(self.config.exists())

    def test_account_switch_refuses_report_but_explicit_enrollment_switches_safely(self):
        with mock.patch("enrollment.identity.read_account", return_value=account()), mock.patch("enrollment.request_enrollment", side_effect=receiver):
            first = enrollment.ensure_remote(**self.args)
        old_config = self.config.read_bytes()
        old_token_name = json.loads(old_config)["token_file"]
        old_token_bytes = (self.config.parent / old_token_name).read_bytes()
        with mock.patch("enrollment.identity.read_account", return_value=account("second@example.test")), mock.patch("enrollment.request_enrollment", side_effect=receiver) as send:
            with self.assertRaisesRegex(enrollment.EnrollmentError, "account_changed"):
                enrollment.ensure_remote(**self.args)
            send.assert_not_called()
            stopped = report.run_report(config=self.config, codex_home=self.codex_home, output_dir=self.root / "reports")
            self.assertEqual(stopped["status"], "enrollment_stopped")
            self.assertFalse((self.root / "reports").exists())
            second = enrollment.ensure_remote(**self.args, allow_account_switch=True)
        self.assertEqual(second["expected_email"], "second@example.test")
        self.assertNotEqual(first["token"], second["token"])
        self.assertEqual(first["device_id"], second["device_id"])
        self.assertEqual((self.config.parent / old_token_name).read_bytes(), old_token_bytes)

    def test_legacy_config_does_not_force_account_lookup_or_enroll(self):
        self.config.parent.mkdir(parents=True)
        token_path = self.config.parent / "admin-issued.txt"
        token_path.write_text("PRIVATE-LEGACY-UPLOAD-TOKEN-LONG-ENOUGH")
        self.config.write_text(json.dumps({"url": self.args["url"], "email": "legacy@example.test", "token_file": token_path.name}))
        with mock.patch("enrollment.identity.read_account", side_effect=AssertionError("legacy must not read identity")), mock.patch("enrollment.request_enrollment", side_effect=AssertionError("legacy must not enroll")):
            remote = enrollment.ensure_remote(**self.args)
        self.assertEqual(remote["expected_email"], "legacy@example.test")
        self.assertEqual(remote["enrollment_status"], "legacy_configuration_ready")

    def test_ack_requires_exact_mailbox_device_and_self_reported_mode(self):
        good = {"ok": True, "email": "worker@example.test", "device_id": "device-a", "upload_token": "cu_" + "a" * 64,
                "identity_assurance": "self_reported", "enrollment_repeated": False}
        variants = [{**good, "email": "other@example.test"}, {**good, "device_id": "device-b"},
                    {**good, "identity_assurance": "verified"}, {**good, "upload_token": "PRIVATE-NOT-AN-ACK-TOKEN"}]
        for ack in variants:
            with self.subTest(ack=ack), mock.patch("enrollment.build_opener") as opener:
                opener.return_value.open.return_value = io.BytesIO(json.dumps(ack).encode())
                with self.assertRaises(enrollment.EnrollmentError):
                    enrollment.request_enrollment("https://example.test/team", "worker@example.test", "device-a", "k" * 40)
        with mock.patch("enrollment.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(good).encode())
            result = enrollment.request_enrollment("https://example.test/team", "worker@example.test", "device-a", "k" * 40)
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://example.test/team/api/enroll")
            self.assertFalse(request.has_header("Authorization"))
            self.assertEqual(json.loads(request.data)["registration_key"], "k" * 40)
            self.assertEqual(result["upload_token"], good["upload_token"])

    def test_save_failure_keeps_registration_key_for_retry_and_cli_hides_secrets(self):
        with mock.patch("enrollment.identity.read_account", return_value=account()), mock.patch("enrollment.request_enrollment", side_effect=receiver) as enroll, mock.patch("enrollment.configure.save_enrolled_configuration", side_effect=OSError("PRIVATE-SECRET-ERROR")):
            with self.assertRaises(OSError):
                enrollment.ensure_remote(**self.args)
            key = enroll.call_args.args[3]
        self.assertFalse(self.config.exists())
        with mock.patch("enrollment.identity.read_account", return_value=account()), mock.patch("enrollment.request_enrollment", side_effect=receiver) as enroll:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = enrollment.main(["--url", self.args["url"], "--config", str(self.config), "--codex-home", str(self.codex_home)])
            self.assertEqual(code, 0)
            self.assertEqual(enroll.call_args.args[3], key)
            self.assertNotIn(key, output.getvalue())
            self.assertNotIn("upload_token", output.getvalue())
            self.assertNotIn("worker@example.test", output.getvalue())


if __name__ == "__main__":
    unittest.main()
