"""HubSpot and Slack catalog entries: the specs match their tools and the check
scripts report what they should (the services are faked at the urlopen level).
Run: python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import runpy
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTEGRATIONS = os.path.join(ROOT, "deploy", "integrations")
TOOLS = os.path.join(ROOT, "deploy", "agent-tools")


def spec(name):
    with open(os.path.join(INTEGRATIONS, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def tool_source(name):
    with open(os.path.join(TOOLS, name), encoding="utf-8") as f:
        return f.read()


class Resp(io.BytesIO):
    status = 200

    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers or {}


def run_check(script, env, urlopen):
    out = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), mock.patch("urllib.request.urlopen", urlopen), contextlib.redirect_stdout(out):
        try:
            runpy.run_path(os.path.join(INTEGRATIONS, script), run_name="__main__")
        except SystemExit:
            pass
    return json.loads(out.getvalue().strip().splitlines()[-1])


def hubspot_api(denied=(), fail_auth=False):
    def urlopen(req, timeout=None):
        path = req.full_url.split("hubapi.com", 1)[1].split("?")[0]
        if fail_auth or any(path.endswith("/" + d) for d in denied):
            raise urllib.error.HTTPError(req.full_url, 401 if fail_auth else 403, "x", {}, io.BytesIO(b'{"message":"no"}'))
        if path.startswith("/account-info"):
            return Resp({"portalId": 4242})
        return Resp({"results": []})
    return urlopen


def slack_api(scopes="channels:read,channels:history,users:read", member=True, ok=True):
    def urlopen(req, timeout=None):
        method = req.full_url.split("slack.com/api/", 1)[1].split("?")[0]
        if method == "auth.test":
            if not ok:
                return Resp({"ok": False, "error": "invalid_auth"})
            return Resp({"ok": True, "team": "Acme", "user": "agentbot"}, {"X-OAuth-Scopes": scopes})
        return Resp({"ok": True, "channels": [{"id": "C1", "name": "a", "is_member": member}]})
    return urlopen


class HubspotCheckTest(unittest.TestCase):
    def check(self, urlopen, **env):
        base = {"HUBSPOT_TOKEN": "pat-secret-token", "AGENT_HUBSPOT_TOOL": os.path.join(TOOLS, "agent-hubspot.py")}
        base.update(env)
        return run_check("hubspot.check.py", base, urlopen)

    def test_a_working_token(self):
        r = self.check(hubspot_api())
        self.assertEqual(r["status"], "ok", r)
        text = json.dumps(r, ensure_ascii=False)
        self.assertIn("4242", text)
        self.assertIn("только чтение", text)
        self.assertNotIn("pat-secret-token", text)
        self.assertIn("чтение, создание и изменение", json.dumps(self.check(hubspot_api(), HUBSPOT_ALLOW_WRITE="1"), ensure_ascii=False))

    def test_missing_rights_are_a_warning_and_a_bad_token_a_failure(self):
        r = self.check(hubspot_api(denied=("deals",)))
        self.assertEqual(r["status"], "warn")
        self.assertIn("сделки недоступны", json.dumps(r, ensure_ascii=False))
        r = self.check(hubspot_api(fail_auth=True))
        self.assertEqual(r["status"], "fail")
        self.assertIn("не принял токен", json.dumps(r, ensure_ascii=False))

    def test_unconfigured_and_missing_tool(self):
        self.assertEqual(run_check("hubspot.check.py", {}, hubspot_api())["status"], "unconfigured")
        r = run_check("hubspot.check.py", {"HUBSPOT_TOKEN": "t", "AGENT_HUBSPOT_TOOL": "/nonexistent/x"}, hubspot_api())
        self.assertEqual(r["status"], "fail")


class SlackCheckTest(unittest.TestCase):
    def check(self, urlopen, **env):
        base = {"SLACK_BOT_TOKEN": "test-slack-token", "AGENT_SLACK_TOOL": os.path.join(TOOLS, "agent-slack.py")}
        base.update(env)
        return run_check("slack.check.py", base, urlopen)

    def test_a_working_bot(self):
        r = self.check(slack_api())
        self.assertEqual(r["status"], "ok", r)
        text = json.dumps(r, ensure_ascii=False)
        self.assertIn("Acme", text)
        self.assertIn("каналах: 1", text)
        self.assertNotIn("test-slack-token", text)

    def test_what_is_missing_is_named(self):
        r = self.check(slack_api(scopes="users:read"))
        self.assertEqual(r["status"], "warn")
        self.assertIn("channels:history", json.dumps(r, ensure_ascii=False))
        r = self.check(slack_api(member=False))
        self.assertEqual(r["status"], "warn")
        self.assertIn("/invite", json.dumps(r, ensure_ascii=False))
        r = self.check(slack_api(), SLACK_ALLOW_WRITE="1")
        self.assertEqual(r["status"], "warn")
        self.assertIn("chat:write", json.dumps(r, ensure_ascii=False))
        r = self.check(slack_api(scopes="channels:read,channels:history,chat:write"), SLACK_ALLOW_WRITE="1")
        self.assertEqual(r["status"], "ok")

    def test_a_bad_token_and_unconfigured(self):
        r = self.check(slack_api(ok=False))
        self.assertEqual(r["status"], "fail")
        self.assertIn("не принял токен", json.dumps(r, ensure_ascii=False))
        self.assertEqual(run_check("slack.check.py", {}, slack_api())["status"], "unconfigured")


class SpecsTest(unittest.TestCase):
    def test_specs_match_their_tools(self):
        for sid, tool, token, write_env in (("hubspot", "agent-hubspot.py", "HUBSPOT_TOKEN", "HUBSPOT_ALLOW_WRITE"),
                                            ("slack", "agent-slack.py", "SLACK_BOT_TOKEN", "SLACK_ALLOW_WRITE")):
            s = spec(sid)
            src = tool_source(tool)
            self.assertEqual(s["tool"], {"file": "deploy/agent-tools/" + tool, "dest": "/usr/local/bin/" + tool[:-3]})
            self.assertEqual(s["write_env"], write_env)
            self.assertIn('"%s"' % token, src)
            self.assertIn('"%s"' % write_env, src)
            field = next(f for f in s["fields"] if f["key"] == token)
            self.assertTrue(field["secret"] and field["required"])
            self.assertEqual(s["configured_when"], [token])
            self.assertTrue(s["write_supported"])
            self.assertEqual(s["icon_asset"], "assets/brand/%s.png" % sid)


if __name__ == "__main__":
    unittest.main()
