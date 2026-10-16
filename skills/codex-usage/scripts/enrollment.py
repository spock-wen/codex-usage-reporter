"""Enroll the current locally logged-in Codex mailbox as an internal self-report."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, ProxyHandler

import client
import configure
import identity

DEFAULT_URL = configure.DEFAULT_URL


class EnrollmentError(ValueError):
    pass


def _email(value):
    if not isinstance(value, str) or len(value) > 320 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value.strip()):
        raise EnrollmentError("current_account_email_unavailable")
    return value.strip().casefold()


def request_enrollment(base, email, device, registration_key, connect_ip=None, timeout=15):
    """Anonymous enroll only; TLS/Host/redirect rules match the upload transport."""
    upload_endpoint = client.upload_url(base)
    parts = urlsplit(upload_endpoint)
    endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path.removesuffix("/events") + "/enroll", "", ""))
    pin = client.validate_connect_ip(connect_ip)
    if pin and parts.scheme != "https":
        raise EnrollmentError("connect_ip_requires_https_url")
    payload = {"email": email, "device_id": device, "registration_key": registration_key}
    request = Request(endpoint, data=json.dumps(payload, separators=(",", ":")).encode("utf-8"), method="POST",
                      headers={"Content-Type": "application/json"})
    opener = build_opener(client.NoRedirect, ProxyHandler({}), client.PinnedHTTPSHandler(pin)) if pin else build_opener(client.NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(16385)
        if len(raw) > 16384:
            raise EnrollmentError("invalid_enrollment_ack")
    try:
        ack = json.loads(raw)
    except (ValueError, UnicodeError):
        raise EnrollmentError("invalid_enrollment_ack") from None
    if (not isinstance(ack, dict) or ack.get("ok") is not True or ack.get("email") != email or
            ack.get("device_id") != device or ack.get("identity_assurance") != "self_reported" or
            type(ack.get("enrollment_repeated")) is not bool):
        raise EnrollmentError("enrollment_ack_identity_mismatch")
    token = ack.get("upload_token")
    if not isinstance(token, str) or not re.fullmatch(r"cu_[0-9a-f]{64}", token):
        raise EnrollmentError("invalid_enrollment_token")
    # Discard every non-contract field; never expose the token in CLI output.
    return {"email": email, "device_id": device, "upload_token": token,
            "identity_assurance": "self_reported", "enrollment_repeated": ack["enrollment_repeated"]}


def ensure_remote(config_path=None, codex_home=None, codex_executable=None, url=None,
                  connect_ip=None, allow_account_switch=False):
    target = Path(config_path) if config_path else client.usage_state_dir(codex_home) / "remote.json"
    target = target.absolute()
    if target.is_symlink() or target.resolve().name.casefold() == "auth.json":
        raise EnrollmentError("invalid_private_config_path")
    state = target.parent
    config = {}
    expected_existing = None
    if target.exists():
        config, _ = client.load_remote_config(target)
        if url and configure.normalized_url(url) != configure.normalized_url(config.get("url", "")):
            raise EnrollmentError("configured_destination_differs")
        if config.get("enrollment_mode") != "self_reported" and not allow_account_switch:
            # Advanced/legacy administrator-issued credentials remain usable.
            remote = client.resolve_remote(config_path=target, codex_home=codex_home)
            if connect_ip is not None:
                remote["connect_ip"] = client.validate_connect_ip(connect_ip)
            remote["enrollment_status"] = "legacy_configuration_ready"
            return remote
        expected_existing = (configure.normalized_url(config.get("url", "")), config.get("email", "").casefold(), config.get("enrollment_mode"))
    account = identity.read_account(executable=codex_executable, codex_home=codex_home, include_email=True)
    if account.get("status") != "ok" or account.get("login_type") != "chatgpt":
        raise EnrollmentError("current_chatgpt_account_unavailable_no_enrollment")
    email = _email(account.get("email"))
    base = configure.normalized_url(url or config.get("url") or DEFAULT_URL)
    pin = client.validate_connect_ip(connect_ip) if connect_ip is not None else config.get("connect_ip")
    if pin and urlsplit(base).scheme != "https":
        raise EnrollmentError("connect_ip_requires_https_url")
    if config:
        if config.get("email", "").casefold() != email and not allow_account_switch:
            raise EnrollmentError("account_changed_run_enrollment_explicitly")
        if config.get("email", "").casefold() == email and config.get("enrollment_mode") == "self_reported":
            try:
                remote = client.resolve_remote(config_path=target, codex_home=codex_home)
                remote["connect_ip"] = pin
                remote["enrollment_status"] = "current_account_matches"
                return remote
            except (ValueError, OSError):
                pass  # Recover this same mailbox using its persisted registration key.
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = state / ".enrollment.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise EnrollmentError("enrollment_busy_retry_later") from None
    os.close(descriptor)
    try:
        device = config.get("device_id") or client.device_id(state)
        config_tag = hashlib.sha256(target.name.encode()).hexdigest()[:16]
        selection_path = state / ("enrollment-selection-" + config_tag + ".json")
        if selection_path.is_symlink():
            raise EnrollmentError("invalid_pending_registration")
        selection = {"url": base, "email": email, "device_id": device}
        if selection_path.exists():
            try:
                selected = json.loads(selection_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raise EnrollmentError("invalid_pending_registration") from None
            if selected != selection and not allow_account_switch:
                raise EnrollmentError("pending_registration_identity_changed")
        identity_tag = hashlib.sha256(json.dumps(selection, sort_keys=True).encode()).hexdigest()
        pending_path = state / ("enrollment-pending-" + config_tag + "-" + identity_tag + ".json")
        if pending_path.is_symlink():
            raise EnrollmentError("invalid_pending_registration")
        if pending_path.exists():
            try:
                pending = json.loads(pending_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raise EnrollmentError("invalid_pending_registration") from None
            if (not isinstance(pending, dict) or any(pending.get(k) != v for k, v in selection.items()) or
                    not isinstance(pending.get("registration_key"), str) or
                    not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", pending["registration_key"])):
                raise EnrollmentError("invalid_pending_registration")
        else:
            pending = {"schema_version": 1, **selection, "registration_key": secrets.token_urlsafe(32)}
            configure.atomic_private_json(pending_path, pending)
        configure.atomic_private_json(selection_path, selection)
        # Nothing is sent until the same key, mailbox, endpoint and device are
        # durably saved. Every retry uses this exact request, never a new email.
        ack, attempts = client.perform_with_retries(
            lambda seconds: request_enrollment(base, email, device, pending["registration_key"], pin, seconds))
        configure.save_enrolled_configuration(email, ack["upload_token"], base, target, device,
                                                connect_ip=pin, expected_existing=expected_existing)
        remote = client.resolve_remote(config_path=target, codex_home=codex_home)
        remote.update(enrollment_status="enrolled", enrollment_attempts=attempts,
                      enrollment_repeated=ack["enrollment_repeated"])
        return remote
    finally:
        lock.unlink()


def safe_result(remote):
    return {"ok": True, "status": remote.get("enrollment_status", "ready"),
            "email_masked": configure.mask_email(remote["expected_email"]) if remote.get("expected_email") else None,
            "device_id": remote["device_id"], "identity_assurance": "self_reported" if remote.get("enrollment_mode") == "self_reported" else "legacy_configured",
            "notice": "Internal self-report only; this does not verify mailbox ownership or historical OpenAI account usage."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Destination override; defaults to existing config or the team service")
    parser.add_argument("--config", help="Private config path; state is kept in this directory, separately from Codex login/logs")
    parser.add_argument("--codex-home")
    parser.add_argument("--codex-executable")
    parser.add_argument("--connect-ip", help="Optional literal IP pin, retaining the original domain and TLS verification")
    args = parser.parse_args(argv)
    try:
        remote = ensure_remote(args.config, args.codex_home, args.codex_executable, args.url,
                               args.connect_ip, allow_account_switch=True)
        result = safe_result(remote)
    except EnrollmentError as error:
        result = {"ok": False, "status": "enrollment_stopped", "reason": str(error)}
    except HTTPError as error:
        result = {"ok": False, "status": "enrollment_failed", "reason": "http_error_same_registration_key_preserved", "http_status": error.code}
    except (URLError, TimeoutError, OSError):
        result = {"ok": False, "status": "enrollment_failed", "reason": "network_or_state_error_same_registration_key_preserved"}
    except (ValueError, TypeError):
        result = {"ok": False, "status": "enrollment_failed", "reason": "invalid_configuration_or_ack"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
