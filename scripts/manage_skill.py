"""Sync and install the allowlisted, self-contained Codex usage skill bundle."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

CANONICAL_FILES = ("collector.py", "identity.py", "client.py", "report.py", "configure.py", "enrollment.py", "prices.json")
PACKAGE_FILES = ("SKILL.md", "agents/openai.yaml", *("scripts/" + name for name in CANONICAL_FILES))


def repository_root():
    return Path(__file__).resolve().parent.parent


def sync_bundle(root=None, check=False):
    root = Path(root) if root else repository_root()
    bundle = root / "skills" / "codex-usage"
    mismatches = []
    for name in CANONICAL_FILES:
        source, target = root / name, bundle / "scripts" / name
        if not source.is_file() or source.is_symlink():
            raise ValueError("canonical_source_missing_or_symlink")
        if target.is_symlink():
            raise ValueError("managed_target_is_symlink")
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            mismatches.append(name)
    if not check:
        for name in mismatches:
            target = bundle / "scripts" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            _replace_file(root / name, target)
    return {"ok": not mismatches if check else True, "mode": "check" if check else "sync",
            "managed_files": list(CANONICAL_FILES), "changed" if not check else "mismatches": mismatches}


def default_destination(home=None):
    home = Path(home) if home else Path.home()
    modern = home / ".agents" / "skills" / "codex-usage"
    legacy = home / ".codex" / "skills" / "codex-usage"
    if modern.exists() and legacy.exists():
        raise ValueError("duplicate_skill_destinations_choose_explicit_dest")
    return legacy if legacy.exists() and not modern.exists() else modern


def _replace_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(dir=target.parent, prefix="." + target.name + ".", suffix=".tmp", delete=False)
    temp = Path(handle.name)
    handle.close()
    try:
        shutil.copyfile(source, temp)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()


def install_bundle(root=None, dest=None, home=None):
    root = Path(root) if root else repository_root()
    bundle = root / "skills" / "codex-usage"
    destination = Path(dest).expanduser() if dest else default_destination(home)
    destination = destination.absolute()
    if not sync_bundle(root, check=True)["ok"]:
        raise ValueError("bundle_out_of_sync_run_sync_first")
    changes, existing = [], []
    for relative in PACKAGE_FILES:
        source, target = bundle / relative, destination / relative
        if not source.is_file() or source.is_symlink():
            raise ValueError("package_file_missing_or_symlink")
        for parent in (target, *target.parents):
            if parent == destination.parent:
                break
            if parent.is_symlink():
                raise ValueError("managed_target_is_symlink")
        if target.exists() and not target.is_file():
            raise ValueError("managed_target_is_not_file")
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            changes.append(relative)
            if target.is_file():
                existing.append(relative)
    backup = None
    if existing:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        # Keep old SKILL.md files outside every installed skill-discovery tree.
        backup = root / ".local" / "skill-install-backups" / stamp
        for relative in existing:
            target = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination / relative, target)
    for relative in changes:
        _replace_file(bundle / relative, destination / relative)
    return {"ok": True, "mode": "install", "destination": str(destination), "changed": changes,
            "backup": str(backup) if backup else None,
            "notice": "Only managed skill files are updated; private state, credentials and unknown files are untouched."}


def enroll_installed(destination, codex_executable=None, codex_home=None, config=None, url=None, connect_ip=None):
    command = [sys.executable, str(Path(destination) / "scripts" / "enrollment.py")]
    for flag, value in (("--codex-executable", codex_executable), ("--codex-home", codex_home),
                        ("--config", config), ("--url", url), ("--connect-ip", connect_ip)):
        if value:
            command.extend([flag, str(value)])
    kwargs = {"capture_output": True, "text": True, "timeout": 90, "shell": False}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.run(command, **kwargs)
        payload = json.loads(process.stdout)
        if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
            raise ValueError("invalid")
        # Enrollment CLI only emits its safe mailbox/device/status whitelist.
        safe = {k: payload[k] for k in ("ok", "status", "reason", "email_masked", "device_id", "identity_assurance", "notice", "http_status") if k in payload}
        return safe
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"ok": False, "status": "enrollment_failed", "reason": "installed_enrollment_process_failed"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="Copy only allowlisted canonical scripts/prices into the skill bundle")
    sync.add_argument("--check", action="store_true")
    install = sub.add_parser("install", help="Install/update the bundled skill without touching private user state")
    install.add_argument("--dest", help="Exact skill directory; default modern location, or existing legacy-only location")
    install.add_argument("--enroll", action="store_true", help="Explicitly read the current Codex mailbox and self-enroll after local installation")
    install.add_argument("--codex-executable")
    install.add_argument("--codex-home")
    install.add_argument("--config", help="Private auto-enrollment state/config path, separate from Codex login/logs")
    install.add_argument("--url")
    install.add_argument("--connect-ip")
    args = parser.parse_args(argv)
    try:
        result = sync_bundle(check=args.check) if args.command == "sync" else install_bundle(dest=args.dest)
        if args.command == "install" and args.enroll:
            result["enrollment"] = enroll_installed(result["destination"], args.codex_executable,
                                                    args.codex_home, args.config, args.url, args.connect_ip)
            result["ok"] = result["enrollment"]["ok"]
            result["notice"] = "Skill files are installed. Enrollment is an explicitly requested internal self-report; no mailbox ownership verification is claimed."
    except (ValueError, OSError) as error:
        known = {"canonical_source_missing_or_symlink", "managed_target_is_symlink", "managed_target_is_not_file",
                 "duplicate_skill_destinations_choose_explicit_dest", "bundle_out_of_sync_run_sync_first", "package_file_missing_or_symlink"}
        result = {"ok": False, "reason": str(error) if str(error) in known else "skill_file_operation_failed"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
