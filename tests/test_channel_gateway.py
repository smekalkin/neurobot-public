import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neurobot import bot, channel_gateway, relogin  # noqa: E402


def human_message(text="Сделай задачу", thread_id=3):
    return {
        "message_id": 10,
        "message_thread_id": thread_id,
        "chat": {"id": -1001},
        "from": {"id": 42, "username": "owner", "is_bot": False},
        "text": text,
    }


class ChannelGatewayTests(unittest.TestCase):
    def setUp(self):
        # These cases are about routing, so pin the login advisory to "fine"
        # rather than letting them depend on whether the machine running the
        # tests happens to have a valid ~/.claude/.credentials.json.
        patcher = patch.object(
            channel_gateway, "claude_login_warning", return_value=None
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def common_patches(self):
        return (
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 3),
            patch.object(bot, "CODEX_DEFAULT_THREAD_ID", "codex-thread"),
        )

    def test_inbound_drops_other_topics(self):
        state = {}
        patches = self.common_patches()
        with patches[0], patches[1], patches[2], patch.object(bot, "load_state", return_value=state):
            result = channel_gateway.handle_inbound(
                {"message": human_message(thread_id=99)}
            )
        self.assertEqual(result, {"action": "drop"})

    def test_topicless_group_messages_reach_the_session(self):
        # TELEGRAM_THREAD_ID=0: the group has no topics, so messages carry no
        # message_thread_id at all -- they must not be dropped as "other topic".
        state = {channel_gateway.CHANNEL_MODE_KEY: "auto"}
        message = human_message("Привет", thread_id=0)
        del message["message_thread_id"]
        with (
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 0),
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(channel_gateway, "validate_human", return_value=True),
            patch.object(bot, "describe_current_claude_model", return_value="Claude"),
            patch.object(bot, "log_backend_usage"),
        ):
            result = channel_gateway.handle_inbound({"message": message})
        self.assertEqual(result["action"], "claude")

    def test_backend_command_changes_mode(self):
        state = {}
        patches = self.common_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state") as save_state,
            patch.object(channel_gateway, "validate_human", return_value=True),
        ):
            result = channel_gateway.handle_inbound(
                {"message": human_message("/backend codex")}
            )
        self.assertEqual(state[channel_gateway.CHANNEL_MODE_KEY], "codex")
        self.assertEqual(result["action"], "reply")
        save_state.assert_called()

    def test_claude_mode_returns_prompt_for_persistent_session(self):
        state = {channel_gateway.CHANNEL_MODE_KEY: "auto"}
        patches = self.common_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(channel_gateway, "validate_human", return_value=True),
            patch.object(
                bot, "describe_current_claude_model", return_value="Claude Sonnet5 High"
            ),
            patch.object(bot, "log_backend_usage") as log_backend_usage,
        ):
            result = channel_gateway.handle_inbound(
                {"message": human_message("@ExampleDeveloperBot Проверь сервер")}
            )
        self.assertEqual(result["action"], "claude")
        self.assertIn("Проверь сервер", result["prompt"])
        self.assertEqual(state[channel_gateway.CHANNEL_LAST_BACKEND_KEY], "claude")
        log_backend_usage.assert_called_once_with(
            "claude", "Claude Sonnet5 High", human_message("@ExampleDeveloperBot Проверь сервер")
        )

    def test_codex_mode_reuses_existing_message_handler(self):
        state = {channel_gateway.CHANNEL_MODE_KEY: "codex", "sessions": {}}
        patches = self.common_patches()
        handler = Mock()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(channel_gateway, "validate_human", return_value=True),
            patch.object(bot, "handle_message", handler),
            patch.object(bot, "log_backend_usage") as log_backend_usage,
        ):
            result = channel_gateway.handle_inbound(
                {"message": human_message("Проверь сервер")}
            )
        self.assertEqual(result, {"action": "handled", "backend": "codex"})
        handler.assert_called_once()
        self.assertEqual(state["sessions"]["-1001:3"], "codex-thread")
        log_backend_usage.assert_called_once_with(
            "codex", bot.backend_model_label("codex"), human_message("Проверь сервер")
        )

    def test_backend_deepseek_refused_without_key(self):
        state = {}
        patches = self.common_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state") as save_state,
            patch.object(channel_gateway, "validate_human", return_value=True),
            patch.object(bot, "deepseek_configured", return_value=False),
        ):
            result = channel_gateway.handle_inbound(
                {"message": human_message("/backend deepseek")}
            )
        self.assertNotIn(channel_gateway.CHANNEL_MODE_KEY, state)
        self.assertEqual(result["action"], "reply")
        self.assertIn("не настроен", result["text"])
        save_state.assert_not_called()

    def test_backend_deepseek_switches_mode_when_key_present(self):
        state = {}
        patches = self.common_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(channel_gateway, "validate_human", return_value=True),
            patch.object(bot, "deepseek_configured", return_value=True),
            patch.object(bot, "DEEPSEEK_MODEL", "deepseek-v4-flash"),
        ):
            result = channel_gateway.handle_inbound(
                {"message": human_message("/backend deepseek")}
            )
        self.assertEqual(state[channel_gateway.CHANNEL_MODE_KEY], "deepseek")
        self.assertEqual(result["action"], "reply")

    def test_deepseek_mode_reuses_message_handler_with_own_session_key(self):
        state = {channel_gateway.CHANNEL_MODE_KEY: "deepseek", "sessions": {}}
        patches = self.common_patches()
        handler = Mock()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(channel_gateway, "validate_human", return_value=True),
            patch.object(bot, "deepseek_configured", return_value=True),
            patch.object(bot, "handle_message", handler),
            patch.object(bot, "log_backend_usage") as log_backend_usage,
        ):
            result = channel_gateway.handle_inbound(
                {"message": human_message("Проверь сервер")}
            )
        self.assertEqual(result, {"action": "handled", "backend": "deepseek"})
        handler.assert_called_once()
        self.assertEqual(
            handler.call_args.kwargs.get("session_key_suffix"),
            channel_gateway.DEEPSEEK_SESSION_KEY_SUFFIX,
        )
        log_backend_usage.assert_called_once_with(
            "deepseek", bot.backend_model_label("deepseek"), human_message("Проверь сервер")
        )

    def test_deepseek_mode_reverts_to_auto_if_key_disappears(self):
        state = {channel_gateway.CHANNEL_MODE_KEY: "deepseek"}
        patches = self.common_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(channel_gateway, "validate_human", return_value=True),
            patch.object(bot, "deepseek_configured", return_value=False),
        ):
            result = channel_gateway.handle_inbound(
                {"message": human_message("Проверь сервер")}
            )
        self.assertEqual(state[channel_gateway.CHANNEL_MODE_KEY], "auto")
        self.assertEqual(result["action"], "reply")
        self.assertIn("не настроен", result["text"])

    def test_outbound_prepends_claude_model_label_when_enabled(self):
        state = {}
        with (
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(bot, "SHOW_MODEL_LABEL", True),
            patch.object(
                bot, "describe_current_claude_model", return_value="Claude Sonnet5 High"
            ),
        ):
            result = channel_gateway.handle_outbound({"text": "Готово."})
        # describe_current_claude_model's plain text is transliterated to
        # superscript at the splice point, not left as-is, and joined with a
        # single newline (no blank-line gap between label and message).
        self.assertEqual(result["text"], "ᶜˡᵃᵘᵈᵉ ˢᵒⁿⁿᵉᵗ⁵ ʰⁱᵍʰ\nГотово.")

    def test_outbound_label_off_by_default(self):
        state = {}
        with (
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(bot, "SHOW_MODEL_LABEL", False),
        ):
            result = channel_gateway.handle_outbound({"text": "Готово."})
        self.assertEqual(result["text"], "Готово.")

    def test_outbound_dispatches_route_and_strips_machine_block(self):
        state = {}
        answer = (
            "Передаю маркетологу.\n"
            '<telegram_task target="marketing">Подготовь план.</telegram_task>'
        )
        with (
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(bot, "dispatch_task", return_value="task-1") as dispatch,
        ):
            result = channel_gateway.handle_outbound(
                {"text": answer, "reply_to": "10"}
            )
        self.assertEqual(result["text"], "Передаю маркетологу.")
        self.assertEqual(result["routed_count"], 1)
        dispatch.assert_called_once_with("marketing", "Подготовь план.")

    def test_outbound_returns_peer_result(self):
        routed = {
            "kind": "task",
            "task_id": "manager-1",
            "reply_agent": "manager",
            "hop": 2,
        }
        state = {
            channel_gateway.CHANNEL_PENDING_ROUTES_KEY: {"10": routed},
            channel_gateway.CHANNEL_LATEST_ROUTE_KEY: "10",
        }
        with (
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(bot, "return_routed_response") as return_response,
        ):
            result = channel_gateway.handle_outbound(
                {"text": "Готово.", "reply_to": "10"}
            )
        self.assertTrue(result["returned_route"])
        return_response.assert_called_once_with(routed, "Готово.")
        self.assertNotIn("10", state[channel_gateway.CHANNEL_PENDING_ROUTES_KEY])


AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize?client_id=x&state=y"


def credentials(refresh_expires_in_hours=672.0, refresh_token="rt"):
    payload = {
        "claudeAiOauth": {
            "accessToken": "at",
            "refreshToken": refresh_token,
            # The access token really does sit ~4h out and is refreshed
            # silently — alarming on it would cry wolf several times a day.
            "expiresAt": int((time.time() + 4 * 3600) * 1000),
            "refreshTokenExpiresAt": int(
                (time.time() + refresh_expires_in_hours * 3600) * 1000
            ),
            "subscriptionType": "pro",
        }
    }
    return json.dumps(payload)


class ClaudeLoginWarningTests(unittest.TestCase):
    def warning_for(self, contents):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".credentials.json"
            if contents is not None:
                path.write_text(contents, encoding="utf-8")
            with patch.object(
                channel_gateway, "claude_credentials_path", return_value=path
            ):
                return channel_gateway.claude_login_warning()

    def test_missing_credentials_file_warns(self):
        self.assertEqual(
            self.warning_for(None), channel_gateway.RELOGIN_NOT_LOGGED_IN_MESSAGE
        )

    def test_valid_credentials_do_not_warn(self):
        self.assertIsNone(self.warning_for(credentials()))

    def test_soon_expiring_access_token_does_not_warn(self):
        # expiresAt is always only hours away; the CLI refreshes it in the
        # background, so this must stay silent.
        self.assertIsNone(self.warning_for(credentials(refresh_expires_in_hours=1.0)))

    def test_expired_refresh_token_warns(self):
        self.assertEqual(
            self.warning_for(credentials(refresh_expires_in_hours=-1.0)),
            channel_gateway.RELOGIN_EXPIRED_MESSAGE,
        )

    def test_credentials_without_oauth_block_warn(self):
        self.assertEqual(
            self.warning_for(json.dumps({"mcpOAuth": {}})),
            channel_gateway.RELOGIN_NOT_LOGGED_IN_MESSAGE,
        )

    def test_malformed_credentials_never_block_delivery(self):
        # Advisory only: anything unexpected must fall back to delivering the
        # message, not to swallowing it.
        self.assertIsNone(self.warning_for("{not json"))

    def test_missing_expiry_field_does_not_warn(self):
        self.assertIsNone(
            self.warning_for(json.dumps({"claudeAiOauth": {"refreshToken": "rt"}}))
        )

    def test_config_dir_env_overrides_the_home_path(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/somewhere/.claude"}):
            self.assertEqual(
                channel_gateway.claude_credentials_path(),
                Path("/somewhere/.claude/.credentials.json"),
            )


class GatewayInboundMixin:
    """Shared handle_inbound driver for the /relogin and logged-out suites."""

    def common_patches(self):
        return (
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 3),
            patch.object(bot, "ALLOWED_USER_IDS", {42}),
            patch.object(bot, "STATE_DIR", Path(tempfile.gettempdir())),
        )

    def inbound(self, text, state, sender_id=42, login_warning=None, **extra):
        message = human_message(text)
        message["from"] = {"id": sender_id, "username": "owner", "is_bot": False}
        entered = list(self.common_patches()) + [
            patch.object(bot, "load_state", return_value=state),
            patch.object(bot, "save_state"),
            patch.object(channel_gateway, "validate_human", return_value=True),
            patch.object(bot, "CLAUDE_BIN", "/usr/bin/true"),
            patch.object(bot, "describe_current_claude_model", return_value="Claude"),
            patch.object(bot, "log_backend_usage"),
            # Default to "logged in" so these cases exercise routing rather than
            # the advisory; the advisory has its own suite below.
            patch.object(
                channel_gateway, "claude_login_warning", return_value=login_warning
            ),
        ] + [patch.object(relogin, name, value) for name, value in extra.items()]
        with ExitStack() as stack:
            for context in entered:
                stack.enter_context(context)
            return channel_gateway.handle_inbound({"message": message})

    def pending(self, user_id=42, ttl=600.0):
        return {
            "user_id": user_id,
            "socket": "/tmp/relogin-test.sock",
            "pid": 12345,
            "started_at": time.time(),
            "expires_at": time.time() + ttl,
        }


