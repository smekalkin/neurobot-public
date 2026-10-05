import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neurobot import relogin  # noqa: E402


AUTHORIZE_URL = (
    "https://claude.com/cai/oauth/authorize?code=true"
    "&client_id=example-client-id&response_type=code"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&code_challenge_method=S256&state=example-state"
)

# Stands in for `claude auth login --claudeai`: prints the same lines in the
# same order the real CLI does (verified live against Claude Code 2.1.231),
# then waits on a tty prompt for the pasted code. Lets the whole pty state
# machine be exercised without touching Anthropic or any real account.
FAKE_CLI = """#!/usr/bin/env python3
import sys, time
assert sys.argv[1:4] == ["auth", "login", "--claudeai"], sys.argv
sys.stdout.write("Opening browser to sign in\\u2026\\n")
sys.stdout.flush()
time.sleep(0.2)
sys.stdout.write("If the browser didn't open, visit: {url}\\n")
sys.stdout.write("Paste code here if prompted > ")
sys.stdout.flush()
line = sys.stdin.readline().strip()
sys.stdout.write("{outcome}\\n")
sys.stdout.flush()
time.sleep(0.2)
"""


class UrlExtractionTests(unittest.TestCase):
    def test_partial_write_is_not_captured(self):
        # The live PoC captured the URL mid-write and lost the &state= suffix,
        # which silently produces an unusable link. Nothing may be returned
        # until the line is terminated.
        partial = "If the browser didn't open, visit: " + AUTHORIZE_URL[:-20]
        self.assertIsNone(relogin.extract_authorize_url(partial))

    def test_complete_line_is_captured(self):
        text = "visit: {}\nPaste code here if prompted > ".format(AUTHORIZE_URL)
        self.assertEqual(relogin.extract_authorize_url(text), AUTHORIZE_URL)

    def test_longest_complete_match_wins(self):
        text = "{}\n{}\n".format(AUTHORIZE_URL[:-20], AUTHORIZE_URL)
        self.assertEqual(relogin.extract_authorize_url(text), AUTHORIZE_URL)

    def test_ansi_escapes_are_stripped_before_matching(self):
        raw = "\x1b[2mvisit: {}\x1b[0m\r\n".format(AUTHORIZE_URL)
        self.assertEqual(
            relogin.extract_authorize_url(relogin.strip_ansi(raw)), AUTHORIZE_URL
        )


class SummarizeTests(unittest.TestCase):
    def test_failure_line_is_cut_at_the_marker(self):
        clean = (
            "Paste code here if prompted > Login failed: "
            "Request failed with status code 400\n"
        )
        self.assertEqual(
            relogin.summarize(clean),
            "Login failed: Request failed with status code 400",
        )

    def test_echoed_code_is_redacted(self):
        code = "abcd1234efgh#state9876"
        clean = "Paste code here if prompted > {}\nLogin failed: bad {}\n".format(
            code, code
        )
        summary = relogin.summarize(clean, code)
        self.assertNotIn(code, summary)
        self.assertNotIn("abcd1234efgh", summary)
        self.assertIn("Login failed:", summary)

    def test_success_marker_is_reported(self):
        self.assertIn("Login successful", relogin.summarize("Login successful.\n"))


class SupervisorProcessTests(unittest.TestCase):
    """Drives the real detached supervisor + real pty against a fake CLI."""

    def setUp(self):
        # start_supervisor detaches on purpose and never waits on the child, so
        # Popen.__del__ warns about a process it no longer owns. Expected here.
        warnings.simplefilter("ignore", ResourceWarning)
        self.tmp = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.tmp.name, "relogin.sock")
        self.log_path = os.path.join(self.tmp.name, "relogin.log")

    def tearDown(self):
        relogin.terminate(self.socket_path)
        self.tmp.cleanup()

    def fake_cli(self, outcome):
        path = os.path.join(self.tmp.name, "fake-claude")
        Path(path).write_text(
            FAKE_CLI.format(url=AUTHORIZE_URL, outcome=outcome), encoding="utf-8"
        )
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def start(self, outcome, timeout=30.0):
        pid = relogin.start_supervisor(
            socket_path=self.socket_path,
            claude_bin=self.fake_cli(outcome),
            session_timeout=timeout,
            log_path=self.log_path,
        )
        self.addCleanup(relogin.terminate, self.socket_path, pid)
        return pid

    def wait_gone(self, pid, budget=15.0):
        # The supervisor is a direct child of the test runner, so it lingers as
        # a zombie until reaped — kill(pid, 0) would still succeed. In
        # production it is reparented to init the moment the one-shot gateway
        # exits. Reap it here so "gone" means gone.
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return True
            if waited == pid:
                return True
            time.sleep(0.1)
        return False

    def test_captures_url_then_reports_failed_exchange(self):
        pid = self.start("Login failed: Request failed with status code 400")
        status = relogin.wait_for_url(self.socket_path, budget=20.0)
        self.assertEqual(status.get("url"), AUTHORIZE_URL)
        self.assertEqual(status.get("state"), relogin.STATE_AWAITING_CODE)

        result = relogin.submit_code(self.socket_path, "bogus1234#bogusstate")
        self.assertFalse(result["ok"])
        self.assertIn("status code 400", result["detail"])
        # A finished attempt tears itself down: no stray pty, no stale socket.
        self.assertTrue(self.wait_gone(pid))
        self.assertFalse(os.path.exists(self.socket_path))

    def test_successful_exchange_is_reported(self):
        pid = self.start("Login successful.")
        status = relogin.wait_for_url(self.socket_path, budget=20.0)
        self.assertEqual(status.get("url"), AUTHORIZE_URL)
        result = relogin.submit_code(self.socket_path, "good1234#goodstate")
        self.assertTrue(result["ok"])
        self.assertIn("Login successful", result["detail"])
        self.assertTrue(self.wait_gone(pid))

    def test_pasted_code_never_reaches_the_log(self):
        pid = self.start("Login failed: Request failed with status code 400")
        relogin.wait_for_url(self.socket_path, budget=20.0)
        relogin.submit_code(self.socket_path, "s3cr3tcode#s3cr3tstate")
        self.wait_gone(pid)
        log = Path(self.log_path).read_text(encoding="utf-8")
        self.assertNotIn("s3cr3tcode", log)
        self.assertNotIn("s3cr3tstate", log)
        self.assertIn("code submitted", log)

    def test_abandoned_attempt_times_out_and_cleans_up(self):
        pid = self.start("Login failed: never reached", timeout=3.0)
        relogin.wait_for_url(self.socket_path, budget=20.0)
        # Nobody ever pastes anything: the supervisor and the CLI behind it must
        # both disappear on their own rather than leaking a pty forever.
        self.assertTrue(self.wait_gone(pid, budget=20.0))
        self.assertFalse(os.path.exists(self.socket_path))
        marker = str(self.fake_cli).encode()

        def helper_is_running():
            for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    if marker in cmdline.read_bytes():
                        return True
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
            return False

        deadline = time.monotonic() + 5.0
        while helper_is_running() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(helper_is_running())

    def test_terminate_kills_a_running_attempt(self):
        pid = self.start("Login failed: never reached", timeout=120.0)
        relogin.wait_for_url(self.socket_path, budget=20.0)
        relogin.terminate(self.socket_path, pid)
        self.assertTrue(self.wait_gone(pid))
        self.assertFalse(os.path.exists(self.socket_path))


if __name__ == "__main__":
    unittest.main()
