#!/usr/bin/env python3
"""agent_scheduler: cron arithmetic, task validation, and the run engine driven
by a fake clock and a fake "type into the agent" function, so a day of
schedule takes milliseconds and nothing touches tmux.

Run: python3 test_agent_scheduler.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_scheduler as sch  # noqa: E402

UTC = ZoneInfo("UTC")
MSK = ZoneInfo("Europe/Moscow")
NY = ZoneInfo("America/New_York")


def ts(y, mo, d, h=0, mi=0, tz=UTC):
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


class CronTest(unittest.TestCase):
    def prev(self, expr, now):
        got = sch.Cron.parse(expr).previous(now)
        return got and got.strftime("%Y-%m-%d %H:%M")

    def nxt(self, expr, now):
        got = sch.Cron.parse(expr).following(now)
        return got and got.strftime("%Y-%m-%d %H:%M")

    def test_every_minute_and_steps(self):
        now = datetime(2026, 10, 2, 10, 7, 30, tzinfo=UTC)
        self.assertEqual(self.prev("* * * * *", now), "2026-10-02 10:07")
        self.assertEqual(self.prev("*/15 * * * *", now), "2026-10-02 10:00")
        self.assertEqual(self.nxt("*/15 * * * *", now), "2026-10-02 10:15")
        self.assertEqual(self.nxt("5/20 * * * *", now), "2026-10-02 10:25")  # 5, 25, 45

    def test_a_time_exactly_now_counts_as_previous_not_next(self):
        now = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
        self.assertEqual(self.prev("0 9 * * *", now), "2026-10-02 09:00")
        self.assertEqual(self.nxt("0 9 * * *", now), "2026-10-03 09:00")

    def test_ranges_lists_and_weekday_names(self):
        fri = datetime(2026, 10, 2, 18, 0, tzinfo=UTC)         # a Friday
        self.assertEqual(self.prev("0 9,17 * * mon-fri", fri), "2026-10-02 17:00")
        sat = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
        self.assertEqual(self.prev("0 9,17 * * mon-fri", sat), "2026-10-02 17:00")
        self.assertEqual(self.nxt("0 9,17 * * mon-fri", sat), "2026-10-05 09:00")

    def test_sunday_is_both_zero_and_seven(self):
        sun = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
        for expr in ("0 9 * * 0", "0 9 * * 7", "0 9 * * sun"):
            self.assertEqual(self.prev(expr, sun), "2026-10-04 09:00", expr)

    def test_day_of_month_and_weekday_are_alternatives_when_both_are_given(self):
        # "the 13th, or any Friday"
        now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)         # Monday the 12th
        self.assertEqual(self.nxt("0 9 13 * fri", now), "2026-10-13 09:00")
        thu = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
        self.assertEqual(self.nxt("0 9 13 * fri", thu), "2026-10-16 09:00")
        # a bare * in one of them leaves the other in charge
        self.assertEqual(self.nxt("0 9 13 * *", thu), "2026-11-13 09:00")

    def test_months_by_name_and_far_future_dates(self):
        now = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
        self.assertEqual(self.nxt("0 0 1 jan *", now), "2027-01-01 00:00")
        self.assertEqual(self.nxt("0 0 29 2 *", now), "2028-02-29 00:00")  # next leap day

    def test_a_date_that_never_exists_never_fires(self):
        now = datetime(2026, 10, 2, 0, 0, tzinfo=UTC)
        self.assertIsNone(sch.Cron.parse("0 0 31 2 *").following(now))

    def test_invalid_expressions(self):
        for bad in ("", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *",
                    "*/0 * * * *", "a * * * *", "5-1 * * * *", ", * * * *", "* * * * 8", "*/x * * * *"):
            with self.assertRaises(sch.ScheduleError, msg=bad):
                sch.Cron.parse(bad)

    def test_valid_expressions(self):
        # the same list is in agentdesk's internal/schedule/schedule_test.go
        for good in ("* * * * *", "*/15 9-17 * * mon-fri", "0 0 1 jan,jul *", "5/20 * * * *", "0 9 * * 7"):
            sch.Cron.parse(good)

    def test_the_time_zone_decides_what_9am_means(self):
        now_utc = datetime(2026, 10, 2, 7, 0, tzinfo=UTC)          # 10:00 in Moscow
        local = now_utc.astimezone(MSK)
        self.assertEqual(self.prev("0 9 * * *", local), "2026-10-02 09:00")
        self.assertEqual(sch.Cron.parse("0 9 * * *").previous(local).timestamp(), ts(2026, 10, 2, 6, 0))

    def test_across_a_daylight_saving_change_the_wall_clock_time_holds(self):
        # US clocks go back on 2026-11-01; 09:00 New York is 14:00 UTC afterwards.
        before = datetime(2026, 10, 31, 12, 0, tzinfo=NY)
        after = datetime(2026, 11, 1, 12, 0, tzinfo=NY)
        c = sch.Cron.parse("0 9 * * *")
        self.assertEqual(c.previous(before).timestamp(), ts(2026, 10, 31, 13, 0))
        self.assertEqual(c.previous(after).timestamp(), ts(2026, 11, 1, 14, 0))


class TaskParsingTest(unittest.TestCase):
    def raw(self, **over):
        base = {"id": "t1", "name": "Сводка", "prompt": "Собери сводку", "schedule": {"kind": "cron", "expr": "0 9 * * *"}}
        base.update(over)
        return base

    def test_a_minimal_task_gets_defaults(self):
        t = sch.parse_task(self.raw(), "Europe/Moscow")
        self.assertEqual((t.kind, t.expr, t.tz, t.enabled), ("cron", "0 9 * * *", "Europe/Moscow", True))
        self.assertEqual((t.max_attempts, t.retry_interval, t.retry_on_failure), (3, 5, True))
        self.assertEqual((t.window_minutes, t.catchup_minutes, t.report_timeout), (60, 60, 0))
        self.assertEqual(t.executor, "auto")

    def test_explicit_values_and_clamping(self):
        t = sch.parse_task(self.raw(timezone="UTC", enabled=False, window_minutes=10, catchup_minutes=0,
                                    report_timeout_minutes=30, retry={"max_attempts": 99, "interval_minutes": 0, "on_failure": False}), "X")
        self.assertEqual((t.tz, t.enabled, t.window_minutes, t.catchup_minutes, t.report_timeout), ("UTC", False, 10, 0, 30))
        self.assertEqual((t.max_attempts, t.retry_interval, t.retry_on_failure), (20, 1, False))

    def test_interval_and_once(self):
        t = sch.parse_task(self.raw(schedule={"kind": "interval", "every_minutes": "30"}), "UTC")
        self.assertEqual(t.every_minutes, 30)
        t = sch.parse_task(self.raw(schedule={"kind": "once", "at": "2026-10-03T09:00"}), "UTC")
        self.assertEqual(t.at, "2026-10-03T09:00")

    def test_interval_cannot_exceed_the_control_plane_limit(self):
        with self.assertRaises(sch.ScheduleError):
            sch.parse_task(self.raw(schedule={"kind": "interval", "every_minutes": 366 * 24 * 60 + 1}), "UTC")

    def test_every_kind_of_bad_task_is_refused_with_a_reason(self):
        bad = [
            "not a dict", self.raw(id="bad id!"), self.raw(id=""), self.raw(prompt="  "),
            self.raw(schedule={"kind": "weekly"}), self.raw(schedule={"kind": "cron", "expr": "nope"}),
            self.raw(schedule={"kind": "cron", "expr": "0 0 31 2 *"}),
            self.raw(schedule={"kind": "interval"}), self.raw(schedule={"kind": "interval", "every_minutes": 0}),
            self.raw(schedule={"kind": "once", "at": "tomorrow"}), self.raw(timezone="Mars/Base"),
            self.raw(window_minutes="soon"),
            self.raw(executor="both"),
        ]
        for raw in bad:
            with self.assertRaises(sch.ScheduleError, msg=raw):
                sch.parse_task(raw, "UTC")

    def test_signature_changes_with_the_schedule_only(self):
        a = sch.parse_task(self.raw(), "UTC")
        b = sch.parse_task(self.raw(prompt="другой текст", name="Другое"), "UTC")
        c = sch.parse_task(self.raw(schedule={"kind": "cron", "expr": "5 9 * * *"}), "UTC")
        self.assertEqual(a.signature(), b.signature())
        self.assertNotEqual(a.signature(), c.signature())


class SlotsTest(unittest.TestCase):
    def task(self, schedule, created=0.0, tz="UTC"):
        return sch.parse_task({"id": "t", "prompt": "p", "schedule": schedule, "timezone": tz, "created_at": created}, "UTC")

    def test_interval_slots_are_anchored_at_creation(self):
        t = self.task({"kind": "interval", "every_minutes": 60}, created=ts(2026, 10, 2, 8, 0))
        self.assertIsNone(sch.previous_slot(t, ts(2026, 10, 2, 8, 30)))
        self.assertEqual(sch.next_slot(t, ts(2026, 10, 2, 8, 30)), ts(2026, 10, 2, 9, 0))
        self.assertEqual(sch.previous_slot(t, ts(2026, 10, 2, 11, 59)), ts(2026, 10, 2, 11, 0))
        self.assertEqual(sch.next_slot(t, ts(2026, 10, 2, 11, 59)), ts(2026, 10, 2, 12, 0))

    def test_once_has_one_slot_in_its_own_time_zone(self):
        t = self.task({"kind": "once", "at": "2026-10-03T09:00"}, tz="Europe/Moscow")
        at = ts(2026, 10, 3, 6, 0)
        self.assertIsNone(sch.previous_slot(t, at - 1))
        self.assertEqual(sch.next_slot(t, at - 1), at)
        self.assertEqual(sch.previous_slot(t, at + 5), at)
        self.assertIsNone(sch.next_slot(t, at + 5))


class DeliverTest(unittest.TestCase):
    def runner(self, pane="x bypass permissions on y", fail_at=None):
        calls = []

        def run(args, timeout=15):
            calls.append(args)
            kind = args[2]
            if fail_at == kind:
                return 1, "boom"
            return 0, pane if kind == "capture-pane" else ""
        return run, calls

    def test_types_the_flattened_text_and_presses_enter(self):
        run, calls = self.runner()
        ok, why = sch.deliver("exampleuser", "example-dev", "строка 1\nстрока 2\t\x1b[31mred", run)
        self.assertEqual((ok, why), (True, ""))
        self.assertEqual(calls[0][:3], ["-S", "/home/exampleuser/.claude/example-dev.tmux.sock", "capture-pane"])
        typed = calls[1]
        self.assertEqual(typed[-1], "строка 1 строка 2 [31mred".replace("[31mred", "[31mred"))
        self.assertEqual(calls[2][-1], "Enter")

    def test_an_agent_that_is_not_at_its_prompt_gets_nothing_typed(self):
        run, calls = self.runner(pane="Do you want to proceed? 1. Yes 2. No")
        ok, why = sch.deliver("u", "u", "text", run)
        self.assertFalse(ok)
        self.assertIn("занят", why)
        self.assertEqual(len(calls), 1)

    def test_a_missing_session_and_tmux_failures_are_explained(self):
        run, _ = self.runner(fail_at="capture-pane")
        self.assertIn("не найдена", sch.deliver("u", "u", "t", run)[1])
        run, _ = self.runner(fail_at="send-keys")
        self.assertIn("не удалось ввести", sch.deliver("u", "u", "t", run)[1])
        calls = []

        def run2(args, timeout=15):
            calls.append(args)
            if args[2] == "send-keys" and args[-1] == "Enter":
                return 1, "no enter"
            return 0, "bypass permissions on"
        self.assertIn("Enter", sch.deliver("u", "u", "t", run2)[1])

    def test_the_text_is_capped_and_the_prompt_names_the_report_command(self):
        self.assertEqual(len(sch.flatten("a" * 10000)), sch.MAX_PROMPT_CHARS)
        self.assertEqual(sch.flatten(" a \n\n b\x00c "), "a b c")
        t = sch.parse_task({"id": "t", "name": "Сводка", "prompt": "Сделай", "schedule": {"kind": "interval", "every_minutes": 5}}, "UTC")
        text = sch.compose_prompt(t, "t-100")
        for part in ("Сводка", "Сделай", "agent-task done t-100", "agent-task fail t-100"):
            self.assertIn(part, text)

    def test_tmux_run_wraps_missing_programs(self):
        with mock.patch.object(sch.subprocess, "run", side_effect=OSError("no tmux")):
            self.assertEqual(sch.tmux_run(["ls"]), (1, "no tmux"))
        ok = mock.Mock(returncode=0, stdout="out", stderr="")
        with mock.patch.object(sch.subprocess, "run", return_value=ok):
            self.assertEqual(sch.tmux_run(["ls"]), (0, "out"))
        bad = mock.Mock(returncode=1, stdout="", stderr="err")
        with mock.patch.object(sch.subprocess, "run", return_value=bad):
            self.assertEqual(sch.tmux_run(["ls"]), (1, "err"))


class ExecutorRoutingTest(unittest.TestCase):
    def task(self, executor="auto"):
        return sch.parse_task({"id": "t", "prompt": "p", "executor": executor,
                               "schedule": {"kind": "interval", "every_minutes": 5}}, "UTC")

    def test_inventory_splits_one_logical_agent_into_executors(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "fleet.json"
            p.write_text(json.dumps({"fleet": [
                {"tenant": "main", "backend": "claude", "tmux_socket": "/c.sock", "tmux_session": "main"},
                {"tenant": "main", "backend": "codex", "codex_socket": "/x.sock", "workspace": "/root"},
                {"tenant": "other", "backend": "claude", "tmux_socket": "/o.sock", "tmux_session": "other"},
            ]}))
            routes = sch.load_executor_routes("main", str(p))
        self.assertEqual([r.backend for r in routes], ["claude", "codex"])

    def test_auto_uses_exactly_one_executor_and_falls_back(self):
        sched = sch.Scheduler(Path("/unused"))
        agent = sch.AgentState("root")
        agent.tenant = "main"
        routes = [
            sch.ExecutorRoute("claude", tmux_socket="/c.sock", tmux_session="main"),
            sch.ExecutorRoute("codex", codex_socket="/x.sock", workspace="/root"),
        ]
        with mock.patch.object(sch, "load_executor_routes", return_value=routes), \
             mock.patch.object(sch, "deliver_tmux", return_value=(False, "занят")) as claude, \
             mock.patch.object(sched, "_codex", return_value=(True, "")) as codex:
            got = sched._dispatch(agent, self.task("auto"), "do it")
        self.assertEqual(got, (True, "", "codex"))
        claude.assert_called_once()
        codex.assert_called_once()

    def test_a_concrete_choice_never_fans_out(self):
        sched = sch.Scheduler(Path("/unused"))
        agent = sch.AgentState("root")
        agent.tenant = "main"
        routes = [
            sch.ExecutorRoute("claude", tmux_socket="/c.sock", tmux_session="main"),
            sch.ExecutorRoute("codex", codex_socket="/x.sock", workspace="/root"),
        ]
        with mock.patch.object(sch, "load_executor_routes", return_value=routes), \
             mock.patch.object(sch, "deliver_tmux", return_value=(False, "занят")), \
             mock.patch.object(sched, "_codex", return_value=(True, "")) as codex:
            got = sched._dispatch(agent, self.task("claude"), "do it")
        self.assertFalse(got[0])
        self.assertEqual(got[2], "claude")
        codex.assert_not_called()

    def test_codex_limit_threshold_is_ten_percent(self):
        snap = {"rateLimits": {"primary": {"usedPercent": 90}}}
        self.assertTrue(sch.codex_limit_allowed(snap)[0])
        snap["rateLimits"]["primary"]["usedPercent"] = 91
        self.assertFalse(sch.codex_limit_allowed(snap)[0])


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds=0, minutes=0):
        self.t += seconds + minutes * 60


class EngineBase(unittest.TestCase):
    start = ts(2026, 10, 2, 8, 0)

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sched-"))
        self.clock = Clock(self.start)
        self.sent = []
        self.busy = False
        self.reason = "агент занят: сессия не на обычной строке ввода"
        self.sched = self.make()

    def make(self):
        def send(user, unit, text):
            if self.busy:
                return False, self.reason
            self.sent.append((user, unit, text))
            return True, ""
        return sch.Scheduler(self.root, now=self.clock, send=send, uid_of=lambda u: os.getuid())

    def write_tasks(self, tasks, user="exampleuser", **extra):
        d = self.root / user
        d.mkdir(parents=True, exist_ok=True)
        data = {"version": "v1", "unit": "example-dev", "timezone": "UTC", "tasks": tasks}
        data.update(extra)
        (d / "tasks.json").write_text(json.dumps(data), encoding="utf-8")

    def task(self, **over):
        t = {"id": "daily", "name": "Сводка", "prompt": "Собери сводку", "schedule": {"kind": "cron", "expr": "0 9 * * *"},
             "created_at": self.start - 3600}
        t.update(over)
        return t

    def tick(self):
        return self.sched.tick()[0]

    def runs(self, agent=None):
        return (agent or self.sched.load("exampleuser")).state["runs"]

    def statuses(self):
        return [r["status"] for r in self.runs()]


class SchedulingTest(EngineBase):
    def test_a_due_task_is_delivered_once_with_its_prompt(self):
        self.write_tasks([self.task()])
        self.tick()
        self.assertEqual(self.sent, [])                       # 08:00, not yet
        self.clock.advance(minutes=61)                        # 09:01
        self.tick()
        self.assertEqual(len(self.sent), 1)
        user, unit, text = self.sent[0]
        self.assertEqual((user, unit), ("exampleuser", "example-dev"))
        self.assertIn("Собери сводку", text)
        self.assertEqual(self.statuses(), ["delivered_final"])
        self.clock.advance(minutes=1)
        self.tick()
        self.tick()
        self.assertEqual(len(self.sent), 1, "the same slot must never be run twice")

    def test_the_next_day_runs_again(self):
        self.write_tasks([self.task()])
        self.clock.advance(minutes=61)
        self.tick()
        self.clock.advance(minutes=24 * 60)
        self.tick()
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(len({r["id"] for r in self.runs()}), 2)

    def test_a_new_task_does_not_replay_the_past(self):
        # created long ago in time, but first seen now: yesterday's slot is not owed
        self.write_tasks([self.task(created_at=0)])
        self.tick()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.runs(), [])

    def test_a_slot_between_creation_and_discovery_is_owed(self):
        created = ts(2026, 10, 2, 8, 59)
        self.write_tasks([self.task(created_at=created)])
        self.clock.t = ts(2026, 10, 2, 9, 0) + 20
        self.tick()
        self.assertEqual(len(self.sent), 1)

    def test_a_slot_missed_for_too_long_is_recorded_not_run(self):
        self.write_tasks([self.task(catchup_minutes=30)])
        self.tick()
        self.clock.t = ts(2026, 10, 2, 9, 0)                  # scheduler was "down" ...
        self.sched = self.make()
        self.clock.advance(minutes=45)                        # ... until 09:45
        self.tick()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.statuses(), ["missed"])
        self.assertIn("недоступны", self.runs()[0]["reason"])

    def test_a_recent_missed_slot_is_run_once(self):
        self.write_tasks([self.task(catchup_minutes=30)])
        self.tick()
        self.clock.t = ts(2026, 10, 2, 9, 20)
        self.tick()
        self.assertEqual(len(self.sent), 1)

    def test_a_task_is_not_started_while_its_previous_run_is_unfinished(self):
        self.write_tasks([self.task(schedule={"kind": "interval", "every_minutes": 10}, report_timeout_minutes=120)])
        self.clock.advance(minutes=11)
        self.tick()                                            # delivered, waiting for a report
        self.clock.advance(minutes=10)
        self.tick()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.statuses(), ["delivered", "skipped"])
        self.assertIn("ещё не завершён", self.runs()[1]["reason"])

    def test_disabled_tasks_do_not_run_and_owe_nothing_when_enabled_again(self):
        self.write_tasks([self.task(enabled=False)])
        self.clock.advance(minutes=61)
        self.tick()
        self.assertEqual(self.sent, [])
        self.write_tasks([self.task(enabled=True)])
        self.clock.advance(minutes=5)
        self.tick()
        self.assertEqual(self.sent, [], "the 09:00 slot passed while it was disabled")
        self.clock.advance(minutes=24 * 60)
        self.tick()
        self.assertEqual(len(self.sent), 1)

    def test_a_once_task_runs_once_and_is_finished(self):
        self.write_tasks([self.task(id="once", schedule={"kind": "once", "at": "2026-10-02T08:30"}, created_at=self.start)])
        self.tick()
        self.clock.advance(minutes=31)
        self.tick()
        self.clock.advance(minutes=120)
        self.tick()
        self.assertEqual(len(self.sent), 1)
        report = self.sched.report(self.sched.tick())["agents"][0]["tasks"][0]
        self.assertIsNone(report["next_run"])

    def test_a_once_task_set_in_the_past_never_runs(self):
        self.write_tasks([self.task(id="old", schedule={"kind": "once", "at": "2026-10-01T08:30"}, created_at=self.start)])
        self.tick()
        self.assertEqual(self.sent, [])

    def test_changing_the_schedule_resets_what_is_owed(self):
        self.write_tasks([self.task()])
        self.tick()
        self.write_tasks([self.task(schedule={"kind": "cron", "expr": "30 8 * * *"})])   # 08:30 is "now-ish"
        self.clock.advance(minutes=45)
        self.tick()
        self.assertEqual(self.sent, [], "a changed schedule does not run slots from before the change")
        self.clock.advance(minutes=24 * 60)
        self.tick()
        self.assertEqual(len(self.sent), 1)

    def test_removed_tasks_cancel_their_open_runs_and_forget_their_state(self):
        self.write_tasks([self.task(schedule={"kind": "interval", "every_minutes": 10}, report_timeout_minutes=60)])
        self.clock.advance(minutes=11)
        self.tick()
        self.write_tasks([])
        self.tick()
        agent = self.sched.load("exampleuser")
        self.assertEqual([r["status"] for r in agent.state["runs"]], ["cancelled"])
        self.assertEqual(agent.state["tasks"], {})

    def test_two_agents_are_independent(self):
        self.write_tasks([self.task()], user="exampleuser")
        self.write_tasks([self.task()], user="seconduser", unit="second-dev")
        self.clock.advance(minutes=61)
        self.sched.tick()
        self.assertEqual(sorted((u, unit) for u, unit, _ in self.sent), [("exampleuser", "example-dev"), ("seconduser", "second-dev")])

    def test_state_survives_a_restart_without_repeating_anything(self):
        self.write_tasks([self.task()])
        self.clock.advance(minutes=61)
        self.tick()
        self.sched = self.make()                              # the daemon restarts
        self.clock.advance(minutes=1)
        self.tick()
        self.assertEqual(len(self.sent), 1)


class RetryTest(EngineBase):
    def test_a_busy_agent_is_retried_until_it_is_free(self):
        self.write_tasks([self.task(retry={"max_attempts": 5, "interval_minutes": 5})])
        self.clock.t = ts(2026, 10, 2, 9, 0) + 10
        self.busy = True
        self.tick()
        self.assertEqual((self.statuses(), self.runs()[0]["attempts"]), (["pending"], 1))
        self.clock.advance(minutes=2)
        self.tick()
        self.assertEqual(self.runs()[0]["attempts"], 1, "not yet time for the next attempt")
        self.clock.advance(minutes=4)
        self.busy = False
        self.tick()
        self.assertEqual((self.statuses(), self.runs()[0]["attempts"]), (["delivered_final"], 2))
        self.assertEqual(len(self.sent), 1)

    def test_attempts_run_out(self):
        self.write_tasks([self.task(retry={"max_attempts": 3, "interval_minutes": 1})])
        self.clock.t = ts(2026, 10, 2, 9, 0)
        self.busy = True
        for _ in range(6):
            self.tick()
            self.clock.advance(minutes=1)
        run = self.runs()[0]
        self.assertEqual((run["status"], run["attempts"]), ("failed", 3))
        self.assertIn("занят", run["reason"])

    def test_the_window_closes_the_series_early(self):
        self.write_tasks([self.task(retry={"max_attempts": 10, "interval_minutes": 20}, window_minutes=30)])
        self.clock.t = ts(2026, 10, 2, 9, 0)
        self.busy = True
        self.tick()                                            # 09:00, next would be 09:20
        self.clock.advance(minutes=20)
        self.tick()                                            # 09:20, next would be 09:40 > window
        self.assertEqual(self.statuses(), ["failed"])
        self.assertEqual(self.runs()[0]["attempts"], 2)

    def test_one_attempt_means_no_retry(self):
        self.write_tasks([self.task(retry={"max_attempts": 1, "interval_minutes": 5})])
        self.clock.t = ts(2026, 10, 2, 9, 0)
        self.busy = True
        self.tick()
        self.assertEqual(self.statuses(), ["failed"])


class ReportTest(EngineBase):
    def setUp(self):
        super().setUp()
        self.write_tasks([self.task(schedule={"kind": "interval", "every_minutes": 60}, report_timeout_minutes=15,
                                    retry={"max_attempts": 3, "interval_minutes": 5})])
        self.clock.advance(minutes=61)
        self.tick()
        self.run_id = self.runs()[0]["id"]

    def report(self, status, note="итог", run_id=None, **over):
        d = self.root / "exampleuser" / "reports"
        d.mkdir(parents=True, exist_ok=True)
        data = {"run": run_id or self.run_id, "status": status, "note": note}
        data.update(over)
        (d / ((run_id or self.run_id) + ".json")).write_text(json.dumps(data))

    def test_a_task_that_waits_for_a_report_stays_delivered_until_it_comes(self):
        self.assertEqual(self.statuses(), ["delivered"])
        self.clock.advance(minutes=5)
        self.tick()
        self.assertEqual(self.statuses(), ["delivered"])

    def test_done_closes_the_run_with_the_agents_note(self):
        self.report("done", "создано 3 задачи")
        self.tick()
        run = self.runs()[0]
        self.assertEqual((run["status"], run["note"]), ("done", "создано 3 задачи"))
        self.assertFalse((self.root / "exampleuser" / "reports" / (self.run_id + ".json")).exists())

    def test_a_failure_report_is_retried_then_final(self):
        self.report("failed", "нет доступа")
        self.tick()
        run = self.runs()[0]
        self.assertEqual(run["status"], "pending")
        self.assertIn("нет доступа", run["reason"])
        self.assertFalse((self.root / "exampleuser" / "reports" / (self.run_id + ".json")).exists())
        self.clock.advance(minutes=6)
        self.tick()
        self.assertEqual((self.runs()[0]["status"], len(self.sent)), ("delivered", 2))
        self.report("failed", "снова нет")
        self.tick()
        self.clock.advance(minutes=6)
        self.tick()
        self.report("failed", "и снова")
        self.tick()
        run = self.runs()[0]
        self.assertEqual((run["status"], run["attempts"], run["note"]), ("failed", 3, "и снова"))
        self.assertFalse((self.root / "exampleuser" / "reports" / (self.run_id + ".json")).exists())

    def test_a_failure_is_final_when_retrying_failures_is_switched_off(self):
        self.write_tasks([self.task(schedule={"kind": "interval", "every_minutes": 60}, report_timeout_minutes=15,
                                    retry={"max_attempts": 3, "interval_minutes": 5, "on_failure": False})])
        self.sched = self.make()
        self.report("failed", "нет", run_id=self.run_id)
        self.tick()
        self.assertEqual(self.statuses()[0], "failed")
        self.assertFalse((self.root / "exampleuser" / "reports" / (self.run_id + ".json")).exists())

    def test_silence_is_retried_and_finally_a_timeout(self):
        self.clock.advance(minutes=16)
        self.tick()
        self.assertEqual(self.runs()[0]["status"], "pending")
        self.assertIn("не ответил", self.runs()[0]["reason"])
        for _ in range(2):
            self.tick()                                        # re-delivered at once
            self.clock.advance(minutes=16)
            self.tick()
        run = self.runs()[0]
        self.assertEqual((run["status"], run["attempts"]), ("timeout", 3))
        self.assertEqual(len(self.sent), 3)

    def test_reports_that_cannot_be_trusted_are_ignored(self):
        d = self.root / "exampleuser" / "reports"
        d.mkdir(parents=True, exist_ok=True)
        secret = self.root / "secret.json"
        secret.write_text(json.dumps({"run": self.run_id, "status": "done", "note": "ROOT-ONLY"}))
        os.symlink(secret, d / (self.run_id + ".json"))
        self.tick()
        self.assertEqual(self.statuses()[0], "delivered", "a symlink must never be followed")
        os.unlink(d / (self.run_id + ".json"))
        self.report("done", run_id=self.run_id, run="some-other-run")
        self.tick()
        self.assertEqual(self.statuses()[0], "delivered", "the report must name this run")
        self.report("maybe")
        self.tick()
        self.assertEqual(self.statuses()[0], "delivered", "only done/failed count")
        (d / (self.run_id + ".json")).write_text("{" + "x" * 20000)
        self.tick()
        self.assertEqual(self.statuses()[0], "delivered", "an oversized file is ignored")
        (d / (self.run_id + ".json")).unlink()
        os.mkfifo(d / (self.run_id + ".json"))
        self.tick()
        self.assertEqual(self.statuses()[0], "delivered", "a pipe is not a report")

    def test_a_report_owned_by_someone_else_is_ignored(self):
        self.sched = sch.Scheduler(self.root, now=self.clock, send=lambda *a: (True, ""), uid_of=lambda u: os.getuid() + 1)
        self.report("done")
        self.tick()
        self.assertEqual(self.statuses()[0], "delivered")

    def test_the_note_is_cut(self):
        self.report("done", "a" * 3000)
        self.tick()
        self.assertEqual(len(self.runs()[0]["note"]), 500)

    def test_read_report_rejects_unsafe_run_ids(self):
        self.assertIsNone(sch.read_report(self.root, "../etc/passwd", None))
        self.assertIsNone(sch.read_report(self.root, "", None))


class TriggerTest(EngineBase):
    def test_run_now_creates_a_manual_run_and_consumes_the_request(self):
        self.write_tasks([self.task()])
        d = self.root / "exampleuser" / "trigger"
        d.mkdir()
        (d / "daily.abc123").write_text("")
        (d / "ghost.abc123").write_text("")                    # an unknown task is dropped silently
        self.tick()
        self.assertEqual(len(self.sent), 1)
        run = self.runs()[0]
        self.assertEqual(run["kind"], "manual")
        self.assertIn("-m", run["id"])
        self.assertEqual(list(d.iterdir()), [])

    def test_two_requests_in_one_second_get_distinct_ids_and_the_second_is_skipped(self):
        self.write_tasks([self.task(report_timeout_minutes=30)])
        d = self.root / "exampleuser" / "trigger"
        d.mkdir()
        (d / "daily.1").write_text("")
        (d / "daily.2").write_text("")
        self.tick()
        runs = self.runs()
        self.assertEqual(len({r["id"] for r in runs}), 2)
        self.assertEqual(sorted(r["status"] for r in runs), ["delivered", "skipped"])


class ReportingTest(EngineBase):
    def test_what_agentdesk_is_told(self):
        self.write_tasks([self.task(), self.task(id="broken", schedule={"kind": "cron", "expr": "bad"}),
                          self.task(id="off", enabled=False)], version="abc")
        self.clock.advance(minutes=61)
        agents = self.sched.tick()
        payload = self.sched.report(agents)
        a = payload["agents"][0]
        self.assertEqual((a["user"], a["version"], a["error"]), ("exampleuser", "abc", ""))
        self.assertEqual(len(a["invalid"]), 1)
        by_id = {t["id"]: t for t in a["tasks"]}
        self.assertEqual(by_id["daily"]["last_status"], "delivered_final")
        self.assertEqual(by_id["daily"]["next_run"], ts(2026, 10, 3, 9, 0))
        self.assertIsNone(by_id["off"]["next_run"])
        self.assertEqual(len(a["runs"]), 1)
        self.assertNotIn("pushed", a["runs"][0])

    def test_only_unpushed_runs_are_sent_and_a_changed_run_is_sent_again(self):
        self.write_tasks([self.task(schedule={"kind": "interval", "every_minutes": 60}, report_timeout_minutes=30)])
        self.clock.advance(minutes=61)
        agents = self.sched.tick()
        payload = self.sched.report(agents)
        self.sched.mark_pushed(agents, payload)
        self.assertEqual(self.sched.report(self.sched.tick())["agents"][0]["runs"], [])
        run_id = self.runs()[0]["id"]
        d = self.root / "exampleuser" / "reports"
        d.mkdir()
        (d / (run_id + ".json")).write_text(json.dumps({"run": run_id, "status": "done", "note": "ok"}))
        agents = self.sched.tick()
        again = self.sched.report(agents)["agents"][0]["runs"]
        self.assertEqual([(r["id"], r["status"]) for r in again], [(run_id, "done")])

    def test_a_run_that_changed_while_being_pushed_stays_unpushed(self):
        self.write_tasks([self.task()])
        self.clock.advance(minutes=61)
        agents = self.sched.tick()
        payload = self.sched.report(agents)
        agents[0].state["runs"][0]["seq"] += 1               # changed after the payload was built
        self.sched.mark_pushed(agents, payload)
        self.assertFalse(agents[0].state["runs"][0]["pushed"])

    def test_an_unreadable_tasks_file_is_reported_not_fatal(self):
        d = self.root / "exampleuser"
        d.mkdir()
        (d / "tasks.json").write_text("{broken")
        agents = self.sched.tick()
        self.assertEqual(self.sched.report(agents)["agents"][0]["error"], "tasks.json is unreadable")
        self.write_tasks([self.task()], user="seconduser")
        self.assertEqual(len(self.sched.tick()), 2)

    def test_a_crash_in_one_agent_does_not_stop_the_others(self):
        self.write_tasks([self.task()], user="exampleuser")
        self.write_tasks([self.task()], user="seconduser", unit="b")
        real = self.sched.tick_agent

        def flaky(agent):
            if agent.user == "seconduser":
                raise RuntimeError("boom")
            return real(agent)
        self.sched.tick_agent = flaky
        with mock.patch.object(sys, "stderr", io.StringIO()):
            agents = self.sched.tick()
        self.assertEqual({a.user: a.error for a in agents}, {"seconduser": "boom", "exampleuser": ""})

    def test_directories_that_are_not_agents_are_ignored(self):
        (self.root / ".state").mkdir()
        (self.root / "Bad Name").mkdir()
        (self.root / "Bad Name" / "tasks.json").write_text("{}")
        (self.root / "nodir").mkdir()
        self.assertEqual(self.sched.users(), [])
        self.assertEqual(sch.Scheduler(self.root / "missing").users(), [])

    def test_finished_runs_are_trimmed_but_unpushed_ones_are_kept_for_a_while(self):
        self.write_tasks([self.task(schedule={"kind": "interval", "every_minutes": 1})])
        for _ in range(sch.KEEP_RUNS + 20):
            self.clock.advance(minutes=1)
            agents = self.sched.tick()
            self.sched.mark_pushed(agents, self.sched.report(agents))
        self.assertEqual(len(self.runs()), sch.KEEP_RUNS)
        # agentdesk away: nothing is marked pushed; the bound is looser but still a bound
        for _ in range(sch.KEEP_RUNS * 10 + 20):
            self.clock.advance(minutes=1)
            self.sched.tick()
        self.assertLessEqual(len(self.runs()), sch.KEEP_RUNS * 10)


class ExampleTest(unittest.TestCase):
    def test_the_documented_example_is_valid(self):
        raw = json.loads((Path(__file__).resolve().parent / "example" / "tasks.json").read_text(encoding="utf-8"))
        tasks = [sch.parse_task(t, raw["timezone"]) for t in raw["tasks"]]
        self.assertEqual([t.kind for t in tasks], ["cron", "interval", "once"])
        self.assertEqual(tasks[0].report_timeout, 30)
        self.assertEqual(tasks[2].tz, "UTC")


class PushTest(unittest.TestCase):
    def test_push_sends_a_bearer_token_and_reports_success(self):
        seen = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b""

        def opener(req, timeout=None):
            seen["url"], seen["auth"], seen["body"] = req.full_url, req.get_header("Authorization"), json.loads(req.data)
            return Resp()

        self.assertTrue(sch.push("http://ad/api/fleet/tasks", "tok", {"agents": []}, opener))
        self.assertEqual((seen["url"], seen["auth"], seen["body"]), ("http://ad/api/fleet/tasks", "Bearer tok", {"agents": []}))

    def test_a_failed_push_is_survivable(self):
        def opener(req, timeout=None):
            raise urllib.error.URLError("down")

        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertFalse(sch.push("http://ad/x", "tok", {}, opener))


class RunOnceTest(EngineBase):
    def test_nothing_is_pushed_without_agentdesk_settings_or_agents(self):
        self.assertFalse(sch.run_once(self.sched, "http://ad", "tok"))          # no agents yet
        self.write_tasks([self.task()])
        self.assertFalse(sch.run_once(self.sched, None, None))

    def test_a_successful_push_marks_runs_and_a_failed_one_keeps_them(self):
        self.write_tasks([self.task()])
        self.clock.advance(minutes=61)

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b""

        def bad(req, timeout=None):
            raise OSError("down")

        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertFalse(sch.run_once(self.sched, "http://ad", "tok", opener=bad))
        self.assertFalse(self.runs()[0]["pushed"])
        self.assertTrue(sch.run_once(self.sched, "http://ad", "tok", opener=lambda r, timeout=None: Resp()))
        self.assertTrue(self.runs()[0]["pushed"])


class MainTest(EngineBase):
    def test_once_prints_the_report(self):
        self.write_tasks([self.task()])
        buf = io.StringIO()
        with mock.patch.object(sch, "ROOT", self.root), mock.patch.object(sys, "argv", ["x", "--once"]), \
                mock.patch.object(sys, "stdout", buf):
            sch.main()
        out = json.loads(buf.getvalue())
        self.assertEqual(out["agents"][0]["user"], "exampleuser")

    def test_the_loop_ticks_pushes_and_survives_errors(self):
        env = {"AGENTDESK_URL": "http://ad/", "AGENT_EXPORTER_TOKEN": "tok"}
        pushes = []
        calls = {"n": 0}

        def fake_tick(self_):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("bad tick")
            return []

        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", ["x"]), \
                mock.patch.object(sch.Scheduler, "tick", fake_tick), \
                mock.patch.object(sch, "push", lambda url, tok, payload, *a: pushes.append(url) or True), \
                mock.patch.object(sch.time, "sleep", side_effect=[None, None, KeyboardInterrupt]), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                sch.main()
        self.assertEqual(pushes, ["http://ad/api/fleet/tasks"])


if __name__ == "__main__":
    unittest.main()
