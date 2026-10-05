"""bot.py's configuration checks, peer/people files, Telegram HTTP retry rules
and the usage summary. A wrong value here makes the bridge refuse to start (or,
worse, start half-configured), so each rule has its own case.
"""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neurobot import bot  # noqa: E402


def tmp_file(content, name="f.json"):
    d = Path(tempfile.mkdtemp())
    p = d / name
    p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return p


def good_config(**over):
    peers = tmp_file({"agents": {"manager": {"bot_id": 1, "thread_id": 2, "username": "@m_bot"}}})
    cfg = dict(
        BOT_TOKEN="123:abc", TARGET_CHAT_ID=-100, ALLOW_ALL_CHAT_MEMBERS=True, ALLOWED_USER_IDS=set(),
        AGENT_KEY="manager", PEERS_FILE=peers, EXECUTOR="codex", HYBRID_EXECUTOR_ORDER=[],
        RATE_LIMIT_MIN_REMAINING_PERCENT=10, MANAGER_AUTONOMY_MIN_REMAINING_PERCENT=10,
        DAILY_ENABLED=False, MANAGER_AUTONOMY_ENABLED=False, CODEX_WATCH_ENABLED=False,
        PEOPLE_FILE=None,
    )
    cfg.update(over)
    return cfg


class RequireConfigTest(unittest.TestCase):
    def check(self, **over):
        with patch.multiple(bot, **good_config(**over)):
            bot.require_config()

    def refused(self, text, **over):
        with self.assertRaises(RuntimeError) as cm:
            self.check(**over)
        self.assertIn(text, str(cm.exception))

    def test_a_complete_configuration_passes(self):
        self.check()

    def test_each_missing_required_value_is_named(self):
        self.refused("TELEGRAM_BOT_TOKEN", BOT_TOKEN="")
        self.refused("TELEGRAM_CHAT_ID", TARGET_CHAT_ID=0)
        self.refused("NEUROBOT_AGENT_KEY", AGENT_KEY="")
        self.refused("NEUROBOT_PEERS_FILE", PEERS_FILE=Path("/no/such/peers.json"))

    def test_someone_must_be_allowed_to_talk_to_the_bot(self):
        self.refused("TELEGRAM_ALLOWED_USER_IDS", ALLOW_ALL_CHAT_MEMBERS=False, ALLOWED_USER_IDS=set())
        self.check(ALLOW_ALL_CHAT_MEMBERS=False, ALLOWED_USER_IDS={42})

    def test_all_missing_values_are_reported_together(self):
        with self.assertRaises(RuntimeError) as cm:
            self.check(BOT_TOKEN="", AGENT_KEY="")
        self.assertIn("TELEGRAM_BOT_TOKEN, NEUROBOT_AGENT_KEY", str(cm.exception))

    def test_executor_and_hybrid_order(self):
        self.refused("NEUROBOT_EXECUTOR", EXECUTOR="gpt")
        self.refused("HYBRID_EXECUTOR_ORDER", EXECUTOR="hybrid", HYBRID_EXECUTOR_ORDER=["claude"])
        self.refused("HYBRID_EXECUTOR_ORDER", EXECUTOR="hybrid", HYBRID_EXECUTOR_ORDER=["claude", "claude"])
        self.check(EXECUTOR="hybrid", HYBRID_EXECUTOR_ORDER=["claude", "codex"])

    def test_percent_thresholds_are_bounded(self):
        self.refused("CODEX_MIN_REMAINING_PERCENT", RATE_LIMIT_MIN_REMAINING_PERCENT=101)
        self.refused("MANAGER_AUTONOMY_MIN_REMAINING_PERCENT", MANAGER_AUTONOMY_MIN_REMAINING_PERCENT=100)
        self.check(RATE_LIMIT_MIN_REMAINING_PERCENT=0)

    def test_daily_initiative_needs_a_valid_time_zone_and_its_people_file(self):
        self.refused("Unknown NEUROBOT_DAILY_TIMEZONE", DAILY_ENABLED=True, DAILY_TIME="09:00", DAILY_TIMEZONE="Mars/Base")
        self.refused("HH:MM", DAILY_ENABLED=True, DAILY_TIME="9am", DAILY_TIMEZONE="UTC")
        self.refused("NEUROBOT_PEOPLE_FILE", DAILY_ENABLED=True, DAILY_TIME="09:00", DAILY_TIMEZONE="UTC", AGENT_KEY="motivator", PEOPLE_FILE=None)

    def test_manager_autonomy_rules(self):
        base = dict(MANAGER_AUTONOMY_ENABLED=True, MANAGER_AUTONOMY_TIME="09:00", MANAGER_AUTONOMY_TIMEZONE="UTC",
                    MANAGER_AUTONOMY_ALLOWED_TARGETS={"marketing"})
        self.refused("requires NEUROBOT_AGENT_KEY=manager", AGENT_KEY="marketing", **base)
        self.refused("requires NEUROBOT_EXECUTOR=codex", EXECUTOR="claude", **base)
        self.refused("must not be empty", **dict(base, MANAGER_AUTONOMY_ALLOWED_TARGETS=set()))
        self.refused("Unknown NEUROBOT_MANAGER_AUTONOMY_TIMEZONE", **dict(base, MANAGER_AUTONOMY_TIMEZONE="Nowhere/Land"))
        self.check(**base)

    def test_codex_watcher_rules(self):
        self.refused("requires NEUROBOT_EXECUTOR=codex", CODEX_WATCH_ENABLED=True, EXECUTOR="claude")
        self.refused("only the motivator", CODEX_WATCH_ENABLED=True)
        self.refused("CODEX_DEFAULT_THREAD_ID", CODEX_WATCH_ENABLED=True, AGENT_KEY="motivator",
                     CODEX_DEFAULT_THREAD_ID="", PEOPLE_FILE=tmp_file("{}"))


