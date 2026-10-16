"""Collect one local Codex report and optionally submit the same safe event batch."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import client
import collector


def masked_email(value):
    if not isinstance(value, str) or "@" not in value:
        return None
    local, domain = value.rsplit("@", 1)
    if not local or not domain:
        return None
    return local[0] + "***@" + domain


def run_report(period="last7", now=None, output_dir=None, config=None, codex_home=None,
               prices=None, no_upload=False):
    result = {"ok": False, "status": "not_started", "ack": None, "attempts": 0}
    remote = None
    if not no_upload:
        try:
            remote = client.resolve_remote(config_path=config, codex_home=codex_home)
        except (ValueError, OSError, TypeError):
            return {**result, "status": "configuration_required",
                    "reason": "configure_remote_url_and_upload_token_file_no_automatic_login"}
    try:
        price_path = Path(prices) if prices else Path(__file__).with_name("prices.json")
        report, package = collector.collect(codex_home=codex_home, period=period, now=now,
                                            prices=collector.load_prices(price_path), include_events=True)
        target = Path(output_dir or Path.cwd() / "codex-usage-reports").resolve()
        suffix = datetime.fromisoformat(report["as_of"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = target / (period + "-" + suffix + ".json")
        events_path = target / (period + "-" + suffix + "-events.json")
        report["submission_context"] = {
            "configured_email_masked": masked_email(remote.get("expected_email")) if remote else None,
            "attribution": "Configured reporting label only; current login and upload token do not establish ownership of historical local usage.",
            "upload_requested": not no_upload}
        collector.write_json(report_path, report)
        collector.write_json(events_path, package)
        result.update(status="local_files_ready", report_path=str(report_path), events_path=str(events_path),
                      period_start=report["period_start"], period_end=report["period_end"],
                      totals=report["totals"], data_status=report["data_status"],
                      configured_email_masked=report["submission_context"]["configured_email_masked"])
    except (ValueError, OSError, TypeError):
        return {**result, "status": "local_collection_failed", "reason": "invalid_local_input_or_output"}
    if no_upload:
        return {**result, "ok": True, "status": "local_only"}
    try:
        ack, attempts = client.upload_with_retries(remote["url"], remote["token"], package, remote["device_id"],
                                                   connect_ip=remote.get("connect_ip"))
        result.update(ok=ack["ok"], status="uploaded" if ack["ok"] else "receiver_rejected",
                      ack=ack, attempts=attempts)
    except HTTPError as error:
        result.update(status="upload_failed", reason="http_error_retry_saved_events", http_status=error.code)
    except (URLError, TimeoutError, OSError):
        result.update(status="upload_failed", reason="network_error_retry_saved_events")
    except (ValueError, TypeError):
        result.update(status="upload_failed", reason="invalid_package_or_ack_retry_saved_events")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("run", help="One scan, local report and safe events, then optional bounded upload")
    cmd.add_argument("--period", choices=("last7", "last-week"), default="last7")
    cmd.add_argument("--now", help="Timezone-aware ISO timestamp for a reproducible cutoff")
    cmd.add_argument("--output-dir", help="Report directory outside the installed skill; default current workspace/codex-usage-reports")
    cmd.add_argument("--config", help="Private remote.json; never stored in the skill bundle")
    cmd.add_argument("--codex-home")
    cmd.add_argument("--prices")
    cmd.add_argument("--no-upload", action="store_true", help="Generate local files without reading remote config or upload token")
    args = parser.parse_args(argv)
    result = run_report(args.period, args.now, args.output_dir, args.config, args.codex_home,
                        args.prices, args.no_upload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
