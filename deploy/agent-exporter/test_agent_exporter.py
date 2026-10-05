#!/usr/bin/env python3
"""Tests for agent_exporter.py's standalone logic: JSON collection shape
(collect_agents), the heartbeat push itself, fleet-config parsing
(including the string-vs-object bot_units back-compat shim),
hybrid-backend active detection, and the bad-state regexes. Deliberately
NOT covered: the live probes themselves (tmux/subprocess/HTTP calls) —
those need a real host to exercise meaningfully and are exactly what
--once smoke-testing against a real fleet.json is for at deploy time.

Run: python3 test_agent_exporter.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# agent_exporter.py loads its fleet config at IMPORT time (module-level
# `SERVER, FLEET, BOT_UNITS = load_fleet_config(CONFIG_PATH)`) — point it
# at a throwaway fixture before importing so these tests never depend on,
# or risk touching, a real host's hosts/*.fleet.json.
_FIXTURE_DIR = tempfile.mkdtemp(prefix="agent-exporter-test-")
_BOOTSTRAP_CONFIG = os.path.join(_FIXTURE_DIR, "bootstrap.fleet.json")
with open(_BOOTSTRAP_CONFIG, "w", encoding="utf-8") as _f:
    json.dump({"server": "test", "fleet": [], "bot_units": {}}, _f)
os.environ["AGENT_EXPORTER_CONFIG"] = _BOOTSTRAP_CONFIG

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_exporter as ae  # noqa: E402


def _write_json(name: str, data) -> str:
    path = os.path.join(_FIXTURE_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


class CollectAgentsTest(unittest.TestCase):
    """collect_agents() for a bot_unit entry -- the one shape that needs no
    live tmux/subprocess/HTTP probing beyond `systemctl is-active` on a
    unit name that plainly doesn't exist (which just reads as inactive,
    same as any other never-started unit), so it's safe to exercise for
    real rather than needing a live host."""

    def setUp(self):
        self._orig = (ae.SERVER, ae.FLEET, ae.BOT_UNITS)
        ae.SERVER = "test-server"
        ae.FLEET = []
        ae.BOT_UNITS = {
            "acme-manager": ae.BotUnit(
                unit="definitely-not-a-real-unit.service",
                telegram_bot="@AcmeManagerBot", backend="codex",
                model="gpt-5.6-sol", remote_control=True,
                role="Менеджер", project="Acme",
            )
        }

    def tearDown(self):
        ae.SERVER, ae.FLEET, ae.BOT_UNITS = self._orig

    def test_bot_unit_produces_one_json_serializable_dict(self):
        agents = ae.collect_agents()
        self.assertEqual(len(agents), 1)
        a = agents[0]
        json.dumps(a)  # must round-trip through JSON with no surprises
        self.assertEqual(a["server"], "test-server")
        self.assertEqual(a["tenant"], "acme-manager")
        self.assertEqual(a["agent"], "telegram-bot")
        self.assertEqual(a["backend"], "codex")
        self.assertFalse(a["up"])  # the fake unit name is never active
        self.assertFalse(a["online"])
        self.assertEqual(a["bad_states"], ["service_down"])
        self.assertEqual(a["probe_errors"], [])
        self.assertEqual(a["telegram_bot"], "@AcmeManagerBot")
        self.assertEqual(a["model"], "gpt-5.6-sol")
        self.assertIs(a["remote_control"], True)
        self.assertEqual(a["role"], "Менеджер")
        self.assertEqual(a["project"], "Acme")

    def test_unset_tri_state_fields_are_json_null_not_missing(self):
        # A Go *bool field decodes "null" as nil and "false" as a real
        # false -- collect_agents() must emit an actual None (-> JSON
        # null), never omit the key or coerce it to False.
        ae.BOT_UNITS["acme-manager"].remote_control = None
        a = ae.collect_agents()[0]
        self.assertIn("remote_control", a)
        self.assertIsNone(a["remote_control"])
        self.assertIsNone(a["active_backend"])

    @mock.patch.object(ae, "unit_active")
    def test_live_bot_with_dead_backend_is_not_online(self, unit_active):
        unit_active.side_effect = lambda unit: unit == "telegram.service"
        ae.BOT_UNITS = {
            "acme-manager": ae.BotUnit(
                unit="telegram.service",
                backend_unit="codex-remote-control.service",
            )
        }
        a = ae.collect_agents()[0]
        self.assertTrue(a["up"])
        self.assertFalse(a["online"])
        self.assertEqual(a["bad_states"], ["backend_down"])

    @mock.patch.object(ae, "unit_active", return_value=True)
    def test_live_bot_with_empty_codex_auth_requires_login(self, _unit_active):
        codex_home = os.path.join(_FIXTURE_DIR, "empty-codex")
        os.makedirs(codex_home, exist_ok=True)
        Path(codex_home, "auth.json").write_text("", encoding="utf-8")
        ae.BOT_UNITS = {
            "acme-manager": ae.BotUnit(
                unit="telegram.service",
                backend_unit="codex-remote-control.service",
                codex_home=codex_home,
            )
        }
        a = ae.collect_agents()[0]
        self.assertTrue(a["up"])
        self.assertFalse(a["online"])
        self.assertEqual(a["bad_states"], ["not_logged_in"])


class PushTest(unittest.TestCase):
    """push() is the only network side effect this script has now that
    there's no Prometheus /metrics endpoint to scrape -- verify it POSTs
    the right JSON body to the right path with the bearer token attached,
    against a real (if throwaway) HTTP server rather than mocking
    urllib.request out from under it."""

    def test_push_sends_authenticated_json_post(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                received["path"] = self.path
                received["auth"] = self.headers.get("Authorization")
                length = int(self.headers.get("Content-Length", "0"))
                received["body"] = json.loads(self.rfile.read(length))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = "http://127.0.0.1:{}/api/fleet/heartbeat".format(server.server_port)
            ae.push(url, "s3cr3t-token", [{"tenant": "acme-manager"}])
        finally:
            server.shutdown()
            thread.join(timeout=5)

        self.assertEqual(received.get("path"), "/api/fleet/heartbeat")
        self.assertEqual(received.get("auth"), "Bearer s3cr3t-token")
        self.assertEqual(received.get("body"), {"agents": [{"tenant": "acme-manager"}]})

    def test_push_swallows_connection_errors(self):
        # An unreachable agentdesk (network blip, restart) must not raise
        # out of push() and kill the loop in main() — it's caught and
        # logged to stderr, then the next PUSH_INTERVAL tick tries again.
        try:
            ae.push("http://127.0.0.1:1/api/fleet/heartbeat", "tok", [])
        except Exception as exc:  # pragma: no cover - the assertion is that this doesn't happen
            self.fail("push() raised {!r} instead of swallowing the connection error".format(exc))


class LoadFleetConfigTest(unittest.TestCase):
    def test_bare_string_bot_unit_is_backward_compatible(self):
        path = _write_json("bare-string.json", {
            "server": "server-a",
            "fleet": [],
            "bot_units": {"example-manager": "example-manager.service"},
        })
        server, fleet, bots = ae.load_fleet_config(path)
        self.assertEqual(server, "server-a")
        self.assertEqual(fleet, [])
        self.assertEqual(bots["example-manager"].unit, "example-manager.service")
        self.assertEqual(bots["example-manager"].backend, "codex")  # BotUnit's own default

    def test_object_bot_unit(self):
        path = _write_json("object-bot-unit.json", {
            "server": "server-a",
            "fleet": [],
            "bot_units": {
                "example-manager": {
                    "unit": "example-manager.service",
                    "telegram_bot": "@ExampleManagerBot",
                    "model": "gpt-5.6-sol",
                    "remote_control": True,
                    "backend_unit": "example-agent-remote-control.service",
                }
            },
        })
        _, _, bots = ae.load_fleet_config(path)
        bot = bots["example-manager"]
        self.assertEqual(bot.telegram_bot, "@ExampleManagerBot")
        self.assertEqual(bot.model, "gpt-5.6-sol")
        self.assertTrue(bot.remote_control)
        self.assertEqual(bot.backend_unit, "example-agent-remote-control.service")

    def test_fleet_entries_become_agent_dataclasses(self):
        path = _write_json("fleet-entry.json", {
            "server": "server-b",
            "fleet": [{"tenant": "example-developer", "agent": "claude", "backend": "claude",
                       "telegram_bot": "@ExampleDeveloperBot"}],
            "bot_units": {},
        })
        server, fleet, _ = ae.load_fleet_config(path)
        self.assertEqual(server, "server-b")
        self.assertEqual(len(fleet), 1)
        self.assertIsInstance(fleet[0], ae.Agent)
        self.assertEqual(fleet[0].telegram_bot, "@ExampleDeveloperBot")

    def test_agent_exporter_server_env_overrides_file(self):
        path = _write_json("server-override.json", {"server": "from-file", "fleet": [], "bot_units": {}})
        os.environ["AGENT_EXPORTER_SERVER"] = "from-env"
        try:
            server, _, _ = ae.load_fleet_config(path)
        finally:
            del os.environ["AGENT_EXPORTER_SERVER"]
        self.assertEqual(server, "from-env")


class IsActiveBackendTest(unittest.TestCase):
    """is_active_backend() backs agent_meta.active_backend, which the
    agentdesk dashboard uses to decide which of a hybrid tenant's sibling
    backends (e.g. example-developer: claude/codex/deepseek) to show as
    the live one vs. just "also available". Getting "unknown" wrong here
    means the dashboard either hides a real backend or fabricates an
    active one that isn't."""

    @staticmethod
    def _agent(backend: str, state_file: "str | None" = None) -> "ae.Agent":
        return ae.Agent(tenant="x", agent=backend, backend=backend, backend_state_file=state_file)

    def test_no_state_file_configured_is_unknown(self):
        self.assertIsNone(ae.is_active_backend(self._agent("claude")))

    def test_missing_file_is_unknown_not_an_error(self):
        agent = self._agent("claude", "/nonexistent/path/bot-state.json")
        self.assertIsNone(ae.is_active_backend(agent))

    def test_malformed_json_is_unknown(self):
        path = os.path.join(_FIXTURE_DIR, "malformed-state.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self.assertIsNone(ae.is_active_backend(self._agent("claude", path)))

    def test_missing_channel_backend_mode_key_is_unknown(self):
        path = _write_json("no-mode-key.json", {"sessions": {}})
        self.assertIsNone(ae.is_active_backend(self._agent("claude", path)))

    def test_matching_and_non_matching_backends(self):
        path = _write_json("matching-mode.json", {"channel_backend_mode": "claude"})
        self.assertTrue(ae.is_active_backend(self._agent("claude", path)))
        self.assertFalse(ae.is_active_backend(self._agent("codex", path)))
        self.assertFalse(ae.is_active_backend(self._agent("deepseek", path)))


class BadStatePatternsTest(unittest.TestCase):
    """BAD_STATES' own comment says these are what real incidents looked
    like on the pane — this test pins each pattern to its real example so
    a future regex tweak can't silently stop catching the incident it was
    written for."""

    def test_each_pattern_matches_its_real_example(self):
        examples = {
            "not_logged_in": "Not logged in. Run /login to continue.",
            "resume_prompt": "Do you want to resume from summary?",
            "limit_reached": "You've reached your usage limit reached for this window.",
            "error_state": "API Error: Credit balance is too low to make this request.",
        }
        by_name = dict(ae.BAD_STATES)
        for name, example in examples.items():
            with self.subTest(state=name):
                self.assertTrue(by_name[name].search(example), "{} didn't match {!r}".format(name, example))

    def test_limit_reached_matches_the_actual_session_limit_message(self):
        # The literal phrasing Claude Code showed live during the
        # example-developer-two ("Coder") incident this session -- the original
        # pattern (just "usage limit reached|rate limit") never matched it,
        # so the incident was invisible to agentdesk end to end.
        real = "You've hit your session limit · resets 12:50pm (UTC)"
        by_name = dict(ae.BAD_STATES)
        self.assertTrue(by_name["limit_reached"].search(real))

    def test_healthy_status_bar_matches_no_bad_state(self):
        healthy = "bypass permissions on (shift+tab to cycle) · ← for agents      /rc active"
        for name, pattern in ae.BAD_STATES:
            with self.subTest(state=name):
                self.assertIsNone(pattern.search(healthy))


def _stuck_states(pane: str):
    """Exercises the real collect_agents detection logic end to end (find
    each pattern's LAST matching line, then check pane_is_stale_match)
    without needing a live tmux pane -- the same two steps
    agent_exporter.py's own per-agent loop performs."""
    lines = pane.split("\n")
    stuck = []
    for state, pattern in ae.BAD_STATES:
        last_match = None
        for i, line in enumerate(lines):
            if pattern.search(line):
                last_match = i
        if last_match is not None and not ae.pane_is_stale_match(lines, last_match):
            stuck.append(state)
    return stuck


class BadStateStalenessTest(unittest.TestCase):
    """Regression tests for the scrollback false-positive found live
    TWICE (sample-developer, then demo-developer): after a successful
    relogin, the pane still showed its own old "Not logged in" message
    (`claude --continue` restores the whole prior transcript) with real,
    successful new exchanges below it -- and kept being reported
    not_logged_in because BAD_STATES used to search the whole pane with
    no notion of "has anything actually happened since this line."

    A fixed trailing-line-count window was tried first and already fixed
    the sample case, but broke again live on demo (see
    SHORT_REPLY_STILL_WITHIN_A_10_LINE_TAIL below) because a short reply
    right after the fix can leave the old line well within even a
    generous line count. The real fix checks for a genuine new assistant
    turn (a "●" line) after the match, which is what actually
    distinguishes "resolved, not yet scrolled off" from "still frozen."
    """

    # A successful relogin can leave an old status line in scrollback even
    # after the agent has answered a new message.
    RESOLVED_BUT_STILL_VISIBLE = "\n".join([
        "  Готово. Проверка завершена, временный файл можно удалить.",
        "",
        "✻ Sautéed for 22s",
        "",
        "● Remote Control disconnected — Claude.ai login expired — run /login to",
        "  restore Remote Control",
        "",
        "❯ привет, ты тут?",
        "",
        "● Привет! Да, тут. Новая задача принята.",
        "",
        "✻ Crunched for 15s",
        "                                                    /remote-control is active · Continue here, on your phone, or at",
        "  https://example.com/session/one",
        "  You've used 90% of your session limit · resets 9:20pm (UTC) · /upgrade to k…",
        "───────────────────────────────────────────────────────── host_sample_developer ─",
        "❯ ",
        "────────────────────────────────────────────────────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents      /rc active",
    ])

    # A single short exchange after relogin can leave
    # "Remote Control disconnected" well
    # within even a 10-non-blank-line trailing window (it was the false
    # negative that proved the line-count fix wasn't the real answer).
    SHORT_REPLY_STILL_WITHIN_A_10_LINE_TAIL = "\n".join([
        "  Called plugin:telegram:telegram",
        "",
        "● Ответил на «Тест1».",
        "",
        "✻ Crunched for 3s",
        "",
        "  /remote-control is active · Continue here, on your phone, or at",
        "  https://example.com/session/two",
        "",
        "● Remote Control disconnected — Claude.ai login expired — run /login to",
        "  restore Remote Control",
        "",
        "❯ привет, работаешь?",
        "",
        "● Привет! Да, работаю, на связи.",
        "",
        "✻ Cogitated for 2s",
        "",
        "───────────────────────────────────────────────────────── host_demo_developer ─",
        "❯ ",
        "────────────────────────────────────────────────────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents             /rc",
    ])

    # A genuine logged-out state must still be caught.
    GENUINELY_STUCK_NOT_LOGGED_IN = "\n".join([
        "● Remote Control disconnected — Claude.ai login expired — run /login to",
        "  restore Remote Control",
        "                                                    Not logged in · Run /login",
        "───────────────────────────────────────────────────────── host_sample_developer ─",
        "❯ ",
        "────────────────────────────────────────────────────────────────────────────────",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents",
    ])

    # A genuine usage-limit menu must still be caught. This also rules out
    # "any ❯ line after the match counts as resolved": the menu's own
    # current selection renders as "❯ 1. Stop and wait...", which is NOT
    # a new conversational turn.
    GENUINELY_STUCK_LIMIT_MENU = "\n".join([
        "← telegram · example_user: обнови на v3 example_web и example_api",
        "",
        "  Called plugin:telegram:telegram",
        "  ⎿  You've hit your session limit · resets 12:50pm (UTC)",
        "",
        "▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔",
        "   What do you want to do?",
        "",
        "   ❯ 1. Stop and wait for limit to reset",
        "     2. Upgrade your plan",
        "",
        "   Enter to confirm · Esc to cancel",
    ])

    def test_resolved_incident_no_longer_matches_once_a_reply_follows(self):
        self.assertNotIn(
            "not_logged_in",
            _stuck_states(self.RESOLVED_BUT_STILL_VISIBLE),
            "a resolved incident still visible only in scrollback must not "
            "be reported as a current bad state",
        )

    def test_resolved_incident_not_flagged_even_with_a_short_reply(self):
        self.assertNotIn(
            "not_logged_in",
            _stuck_states(self.SHORT_REPLY_STILL_WITHIN_A_10_LINE_TAIL),
            "one short new reply is enough evidence of recovery, "
            "regardless of how few lines separate it from the old message",
        )

    def test_genuine_not_logged_in_incident_still_caught(self):
        self.assertIn("not_logged_in", _stuck_states(self.GENUINELY_STUCK_NOT_LOGGED_IN))

    def test_limit_reset_time_is_read_from_the_pane(self):
        lines = ["x", "  ⎿  You've hit your session limit · resets 12:50pm (UTC)", "", "❯ 1. Stop and wait for limit to reset"]
        self.assertEqual(ae.limit_reset_from_pane(lines, 1), "12:50pm (UTC)")
        self.assertEqual(ae.limit_reset_from_pane(["nothing"], 0), "")
        boxed = ["│ You've hit your limit · resets Sep 27, 3am (UTC)  │"]
        self.assertEqual(ae.limit_reset_from_pane(boxed, 0), "Sep 27, 3am (UTC)")

    def test_genuine_limit_reached_incident_still_caught(self):
        self.assertIn("limit_reached", _stuck_states(self.GENUINELY_STUCK_LIMIT_MENU))


if __name__ == "__main__":
    unittest.main()


class RemoteControlFromPaneTest(unittest.TestCase):
    BAR = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents      "

    def test_reads_the_status_bar_marker(self):
        self.assertIs(ae.remote_control_from_pane(self.BAR + "/rc active"), True)
        self.assertIs(ae.remote_control_from_pane(self.BAR + "/rc"), True)
        self.assertIs(ae.remote_control_from_pane(self.BAR + "/rc failed"), False)

    def test_unknown_word_is_unknown_not_on(self):
        self.assertIsNone(ae.remote_control_from_pane(self.BAR + "/rc connecting"))

    def test_no_marker_on_a_real_bar_means_off(self):
        self.assertIs(ae.remote_control_from_pane(self.BAR), False)

    def test_no_status_bar_means_cannot_tell(self):
        self.assertIsNone(ae.remote_control_from_pane("Do you want to proceed?\n ❯ 1. Yes"))


class ClaudeModelFromPaneTest(unittest.TestCase):
    def test_reads_brand_new_session_welcome_header(self):
        pane = "▝▜██████▀  Sonnet 5 · Claude Pro\nWelcome to Claude Code"
        self.assertEqual(ae.claude_model_from_pane(pane), "claude-sonnet-5")

    def test_normalizes_dotted_model_version(self):
        self.assertEqual(
            ae.claude_model_from_pane("Haiku 4.5 · Claude Pro"),
            "claude-haiku-4-5",
        )

    def test_unrelated_pane_has_no_guessed_model(self):
        self.assertEqual(ae.claude_model_from_pane("❯ waiting for input"), "")


class BridgeConnectedTest(unittest.TestCase):
    def agent(self):
        return ae.Agent(tenant="t", agent="claude", backend="claude", unit="t.service", user="u",
                        tmux_socket="/home/u/.claude/t.tmux.sock", tmux_session="t",
                        transcript_dir="/home/u/.claude/projects")

    def fake_run(self, pid, session_json):
        def run(cmd, timeout=15, user=None):
            if "display-message" in cmd:
                return pid
            if cmd[0] == "cat":
                self.read = cmd[1]
                return session_json
            raise AssertionError(cmd)
        return run

    def test_connected_when_the_session_file_has_a_bridge_id(self):
        with mock.patch.object(ae, "run", self.fake_run("4242\n", '{"pid": 4242, "bridgeSessionId": "session_abc"}')):
            self.assertIs(ae.bridge_connected(self.agent()), True)
        self.assertEqual(self.read, "/home/u/.claude/sessions/4242.json")

    def test_disconnected_when_the_bridge_id_is_absent_or_empty(self):
        for body in ('{"pid": 1}', '{"bridgeSessionId": null}', '{"bridgeSessionId": ""}'):
            with mock.patch.object(ae, "run", self.fake_run("1\n", body)):
                self.assertIs(ae.bridge_connected(self.agent()), False, body)

    def test_unknown_when_there_is_nothing_to_read(self):
        with mock.patch.object(ae, "run", self.fake_run("", "")):
            self.assertIsNone(ae.bridge_connected(self.agent()))
        with mock.patch.object(ae, "run", self.fake_run("7\n", None)):
            self.assertIsNone(ae.bridge_connected(self.agent()))
        with mock.patch.object(ae, "run", self.fake_run("7\n", "not json")):
            self.assertIsNone(ae.bridge_connected(self.agent()))
