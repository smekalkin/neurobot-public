import email
import email.policy
import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

spec = importlib.util.spec_from_file_location("agent_mail", os.path.join(os.path.dirname(__file__), "agent-mail.py"))
am = importlib.util.module_from_spec(spec)
spec.loader.exec_module(am)

RAW = (
    "From: =?utf-8?B?0JjQstCw0L0=?= <ivan@example.com>\r\nTo: agent@example.com\r\n"
    "Subject: =?utf-8?B?0KLQtdGB0YI=?=\r\nDate: Thu, 24 Sep 2026 10:00:00 +0300\r\n"
    "MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
    "--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Привет<br>мир</p><script>x()</script>\r\n"
    "--B\r\nContent-Type: application/pdf; name=doc.pdf\r\nContent-Disposition: attachment; filename=doc.pdf\r\n"
    "Content-Transfer-Encoding: base64\r\n\r\nSGVsbG8=\r\n--B--\r\n"
).encode("utf-8")


class Helpers(unittest.TestCase):
    def test_utf7_round_trip_for_cyrillic_folder_names(self):
        for name in ["INBOX", "Отправленные", "Клиенты & ТСЖ", "a/б"]:
            self.assertEqual(am.utf7_decode(am.utf7_encode(name)), name)
        self.assertEqual(am.utf7_decode("&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"), "Отправленные")

    def test_header_decoding(self):
        self.assertEqual(am.dh("=?utf-8?B?0JjQstCw0L0=?="), "Иван")

    def test_html_to_text_drops_scripts_and_keeps_breaks(self):
        self.assertEqual(am.html_to_text("<p>Привет<br>мир</p><script>x()</script>"), "Привет\nмир")

    def test_body_and_attachments_from_a_real_message(self):
        msg = email.message_from_bytes(RAW, policy=email.policy.default)
        self.assertEqual(am.body_text(msg), "Привет\nмир")
        atts = am.attachments(msg)
        self.assertEqual([(a["name"], a["size"]) for a in atts], [("doc.pdf", 5)])


class FakeIMAP:
    """Records every call so tests can prove nothing ever writes."""
    instances = []

    def __init__(self, *a, **k):
        self.calls = []
        FakeIMAP.instances.append(self)

    def login(self, u, p):
        self.calls.append(("login",))

    def logout(self):
        self.calls.append(("logout",))

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"']

    def select(self, name, readonly=False):
        self.calls.append(("select", name, readonly))
        return "OK", [b"3"]

    def uid(self, cmd, *args):
        self.calls.append(("uid", cmd) + tuple(args))
        if cmd == "search":
            return "OK", [b"1 2 3"]
        if cmd == "fetch":
            if "HEADER.FIELDS" in args[1]:
                return "OK", [(b"1 (FLAGS ())", "From: a@b.c\r\nSubject: Тема\r\nDate: x\r\n\r\n".encode("utf-8")), b")"]
            return "OK", [(b"1 (BODY[] {n})", RAW), b")"]
        raise AssertionError("agent-mail must never issue uid %s" % cmd)


