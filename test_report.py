import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

import report
import collector


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex = self.root / "codex"
        self.state = self.codex / "codex-usage"
        self.state.mkdir(parents=True)
        self.token = "PRIVATE-UPLOAD-TOKEN-LONG-ENOUGH-123"
        (self.state / "upload.txt").write_text(self.token, encoding="utf-8")
        self.config = self.state / "remote.json"
        self.config.write_text(json.dumps({"url": "https://example.test/usage", "token_file": "upload.txt", "email": "worker@example.test"}), encoding="utf-8")
        folder = self.codex / "sessions"
        folder.mkdir()
        rows = [{"type": "session_meta", "payload": {"id": "PRIVATE-THREAD"}},
                {"type": "turn_context", "payload": {"model": "fixture-model", "turn_id": "t1"}},
                {"type": "token_usage_record", "timestamp": "2026-10-08T00:00:00Z", "payload": {"response_id": "PRIVATE-OLD", "turn_id": "t1", "usage": {"input_tokens": 1, "output_tokens": 1}}},
                {"type": "token_usage_record", "timestamp": "2026-10-12T01:00:00Z", "payload": {"response_id": "PRIVATE-RESPONSE", "turn_id": "t1", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10}}}]
        (folder / "fixture.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        self.prices = self.root / "prices.json"
        self.prices.write_text(json.dumps({"models": {"fixture-model": {"input": 2, "cached_input": .1, "output": 10}}}), encoding="utf-8")
        self.args = dict(period="last7", now="2026-10-16T17:38:34+08:00", codex_home=self.codex,
                         config=self.config, prices=self.prices, output_dir=self.root / "reports")

    def tearDown(self):
        self.temp.cleanup()

    def test_default_upload_matches_explicit_previous_week_and_last7_is_opt_in(self):
        args = {key: value for key, value in self.args.items() if key != "period"}
        with mock.patch("report.client.upload_with_retries", return_value=({"ok": True}, 1)) as send:
            default = report.run_report(**args)
            default_package = send.call_args.args[2]
            explicit = report.run_report(**args, period="last-week")
            explicit_package = send.call_args.args[2]
            rolling = report.run_report(**args, period="last7")
            rolling_package = send.call_args.args[2]
        self.assertTrue(default["ok"])
        self.assertEqual(default["period_start"], "2026-10-05T00:00:00+08:00")
        self.assertEqual(default["period_end"], "2026-10-12T00:00:00+08:00")
        self.assertEqual(default["totals"]["total_tokens"], 2)
        self.assertEqual(default_package["batch_id"], explicit_package["batch_id"])
        self.assertEqual(default_package["events"], explicit_package["events"])
        self.assertEqual(default["events_path"], explicit["events_path"])
        self.assertEqual(rolling["totals"]["total_tokens"], 110)
        self.assertNotEqual(default_package["batch_id"], rolling_package["batch_id"])

    def test_report_cli_without_period_is_previous_week_and_local_only(self):
        with mock.patch("report.enrollment.ensure_remote", side_effect=AssertionError("local only")), mock.patch("report.client.upload_with_retries", side_effect=AssertionError("local only")), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = report.main(["run", "--no-upload", "--now", self.args["now"], "--codex-home", str(self.codex), "--output-dir", str(self.root / "cli-reports")])
        result = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "local_only")
        self.assertEqual(result["period_start"], "2026-10-05T00:00:00+08:00")
        self.assertEqual(result["period_end"], "2026-10-12T00:00:00+08:00")
        self.assertEqual(result["totals"]["total_tokens"], 2)

    def test_one_scan_writes_safe_files_and_uploads_same_batch(self):
        ack = {"ok": True, "batch_id": "a" * 64, "received": 1, "new": 1, "duplicates": 0,
               "conflicts": 0, "rejected": 0, "quarantined": 0}
        with mock.patch("report.collector.collect", wraps=collector.collect) as scan, mock.patch("report.client.upload_with_retries", return_value=(ack, 1)) as send:
            result = report.run_report(**self.args)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "uploaded")
        self.assertEqual(scan.call_count, 1)
        self.assertTrue(scan.call_args.kwargs["include_events"])
        self.assertEqual(result["totals"]["total_tokens"], 110)
        package = json.loads(Path(result["events_path"]).read_text(encoding="utf-8"))
        self.assertEqual(send.call_args.args[2], package)
        self.assertEqual(send.call_args.args[1], self.token)
        self.assertEqual(result["configured_email_masked"], "w***@example.test")
        combined = json.dumps(result) + Path(result["report_path"]).read_text(encoding="utf-8") + json.dumps(package)
        for secret in (self.token, "worker@example.test", "PRIVATE-THREAD", "PRIVATE-RESPONSE"):
            self.assertNotIn(secret, combined)
        self.assertIn("do not establish ownership", combined)

    def test_local_only_never_loads_remote_credentials(self):
        with mock.patch("report.enrollment.ensure_remote", side_effect=AssertionError("must not read account/config or enroll")), mock.patch("report.client.resolve_remote", side_effect=AssertionError("must not read token")), mock.patch("report.client.upload_with_retries", side_effect=AssertionError("must not upload")):
            result = report.run_report(**self.args, no_upload=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "local_only")
        self.assertIsNone(result["configured_email_masked"])

    def test_scheduled_jitter_happens_once_after_fixed_files_before_upload(self):
        saved = {}
        def delayed(seconds):
            self.assertEqual(seconds, 437)
            files = list((self.root / "reports").glob("*-events.json"))
            self.assertEqual(len(files), 1)
            saved["bytes"] = files[0].read_bytes()
            saved["package"] = json.loads(saved["bytes"])
            self.assertTrue(list((self.root / "reports").glob("last7-*.json")))
        def uploaded(base, token, package, device, **kwargs):
            self.assertEqual(package, saved["package"])
            self.assertEqual(next((self.root / "reports").glob("*-events.json")).read_bytes(), saved["bytes"])
            return {"ok": True}, 1
        with mock.patch("report.random.randint", return_value=437) as jitter, mock.patch("report.time.sleep", side_effect=delayed) as sleep, mock.patch("report.client.upload_with_retries", side_effect=uploaded):
            result = report.run_report(**self.args, max_upload_delay_seconds=600)
        self.assertTrue(result["ok"])
        self.assertEqual(result["upload_delay_seconds"], 437)
        jitter.assert_called_once_with(0, 600)
        sleep.assert_called_once_with(437)

    def test_first_manual_and_local_only_reports_do_not_jitter(self):
        with mock.patch("report.random.randint", side_effect=AssertionError("must not draw delay")), mock.patch("report.time.sleep", side_effect=AssertionError("must not sleep")), mock.patch("report.client.upload_with_retries", return_value=({"ok": True}, 1)) as send:
            manual = report.run_report(**self.args)
            local = report.run_report(**self.args, no_upload=True, max_upload_delay_seconds=600)
        self.assertTrue(manual["ok"])
        self.assertEqual(local["status"], "local_only")
        self.assertEqual(send.call_count, 1)

    def test_invalid_delay_stops_before_identity_collection_or_upload(self):
        for invalid in (-1, 601, True, 1.5, "600", None):
            with self.subTest(invalid=invalid), mock.patch("report.enrollment.ensure_remote", side_effect=AssertionError("must not enroll")), mock.patch("report.collector.collect", side_effect=AssertionError("must not collect")):
                result = report.run_report(**self.args, max_upload_delay_seconds=invalid)
            self.assertEqual(result["status"], "invalid_options")

    def test_cli_forwards_valid_delay_and_rejects_out_of_range(self):
        with mock.patch("report.run_report", return_value={"ok": True}) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(report.main(["run", "--max-upload-delay-seconds", "600"]), 0)
        self.assertEqual(run.call_args.args[-1], 600)
        for invalid in ("-1", "601", "1.5"):
            with self.subTest(invalid=invalid), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                report.main(["run", "--max-upload-delay-seconds", invalid])
            self.assertEqual(error.exception.code, 2)

    def test_missing_config_stops_before_collect_and_does_not_log_secret(self):
        args = {**self.args, "config": self.root / "missing.json"}
        with mock.patch("report.enrollment.identity.read_account", return_value={"status": "no_account"}), mock.patch("report.collector.collect", side_effect=AssertionError("must not scan")):
            result = report.run_report(**args)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "enrollment_stopped")
        self.assertNotIn(self.token, json.dumps(result))

    def test_upload_failure_preserves_same_events_for_retry(self):
        error = HTTPError("https://example.test", 401, "PRIVATE-NETWORK-ERROR", {}, None)
        with mock.patch("report.client.upload_with_retries", side_effect=error):
            result = report.run_report(**self.args)
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 401)
        self.assertTrue(Path(result["report_path"]).is_file())
        self.assertTrue(Path(result["events_path"]).is_file())
        self.assertNotIn("PRIVATE-NETWORK-ERROR", json.dumps(result))

    def test_report_forwards_optional_config_pin(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["connect_ip"] = "203.0.113.65"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        with mock.patch("report.client.upload_with_retries", return_value=({"ok": True}, 1)) as send:
            result = report.run_report(**self.args)
        self.assertTrue(result["ok"])
        self.assertEqual(send.call_args.kwargs["connect_ip"], "203.0.113.65")


if __name__ == "__main__":
    unittest.main()
