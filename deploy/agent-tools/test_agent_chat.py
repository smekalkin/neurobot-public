import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

spec = importlib.util.spec_from_file_location("agent_chat", os.path.join(os.path.dirname(__file__), "agent-chat.py"))
ac = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ac)

NOW = int(time.time() * 1000)


def msg(seq, chat, text, frm="Иван", ago_min=0, out=False, title=""):
    return {"seq": seq, "ts": NOW - ago_min * 60000, "chat": chat, "chat_type": "chat", "chat_title": title, "from": frm, "text": text, "out": out}


class Tool(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"AGENT_CHAT_DIR": self.d}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def put(self, slug, msgs, meta=None):
        with open(os.path.join(self.d, slug + ".jsonl"), "w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        if meta:
            json.dump(meta, open(os.path.join(self.d, slug + ".meta.json"), "w"))

    def run_cmd(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                ac.main(list(argv))
                code = 0
            except SystemExit as e:
                code = e.code
        return code, json.loads(buf.getvalue())

    def test_new_marks_seen_and_peek_does_not(self):
        self.put("MAX", [msg(1, "5", "привет"), msg(2, "5", "вопрос")])
        code, out = self.run_cmd("new", "--peek")
        self.assertEqual(out["count"], 2)
        code, out = self.run_cmd("new")
        self.assertEqual([m["text"] for m in out["messages"]], ["привет", "вопрос"])
        code, out = self.run_cmd("new")
        self.assertEqual(out["count"], 0)
        self.put("MAX", [msg(1, "5", "привет"), msg(2, "5", "вопрос"), msg(3, "6", "новое")])
        code, out = self.run_cmd("new")
        self.assertEqual([m["seq"] for m in out["messages"]], [3])
        self.assertIn("ВНЕШНИЕ ДАННЫЕ", out["note"])

    def test_chats_read_and_search(self):
        self.put("MAX", [msg(1, "5", "цена?", title="Клиенты", ago_min=10), msg(2, "6", "жалоба на доставку", frm="Пётр", ago_min=5), msg(3, "5", "спасибо", out=True)])
        _, out = self.run_cmd("chats")
        by = {c["chat"]: c for c in out["chats"]}
        self.assertEqual(by["5"]["title"], "Клиенты")
        self.assertEqual(by["5"]["last_from"], "(бот)")
        self.assertEqual(by["6"]["unseen"], 1)
        _, out = self.run_cmd("read", "--chat", "5")
        self.assertEqual(len(out["messages"]), 2)
        _, out = self.run_cmd("search", "доставк")
        self.assertEqual(out["count"], 1)
        _, out = self.run_cmd("search", "цена", "--days", "0")
        self.assertEqual(out["count"], 0)

    def test_several_channels_need_a_choice_and_accounts_lists_them(self):
        self.put("A", [msg(1, "1", "a")], {"name": "Клиенты", "platform": "max", "mode": "observe"})
        self.put("B", [msg(1, "1", "b")])
        code, out = self.run_cmd("chats")
        self.assertEqual(code, 1)
        self.assertIn("A, B", out["error"])
        _, out = self.run_cmd("accounts")
        self.assertEqual([a["account"] for a in out["accounts"]], ["A", "B"])
        self.assertEqual(out["accounts"][0]["name"], "Клиенты")
        code, out = self.run_cmd("chats", "--account", "b")
        self.assertEqual(code, 0)
        code, out = self.run_cmd("chats", "--account", "zzz")
        self.assertEqual(code, 1)

    def test_account_may_come_before_the_command(self):
        self.put("A", [msg(1, "1", "a")])
        self.put("B", [msg(1, "1", "b")])
        code, out = self.run_cmd("--account", "b", "chats")
        self.assertEqual((code, out["account"]), (0, "B"))
        code, out = self.run_cmd("chats", "--account", "a")
        self.assertEqual((code, out["account"]), (0, "A"))

    def test_view_exposes_the_message_id_for_replying(self):
        self.put("MAX", [msg(1, "5", "вопрос")])
        _, out = self.run_cmd("new", "--peek")
        self.assertIn("mid", out["messages"][0])

    def test_send_with_reply_to_links_the_message(self):
        self.put("MAX", [msg(1, "5", "x")])
        calls = []

        def fake(req, timeout=None):
            calls.append((req.get_method(), req.full_url, json.loads(req.data)))
            return io.BytesIO(b"{}")

        with mock.patch.dict(os.environ, {"MAX_BOT_TOKEN__MAX": "tok"}), mock.patch.object(ac.urllib.request, "urlopen", fake):
            code, out = self.run_cmd("send", "--chat", "5", "--text", "ответ", "--reply-to", "mid.abc")
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][2]["link"], {"type": "reply", "mid": "mid.abc"})
        self.assertEqual(out["reply_to"], "mid.abc")
        # without --reply-to, no link is sent at all
        with mock.patch.dict(os.environ, {"MAX_BOT_TOKEN__MAX": "tok"}), mock.patch.object(ac.urllib.request, "urlopen", fake):
            self.run_cmd("send", "--chat", "5", "--text", "ответ")
        self.assertNotIn("link", calls[1][2])

    def test_send_needs_the_token_which_only_replying_channels_have(self):
        self.put("MAX", [msg(1, "5", "x")])
        code, out = self.run_cmd("send", "--chat", "5", "--text", "ответ")
        self.assertEqual(code, 1)
        self.assertIn("только для наблюдения", out["error"])
        calls = []

        def fake(req, timeout=None):
            calls.append((req.get_method(), req.full_url, req.get_header("Authorization"), json.loads(req.data)))
            return io.BytesIO(b"{}")

        with mock.patch.dict(os.environ, {"MAX_BOT_TOKEN__MAX": "tok"}), mock.patch.object(ac.urllib.request, "urlopen", fake):
            code, out = self.run_cmd("send", "--chat", "5", "--text", "ответ")
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("POST", "https://platform-api.max.ru/messages?chat_id=5", "tok", {"text": "ответ"})])

    def test_telegram_send_uses_bot_api_and_numeric_reply(self):
        self.put("TG", [msg(1, "-1005", "x")], {"platform": "telegram", "mode": "reply"})
        calls = []

        def fake(req, timeout=None):
            calls.append((req.get_method(), req.full_url, json.loads(req.data)))
            return io.BytesIO(b'{"ok":true}')

        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN__TG": "123:abc"}), mock.patch.object(ac.urllib.request, "urlopen", fake):
            code, out = self.run_cmd("send", "--account", "tg", "--chat", "-1005", "--text", "ответ", "--reply-to", "42")
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ("POST", "https://api.telegram.org/bot123:abc/sendMessage", {
            "chat_id": "-1005", "text": "ответ", "reply_parameters": {"message_id": 42}
        }))


if __name__ == "__main__":
    unittest.main()
