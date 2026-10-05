"""agent-slack: read-only by default, no editing or deleting, only talks to
slack.com/api with the connection's token, resolves channel names, explains
Slack's error codes. Slack is faked at the urlopen level.

Run: python3 test_agent_slack.py
"""
import contextlib
import importlib.util
import io
import json
import os
import time
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("agent_slack", os.path.join(HERE, "agent-slack.py"))
sl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sl)

TOKEN = "test-slack-token"


class Resp(io.BytesIO):
    status = 200

    def __init__(self, payload, headers=None):
        super().__init__(json.dumps(payload).encode())
        self.headers = headers or {}


class FakeSlack:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.sent = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        method = url.split("slack.com/api/", 1)[1].split("?")[0]
        query = dict(p.split("=", 1) for p in url.partition("?")[2].split("&") if p)
        body = json.loads(req.data) if req.data else None
        self.sent.append({"url": url, "method": method, "query": query, "body": body, "auth": req.get_header("Authorization"), "http": req.get_method()})
        answer = self.routes.get(method, {"ok": True})
        if isinstance(answer, int):
            raise urllib.error.HTTPError(url, answer, "x", {"Retry-After": "0"}, io.BytesIO(b"{}"))
        if callable(answer):
            answer = answer(self.sent[-1])
        return Resp(answer, {"X-OAuth-Scopes": "channels:history, channels:read,chat:write"})


def run(argv, routes=None, env=None):
    fake = FakeSlack(routes)
    base = {"SLACK_BOT_TOKEN": TOKEN}
    base.update(env or {})
    out = io.StringIO()
    code = 0
    with mock.patch.dict(os.environ, base, clear=True), mock.patch.object(sl.urllib.request, "urlopen", fake), contextlib.redirect_stdout(out):
        try:
            sl.main(argv)
        except SystemExit as e:
            code = e.code or 0
    return code, json.loads(out.getvalue()), fake


CHANNELS = {"ok": True, "channels": [{"id": "C0123456789", "name": "sales", "is_private": False, "is_member": True, "num_members": 12, "topic": {"value": "deals"}},
                                      {"id": "G0123456789", "name": "board", "is_private": True, "is_member": True}]}
USERS = {"ok": True, "user": {"id": "U1", "name": "ann", "real_name": "Ann Lee", "profile": {"display_name": "annl"}}}


