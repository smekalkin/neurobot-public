"""agent-http: the boundaries that matter -- the secret is added by the tool and
never shown, requests cannot leave the connected server, nothing is written
unless the connection allows it, redirects stay on the same origin.

A real local HTTP server stands in for the API. Run: python3 test_agent_http.py
"""
import contextlib
import importlib.util
import io
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("agent_http", os.path.join(HERE, "agent-http.py"))
http = importlib.util.module_from_spec(spec)
spec.loader.exec_module(http)

SECRET = "s3cr3t-token-value"
seen = []


class Handler(BaseHTTPRequestHandler):
    other_port = 0

    def log_message(self, *a):
        pass

    def reply(self, status, body, ctype="application/json", extra=None):
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        p = self.path.split("?")[0]
        if p == "/echo":
            self.reply(200, json.dumps({"auth": self.headers.get("Authorization"), "leak": SECRET}))
        elif p == "/big":
            self.reply(200, json.dumps({"rows": ["x" * 100] * 1000}))
        elif p == "/text":
            self.reply(200, "plain words", "text/plain")
        elif p == "/missing":
            self.reply(404, json.dumps({"error": "no such thing"}))
        elif p == "/hop":
            self.reply(302, "", extra={"Location": "/echo"})
        elif p == "/away":
            self.reply(302, "", extra={"Location": "http://127.0.0.1:%d/echo" % self.other_port})
        elif p == "/openapi.json":
            self.reply(200, json.dumps({"info": {"title": "Shop"}, "paths": {
                "/orders": {"get": {"summary": "List orders"}, "post": {"summary": "Create order"}},
                "/customers/{id}": {"get": {"summary": "One customer"}, "parameters": []}}}))
        elif p == "/docs.txt":
            self.reply(200, "just text", "text/plain")
        else:
            self.reply(200, json.dumps({"path": self.path, "method": self.command}))

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any


def serve():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.other = serve(), serve()
        Handler.other_port = cls.other.server_port
        cls.url = "http://127.0.0.1:%d" % cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.other.shutdown()

    def setUp(self):
        del seen[:]

    def cfg(self, **env):
        base = {"API_BASE_URL": self.url + "/api", "API_AUTH": "bearer", "API_TOKEN": SECRET}
        base.update(env)
        return http.Config(base)


class AuthTest(Base):
    def test_bearer_token_is_added_and_never_shown(self):
        cfg = self.cfg(API_BASE_URL=self.url)
        out = http.do_request(cfg, "GET", "/echo")
        self.assertEqual(seen[0]["headers"]["Authorization"], "Bearer " + SECRET)
        self.assertNotIn(SECRET, json.dumps(out))
        self.assertEqual(out["body"]["leak"], "***")
        self.assertTrue(out["ok"])

    def test_header_basic_query_and_none(self):
        http.do_request(self.cfg(API_AUTH="header"), "GET", "/x")
        self.assertEqual(seen[-1]["headers"]["X-Api-Key"], SECRET)
        http.do_request(self.cfg(API_AUTH="header", API_AUTH_NAME="X-Token"), "GET", "/x")
        self.assertEqual(seen[-1]["headers"]["X-Token"], SECRET)
        http.do_request(self.cfg(API_AUTH="basic", API_USER="bob"), "GET", "/x")
        import base64
        self.assertEqual(seen[-1]["headers"]["Authorization"], "Basic " + base64.b64encode(("bob:" + SECRET).encode()).decode())
        http.do_request(self.cfg(API_AUTH="query"), "GET", "/x", ["a=1"])
        self.assertIn("a=1", seen[-1]["path"])
        self.assertIn("api_key=" + SECRET, seen[-1]["path"])
        http.do_request(self.cfg(API_AUTH="none", API_TOKEN=""), "GET", "/x")
        self.assertNotIn("Authorization", seen[-1]["headers"])

    def test_extra_headers_and_forbidden_ones(self):
        http.do_request(self.cfg(API_HEADERS='{"X-Tenant": "acme"}'), "GET", "/x")
        self.assertEqual(seen[-1]["headers"]["X-Tenant"], "acme")
        with self.assertRaises(http.Fail):
            http.do_request(self.cfg(API_HEADERS='{"Host": "evil"}'), "GET", "/x")
        with self.assertRaises(http.Fail):
            http.do_request(self.cfg(), "GET", "/x", extra_headers=["Content-Length: 5"])
        with self.assertRaises(http.Fail):
            self.cfg(API_HEADERS="not json")

    def test_bad_settings_are_refused(self):
        for env in ({"API_BASE_URL": "ftp://x"}, {"API_BASE_URL": "http://u:p@host/"}, {"API_BASE_URL": ""},
                    {"API_AUTH": "magic"}, {"API_TOKEN": ""}):
            with self.assertRaises(http.Fail, msg=str(env)):
                self.cfg(**env)


