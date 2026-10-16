import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import client
import configure


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.token = "PRIVATE-FIRST-UPLOAD-TOKEN-123456789"
        self.source = self.root / "issued-token.txt"
        self.source.write_text(self.token, encoding="utf-8")
        self.state = self.codex / "codex-usage"

    def tearDown(self):
        self.temp.cleanup()

    def run_configure(self, **kwargs):
        return configure.configure("Worker@Example.test", self.source, codex_home=self.codex, **kwargs)

    def test_first_setup_is_private_masked_and_idempotent_without_changing_device(self):
        self.state.mkdir(parents=True)
        device = self.state / "device-id.txt"
        device.write_text("device-existing", encoding="ascii")
        result = self.run_configure()
        first_config = (self.state / "remote.json").read_bytes()
        first_token = (self.state / "upload-token.json").read_bytes()
        self.assertTrue(result["ok"])
        self.assertEqual(result["email_masked"], "w***@example.test")
        self.assertEqual(client.resolve_remote(config_path=self.state / "remote.json")["token"], self.token)
        self.assertEqual(json.loads(first_config)["token_file"], "upload-token.json")
        self.assertEqual(self.run_configure()["status"], "already_configured")
        self.assertEqual((self.state / "remote.json").read_bytes(), first_config)
        self.assertEqual((self.state / "upload-token.json").read_bytes(), first_token)
        self.assertEqual(device.read_text(), "device-existing")
        self.assertFalse((self.state / ".configure.lock").exists())
        self.assertNotIn(self.token, json.dumps(result))
        self.assertNotIn("worker@example.test", json.dumps(result))

    def test_changed_token_email_or_destination_requires_replace(self):
        self.run_configure()
        old_config = (self.state / "remote.json").read_bytes()
        self.source.write_text(json.dumps({"upload_token": "PRIVATE-SECOND-UPLOAD-TOKEN-123456789"}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "configuration_exists_use_replace"):
            self.run_configure()
        self.source.write_text(self.token, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "configuration_exists_use_replace"):
            self.run_configure(url="https://another.example.test")
        with self.assertRaisesRegex(ValueError, "configuration_exists_use_replace"):
            configure.configure("different@example.test", self.source, codex_home=self.codex)
        self.assertEqual((self.state / "remote.json").read_bytes(), old_config)

    def test_replace_switches_unique_pointer_and_keeps_old_url_token_pair_intact(self):
        self.run_configure()
        old_config = json.loads((self.state / "remote.json").read_text())
        old_token_path = self.state / old_config["token_file"]
        original = old_token_path.read_bytes()
        self.source.write_text("PRIVATE-SECOND-UPLOAD-TOKEN-123456789", encoding="utf-8")
        result = self.run_configure(url="https://another.example.test", replace=True)
        current = json.loads((self.state / "remote.json").read_text())
        self.assertTrue(result["ok"])
        self.assertNotEqual(current["token_file"], old_config["token_file"])
        self.assertEqual(old_token_path.read_bytes(), original)
        self.assertEqual(current["url"], "https://another.example.test")
        self.assertEqual(client.resolve_remote(config_path=self.state / "remote.json")["token"], "PRIVATE-SECOND-UPLOAD-TOKEN-123456789")

    def test_failed_remote_pointer_write_preserves_old_pair_and_cleans_new_secret(self):
        self.run_configure()
        config_path = self.state / "remote.json"
        old_config, old_token = config_path.read_bytes(), (self.state / "upload-token.json").read_bytes()
        self.source.write_text("PRIVATE-SECOND-UPLOAD-TOKEN-123456789", encoding="utf-8")
        actual_write = configure.atomic_private_json
        def fail_config(path, value):
            if Path(path).name == "remote.json":
                raise OSError("test failure")
            actual_write(path, value)
        with mock.patch("configure.atomic_private_json", side_effect=fail_config):
            with self.assertRaises(OSError):
                self.run_configure(url="https://another.example.test", replace=True)
        self.assertEqual(config_path.read_bytes(), old_config)
        self.assertEqual((self.state / "upload-token.json").read_bytes(), old_token)
        self.assertFalse(list(self.state.glob("upload-token-*.json")))
        self.assertFalse((self.state / ".configure.lock").exists())

    def test_concurrent_config_refused_and_pin_preserved_only_for_same_destination(self):
        self.run_configure()
        lock = self.state / ".configure.lock"
        lock.write_text("existing lock")
        with self.assertRaisesRegex(ValueError, "configuration_busy_retry_later"):
            self.run_configure(replace=True)
        self.assertTrue(lock.exists())
        lock.unlink()
        path = self.state / "remote.json"
        config = json.loads(path.read_text())
        config.update(connect_ip="203.0.113.65", device_id="device-configured")
        path.write_text(json.dumps(config), encoding="utf-8")
        self.source.write_text("PRIVATE-SECOND-UPLOAD-TOKEN-123456789", encoding="utf-8")
        self.run_configure(replace=True)
        config = json.loads(path.read_text())
        self.assertEqual(config["connect_ip"], "203.0.113.65")
        self.assertEqual(config["device_id"], "device-configured")
        self.run_configure(url="https://another.example.test", replace=True)
        self.assertNotIn("connect_ip", json.loads(path.read_text()))

    def test_no_auth_file_read_and_cli_output_never_contains_token(self):
        auth = self.root / "auth.json"
        # File does not exist: rejection must happen before any read/stat.
        with self.assertRaisesRegex(ValueError, "openai_auth_file_not_allowed"):
            configure.configure("worker@example.test", auth, codex_home=self.codex)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = configure.main(["--email", "worker@example.test", "--token-file", str(self.source), "--codex-home", str(self.codex)])
        self.assertEqual(code, 0)
        self.assertNotIn(self.token, output.getvalue())
        self.assertNotIn("worker@example.test", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = configure.main(["--email", "not-email", "--token-file", str(self.source), "--codex-home", str(self.codex)])
        self.assertEqual(code, 1)
        self.assertNotIn(self.token, output.getvalue())


if __name__ == "__main__":
    unittest.main()
