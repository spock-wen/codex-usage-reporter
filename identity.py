"""Read local Codex account identity through a bounded, read-only app-server RPC."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

MAX_LINE_BYTES = 1024 * 1024


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def resolve_executable(executable=None):
    candidate = executable or shutil.which("codex.exe") or shutil.which("codex")
    if not executable and (not candidate or Path(candidate).suffix.lower() in (".cmd", ".bat", ".ps1")):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            vendor = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
            try:
                native = [p for p in vendor.glob("*/codex.exe") if p.is_file() and not p.is_symlink()]
                if native:
                    candidate = max(native, key=lambda p: p.stat().st_mtime_ns)
            except OSError:
                pass
    if not candidate:
        return None, "executable_not_found"
    found = shutil.which(str(candidate)) or (str(Path(candidate).resolve()) if Path(candidate).is_file() else None)
    if not found:
        return None, "executable_not_found"
    # A .cmd/.bat wrapper invokes a shell and can add unrelated startup behavior.
    # Prefer a native sibling if provided; never concatenate a shell command.
    path = Path(found)
    if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        sibling = path.with_suffix(".exe")
        if sibling.is_file():
            path = sibling
        else:
            return None, "native_codex_executable_required"
    return str(path.resolve()), None


def _rpc_account(command, codex_home=None, timeout=10.0):
    """Run the only permitted RPC sequence. Never return raw protocol or stderr."""
    env = os.environ.copy()
    if codex_home:
        env["CODEX_HOME"] = str(Path(codex_home).resolve())
    flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0) |
             getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env, shell=False,
                                   creationflags=flags, bufsize=0)
    except (OSError, ValueError):
        return None, "start_failed"
    messages = queue.Queue(maxsize=128)
    stopping = threading.Event()

    def push(item):
        while not stopping.is_set():
            try:
                messages.put(item, timeout=.05)
                return
            except queue.Full:
                pass

    def read_stdout():
        try:
            while not stopping.is_set():
                line = process.stdout.readline(MAX_LINE_BYTES + 1)
                if not line:
                    push(("eof", None))
                    return
                if len(line) > MAX_LINE_BYTES:
                    push(("bad", None))
                    return
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    push(("bad", None))
                    continue
                if isinstance(value, dict):
                    push(("message", value))
                else:
                    push(("bad", None))
        except (OSError, ValueError):
            push(("eof", None))

    reader = threading.Thread(target=read_stdout, name="codex-identity-rpc-reader", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout

    def send(value):
        process.stdin.write((json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8"))
        process.stdin.flush()

    def receive(expected_id):
        invalid_lines = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "timeout"
            try:
                kind, message = messages.get(timeout=remaining)
            except queue.Empty:
                return None, "timeout"
            if kind == "eof":
                return None, "process_ended_before_response"
            if kind == "bad":
                invalid_lines += 1
                if invalid_lines >= 8:
                    return None, "invalid_protocol"
                continue
            # Notifications and unrelated responses are safe to ignore.
            # Server-initiated requests are not answered or acted upon.
            if message.get("id") != expected_id or isinstance(message.get("id"), bool):
                continue
            if "error" in message:
                return None, "rpc_error"
            if "result" not in message:
                return None, "invalid_response"
            return message["result"], None

    try:
        send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex_usage_identity", "version": "1.0.0"}, "capabilities": {}}})
        initialized, error = receive(1)
        if error:
            return None, error
        if not isinstance(initialized, dict):
            return None, "invalid_initialize_response"
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "account/read", "params": {"refreshToken": False}})
        return receive(2)
    except (OSError, ValueError):
        return None, "protocol_write_failed"
    finally:
        stopping.set()
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        try:
            process.stdout.close()
        except (OSError, ValueError):
            pass
        reader.join(timeout=.5)


def read_account(executable=None, codex_home=None, include_email=False, timeout=10.0, *, _command=None):
    result = {"schema_version": 1, "status": "error", "queried_at": timestamp(),
              "login_type": None, "email": None, "email_is_masked": not include_email,
              "plan_type": None, "account_key": None, "email_candidate_key": None,
              "identity_assurance": "local_read_only_not_verified",
              "source": "new_local_codex_app_server",
              "source_scope": "local_login_visible_to_new_process_not_current_desktop_authorization",
              "codex_home_override": bool(codex_home), "reason_code": None}
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 120:
        result["reason_code"] = "invalid_timeout"
        return result
    if _command is None:
        binary, error = resolve_executable(executable)
        if error:
            result.update(status="unavailable", reason_code=error)
            return result
        command = [binary, "app-server"]
    else:
        command = _command  # Test seam for a real fake-RPC subprocess; never exposed by CLI.
    payload, error = _rpc_account(command, codex_home, timeout)
    if error:
        result.update(status="timeout" if error == "timeout" else "error", reason_code=error)
        return result
    if not isinstance(payload, dict) or "account" not in payload:
        result["reason_code"] = "invalid_account_response"
        return result
    account = payload["account"]
    if account is None:
        result.update(status="no_account", reason_code="no_local_account")
        return result
    if not isinstance(account, dict):
        result["reason_code"] = "invalid_account_shape"
        return result
    kind = account.get("type")
    if kind == "apiKey":
        result.update(status="api_key", login_type="apiKey", reason_code="api_key_has_no_chatgpt_identity")
        return result
    if kind != "chatgpt":
        result["reason_code"] = "unsupported_account_type"
        return result
    result.update(status="ok", login_type="chatgpt", reason_code="no_verified_stable_account_id")
    plan = account.get("planType")
    if isinstance(plan, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", plan):
        result["plan_type"] = plan
    email = account.get("email")
    if isinstance(email, str) and len(email) <= 320 and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        local, domain = email.rsplit("@", 1)
        result["email"] = email if include_email else local[0] + "***@" + domain
        result["email_candidate_key"] = "sha256-email-candidate:" + hashlib.sha256(email.strip().casefold().encode()).hexdigest()
    # This documented account/read schema contains no verified stable account ID.
    # Email hashes may assist matching, but never prove an authenticated person.
    return result


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=path.name + ".", suffix=".tmp",
                                         dir=path.parent, delete=False)
    tmp = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    account = sub.add_parser("account", help="Read current local app-server login; no login, refresh or model call")
    account.add_argument("--codex-executable")
    account.add_argument("--codex-home")
    account.add_argument("--include-email", action="store_true", help="Include full email; default masks the local part")
    account.add_argument("--timeout", type=float, default=10)
    account.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        result = read_account(args.codex_executable, args.codex_home, args.include_email, args.timeout)
        if getattr(args, "output", None):
            _atomic_json(Path(args.output), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError):
        print(json.dumps({"status": "error", "reason_code": "invalid_output"}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