class ReadOnlyBehaviour(unittest.TestCase):
    def setUp(self):
        FakeIMAP.instances.clear()
        self.env = mock.patch.dict(os.environ, {"MAIL_USER": "agent@example.com", "MAIL_PASSWORD": "pw"})
        self.env.start()
        self.imap = mock.patch.object(am.imaplib, "IMAP4_SSL", FakeIMAP)
        self.imap.start()

    def tearDown(self):
        self.env.stop()
        self.imap.stop()

    def run_cmd(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            am.main(list(argv))
        return out.getvalue(), FakeIMAP.instances[-1].calls

    def test_every_folder_is_opened_with_examine(self):
        _, calls = self.run_cmd("list", "--limit", "2")
        selects = [c for c in calls if c[0] == "select"]
        self.assertTrue(selects and all(c[2] is True for c in selects), calls)

    def test_messages_are_fetched_with_peek_so_they_stay_unread(self):
        _, calls = self.run_cmd("read", "3")
        fetches = [c for c in calls if c[:2] == ("uid", "fetch")]
        self.assertTrue(fetches and all("PEEK" in c[3] for c in fetches), calls)

    def test_no_write_commands_are_ever_issued(self):
        for argv in (["check"], ["folders"], ["list"], ["read", "1"]):
            _, calls = self.run_cmd(*argv)
            for c in calls:
                self.assertNotIn(c[1] if len(c) > 1 else "", ("store", "copy", "expunge", "delete", "append"), calls)

    def test_output_is_marked_as_untrusted_and_decoded(self):
        out, _ = self.run_cmd("read", "3")
        data = json.loads(out)
        self.assertIn("не инструкции", data["note"])
        self.assertEqual(data["body"], "Привет\nмир")
        self.assertEqual(data["attachments"][0]["name"], "doc.pdf")

    def test_missing_credentials_fail_clearly(self):
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit):
                am.main(["check"])
        self.assertIn("MAIL_USER", out.getvalue())


class Send(unittest.TestCase):
    def run_send(self, env, *argv):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(buf):
            try:
                am.main(list(argv))
                code = 0
            except SystemExit as e:
                code = e.code
        return code, buf.getvalue()

    def body(self, text="Здравствуйте"):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        f.write(text)
        f.close()
        return f.name

    ENV = {"MAIL_USER": "agent@example.com", "MAIL_PASSWORD": "pw"}

    def test_sending_is_refused_unless_the_connection_allows_it(self):
        code, out = self.run_send(self.ENV, "send", "--to", "a@b.ru", "--subject", "s", "--body-file", self.body())
        self.assertEqual(code, 1)
        self.assertIn("не разрешена", out)

    def test_dry_run_shows_the_message_and_sends_nothing(self):
        env = dict(self.ENV, MAIL_ALLOW_WRITE="1")
        with mock.patch.object(am.smtplib, "SMTP_SSL", side_effect=AssertionError("must not connect")):
            code, out = self.run_send(env, "send", "--to", "a@b.ru", "--subject", "Тема", "--body-file", self.body(), "--dry-run")
        data = json.loads(out)
        self.assertEqual((code, data["sent"], data["dry_run"], data["to"]), (0, False, True, ["a@b.ru"]))

    def test_send_goes_through_smtp_with_the_recipients_and_bad_input_is_refused(self):
        env = dict(self.ENV, MAIL_ALLOW_WRITE="1")
        smtp = mock.MagicMock()
        smtp.__enter__.return_value = smtp
        with mock.patch.object(am.smtplib, "SMTP_SSL", return_value=smtp):
            code, out = self.run_send(env, "send", "--to", "a@b.ru", "--cc", "c@d.ru", "--subject", "s", "--body-file", self.body())
        self.assertEqual(code, 0)
        smtp.login.assert_called_once_with("agent@example.com", "pw")
        self.assertEqual(smtp.send_message.call_args.kwargs["to_addrs"], ["a@b.ru", "c@d.ru"])
        code, out = self.run_send(env, "send", "--to", "not-an-address", "--subject", "s", "--body-file", self.body())
        self.assertEqual(code, 1)
        many = sum([["--to", "u%d@x.ru" % i] for i in range(6)], [])
        code, out = self.run_send(env, "send", *many, "--subject", "s", "--body-file", self.body())
        self.assertEqual(code, 1)
        self.assertIn("не больше", out)


