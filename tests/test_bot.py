import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neurobot import bot  # noqa: E402


class FakeRateLimitClient:
    snapshot = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read_account_rate_limits(self):
        return {"rateLimits": self.snapshot}


class NeurobotTests(unittest.TestCase):
    def test_extract_local_attachment_rewrites_workspace_report_link(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            workspace = Path(temporary_dir)
            reports = workspace / "reports"
            reports.mkdir()
            report = reports / "marketing-report.md"
            report.write_text("report", encoding="utf-8")
            answer = "Готово: [скачать отчёт]({})".format(report)

            with (
                patch.object(bot, "WORKSPACE", workspace),
                patch.object(bot, "ATTACHMENT_ROOTS", [reports.resolve()]),
            ):
                visible, attachments = bot.extract_local_attachments(answer)

        self.assertEqual(attachments, [report.resolve()])
        self.assertEqual(
            visible,
            "Готово: скачать отчёт — файл прикреплён ниже",
        )

    def test_extract_local_attachment_rejects_file_outside_export_roots(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            workspace = Path(temporary_dir)
            reports = workspace / "reports"
            reports.mkdir()
            secret = workspace / ".codex" / "auth.json"
            secret.parent.mkdir()
            secret.write_text("secret", encoding="utf-8")
            answer = "[файл]({})".format(secret)

            with (
                patch.object(bot, "WORKSPACE", workspace),
                patch.object(bot, "ATTACHMENT_ROOTS", [reports.resolve()]),
            ):
                visible, attachments = bot.extract_local_attachments(answer)

        self.assertEqual(attachments, [])
        self.assertEqual(visible, answer)

    def test_process_prompt_uploads_linked_local_report(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            workspace = Path(temporary_dir)
            reports = workspace / "reports"
            reports.mkdir()
            report = reports / "report.md"
            report.write_text("report", encoding="utf-8")
            run_turn = Mock(
                return_value=("Отчёт: [скачать]({})".format(report), None)
            )
            edit_text = Mock()
            send_document = Mock()

            with (
                patch.object(bot, "WORKSPACE", workspace),
                patch.object(bot, "ATTACHMENT_ROOTS", [reports.resolve()]),
                patch.object(bot, "rate_limit_block_message", return_value=None),
                patch.object(bot, "run_turn", run_turn),
                patch.object(bot, "remember_codex_bridge_answer"),
                patch.object(bot, "send_text", return_value={"message_id": 10}),
                patch.object(bot, "edit_text", edit_text),
                patch.object(bot, "send_document", send_document),
            ):
                success = bot.process_prompt("Сделай отчёт", {"sessions": {}}, "test")

        self.assertTrue(success)
        self.assertIn(bot.TELEGRAM_FILE_GUIDANCE, run_turn.call_args.args[0])
        edit_text.assert_called_once_with(
            10, "Отчёт: скачать — файл прикреплён ниже"
        )
        send_document.assert_called_once_with(report.resolve(), None)

    def test_run_turn_with_heartbeat_pings_placeholder_while_waiting(self):
        edit_calls = []
        edit_text = Mock(side_effect=lambda mid, text: edit_calls.append((mid, text)))

        def slow_run_turn(prompt, session_id):
            time.sleep(0.25)
            return ("done", None)

        with (
            patch.object(bot, "HEARTBEAT_INTERVAL_SECONDS", 0.05),
            patch.object(bot, "run_turn", slow_run_turn),
            patch.object(bot, "edit_text", edit_text),
            patch.object(bot, "SHOW_MODEL_LABEL", False),
        ):
            answer, session_id = bot.run_turn_with_heartbeat(
                "prompt", None, 99, {}, "key"
            )

        self.assertEqual(answer, "done")
        self.assertIsNone(session_id)
        self.assertTrue(edit_calls, "expected at least one heartbeat edit_text call")
        for message_id, text in edit_calls:
            self.assertEqual(message_id, 99)
            self.assertIn("Ещё работаю", text)

    def test_run_turn_with_heartbeat_stops_ticking_once_run_turn_returns(self):
        edit_text = Mock()

        with (
            patch.object(bot, "HEARTBEAT_INTERVAL_SECONDS", 0.05),
            patch.object(bot, "run_turn", return_value=("done", None)),
            patch.object(bot, "edit_text", edit_text),
        ):
            bot.run_turn_with_heartbeat("prompt", None, 1, {}, "key")
            # Give a would-be leaked ticker thread time to fire at least once.
            time.sleep(0.2)

        edit_text.assert_not_called()

    def test_send_document_uses_target_topic(self):
        document = Path("/tmp/report.md")
        multipart = Mock(return_value={"message_id": 12})
        with (
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 121),
            patch.object(bot, "telegram_multipart", multipart),
        ):
            result = bot.send_document(document, reply_to=9)

        self.assertEqual(result, {"message_id": 12})
        multipart.assert_called_once_with(
            "sendDocument",
            {
                "chat_id": -1001,
                "message_thread_id": 121,
                "caption": "📎 report.md",
                "reply_to_message_id": 9,
                "allow_sending_without_reply": "true",
            },
            "document",
            document,
        )

    def test_telegram_multipart_encodes_document_bytes(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return b'{"ok":true,"result":{"message_id":12}}'

        with tempfile.TemporaryDirectory() as temporary_dir:
            document = Path(temporary_dir) / "report.md"
            document.write_bytes(b"report-body")
            urlopen = Mock(return_value=FakeResponse())
            with (
                patch.object(bot, "BOT_TOKEN", "test-token"),
                patch.object(bot, "TELEGRAM_RETRY_ATTEMPTS", 1),
                patch.object(bot.urllib.request, "urlopen", urlopen),
            ):
                result = bot.telegram_multipart(
                    "sendDocument",
                    {"chat_id": -1001, "message_thread_id": 121},
                    "document",
                    document,
                )

        request = urlopen.call_args.args[0]
        self.assertEqual(result, {"message_id": 12})
        self.assertTrue(request.full_url.endswith("/bottest-token/sendDocument"))
        boundary = request.headers["Content-type"].split("boundary=", 1)[1]
        self.assertLessEqual(len(boundary), 70)
        self.assertIn(b'name="message_thread_id"', request.data)
        self.assertIn(b'filename="report.md"', request.data)
        self.assertIn(b"report-body", request.data)

    def test_long_routed_message_round_trip(self):
        sent = []
        peer = {"username": "TargetBot", "thread_id": 200}
        body = "НАЧАЛО " + ("данные " * 1500) + "КОНЕЦ"

        with (
            patch.object(bot, "AGENT_KEY", "manager"),
            patch.object(bot, "BOT_USERNAME", "TargetBot"),
            patch.object(
                bot,
                "send_text_to_thread",
                side_effect=lambda thread_id, text: sent.append((thread_id, text)),
            ),
        ):
            bot.send_routed_message("task", peer, "test-task", "manager", 1, body)
            parsed = [bot.parse_routed_message(text) for _, text in sent]

        self.assertGreater(len(parsed), 1)
        self.assertTrue(all(parsed))
        rebuilt = "\n\n".join(item["body"] for item in parsed)
        self.assertIn("НАЧАЛО", rebuilt)
        self.assertIn("КОНЕЦ", rebuilt)
        self.assertEqual([item["part"] for item in parsed], list(range(1, len(parsed) + 1)))

    def test_rate_limit_blocks_below_threshold(self):
        FakeRateLimitClient.snapshot = {
            "planType": "plus",
            "primary": {"usedPercent": 91, "windowDurationMins": 300},
            "secondary": None,
            "spendControlReached": False,
            "rateLimitReachedType": None,
        }
        with (
            patch.object(bot, "CodexAppServerClient", FakeRateLimitClient),
            patch.object(bot, "RATE_LIMIT_MIN_REMAINING_PERCENT", 10),
        ):
            status = bot.read_codex_rate_limit_status()

        self.assertEqual(status["remaining_percent"], 9)
        self.assertFalse(status["allowed"])

    def test_rate_limit_allows_exact_threshold(self):
        FakeRateLimitClient.snapshot = {
            "planType": "plus",
            "primary": {"usedPercent": 90, "windowDurationMins": 300},
            "spendControlReached": False,
            "rateLimitReachedType": None,
        }
        with (
            patch.object(bot, "CodexAppServerClient", FakeRateLimitClient),
            patch.object(bot, "RATE_LIMIT_MIN_REMAINING_PERCENT", 10),
        ):
            status = bot.read_codex_rate_limit_status()

        self.assertEqual(status["remaining_percent"], 10)
        self.assertTrue(status["allowed"])

    def test_hybrid_falls_back_to_codex_only_for_claude_limit_error(self):
        run_codex = Mock(return_value=("Готово через Codex", "codex-thread"))
        with (
            patch.object(bot, "HYBRID_EXECUTOR_ORDER", ["claude", "codex"]),
            patch.object(
                bot,
                "run_claude",
                side_effect=RuntimeError("Claude rate limit reached"),
            ),
            patch.object(bot, "run_codex", run_codex),
            patch.object(
                bot,
                "read_codex_rate_limit_status",
                return_value={"allowed": True, "remaining_percent": 50},
            ),
        ):
            answer, session = bot.run_hybrid(
                "Продолжи задачу",
                {"mode": "auto", "claude": "claude-session"},
            )

        self.assertIn("задачу подхватил Codex", answer)
        self.assertEqual(session["codex"], "codex-thread")
        self.assertEqual(session["last_backend"], "codex")
        run_codex.assert_called_once()

    def test_hybrid_does_not_repeat_task_after_generic_claude_error(self):
        run_codex = Mock()
        with (
            patch.object(bot, "HYBRID_EXECUTOR_ORDER", ["claude", "codex"]),
            patch.object(
                bot,
                "run_claude",
                side_effect=RuntimeError("SSH command failed after making changes"),
            ),
            patch.object(bot, "run_codex", run_codex),
        ):
            with self.assertRaisesRegex(RuntimeError, "SSH command failed"):
                bot.run_hybrid(
                    "Выполни деплой",
                    {"mode": "auto", "claude": "claude-session"},
                )

        run_codex.assert_not_called()

    def test_hybrid_manual_codex_mode_skips_claude(self):
        run_claude = Mock()
        with (
            patch.object(bot, "run_claude", run_claude),
            patch.object(
                bot,
                "read_codex_rate_limit_status",
                return_value={"allowed": True, "remaining_percent": 50},
            ),
            patch.object(
                bot,
                "run_codex",
                return_value=("Codex result", "codex-thread"),
            ),
        ):
            answer, session = bot.run_hybrid(
                "Задача",
                {"mode": "codex", "codex": None},
            )

        self.assertEqual(answer, "Codex result")
        self.assertEqual(session["last_backend"], "codex")
        run_claude.assert_not_called()

    def test_stateless_claude_always_starts_fresh_and_discards_session(self):
        client = Mock()
        client.__enter__ = Mock(return_value=client)
        client.__exit__ = Mock(return_value=None)
        client.run_turn_new_session.return_value = ("Fresh result", "new-session")

        with (
            patch.object(bot, "CLAUDE_STATELESS", True),
            patch.object(bot, "ClaudeExecutorClient", return_value=client),
        ):
            answer, session = bot.run_claude("Задача", "legacy-session")

        self.assertEqual(answer, "Fresh result")
        self.assertIsNone(session)
        client.run_turn_new_session.assert_called_once_with("Задача")
        client.run_turn.assert_not_called()

    def test_deepseek_api_key_read_fresh_from_disk_every_call(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            env_file = Path(temporary_dir) / "deepseek.env"
            with patch.object(bot, "DEEPSEEK_ENV_FILE", env_file):
                self.assertEqual(bot.read_deepseek_api_key(), "")
                self.assertFalse(bot.deepseek_configured())

                env_file.write_text("DEEPSEEK_API_KEY=sk-test-123\n", encoding="utf-8")

                # No caching: the same process sees the new key immediately,
                # which is the whole point (no service restart needed once a
                # human fills in the real key).
                self.assertEqual(bot.read_deepseek_api_key(), "sk-test-123")
                self.assertTrue(bot.deepseek_configured())

    def test_deepseek_client_rejects_missing_key(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            env_file = Path(temporary_dir) / "deepseek.env"
            with patch.object(bot, "DEEPSEEK_ENV_FILE", env_file):
                with self.assertRaises(RuntimeError):
                    bot.run_deepseek("Задача", None)

    def test_run_deepseek_points_claude_executor_at_deepseek_endpoint(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            env_file = Path(temporary_dir) / "deepseek.env"
            env_file.write_text("DEEPSEEK_API_KEY=sk-live\n", encoding="utf-8")
            client = Mock()
            client.__enter__ = Mock(return_value=client)
            client.__exit__ = Mock(return_value=None)
            client.run_turn_new_session.return_value = ("DeepSeek result", "ds-session")
            captured = {}

            def fake_client(*args, **kwargs):
                captured.update(kwargs)
                return client

            with (
                patch.object(bot, "DEEPSEEK_ENV_FILE", env_file),
                patch.object(bot, "DEEPSEEK_MODEL", "deepseek-v4-flash"),
                patch.object(bot, "ClaudeExecutorClient", fake_client),
            ):
                answer, session = bot.run_deepseek("Задача", None)

            self.assertEqual(answer, "DeepSeek result")
            self.assertEqual(session, "ds-session")
            overrides = captured["env_overrides"]
            self.assertEqual(overrides["ANTHROPIC_AUTH_TOKEN"], "sk-live")
            self.assertEqual(overrides["ANTHROPIC_MODEL"], "deepseek-v4-flash")
            self.assertEqual(overrides["ANTHROPIC_BASE_URL"], bot.DEEPSEEK_BASE_URL)

    def test_run_turn_dispatches_deepseek(self):
        run_deepseek = Mock(return_value=("ok", "session-x"))
        with (
            patch.object(bot, "EXECUTOR", "deepseek"),
            patch.object(bot, "run_deepseek", run_deepseek),
        ):
            answer, session = bot.run_turn("Задача", None)
        self.assertEqual(answer, "ok")
        run_deepseek.assert_called_once_with("Задача", None)

    def test_handle_message_uses_suffixed_session_key_for_deepseek(self):
        state = {"sessions": {"-1001:3": "codex-session"}}
        run_turn = Mock(return_value=("DeepSeek answer", "deepseek-session"))
        with (
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 3),
            patch.object(bot, "ALLOWED_USER_IDS", {42}),
            patch.object(bot, "EXECUTOR", "deepseek"),
            patch.object(bot, "deepseek_configured", return_value=True),
            patch.object(bot, "run_turn", run_turn),
            patch.object(bot, "send_text", return_value={"message_id": 1}),
            patch.object(bot, "edit_text"),
            patch.object(bot, "set_reaction"),
        ):
            bot.handle_message(
                {
                    "chat": {"id": -1001},
                    "message_thread_id": 3,
                    "from": {"id": 42, "username": "owner", "is_bot": False},
                    "text": "Сделай задачу",
                    "message_id": 10,
                },
                state,
                session_key_suffix=":deepseek",
            )
        # The DeepSeek turn must not have been resumed against Codex's
        # session id, and must be stored under its own suffixed key —
        # switching backends must not clobber each other's session state.
        run_turn.assert_called_once()
        called_session_id = run_turn.call_args[0][1]
        self.assertNotEqual(called_session_id, "codex-session")
        self.assertEqual(state["sessions"]["-1001:3:deepseek"], "deepseek-session")
        self.assertEqual(state["sessions"]["-1001:3"], "codex-session")

    def test_format_model_id_uses_static_table_and_generic_fallback(self):
        self.assertEqual(bot.format_model_id("claude-sonnet-5"), "Sonnet5")
        self.assertEqual(bot.format_model_id("claude-opus-6"), "Opus6")

    def test_describe_current_claude_model_reads_latest_assistant_turn(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            transcript_dir = Path(temporary_dir)
            transcript = transcript_dir / "session.jsonl"
            lines = [
                '{"type": "assistant", "effort": "medium", '
                '"message": {"model": "claude-sonnet-5"}}',
                '{"type": "assistant", "effort": "high", '
                '"message": {"model": "claude-sonnet-5"}}',
            ]
            transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with patch.object(bot, "CLAUDE_TRANSCRIPT_DIR_OVERRIDE", str(transcript_dir)):
                label = bot.describe_current_claude_model()
        self.assertEqual(label, "Claude Sonnet5 High")

    def test_describe_current_claude_model_falls_back_when_no_transcript(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            with patch.object(bot, "CLAUDE_TRANSCRIPT_DIR_OVERRIDE", temporary_dir):
                label = bot.describe_current_claude_model()
        self.assertEqual(label, "Claude")

    def test_describe_current_codex_model_reads_latest_turn_context(self):
        # Shape used by a Codex App Server thread's rollout log.
        with tempfile.TemporaryDirectory() as temporary_dir:
            rollout = Path(temporary_dir) / "rollout.jsonl"
            lines = [
                '{"type": "turn_context", "payload": {"model": "gpt-5.6-sol", '
                '"collaboration_mode": {"settings": {"reasoning_effort": null}}}}',
                '{"type": "turn_context", "payload": {"model": "gpt-5.6-sol", '
                '"collaboration_mode": {"settings": {"reasoning_effort": "high"}}}}',
            ]
            rollout.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with patch.object(
                bot,
                "read_codex_thread_meta",
                return_value={"thread": {"path": str(rollout)}},
            ):
                label = bot.describe_current_codex_model("thread-1")
        self.assertEqual(label, "Codex gpt-5.6-sol High")

    def test_describe_current_codex_model_omits_effort_when_unset(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            rollout = Path(temporary_dir) / "rollout.jsonl"
            rollout.write_text(
                '{"type": "turn_context", "payload": {"model": "gpt-5.6-sol", '
                '"collaboration_mode": {"settings": {"reasoning_effort": null}}}}\n',
                encoding="utf-8",
            )
            with patch.object(
                bot,
                "read_codex_thread_meta",
                return_value={"thread": {"path": str(rollout)}},
            ):
                label = bot.describe_current_codex_model("thread-1")
        self.assertEqual(label, "Codex gpt-5.6-sol")

    def test_describe_current_codex_model_falls_back_without_thread_id(self):
        with patch.object(bot, "CODEX_MODEL_LABEL", "Codex"):
            self.assertEqual(bot.describe_current_codex_model(None), "Codex")

    def test_describe_current_codex_model_falls_back_on_app_server_error(self):
        with (
            patch.object(bot, "CODEX_MODEL_LABEL", "Codex"),
            patch.object(
                bot, "read_codex_thread_meta", side_effect=RuntimeError("socket down")
            ),
        ):
            self.assertEqual(bot.describe_current_codex_model("thread-1"), "Codex")

    def test_describe_current_codex_model_falls_back_without_rollout_path(self):
        with (
            patch.object(bot, "CODEX_MODEL_LABEL", "Codex"),
            patch.object(bot, "read_codex_thread_meta", return_value={"thread": {}}),
        ):
            self.assertEqual(bot.describe_current_codex_model("thread-1"), "Codex")

    def test_with_model_label_is_off_by_default(self):
        with patch.object(bot, "SHOW_MODEL_LABEL", False):
            self.assertEqual(bot.with_model_label("codex", "Ответ"), "Ответ")

    def test_with_model_label_prepends_when_enabled(self):
        with (
            patch.object(bot, "SHOW_MODEL_LABEL", True),
            patch.object(bot, "DEEPSEEK_MODEL_LABEL", "DeepSeek V4 Flash"),
        ):
            self.assertEqual(
                bot.with_model_label("deepseek", "Ответ"),
                # Cosmetic superscript transform, not the raw label text —
                # see test_to_superscript_* for the transliteration itself.
                # Single newline separator, no blank-line gap.
                "ᵈᵉᵉᵖˢᵉᵉᵏ ᵛ⁴ ᶠˡᵃˢʰ\nОтвет",
            )

    def test_with_model_label_uses_dynamic_codex_model(self):
        # Codex's label is no longer the static "Codex" placeholder — it
        # goes through describe_current_codex_model, threaded the current
        # session's codex_thread_id.
        with (
            patch.object(bot, "SHOW_MODEL_LABEL", True),
            patch.object(
                bot, "describe_current_codex_model", return_value="Codex gpt-5.6-sol High"
            ) as describe,
        ):
            self.assertEqual(
                bot.with_model_label("codex", "Ответ", "thread-42"),
                "ᶜᵒᵈᵉˣ ᵍᵖᵗ-⁵.⁶-ˢᵒˡ ʰⁱᵍʰ\nОтвет",
            )
        describe.assert_called_once_with("thread-42")

    def test_to_superscript_transliterates_letters_and_digits(self):
        # Reference example approved live by the user.
        self.assertEqual(
            bot.to_superscript("Claude Sonnet5 High"),
            "ᶜˡᵃᵘᵈᵉ ˢᵒⁿⁿᵉᵗ⁵ ʰⁱᵍʰ",
        )

    def test_to_superscript_empty_string(self):
        self.assertEqual(bot.to_superscript(""), "")

    def test_to_superscript_leaves_unmapped_characters_alone(self):
        # "q" has no standard Unicode superscript code point (its other
        # letters still map normally), and punctuation like "(" ")" "-" is
        # intentionally left untouched.
        self.assertEqual(
            bot.to_superscript("Codex (gpt-5.6) queue"),
            "ᶜᵒᵈᵉˣ (ᵍᵖᵗ-⁵.⁶) qᵘᵉᵘᵉ",
        )

    def test_stateless_hybrid_drops_legacy_claude_session(self):
        with patch.object(bot, "CLAUDE_STATELESS", True):
            session = bot.normalize_hybrid_session("legacy-session")

        self.assertIsNone(session["claude"])
        self.assertEqual(session["last_backend"], "claude")

    def test_friendly_message_route_round_trip(self):
        sent = []
        peer = {"username": "TargetBot", "thread_id": 200}
        with (
            patch.object(bot, "AGENT_KEY", "motivator"),
            patch.object(bot, "BOT_USERNAME", "TargetBot"),
            patch.object(bot, "PEERS_BY_KEY", {"marketing": peer}),
            patch.object(
                bot,
                "send_text_to_thread",
                side_effect=lambda thread_id, text: sent.append((thread_id, text)),
            ),
        ):
            route_id = bot.dispatch_message("marketing", "Как дела?")

        # dispatch_message validates the target through the configured peer map.
        self.assertIsInstance(route_id, str)
        self.assertEqual(len(sent), 1)
        with patch.object(bot, "BOT_USERNAME", "TargetBot"):
            parsed = bot.parse_routed_message(sent[0][1])
        self.assertEqual(parsed["kind"], "message")
        self.assertEqual(parsed["body"], "Как дела?")

    def test_daily_schedule_runs_once_after_local_time(self):
        before = datetime(2026, 7, 23, 5, 59, tzinfo=timezone.utc)
        due = datetime(2026, 7, 23, 6, 0, tzinfo=timezone.utc)
        state = {}
        with (
            patch.object(bot, "DAILY_ENABLED", True),
            patch.object(bot, "AGENT_KEY", "motivator"),
            patch.object(bot, "DAILY_TIME", "09:00"),
            patch.object(bot, "DAILY_TIMEZONE", "Europe/Moscow"),
        ):
            self.assertFalse(bot.daily_run_due(state, before))
            self.assertTrue(bot.daily_run_due(state, due))
            state["scheduled_runs"] = {
                "daily_motivation": {"local_date": "2026-07-23"}
            }
            self.assertFalse(bot.daily_run_due(state, due))

    def test_manager_autonomy_schedule_runs_once_after_local_time(self):
        before = datetime(2026, 7, 30, 7, 59, tzinfo=timezone.utc)
        due = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
        state = {}
        with (
            patch.object(bot, "MANAGER_AUTONOMY_ENABLED", True),
            patch.object(bot, "AGENT_KEY", "manager"),
            patch.object(bot, "MANAGER_AUTONOMY_TIME", "11:00"),
            patch.object(bot, "MANAGER_AUTONOMY_TIMEZONE", "Europe/Moscow"),
        ):
            self.assertFalse(bot.manager_autonomy_run_due(state, before))
            self.assertTrue(bot.manager_autonomy_run_due(state, due))
            state["scheduled_runs"] = {
                "daily_manager_autonomy": {"local_date": "2026-07-30"}
            }
            self.assertFalse(bot.manager_autonomy_run_due(state, due))

    def test_manager_autonomy_skips_at_exact_threshold(self):
        state = {}
        current = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
        run_manager_autonomy = Mock(return_value=True)
        with (
            patch.object(bot, "MANAGER_AUTONOMY_ENABLED", True),
            patch.object(bot, "AGENT_KEY", "manager"),
            patch.object(bot, "MANAGER_AUTONOMY_TIME", "11:00"),
            patch.object(bot, "MANAGER_AUTONOMY_TIMEZONE", "Europe/Moscow"),
            patch.object(
                bot,
                "MANAGER_AUTONOMY_MIN_REMAINING_PERCENT",
                80,
            ),
            patch.object(
                bot,
                "read_rate_limit_status",
                return_value={"allowed": True, "remaining_percent": 80},
            ),
            patch.object(bot, "run_manager_autonomy", run_manager_autonomy),
            patch.object(bot, "save_state"),
        ):
            self.assertFalse(bot.maybe_run_manager_autonomy(state, current))

        run_manager_autonomy.assert_not_called()
        self.assertEqual(
            state["scheduled_runs"]["daily_manager_autonomy"]["status"],
            "skipped_low_limit",
        )
        self.assertEqual(
            state["scheduled_runs"]["daily_manager_autonomy"]["remaining_percent"],
            80,
        )

    def test_manager_autonomy_runs_above_threshold(self):
        state = {}
        current = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
        run_manager_autonomy = Mock(return_value=True)
        with (
            patch.object(bot, "MANAGER_AUTONOMY_ENABLED", True),
            patch.object(bot, "AGENT_KEY", "manager"),
            patch.object(bot, "MANAGER_AUTONOMY_TIME", "11:00"),
            patch.object(bot, "MANAGER_AUTONOMY_TIMEZONE", "Europe/Moscow"),
            patch.object(
                bot,
                "MANAGER_AUTONOMY_MIN_REMAINING_PERCENT",
                80,
            ),
            patch.object(
                bot,
                "read_rate_limit_status",
                return_value={"allowed": True, "remaining_percent": 81},
            ),
            patch.object(bot, "run_manager_autonomy", run_manager_autonomy),
            patch.object(bot, "save_state"),
        ):
            self.assertTrue(bot.maybe_run_manager_autonomy(state, current))

        run_manager_autonomy.assert_called_once()
        self.assertEqual(
            state["scheduled_runs"]["daily_manager_autonomy"]["status"],
            "completed",
        )
        self.assertEqual(
            state["scheduled_runs"]["daily_manager_autonomy"]["remaining_percent"],
            81,
        )

    def test_manager_autonomy_prompt_includes_owner_strategy_context(self):
        current = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)
        context = (
            "Первый приоритет — Россия; второй — Европа. "
            "Команда находится в Краснодаре и Бургасе."
        )
        with (
            patch.object(
                bot,
                "MANAGER_AUTONOMY_STRATEGY_CONTEXT",
                context,
            ),
            patch.object(
                bot,
                "MANAGER_AUTONOMY_ALLOWED_TARGETS",
                {"marketing", "developer"},
            ),
        ):
            prompt = bot.build_manager_autonomy_prompt(current, 95, {})

        self.assertIn(context, prompt)
        self.assertIn("Следуй указанному порядку рынков", prompt)
        self.assertIn("За один цикл выбирай один рынок", prompt)

    def test_manager_autonomy_rejects_multiple_tasks(self):
        answer = (
            "Предлагаю проверить два направления.\n"
            '<telegram_task target="marketing">Первая задача.</telegram_task>\n'
            '<telegram_task target="developer">Вторая задача.</telegram_task>'
        )
        send_text = Mock(return_value={"message_id": 10})
        edit_text = Mock()
        dispatch_task = Mock()
        with (
            patch.object(bot, "rate_limit_block_message", return_value=None),
            patch.object(bot, "run_turn", return_value=(answer, "session-id")),
            patch.object(bot, "send_text", send_text),
            patch.object(bot, "edit_text", edit_text),
            patch.object(bot, "dispatch_task", dispatch_task),
            patch.object(bot, "save_state"),
        ):
            success = bot.process_prompt(
                "Автономный цикл.",
                {"sessions": {}},
                "manager",
                allowed_task_targets={"marketing", "developer"},
                max_task_routes=1,
                allowed_message_targets=set(),
            )

        self.assertFalse(success)
        dispatch_task.assert_not_called()
        self.assertIn(
            "at most 1 delegated task",
            edit_text.call_args.args[1],
        )

    def test_people_are_due_at_different_personal_times(self):
        people = {
            "early_founder": {
                "username": "early_founder",
                "daily_time": "10:00",
                "daily_timezone": "Europe/Moscow",
            },
            "late_founder": {
                "username": "late_founder",
                "daily_time": "15:00",
                "daily_timezone": "Europe/Moscow",
            },
        }
        noon_moscow = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
        state = {}
        with (
            patch.object(bot, "DAILY_ENABLED", True),
            patch.object(bot, "AGENT_KEY", "motivator"),
            patch.object(bot, "PEOPLE_BY_USERNAME", people),
        ):
            self.assertEqual(
                bot.due_motivation_usernames(state, noon_moscow),
                ["early_founder"],
            )
            state["scheduled_runs"] = {
                "daily_motivation_people": {
                    "early_founder": {"local_date": "2026-07-24"}
                }
            }
            self.assertEqual(
                bot.due_motivation_usernames(state, noon_moscow),
                [],
            )

    def test_legacy_completed_round_prevents_same_day_duplicates(self):
        people = {
            "early_founder": {
                "username": "early_founder",
                "daily_time": "10:00",
                "daily_timezone": "Europe/Moscow",
            }
        }
        noon_moscow = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
        state = {
            "scheduled_runs": {
                "daily_motivation": {
                    "local_date": "2026-07-24",
                    "status": "completed",
                }
            }
        }
        with patch.object(bot, "PEOPLE_BY_USERNAME", people):
            self.assertEqual(
                bot.due_motivation_usernames(state, noon_moscow),
                [],
            )

    def test_motivation_prompt_contains_each_enabled_profile(self):
        peers = {
            "manager": {
                "display_name": "Участник",
                "role": "менеджер",
                "description": "Координирует сотрудников.",
            },
            "marketing": {
                "display_name": "Марина",
                "role": "маркетолог",
                "description": "Проверяет гипотезы.",
            },
            "disabled": {
                "display_name": "Не писать",
                "motivation_enabled": False,
            },
            "motivator": {"display_name": "Мотиватор"},
        }
        with (
            patch.object(bot, "PEERS_BY_KEY", peers),
            patch.object(bot, "PEOPLE_BY_USERNAME", {}),
            patch.object(bot, "AGENT_KEY", "motivator"),
        ):
            prompt = bot.build_motivation_prompt(
                datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
            )
        self.assertIn("Участник", prompt)
        self.assertIn("Марина", prompt)
        self.assertNotIn("Не писать", prompt)
        self.assertIn("telegram_message", prompt)

    def test_human_motivation_prompt_uses_private_profiles_and_direct_blocks(self):
        people = {
            "member_one": {
                "username": "member_one",
                "display_name": "Участник",
                "role": "основатель и разработчик",
                "description": "Создавал CRM и управлял командами.",
                "motivation_goal": "Помочь выбрать следующий небольшой шаг.",
                "cautions": "Не давить и не публиковать личные сложности.",
                "motivation_enabled": True,
            },
            "member_two": {
                "username": "member_two",
                "display_name": "Коллега",
                "role": "основатель и администратор",
                "description": "Сохраняет спокойствие и общается с клиентами.",
                "motivation_goal": "Поддерживать устойчивый рабочий ритм.",
                "news_interests": "технологии и управление",
                "allow_mild_profanity": True,
                "motivation_enabled": True,
            },
            "disabled_user": {
                "username": "disabled_user",
                "display_name": "Не писать",
                "motivation_enabled": False,
            },
        }
        with (
            patch.object(bot, "PEOPLE_BY_USERNAME", people),
            patch.object(bot, "AGENT_KEY", "motivator"),
        ):
            prompt = bot.build_motivation_prompt(
                datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc),
                state={
                    "motivation_history": {
                        "member_two": [
                            {
                                "local_date": "2026-07-22",
                                "text": "Предыдущее сообщение.",
                            }
                        ]
                    }
                },
            )

        self.assertIn("Участник", prompt)
        self.assertIn("Коллега", prompt)
        self.assertNotIn("Не писать", prompt)
        self.assertIn("telegram_human", prompt)
        self.assertIn("Не пересказывай публично возраст", prompt)
        self.assertIn("Не используй telegram_task и telegram_message", prompt)
        self.assertIn("По умолчанию не используй новости", prompt)
        self.assertIn("не запускай web search", prompt)
        self.assertIn("прямую ссылку", prompt)
        self.assertIn("Предыдущее сообщение", prompt)
        self.assertIn("не называй человека ленивым", prompt.lower())
        self.assertIn("Чего не работаешь?", prompt)
        self.assertIn("Боты кушать хотят", prompt)
        self.assertIn("Не пиши ничего до, после или между ними", prompt)
        self.assertNotIn("явно укажи, что это дружеское сообщение", prompt)

    def test_process_prompt_requires_one_message_per_employee(self):
        answer = (
            "Начинаю обход.\n"
            '<telegram_message target="manager">Привет, Участник!</telegram_message>\n'
            '<telegram_message target="marketing">Привет, Марина!</telegram_message>'
        )
        send_text = Mock(return_value={"message_id": 10})
        dispatch_message = Mock(return_value="route-id")
        peers = {
            "manager": {"username": "ManagerBot", "thread_id": 100},
            "marketing": {"username": "MarketingBot", "thread_id": 200},
        }
        with (
            patch.object(bot, "rate_limit_block_message", return_value=None),
            patch.object(bot, "run_turn", return_value=(answer, "session-id")),
            patch.object(bot, "send_text", send_text),
            patch.object(bot, "edit_text"),
            patch.object(bot, "dispatch_message", dispatch_message),
            patch.object(bot, "PEERS_BY_KEY", peers),
            patch.object(bot, "save_state"),
        ):
            success = bot.process_prompt(
                "daily",
                {"sessions": {}},
                "chat:topic",
                required_message_targets={"manager", "marketing"},
            )

        self.assertTrue(success)
        self.assertEqual(dispatch_message.call_count, 2)

    def test_process_prompt_sends_one_direct_message_per_person(self):
        answer = (
            "Начинаю дружеский обход.\n"
            '<telegram_human username="member_one">Участник, как настроение?</telegram_human>\n'
            '<telegram_human username="@member_two">Коллега, как дела?</telegram_human>'
        )
        send_text = Mock(return_value={"message_id": 10})
        edit_text = Mock()
        with (
            patch.object(bot, "rate_limit_block_message", return_value=None),
            patch.object(bot, "run_turn", return_value=(answer, "session-id")),
            patch.object(bot, "send_text", send_text),
            patch.object(bot, "edit_text", edit_text),
            patch.object(bot, "save_state"),
        ):
            success = bot.process_prompt(
                "daily",
                {"sessions": {}},
                "chat:topic",
                required_human_usernames={"member_one", "member_two"},
            )

        self.assertTrue(success)
        edit_text.assert_called_once_with(
            10,
            "@member_one\n\nУчастник, как настроение?",
        )
        direct_messages = [
            call.args[0]
            for call in send_text.call_args_list
            if call.args and call.args[0].startswith("@")
        ]
        self.assertEqual(
            direct_messages,
            [
                "@member_two\n\nКоллега, как дела?",
            ],
        )

    def test_process_prompt_repairs_wrong_human_block_format_once(self):
        wrong = (
            '<telegram_message target="member_one">Привет!</telegram_message>'
        )
        corrected = (
            '<telegram_human username="member_one">'
            "Участник, это дружеский привет.</telegram_human>"
        )
        run_turn = Mock(
            side_effect=[
                (wrong, "session-id"),
                (corrected, "session-id"),
            ]
        )
        send_text = Mock(return_value={"message_id": 10})
        edit_text = Mock()
        with (
            patch.object(bot, "rate_limit_block_message", return_value=None),
            patch.object(bot, "run_turn", run_turn),
            patch.object(bot, "send_text", send_text),
            patch.object(bot, "edit_text", edit_text),
            patch.object(bot, "save_state"),
            patch.object(
                bot,
                "PEERS_BY_KEY",
                {
                    "member_one": {
                        "username": "SomeBot",
                        "thread_id": 100,
                    }
                },
            ),
            patch.object(bot, "dispatch_message", return_value="route-id"),
        ):
            success = bot.process_prompt(
                "daily",
                {"sessions": {}},
                "chat:topic",
                required_human_usernames={"member_one"},
            )

        self.assertTrue(success)
        self.assertEqual(run_turn.call_count, 2)
        self.assertIn(
            '<telegram_human username="member_one">',
            run_turn.call_args_list[1].args[0],
        )
        self.assertTrue(
            edit_text.call_args
            and edit_text.call_args.args
            == (
                10,
                "@member_one\n\nУчастник, это дружеский привет.",
            )
        )

    def test_codex_watcher_baselines_existing_messages_without_sending(self):
        state = {}
        with (
            patch.object(bot, "CODEX_WATCH_ENABLED", True),
            patch.object(bot, "CODEX_DEFAULT_THREAD_ID", "thread-1"),
            patch.object(
                bot,
                "read_codex_final_messages",
                return_value=[
                    (
                        "item-old",
                        '<telegram_human username="member_one">'
                        "Старое сообщение.</telegram_human>",
                    )
                ],
            ),
            patch.object(bot, "send_text") as send_text,
            patch.object(bot, "save_state") as save_state,
        ):
            bot.initialize_codex_watcher(state)

        send_text.assert_not_called()
        save_state.assert_called_once_with(state)
        self.assertTrue(
            state["codex_watcher"]["threads"]["thread-1"]["initialized"]
        )
        self.assertIn(
            "item-old",
            state["codex_watcher"]["threads"]["thread-1"]["seen_item_ids"],
        )

    def test_codex_watcher_forwards_only_configured_human_blocks_once(self):
        state = {
            "codex_watcher": {
                "threads": {
                    "thread-1": {
                        "initialized": True,
                        "seen_item_ids": {},
                    }
                }
            }
        }
        answer = (
            "Отправляю.\n"
            '<telegram_human username="member_one">'
            "Участник, добрый день!</telegram_human>"
        )
        with (
            patch.object(bot, "CODEX_WATCH_ENABLED", True),
            patch.object(bot, "CODEX_DEFAULT_THREAD_ID", "thread-1"),
            patch.object(
                bot,
                "PEOPLE_BY_USERNAME",
                {"member_one": {"username": "member_one"}},
            ),
            patch.object(
                bot,
                "read_codex_final_messages",
                return_value=[("item-new", answer)],
            ),
            patch.object(bot, "send_text") as send_text,
            patch.object(bot, "save_state"),
        ):
            first = bot.poll_codex_watcher(state)
            second = bot.poll_codex_watcher(state)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        send_text.assert_called_once_with(
            "@member_one\n\nУчастник, добрый день!"
        )

    def test_codex_watcher_ignores_bridge_owned_answer(self):
        answer = (
            '<telegram_human username="member_one">'
            "Уже отправлено мостом.</telegram_human>"
        )
        state = {
            "codex_watcher": {
                "ignored_answer_hashes": [bot.codex_answer_hash(answer)],
                "threads": {
                    "thread-1": {
                        "initialized": True,
                        "seen_item_ids": {},
                    }
                },
            }
        }
        with (
            patch.object(bot, "CODEX_WATCH_ENABLED", True),
            patch.object(bot, "CODEX_DEFAULT_THREAD_ID", "thread-1"),
            patch.object(
                bot,
                "PEOPLE_BY_USERNAME",
                {"member_one": {"username": "member_one"}},
            ),
            patch.object(
                bot,
                "read_codex_final_messages",
                return_value=[("item-bridge", answer)],
            ),
            patch.object(bot, "send_text") as send_text,
            patch.object(bot, "save_state"),
        ):
            sent = bot.poll_codex_watcher(state)

        self.assertEqual(sent, 0)
        send_text.assert_not_called()
        self.assertEqual(
            state["codex_watcher"]["ignored_answer_hashes"],
            [],
        )

    def test_codex_watcher_rejects_unknown_human_username(self):
        state = {
            "codex_watcher": {
                "threads": {
                    "thread-1": {
                        "initialized": True,
                        "seen_item_ids": {},
                    }
                }
            }
        }
        answer = (
            '<telegram_human username="unknown_person">'
            "Это не должно уйти.</telegram_human>"
        )
        with (
            patch.object(bot, "CODEX_WATCH_ENABLED", True),
            patch.object(bot, "CODEX_DEFAULT_THREAD_ID", "thread-1"),
            patch.object(bot, "PEOPLE_BY_USERNAME", {}),
            patch.object(
                bot,
                "read_codex_final_messages",
                return_value=[("item-unknown", answer)],
            ),
            patch.object(bot, "send_text") as send_text,
            patch.object(bot, "save_state"),
        ):
            sent = bot.poll_codex_watcher(state)

        self.assertEqual(sent, 0)
        send_text.assert_not_called()

    def test_person_username_is_bound_to_first_sender_id(self):
        people = {
            "member_one": {
                "username": "member_one",
                "display_name": "Участник",
            }
        }
        state = {}
        with (
            patch.object(bot, "PEOPLE_BY_USERNAME", people),
            patch.object(bot, "save_state") as save_state,
        ):
            person = bot.person_for_sender(101, "Member_One", state)
            impostor = bot.person_for_sender(202, "member_one", state)
            renamed = bot.person_for_sender(101, "new_username", state)

        self.assertEqual(person["display_name"], "Участник")
        self.assertIsNone(impostor)
        self.assertEqual(renamed["display_name"], "Участник")
        self.assertEqual(state["people_user_ids"]["member_one"], 101)
        save_state.assert_called_once_with(state)

    def test_motivator_reply_to_person_allows_only_that_human_target(self):
        answer = (
            '<telegram_human username="member_one">'
            "Участник, бэклог никуда не убежит 😄</telegram_human>"
        )
        run_turn = Mock(return_value=(answer, "session-id"))
        send_text = Mock(return_value={"message_id": 10})
        edit_text = Mock()
        people = {
            "member_one": {
                "username": "member_one",
                "display_name": "Участник",
                "telegram_user_id": 42,
                "role": "основатель",
            }
        }
        with (
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 100),
            patch.object(bot, "AGENT_KEY", "motivator"),
            patch.object(bot, "PEOPLE_BY_USERNAME", people),
            patch.object(bot, "ALLOWED_USER_IDS", {42}),
            patch.object(bot, "set_reaction"),
            patch.object(bot, "save_state"),
            patch.object(bot, "rate_limit_block_message", return_value=None),
            patch.object(bot, "run_turn", run_turn),
            patch.object(bot, "send_text", send_text),
            patch.object(bot, "edit_text", edit_text),
        ):
            bot.handle_message(
                {
                    "chat": {"id": -1001},
                    "from": {
                        "id": 42,
                        "is_bot": False,
                        "username": "Member_One",
                    },
                    "message_thread_id": 100,
                    "message_id": 9,
                    "text": "Вполне возможно 😄",
                },
                {"sessions": {}},
            )

        self.assertIn(
            "ровно в одном telegram_human-блоке для @member_one",
            run_turn.call_args.args[0],
        )
        edit_text.assert_called_once_with(
            10,
            "@member_one\n\nУчастник, бэклог никуда не убежит 😄",
        )
        self.assertEqual(send_text.call_count, 1)

    def test_inbound_friendly_message_returns_one_reply(self):
        inbound = (
            "/message@TargetBot\n"
            "Task-ID: motivator-1\n"
            "From-Agent: motivator\n"
            "Reply-Agent: motivator\n"
            "Hop: 1\n"
            "Part: 1/1\n\n"
            "Марина, как дела?"
        )
        run_turn = Mock(return_value=("Всё хорошо, спасибо!", "session-id"))
        dispatch_reply = Mock()
        with (
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 100),
            patch.object(bot, "BOT_USERNAME", "TargetBot"),
            patch.object(
                bot,
                "PEERS_BY_BOT_ID",
                {
                    77: {
                        "key": "motivator",
                        "username": "MotivatorBot",
                        "thread_id": 300,
                    }
                },
            ),
            patch.object(
                bot,
                "PEERS_BY_KEY",
                {
                    "motivator": {
                        "key": "motivator",
                        "username": "MotivatorBot",
                        "thread_id": 300,
                    }
                },
            ),
            patch.object(bot, "set_reaction"),
            patch.object(bot, "save_state"),
            patch.object(bot, "rate_limit_block_message", return_value=None),
            patch.object(bot, "run_turn", run_turn),
            patch.object(bot, "send_text", return_value={"message_id": 10}),
            patch.object(bot, "edit_text"),
            patch.object(bot, "dispatch_reply", dispatch_reply),
        ):
            bot.handle_message(
                {
                    "chat": {"id": -1001},
                    "from": {"id": 77, "is_bot": True},
                    "message_thread_id": 100,
                    "message_id": 9,
                    "text": inbound,
                },
                {"sessions": {}},
            )

        self.assertIn("личное дружеское сообщение", run_turn.call_args.args[0])
        dispatch_reply.assert_called_once_with(
            "motivator",
            "motivator-1",
            "Всё хорошо, спасибо!",
            1,
        )

    def test_handler_does_not_start_codex_when_limit_is_low(self):
        FakeRateLimitClient.snapshot = {
            "planType": "plus",
            "primary": {"usedPercent": 99, "windowDurationMins": 300},
            "spendControlReached": False,
            "rateLimitReachedType": None,
        }
        send_text = Mock(return_value={"message_id": 1})
        run_turn = Mock(side_effect=AssertionError("Executor must not run"))
        message = {
            "chat": {"id": -1001},
            "from": {"id": 42, "is_bot": False},
            "message_thread_id": 100,
            "message_id": 5,
            "text": "Выполни задачу",
        }

        with (
            patch.object(bot, "CodexAppServerClient", FakeRateLimitClient),
            patch.object(bot, "RATE_LIMIT_MIN_REMAINING_PERCENT", 10),
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 100),
            patch.object(bot, "ALLOWED_USER_IDS", {42}),
            patch.object(bot, "send_text", send_text),
            patch.object(bot, "set_reaction"),
            patch.object(bot, "run_turn", run_turn),
        ):
            bot.handle_message(message, {"sessions": {}})

        run_turn.assert_not_called()
        self.assertIn("Задача не запущена", send_text.call_args.args[0])

    def test_allow_all_chat_members_accepts_unknown_human_in_target_topic(self):
        run_turn = Mock(return_value=("Готово", "session-id"))
        with (
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 100),
            patch.object(bot, "ALLOW_ALL_CHAT_MEMBERS", True),
            patch.object(bot, "ALLOWED_USER_IDS", set()),
            patch.object(bot, "set_reaction"),
            patch.object(bot, "save_state"),
            patch.object(bot, "rate_limit_block_message", return_value=None),
            patch.object(bot, "run_turn", run_turn),
            patch.object(bot, "send_text", return_value={"message_id": 10}),
            patch.object(bot, "edit_text"),
        ):
            bot.handle_message(
                {
                    "chat": {"id": -1001},
                    "from": {
                        "id": 777,
                        "is_bot": False,
                        "username": "new_member",
                    },
                    "message_thread_id": 100,
                    "message_id": 9,
                    "text": "Помоги с задачей",
                },
                {"sessions": {}},
            )

        run_turn.assert_called_once()

    def test_allow_all_chat_members_keeps_topic_boundary(self):
        run_turn = Mock()
        with (
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 100),
            patch.object(bot, "ALLOW_ALL_CHAT_MEMBERS", True),
            patch.object(bot, "ALLOWED_USER_IDS", set()),
            patch.object(bot, "run_turn", run_turn),
            patch.object(bot, "set_reaction"),
        ):
            bot.handle_message(
                {
                    "chat": {"id": -1001},
                    "from": {
                        "id": 777,
                        "is_bot": False,
                        "username": "new_member",
                    },
                    "message_thread_id": 999,
                    "message_id": 9,
                    "text": "Сообщение не в топике агента",
                },
                {"sessions": {}},
            )

        run_turn.assert_not_called()

    def test_allow_all_chat_members_keeps_unknown_bots_blocked(self):
        run_turn = Mock()
        with (
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 100),
            patch.object(bot, "ALLOW_ALL_CHAT_MEMBERS", True),
            patch.object(bot, "ALLOWED_USER_IDS", set()),
            patch.object(bot, "PEERS_BY_BOT_ID", {}),
            patch.object(bot, "run_turn", run_turn),
            patch.object(bot, "set_reaction"),
        ):
            bot.handle_message(
                {
                    "chat": {"id": -1001},
                    "from": {
                        "id": 888,
                        "is_bot": True,
                        "username": "UnknownBot",
                    },
                    "message_thread_id": 100,
                    "message_id": 9,
                    "text": "Сообщение от неизвестного бота",
                },
                {"sessions": {}},
            )

        run_turn.assert_not_called()

    def test_log_backend_usage_appends_jsonl_line(self):
        import json as json_module

        with tempfile.TemporaryDirectory() as temporary_dir:
            log_path = Path(temporary_dir) / "state" / "backend-usage.jsonl"
            with patch.object(bot, "BACKEND_USAGE_LOG_FILE", log_path):
                bot.log_backend_usage(
                    "codex",
                    "Codex",
                    {
                        "message_id": 5,
                        "chat": {"id": -1001},
                        "message_thread_id": 3,
                    },
                )
                bot.log_backend_usage(
                    "claude",
                    "Claude Sonnet5 High",
                    {"message_id": 6, "chat": {"id": -1001}, "message_thread_id": 3},
                )

            lines = log_path.read_text(encoding="utf-8").strip().splitlines()

        self.assertEqual(len(lines), 2)
        first = json_module.loads(lines[0])
        self.assertEqual(first["backend"], "codex")
        self.assertEqual(first["model"], "Codex")
        self.assertEqual(first["message_id"], 5)
        self.assertEqual(first["chat_id"], -1001)
        self.assertEqual(first["thread_id"], 3)
        self.assertIn("ts", first)
        second = json_module.loads(lines[1])
        self.assertEqual(second["backend"], "claude")
        self.assertEqual(second["model"], "Claude Sonnet5 High")
        self.assertEqual(second["message_id"], 6)

    def test_log_backend_usage_write_failure_does_not_raise(self):
        broken_path = Mock()
        broken_path.parent.mkdir.side_effect = OSError("disk full")
        with patch.object(bot, "BACKEND_USAGE_LOG_FILE", broken_path):
            bot.log_backend_usage("deepseek", "DeepSeek V4 Flash", {"message_id": 1})
        # Reaching this line without an exception is the assertion: a
        # logging failure must never propagate out of log_backend_usage.


