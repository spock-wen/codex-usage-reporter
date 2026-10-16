"""Upload one local Codex request package; retries are deduplicated by the receiver."""
from __future__ import annotations

import argparse
import ipaddress
import http.client
import json
import os
from pathlib import Path
import re
import sys
import ssl
import queue
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, build_opener
import uuid

MAX_PACKAGE_BYTES = 16 * 1024 * 1024


def validate_connect_ip(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("connect_ip_must_be_ip_literal")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("connect_ip_must_be_ip_literal") from None
    # Zone identifiers would add platform-specific interface semantics.
    if "%" in value:
        raise ValueError("connect_ip_must_be_ip_literal")
    return str(parsed)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Dial only the configured IP; verify TLS against the original URL host."""
    def __init__(self, host, *, connect_ip, **kwargs):
        self.connect_ip = validate_connect_ip(connect_ip)
        super().__init__(host, **kwargs)

    def connect(self):
        if self._tunnel_host:
            raise ValueError("proxy_tunnel_not_allowed_with_connect_ip")
        self.sock = self._create_connection((self.connect_ip, self.port), self.timeout, self.source_address)
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except BaseException:
            self.sock.close()
            self.sock = None
            raise


class PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, connect_ip):
        self.connect_ip = validate_connect_ip(connect_ip)
        super().__init__(context=ssl.create_default_context())

    def https_open(self, request):
        def connection(host, **kwargs):
            return PinnedHTTPSConnection(host, connect_ip=self.connect_ip, **kwargs)
        return self.do_open(connection, request, context=self._context)


def usage_state_dir(codex_home=None):
    return Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "codex-usage"


def load_remote_config(path=None, codex_home=None):
    target = Path(path) if path else usage_state_dir(codex_home) / "remote.json"
    if not target.is_file():
        if path:
            raise ValueError("remote_config_missing")
        return {}, target
    if target.stat().st_size > 64 * 1024:
        raise ValueError("invalid_remote_config")
    value = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("invalid_remote_config")
    config = {}
    for field in ("url", "token_file", "device_id", "email", "connect_ip", "enrollment_mode", "identity_assurance"):
        if field in value:
            if field == "connect_ip" and value[field] in (None, ""):
                continue
            if not isinstance(value[field], str) or not value[field].strip():
                raise ValueError("invalid_remote_config")
            config[field] = value[field].strip()
    if "url" in config:
        upload_url(config["url"])
    if "connect_ip" in config:
        config["connect_ip"] = validate_connect_ip(config["connect_ip"])
        if "url" not in config or urlsplit(config["url"]).scheme != "https":
            raise ValueError("connect_ip_requires_https_url")
    if "token_file" in config:
        token_path = Path(config["token_file"])
        config["token_file"] = str(token_path if token_path.is_absolute() else target.parent / token_path)
    if "device_id" in config and not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", config["device_id"]):
        raise ValueError("invalid_config_device_id")
    return config, target


def resolve_remote(url=None, token_file=None, device=None, state_dir=None, config_path=None, codex_home=None):
    config, config_target = load_remote_config(config_path, codex_home)
    base = url or config.get("url")
    if not base:
        raise ValueError("remote_url_missing")
    upload_url(base)
    # A destination override must not accidentally send another server's token.
    same_target = not url or not config.get("url") or upload_url(url) == upload_url(config["url"])
    selected_token_file = token_file or (config.get("token_file") if same_target else None)
    token = load_token(selected_token_file)
    stable_device = device or config.get("device_id") or device_id(state_dir or config_target.parent)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", stable_device):
        raise ValueError("invalid_device_id")
    return {"url": base, "token": token, "device_id": stable_device,
            "expected_email": config.get("email") if same_target else None,
            "connect_ip": config.get("connect_ip") if same_target else None,
            "enrollment_mode": config.get("enrollment_mode") if same_target else None}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


def upload_url(base):
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("server URL must be HTTP(S)")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("server URL must not contain credentials, query or fragment")
    try:
        loopback = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        loopback = parts.hostname.lower() == "localhost"
    if parts.scheme == "http" and not loopback:
        raise ValueError("HTTP is allowed only on loopback; remote servers require HTTPS")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + "/api/events", "", ""))


def device_id(state_dir):
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "device-id.txt"
    try:
        with target.open("x", encoding="ascii") as handle:
            handle.write("device-" + uuid.uuid4().hex)
    except FileExistsError:
        pass
    value = target.read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        raise ValueError("invalid stored device ID")
    return value


def load_token(path=None):
    token = os.environ.get("CODEX_USAGE_UPLOAD_TOKEN")
    if path:
        if Path(path).resolve().name.casefold() == "auth.json":
            raise ValueError("OpenAI auth.json is not an upload-token file")
        raw = Path(path).read_text(encoding="utf-8-sig").strip()
        if raw.startswith("{"):
            token = json.loads(raw).get("upload_token")
        else:
            token = raw
    if not isinstance(token, str) or not 24 <= len(token) <= 256 or any(c.isspace() for c in token):
        raise ValueError("provide an upload token using --token-file or CODEX_USAGE_UPLOAD_TOKEN")
    return token


def safe_ack(ack):
    if not isinstance(ack, dict) or type(ack.get("ok")) is not bool:
        raise ValueError("invalid acknowledgment")
    batch = ack.get("batch_id")
    if not isinstance(batch, str) or not re.fullmatch(r"[0-9a-f]{64}", batch):
        raise ValueError("invalid acknowledgment batch ID")
    result = {"ok": ack["ok"], "batch_id": batch}
    for field in ("received", "new", "duplicates", "conflicts", "rejected", "quarantined"):
        value = ack.get(field)
        if type(value) is not int or not 0 <= value <= 50000:
            raise ValueError("invalid acknowledgment count")
        result[field] = value
    if sum(result[k] for k in ("new", "duplicates", "conflicts", "rejected")) != result["received"]:
        raise ValueError("inconsistent acknowledgment counts")
    for field in ("repeated_batch", "batch_repeated"):
        if field in ack:
            if type(ack[field]) is not bool:
                raise ValueError("invalid acknowledgment flag")
            result[field] = ack[field]
    return result


def upload(base, token, package, device, timeout=30, connect_ip=None):
    endpoint = upload_url(base)
    pin = validate_connect_ip(connect_ip)
    if pin and urlsplit(endpoint).scheme != "https":
        raise ValueError("connect_ip_requires_https_url")
    if not isinstance(package, dict) or package.get("kind") != "codex_usage_events":
        raise ValueError("input must be a collector --events-output package")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", device):
        raise ValueError("invalid device ID")
    body = json.dumps({"device_id": device, "package": package}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_PACKAGE_BYTES:
        raise ValueError("upload exceeds 16 MiB")
    request = Request(endpoint, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + token})
    opener = build_opener(NoRedirect, ProxyHandler({}), PinnedHTTPSHandler(pin)) if pin else build_opener(NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("unexpectedly large acknowledgment")
    ack = safe_ack(json.loads(raw))
    if ack["batch_id"] != package.get("batch_id"):
        raise ValueError("acknowledgment does not match uploaded batch")
    events = package.get("events")
    if not isinstance(events, list) or ack["received"] != len(events):
        raise ValueError("acknowledgment does not match uploaded event count")
    return ack


def upload_with_retries(base, token, package, device, timeout=15, max_attempts=3, connect_ip=None):
    """Retry the identical in-memory batch, with a hard 55-second caller deadline.

    Each network worker also has a socket timeout. Daemon workers prevent DNS or
    platform network stalls from keeping the CLI alive after its bounded result.
    """
    return perform_with_retries(lambda seconds: upload(base, token, package, device, seconds, connect_ip=connect_ip),
                                timeout=timeout, max_attempts=max_attempts)


def perform_with_retries(operation, timeout=15, max_attempts=3):
    """Bound one fixed-payload network operation; never recollect or change identity."""
    if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
        raise ValueError("invalid_retry_count")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 15:
        raise ValueError("timeout_must_be_0_to_15")
    deadline = time.monotonic() + 55
    last_error = None
    for attempt in range(1, max_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("upload_deadline_exceeded")
        result = queue.Queue(maxsize=1)
        def network_worker(result_queue=result, seconds=min(timeout, remaining)):
            try:
                result_queue.put((True, operation(seconds)))
            except Exception as error:
                result_queue.put((False, error))
        threading.Thread(target=network_worker, name="codex-usage-upload", daemon=True).start()
        try:
            success, value = result.get(timeout=min(timeout, remaining))
        except queue.Empty:
            success, value = False, TimeoutError("upload_attempt_timeout")
        if success:
            return value, attempt
        last_error = value
        retryable = isinstance(value, (URLError, TimeoutError, OSError))
        if isinstance(value, HTTPError):
            retryable = value.code in (408, 429, 500, 502, 503, 504)
        if not retryable or attempt == max_attempts:
            raise value
        delay = min(float(attempt), max(0, deadline - time.monotonic()))
        if delay:
            time.sleep(delay)
    raise last_error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Explicit server URL; otherwise use private remote.json")
    parser.add_argument("--config", help="Private remote.json; defaults to CODEX_HOME/codex-usage/remote.json")
    parser.add_argument("--codex-home")
    parser.add_argument("--codex-executable", help="Native Codex binary for read-only account detection when auto-enrollment is needed")
    parser.add_argument("--events", required=True)
    parser.add_argument("--token-file", help="Upload token text or JSON {upload_token: ...}; never an OpenAI token")
    parser.add_argument("--device-id")
    parser.add_argument("--state-dir")
    parser.add_argument("--timeout", type=float, default=15, help="Per-attempt timeout, maximum 15 seconds")
    args = parser.parse_args(argv)
    try:
        if not 0 < args.timeout <= 15:
            raise ValueError("timeout must be 0..15 seconds")
        path = Path(args.events)
        if path.stat().st_size > MAX_PACKAGE_BYTES:
            raise ValueError("input package exceeds 16 MiB")
        package = json.loads(path.read_text(encoding="utf-8-sig"))
        if args.token_file or (args.url and os.environ.get("CODEX_USAGE_UPLOAD_TOKEN")):
            remote = resolve_remote(args.url, args.token_file, args.device_id, args.state_dir, args.config, args.codex_home)
        else:
            import enrollment
            try:
                remote = enrollment.ensure_remote(config_path=args.config, codex_home=args.codex_home,
                                                   codex_executable=args.codex_executable, url=args.url)
            except enrollment.EnrollmentError as error:
                print(json.dumps({"ok": False, "reason": str(error), "status": "enrollment_stopped"}))
                return 1
            selected_device = args.device_id or (device_id(args.state_dir) if args.state_dir else None)
            if selected_device:
                if remote.get("enrollment_mode") == "self_reported" and selected_device != remote["device_id"]:
                    raise ValueError("auto_enrolled_device_cannot_be_overridden")
                remote["device_id"] = selected_device
        result, attempts = upload_with_retries(remote["url"], remote["token"], package, remote["device_id"], args.timeout,
                                              connect_ip=remote["connect_ip"])
        result["attempts"] = attempts
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    except HTTPError as error:
        print(json.dumps({"ok": False, "reason": "http_error", "status": error.code}))
    except (URLError, TimeoutError):
        print(json.dumps({"ok": False, "reason": "network_error_retry_same_package"}))
    except (ValueError, OSError, TypeError):
        print(json.dumps({"ok": False, "reason": "invalid_local_input_or_ack"}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
