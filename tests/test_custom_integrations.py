"""The generic integrations (custom REST API, MCP server, inbound webhook): the
catalog entries are well formed and the check scripts report what they should
against a real local server. Run: python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTEGRATIONS = os.path.join(ROOT, "deploy", "integrations")
TOOL = os.path.join(ROOT, "deploy", "agent-tools", "agent-http.py")


class Api(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        code, body = 200, b"{}"
        if p == "/denied/":
            code = 403
        elif p == "/broken/":
            code = 502
        elif p == "/gone/":
            code = 404
        elif p == "/spec.json":
            body = json.dumps({"info": {"title": "T"}, "paths": {"/a": {"get": {"summary": "A"}}, "/b": {"post": {"summary": "B"}}}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_HEAD = do_GET


def run_check(name, env):
    full = dict(os.environ, **env)
    out = subprocess.run([sys.executable, os.path.join(INTEGRATIONS, name)], env=full, capture_output=True, text=True, timeout=60)
    return json.loads(out.stdout.strip().splitlines()[-1])


class CustomApiCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Api)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def check(self, **env):
        base = {"AGENT_HTTP_TOOL": TOOL, "API_AUTH": "bearer", "API_TOKEN": "tok-123456"}
        base.update(env)
        return run_check("custom-api.check.py", base)

    def test_unconfigured_and_missing_tool(self):
        self.assertEqual(run_check("custom-api.check.py", {"API_BASE_URL": ""})["status"], "unconfigured")
        r = run_check("custom-api.check.py", {"API_BASE_URL": self.base, "AGENT_HTTP_TOOL": "/nonexistent/agent-http"})
        self.assertEqual(r["status"], "fail")

    def test_a_working_api(self):
        r = self.check(API_BASE_URL=self.base + "/ok", API_OPENAPI_URL=self.base + "/spec.json")
        self.assertEqual(r["status"], "ok", r)
        text = json.dumps(r, ensure_ascii=False)
        self.assertIn("только чтение", text)
        self.assertIn("2 методов", text)
        self.assertNotIn("tok-123456", text)
        self.assertIn("Адрес без шифрования", text)  # plain http is flagged

    def test_the_servers_answers_are_told_apart(self):
        self.assertEqual(self.check(API_BASE_URL=self.base + "/denied")["status"], "fail")
        self.assertEqual(self.check(API_BASE_URL=self.base + "/broken")["status"], "warn")
        gone = self.check(API_BASE_URL=self.base + "/gone")
        self.assertEqual(gone["status"], "ok", gone)

    def test_bad_settings_and_dead_servers(self):
        self.assertEqual(self.check(API_BASE_URL="ftp://example.com")["status"], "fail")
        self.assertEqual(self.check(API_BASE_URL=self.base, API_TOKEN="")["status"], "fail")
        self.assertEqual(self.check(API_BASE_URL="http://127.0.0.1:1")["status"], "fail")
        self.assertEqual(self.check(API_BASE_URL=self.base, API_OPENAPI_URL="file:///etc/passwd")["status"], "warn")

    def test_write_mode_is_reported(self):
        r = self.check(API_BASE_URL=self.base + "/ok", API_ALLOW_WRITE="1")
        self.assertIn("чтение и изменение", json.dumps(r, ensure_ascii=False))


class SpecsTest(unittest.TestCase):
    def test_the_api_spec_matches_what_the_tool_reads(self):
        with open(os.path.join(INTEGRATIONS, "custom-api.json"), encoding="utf-8") as f:
            spec = json.load(f)
        keys = {f["key"] for f in spec["fields"]}
        with open(TOOL, encoding="utf-8") as f:
            src = f.read()
        for k in keys | {spec["write_env"]}:
            self.assertIn('"%s"' % k, src, k)
        self.assertEqual(spec["tool"]["dest"], "/usr/local/bin/agent-http")
        auth = next(f for f in spec["fields"] if f["key"] == "API_AUTH")
        self.assertEqual({o["value"] for o in auth["options"]}, {"bearer", "header", "basic", "query", "none"})
        self.assertNotIn("default", auth, "a default would not be stored, and the tool would read it as no sign-in")
        secret = {f["key"] for f in spec["fields"] if f["secret"]}
        self.assertTrue({"API_TOKEN", "API_HEADERS"} <= secret)


class FakeMcp(BaseHTTPRequestHandler):
    """A tiny MCP server over HTTPS: initialize, initialized, tools/list."""
    sse = False
    tools = [{"name": "search"}, {"name": "get_item"}]
    log_message = lambda *a: None

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer good-token":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if "id" not in msg:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if msg["method"] == "initialize":
            result = {"serverInfo": {"name": "demo-mcp", "version": "1.2"}, "protocolVersion": "2025-03-26", "capabilities": {}}
        elif msg["method"] == "tools/list":
            assert self.headers.get("Mcp-Session-Id") == "sess-1", "the session id must be sent back"
            result = {"tools": self.tools}
        else:
            result = {}
        payload = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        if self.sse and msg["method"] == "tools/list":
            body, ctype = ("event: message\ndata: %s\n\n" % payload).encode(), "text/event-stream"
        else:
            body, ctype = payload.encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Mcp-Session-Id", "sess-1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        body = b"event: endpoint\ndata: /messages\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if self.path.startswith("/sse") else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl command to make a test certificate")
class McpCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cert, key = os.path.join(cls.tmp, "c.pem"), os.path.join(cls.tmp, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "2",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, capture_output=True)
        cls.cert = cert
        cls.srv = HTTPServer(("127.0.0.1", 0), FakeMcp)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        cls.srv.socket = ctx.wrap_socket(cls.srv.socket, server_side=True)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = "https://127.0.0.1:%d/mcp" % cls.srv.server_port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def check(self, **env):
        base = {"MCP_URL": self.url, "MCP_TOKEN": "good-token", "SSL_CERT_FILE": self.cert}
        base.update(env)
        return run_check("custom-mcp.check.py", base)

    def test_a_working_server(self):
        r = self.check()
        self.assertEqual(r["status"], "ok", r)
        text = json.dumps(r, ensure_ascii=False)
        self.assertIn("demo-mcp 1.2", text)
        self.assertIn("Инструментов: 2", text)
        self.assertNotIn("good-token", text)

    def test_tools_listed_as_an_event_stream_are_understood(self):
        FakeMcp.sse = True
        try:
            self.assertIn("Инструментов: 2", json.dumps(self.check(), ensure_ascii=False))
        finally:
            FakeMcp.sse = False

    def test_no_tools_is_a_warning_and_a_wrong_token_a_failure(self):
        FakeMcp.tools = []
        try:
            self.assertEqual(self.check()["status"], "warn")
        finally:
            FakeMcp.tools = [{"name": "search"}, {"name": "get_item"}]
        bad = self.check(MCP_TOKEN="nope")
        self.assertEqual(bad["status"], "fail")
        self.assertIn("токен не подходит", json.dumps(bad, ensure_ascii=False))

    def test_unconfigured_plain_http_and_unreachable(self):
        self.assertEqual(run_check("custom-mcp.check.py", {"MCP_URL": ""})["status"], "unconfigured")
        self.assertEqual(self.check(MCP_URL="http://example.com/mcp")["status"], "fail")
        self.assertEqual(self.check(MCP_URL="https://127.0.0.1:1/mcp")["status"], "fail")

    def test_the_legacy_sse_transport(self):
        base = self.url.rsplit("/", 1)[0]
        self.assertEqual(self.check(MCP_TRANSPORT="sse", MCP_URL=base + "/sse")["status"], "ok")
        self.assertEqual(self.check(MCP_TRANSPORT="sse", MCP_URL=base + "/other")["status"], "warn")

    def test_a_bare_key_header_without_prefix(self):
        # the server above wants "Authorization: Bearer good-token", so a raw key must be refused
        r = self.check(MCP_AUTH_HEADER="X-API-Key", MCP_AUTH_SCHEME="-")
        self.assertEqual(r["status"], "fail")


class WebhookSpecTest(unittest.TestCase):
    def test_the_webhook_entry_is_a_receiver_without_a_way_to_answer(self):
        with open(os.path.join(INTEGRATIONS, "custom-webhook.json"), encoding="utf-8") as f:
            spec = json.load(f)
        self.assertEqual(spec["listener"], "webhook")
        self.assertFalse(spec.get("write_supported"))
        self.assertEqual(spec["tool"]["dest"], "/usr/local/bin/agent-chat")
        self.assertTrue(all(f.get("agentdesk_only") for f in spec["fields"]), "nothing of it may reach an agent's server")

    def test_the_mcp_entry_names_real_fields(self):
        with open(os.path.join(INTEGRATIONS, "custom-mcp.json"), encoding="utf-8") as f:
            spec = json.load(f)
        keys = {f["key"] for f in spec["fields"]}
        self.assertTrue(set(spec["mcp"].values()) <= keys)
        self.assertTrue(next(f for f in spec["fields"] if f["key"] == "MCP_URL")["secret"])


if __name__ == "__main__":
    unittest.main()