class ConfinementTest(Base):
    def test_a_path_cannot_leave_the_connected_server(self):
        cfg = self.cfg()
        for bad in ("http://evil.example/x", "//evil.example/x", "../x", "/a/../b", "/a/./b", "/a b", "", "  ", "/a\\b",
                    "https://x", "/x\r\nHost: evil"):
            with self.assertRaises(http.Fail, msg=repr(bad)):
                http.build_url(cfg, bad, [])
        self.assertEqual(http.build_url(cfg, "orders/12", ["page=2"]), self.url + "/api/orders/12?page=2")
        self.assertEqual(http.build_url(cfg, "/orders?x=1", ["y=2"]), self.url + "/api/orders?x=1&y=2")
        with self.assertRaises(http.Fail):
            http.build_url(cfg, "/x", ["noequals"])

    def test_at_sign_tricks_stay_on_the_server(self):
        # "@host" after the base would move the authority only if the base ended
        # without a slash and the path did not start with one; the path always gets one
        cfg = self.cfg()
        url = http.build_url(cfg, "@evil.example/x", [])
        self.assertEqual(http.origin_of(url), cfg.origin)

    def test_redirects_inside_the_server_are_followed_others_refused(self):
        out = http.do_request(self.cfg(API_BASE_URL=self.url), "GET", "/hop")
        self.assertEqual(out["status"], 200)
        with self.assertRaises(http.Fail) as c:
            http.do_request(self.cfg(API_BASE_URL=self.url), "GET", "/away")
        self.assertIn("другой адрес", str(c.exception))
        self.assertFalse([s for s in seen if s["path"] == "/echo" and s["headers"].get("Host", "").endswith(str(self.other.server_port))],
                         "the credentials must not reach the other server")

    def test_a_dead_server_is_reported_without_a_traceback(self):
        cfg = http.Config({"API_BASE_URL": "http://127.0.0.1:1", "API_AUTH": "none"})
        with self.assertRaises(http.Fail):
            http.do_request(cfg, "GET", "/x")


class WriteTest(Base):
    def test_read_only_by_default(self):
        cfg = self.cfg()
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            with self.assertRaises(http.Fail, msg=m):
                http.do_request(cfg, m, "/orders", body="{}")
        for m in ("TRACE", "CONNECT", "FOO"):
            with self.assertRaises(http.Fail, msg=m):
                http.do_request(self.cfg(API_ALLOW_WRITE="1"), m, "/orders")
        self.assertEqual(seen, [], "nothing may reach the server")
        self.assertTrue(http.do_request(cfg, "HEAD", "/orders")["ok"])

    def test_writing_when_allowed_sends_the_body(self):
        cfg = self.cfg(API_ALLOW_WRITE="1")
        out = http.do_request(cfg, "POST", "/orders", body='{"a": 1}')
        self.assertTrue(out["ok"])
        self.assertEqual(seen[-1]["method"], "POST")
        self.assertEqual(seen[-1]["body"], '{"a": 1}')
        self.assertEqual(seen[-1]["headers"]["Content-Type"], "application/json")


class ResponseTest(Base):
    def test_errors_keep_their_body_and_status(self):
        out = http.do_request(self.cfg(API_BASE_URL=self.url), "GET", "/missing")
        self.assertFalse(out["ok"])
        self.assertEqual((out["status"], out["body"]), (404, {"error": "no such thing"}))

    def test_big_answers_are_cut_and_text_is_text(self):
        out = http.do_request(self.cfg(API_BASE_URL=self.url), "GET", "/big", limit=500)
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["body"]), 500)
        self.assertIsInstance(out["body"], str)
        out = http.do_request(self.cfg(API_BASE_URL=self.url), "GET", "/text")
        self.assertEqual((out["body"], out["content_type"], out["truncated"]), ("plain words", "text/plain", False))
        self.assertIn("не инструкции", out["note"])