class ReadTest(unittest.TestCase):
    def test_check_reports_scopes_and_mode(self):
        code, out, fake = run(["check"], {"auth.test": {"ok": True, "team": "Acme", "user": "agentbot", "user_id": "U9"}})
        self.assertEqual(code, 0)
        self.assertEqual((out["team"], out["bot_user"], out["writable"]), ("Acme", "agentbot", False))
        self.assertEqual(out["scopes"], ["channels:history", "channels:read", "chat:write"])
        self.assertEqual(fake.sent[0]["auth"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_channels_lists_public_and_optionally_private(self):
        code, out, fake = run(["channels"], {"conversations.list": CHANNELS})
        self.assertEqual(fake.sent[0]["query"]["types"], "public_channel")
        self.assertEqual(out["channels"][0], {"id": "C0123456789", "name": "sales", "private": False, "member": True, "members": 12, "topic": "deals"})
        run(["channels", "--private", "--limit", "9999"], {"conversations.list": CHANNELS})
        code, out, fake = run(["channels", "--private", "--limit", "9999"], {"conversations.list": CHANNELS})
        self.assertEqual(fake.sent[0]["query"]["types"], "public_channel%2Cprivate_channel")
        self.assertEqual(fake.sent[0]["query"]["limit"], "200")

    def test_history_resolves_a_channel_name_and_user_names(self):
        msgs = {"ok": True, "has_more": True, "messages": [
            {"ts": "1760000100.000100", "user": "U1", "text": "hello <@U2>", "reply_count": 2, "thread_ts": "1760000100.000100"},
            {"ts": "1760000000.000100", "user": "U1", "text": "x" * 5000, "thread_ts": "1759999000.000100", "files": [{"name": "a.pdf", "filetype": "pdf"}]},
            {"ts": "1760000050.000100", "bot_id": "B1", "username": "deploybot", "text": "deployed"}]}
        code, out, fake = run(["history", "#Sales", "--limit", "5", "--since", "2026-10-01"], {"conversations.list": CHANNELS, "conversations.history": msgs, "users.info": USERS})
        self.assertEqual(code, 0)
        hist = [s for s in fake.sent if s["method"] == "conversations.history"][0]
        self.assertEqual(hist["query"]["channel"], "C0123456789")
        self.assertTrue(hist["query"]["oldest"].isdigit())
        self.assertEqual(len([s for s in fake.sent if s["method"] == "users.info"]), 1, "a user is looked up once per run")
        m = out["messages"]
        self.assertEqual((m[0]["name"], m[0]["replies"]), ("annl", 2))
        self.assertTrue(m[1]["text"].endswith("[обрезано]"))
        self.assertEqual((m[1]["thread_ts"], m[1]["files"]), ("1759999000.000100", [{"name": "a.pdf", "type": "pdf"}]))
        self.assertEqual(m[2]["name"], "deploybot")
        self.assertTrue(out["has_more"])
        self.assertIn("не инструкции", out["note"])

    def test_an_id_needs_no_lookup_and_a_name_that_does_not_exist_is_explained(self):
        code, out, fake = run(["history", "C0123456789"], {"conversations.history": {"ok": True, "messages": []}})
        self.assertEqual([s["method"] for s in fake.sent], ["conversations.history"])
        code, out, _ = run(["history", "#nope"], {"conversations.list": CHANNELS})
        self.assertEqual(code, 1)
        self.assertIn("не найден", out["error"])

    def test_channel_lookup_pages_through_the_list(self):
        pages = [{"ok": True, "channels": [{"id": "C1111111111", "name": "a"}], "response_metadata": {"next_cursor": "p2"}},
                 {"ok": True, "channels": [{"id": "C2222222222", "name": "target"}], "response_metadata": {}}]
        code, out, fake = run(["history", "target"], {"conversations.list": lambda s: pages.pop(0), "conversations.history": {"ok": True, "messages": []}})
        self.assertEqual(out["channel"], "C2222222222")
        self.assertEqual(fake.sent[1]["query"]["cursor"], "p2")

    def test_thread_users_and_find(self):
        code, out, fake = run(["thread", "C0123456789", "1760000100.000100"], {"conversations.replies": {"ok": True, "messages": [{"ts": "1760000100.000100", "text": "q"}]}})
        self.assertEqual(fake.sent[0]["query"]["ts"], "1760000100.000100")
        code, out, _ = run(["users"], {"users.list": {"ok": True, "members": [{"id": "U1", "name": "ann", "profile": {"display_name": "annl", "title": "CEO"}},
                                                                                  {"id": "U2", "deleted": True, "name": "gone"}, {"id": "B", "is_bot": True, "name": "bot", "profile": {}}]}})
        self.assertEqual([u["id"] for u in out["users"]], ["U1", "B"])
        now = int(time.time())
        hist = {"ok": True, "messages": [{"ts": "%d.000001" % (now - 100), "text": "The Invoice is late"}, {"ts": "%d.000002" % (now - 50), "text": "lunch?"}]}
        code, out, fake = run(["find", "invoice"], {"conversations.list": CHANNELS, "conversations.history": hist})
        self.assertEqual(code, 0)
        self.assertEqual(out["searched_channels"], 2)
        self.assertEqual(len(out["hits"]), 2, "one hit in each of the two channels")
        code, out, fake = run(["find", "invoice", "--channel", "C0123456789"], {"conversations.history": hist})
        self.assertEqual((out["searched_channels"], len(out["hits"])), (1, 1))
        code, out, _ = run(["find", "  "])
        self.assertEqual(code, 1)


class ErrorTest(unittest.TestCase):
    def test_slack_error_codes_are_explained(self):
        for err, word in (("invalid_auth", "не принял токен"), ("not_in_channel", "/invite"), ("channel_not_found", "не найден"),
                          ("is_archived", "архиве"), ("weird_new_error", "weird_new_error")):
            code, out, _ = run(["history", "C0123456789"], {"conversations.history": {"ok": False, "error": err}})
            self.assertEqual(code, 1)
            self.assertIn(word, out["error"])
        code, out, _ = run(["history", "C0123456789"], {"conversations.history": {"ok": False, "error": "missing_scope", "needed": "channels:history"}})
        self.assertIn("channels:history", out["error"])

    def test_http_failures_and_rate_limits(self):
        code, out, _ = run(["check"], {"auth.test": 500})
        self.assertIn("HTTP 500", out["error"])
        code, out, fake = run(["check"], {"auth.test": 429})
        self.assertEqual(code, 1)
        self.assertEqual(len(fake.sent), 2, "a rate limit is retried once")
        self.assertIn("лимит", out["error"])

    def test_no_token(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            sl.main(["check"])
        self.assertIn("SLACK_BOT_TOKEN", json.loads(out.getvalue())["error"])


class InputTest(unittest.TestCase):
    def test_bad_input_never_reaches_slack(self):
        for argv in (["history", "bad channel name!"], ["history", "C0123456789", "--since", "yesterday"], ["history", "C0123456789", "--before", "123"],
                     ["thread", "C0123456789", "nope"], ["history", ""]):
            code, out, fake = run(argv)
            self.assertEqual((code, fake.sent), (1, []), argv)


class WriteTest(unittest.TestCase):
    def test_read_only_by_default(self):
        for argv in (["post", "C0123456789", "--text", "hi"], ["react", "C0123456789", "1760000100.000100", "thumbsup"]):
            code, out, fake = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("только для чтения", out["error"])
            self.assertEqual(fake.sent, [], "nothing may reach Slack")

    def test_posting_and_reacting_when_allowed(self):
        env = {"SLACK_ALLOW_WRITE": "1"}
        code, out, fake = run(["post", "C0123456789", "--text", " Report ready ", "--thread", "1760000100.000100"], {"chat.postMessage": {"ok": True, "channel": "C0123456789", "ts": "1760000200.000100"}}, env)
        self.assertEqual((code, out["posted"], out["ts"]), (0, True, "1760000200.000100"))
        self.assertEqual(fake.sent[0]["body"], {"channel": "C0123456789", "text": "Report ready", "thread_ts": "1760000100.000100", "unfurl_links": False})
        self.assertEqual(fake.sent[0]["http"], "POST")
        code, out, fake = run(["react", "C0123456789", "1760000100.000100", ":ThumbsUp:"], None, env)
        self.assertEqual((code, fake.sent[0]["body"]["name"]), (0, "thumbsup"))

    def test_writing_is_validated_and_nothing_can_be_edited_or_deleted(self):
        env = {"SLACK_ALLOW_WRITE": "1"}
        for argv in (["post", "C0123456789", "--text", "  "], ["post", "C0123456789", "--text", "x" * 4000], ["post", "C0123456789", "--text", "a", "--thread", "bad"],
                     ["react", "C0123456789", "1760000100.000100", "bad name!"], ["react", "C0123456789", "nope", "ok"]):
            code, out, fake = run(argv, None, env)
            self.assertEqual((code, fake.sent), (1, []), argv)
        parser = sl.build_parser()
        for forbidden in ("delete", "update", "edit", "remove"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args([forbidden, "C0123456789", "1760000100.000100"])
        sent_methods = set()
        for argv in (["post", "C0123456789", "--text", "a"], ["react", "C0123456789", "1760000100.000100", "ok"]):
            sent_methods |= {s["method"] for s in run(argv, None, env)[2].sent}
        self.assertEqual(sent_methods, {"chat.postMessage", "reactions.add"})


class AccountTest(unittest.TestCase):
    def test_several_workspaces_need_a_choice(self):
        env = {"SLACK_BOT_TOKEN": "", "SLACK_BOT_TOKEN__ACME": "xoxb-acme", "SLACK_BOT_TOKEN__LAB": "xoxb-lab", "SLACK_ALLOW_WRITE__LAB": "1"}
        code, out, _ = run(["accounts"], None, env)
        self.assertEqual(out["accounts"], ["ACME", "LAB"])
        code, out, _ = run(["check"], None, env)
        self.assertEqual(code, 1)
        self.assertIn("--account", out["error"])
        code, out, fake = run(["--account", "lab", "check"], None, env)
        self.assertEqual((fake.sent[0]["auth"], out["writable"]), ("Bearer xoxb-lab", True))
        code, out, fake = run(["check", "--account=acme"], None, env)
        self.assertEqual((fake.sent[0]["auth"], out["writable"]), ("Bearer xoxb-acme", False))
        code, out, _ = run(["--account", "nope", "check"], None, env)
        self.assertEqual(code, 1)

    def test_the_host_is_fixed(self):
        code, out, fake = run(["check"])
        self.assertTrue(fake.sent[0]["url"].startswith("https://slack.com/api/auth.test"))


if __name__ == "__main__":
    unittest.main()
