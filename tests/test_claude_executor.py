import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neurobot.claude_executor import ClaudeExecutorClient, ClaudeExecutorError  # noqa: E402


def fake_completed_process(returncode=0, stdout="", stderr=""):
    proc = Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class ClaudeExecutorErrorSurfacingTests(unittest.TestCase):
    """Structured provider errors must take precedence over harmless stderr
    warnings when the executor exits with a non-zero status.
    """

    def make_client(self):
        return ClaudeExecutorClient(
            workspace=Path("/tmp"),
            timeout=60,
            claude_bin="claude",
            extra_args=[],
        )

    def test_nonzero_exit_prefers_json_result_over_stderr_warning(self):
        # Exact shape reproduced live against the real DeepSeek endpoint.
        stdout = (
            '{"is_error":true,"result":"API Error: 402 Insufficient Balance",'
            '"session_id":"s-1","type":"result"}'
        )
        stderr = (
            "⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY "
            "or another auth source is set and takes precedence over your "
            "claude.ai login · Unset it to load your organization's connectors"
        )
        with patch(
            "neurobot.claude_executor.subprocess.run",
            return_value=fake_completed_process(1, stdout, stderr),
        ):
            with self.assertRaises(ClaudeExecutorError) as ctx:
                self.make_client().run_turn_new_session("hi")
        message = str(ctx.exception)
        self.assertIn("API Error: 402 Insufficient Balance", message)
        self.assertNotIn("connectors are disabled", message)

    def test_nonzero_exit_falls_back_to_stderr_when_stdout_unparseable(self):
        # No JSON at all (e.g. the binary crashed before ever writing
        # --output-format json output) — stderr is genuinely the only
        # available signal, so the original behavior must be preserved.
        with patch(
            "neurobot.claude_executor.subprocess.run",
            return_value=fake_completed_process(1, "", "segfault or similar\n"),
        ):
            with self.assertRaises(ClaudeExecutorError) as ctx:
                self.make_client().run_turn_new_session("hi")
        self.assertIn("segfault or similar", str(ctx.exception))

    def test_nonzero_exit_with_valid_json_but_no_result_or_is_error_falls_back_to_stderr(self):
        with patch(
            "neurobot.claude_executor.subprocess.run",
            return_value=fake_completed_process(1, '{"unrelated":"field"}', "real stderr detail"),
        ):
            with self.assertRaises(ClaudeExecutorError) as ctx:
                self.make_client().run_turn_new_session("hi")
        self.assertIn("real stderr detail", str(ctx.exception))

    def test_zero_exit_with_is_error_payload_still_raises(self):
        stdout = '{"is_error":true,"result":"some failure","session_id":"s-1"}'
        with patch(
            "neurobot.claude_executor.subprocess.run",
            return_value=fake_completed_process(0, stdout, ""),
        ):
            with self.assertRaises(ClaudeExecutorError) as ctx:
                self.make_client().run_turn_new_session("hi")
        self.assertIn("some failure", str(ctx.exception))

    def test_success_path_unchanged(self):
        stdout = '{"is_error":false,"result":"hello back","session_id":"s-2"}'
        with patch(
            "neurobot.claude_executor.subprocess.run",
            return_value=fake_completed_process(0, stdout, ""),
        ):
            answer, session_id = self.make_client().run_turn_new_session("hi")
        self.assertEqual(answer, "hello back")
        self.assertEqual(session_id, "s-2")


@unittest.skipUnless(
    os.environ.get("NEUROBOT_LIVE_DEEPSEEK_TEST") == "1",
    "Opt-in only: hits the real `claude` binary and the real DeepSeek API. "
    "Set NEUROBOT_LIVE_DEEPSEEK_TEST=1 plus DEEPSEEK_LIVE_TEST_* env vars "
    "below to run this as an actual end-to-end connectivity check — this is "
    "exactly the gap that let the 2026-08 auth/balance incident ship "
    "undetected (every other test mocks subprocess.run, so nothing ever "
    "exercised a real `claude -p` invocation end to end).",
)
class LiveDeepSeekSmokeTest(unittest.TestCase):
    """Real, non-mocked smoke check: actually invokes the `claude` CLI with
    DeepSeek env_overrides and expects a real, non-error text response.

    Not run by default (unittest discover / CI) since it costs real money,
    needs live credentials, and needs the `claude` binary installed — opt in
    explicitly via NEUROBOT_LIVE_DEEPSEEK_TEST=1. Configure via:
      DEEPSEEK_LIVE_TEST_API_KEY   (required)
      DEEPSEEK_LIVE_TEST_CLAUDE_BIN (default: "claude")
      DEEPSEEK_LIVE_TEST_WORKSPACE  (default: current directory)
    """

    def test_real_deepseek_turn_returns_text(self):
        api_key = os.environ.get("DEEPSEEK_LIVE_TEST_API_KEY", "").strip()
        self.assertTrue(api_key, "DEEPSEEK_LIVE_TEST_API_KEY must be set for this test")
        client = ClaudeExecutorClient(
            workspace=Path(os.environ.get("DEEPSEEK_LIVE_TEST_WORKSPACE", ".")),
            timeout=60,
            claude_bin=os.environ.get("DEEPSEEK_LIVE_TEST_CLAUDE_BIN", "claude"),
            extra_args=["--dangerously-skip-permissions"],
            env_overrides={
                "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                "ANTHROPIC_AUTH_TOKEN": api_key,
                "ANTHROPIC_MODEL": "deepseek-v4-flash",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-flash",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
            },
        )
        answer, session_id = client.run_turn_new_session(
            "Reply with exactly one word: hi"
        )
        self.assertTrue(answer.strip())
        self.assertTrue(session_id)


if __name__ == "__main__":
    unittest.main()