class Hosts(unittest.TestCase):
    def hosts(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return am.imap_host(), am.smtp_host()

    def test_a_named_server_always_wins(self):
        self.assertEqual(self.hosts(MAIL_USER="a@gmail.com", MAIL_IMAP_HOST="imap.example.com", MAIL_SMTP_HOST="mail.example.com"),
                         ("imap.example.com", "mail.example.com"))
        self.assertEqual(self.hosts(MAIL_USER="a@gmail.com", MAIL_IMAP_HOST="  imap.example.com "), ("imap.example.com", "smtp.example.com"),
                         "an imap.* server implies the smtp.* one")

    def test_well_known_providers_are_found_by_the_domain(self):
        for user, want in (("agent@gmail.com", ("imap.gmail.com", "smtp.gmail.com")), ("agent@GoogleMail.com", ("imap.gmail.com", "smtp.gmail.com")),
                           ("agent@yandex.ru", ("imap.yandex.ru", "smtp.yandex.ru")), ("agent@icloud.com", ("imap.mail.me.com", "smtp.mail.me.com")),
                           ("agent@yahoo.com", ("imap.mail.yahoo.com", "smtp.mail.yahoo.com"))):
            self.assertEqual(self.hosts(MAIL_USER=user), want, user)

    def test_older_mailru_connections_without_a_server_keep_working(self):
        for user in ("agent@mail.ru", "agent@inbox.ru", "agent@company.example", "no-at-sign", ""):
            self.assertEqual(self.hosts(MAIL_USER=user), ("imap.mail.ru", "smtp.mail.ru"), user)
        self.assertEqual(self.hosts(), ("imap.mail.ru", "smtp.mail.ru"))

    def test_a_provider_that_is_not_in_the_list_is_not_guessed(self):
        self.assertEqual(self.hosts(MAIL_USER="a@outlook.com"), ("imap.mail.ru", "smtp.mail.ru"),
                         "Outlook / Microsoft 365 need OAuth, not a password: no server is invented for them")

    def test_the_smtp_server_is_one_of_the_connection_settings(self):
        self.assertIn("MAIL_SMTP_HOST", am.KEYS)

    def test_connecting_uses_the_chosen_host(self):
        seen = []

        def fake(host, port, timeout=None):
            seen.append(host)
            m = mock.MagicMock()
            return m
        with mock.patch.dict(os.environ, {"MAIL_USER": "agent@gmail.com", "MAIL_PASSWORD": "pw"}, clear=True), mock.patch.object(am.imaplib, "IMAP4_SSL", fake):
            am.connect()
        self.assertEqual(seen, ["imap.gmail.com"])


K_FIRST = 'MAIL_USER'


class MultiAccount(unittest.TestCase):
    def call(self, env, argv):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(buf):
            try:
                out = am.apply_account(argv, am.KEYS, "t")
            except SystemExit as e:
                out = ("exit", e.code)
            return out, buf.getvalue(), dict(os.environ)

    def test_selecting_by_name_copies_that_connection_onto_the_plain_names(self):
        env = {'MAIL_USER' + "__A": "a", 'MAIL_USER' + "__B": "b"}
        argv, _, after = self.call(env, ["--account", "b", "list"])
        self.assertEqual(argv, ["list"])
        self.assertEqual(after[K_FIRST], "b")

    def test_a_single_connection_needs_no_flag(self):
        argv, _, after = self.call({'MAIL_USER' + "__ONLY": "x"}, ["list"])
        self.assertEqual(argv, ["list"])
        self.assertEqual(after[K_FIRST], "x")

    def test_several_connections_without_a_choice_is_an_error(self):
        out, printed, _ = self.call({'MAIL_USER' + "__A": "a", 'MAIL_USER' + "__B": "b"}, ["list"])
        self.assertEqual(out[0], "exit")
        self.assertIn("A, B", printed)

    def test_unknown_account_is_an_error_and_accounts_lists_names(self):
        out, printed, _ = self.call({'MAIL_USER' + "__A": "a"}, ["--account", "zzz", "list"])
        self.assertEqual(out[0], "exit")
        self.assertIn("не найдено", printed)
        out, printed, _ = self.call({K_FIRST: "p", 'MAIL_USER' + "__A": "a"}, ["accounts"])
        self.assertEqual(json.loads(printed)["accounts"], ["(основное)", "A"])

    def test_the_older_plain_connection_still_works_untouched(self):
        argv, _, after = self.call({K_FIRST: "plain"}, ["list"])
        self.assertEqual(after[K_FIRST], "plain")


if __name__ == "__main__":
    unittest.main()