class ParseDailyTimeTest(unittest.TestCase):
    def test_valid_and_invalid_times(self):
        self.assertEqual(bot.parse_daily_time("09:05"), (9, 5))
        for bad in ("24:00", "12:60", "noon", "", "1"):
            with self.assertRaises(RuntimeError):
                bot.parse_daily_time(bad)

    def test_schedule_time_is_in_the_configured_zone(self):
        from datetime import datetime, timezone
        with patch.object(bot, "DAILY_TIMEZONE", "Asia/Tokyo"):
            local = bot.local_schedule_time(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(local.hour, 9)
            naive = bot.local_schedule_time(datetime(2026, 1, 1, 0, 0))
            self.assertEqual(naive.hour, 9)


class PeersAndPeopleFilesTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(bot.PEERS_BY_KEY.clear)
        self.addCleanup(bot.PEERS_BY_BOT_ID.clear)
        self.addCleanup(bot.PEOPLE_BY_USERNAME.clear)

    def peers(self, agents, **over):
        p = tmp_file({"agents": agents} if agents is not None else [1])
        with patch.multiple(bot, PEERS_FILE=p, AGENT_KEY="manager", MANAGER_AUTONOMY_ENABLED=False, **over):
            bot.initialize_peers()

    def test_peers_are_indexed_by_key_and_bot_id(self):
        self.peers({"Manager": {"bot_id": "10", "thread_id": "3", "username": "@m_bot"}, "skip": "not-a-dict"})
        self.assertEqual(bot.PEERS_BY_KEY["manager"]["bot_id"], 10)
        self.assertEqual(bot.PEERS_BY_KEY["manager"]["username"], "m_bot")
        self.assertIn(10, bot.PEERS_BY_BOT_ID)
        self.assertNotIn("skip", bot.PEERS_BY_KEY)

    def test_a_bad_peers_file_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.peers(None)
        with self.assertRaises(RuntimeError) as cm:
            self.peers({"other": {"bot_id": 1, "thread_id": 1, "username": "o"}})
        self.assertIn("missing from Neurobot peers", str(cm.exception))

    def test_manager_autonomy_targets_must_be_real_peers(self):
        p = tmp_file({"agents": {"manager": {"bot_id": 1, "thread_id": 1, "username": "m"}}})
        with patch.multiple(bot, PEERS_FILE=p, AGENT_KEY="manager", MANAGER_AUTONOMY_ENABLED=True,
                            MANAGER_AUTONOMY_ALLOWED_TARGETS={"ghost", "manager"}):
            with self.assertRaises(RuntimeError) as cm:
                bot.initialize_peers()
        self.assertIn("ghost, manager", str(cm.exception))

    def people(self, people):
        p = tmp_file({"people": people} if people is not None else [])
        with patch.object(bot, "PEOPLE_FILE", p):
            bot.initialize_people()

    def test_people_are_indexed_by_lowercase_username(self):
        self.people([{"username": "@ExampleUser", "telegram_user_id": "77", "daily_time": "10:30", "daily_timezone": "UTC"}, "skip"])
        self.assertEqual(bot.PEOPLE_BY_USERNAME["exampleuser"]["telegram_user_id"], 77)

    def test_invalid_people_entries_are_refused(self):
        for entry in ({"username": "ab"}, {"username": "good_name", "daily_time": "99:99"},
                      {"username": "good_name", "daily_timezone": "Nowhere/Land"}):
            with self.assertRaises(RuntimeError):
                self.people([entry])
        with self.assertRaises(RuntimeError):
            self.people(None)

    def test_no_people_file_means_no_people(self):
        bot.PEOPLE_BY_USERNAME["x"] = {}
        with patch.object(bot, "PEOPLE_FILE", None):
            bot.initialize_people()
        self.assertEqual(bot.PEOPLE_BY_USERNAME, {})


class UrlopenResult:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class TelegramCallTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(bot.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        p2 = patch.object(bot, "BOT_TOKEN", "123:SECRET")
        p2.start()
        self.addCleanup(p2.stop)

    def test_success_returns_the_result(self):
        with patch.object(bot.urllib.request, "urlopen", return_value=UrlopenResult({"ok": True, "result": {"id": 1}})) as u:
            self.assertEqual(bot.telegram("getMe"), {"id": 1})
        self.assertIn("/bot123:SECRET/getMe", u.call_args[0][0].full_url)

    def test_network_errors_are_retried_with_backoff_then_succeed(self):
        calls = [urllib.error.URLError("down"), TimeoutError(), UrlopenResult({"ok": True, "result": "fine"})]
        with patch.object(bot, "TELEGRAM_RETRY_ATTEMPTS", 3), patch.object(bot.urllib.request, "urlopen", side_effect=calls):
            self.assertEqual(bot.telegram("sendMessage", {"a": "b"}), "fine")
        self.assertEqual(self.sleep.call_count, 2)

    def test_persistent_network_failure_stops_after_the_attempts_without_the_token(self):
        with patch.object(bot, "TELEGRAM_RETRY_ATTEMPTS", 2), \
                patch.object(bot.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")) as u:
            with self.assertRaises(RuntimeError) as cm:
                bot.telegram("getMe")
        self.assertEqual(u.call_count, 2)
        self.assertIn("failed after 2 attempts", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))

    def test_a_real_api_rejection_is_not_retried(self):
        err = urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"description":"chat not found"}'))
        with patch.object(bot.urllib.request, "urlopen", side_effect=err) as u:
            with self.assertRaises(RuntimeError) as cm:
                bot.telegram("sendMessage")
        self.assertEqual(u.call_count, 1)
        self.assertIn("Telegram HTTP 400", str(cm.exception))
        with patch.object(bot.urllib.request, "urlopen", return_value=UrlopenResult({"ok": False, "description": "Forbidden"})) as u:
            with self.assertRaises(RuntimeError) as cm:
                bot.telegram("sendMessage")
        self.assertIn("Forbidden", str(cm.exception))
        self.assertEqual(u.call_count, 1)

    def test_multipart_upload_has_the_right_shape_and_the_same_retry_rules(self):
        f = tmp_file("hello", "report.txt")
        with patch.object(bot.urllib.request, "urlopen", return_value=UrlopenResult({"ok": True, "result": "sent"})) as u:
            self.assertEqual(bot.telegram_multipart("sendDocument", {"chat_id": 5}, "document", f), "sent")
        req = u.call_args[0][0]
        body = req.data.decode()
        self.assertIn('name="chat_id"', body)
        self.assertIn('name="document"; filename="report.txt"', body)
        self.assertIn("hello", body)
        self.assertIn("multipart/form-data; boundary=", req.headers["Content-type"])

        calls = [OSError("reset"), UrlopenResult({"ok": True, "result": 1})]
        with patch.object(bot.urllib.request, "urlopen", side_effect=calls):
            self.assertEqual(bot.telegram_multipart("sendDocument", {}, "document", f), 1)
        err = urllib.error.HTTPError("u", 413, "big", {}, io.BytesIO(b"too large"))
        with patch.object(bot.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                bot.telegram_multipart("sendDocument", {}, "document", f)
        self.assertIn("413", str(cm.exception))
        with patch.object(bot.urllib.request, "urlopen", return_value=UrlopenResult({"ok": False, "description": "bad"})):
            with self.assertRaises(RuntimeError):
                bot.telegram_multipart("sendDocument", {}, "document", f)
        with patch.object(bot, "TELEGRAM_RETRY_ATTEMPTS", 2), patch.object(bot.urllib.request, "urlopen", side_effect=OSError("x")):
            with self.assertRaises(RuntimeError) as cm:
                bot.telegram_multipart("sendDocument", {}, "document", f)
        self.assertIn("upload failed after 2 attempts", str(cm.exception))


class UsageLogTest(unittest.TestCase):
    def test_summary_counts_backends_and_ignores_corrupt_lines(self):
        log = tmp_file("\n".join([
            json.dumps({"backend": "claude"}), "garbage", json.dumps({"backend": "codex"}),
            json.dumps({"backend": "claude"}), json.dumps({"no": "backend"}), "",
        ]), "usage.jsonl")
        with patch.object(bot, "BACKEND_USAGE_LOG_FILE", log):
            text = bot.summarize_backend_usage()
        self.assertIn("claude=2, codex=1", text)
        with patch.object(bot, "BACKEND_USAGE_LOG_FILE", Path("/no/such/log")):
            self.assertEqual(bot.summarize_backend_usage(), "")
        empty = tmp_file("", "empty.jsonl")
        with patch.object(bot, "BACKEND_USAGE_LOG_FILE", empty):
            self.assertEqual(bot.summarize_backend_usage(), "")

    def test_only_the_tail_is_read_and_the_log_is_never_modified(self):
        lines = [json.dumps({"backend": "old"})] * 10 + [json.dumps({"backend": "new"})] * 3
        log = tmp_file("\n".join(lines), "usage.jsonl")
        before = log.read_text()
        with patch.object(bot, "BACKEND_USAGE_LOG_FILE", log):
            self.assertIn("new=3", bot.summarize_backend_usage(max_lines=3))
        self.assertEqual(log.read_text(), before)

    def test_logging_a_turn_appends_one_line_and_never_raises(self):
        d = Path(tempfile.mkdtemp())
        log = d / "sub" / "usage.jsonl"
        with patch.object(bot, "BACKEND_USAGE_LOG_FILE", log):
            bot.log_backend_usage("claude", "opus", {"message_id": 7, "chat": {"id": 5}, "message_thread_id": 9})
            bot.log_backend_usage("codex", "gpt")
        rows = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual([r["backend"] for r in rows], ["claude", "codex"])
        self.assertEqual((rows[0]["message_id"], rows[0]["chat_id"], rows[0]["thread_id"]), (7, 5, 9))
        # a failing disk is swallowed: the reply must still go out
        with patch.object(bot, "BACKEND_USAGE_LOG_FILE", Path("/proc/forbidden/usage.jsonl")):
            bot.log_backend_usage("claude", "x")


class StateFileTest(unittest.TestCase):
    def test_state_round_trips_and_a_missing_or_corrupt_file_starts_empty(self):
        d = Path(tempfile.mkdtemp())
        with patch.multiple(bot, STATE_DIR=d / "state", STATE_FILE=d / "state" / "s.json"):
            self.assertEqual(bot.load_state(), {"offset": 0, "sessions": {}})
            bot.save_state({"offset": 5, "sessions": {"a": "b"}})
            self.assertEqual(bot.load_state()["offset"], 5)
            (d / "state" / "s.json").write_text("{broken")
            self.assertEqual(bot.load_state(), {"offset": 0, "sessions": {}})
        self.assertFalse(list((d / "state").glob("*.tmp")), "the temporary file must be replaced, not left behind")


if __name__ == "__main__":
    unittest.main()
