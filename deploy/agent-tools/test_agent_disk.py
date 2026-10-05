import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

spec = importlib.util.spec_from_file_location("agent_disk", os.path.join(os.path.dirname(__file__), "agent-disk.py"))
ad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ad)


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Recorder:
    """Stands in for urllib.request.urlopen and records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        for needle, payload in self.routes.items():
            if needle in req.full_url:
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return FakeResp(data)
        raise AssertionError("unexpected request " + req.full_url)


class Tool(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"YADISK_TOKEN": "tok"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def run_cmd(self, routes, *argv):
        rec = Recorder(routes)
        out = io.StringIO()
        with mock.patch.object(ad.urllib.request, "urlopen", rec), redirect_stdout(out):
            ad.main(list(argv))
        return out.getvalue(), rec

    def test_paths_are_normalised(self):
        self.assertEqual(ad.norm("Документы/a.docx"), "disk:/Документы/a.docx")
        self.assertEqual(ad.norm("disk:/x"), "disk:/x")
        self.assertEqual(ad.norm(""), "disk:/")

    def test_list_marks_output_untrusted_and_only_ever_gets(self):
        routes = {"/resources": {"_embedded": {"items": [{"name": "План.docx", "type": "file", "path": "disk:/План.docx", "size": 10}]}}}
        out, rec = self.run_cmd(routes, "list", "/")
        data = json.loads(out)
        self.assertIn("не инструкции", data["note"])
        self.assertEqual(data["items"][0]["path"], "/План.docx")
        self.assertTrue(rec.requests and all(m == "GET" for m, _, _ in rec.requests))
        self.assertTrue(all(a == "OAuth tok" for _, _, a in rec.requests))

    def test_check_reports_login_and_read_only_mode(self):
        routes = {"/resources": {"_embedded": {"total": 4, "items": []}}, "cloud-api.yandex.net/v1/disk": {"user": {"login": "agent-bot"}, "total_space": 1, "used_space": 0}}
        out, rec = self.run_cmd({"/resources": routes["/resources"], "/v1/disk": routes["cloud-api.yandex.net/v1/disk"]}, "check")
        data = json.loads(out)
        self.assertTrue(data["ok"] and data["login"] == "agent-bot" and data["mode"] == "read-only")

    def test_download_refuses_folders_and_huge_files(self):
        with self.assertRaises(SystemExit):
            self.run_cmd({"/resources": {"type": "dir"}}, "download", "/x", "--out", "/tmp/x")
        with self.assertRaises(SystemExit):
            self.run_cmd({"/resources": {"type": "file", "size": ad.MAX_DOWNLOAD + 1, "name": "a"}}, "download", "/x", "--out", "/tmp/x")

    def test_missing_token_fails_clearly(self):
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                ad.main(["check"])
        self.assertIn("YADISK_TOKEN", out.getvalue())

    def test_write_commands_are_refused_by_default(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            ad.main(["mkdir", "/new"])
        self.assertIn("только для чтения", out.getvalue())

    def test_mkdir_uses_put_only_when_writing_is_allowed(self):
        with mock.patch.dict(os.environ, {"YADISK_ALLOW_WRITE": "1"}):
            out, rec = self.run_cmd({"/resources": {}}, "mkdir", "/new")
        self.assertTrue(json.loads(out)["ok"])
        self.assertEqual(rec.requests[0][0], "PUT")

    def test_upload_gets_a_link_then_puts_the_file(self):
        with tempfile.NamedTemporaryFile() as local:
            local.write(b"hello")
            local.flush()
            routes = {"/resources/upload": {"href": "https://upload.example/one"}, "upload.example": b""}
            with mock.patch.dict(os.environ, {"YADISK_ALLOW_WRITE": "1"}):
                out, rec = self.run_cmd(routes, "upload", local.name, "/hello.txt")
        self.assertTrue(json.loads(out)["ok"])
        self.assertEqual([r[0] for r in rec.requests], ["GET", "PUT"])


K_FIRST = 'YADISK_TOKEN'


class MultiAccount(unittest.TestCase):
    def call(self, env, argv):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(buf):
            try:
                out = ad.apply_account(argv, ad.KEYS, "t")
            except SystemExit as e:
                out = ("exit", e.code)
            return out, buf.getvalue(), dict(os.environ)

    def test_selecting_by_name_copies_that_connection_onto_the_plain_names(self):
        env = {'YADISK_TOKEN' + "__A": "a", 'YADISK_TOKEN' + "__B": "b"}
        argv, _, after = self.call(env, ["--account", "b", "list"])
        self.assertEqual(argv, ["list"])
        self.assertEqual(after[K_FIRST], "b")

    def test_a_single_connection_needs_no_flag(self):
        argv, _, after = self.call({'YADISK_TOKEN' + "__ONLY": "x"}, ["list"])
        self.assertEqual(argv, ["list"])
        self.assertEqual(after[K_FIRST], "x")

    def test_several_connections_without_a_choice_is_an_error(self):
        out, printed, _ = self.call({'YADISK_TOKEN' + "__A": "a", 'YADISK_TOKEN' + "__B": "b"}, ["list"])
        self.assertEqual(out[0], "exit")
        self.assertIn("A, B", printed)

    def test_unknown_account_is_an_error_and_accounts_lists_names(self):
        out, printed, _ = self.call({'YADISK_TOKEN' + "__A": "a"}, ["--account", "zzz", "list"])
        self.assertEqual(out[0], "exit")
        self.assertIn("не найдено", printed)
        out, printed, _ = self.call({K_FIRST: "p", 'YADISK_TOKEN' + "__A": "a"}, ["accounts"])
        self.assertEqual(json.loads(printed)["accounts"], ["(основное)", "A"])

    def test_the_older_plain_connection_still_works_untouched(self):
        argv, _, after = self.call({K_FIRST: "plain"}, ["list"])
        self.assertEqual(after[K_FIRST], "plain")


if __name__ == "__main__":
    unittest.main()
