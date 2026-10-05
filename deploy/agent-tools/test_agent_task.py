import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

spec = importlib.util.spec_from_file_location("agent_task", os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-task.py"))
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)


class Tool(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.mkdir(os.path.join(self.d, "reports"))
        self.env = mock.patch.dict(os.environ, {"AGENT_TASKS_DIR": self.d}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def tasks(self, *ids):
        with open(os.path.join(self.d, "tasks.json"), "w") as f:
            json.dump({"tasks": [{"id": i, "name": "N " + i, "schedule": {"kind": "cron", "expr": "0 9 * * *"}} for i in ids]}, f)

    def run_cmd(self, *argv):
        buf = io.StringIO()
        code = 0
        with redirect_stdout(buf):
            try:
                at.main(list(argv))
            except SystemExit as e:
                code = e.code or 0
        out = buf.getvalue()
        try:
            return code, json.loads(out)
        except ValueError:
            return code, out

    def report(self, run):
        with open(os.path.join(self.d, "reports", run + ".json"), encoding="utf-8") as f:
            return json.load(f)

    def test_done_and_fail_write_a_private_report_the_scheduler_can_read(self):
        self.tasks("daily")
        code, out = self.run_cmd("done", "daily-1759395600", "создано", "3", "задачи")
        self.assertEqual((code, out["status"]), (0, "done"))
        r = self.report("daily-1759395600")
        self.assertEqual((r["run"], r["status"], r["note"]), ("daily-1759395600", "done", "создано 3 задачи"))
        mode = stat.S_IMODE(os.stat(os.path.join(self.d, "reports", "daily-1759395600.json")).st_mode)
        self.assertEqual(mode, 0o600)
        self.run_cmd("fail", "daily-1759395601", "нет доступа")
        self.assertEqual(self.report("daily-1759395601")["status"], "failed")

    def test_the_scheduler_accepts_what_the_tool_writes(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent-scheduler"))
        import agent_scheduler as sch
        self.tasks("daily")
        self.run_cmd("done", "daily-100", "ok")
        got = sch.read_report(__import__("pathlib").Path(self.d) / "reports", "daily-100", os.getuid())
        self.assertEqual(got, {"status": "done", "note": "ok"})

    def test_a_long_note_is_cut_and_non_ascii_is_kept_readable(self):
        self.tasks("daily")
        self.run_cmd("done", "daily-1", "я" * 900)
        self.assertEqual(len(self.report("daily-1")["note"]), 500)
        with open(os.path.join(self.d, "reports", "daily-1.json"), encoding="utf-8") as f:
            self.assertIn("я", f.read())

    def test_a_run_of_someone_elses_task_or_a_malformed_id_is_refused(self):
        self.tasks("daily")
        self.assertEqual(self.run_cmd("done", "other-1")[0], 1)
        self.assertEqual(self.run_cmd("done", "../../etc/passwd")[0], 1)
        self.assertEqual(self.run_cmd("done", "")[0], 1)
        self.assertEqual(self.run_cmd("done")[0], 1)
        self.assertEqual(os.listdir(os.path.join(self.d, "reports")), [])

    def test_a_symlink_in_the_way_is_not_followed(self):
        self.tasks("daily")
        victim = os.path.join(self.d, "victim")
        with open(victim, "w") as f:
            f.write("keep")
        os.symlink(victim, os.path.join(self.d, "reports", "daily-1.json.tmp"))
        self.assertEqual(self.run_cmd("done", "daily-1")[0], 1)
        with open(victim) as f:
            self.assertEqual(f.read(), "keep")

    def test_no_reports_directory_means_tasks_are_not_set_up(self):
        os.rmdir(os.path.join(self.d, "reports"))
        code, out = self.run_cmd("done", "daily-1")
        self.assertEqual(code, 1)
        self.assertIn("не найден", out["error"])

    def test_list_describes_each_schedule(self):
        with open(os.path.join(self.d, "tasks.json"), "w") as f:
            json.dump({"tasks": [
                {"id": "a", "name": "А", "schedule": {"kind": "cron", "expr": "0 9 * * *"}},
                {"id": "b", "name": "Б", "schedule": {"kind": "interval", "every_minutes": 30}, "enabled": False},
                {"id": "c", "name": "В", "schedule": {"kind": "once", "at": "2026-10-03T09:00"}},
                "junk",
            ]}, f)
        code, out = self.run_cmd("list")
        self.assertEqual([(t["id"], t["when"], t["enabled"]) for t in out["tasks"]],
                         [("a", "0 9 * * *", True), ("b", "каждые 30 мин", False), ("c", "2026-10-03T09:00", True)])

    def test_list_without_tasks_and_unknown_commands(self):
        self.assertEqual(self.run_cmd("list")[0], 1)
        self.assertEqual(self.run_cmd("nope")[0], 1)
        self.assertIn("agent-task done", self.run_cmd()[1])

    def test_the_default_directory_is_the_users_own(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(at.getpass, "getuser", return_value="exampleuser"):
            self.assertEqual(at.base_dir(), "/var/lib/agentdesk-tasks/exampleuser")


if __name__ == "__main__":
    unittest.main()
