import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import identity


FAKE_RPC = r'''
import json, os, sys, time
mode, trace = sys.argv[1:3]
def write(value):
    print(json.dumps(value), flush=True)
def read():
    value = json.loads(sys.stdin.readline())
    with open(trace, 'a', encoding='utf-8') as f:
        f.write(json.dumps(value) + '\n')
    return value
init = read()
assert init['method'] == 'initialize'
assert set(init) == {'id', 'method', 'params'}
if mode == 'timeout':
    time.sleep(30)
    sys.exit()
if mode == 'eof':
    sys.exit()
if mode == 'init_error':
    write({'id': 1, 'error': {'message': 'SUPER-SECRET-RAW-ERROR'}})
    sys.exit()
if mode == 'bad_init':
    write({'id': 1, 'result': []})
    sys.exit()
print('SUPER-SECRET-STDERR', file=sys.stderr, flush=True)
write({'method': 'notification', 'params': {'access_token': 'SUPER-SECRET-NOTIFICATION'}})
print('non-json-log-line', flush=True)
write({'id': 999, 'result': {'access_token': 'SUPER-SECRET-UNRELATED'}})
write({'id': 1, 'result': {'userAgent': 'fake'}})
notification = read()
assert notification['method'] == 'initialized'
account = read()
assert account == {'id': 2, 'method': 'account/read', 'params': {'refreshToken': False}}
if mode == 'account_error':
    write({'id': 2, 'error': {'message': 'SUPER-SECRET-RPC-ERROR'}})
elif mode == 'no_account':
    write({'id': 2, 'result': {'account': None, 'requiresOpenaiAuth': True}})
elif mode == 'apikey':
    write({'id': 2, 'result': {'account': {'type': 'apiKey', 'apiKey': 'SUPER-SECRET-APIKEY'}}})
elif mode == 'wrongshape':
    write({'id': 2, 'result': ['SUPER-SECRET-SHAPE']})
elif mode == 'wrong_account_shape':
    write({'id': 2, 'result': {'account': 'SUPER-SECRET-ACCOUNT'}})
elif mode == 'missing_account':
    write({'id': 2, 'result': {'secret': 'SUPER-SECRET-MISSING'}})
elif mode == 'unsupported':
    write({'id': 2, 'result': {'account': {'type': 'SUPER-SECRET-TYPE'}}})
else:
    email = None if mode == 'null_email' else 'worker@example.test'
    if mode == 'home':
        assert os.environ.get('CODEX_HOME') == sys.argv[3]
    write({'id': 2, 'result': {'account': {'type': 'chatgpt', 'email': email,
           'planType': 'pro', 'accessToken': 'SUPER-SECRET-TOKEN', 'id': 'SUPER-SECRET-ID'},
           'access_token': 'SUPER-SECRET-TOPLEVEL'}})
'''


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fake = self.root / "fake_rpc.py"
        self.fake.write_text(FAKE_RPC, encoding="utf-8")
        self.trace = self.root / "trace.jsonl"

    def tearDown(self):
        self.temp.cleanup()

    def run_fake(self, mode="success", **kwargs):
        return identity.read_account(_command=[sys.executable, str(self.fake), mode, str(self.trace)], **kwargs)

    def test_success_masked_whitelist_and_exact_read_only_rpc_sequence(self):
        result = self.run_fake()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["login_type"], "chatgpt")
        self.assertEqual(result["email"], "w***@example.test")
        self.assertEqual(result["plan_type"], "pro")
        self.assertIsNone(result["account_key"])
        self.assertTrue(result["email_candidate_key"].startswith("sha256-email-candidate:"))
        self.assertEqual(result["identity_assurance"], "local_read_only_not_verified")
        output = json.dumps(result)
        self.assertNotIn("worker@example.test", output)
        self.assertNotIn("SUPER-SECRET", output)
        sent = [json.loads(line) for line in self.trace.read_text().splitlines()]
        self.assertEqual([r["method"] for r in sent], ["initialize", "initialized", "account/read"])
        self.assertEqual(sent[-1]["params"], {"refreshToken": False})

    def test_opt_in_email_does_not_turn_hash_into_verified_account_id(self):
        result = self.run_fake(include_email=True)
        self.assertEqual(result["email"], "worker@example.test")
        self.assertFalse(result["email_is_masked"])
        self.assertIsNone(result["account_key"])
        self.assertEqual(result["reason_code"], "no_verified_stable_account_id")

    def test_no_account_apikey_and_email_null(self):
        for mode, status in (("no_account", "no_account"), ("apikey", "api_key"), ("null_email", "ok")):
            with self.subTest(mode=mode):
                result = self.run_fake(mode)
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["email"])
                self.assertIsNone(result["email_candidate_key"])
                self.assertNotIn("SUPER-SECRET", json.dumps(result))

    def test_rpc_error_and_wrong_shapes_have_only_safe_reason_codes(self):
        for mode in ("init_error", "bad_init", "account_error", "wrongshape", "wrong_account_shape",
                     "missing_account", "unsupported", "eof"):
            with self.subTest(mode=mode):
                result = self.run_fake(mode)
                self.assertEqual(result["status"], "error")
                self.assertIsNotNone(result["reason_code"])
                self.assertNotIn("SUPER-SECRET", json.dumps(result))

    def test_timeout_terminates_process_and_leaves_no_reader(self):
        actual_popen = subprocess.Popen
        children = []
        def tracked_popen(*args, **kwargs):
            child = actual_popen(*args, **kwargs)
            children.append(child)
            self.assertFalse(kwargs["shell"])
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            return child
        started = time.monotonic()
        with mock.patch.object(identity.subprocess, "Popen", side_effect=tracked_popen):
            result = self.run_fake("timeout", timeout=.15)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue(all(p.poll() is not None for p in children))
        self.assertFalse(any(t.name == "codex-identity-rpc-reader" for t in __import__('threading').enumerate()))

    def test_codex_home_only_changes_child_environment(self):
        original = dict(identity.os.environ)
        home = str((self.root / "other-home").resolve())
        result = identity.read_account(codex_home=home, _command=[sys.executable, str(self.fake), "home", str(self.trace), home])
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["codex_home_override"])
        self.assertEqual(dict(identity.os.environ), original)

    def test_missing_executable_and_shell_wrappers_fail_safely(self):
        result = identity.read_account(executable=str(self.root / "missing.exe"))
        self.assertEqual(result["status"], "unavailable")
        wrapper = self.root / "codex & do-not-run.cmd"
        wrapper.write_text("DO NOT EXECUTE", encoding="utf-8")
        path, error = identity.resolve_executable(wrapper)
        self.assertIsNone(path)
        self.assertEqual(error, "native_codex_executable_required")

    def test_timeout_validation_and_output_file_is_only_whitelist(self):
        for timeout in (0, -1, True, 121, float("nan")):
            self.assertEqual(self.run_fake(timeout=timeout)["reason_code"], "invalid_timeout")
        target = self.root / "result.json"
        safe = self.run_fake()
        with mock.patch.object(identity, "read_account", return_value=safe), contextlib.redirect_stdout(io.StringIO()) as out:
            code = identity.main(["account", "--output", str(target)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), safe)
        self.assertNotIn("SUPER-SECRET", out.getvalue())
        self.assertFalse(list(self.root.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