class OpenapiTest(Base):
    def test_operations_are_listed_and_filtered(self):
        cfg = self.cfg(API_BASE_URL=self.url, API_OPENAPI_URL=self.url + "/openapi.json")
        out = http.do_openapi(cfg)
        self.assertEqual(out["format"], "openapi")
        self.assertEqual(out["title"], "Shop")
        self.assertEqual(out["operations"].splitlines(), ["GET /customers/{id} - One customer", "GET /orders - List orders",
                                                           "POST /orders - Create order"])
        only = http.do_openapi(cfg, "customer")
        self.assertEqual(only["operations"], "GET /customers/{id} - One customer")
        # same server: the credentials go along
        self.assertEqual(seen[0]["headers"].get("Authorization"), "Bearer " + SECRET)

    def test_other_servers_get_no_credentials_and_text_is_passed_through(self):
        cfg = self.cfg(API_OPENAPI_URL="http://127.0.0.1:%d/docs.txt" % self.other.server_port)
        out = http.do_openapi(cfg)
        self.assertEqual((out["format"], out["body"]), ("text", "just text"))
        self.assertNotIn("Authorization", seen[-1]["headers"])

    def test_missing_or_bad_description(self):
        with self.assertRaises(http.Fail):
            http.do_openapi(self.cfg())
        with self.assertRaises(http.Fail):
            http.do_openapi(self.cfg(API_OPENAPI_URL="file:///etc/passwd"))


class CliTest(Base):
    def run_cli(self, argv, env):
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                http.main(argv, env)
            except SystemExit as e:
                code = e.code or 0
        return code, json.loads(buf.getvalue())

    def env(self, slug="", **kw):
        base = {"API_BASE_URL": self.url, "API_AUTH": "bearer", "API_TOKEN": SECRET}
        base.update(kw)
        return {(k + "__" + slug if slug else k): v for k, v in base.items()}

    def test_get_info_and_errors(self):
        code, out = self.run_cli(["get", "/echo"], self.env())
        self.assertEqual((code, out["status"]), (0, 200))
        self.assertNotIn(SECRET, json.dumps(out))
        code, out = self.run_cli(["info"], self.env())
        self.assertEqual((code, out["writable"], out["methods"]), (0, False, ["GET", "HEAD", "OPTIONS"]))
        self.assertNotIn(SECRET, json.dumps(out))
        code, out = self.run_cli(["get", "/missing"], self.env())
        self.assertEqual((code, out["ok"]), (1, False))
        code, out = self.run_cli(["get", "http://evil/x"], self.env())
        self.assertEqual(code, 1)
        self.assertIn("путь", out["error"])
        code, out = self.run_cli(["request", "POST", "/x", "--json", "{}"], self.env())
        self.assertEqual(code, 1)
        self.assertIn("только для чтения", out["error"])
        code, out = self.run_cli(["request", "POST", "/x", "--json", "{bad"], self.env(API_ALLOW_WRITE="1"))
        self.assertEqual(code, 1)
        self.assertIn("JSON", out["error"])
        code, out = self.run_cli(["request", "POST", "/x", "--json", "{}", "--json-file", "f"], self.env(API_ALLOW_WRITE="1"))
        self.assertEqual(code, 1)
        code, out = self.run_cli(["request", "DELETE", "/x/1"], self.env(API_ALLOW_WRITE="1"))
        self.assertEqual((code, seen[-1]["method"]), (0, "DELETE"))

    def test_json_file_body(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write('{"from": "file"}')
        try:
            code, _ = self.run_cli(["request", "PUT", "/x", "--json-file", f.name], self.env(API_ALLOW_WRITE="1"))
            self.assertEqual((code, seen[-1]["body"]), (0, '{"from": "file"}'))
            code, out = self.run_cli(["request", "PUT", "/x", "--json-file", f.name + ".nope"], self.env(API_ALLOW_WRITE="1"))
            self.assertEqual(code, 1)
        finally:
            os.unlink(f.name)

    def test_several_connections_need_a_choice(self):
        env = dict(self.env("CRM"), **self.env("SHOP", API_AUTH="none", API_TOKEN=""))
        code, out = self.run_cli(["accounts"], env)
        self.assertEqual(out["accounts"], ["CRM", "SHOP"])
        code, out = self.run_cli(["get", "/echo"], env)
        self.assertEqual(code, 1)
        self.assertIn("--account", out["error"])
        code, out = self.run_cli(["--account", "shop", "get", "/echo"], env)
        self.assertEqual(code, 0)
        self.assertIsNone(out["body"]["auth"], "the SHOP connection has no credentials")
        code, out = self.run_cli(["get", "--account=crm", "/echo"], env)
        self.assertEqual((code, out["body"]["auth"]), (0, "Bearer ***"))  # sent, but shown redacted
        code, out = self.run_cli(["--account", "nope", "get", "/x"], env)
        self.assertEqual(code, 1)
        # one connection: used without being named
        code, out = self.run_cli(["get", "/echo"], self.env("ONLY"))
        self.assertEqual(code, 0)

    def test_openapi_command(self):
        code, out = self.run_cli(["openapi", "--grep", "order"], self.env(API_OPENAPI_URL=self.url + "/openapi.json"))
        self.assertEqual(code, 0)
        self.assertIn("POST /orders", out["operations"])


if __name__ == "__main__":
    unittest.main()
