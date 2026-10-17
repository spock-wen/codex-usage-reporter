import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from collector import TOKEN_FIELDS, collect, estimate_cost, main, period_bounds

MODEL = "example-model"
PRICES = {"models": {MODEL: {"input": 2, "cached_input": .1, "cache_write": 2.5,
                            "output": 10, "long_context_threshold": 272000,
                            "long_input_multiplier": 2, "long_output_multiplier": 1.5}}}


def usage(i, c=0, o=10, r=0, w=0):
    return dict(input_tokens=i, cached_input_tokens=c, output_tokens=o,
                reasoning_output_tokens=r, cache_write_input_tokens=w)


def event(rid, ts="2026-10-12T01:00:00Z", counts=None, **payload):
    return {"type": "token_usage_record", "timestamp": ts,
            "payload": {"response_id": rid, "usage": counts if counts is not None else usage(100, 20), **payload}}


def meta(sid):
    return {"type": "session_meta", "payload": {"id": sid}}


def context(model=MODEL):
    return {"type": "turn_context", "payload": {"model": model}}


# A valid pre-window response establishes that these fixtures' source logs
# extend before the requested start. It must never count in the last7 totals.
BASELINE = event("before", "2026-10-08T00:00:00Z", usage(1, o=1), model=MODEL)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, rows):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return p

    def run_report(self, **kwargs):
        return collect(self.root, period="last7", now="2026-10-16T17:38:34+08:00", prices=PRICES, **kwargs)

    def test_response_id_dedup_across_path_machine_session_and_parent_meta(self):
        rows = [meta("actual-child"), meta("copied-parent"), context(), BASELINE,
                event("global-id"), event("different-id", counts=usage(50, 10, 5))]
        self.write("sessions/a.jsonl", rows)
        self.write("archived_sessions/machine-backup.jsonl", [meta("another-session"), context(), *rows[3:]])
        report = self.run_report()
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["totals"]["input_tokens"], 150)
        self.assertEqual(report["totals"]["requests"], 2)
        self.assertEqual(report["totals"]["sessions"], 1)
        self.assertEqual(report["quality"]["duplicate_response_records_skipped"], 3)
        self.assertAlmostEqual(report["totals"]["estimated_cost_usd"], .000393)

    def test_usage_time_and_model_conflicts_exclude_entire_id(self):
        for field in ("counts", "time", "model"):
            with self.subTest(field=field):
                changed = event("bad", model=MODEL)
                if field == "counts":
                    changed["payload"]["usage"] = usage(200)
                elif field == "time":
                    changed["timestamp"] = "2026-10-12T01:00:01Z"
                else:
                    changed["payload"]["model"] = "other-model"
                self.write("sessions/a.jsonl", [meta("a"), context(), BASELINE, event("bad", model=MODEL), changed, event("valid")])
                report = self.run_report()
                self.assertEqual(report["totals"]["requests"], 1)
                self.assertEqual(report["totals"]["input_tokens"], 100)
                self.assertEqual(report["quality"]["conflicted_response_ids"], 1)
                self.assertFalse(report["is_complete"])
                self.assertIsNone(report["totals"]["estimated_cost_usd"])
                self.assertGreater(report["totals"]["priced_cost_usd"], 0)

    def test_copied_context_disagreement_is_not_silently_resolved(self):
        self.write("sessions/a.jsonl", [meta("a"), context(), BASELINE, event("same")])
        self.write("sessions/b.jsonl", [meta("b"), context("other-model"), event("same")])
        report = self.run_report()
        self.assertEqual(report["totals"]["requests"], 0)
        self.assertEqual(report["data_status"], "incomplete")
        self.assertIsNone(report["totals"]["estimated_cost_usd"])

    def test_explicit_logged_model_overrides_previous_context(self):
        self.write("sessions/a.jsonl", [meta("a"), context("other-model"), BASELINE, event("one", model=MODEL)])
        report = self.run_report()
        self.assertEqual(report["by_model"][0]["model"], MODEL)
        self.assertEqual(report["quality"]["in_period_explicit_model_responses"], 1)
        self.assertIsNotNone(report["totals"]["estimated_cost_usd"])

    def test_turn_id_match_precedes_later_interleaved_context(self):
        first, second = context(), context("other-model")
        first["payload"]["turn_id"] = "first-turn"
        second["payload"]["turn_id"] = "second-turn"
        self.write("sessions/a.jsonl", [meta("a"), BASELINE, first, second,
                   event("delayed", turn_id="first-turn"), event("fallback", turn_id="not-found")])
        report = self.run_report()
        rows = {row["model"]: row for row in report["by_model"]}
        self.assertEqual(rows[MODEL]["requests"], 1)
        self.assertEqual(rows["other-model"]["requests"], 1)
        self.assertEqual(report["quality"]["in_period_turn_id_model_responses"], 1)
        self.assertEqual(report["quality"]["in_period_model_context_fallback_responses"], 1)

    def test_half_open_response_timestamp_boundaries(self):
        self.write("sessions/a.jsonl", [meta("a"), context(), BASELINE,
                   event("before-start", "2026-10-09T09:38:33.999Z"),
                   event("start", "2026-10-09T09:38:34Z"),
                   event("before-end", "2026-10-16T09:38:33.999Z"),
                   event("end", "2026-10-16T09:38:34Z")])
        report = self.run_report()
        self.assertEqual(report["totals"]["requests"], 2)
        self.assertEqual(report["by_day"][0]["day"], "2026-10-09")
        self.assertEqual(report["latest_source_usage_at"], "2026-10-16T17:38:34+08:00")

    def test_cache_write_reasoning_and_long_threshold_price_parts(self):
        cost, long = estimate_cost((1000000, 100000, 100000, 10000, 2000), MODEL, PRICES)
        self.assertTrue(long)
        self.assertEqual(float(cost), 3.87)
        cost, long = estimate_cost((272000, 0, 0, 10, 5), MODEL, PRICES)
        self.assertFalse(long)
        self.assertAlmostEqual(float(cost), .5441)
        self.write("sessions/a.jsonl", [meta("a"), context(), BASELINE,
                   event("long", counts=usage(1000000, 100000, 10000, 2000, 100000))])
        totals = self.run_report()["totals"]
        self.assertEqual(totals["total_tokens"], 1010000)
        self.assertEqual(totals["reasoning_output_tokens"], 2000)
        self.assertEqual(totals["priced_cost_breakdown_usd"],
                         {"input": 3.2, "cached_input": .02, "cache_write": .5, "output": .15})

    def test_unknown_model_preserves_tokens_without_free_price(self):
        self.write("sessions/a.jsonl", [meta("a"), BASELINE, event("missing-model")])
        report = self.run_report()
        self.assertEqual(report["totals"]["input_tokens"], 100)
        self.assertEqual(report["by_model"][0]["model"], "unknown")
        self.assertIsNone(report["totals"]["estimated_cost_usd"])
        self.assertEqual(report["totals"]["unpriced_requests"], 1)

    def test_legacy_and_missing_id_excluded(self):
        self.write("sessions/a.jsonl", [meta("a"), context(), BASELINE, event("valid"),
                   event(None), {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": usage(999999)}}}])
        report = self.run_report()
        self.assertEqual(report["totals"]["requests"], 1)
        self.assertEqual(report["quality"]["usage_records_without_response_id"], 1)
        self.assertEqual(report["quality"]["legacy_token_count_events_excluded"], 1)
        self.assertFalse(report["is_complete"])

    def test_invalid_tokens_and_invalid_clone_poison_id(self):
        variants = [usage(-1), usage(True), usage(10, 11), usage(10, 0, 2, 3),
                    usage(10, 5, 2, 0, 6), usage(10.0), {"input_tokens": 10},
                    dict(usage(10), total_tokens=999)]
        for invalid in variants:
            with self.subTest(invalid=invalid):
                self.write("sessions/a.jsonl", [meta("a"), context(), BASELINE, event("one"), event("one", counts=invalid)])
                report = self.run_report()
                self.assertEqual(report["totals"]["requests"], 0)
                self.assertIsNone(report["totals"]["estimated_cost_usd"])
                self.assertEqual(report["quality"]["invalid_usage_records"], 1)

    def test_malformed_and_invalid_timestamp_make_result_incomplete(self):
        bad_time = event("bad-time", "2026-10-12T01:00:00")
        p = self.write("sessions/a.jsonl", [meta("a"), context(), BASELINE, event("one"), bad_time])
        with p.open("ab") as stream:
            stream.write(b"bad-json\n[]\n\xff\n")
        report = self.run_report()
        self.assertEqual(report["totals"]["requests"], 1)
        self.assertEqual(report["quality"]["malformed_lines"], 3)
        self.assertEqual(report["quality"]["invalid_usage_timestamps"], 1)
        self.assertFalse(report["is_complete"])

    def test_empty_missing_and_late_source_not_reported_as_true_zero(self):
        for root in (self.root, self.root / "missing"):
            report = collect(root, now="2026-10-16T17:38:34+08:00")
            self.assertEqual(report["data_status"], "incomplete")
            self.assertIn("no_files", report["incomplete_reasons"])
            self.assertIsNone(report["totals"]["estimated_cost_usd"])
            self.assertIsNone(report["earliest_source_usage_at"])
        self.write("sessions/empty.jsonl", [])
        self.assertIn("no_canonical_usage_records", self.run_report()["incomplete_reasons"])
        self.write("sessions/late.jsonl", [meta("a"), context(), event("late")])
        self.assertIn("source_starts_after_period", self.run_report()["incomplete_reasons"])

    def test_privacy_and_group_sum_invariants(self):
        self.write("sessions/a.jsonl", [meta("PRIVATE-THREAD"), context(), BASELINE, event("PRIVATE-RESPONSE"),
                   {"type": "response_item", "payload": {"content": "PRIVATE-PROMPT"}},
                   event("two", "2026-10-13T01:00:00Z", usage(10, 1, 3, 2))])
        report = self.run_report()
        text = json.dumps(report)
        for secret in ("PRIVATE-THREAD", "PRIVATE-RESPONSE", "PRIVATE-PROMPT", str(self.root)):
            self.assertNotIn(secret, text)
        for group in ("by_day", "by_model", "by_day_model", "pricing_buckets"):
            for field in ("requests", "input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
                self.assertEqual(sum(row[field] for row in report[group]), report["totals"][field])

    def test_periods_beijing_monday_and_custom_timezone_requirement(self):
        now, lo, hi = period_bounds("last-week", "2026-10-16T17:38:34+08:00")
        self.assertEqual(lo.isoformat(), "2026-10-05T00:00:00+08:00")
        self.assertEqual(hi.isoformat(), "2026-10-12T00:00:00+08:00")
        with self.assertRaises(ValueError):
            period_bounds(now="2026-10-16T17:38:34")
        with self.assertRaises(ValueError):
            period_bounds(start="2026-10-05T00:00:00+08:00")

    def test_default_previous_week_is_stable_from_monday_through_sunday(self):
        self.write("sessions/defaults.jsonl", [meta("week-default"), context(),
                   event("previous", "2026-10-05T00:00:00+08:00", usage(30, o=4)),
                   event("current", "2026-10-12T00:00:00+08:00", usage(100, o=10))])
        for cutoff in ("2026-10-12T00:01:00+08:00", "2026-10-13T15:30:00+08:00", "2026-10-18T23:59:59+08:00"):
            with self.subTest(cutoff=cutoff):
                _, lo, hi = period_bounds(now=cutoff)
                result = collect(self.root, now=cutoff, prices=PRICES)
                self.assertEqual(lo.isoformat(), "2026-10-05T00:00:00+08:00")
                self.assertEqual(hi.isoformat(), "2026-10-12T00:00:00+08:00")
                self.assertEqual(result["period_start"], lo.isoformat())
                self.assertEqual(result["period_end"], hi.isoformat())
                self.assertEqual(result["totals"]["total_tokens"], 34)
        cutoff = "2026-10-13T15:30:00+08:00"
        _, lo, hi = period_bounds("last7", now=cutoff)
        self.assertEqual(lo.isoformat(), "2026-10-06T15:30:00+08:00")
        self.assertEqual(hi.isoformat(), cutoff)
        result = collect(self.root, period="last7", now=cutoff, prices=PRICES)
        self.assertEqual(result["totals"]["total_tokens"], 110)

    def test_collector_cli_without_period_uses_previous_week(self):
        self.write("sessions/cli.jsonl", [meta("cli"), context(),
                   event("previous", "2026-10-08T00:00:00Z", usage(30, o=4)),
                   event("current", "2026-10-12T00:00:00Z", usage(100, o=10))])
        output = self.root / "cli-report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["collect", "--codex-home", str(self.root), "--now", "2026-10-13T15:30:00+08:00", "--output", str(output)])
        self.assertIsNone(code)
        result = json.loads(output.read_text())
        self.assertEqual(result["period_start"], "2026-10-05T00:00:00+08:00")
        self.assertEqual(result["period_end"], "2026-10-12T00:00:00+08:00")
        self.assertEqual(result["totals"]["total_tokens"], 34)

    def test_event_package_stable_repeat_and_diagnostic_changes(self):
        p = self.write("sessions/a.jsonl", [meta("private-session"), context(), BASELINE,
                       event("stable-a"), event("stable-b", counts=usage(2, o=3))])
        report, first = self.run_report(include_events=True)
        _, second = self.run_report(include_events=True)
        self.assertEqual(first["events"], second["events"])
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(first["events"], sorted(first["events"], key=lambda r: r["event_id"]))
        expected = hashlib.sha256(b"openai:codex:stable-a").hexdigest()
        self.assertIn(expected, {row["event_id"] for row in first["events"]})
        # Changing scan diagnostics and observing later unrelated activity must
        # not change the batch ID for the same fixed range and canonical events.
        with p.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event("outside-period", "2026-10-17T00:00:00Z")) + "\nmalformed\n")
        _, third = self.run_report(include_events=True)
        self.assertNotEqual(first["quality"], third["quality"])
        self.assertEqual(first["batch_id"], third["batch_id"])
        self.assertEqual(first["events"], third["events"])
        self.assertEqual(first["is_complete"], report["is_complete"])
        self.assertNotIn("events", report)
        self.assertNotIn(expected, json.dumps(report))

    def test_event_package_clone_across_devices_has_same_hashes(self):
        rows = [meta("source-thread"), context(), BASELINE, event("shared-request")]
        p = self.write("sessions/a.jsonl", rows)
        _, original = self.run_report(include_events=True)
        self.write("archived_sessions/copied.jsonl", rows)
        _, cloned_local = self.run_report(include_events=True)
        self.assertEqual(original["events"], cloned_local["events"])
        self.assertEqual(original["batch_id"], cloned_local["batch_id"])
        with tempfile.TemporaryDirectory() as other_device:
            target = Path(other_device) / "sessions" / "renamed.jsonl"
            target.parent.mkdir()
            target.write_bytes(p.read_bytes())
            _, cloned_device = collect(other_device, period="last7", now="2026-10-16T17:38:34+08:00",
                                       prices=PRICES, include_events=True)
        self.assertEqual(original["events"], cloned_device["events"])
        self.assertEqual(original["batch_id"], cloned_device["batch_id"])

    def test_event_union_preserves_independent_machine_requests(self):
        shared = event("shared")
        self.write("sessions/a.jsonl", [context(), BASELINE, shared, event("device-one", counts=usage(20, o=3))])
        _, first = self.run_report(include_events=True)
        with tempfile.TemporaryDirectory() as other_device:
            target = Path(other_device) / "sessions" / "b.jsonl"
            target.parent.mkdir()
            target.write_text("\n".join(json.dumps(r) for r in [context(), BASELINE, shared,
                              event("device-two", counts=usage(30, o=5))]), encoding="utf-8")
            _, second = collect(other_device, period="last7", now="2026-10-16T17:38:34+08:00", prices=PRICES,
                                 include_events=True)
        # This is the future receiver's uniqueness rule, scoped to one team.
        merged = {event["event_id"]: event for event in first["events"] + second["events"] + first["events"]}
        self.assertEqual(len(merged), 3)
        self.assertEqual(sum(row["input_tokens"] for row in merged.values()), 150)
        self.assertEqual(sum(row["output_tokens"] for row in merged.values()), 18)

    def test_event_overlapping_periods_keep_request_hash_identical(self):
        self.write("sessions/a.jsonl", [context(), BASELINE, event("overlap", "2026-10-12T00:00:00Z"),
                                       event("later", "2026-10-14T00:00:00Z")])
        _, first = self.run_report(include_events=True, start="2026-10-10T00:00:00+08:00", end="2026-10-13T00:00:00+08:00")
        _, second = self.run_report(include_events=True, start="2026-10-11T00:00:00+08:00", end="2026-10-15T00:00:00+08:00")
        self.assertEqual(first["events"][0], next(row for row in second["events"] if row["event_id"] == first["events"][0]["event_id"]))
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(len({r["event_id"] for r in first["events"] + second["events"]}), 2)

    def test_events_conflicts_excluded_privacy_and_aggregate_alignment(self):
        self.write("sessions/a.jsonl", [meta("PRIVATE-SESSION"), BASELINE,
                   event("PRIVATE-RESPONSE", model=MODEL),
                   event("CONFLICT-RESPONSE", model=MODEL), event("CONFLICT-RESPONSE", counts=usage(777), model=MODEL),
                   {"type": "response_item", "payload": {"content": "PRIVATE-PROMPT", "authorization": "PRIVATE-AUTH"}},
                   event("UNKNOWN-RESPONSE", "2026-10-13T01:00:00Z", usage(10, 1, 3, 2))])
        report, package = self.run_report(include_events=True)
        encoded = json.dumps(package)
        for secret in ("PRIVATE-SESSION", "PRIVATE-RESPONSE", "CONFLICT-RESPONSE", "UNKNOWN-RESPONSE", "PRIVATE-PROMPT", "PRIVATE-AUTH", str(self.root)):
            self.assertNotIn(secret, encoded)
        excluded_hash = hashlib.sha256(b"openai:codex:CONFLICT-RESPONSE").hexdigest()
        self.assertNotIn(excluded_hash, encoded)
        self.assertFalse(package["is_complete"])
        self.assertEqual(package["data_status"], "incomplete")
        self.assertIn("conflicting_response_ids", package["incomplete_reasons"])
        self.assertEqual(len(package["events"]), report["totals"]["requests"])
        for key in (*TOKEN_FIELDS, "total_tokens"):
            self.assertEqual(sum(row[key] for row in package["events"]), report["totals"][key])
        for row in package["events"]:
            self.assertEqual(set(row), {"event_id", "timestamp", "model", *TOKEN_FIELDS, "total_tokens"})
            self.assertTrue(row["timestamp"].endswith("+08:00"))
        self.assertIn("unknown", {row["model"] for row in package["events"]})
        self.assertIsNone(report["totals"]["estimated_cost_usd"])

    def test_event_empty_sources_are_explicitly_incomplete(self):
        report, package = self.run_report(include_events=True)
        self.assertEqual(package["events"], [])
        self.assertEqual(report["totals"]["requests"], 0)
        self.assertFalse(package["is_complete"])
        self.assertEqual(package["data_status"], "incomplete")
        self.assertIn("no_files", package["incomplete_reasons"])

    def test_cli_event_output_is_separate_and_path_collision_rejected(self):
        self.write("sessions/a.jsonl", [context(), BASELINE, event("one")])
        aggregate = self.root / "aggregate.json"
        package = self.root / "events.json"
        argv = ["collect", "--codex-home", str(self.root), "--now", "2026-10-16T17:38:34+08:00",
                "--output", str(aggregate), "--events-output", str(package)]
        with contextlib.redirect_stdout(io.StringIO()):
            main(argv)
        self.assertNotIn("events", json.loads(aggregate.read_text(encoding="utf-8")))
        self.assertEqual(len(json.loads(package.read_text(encoding="utf-8"))["events"]), 1)
        original = aggregate.read_bytes()
        argv[-1] = str(aggregate)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(argv)
        self.assertEqual(aggregate.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
