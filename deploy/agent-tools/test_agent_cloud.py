import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

spec = importlib.util.spec_from_file_location("agent_cloud", os.path.join(os.path.dirname(__file__), "agent-cloud.py"))
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)

ROOT = """<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">
<d:response><d:href>/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
<d:response><d:href>/%D0%94%D0%BE%D0%BA%D1%83%D0%BC%D0%B5%D0%BD%D1%82%D1%8B/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
<d:response><d:href>/plan.txt</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>5</d:getcontentlength></d:prop></d:propstat></d:response>
</d:multistatus>""".encode()


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Recorder:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req.get_method(), req.full_url))
        if req.get_method() == "GET":
            return FakeResp(b"hello")
        return FakeResp(ROOT if req.full_url.rstrip("/").endswith("mail.ru") else ROOT.replace(b"plan.txt", b"plan.txt"))


class Tool(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"MAILCLOUD_USER": "u@mail.ru", "MAILCLOUD_PASSWORD": "pw"})
        self.env.start()
        self.rec = Recorder()
        self.patch = mock.patch.object(ac.urllib.request, "urlopen", self.rec)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.stop()

    def run_cmd(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ac.main(list(argv))
        return json.loads(buf.getvalue())

    def test_list_parses_names_and_hides_the_folder_itself(self):
        out = self.run_cmd("list", "/")
        self.assertEqual([i["name"] for i in out["items"]], ["Документы", "plan.txt"])
        self.assertIn("ВНЕШНИЕ ДАННЫЕ", out["note"])

    def test_only_read_methods_are_ever_sent(self):
        self.run_cmd("list", "/")
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(ac, "propfind", return_value=[{"name": "plan.txt", "type": "file", "size": 5}]):
                self.run_cmd("download", "/plan.txt", "--out", d)
                self.assertEqual(open(os.path.join(d, "plan.txt"), "rb").read(), b"hello")
        self.assertTrue({m for m, _ in self.rec.requests} <= {"PROPFIND", "GET"})
        with self.assertRaises(ValueError):
            ac.request("DELETE", "https://x/")

    def test_missing_credentials_are_an_error(self):
        with mock.patch.dict(os.environ, {"MAILCLOUD_PASSWORD": ""}):
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                ac.main(["list"])


class Writing(unittest.TestCase):
    ENV = {"MAILCLOUD_USER": "u@mail.ru", "MAILCLOUD_PASSWORD": "pw"}
    missing = True

    def run_cmd(self, env, *argv):
        buf = io.StringIO()
        calls = []

        def fake(req, timeout=None):
            calls.append((req.get_method(), req.full_url, dict(req.header_items())))
            if req.get_method() == "PROPFIND" and self.missing:
                raise ac.urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
            return FakeResp(b"<x/>")

        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(ac.urllib.request, "urlopen", fake), redirect_stdout(buf):
            try:
                ac.main(list(argv))
                code = 0
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue(), calls

    def test_every_writing_command_is_refused_without_the_flag(self):
        for argv in (["upload", "/etc/hostname", "/a.txt"], ["mkdir", "/d"], ["move", "/a", "/b"], ["delete", "/a", "--yes"]):
            code, out, calls = self.run_cmd(self.ENV, *argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("только для чтения", out)
            self.assertEqual(calls, [], "nothing may be sent: %s" % argv)

    def test_with_the_flag_writes_go_out_but_never_overwrite_silently(self):
        env = dict(self.ENV, MAILCLOUD_ALLOW_WRITE="1")
        self.missing = True
        code, out, calls = self.run_cmd(env, "mkdir", "/Отчёты")
        self.assertEqual((code, calls[-1][0]), (0, "MKCOL"))
        code, out, calls = self.run_cmd(env, "upload", "/etc/hostname", "/new.txt")
        self.assertEqual((code, calls[-1][0]), (0, "PUT"))
        self.missing = False  # the target exists
        code, out, calls = self.run_cmd(env, "upload", "/etc/hostname", "/new.txt")
        self.assertEqual(code, 1)
        self.assertNotIn("PUT", [c[0] for c in calls])
        code, out, calls = self.run_cmd(env, "upload", "/etc/hostname", "/new.txt", "--overwrite")
        self.assertEqual((code, calls[-1][0]), (0, "PUT"))
        code, out, calls = self.run_cmd(env, "move", "/a", "/b")
        self.assertEqual(code, 1)
        code, out, calls = self.run_cmd(env, "move", "/a", "/b", "--overwrite")
        self.assertEqual(calls[-1][0], "MOVE")
        self.assertEqual(calls[-1][2].get("Overwrite"), "T")

    def test_delete_needs_yes_and_never_the_root(self):
        env = dict(self.ENV, MAILCLOUD_ALLOW_WRITE="1")
        code, out, calls = self.run_cmd(env, "delete", "/a")
        self.assertEqual((code, calls), (1, []))
        code, out, calls = self.run_cmd(env, "delete", "/", "--yes")
        self.assertEqual((code, calls), (1, []))
        code, out, calls = self.run_cmd(env, "delete", "/a", "--yes")
        self.assertEqual((code, calls[-1][0]), (0, "DELETE"))


K_FIRST = 'MAILCLOUD_USER'


class MultiAccount(unittest.TestCase):
    def call(self, env, argv):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(buf):
            try:
                out = ac.apply_account(argv, ac.KEYS, "t")
            except SystemExit as e:
                out = ("exit", e.code)
            return out, buf.getvalue(), dict(os.environ)

    def test_selecting_by_name_copies_that_connection_onto_the_plain_names(self):
        env = {'MAILCLOUD_USER' + "__A": "a", 'MAILCLOUD_USER' + "__B": "b"}
        argv, _, after = self.call(env, ["--account", "b", "list"])
        self.assertEqual(argv, ["list"])
        self.assertEqual(after[K_FIRST], "b")

    def test_a_single_connection_needs_no_flag(self):
        argv, _, after = self.call({'MAILCLOUD_USER' + "__ONLY": "x"}, ["list"])
        self.assertEqual(argv, ["list"])
        self.assertEqual(after[K_FIRST], "x")

    def test_several_connections_without_a_choice_is_an_error(self):
        out, printed, _ = self.call({'MAILCLOUD_USER' + "__A": "a", 'MAILCLOUD_USER' + "__B": "b"}, ["list"])
        self.assertEqual(out[0], "exit")
        self.assertIn("A, B", printed)

    def test_unknown_account_is_an_error_and_accounts_lists_names(self):
        out, printed, _ = self.call({'MAILCLOUD_USER' + "__A": "a"}, ["--account", "zzz", "list"])
        self.assertEqual(out[0], "exit")
        self.assertIn("не найдено", printed)
        out, printed, _ = self.call({K_FIRST: "p", 'MAILCLOUD_USER' + "__A": "a"}, ["accounts"])
        self.assertEqual(json.loads(printed)["accounts"], ["(основное)", "A"])

    def test_the_older_plain_connection_still_works_untouched(self):
        argv, _, after = self.call({K_FIRST: "plain"}, ["list"])
        self.assertEqual(after[K_FIRST], "plain")


if __name__ == "__main__":
    unittest.main()
