"""Configure a private local upload destination from an existing upload-token file."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import uuid
from urllib.parse import urlsplit, urlunsplit

import client

DEFAULT_URL = "https://codex.wenspock.site"


def mask_email(email):
    local, domain = email.rsplit("@", 1)
    return local[0] + "***@" + domain


def normalized_url(value):
    client.upload_url(value)
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def atomic_private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def configure(email, token_file, url=DEFAULT_URL, codex_home=None, replace=False):
    if not isinstance(email, str) or len(email) > 320 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip()):
        raise ValueError("invalid_email")
    email = email.strip().casefold()
    base = normalized_url(url)
    # Always an explicit file, never an OpenAI credential file or an inline key.
    # The supplied file is the user-issued upload token for this statistics service.
    source = Path(token_file)
    if source.resolve().name.casefold() == "auth.json":
        raise ValueError("openai_auth_file_not_allowed")
    if source.stat().st_size > 64 * 1024:
        raise ValueError("invalid_upload_token_file")
    token = client.load_token(source)
    state = client.usage_state_dir(codex_home)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = state / ".configure.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("configuration_busy_retry_later") from None
    os.close(descriptor)
    try:
        return _configure_state(email, token, base, state, replace)
    finally:
        lock.unlink()


def _configure_state(email, token, base, state, replace, config_path=None, extra_fields=None, expected_existing=None):
    config_path, managed_token_path = Path(config_path) if config_path else state / "remote.json", state / "upload-token.json"
    if config_path.is_symlink() or managed_token_path.is_symlink():
        raise ValueError("configuration_symlink_not_allowed")
    old = None
    same = False
    if config_path.exists():
        try:
            old, _ = client.load_remote_config(config_path)
            old_token_path = old.get("token_file")
            if old_token_path and Path(old_token_path).resolve().name.casefold() == "auth.json":
                raise ValueError("openai_auth_file_not_allowed")
            try:
                old_token = client.load_token(old_token_path) if old_token_path else None
            except (ValueError, OSError):
                old_token = None
            same = normalized_url(old.get("url", "")) == base and old.get("email", "").casefold() == email and old_token == token
            if extra_fields and any(old.get(key) != value for key, value in extra_fields.items()):
                same = False
        except (ValueError, OSError, TypeError):
            old = None
        if expected_existing is not None:
            actual_identity = (normalized_url(old.get("url", "")), old.get("email", "").casefold(), old.get("enrollment_mode")) if old else None
            if actual_identity != expected_existing:
                raise ValueError("configuration_changed_during_enrollment")
        if same:
            return {"ok": True, "status": "already_configured", "email_masked": mask_email(email),
                    "url": base, "config_path": str(config_path), "credentials_changed": False}
        if not replace:
            raise ValueError("configuration_exists_use_replace")
    elif managed_token_path.exists() and not replace:
        raise ValueError("token_state_exists_use_replace")
    # A replace writes a fresh token file, then atomically switches the remote
    # pointer. Concurrent report readers keep using the intact old pair until
    # the switch; a failed config write cannot send a new key to an old URL.
    token_name = "upload-token-" + uuid.uuid4().hex + ".json" if config_path.exists() or managed_token_path.exists() else "upload-token.json"
    target_token = state / token_name
    config = {"url": base, "token_file": token_name, "email": email}
    if old:
        if old.get("device_id"):
            config["device_id"] = old["device_id"]
        if old.get("connect_ip") and normalized_url(old.get("url", "")) == base:
            config["connect_ip"] = old["connect_ip"]
    if extra_fields:
        config.update(extra_fields)
    # Device state is deliberately not created, replaced or removed here.
    atomic_private_json(target_token, {"upload_token": token})
    try:
        atomic_private_json(config_path, config)
    except BaseException:
        target_token.unlink(missing_ok=True)
        raise
    return {"ok": True, "status": "configured", "email_masked": mask_email(email),
            "url": base, "config_path": str(config_path), "credentials_changed": True,
            "account_scope": "Reporting email only; no login or historical OpenAI-account attribution."}


def save_enrolled_configuration(email, token, base, config_path, device, connect_ip=None, expected_existing=None):
    """Internal validated enrollment save; never print a registration or upload key."""
    if not isinstance(token, str) or not 24 <= len(token) <= 256 or any(c.isspace() for c in token):
        raise ValueError("invalid_enrollment_token")
    target = Path(config_path)
    state = target.parent
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = state / ".configure.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError("configuration_busy_retry_later") from None
    os.close(descriptor)
    try:
        # No pre-existing config was observed: do not overwrite one that raced us.
        if expected_existing is None and target.exists():
            raise ValueError("configuration_changed_during_enrollment")
        extra = {"device_id": device, "enrollment_mode": "self_reported", "identity_assurance": "self_reported"}
        if connect_ip:
            extra["connect_ip"] = client.validate_connect_ip(connect_ip)
        return _configure_state(email, token, normalized_url(base), state, True, target, extra, expected_existing)
    finally:
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="Reporting mailbox; not an OpenAI login operation")
    parser.add_argument("--token-file", required=True, help="Existing raw token or JSON {upload_token: ...}; never put a token on the command line")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--codex-home")
    parser.add_argument("--replace", action="store_true", help="Explicitly replace a different existing local destination/token")
    args = parser.parse_args(argv)
    try:
        result = configure(args.email, args.token_file, args.url, args.codex_home, args.replace)
    except ValueError as error:
        reason = str(error) if str(error) in {"invalid_email", "openai_auth_file_not_allowed", "invalid_upload_token_file", "configuration_exists_use_replace", "token_state_exists_use_replace", "configuration_busy_retry_later", "configuration_symlink_not_allowed"} else "invalid_configuration_input"
        result = {"ok": False, "status": "error", "reason": reason}
    except (OSError, TypeError):
        result = {"ok": False, "status": "error", "reason": "configuration_file_unavailable"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
