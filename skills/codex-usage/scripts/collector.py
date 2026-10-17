"""Privacy-preserving, streaming Codex JSONL usage collector (Python stdlib only)."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sys

BEIJING = timezone(timedelta(hours=8))
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                "output_tokens", "reasoning_output_tokens")


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO8601 string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return result.astimezone(BEIJING)


def period_bounds(period="last-week", now=None, start=None, end=None):
    now = parse_time(now) if isinstance(now, str) else now or datetime.now(BEIJING)
    now = now.astimezone(BEIJING)
    if bool(start) != bool(end):
        raise ValueError("--start and --end must be supplied together")
    if start:
        lo, hi = parse_time(start), parse_time(end)
    elif period == "last-week":
        hi = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
        lo = hi - timedelta(days=7)
    elif period == "this-week":
        hi = now
        lo = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
        if lo == hi:
            raise ValueError("current_week_has_no_elapsed_interval")
    else:
        hi, lo = now, now - timedelta(days=7)
    if lo >= hi:
        raise ValueError("period start must be before end")
    return now, lo, hi


def usage_values(value):
    if not isinstance(value, dict) or not all(k in value for k in ("input_tokens", "output_tokens")):
        return None
    result = []
    for key in TOKEN_FIELDS:
        n = value.get(key, 0)
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            return None
        result.append(n)
    if result[1] + result[2] > result[0] or result[4] > result[3]:
        return None
    if "total_tokens" in value:
        total = value["total_tokens"]
        if isinstance(total, bool) or not isinstance(total, int) or total != result[0] + result[3]:
            return None
    return tuple(result)


def load_prices(path):
    if not path or not Path(path).exists():
        return {"currency": "USD", "models": {}}
    with Path(path).open(encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise ValueError("prices.json must contain a models object")
    return data


def estimate_cost(tokens, model, prices):
    """Return Decimal USD or None; input includes both cached and cache-write tokens."""
    breakdown, long_context = estimate_cost_parts(tokens, model, prices)
    return (sum(breakdown.values(), Decimal(0)) if breakdown is not None else None), long_context


def estimate_cost_parts(tokens, model, prices):
    rate = prices.get("models", {}).get(model)
    if not isinstance(rate, dict):
        return None, False
    inp, cached, write, output, _ = tokens
    threshold = rate.get("long_context_threshold")
    long_context = isinstance(threshold, (int, float)) and inp > threshold
    components = ((inp - cached - write, "input", "long_input_multiplier"),
                  (cached, "cached_input", "long_cached_input_multiplier"),
                  (write, "cache_write", "long_cache_write_multiplier"),
                  (output, "output", "long_output_multiplier"))
    parts = {}
    for count, field, multiplier in components:
        if not count:
            parts[field] = Decimal(0)
            continue
        if field not in rate or rate[field] is None:
            return None, long_context
        val = Decimal(str(rate[field]))
        if val < 0 or not val.is_finite():
            raise ValueError("prices must be finite non-negative numbers")
        factor = Decimal(str(rate.get(multiplier, rate.get("long_input_multiplier", 1)
                            if field in ("cached_input", "cache_write") else 1))) if long_context else Decimal(1)
        if factor < 0 or not factor.is_finite():
            raise ValueError("price multipliers must be finite non-negative numbers")
        parts[field] = Decimal(count) * val * factor / Decimal(1_000_000)
    return parts, long_context


class Aggregate:
    def __init__(self):
        self.counts = Counter()
        self.sessions = set()
        self.cost = Decimal(0)
        self.cost_parts = defaultdict(Decimal)
        self.unpriced = 0

    def add(self, tokens, session, cost_parts):
        self.counts.update(dict(zip(TOKEN_FIELDS, tokens)))
        self.counts["requests"] += 1
        self.sessions.add(session)
        if cost_parts is None:
            self.unpriced += 1
        else:
            self.cost += sum(cost_parts.values(), Decimal(0))
            for key, value in cost_parts.items():
                self.cost_parts[key] += value

    def row(self):
        counts = {key: self.counts[key] for key in TOKEN_FIELDS}
        counts.update(requests=self.counts["requests"], total_tokens=counts["input_tokens"] + counts["output_tokens"],
                      sessions=len(self.sessions), priced_cost_usd=round(float(self.cost), 8),
                      estimated_cost_usd=None if self.unpriced else round(float(self.cost), 8),
                      priced_cost_breakdown_usd={k: round(float(self.cost_parts[k]), 8)
                                                for k in ("input", "cached_input", "cache_write", "output")},
                      unpriced_requests=self.unpriced)
        return counts


def collect(codex_home=None, period="last-week", now=None, start=None, end=None, prices=None,
            include_events=False):
    """Return aggregates; opt in to (aggregates, safe event package) from one scan."""
    now, lo, hi = period_bounds(period, now, start, end)
    root = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    prices = prices or {"currency": "USD", "models": {}}
    files = sorted(set(p for folder in ("sessions", "archived_sessions")
                       for p in (root / folder).rglob("*.jsonl")))
    quality = Counter()
    # Retain only usage metadata, never messages. Conflict detection must finish
    # before aggregation: the first copy is not necessarily authoritative.
    requests, conflicts = {}, set()
    earliest = latest = None
    for path in files:
        session, model = None, "unknown"
        turn_models = {}
        file_session = hashlib.sha256(str(path).encode()).hexdigest()
        try:
            with path.open("rb") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError):
                        quality["malformed_lines"] += 1
                        continue
                    if not isinstance(record, dict):
                        quality["malformed_lines"] += 1
                        continue
                    payload = record.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    kind = record.get("type")
                    if kind == "session_meta" and session is None:
                        # Only the first metadata belongs to this physical thread.
                        # A fork may subsequently replay its parent's metadata.
                        sid = payload.get("id")
                        session = sid if isinstance(sid, str) and sid else file_session
                        continue
                    if kind == "turn_context":
                        candidate = payload.get("model")
                        model = candidate.strip() if isinstance(candidate, str) and candidate.strip() else "unknown"
                        turn_id = payload.get("turn_id")
                        if isinstance(turn_id, str) and turn_id:
                            if turn_id in turn_models and turn_models[turn_id] != model:
                                turn_models[turn_id] = "unknown"
                                quality["conflicting_turn_context_models"] += 1
                            else:
                                turn_models[turn_id] = model
                        continue
                    if kind == "event_msg" and payload.get("type") == "token_count":
                        quality["legacy_token_count_events_excluded"] += 1
                        continue
                    if kind != "token_usage_record":
                        continue
                    quality["source_usage_records_seen"] += 1
                    response_id = payload.get("response_id")
                    if not isinstance(response_id, str) or not response_id.strip():
                        quality["usage_records_without_response_id"] += 1
                        continue
                    try:
                        stamp = parse_time(record.get("timestamp"))
                    except (ValueError, OverflowError):
                        quality["invalid_usage_timestamps"] += 1
                        conflicts.add(response_id)
                        continue
                    tokens = usage_values(payload.get("usage"))
                    if tokens is None:
                        quality["invalid_usage_records"] += 1
                        conflicts.add(response_id)
                        continue
                    earliest = min(earliest, stamp) if earliest else stamp
                    latest = max(latest, stamp) if latest else stamp
                    explicit_model = payload.get("model", record.get("model"))
                    if explicit_model is not None:
                        actual_model = explicit_model.strip() if isinstance(explicit_model, str) and explicit_model.strip() else "unknown"
                        provenance = "record"
                    else:
                        turn_id = payload.get("turn_id")
                        matched_model = turn_models.get(turn_id) if isinstance(turn_id, str) else None
                        if matched_model is not None:
                            actual_model, provenance = matched_model, "turn_id_context"
                        else:
                            actual_model, provenance = model, "preceding_context_fallback" if model != "unknown" else "missing"
                    candidate = (stamp, tokens, actual_model, session or file_session, provenance)
                    old = requests.get(response_id)
                    if old is not None:
                        # Compare canonical response time, all usage counts, and
                        # effective logged model. A context disagreement is still
                        # a conflict: copied contexts cannot silently override it.
                        if old[:3] != candidate[:3]:
                            conflicts.add(response_id)
                            quality["conflicting_duplicate_records"] += 1
                        else:
                            quality["duplicate_response_records_skipped"] += 1
                    else:
                        requests[response_id] = candidate
        except OSError:
            quality["unreadable_files"] += 1
    total = Aggregate()
    safe_events = [] if include_events else None
    by_model, by_day, by_day_model, bands = (defaultdict(Aggregate) for _ in range(4))
    for response_id, (stamp, tokens, model, sid, provenance) in requests.items():
        if response_id in conflicts:
            if lo <= stamp < hi:
                quality["in_period_conflicted_responses_excluded"] += 1
            continue
        if not lo <= stamp < hi:
            continue
        if model == "unknown":
            quality["in_period_unknown_model_responses"] += 1
        parts, long_context = estimate_cost_parts(tokens, model, prices)
        if parts is None:
            quality["in_period_unpriced_responses"] += 1
        if long_context:
            quality["in_period_long_context_responses"] += 1
        if provenance == "record":
            quality["in_period_explicit_model_responses"] += 1
        elif provenance == "turn_id_context":
            quality["in_period_turn_id_model_responses"] += 1
        elif provenance == "preceding_context_fallback":
            quality["in_period_model_context_fallback_responses"] += 1
        day = stamp.date().isoformat()
        if safe_events is not None:
            safe_events.append({
                "event_id": hashlib.sha256(("openai:codex:" + response_id).encode("utf-8")).hexdigest(),
                "timestamp": stamp.isoformat(), "model": model,
                **dict(zip(TOKEN_FIELDS, tokens)), "total_tokens": tokens[0] + tokens[3]})
        for group in (total, by_model[model], by_day[day], by_day_model[(day, model)], bands[(day, model, long_context)]):
            group.add(tokens, sid, parts)
    quality["conflicted_response_ids"] = len(conflicts)
    quality["unique_response_ids_seen"] = len(requests)
    warnings = []
    incomplete_reasons = []
    if not files:
        incomplete_reasons.append("no_files")
        warnings.append("No JSONL files found in sessions or archived_sessions; zero is not verified zero usage.")
    elif earliest is None:
        incomplete_reasons.append("no_canonical_usage_records")
        warnings.append("No valid canonical token_usage_record with response_id found; zero is not verified zero usage.")
    if earliest is not None and earliest > lo:
        incomplete_reasons.append("source_starts_after_period")
        warnings.append("Earliest valid source usage is after the requested start; the preceding interval is not covered by available logs.")
    if conflicts:
        incomplete_reasons.append("conflicting_response_ids")
        warnings.append("Conflicting response IDs were entirely excluded; no copy was silently preferred. Totals are an incomplete observed subset.")
    for field in ("unreadable_files", "malformed_lines", "usage_records_without_response_id", "invalid_usage_timestamps", "invalid_usage_records"):
        if quality[field]:
            incomplete_reasons.append(field)
    if quality["legacy_token_count_events_excluded"]:
        warnings.append("Legacy token_count events were excluded: they may mirror canonical usage or contain inherited history and do not provide a global response ID.")
    warnings.append("Local logs cannot establish complete account coverage across devices, cloud-only tasks or deleted history; latest source time is not proof of activity up to the report end.")
    if quality["in_period_model_context_fallback_responses"]:
        warnings.append("Some responses lack a matching preceding turn_id context; their model uses the most recent logged context and may be uncertain if turns interleave.")
    incomplete = bool(incomplete_reasons)
    def output_row(aggregate):
        row = aggregate.row()
        row["is_complete"] = not incomplete
        if incomplete:
            row["estimated_cost_usd"] = None
        return row
    price_hash = hashlib.sha256(json.dumps(prices, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    report = {
        "schema_version": 2, "generated_at": datetime.now(BEIJING).isoformat(),
        "as_of": now.isoformat(), "period": "custom" if start else period,
        "period_start": lo.isoformat(), "period_end": hi.isoformat(), "timezone": "Asia/Shanghai",
        "data_status": "incomplete" if incomplete else "observed_local_usage",
        "is_complete": not incomplete,
        "incomplete_reasons": incomplete_reasons,
        "earliest_source_usage_at": earliest.isoformat() if earliest else None,
        "latest_source_usage_at": latest.isoformat() if latest else None,
        "coverage_warnings": warnings,
        "totals": output_row(total),
        "by_model": [dict(model=k, **output_row(v)) for k, v in sorted(by_model.items())],
        "by_day": [dict(day=k, **output_row(v)) for k, v in sorted(by_day.items())],
        "by_day_model": [dict(day=k[0], model=k[1], **output_row(v)) for k, v in sorted(by_day_model.items())],
        "pricing_buckets": [dict(day=k[0], model=k[1], long_context=k[2], **output_row(v)) for k, v in sorted(bands.items())],
        "quality": dict(sorted(quality.items())),
        "metadata": {"source": "local_codex_token_usage_record", "files_scanned": len(files),
                     "pricing_sha256": price_hash, "pricing_basis": prices.get("basis", "standard_api_equivalent"),
                     "pricing_verified_at": prices.get("verified_at"), "pricing_source": prices.get("source"),
                     "currency": "USD", "deduplication": "global exact response_id; conflicting copies fully excluded",
                     "privacy": "Aggregates only; no messages, credentials, paths, response IDs or account identifiers.",
                     "model_policy": "Explicit logged record model, else matching preceding turn_context.turn_id, else latest preceding turn_context.model with fallback diagnostic. No config inference. Any same-ID model disagreement is a conflict, including copied contexts.",
                     "limitations": ["Local account switches are not isolated by this report.",
                                     "API equivalent at the configured price snapshot, not a subscription bill or quota conversion.",
                                     "Reasoning output is included in output; cached input and cache-write are included in input.",
                                     "Unrecorded cache-write, speed tiers and additional tool charges cannot be separately priced.",
                                     "Canonical token_usage_record only; legacy token_count and records without response_id are excluded.",
                                     "is_complete refers to parse and coverage checks for available local sources, not complete account usage."]}}
    if safe_events is None:
        return report
    safe_events.sort(key=lambda event: event["event_id"])
    # Hash only stable payload content, never generated_at, as_of, diagnostics,
    # file paths, device/account/member identities or current price snapshots.
    canonical = {"schema_version": 1, "period_start": lo.isoformat(),
                 "period_end": hi.isoformat(), "events": safe_events}
    batch_id = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=True,
                                          separators=(",", ":")).encode("utf-8")).hexdigest()
    event_package = {
        **canonical, "kind": "codex_usage_events", "batch_id": batch_id,
        "generated_at": report["generated_at"], "timezone": "Asia/Shanghai",
        "data_status": report["data_status"], "is_complete": report["is_complete"],
        "incomplete_reasons": report["incomplete_reasons"],
        "quality": report["quality"], "coverage_warnings": report["coverage_warnings"],
        "metadata": {
            "source": "local_codex_token_usage_record",
            "privacy": "Contains irreversible request hashes and allowlisted usage metadata; no raw response/session IDs, paths, messages, credentials or account identifiers. Hashes remain linkable across packages.",
            "event_id_algorithm": "sha256(UTF-8('openai:codex:' + response_id))",
            "batch_id_algorithm": "sha256(canonical JSON of schema_version, normalized period_start/period_end and event_id-sorted events; sort_keys=true, ensure_ascii=true, compact separators)",
            "account_binding": "unknown; current login cannot establish historical request ownership",
            "deduplication": "Receivers must deduplicate by team_id + event_id, not device or batch; conflicting email ownership requires quarantine/review. This collector only exports local files and performs no upload.",
            "limitations": ["Conflicting or invalid response IDs are excluded; this package may be an incomplete observed subset.",
                            "An empty package does not prove zero account usage; inspect data_status and coverage_warnings.",
                            "Prices are not included; receivers must price valid token counts independently."]}}
    return report, event_package


def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("collect")
    cmd.add_argument("--period", choices=("last7", "last-week", "this-week"), default="last-week", help="Default: previous natural week in Beijing; this-week ends at now, last7 is a rolling seven-day window")
    cmd.add_argument("--codex-home")
    cmd.add_argument("--now")
    cmd.add_argument("--start")
    cmd.add_argument("--end")
    cmd.add_argument("--prices", default=str(Path(__file__).with_name("prices.json")))
    cmd.add_argument("--output", default="-", help="JSON output path, or - for stdout")
    cmd.add_argument("--events-output", help="Optional local JSON package of safe hashed request events; no upload")
    args = parser.parse_args(argv)
    try:
        if args.events_output:
            if args.events_output == "-":
                raise ValueError("--events-output requires a file path, not stdout")
            if args.output != "-" and Path(args.output).resolve() == Path(args.events_output).resolve():
                raise ValueError("aggregate and event outputs must use different files")
        collected = collect(args.codex_home, args.period, args.now, args.start, args.end,
                            load_prices(args.prices), include_events=bool(args.events_output))
        if args.events_output:
            report, event_package = collected
            write_json(args.events_output, event_package)
        else:
            report = collected
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output == "-":
            sys.stdout.write(encoded)
        else:
            target = Path(args.output)
            write_json(target, report)
            print(json.dumps({"output": str(target), "totals": report["totals"]}, ensure_ascii=False))
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