class ReloginCommandTests(GatewayInboundMixin, unittest.TestCase):
    """State machine only — the pty itself is covered by tests/test_relogin.py."""

    # ------------------------------------------------------------------ start
    def test_relogin_starts_supervisor_and_replies_with_link(self):
        state = {}
        result = self.inbound(
            "/relogin",
            state,
            start_supervisor=Mock(return_value=4242),
            terminate=Mock(),
            wait_for_url=Mock(return_value={"state": "awaiting_code", "url": AUTHORIZE_URL}),
        )
        self.assertEqual(result["action"], "reply")
        self.assertIn(AUTHORIZE_URL, result["text"])
        self.assertEqual(state[channel_gateway.CHANNEL_PENDING_RELOGIN_KEY]["user_id"], 42)
        self.assertEqual(state[channel_gateway.CHANNEL_PENDING_RELOGIN_KEY]["pid"], 4242)

    def test_relogin_works_when_the_bot_is_mentioned(self):
        # requireMention: true means the command normally arrives prefixed with
        # the bot's @mention, which would otherwise be read as the command name.
        state = {}
        with patch.object(bot, "BOT_USERNAME", "ExampleDeveloperBot"):
            result = self.inbound(
                "@ExampleDeveloperBot /relogin",
                state,
                start_supervisor=Mock(return_value=1),
                terminate=Mock(),
                wait_for_url=Mock(return_value={"url": AUTHORIZE_URL}),
            )
        self.assertIn(AUTHORIZE_URL, result["text"])

    def test_relogin_refused_for_sender_outside_the_explicit_allowlist(self):
        # validate_human passes (TELEGRAM_ALLOW_ALL_CHAT_MEMBERS is on for the
        # live tenants) but this sender is not one of the named humans.
        state = {}
        start = Mock()
        result = self.inbound("/relogin", state, sender_id=777, start_supervisor=start)
        self.assertEqual(result["action"], "reply")
        self.assertIn("TELEGRAM_ALLOWED_USER_IDS", result["text"])
        start.assert_not_called()
        self.assertNotIn(channel_gateway.CHANNEL_PENDING_RELOGIN_KEY, state)

    def test_no_pending_state_when_the_link_never_appears(self):
        state = {}
        terminate = Mock()
        result = self.inbound(
            "/relogin",
            state,
            start_supervisor=Mock(return_value=99),
            terminate=terminate,
            wait_for_url=Mock(return_value={"state": "finished", "detail": "boom"}),
        )
        self.assertIn("boom", result["text"])
        self.assertNotIn(channel_gateway.CHANNEL_PENDING_RELOGIN_KEY, state)
        terminate.assert_called_with(str(channel_gateway.RELOGIN_SOCKET), 99)

    # ------------------------------------------------------- pending handling
    def test_next_message_is_submitted_as_the_code(self):
        state = {channel_gateway.CHANNEL_PENDING_RELOGIN_KEY: self.pending()}
        submit = Mock(return_value={"ok": True, "detail": "Login successful"})
        result = self.inbound("abc123#def456", state, submit_code=submit, terminate=Mock())
        submit.assert_called_once_with("/tmp/relogin-test.sock", "abc123#def456")
        self.assertEqual(result["action"], "reply")
        self.assertIn("вход выполнен", result["text"])
        self.assertNotIn(channel_gateway.CHANNEL_PENDING_RELOGIN_KEY, state)

    def success_text(self, mode, multi_backend=True):
        state = {
            channel_gateway.CHANNEL_PENDING_RELOGIN_KEY: self.pending(),
            channel_gateway.CHANNEL_MODE_KEY: mode,
        }
        with patch.object(
            channel_gateway, "multi_backend_tenant", return_value=multi_backend
        ):
            result = self.inbound(
                "abc123#def456",
                state,
                submit_code=Mock(return_value={"ok": True, "detail": "Login successful"}),
                terminate=Mock(),
            )
        return result["text"]

    def test_success_hints_at_backend_claude_when_routed_elsewhere(self):
        # /relogin always repairs Claude, so a person who ran it from DeepSeek
        # would otherwise see their next test messages still answered by DeepSeek
        # and conclude the login had failed.
        for mode in ("deepseek", "codex"):
            with self.subTest(mode=mode):
                text = self.success_text(mode)
                self.assertIn("вход выполнен", text)
                self.assertIn("Текущий режим — {}".format(mode), text)
                self.assertIn("/backend claude", text)

    def test_success_has_no_hint_in_claude_mode(self):
        text = self.success_text("claude")
        self.assertIn("вход выполнен", text)
        self.assertNotIn("/backend claude", text)

    def test_success_has_no_hint_in_auto_mode(self):
        # auto already routes to the persistent Claude session, so telling the
        # sender to switch would point at a problem that does not exist.
        text = self.success_text("auto")
        self.assertNotIn("/backend claude", text)

    def test_success_has_no_hint_for_single_backend_tenants(self):
        # A Claude-only tenant has no /backend switch at all, so the hint would
        # name a command that answers "переключать нечего".
        text = self.success_text("deepseek", multi_backend=False)
        self.assertIn("вход выполнен", text)
        self.assertNotIn("/backend claude", text)

    def test_failed_exchange_is_surfaced_and_clears_pending(self):
        state = {channel_gateway.CHANNEL_PENDING_RELOGIN_KEY: self.pending()}
        submit = Mock(
            return_value={
                "ok": False,
                "detail": "Login failed: Request failed with status code 400",
            }
        )
        result = self.inbound("stale#code", state, submit_code=submit, terminate=Mock())
        self.assertIn("status code 400", result["text"])
        # Cleared, so the person can simply run /relogin again instead of being
        # wedged in "awaiting code" forever.
        self.assertNotIn(channel_gateway.CHANNEL_PENDING_RELOGIN_KEY, state)

    def test_unreachable_supervisor_clears_pending(self):
        state = {channel_gateway.CHANNEL_PENDING_RELOGIN_KEY: self.pending()}
        submit = Mock(side_effect=OSError("no such socket"))
        result = self.inbound("abc#def", state, submit_code=submit, terminate=Mock())
        self.assertIn("/relogin", result["text"])
        self.assertNotIn(channel_gateway.CHANNEL_PENDING_RELOGIN_KEY, state)

    def test_message_without_a_hash_is_not_submitted(self):
        state = {channel_gateway.CHANNEL_PENDING_RELOGIN_KEY: self.pending()}
        submit = Mock()
        result = self.inbound("это обычное сообщение", state, submit_code=submit)
        submit.assert_not_called()
        self.assertIn("code#state", result["text"])
        # Still waiting: nothing was burned on a message that was clearly not a
        # code.
        self.assertIn(channel_gateway.CHANNEL_PENDING_RELOGIN_KEY, state)

    def test_cancel_clears_pending(self):
        state = {channel_gateway.CHANNEL_PENDING_RELOGIN_KEY: self.pending()}
        terminate = Mock()
        result = self.inbound("/cancel", state, terminate=terminate, submit_code=Mock())
        self.assertIn("отменена", result["text"])
        self.assertNotIn(channel_gateway.CHANNEL_PENDING_RELOGIN_KEY, state)
        terminate.assert_called_once_with("/tmp/relogin-test.sock", 12345)

    def test_second_relogin_replaces_the_first_attempt(self):
        state = {channel_gateway.CHANNEL_PENDING_RELOGIN_KEY: self.pending()}
        terminate = Mock()
        result = self.inbound(
            "/relogin",
            state,
            terminate=terminate,
            start_supervisor=Mock(return_value=777),
            wait_for_url=Mock(return_value={"url": AUTHORIZE_URL}),
            submit_code=Mock(),
        )
        self.assertIn(AUTHORIZE_URL, result["text"])
        # The abandoned pty from the first attempt is killed before a new one is
        # spawned, so it can never interfere with this login.
        terminate.assert_called_with(str(channel_gateway.RELOGIN_SOCKET), 12345)
        self.assertEqual(state[channel_gateway.CHANNEL_PENDING_RELOGIN_KEY]["pid"], 777)

    # --------------------------------------------------- fall-through cases
    def test_expired_pending_falls_through_to_normal_routing(self):
        state = {channel_gateway.CHANNEL_PENDING_RELOGIN_KEY: self.pending(ttl=-1.0)}
        submit = Mock()
        result = self.inbound("Проверь сервер", state, submit_code=submit, terminate=Mock())
        submit.assert_not_called()
        self.assertEqual(result["action"], "claude")
        self.assertIn("Проверь сервер", result["prompt"])
        self.assertNotIn(channel_gateway.CHANNEL_PENDING_RELOGIN_KEY, state)

    def test_another_sender_is_routed_normally_while_one_relogin_is_pending(self):
        state = {channel_gateway.CHANNEL_PENDING_RELOGIN_KEY: self.pending(user_id=42)}
        submit = Mock()
        result = self.inbound("Проверь сервер", state, sender_id=99, submit_code=submit)
        submit.assert_not_called()
        self.assertEqual(result["action"], "claude")
        # The other person's pending attempt survives untouched.
        self.assertIn(channel_gateway.CHANNEL_PENDING_RELOGIN_KEY, state)

    def test_ordinary_message_without_pending_state_is_untouched(self):
        state = {}
        submit = Mock()
        start = Mock()
        result = self.inbound(
            "Проверь сервер", state, submit_code=submit, start_supervisor=start
        )
        submit.assert_not_called()
        start.assert_not_called()
        self.assertEqual(result["action"], "claude")

    def test_other_commands_still_work_while_relogin_is_pending(self):
        state = {
            channel_gateway.CHANNEL_PENDING_RELOGIN_KEY: self.pending(),
            channel_gateway.CHANNEL_MODE_KEY: "auto",
        }
        # /limits contains no "#", so it must be answered as the hint rather
        # than submitted — the pending window deliberately takes priority.
        result = self.inbound("/limits", state, submit_code=Mock())
        self.assertIn("code#state", result["text"])

    def test_help_advertises_relogin(self):
        state = {}
        result = self.inbound("/help", state)
        self.assertIn("/relogin", result["text"])