if __name__ == "__main__":
    unittest.main()


class TopiclessGroupTests(unittest.TestCase):
    """A group without topics is configured with TELEGRAM_THREAD_ID=0."""

    def test_send_text_omits_the_thread_id_when_there_is_no_topic(self):
        with patch.object(bot, "TARGET_CHAT_ID", -1001), patch.object(bot, "telegram") as tg:
            bot.send_text_to_thread(0, "привет")
        payload = tg.call_args[0][1]
        self.assertNotIn("message_thread_id", payload)
        self.assertEqual(payload["chat_id"], -1001)

    def test_send_text_still_targets_the_topic_when_there_is_one(self):
        with patch.object(bot, "TARGET_CHAT_ID", -1001), patch.object(bot, "telegram") as tg:
            bot.send_text_to_thread(3, "привет")
        self.assertEqual(tg.call_args[0][1]["message_thread_id"], 3)

    def test_send_document_omits_the_thread_id_for_a_topicless_group(self):
        with (
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 0),
            patch.object(bot, "telegram_multipart") as tm,
        ):
            bot.send_document(Path("/tmp/report.txt"))
        self.assertNotIn("message_thread_id", tm.call_args[0][1])

    def test_require_config_no_longer_demands_a_topic(self):
        with (
            patch.object(bot, "BOT_TOKEN", "t"),
            patch.object(bot, "TARGET_CHAT_ID", -1001),
            patch.object(bot, "TARGET_THREAD_ID", 0),
            patch.object(bot, "ALLOW_ALL_CHAT_MEMBERS", True),
            patch.object(bot, "AGENT_KEY", "a"),
            patch.object(bot, "PEERS_FILE", Path(__file__)),
        ):
            try:
                bot.require_config()
            except RuntimeError as exc:
                self.assertNotIn("TELEGRAM_THREAD_ID", str(exc))
