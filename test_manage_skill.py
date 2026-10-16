import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import manage_skill


class ManageSkillTests(unittest.TestCase):
    def test_explicit_enroll_uses_installed_script_and_separate_private_config(self):
        dest = self.home / "installed-skill"
        config = self.home / "independent-state" / "remote.json"
        safe = {"ok": True, "status": "enrolled", "email_masked": "w***@example.test", "identity_assurance": "self_reported"}
        process = mock.Mock(stdout=json.dumps({**safe, "upload_token": "PRIVATE-MUST-NOT-PRINT"}))
        with mock.patch("scripts.manage_skill.subprocess.run", return_value=process) as run:
            result = manage_skill.enroll_installed(dest, codex_home=self.home / "login", config=config)
        command = run.call_args.args[0]
        self.assertEqual(Path(command[1]), dest / "scripts" / "enrollment.py")
        self.assertIn(str(config), command)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertNotIn("PRIVATE-MUST-NOT-PRINT", json.dumps(result))
        self.assertEqual(result, safe)

    def test_actual_repository_bundle_matches_canonical_sources(self):
        actual_root = Path(__file__).resolve().parent
        self.assertTrue(manage_skill.sync_bundle(actual_root, check=True)["ok"],
                        "Run scripts/manage_skill.py sync after changing canonical sources")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.home = Path(self.temp.name) / "isolated-home"
        self.home.mkdir()
        actual = Path(__file__).resolve().parent
        for name in manage_skill.CANONICAL_FILES:
            shutil.copyfile(actual / name, self.root / name)
        bundle = self.root / "skills" / "codex-usage"
        (bundle / "agents").mkdir(parents=True)
        (bundle / "SKILL.md").write_text("---\nname: codex-usage\ndescription: fixture\n---\n", encoding="utf-8")
        (bundle / "agents" / "openai.yaml").write_text("interface:\n  display_name: Codex usage\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_sync_is_allowlisted_and_check_detects_drift(self):
        (self.root / "private-token.json").write_text("PRIVATE-NOT-FOR-BUNDLE")
        (self.root / "test_secret.py").write_text("PRIVATE-NOT-FOR-BUNDLE")
        self.assertFalse(manage_skill.sync_bundle(self.root, check=True)["ok"])
        self.assertTrue(manage_skill.sync_bundle(self.root)["ok"])
        self.assertTrue(manage_skill.sync_bundle(self.root, check=True)["ok"])
        bundle_scripts = self.root / "skills" / "codex-usage" / "scripts"
        self.assertEqual({p.name for p in bundle_scripts.iterdir()}, set(manage_skill.CANONICAL_FILES))
        for name in manage_skill.CANONICAL_FILES:
            self.assertEqual((bundle_scripts / name).read_bytes(), (self.root / name).read_bytes())
        (self.root / "client.py").write_text("# changed canonical\n", encoding="utf-8")
        self.assertEqual(manage_skill.sync_bundle(self.root, check=True)["mismatches"], ["client.py"])

    def test_installed_bundle_runs_without_repository_and_never_reads_private_state(self):
        manage_skill.sync_bundle(self.root)
        installed = manage_skill.install_bundle(self.root, home=self.home)
        dest = Path(installed["destination"])
        self.assertEqual(dest, self.home / ".agents" / "skills" / "codex-usage")
        work = self.home / "workspace"
        work.mkdir()
        empty_codex = self.home / "empty-codex"
        process = subprocess.run([sys.executable, str(dest / "scripts" / "report.py"), "run", "--no-upload",
                                  "--codex-home", str(empty_codex), "--output-dir", str(work / "reports"),
                                  "--now", "2026-10-16T17:38:34+08:00"], cwd=work, capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        data = json.loads(process.stdout)
        self.assertEqual(data["status"], "local_only")
        self.assertTrue(Path(data["report_path"]).is_file())
        self.assertFalse(any(dest.glob("**/remote.json")))
        process = subprocess.run([sys.executable, str(dest / "scripts" / "configure.py"), "--help"], cwd=work, capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 0)

    def test_upgrade_backs_up_managed_changes_and_preserves_unknown_and_state(self):
        manage_skill.sync_bundle(self.root)
        dest = self.home / "custom-skill"
        manage_skill.install_bundle(self.root, dest=dest)
        original = (dest / "scripts" / "collector.py").read_bytes()
        unknown = dest / "local-notes.txt"
        unknown.write_text("keep unknown")
        private = self.home / ".codex" / "codex-usage"
        private.mkdir(parents=True)
        for name in ("remote.json", "upload-token.json", "device-id.txt"):
            (private / name).write_text("PRIVATE-STATE-" + name)
        # An unknown file inside the skill is retained too, without opening it.
        (dest / "remote.json").write_text("PRIVATE-LOCAL-UNKNOWN")
        with (self.root / "collector.py").open("ab") as stream:
            stream.write(b"\n# fixture upgrade\n")
        manage_skill.sync_bundle(self.root)
        result = manage_skill.install_bundle(self.root, dest=dest)
        self.assertEqual(result["changed"], ["scripts/collector.py"])
        backup = Path(result["backup"])
        self.assertEqual(backup.parent, self.root / ".local" / "skill-install-backups")
        self.assertFalse(backup.is_relative_to(dest))
        self.assertFalse(backup.is_relative_to(self.home / ".agents" / "skills"))
        self.assertFalse(backup.is_relative_to(self.home / ".codex" / "skills"))
        self.assertEqual((backup / "scripts" / "collector.py").read_bytes(), original)
        self.assertEqual(unknown.read_text(), "keep unknown")
        self.assertEqual((dest / "remote.json").read_text(), "PRIVATE-LOCAL-UNKNOWN")
        for name in ("remote.json", "upload-token.json", "device-id.txt"):
            self.assertEqual((private / name).read_text(), "PRIVATE-STATE-" + name)
        self.assertEqual(manage_skill.install_bundle(self.root, dest=dest)["changed"], [])

    def test_legacy_only_is_updated_and_duplicates_require_explicit_choice(self):
        manage_skill.sync_bundle(self.root)
        legacy = self.home / ".codex" / "skills" / "codex-usage"
        modern = self.home / ".agents" / "skills" / "codex-usage"
        legacy.mkdir(parents=True)
        self.assertEqual(Path(manage_skill.install_bundle(self.root, home=self.home)["destination"]), legacy)
        self.assertFalse(modern.exists())
        modern.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "duplicate_skill_destinations"):
            manage_skill.install_bundle(self.root, home=self.home)
        result = manage_skill.install_bundle(self.root, dest=modern, home=self.home)
        self.assertEqual(Path(result["destination"]), modern)
        self.assertTrue((legacy / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
