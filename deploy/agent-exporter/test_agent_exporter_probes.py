#!/usr/bin/env python3
"""The exporter's probes and its per-backend collection, with every outside
call (tmux, systemctl, HTTP, the App Server) replaced by a fake.

test_agent_exporter.py covers the pure helpers; this file covers what turns a
host's real state into the JSON agentdesk receives -- which is where a wrong
branch means a wrong status in the panel.

Run: python3 test_agent_exporter_probes.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_FIXTURE_DIR = tempfile.mkdtemp(prefix="agent-exporter-probes-")
_BOOTSTRAP = os.path.join(_FIXTURE_DIR, "bootstrap.fleet.json")
with open(_BOOTSTRAP, "w", encoding="utf-8") as _f:
    json.dump({"server": "t", "fleet": [], "bot_units": {}}, _f)
os.environ["AGENT_EXPORTER_CONFIG"] = _BOOTSTRAP
sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_exporter as ae  # noqa: E402

LOGGED_IN_PANE = "● hello\n bypass permissions on (shift+tab to cycle) · /rc\n"


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="ae-", dir=_FIXTURE_DIR))


class RunTest(unittest.TestCase):
    def test_a_successful_command_returns_its_output(self):
        self.assertEqual(ae.run(["echo", "hi"]), "hi\n")

    def test_failure_timeout_and_a_missing_program_all_read_as_none(self):
        self.assertIsNone(ae.run(["false"]))
        self.assertIsNone(ae.run(["sleep", "5"], timeout=0.05))
        self.assertIsNone(ae.run(["definitely-not-a-program-xyz"]))

    def test_another_user_is_reached_through_sudo_but_root_is_not(self):
        seen = []
        with mock.patch.object(ae.subprocess, "run", side_effect=lambda cmd, **k: seen.append(cmd) or mock.Mock(stdout="", returncode=0)):
            ae.run(["tmux", "ls"], user="exampleuser")
            ae.run(["tmux", "ls"], user="root")
            ae.run(["tmux", "ls"])
        self.assertEqual(seen[0][:4], ["sudo", "-n", "-u", "exampleuser"])
        self.assertEqual(seen[1], ["tmux", "ls"])
        self.assertEqual(seen[2], ["tmux", "ls"])

    def test_unit_active_accepts_active_and_activating_only(self):
        for out, want in (("active\n", True), ("activating\n", True), ("inactive\n", False), (None, False)):
            with mock.patch.object(ae, "run", return_value=out):
                self.assertIs(ae.unit_active("x.service"), want, out)


class TmuxCaptureTest(unittest.TestCase):
    def agent(self, **kw):
        base = dict(tenant="t", agent="a", backend="claude", user="exampleuser", tmux_socket="/s.sock", tmux_session="sess")
        base.update(kw)
        return ae.Agent(**base)

    def test_no_session_configured_means_nothing_to_capture(self):
        self.assertIsNone(ae.tmux_capture(self.agent(tmux_socket=None)))
        self.assertIsNone(ae.tmux_capture(self.agent(tmux_session=None)))

    def test_a_dead_session_is_none_a_live_one_returns_the_pane(self):
        calls = []

        def run(cmd, user=None, timeout=15):
            calls.append(cmd)
            if "has-session" in cmd:
                return ""
            return "PANE"

        with mock.patch.object(ae, "run", run):
            self.assertEqual(ae.tmux_capture(self.agent()), "PANE")
        self.assertEqual(calls[0][:3], ["tmux", "-S", "/s.sock"])
        with mock.patch.object(ae, "run", lambda cmd, user=None, timeout=15: None):
            self.assertIsNone(ae.tmux_capture(self.agent()))

    def test_the_default_socket_is_not_passed_with_dash_S(self):
        seen = []
        with mock.patch.object(ae, "run", lambda cmd, user=None, timeout=15: seen.append(cmd) or ""):
            ae.tmux_capture(self.agent(tmux_socket="default"))
        self.assertNotIn("-S", seen[0])


class TranscriptsTest(unittest.TestCase):
    def test_newest_first_and_limited(self):
        root = tmp()
        paths = []
        for i in range(4):
            d = root / "proj{}".format(i)
            d.mkdir()
            p = d / "s.jsonl"
            p.write_text("{}")
            os.utime(p, (1000 + i, 1000 + i))
            paths.append(p)
        got = ae.recent_transcripts(str(root), limit=2)
        self.assertEqual(got, [paths[3], paths[2]])
        self.assertEqual(ae.recent_transcripts(str(root / "missing")), [])

    def test_tail_bytes_reads_only_the_end_and_survives_a_missing_file(self):
        f = tmp() / "big.jsonl"
        f.write_text("A" * 1000 + "END")
        self.assertEqual(ae.tail_bytes(f, size=10), "A" * 7 + "END")
        self.assertEqual(ae.tail_bytes(tmp() / "none"), "")

    def test_model_and_effort_come_from_the_last_real_assistant_turn(self):
        root = tmp()
        d = root / "p"
        d.mkdir()
        lines = [
            {"type": "assistant", "message": {"model": "claude-opus-4-5"}, "effort": "high"},
            {"type": "user"},
            {"type": "assistant", "message": {"model": "<synthetic>"}},
            "not json",
        ]
        (d / "s.jsonl").write_text("\n".join(l if isinstance(l, str) else json.dumps(l) for l in lines))
        agent = ae.Agent(tenant="t", agent="a", backend="claude", transcript_dir=str(root))
        got = ae.claude_model_effort(agent)
        self.assertEqual((got["model"], got["effort"]), ("claude-opus-4-5", "high"))
        self.assertNotEqual(got["transcript_age"], "")
        none = ae.claude_model_effort(ae.Agent(tenant="t", agent="a", backend="claude"))
        self.assertEqual(none["model"], "")
        empty = ae.claude_model_effort(ae.Agent(tenant="t", agent="a", backend="claude", transcript_dir=str(tmp())))
        self.assertEqual(empty["model"], "")


class HttpTest(unittest.TestCase):
    def test_http_json_returns_parsed_json_or_none(self):
        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"a": 1}'

        with mock.patch.object(ae.urllib.request, "urlopen", return_value=R()):
            self.assertEqual(ae.http_json("http://x", {}), {"a": 1})
        with mock.patch.object(ae.urllib.request, "urlopen", side_effect=ae.urllib.error.URLError("down")):
            self.assertIsNone(ae.http_json("http://x", {}))

    def test_claude_usage_needs_a_token_and_sends_it_as_a_bearer(self):
        d = tmp()
        creds = d / ".credentials.json"
        agent = ae.Agent(tenant="t", agent="a", backend="claude", credentials=str(creds))
        self.assertEqual(ae.claude_usage(ae.Agent(tenant="t", agent="a", backend="claude")), {})
        self.assertEqual(ae.claude_usage(agent), {})  # unreadable
        creds.write_text(json.dumps({"claudeAiOauth": {}}))
        self.assertEqual(ae.claude_usage(agent), {})  # no token
        creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
        with mock.patch.object(ae, "http_json", return_value={"five_hour": {}}) as hj:
            self.assertEqual(ae.claude_usage(agent), {"five_hour": {}})
        self.assertEqual(hj.call_args[0][1]["Authorization"], "Bearer tok")
        with mock.patch.object(ae, "http_json", return_value=None):
            self.assertEqual(ae.claude_usage(agent), {})


class CodexAndDeepseekProbesTest(unittest.TestCase):
    def test_codex_login_states(self):
        self.assertIsNone(ae.codex_logged_in(None))
        home = tmp()
        self.assertIs(ae.codex_logged_in(str(home)), False)
        (home / "auth.json").write_text("not json")
        self.assertIs(ae.codex_logged_in(str(home)), False)
        (home / "auth.json").write_text("[]")
        self.assertIs(ae.codex_logged_in(str(home)), False)
        (home / "auth.json").write_text("{}")
        self.assertIs(ae.codex_logged_in(str(home)), False)
        (home / "auth.json").write_text('{"tokens": {}}')
        self.assertIs(ae.codex_logged_in(str(home)), True)

    def test_codex_model_and_effort_come_from_the_newest_rollout(self):
        home = tmp()
        day = home / "sessions" / "2026" / "10" / "01"
        day.mkdir(parents=True)
        old = day / "rollout-a.jsonl"
        new = day / "rollout-b.jsonl"
        old.write_text(json.dumps({"payload": {"type": "thread_settings_applied", "thread_settings": {"model": "old", "reasoning_effort": "low"}}}))
        new.write_text("\n".join([
            json.dumps({"payload": {"type": "thread_settings_applied", "thread_settings": {"model": "gpt-5", "reasoning_effort": "high"}}}),
            "garbage",
            json.dumps({"payload": {"type": "other"}}),
        ]))
        os.utime(old, (1, 1))
        got = ae.codex_model_effort(ae.Agent(tenant="t", agent="a", backend="codex", codex_home=str(home)))
        self.assertEqual(got, {"model": "gpt-5", "effort": "high"})
        self.assertEqual(ae.codex_model_effort(ae.Agent(tenant="t", agent="a", backend="codex")), {"model": "", "effort": ""})
        self.assertEqual(ae.codex_model_effort(ae.Agent(tenant="t", agent="a", backend="codex", codex_home=str(tmp()))), {"model": "", "effort": ""})

    def test_codex_probe_degrades_to_empty(self):
        a = ae.Agent(tenant="t", agent="a", backend="codex", codex_socket="/nope.sock")
        self.assertEqual(ae.codex_probe(a), {})
        self.assertEqual(ae.codex_probe(ae.Agent(tenant="t", agent="a", backend="codex")), {})

        class Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def request(self, method, params):
                return {"rl": 1} if "rateLimits" in method else {"data": []}

        sock = tmp() / "s.sock"
        sock.write_text("")
        a = ae.Agent(tenant="t", agent="a", backend="codex", codex_socket=str(sock))
        with mock.patch.object(ae, "CodexAppServerClient", Client):
            got = ae.codex_probe(a)
        self.assertEqual(got["rate_limits"], {"rl": 1})

        class Broken(Client):
            def request(self, *a):
                raise RuntimeError("down")

        with mock.patch.object(ae, "CodexAppServerClient", Broken):
            self.assertEqual(ae.codex_probe(a), {})

    def test_deepseek_balance_reads_the_key_from_the_env_file_and_closes_it(self):
        env = tmp() / "ds.env"
        a = ae.Agent(tenant="t", agent="a", backend="deepseek", deepseek_env=str(env))
        self.assertEqual(ae.deepseek_balance(a), {})  # no file
        env.write_text("OTHER=1\n")
        self.assertEqual(ae.deepseek_balance(a), {})  # no key
        env.write_text("DEEPSEEK_API_KEY='sk-123'\n")
        with mock.patch.object(ae, "http_json", return_value={"is_available": True}) as hj:
            self.assertEqual(ae.deepseek_balance(a), {"is_available": True})
        self.assertEqual(hj.call_args[0][1]["Authorization"], "Bearer sk-123")
        self.assertEqual(ae.deepseek_balance(ae.Agent(tenant="t", agent="a", backend="deepseek")), {})


class CollectByBackendTest(unittest.TestCase):
    def setUp(self):
        self._orig = (ae.SERVER, ae.FLEET, ae.BOT_UNITS)
        ae.SERVER, ae.BOT_UNITS = "srv", {}

    def tearDown(self):
        ae.SERVER, ae.FLEET, ae.BOT_UNITS = self._orig

    def collect(self, agent, **patches):
        ae.FLEET = [agent]
        defaults = dict(
            unit_active=lambda unit: True,
            tmux_capture=lambda a: LOGGED_IN_PANE,
            bridge_connected=lambda a: None,
            claude_model_effort=lambda a: {"model": "claude-opus-4-5", "effort": "high", "transcript_age": "12.34"},
            claude_usage=lambda a: {},
        )
        defaults.update(patches)
        with mock.patch.multiple(ae, **defaults):
            out = ae.collect_agents()
        self.assertEqual(len(out), 1)
        return out[0]

    def claude(self, **kw):
        base = dict(tenant="t", agent="a", backend="claude", unit="a.service", user="exampleuser", tmux_socket="/s", tmux_session="x")
        base.update(kw)
        return ae.Agent(**base)

    def test_a_healthy_claude_agent(self):
        usage = {
            "five_hour": {"utilization": 41, "resets_at": "2026-10-01T12:00:00.000Z"},
            "seven_day": {"utilization": 10},
            "spend": {"used": {"amount_minor": 1250, "exponent": 2}},
            "extra_usage": {"is_enabled": True},
        }
        row = self.collect(self.claude(), claude_usage=lambda a: usage)
        self.assertTrue(row["online"] and row["up"])
        self.assertEqual((row["model"], row["effort"], row["plan"]), ("claude-opus-4-5", "high", "extra_usage_on"))
        self.assertEqual([l["window"] for l in row["limits"]], ["five_hour", "seven_day"])
        self.assertIn("resets_at", row["limits"][0])
        self.assertEqual(row["balances"], [{"kind": "spent", "usd": 12.5}])
        self.assertEqual(row["last_activity_seconds"], 12.3)
        self.assertEqual(row["bad_states"], [])
        self.assertIs(row["remote_control"], True)  # from the status bar
        json.dumps(row)

    def test_a_live_bad_state_takes_the_agent_offline_with_its_reset_time(self):
        pane = "● old\nusage limit reached\nresets 12:50pm (UTC)\n"
        row = self.collect(self.claude(), tmux_capture=lambda a: pane)
        self.assertEqual(row["bad_states"], ["limit_reached"])
        self.assertFalse(row["online"])
        self.assertEqual(row["limit_reset"], "12:50pm (UTC)")

    def test_a_missing_tmux_session_is_a_probe_error_and_offline(self):
        row = self.collect(self.claude(), tmux_capture=lambda a: None)
        self.assertFalse(row["online"])
        self.assertIn("tmux", row["probe_errors"])

    def test_the_session_file_overrides_the_pane_for_remote_control(self):
        row = self.collect(self.claude(), bridge_connected=lambda a: False)
        self.assertIs(row["remote_control"], False)

    def test_a_brand_new_session_shows_the_model_from_the_welcome_header(self):
        pane = "Sonnet 5 · Claude Pro\n bypass permissions on\n"
        row = self.collect(self.claude(), tmux_capture=lambda a: pane,
                           claude_model_effort=lambda a: {"model": "", "effort": "", "transcript_age": ""})
        self.assertEqual(row["model"], "claude-sonnet-5")
        self.assertIsNone(row["last_activity_seconds"])

    def test_no_usage_answer_is_reported_not_hidden(self):
        row = self.collect(self.claude())
        self.assertIn("usage_api", row["probe_errors"])

    def test_a_headless_claude_without_tmux_is_judged_by_its_unit(self):
        a = self.claude(tmux_socket=None, tmux_session=None)
        row = self.collect(a, unit_active=lambda unit: False)
        self.assertFalse(row["up"] or row["online"])
        self.assertNotIn("tmux", row["probe_errors"])

    def test_codex_limits_credits_login_and_activity(self):
        probe = {
            "rate_limits": {"rateLimits": {"planType": "plus", "primary": {"usedPercent": 30, "windowDurationMins": 300, "resetsAt": 1800000000},
                                           "credits": {"balance": "4.5"}}},
            "threads": {"data": [{"updatedAt": time.time() - 60}, {"updatedAt": time.time() - 5}]},
        }
        a = ae.Agent(tenant="t", agent="a", backend="codex", unit="c.service", codex_home="/h")
        row = self.collect(a, codex_probe=lambda x: probe, codex_logged_in=lambda h: True,
                           codex_model_effort=lambda x: {"model": "gpt-5", "effort": "medium"})
        self.assertTrue(row["online"])
        self.assertEqual((row["plan"], row["model"], row["effort"]), ("plus", "gpt-5", "medium"))
        self.assertEqual(row["limits"][0]["window"], "w300min")
        self.assertEqual(row["balances"], [{"kind": "credits", "usd": 4.5}])
        self.assertLess(row["last_activity_seconds"], 10)

    def test_codex_bad_states(self):
        a = ae.Agent(tenant="t", agent="a", backend="codex", unit="c.service", codex_home="/h")
        row = self.collect(a, codex_probe=lambda x: {}, codex_logged_in=lambda h: False,
                           codex_model_effort=lambda x: {"model": "", "effort": ""})
        self.assertFalse(row["online"])
        self.assertEqual(row["bad_states"], ["not_logged_in"])
        self.assertIn("app_server", row["probe_errors"])
        spend = {"rate_limits": {"rateLimits": {"spendControlReached": True, "primary": {}}}}
        row = self.collect(a, codex_probe=lambda x: spend, codex_logged_in=lambda h: True,
                           codex_model_effort=lambda x: {"model": "", "effort": ""})
        self.assertIn("spend_control", row["bad_states"])
        self.assertFalse(row["online"])

    def test_deepseek_availability_and_balances(self):
        a = ae.Agent(tenant="t", agent="a", backend="deepseek", unit="d.service")
        ok = {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "3.2"}, {"currency": "CNY", "total_balance": "bad"}]}
        row = self.collect(a, deepseek_balance=lambda x: ok)
        self.assertTrue(row["online"])
        self.assertEqual(row["balances"], [{"kind": "total", "currency": "USD", "usd": 3.2}])
        row = self.collect(a, deepseek_balance=lambda x: {"is_available": False})
        self.assertEqual(row["bad_states"], ["no_balance"])
        self.assertFalse(row["online"])
        row = self.collect(a, deepseek_balance=lambda x: {})
        self.assertIn("balance_api", row["probe_errors"])

    def test_hybrid_siblings_report_which_one_is_routing(self):
        state = tmp() / "state.json"
        state.write_text(json.dumps({"channel_backend_mode": "codex"}))
        a = self.claude(backend_state_file=str(state))
        row = self.collect(a)
        self.assertIs(row["active_backend"], False)


class CycleTest(unittest.TestCase):
    def test_a_failing_collection_pushes_nothing_so_the_panel_keeps_its_last_state(self):
        with mock.patch.object(ae, "collect_agents", side_effect=RuntimeError("boom")), \
                mock.patch.object(ae, "push") as push:
            self.assertFalse(ae.cycle("http://x", "tok"))
        push.assert_not_called()

    def test_a_good_collection_is_pushed(self):
        with mock.patch.object(ae, "collect_agents", return_value=[{"a": 1}]), mock.patch.object(ae, "push") as push:
            self.assertTrue(ae.cycle("http://x", "tok"))
        push.assert_called_once_with("http://x", "tok", [{"a": 1}])

    def test_once_prints_the_collection_and_a_missing_config_is_refused(self):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with mock.patch.object(ae, "collect_agents", return_value=[{"x": 1}]), mock.patch.object(sys, "argv", ["x", "--once"]), redirect_stdout(buf):
            ae.main()
        self.assertEqual(json.loads(buf.getvalue()), [{"x": 1}])
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(sys, "argv", ["x"]):
            os.environ.pop("AGENTDESK_URL", None)
            os.environ.pop("AGENT_EXPORTER_TOKEN", None)
            with self.assertRaises(SystemExit):
                ae.main()

    def test_main_loops_cycle_and_sleep(self):
        env = {"AGENTDESK_URL": "http://ad/", "AGENT_EXPORTER_TOKEN": "tok"}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", ["x"]), \
                mock.patch.object(ae, "cycle") as cycle, mock.patch.object(ae.time, "sleep", side_effect=[None, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                ae.main()
        self.assertEqual(cycle.call_args[0][0], "http://ad/api/fleet/heartbeat")
        self.assertEqual(cycle.call_count, 2)


if __name__ == "__main__":
    unittest.main()
