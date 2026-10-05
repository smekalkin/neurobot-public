"""agent-dropbox, agent-gdrive and agent-icloud: the boundaries that matter --
read-only unless the connection says otherwise, nothing outside the folder the
connection was limited to, deletes only when a person confirmed -- plus the
listing/download behaviour around them. The network and rclone are faked.

Run: python3 test_agent_drives.py
"""
import base64
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dropbox = load("agent_dropbox", "agent-dropbox.py")
gdrive = load("agent_gdrive", "agent-gdrive.py")
icloud = load("agent_icloud", "agent-icloud.py")
cloud = load("agent_cloud2", "agent-cloud.py")
disk = load("agent_disk2", "agent-disk.py")


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run_tool(mod, *argv):
    """(exit_code, parsed_json_or_text) of a tool invocation."""
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            mod.main(list(argv))
        except SystemExit as e:
            code = e.code or 0
    out = buf.getvalue().strip()
    try:
        return code, json.loads(out)
    except ValueError:
        return code, out


class Net:
    """Answers urlopen from a list of (substring, body) routes; remembers requests."""

    def __init__(self, *routes):
        self.routes = list(routes)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req.get_method(), req.full_url, req.data))
        for needle, body in self.routes:
            if needle in req.full_url:
                data = body(req) if callable(body) else body
                return Resp(data if isinstance(data, bytes) else json.dumps(data).encode())
        raise AssertionError("unexpected request " + req.full_url)

    def methods(self):
        return [m for m, _, _ in self.requests]


class DropboxTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"DROPBOX_ACCESS_TOKEN": "tok"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_check_reports_the_mode(self):
        net = Net(("get_current_account", {"name": {"display_name": "Ann"}, "email": "a@b.c"}))
        with mock.patch.object(dropbox.urllib.request, "urlopen", net):
            self.assertEqual(run_tool(dropbox, "check")[1]["mode"], "read-only")
            os.environ["DROPBOX_ALLOW_WRITE"] = "1"
            self.assertEqual(run_tool(dropbox, "check")[1]["mode"], "read-write")

    def test_every_writing_command_is_refused_without_permission_and_sends_nothing(self):
        net = Net()
        with mock.patch.object(dropbox.urllib.request, "urlopen", net):
            for argv in (["upload", "/etc/hostname", "/x"], ["mkdir", "/x"], ["move", "/a", "/b"], ["delete", "/a", "--yes"]):
                code, out = run_tool(dropbox, *argv)
                self.assertEqual(code, 1, argv)
                self.assertFalse(out["ok"], argv)
        self.assertEqual(net.requests, [])

    def test_delete_needs_yes_and_never_the_root(self):
        os.environ["DROPBOX_ALLOW_WRITE"] = "1"
        net = Net(("delete_v2", {"metadata": {"path_display": "/a"}}))
        with mock.patch.object(dropbox.urllib.request, "urlopen", net):
            self.assertEqual(run_tool(dropbox, "delete", "/a")[0], 1)
            self.assertEqual(run_tool(dropbox, "delete", "/", "--yes")[0], 1)
            self.assertEqual(net.requests, [])
            code, out = run_tool(dropbox, "delete", "/a", "--yes")
        self.assertEqual((code, out["deleted"]), (0, "/a"))

    def test_paths_with_dotdot_are_refused(self):
        net = Net()
        os.environ["DROPBOX_ALLOW_WRITE"] = "1"
        with mock.patch.object(dropbox.urllib.request, "urlopen", net):
            for argv in (["list", "/a/../b"], ["info", "../x"], ["mkdir", "/a/../../b"]):
                self.assertEqual(run_tool(dropbox, *argv)[0], 1, argv)
        self.assertEqual(net.requests, [])

    def test_listing_follows_pages_and_marks_data_as_untrusted(self):
        pages = iter([
            {"entries": [{".tag": "folder", "name": "A", "path_display": "/A"}], "has_more": True, "cursor": "c"},
            {"entries": [{".tag": "file", "name": "b.txt", "path_display": "/b.txt", "size": 3}], "has_more": False},
        ])
        net = Net(("list_folder", lambda req: json.dumps(next(pages)).encode()))
        with mock.patch.object(dropbox.urllib.request, "urlopen", net):
            code, out = run_tool(dropbox, "list")
        self.assertEqual([i["name"] for i in out["items"]], ["A", "b.txt"])
        self.assertEqual(out["items"][0]["type"], "dir")
        self.assertIn("ДАННЫЕ", out["note"])

    def test_find_matches_names_case_insensitively(self):
        net = Net(("list_folder", {"entries": [{"name": "Plan.TXT", ".tag": "file"}, {"name": "x", ".tag": "file"}], "has_more": False}))
        with mock.patch.object(dropbox.urllib.request, "urlopen", net):
            out = run_tool(dropbox, "find", "plan")[1]
        self.assertEqual([m["name"] for m in out["matches"]], ["Plan.TXT"])

    def test_download_sanitizes_the_name_and_refuses_folders_and_huge_files(self):
        out_dir = tempfile.mkdtemp()
        meta = {".tag": "file", "name": "re/port?.txt", "size": 5}
        net = Net(("get_metadata", meta), ("files/download", b"hello"))
        with mock.patch.object(dropbox.urllib.request, "urlopen", net):
            code, out = run_tool(dropbox, "download", "/r", "--out", out_dir)
        self.assertEqual(code, 0)
        self.assertEqual(os.listdir(out_dir), ["re_port_.txt"])
        for bad in ({".tag": "folder", "name": "d"}, {".tag": "file", "name": "big", "size": dropbox.MAX_FILE + 1}):
            with mock.patch.object(dropbox.urllib.request, "urlopen", Net(("get_metadata", bad))):
                self.assertEqual(run_tool(dropbox, "download", "/r", "--out", out_dir)[0], 1)

    def test_upload_mode_follows_the_overwrite_flag(self):
        os.environ["DROPBOX_ALLOW_WRITE"] = "1"
        local = os.path.join(tempfile.mkdtemp(), "f.txt")
        with open(local, "w") as f:
            f.write("data")
        net = Net(("files/upload", {"path_display": "/f.txt", "size": 4}))
        with mock.patch.object(dropbox.urllib.request, "urlopen", net):
            self.assertEqual(run_tool(dropbox, "upload", local, "/f.txt")[0], 0)
            self.assertEqual(run_tool(dropbox, "upload", local, "/f.txt", "--overwrite")[0], 0)
            self.assertEqual(run_tool(dropbox, "upload", "/no/such/file", "/f.txt")[0], 1)

    def test_move_and_mkdir_when_allowed(self):
        os.environ["DROPBOX_ALLOW_WRITE"] = "1"
        net = Net(("create_folder_v2", {"metadata": {"path_display": "/n"}}), ("move_v2", {"metadata": {"path_display": "/b"}}))
        with mock.patch.object(dropbox.urllib.request, "urlopen", net):
            self.assertEqual(run_tool(dropbox, "mkdir", "/n")[1]["created"], "/n")
            self.assertEqual(run_tool(dropbox, "move", "/a", "/b")[1]["to"], "/b")

    def test_api_errors_are_reported_without_the_token(self):
        def boom(req, timeout=None):
            raise dropbox.urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"expired"))

        with mock.patch.object(dropbox.urllib.request, "urlopen", boom):
            code, out = run_tool(dropbox, "check")
        self.assertEqual(code, 1)
        self.assertIn("401", out["error"])
        self.assertNotIn("tok", out["error"])
        with mock.patch.object(dropbox.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertIn("нет связи", run_tool(dropbox, "check")[1]["error"])
        os.environ.pop("DROPBOX_ACCESS_TOKEN")
        self.assertEqual(run_tool(dropbox, "check")[0], 1)


class AccountSelectionTest(unittest.TestCase):
    """The same "several connections of one service" rules in every tool."""

    def check(self, mod, key, plain_key, value="v"):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ[plain_key] = value
            os.environ[key + "__SUPPORT"] = "s"
            os.environ[key + "__SALES"] = "x"
            code, out = run_tool(mod, "accounts")
            self.assertEqual(out["accounts"], ["(основное)", "SALES", "SUPPORT"])

        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ[key + "__A"] = "1"
            os.environ[key + "__B"] = "2"
            code, out = run_tool(mod, "list")
            self.assertEqual(code, 1)
            self.assertIn("несколько", out["error"])

    def test_dropbox(self):
        self.check(dropbox, "DROPBOX_ACCESS_TOKEN", "DROPBOX_ACCESS_TOKEN")
        with mock.patch.dict(os.environ, {"DROPBOX_ACCESS_TOKEN__A": "1", "DROPBOX_ACCESS_TOKEN__B": "2"}, clear=True):
            self.assertEqual(run_tool(dropbox, "--account", "C", "list")[0], 1)
            with mock.patch.object(dropbox.urllib.request, "urlopen", Net(("list_folder", {"entries": []}))):
                self.assertEqual(run_tool(dropbox, "--account=B", "list")[0], 0)
                self.assertEqual(os.environ["DROPBOX_ACCESS_TOKEN"], "2")

    def test_a_single_named_connection_is_used_without_naming_it(self):
        with mock.patch.dict(os.environ, {"DROPBOX_ACCESS_TOKEN__ONLY": "9"}, clear=True):
            with mock.patch.object(dropbox.urllib.request, "urlopen", Net(("list_folder", {"entries": []}))):
                self.assertEqual(run_tool(dropbox, "list")[0], 0)
            self.assertEqual(os.environ["DROPBOX_ACCESS_TOKEN"], "9")

    def test_gdrive(self):
        self.check(gdrive, "GDRIVE_REFRESH_TOKEN", "GDRIVE_REFRESH_TOKEN")
        with mock.patch.dict(os.environ, {"GDRIVE_REFRESH_TOKEN__A": "1"}, clear=True):
            self.assertEqual(run_tool(gdrive, "--account", "Z", "list")[0], 1)

    def test_icloud(self):
        self.check(icloud, "ICLOUD_RCLONE_CONFIG_B64", "ICLOUD_RCLONE_CONFIG_B64")


class GdriveTest(unittest.TestCase):
    def setUp(self):
        gdrive._token = None
        self.env = mock.patch.dict(os.environ, {
            "GDRIVE_CLIENT_ID": "id", "GDRIVE_CLIENT_SECRET": "sec", "GDRIVE_REFRESH_TOKEN": "rt",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(lambda: setattr(gdrive, "_token", None))

    def drive(self, files):
        """A fake Drive: files is {id: {name, parents, mimeType, size}}."""
        def handler(req):
            url = req.full_url
            if "oauth2.googleapis.com/token" in url:
                return json.dumps({"access_token": "AT"}).encode()
            import urllib.parse as up
            q = up.parse_qs(up.urlparse(url).query)
            path = up.urlparse(url).path
            if path.endswith("/files") and "q" in q:
                import re as _re
                query = q["q"][0]
                parent = _re.search(r"'([^']*)' in parents", query)
                exact = _re.search(r"name = '([^']*)'", query)
                contains = _re.search(r"name contains '([^']*)'", query)
                out = []
                for fid, f in files.items():
                    if parent and parent.group(1) not in (f.get("parents") or []):
                        continue
                    if exact and f["name"] != exact.group(1):
                        continue
                    if contains and contains.group(1).lower() not in f["name"].lower():
                        continue
                    out.append(dict(f, id=fid))
                return json.dumps({"files": out}).encode()
            if "/files/" in path:
                fid = up.unquote(path.split("/files/")[1])
                if q.get("alt") == ["media"]:
                    return b"content"
                if req.get_method() == "PATCH":
                    return json.dumps({"id": fid}).encode()
                f = files.get(fid)
                if not f:
                    raise AssertionError("unknown file " + fid)
                return json.dumps(dict(f, id=fid)).encode()
            if "upload" in url:
                return json.dumps({"id": "new"}).encode()
            if path.endswith("/files"):
                return json.dumps({"id": "newfolder"}).encode()
            raise AssertionError(url)
        return Net(("googleapis.com", handler))

    TREE = {
        "drive": {"name": "My Drive", "parents": [], "mimeType": "application/vnd.google-apps.folder"},
        "ROOT": {"name": "Agent", "parents": ["drive"], "mimeType": "application/vnd.google-apps.folder"},
        "docs": {"name": "docs", "parents": ["ROOT"], "mimeType": "application/vnd.google-apps.folder"},
        "plan": {"name": "plan.txt", "parents": ["docs"], "mimeType": "text/plain", "size": "7"},
        "secret": {"name": "secret.txt", "parents": ["other"], "mimeType": "text/plain", "size": "3"},
        "other": {"name": "Other", "parents": ["drive"], "mimeType": "application/vnd.google-apps.folder"},
    }

    def test_a_path_is_walked_from_the_root_folder(self):
        os.environ["GDRIVE_ROOT_ID"] = "ROOT"
        with mock.patch.object(gdrive.urllib.request, "urlopen", self.drive(self.TREE)):
            code, out = run_tool(gdrive, "info", "/docs/plan.txt")
        self.assertEqual((code, out["item"]["id"]), (0, "plan"))

    def test_an_id_outside_the_root_folder_is_refused(self):
        os.environ["GDRIVE_ROOT_ID"] = "ROOT"
        net = self.drive(self.TREE)
        with mock.patch.object(gdrive.urllib.request, "urlopen", net):
            self.assertEqual(run_tool(gdrive, "info", "id:plan")[0], 0)      # below the root
            code, out = run_tool(gdrive, "info", "id:secret")                # in another folder
            self.assertEqual(code, 1)
            self.assertIn("вне корневой папки", out["error"])
            self.assertEqual(run_tool(gdrive, "info", "id:ROOT")[0], 0)      # the root itself
            self.assertEqual(run_tool(gdrive, "info", "id:other")[0], 1)
            self.assertEqual(run_tool(gdrive, "download", "id:secret", "--out", tempfile.mkdtemp())[0], 1)

    def test_a_search_shows_only_what_is_inside_the_root_folder(self):
        os.environ["GDRIVE_ROOT_ID"] = "ROOT"
        with mock.patch.object(gdrive.urllib.request, "urlopen", self.drive(self.TREE)):
            code, out = run_tool(gdrive, "find", "plan.txt")
            self.assertEqual([m["id"] for m in out["matches"]], ["plan"])
            code, out = run_tool(gdrive, "find", "secret.txt")
        self.assertEqual(out["matches"], [])

    def test_without_a_dedicated_root_everything_is_in_scope(self):
        with mock.patch.object(gdrive.urllib.request, "urlopen", self.drive(self.TREE)):
            self.assertEqual(run_tool(gdrive, "info", "id:secret")[0], 0)

    def test_writing_needs_permission_and_nothing_is_sent_without_it(self):
        net = self.drive(self.TREE)
        with mock.patch.object(gdrive.urllib.request, "urlopen", net):
            for argv in (["mkdir", "n"], ["move", "id:plan", "--parent", "id:docs"], ["delete", "id:plan", "--yes"],
                         ["upload", "/etc/hostname"]):
                self.assertEqual(run_tool(gdrive, *argv)[0], 1, argv)
        self.assertEqual([m for m in net.methods() if m != "GET"], [])

    def test_delete_trashes_only_with_yes_and_never_the_root(self):
        os.environ.update({"GDRIVE_ALLOW_WRITE": "1", "GDRIVE_ROOT_ID": "ROOT"})
        net = self.drive(self.TREE)
        with mock.patch.object(gdrive.urllib.request, "urlopen", net):
            self.assertEqual(run_tool(gdrive, "delete", "id:plan")[0], 1)
            self.assertEqual(run_tool(gdrive, "delete", "id:ROOT", "--yes")[0], 1)
            self.assertEqual(run_tool(gdrive, "delete", "id:plan", "--yes")[0], 0)
        patches = [r for r in net.requests if r[0] == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertEqual(json.loads(patches[0][2]), {"trashed": True})

    def test_google_documents_and_big_files_are_not_downloaded(self):
        tree = dict(self.TREE, gdoc={"name": "doc", "parents": ["ROOT"], "mimeType": "application/vnd.google-apps.document"},
                    big={"name": "big", "parents": ["ROOT"], "mimeType": "x/y", "size": str(gdrive.MAX_FILE + 1)})
        os.environ["GDRIVE_ROOT_ID"] = "ROOT"
        with mock.patch.object(gdrive.urllib.request, "urlopen", self.drive(tree)):
            out_dir = tempfile.mkdtemp()
            self.assertEqual(run_tool(gdrive, "download", "id:gdoc", "--out", out_dir)[0], 1)
            self.assertEqual(run_tool(gdrive, "download", "id:big", "--out", out_dir)[0], 1)
            self.assertEqual(run_tool(gdrive, "download", "id:plan", "--out", out_dir)[0], 0)
        self.assertEqual(os.listdir(out_dir), ["plan.txt"])

    def test_the_token_is_fetched_once_and_a_failure_is_explained(self):
        net = self.drive(self.TREE)
        with mock.patch.object(gdrive.urllib.request, "urlopen", net):
            run_tool(gdrive, "info", "id:plan")
            run_tool(gdrive, "info", "id:plan")
        self.assertEqual(sum("oauth2" in u for _, u, _ in net.requests), 1)
        gdrive._token = None
        with mock.patch.object(gdrive.urllib.request, "urlopen", side_effect=OSError("offline")):
            code, out = run_tool(gdrive, "check")
        self.assertIn("не удалось обновить токен", out["error"])
        gdrive._token = None
        os.environ.pop("GDRIVE_REFRESH_TOKEN")
        self.assertEqual(run_tool(gdrive, "check")[0], 1)

    def test_an_ambiguous_name_asks_for_an_id_and_a_missing_one_says_so(self):
        tree = dict(self.TREE, dup={"name": "plan.txt", "parents": ["docs"], "mimeType": "text/plain"})
        os.environ["GDRIVE_ROOT_ID"] = "ROOT"
        with mock.patch.object(gdrive.urllib.request, "urlopen", self.drive(tree)):
            self.assertIn("id:", run_tool(gdrive, "info", "/docs/plan.txt")[1]["error"])
            self.assertIn("не найден", run_tool(gdrive, "info", "/docs/missing.txt")[1]["error"])

    def test_upload_mkdir_and_move_when_allowed(self):
        os.environ.update({"GDRIVE_ALLOW_WRITE": "1", "GDRIVE_ROOT_ID": "ROOT"})
        local = os.path.join(tempfile.mkdtemp(), "n.txt")
        with open(local, "w") as f:
            f.write("hello")
        with mock.patch.object(gdrive.urllib.request, "urlopen", self.drive(self.TREE)):
            self.assertEqual(run_tool(gdrive, "upload", local, "--parent", "/docs")[1]["uploaded"]["id"], "new")
            self.assertEqual(run_tool(gdrive, "upload", "/no/file")[0], 1)
            self.assertEqual(run_tool(gdrive, "mkdir", "sub", "--parent", "/docs")[0], 0)
            self.assertEqual(run_tool(gdrive, "move", "id:plan", "--parent", "/")[0], 0)
            # moving something out of the root is refused too
            self.assertEqual(run_tool(gdrive, "move", "id:secret", "--parent", "/")[0], 1)

    def test_listing_marks_data_untrusted_and_check_reports_the_mode(self):
        os.environ["GDRIVE_ROOT_ID"] = "ROOT"
        net = self.drive(self.TREE)
        net.routes.insert(0, ("/about", {"user": {"displayName": "A"}, "storageQuota": {}}))
        with mock.patch.object(gdrive.urllib.request, "urlopen", net):
            out = run_tool(gdrive, "list", "/docs")[1]
            self.assertIn("ДАННЫЕ", out["note"])
            self.assertEqual(out["items"][0]["name"], "plan.txt")
            self.assertEqual(run_tool(gdrive, "check")[1]["mode"], "read-only")


class IcloudTest(unittest.TestCase):
    def setUp(self):
        cfg = base64.b64encode(b"[icloud]\ntype = webdav\n").decode()
        self.env = mock.patch.dict(os.environ, {"ICLOUD_RCLONE_CONFIG_B64": cfg, "ICLOUD_ROOT": "AgentDesk"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []
        self.config_seen = []

        def run(args, **kw):
            self.calls.append(args)
            cfg_path = args[args.index("--config") + 1]
            with open(cfg_path) as f:
                self.config_seen.append((cfg_path, stat.S_IMODE(os.stat(cfg_path).st_mode), f.read()))
            out = mock.Mock(returncode=0, stdout="[]", stderr="")
            return out

        p1 = mock.patch.object(icloud.subprocess, "run", run)
        p2 = mock.patch.object(icloud.shutil, "which", lambda name: "/usr/bin/rclone")
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_every_path_is_placed_under_the_root_folder(self):
        run_tool(icloud, "list", "/docs/a")
        self.assertIn("icloud:AgentDesk/docs/a", self.calls[0])
        self.assertEqual(icloud.target(""), "icloud:AgentDesk")
        os.environ["ICLOUD_ROOT"] = ""
        os.environ["ICLOUD_RCLONE_REMOTE"] = "mine"
        self.assertEqual(icloud.target("x"), "mine:x")

    def test_dotdot_cannot_leave_the_root_folder(self):
        for argv in (["list", "../other"], ["download", "a/../../b", "--out", tempfile.mkdtemp()],
                     ["find", "x", "--path", ".."]):
            code, out = run_tool(icloud, *argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("..", out["error"])
        os.environ["ICLOUD_ALLOW_WRITE"] = "1"
        for argv in (["upload", "/etc/hostname", "../x"], ["mkdir", ".."], ["move", "a", "../b"], ["delete", "../a", "--yes"]):
            self.assertEqual(run_tool(icloud, *argv)[0], 1, argv)
        self.assertEqual(self.calls, [])

    def test_names_that_merely_contain_dots_are_fine(self):
        self.assertEqual(icloud.target("a/..b/c..d/..."), "icloud:AgentDesk/a/..b/c..d/...")

    def test_writing_needs_permission_and_runs_nothing_without_it(self):
        for argv in (["upload", "/etc/hostname", "x"], ["mkdir", "x"], ["move", "a", "b"], ["delete", "a", "--yes"]):
            self.assertEqual(run_tool(icloud, *argv)[0], 1, argv)
        self.assertEqual(self.calls, [])

    def test_delete_needs_yes_and_never_the_root(self):
        os.environ["ICLOUD_ALLOW_WRITE"] = "1"
        self.assertEqual(run_tool(icloud, "delete", "a")[0], 1)
        self.assertEqual(run_tool(icloud, "delete", "/", "--yes")[0], 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(run_tool(icloud, "delete", "a", "--yes")[0], 0)
        self.assertIn("deletefile", self.calls[0])

    def test_the_rclone_config_is_private_and_removed_afterwards(self):
        run_tool(icloud, "check")
        path, mode, content = self.config_seen[0]
        self.assertEqual(mode, 0o600)
        self.assertIn("[icloud]", content)
        self.assertFalse(os.path.exists(path))

    def test_misconfiguration_is_explained(self):
        os.environ["ICLOUD_RCLONE_CONFIG_B64"] = "!!!not-base64"
        self.assertIn("base64", run_tool(icloud, "check")[1]["error"])
        os.environ.pop("ICLOUD_RCLONE_CONFIG_B64")
        self.assertIn("не задана", run_tool(icloud, "check")[1]["error"])
        os.environ["ICLOUD_RCLONE_CONFIG_B64"] = base64.b64encode(b"x").decode()
        with mock.patch.object(icloud.shutil, "which", lambda n: None):
            self.assertIn("rclone", run_tool(icloud, "check")[1]["error"])

    def test_a_failing_rclone_reports_its_last_line(self):
        def run(args, **kw):
            return mock.Mock(returncode=1, stdout="", stderr="first\nNOTICE: boom\n")

        with mock.patch.object(icloud.subprocess, "run", run):
            self.assertEqual(run_tool(icloud, "check")[1]["error"], "rclone: NOTICE: boom")

    def test_find_download_upload_move_mkdir(self):
        os.environ["ICLOUD_ALLOW_WRITE"] = "1"
        out_dir = tempfile.mkdtemp()
        with mock.patch.object(icloud.subprocess, "run", lambda args, **kw: mock.Mock(returncode=0, stderr="", stdout='[{"Name": "Plan.txt"}, {"Name": "x"}]')):
            self.assertEqual([m["Name"] for m in run_tool(icloud, "find", "plan")[1]["matches"]], ["Plan.txt"])
            self.assertEqual(run_tool(icloud, "download", "docs/a.txt", "--out", out_dir)[1]["saved"], os.path.join(out_dir, "a.txt"))
            self.assertEqual(run_tool(icloud, "upload", "/tmp/x", "d/x")[0], 0)
            self.assertEqual(run_tool(icloud, "mkdir", "d")[0], 0)
            self.assertEqual(run_tool(icloud, "move", "a", "b")[0], 0)
            out = run_tool(icloud, "list")[1]
        self.assertIn("ДАННЫЕ", out["note"])


class OtherToolsRefuseDotDotTest(unittest.TestCase):
    def test_cloud_and_disk_normalise_dotdot_away(self):
        for mod in (cloud, disk):
            with self.assertRaises(SystemExit):
                with redirect_stdout(io.StringIO()):
                    mod.norm("/a/../../etc")
        self.assertEqual(cloud.norm("a/b"), "/a/b")
        self.assertEqual(disk.norm("a/..b"), "disk:/a/..b")


if __name__ == "__main__":
    unittest.main()