class LoggedOutRoutingTests(GatewayInboundMixin, unittest.TestCase):
    """A message must never vanish into a session that cannot answer it.

    Drives the real claude_login_warning() through a patched credentials path so
    the ORDER of checks in handle_inbound is exercised, not just mocked away.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.credentials_path = Path(self.tmp.name) / ".credentials.json"
        patcher = patch.object(
            channel_gateway,
            "claude_credentials_path",
            side_effect=lambda: self.credentials_path,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def log_in(self):
        self.credentials_path.write_text(credentials(), encoding="utf-8")

    def inbound(self, text, state, **kwargs):
        # Feed the REAL check's verdict (read from the patched credentials path)
        # through the mixin, so the call site inside handle_inbound still decides
        # what happens — which is what makes the ordering of the checks a real
        # assertion rather than a mocked-away one.
        kwargs["login_warning"] = channel_gateway.claude_login_warning()
        return super().inbound(text, state, **kwargs)

    def test_message_without_credentials_gets_a_hint_instead_of_silence(self):
        state = {}
        result = self.inbound("Проверь сервер", state)
        self.assertEqual(result["action"], "reply")
        self.assertEqual(result["text"], channel_gateway.RELOGIN_NOT_LOGGED_IN_MESSAGE)
        self.assertIn("/relogin", result["text"])

    def test_expired_refresh_token_gets_the_expiry_hint(self):
        self.credentials_path.write_text(
            credentials(refresh_expires_in_hours=-1.0), encoding="utf-8"
        )
        result = self.inbound("Проверь сервер", {})
        self.assertEqual(result["text"], channel_gateway.RELOGIN_EXPIRED_MESSAGE)

    def test_routing_is_unchanged_once_credentials_exist(self):
        self.log_in()
        state = {}
        result = self.inbound("Проверь сервер", state)
        self.assertEqual(result["action"], "claude")
        self.assertIn("Проверь сервер", result["prompt"])

    def test_relogin_still_works_while_logged_out(self):
        # The whole point of /relogin is the logged-out state, so the advisory
        # must not shadow it.
        state = {}
        result = self.inbound(
            "/relogin",
            state,
            start_supervisor=Mock(return_value=7),
            terminate=Mock(),
            wait_for_url=Mock(return_value={"url": AUTHORIZE_URL}),
        )
        self.assertIn(AUTHORIZE_URL, result["text"])
        self.assertIn(channel_gateway.CHANNEL_PENDING_RELOGIN_KEY, state)

    def test_pasted_code_still_reaches_the_supervisor_while_logged_out(self):
        state = {channel_gateway.CHANNEL_PENDING_RELOGIN_KEY: self.pending()}
        submit = Mock(return_value={"ok": True, "detail": "Login successful"})
        result = self.inbound("abc123#def456", state, submit_code=submit, terminate=Mock())
        submit.assert_called_once_with("/tmp/relogin-test.sock", "abc123#def456")
        self.assertIn("вход выполнен", result["text"])

    def test_status_surfaces_the_warning(self):
        result = self.inbound("/status", {})
        self.assertIn("/relogin", result["text"])

    def test_codex_backend_is_not_affected_by_claude_credentials(self):
        # Codex has its own authentication; a missing Claude login says nothing
        # about it and must not block its messages.
        state = {channel_gateway.CHANNEL_MODE_KEY: "codex", "sessions": {}}
        with (
            patch.object(bot, "CODEX_DEFAULT_THREAD_ID", "codex-thread"),
            patch.object(bot, "handle_message") as handler,
        ):
            result = self.inbound("Проверь сервер", state)
        self.assertEqual(result, {"action": "handled", "backend": "codex"})
        handler.assert_called_once()

    def test_deepseek_backend_is_not_affected_by_claude_credentials(self):
        state = {channel_gateway.CHANNEL_MODE_KEY: "deepseek"}
        with (
            patch.object(bot, "deepseek_configured", return_value=True),
            patch.object(bot, "handle_message") as handler,
        ):
            result = self.inbound("Проверь сервер", state)
        self.assertEqual(result, {"action": "handled", "backend": "deepseek"})
        handler.assert_called_once()


if __name__ == "__main__":
    unittest.main()
