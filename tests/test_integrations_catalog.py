import json
import re
import sys
import unittest
from pathlib import Path

CATALOG = Path(__file__).resolve().parent.parent / "deploy" / "integrations"
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")


class CatalogTests(unittest.TestCase):
    def specs(self):
        return [(p, json.loads(p.read_text(encoding="utf-8"))) for p in sorted(CATALOG.glob("*.json"))]

    def test_every_spec_is_valid_and_its_files_exist(self):
        found = self.specs()
        self.assertTrue(found, "catalog is empty")
        ids = set()
        for path, spec in found:
            self.assertRegex(spec["id"], ID_RE, path.name)
            self.assertNotIn(spec["id"], ids, "duplicate id")
            ids.add(spec["id"])
            self.assertEqual(path.stem, spec["id"], "file name must match the id")
            self.assertTrue(spec["title"].strip() and spec["fields"], path.name)
            self.assertRegex(spec.get("color", "#000000"), r"^#[0-9A-Fa-f]{6}$", path.name)
            self.assertLessEqual(len(spec.get("icon", "")), 24, path.name)
            keys = {f["key"] for f in spec["fields"]}
            for f in spec["fields"]:
                self.assertRegex(f["key"], KEY_RE, path.name)
                for o in f.get("options", []):
                    self.assertTrue(o["value"] != "" and o["label"], path.name)
                if f.get("options") and f.get("default"):
                    self.assertIn(f["default"], [o["value"] for o in f["options"]], path.name)
                self.assertTrue(f["label"].strip(), path.name)
            if spec.get("marker_env"):
                self.assertRegex(spec["marker_env"], KEY_RE, path.name)
                keys.add(spec["marker_env"])
            for k in spec.get("configured_when", []):
                self.assertIn(k, keys, "%s: configured_when names an unknown field" % path.name)
            for alt in spec.get("configured_when_any", []):
                self.assertTrue(alt, path.name)
                for k in alt:
                    self.assertIn(k, keys, "%s: configured_when_any names an unknown field" % path.name)
            fields = {f["key"]: f for f in spec["fields"]}
            for f in spec["fields"]:
                for k, v in (f.get("show_when") or {}).items():
                    self.assertIn(k, fields, "%s: show_when names an unknown field" % path.name)
                    self.assertTrue(fields[k].get("options"), "%s: show_when must depend on a choice" % path.name)
                    self.assertIn(v, [o["value"] for o in fields[k]["options"]], "%s: show_when names an unknown choice" % path.name)
            for a in spec.get("aliases", []):
                self.assertRegex(a["id"], ID_RE, path.name)
                self.assertFalse((CATALOG / (a["id"] + ".json")).exists(), "%s: an alias must not also be a spec" % path.name)
                self.assertTrue(set(a.get("values", {})) <= set(fields), path.name)
            if spec.get("write_supported"):
                self.assertTrue(spec.get("readonly_file") and spec.get("write_file"), "%s: write needs both rule files" % path.name)
            if spec.get("write_env"):
                self.assertRegex(spec["write_env"], KEY_RE, path.name)
            for name in (spec.get("instructions_file"), spec.get("check_file"), spec.get("readonly_file"), spec.get("write_file")):
                if name:
                    self.assertTrue((CATALOG / name).is_file(), "%s: missing %s" % (path.name, name))
            tool = spec.get("tool")
            if tool:
                self.assertTrue((CATALOG.parent.parent / tool["file"]).is_file(), "%s: missing tool file" % path.name)
                self.assertTrue(tool["dest"].startswith("/usr/local/bin/"))

    def test_check_scripts_compile_and_report_unconfigured_without_secrets(self):
        import subprocess
        for path, spec in self.specs():
            name = spec.get("check_file")
            if not name:
                continue
            code = (CATALOG / name).read_text(encoding="utf-8")
            compile(code, name, "exec")
            out = subprocess.run([sys.executable, str(CATALOG / name)], capture_output=True, text=True,
                                 env={"PATH": "/usr/bin:/bin"}, timeout=30)
            data = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertEqual(data["status"], "unconfigured", name)

    def test_portal_checks_refuse_addresses_that_are_not_http(self):
        import subprocess
        cases = {
            "webhook": {"BITRIX_WEBHOOK_URL": "file:///etc/passwd"},
            "login": {"B24_URL": "ftp://example.com", "B24_LOGIN": "u", "B24_PASSWORD": "p"},
        }
        for name, env in cases.items():
            out = subprocess.run([sys.executable, str(CATALOG / "bitrix24.check.py")], capture_output=True, text=True,
                                 env=dict(env, PATH="/usr/bin:/bin"), timeout=30)
            data = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertEqual(data["status"], "fail", name)
            self.assertIn("http", data["summary"], name)


class BitrixCardTest(unittest.TestCase):
    """One card for both ways into Bitrix24."""

    def spec(self):
        return json.loads((CATALOG / "bitrix24.json").read_text(encoding="utf-8"))

    def test_the_card_covers_the_webhook_and_the_login_way(self):
        spec = self.spec()
        fields = {f["key"]: f for f in spec["fields"]}
        method = fields["BITRIX_AUTH"]
        self.assertTrue(method["agentdesk_only"], "which way was chosen is the panel's business: nothing of it goes to a server")
        self.assertEqual([o["value"] for o in method["options"]], ["webhook", "login"])
        self.assertEqual(method["default"], "webhook")
        self.assertEqual(fields["BITRIX_WEBHOOK_URL"]["show_when"], {"BITRIX_AUTH": "webhook"})
        for k in ("B24_URL", "B24_LOGIN", "B24_PASSWORD"):
            self.assertEqual(fields[k]["show_when"], {"BITRIX_AUTH": "login"}, k)
            self.assertTrue(fields[k]["required"])
        self.assertEqual(spec["configured_when_any"], [["BITRIX_WEBHOOK_URL"], ["B24_URL", "B24_LOGIN", "B24_PASSWORD"]])
        self.assertEqual(spec["aliases"], [{"id": "bitrix24-login", "values": {"BITRIX_AUTH": "login"}}])
        self.assertFalse(list(CATALOG.glob("bitrix24-login*")), "the second card is gone")

    def test_the_variable_names_are_the_ones_older_connections_already_have(self):
        keys = {f["key"] for f in self.spec()["fields"]}
        self.assertTrue({"BITRIX_WEBHOOK_URL", "B24_URL", "B24_LOGIN", "B24_PASSWORD"} <= keys)

    def test_the_instructions_tell_both_ways_apart(self):
        text = (CATALOG / "bitrix24.md").read_text(encoding="utf-8")
        for needle in ("BITRIX_WEBHOOK_URL", "B24_URL", "B24_LOGIN", "B24_PASSWORD", "капчу"):
            self.assertIn(needle, text)

    def test_the_check_follows_the_variables_that_are_set(self):
        import subprocess
        def run(env):
            out = subprocess.run([sys.executable, str(CATALOG / "bitrix24.check.py")], capture_output=True, text=True,
                                 env=dict(env, PATH="/usr/bin:/bin"), timeout=30)
            return json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(run({})["status"], "unconfigured")
        partial = run({"B24_URL": "https://x.invalid", "B24_LOGIN": "u"})
        self.assertEqual(partial["status"], "unconfigured", "a login without a password is not a connection")
        login = run({"B24_URL": "ftp://x", "B24_LOGIN": "u", "B24_PASSWORD": "p"})
        self.assertEqual(login["status"], "fail")
        both = run({"BITRIX_WEBHOOK_URL": "file:///x", "B24_URL": "https://x", "B24_LOGIN": "u", "B24_PASSWORD": "p"})
        self.assertIn("http", both["summary"], "the webhook is checked first when both are present")


class MailPresetsTest(unittest.TestCase):
    def test_every_preset_fills_real_plain_fields_and_matches_what_the_tool_would_guess(self):
        import importlib.util
        spec = json.loads((CATALOG / "mail.json").read_text(encoding="utf-8"))
        fields = {x["key"]: x for x in spec["fields"]}
        self.assertTrue(spec["presets"])
        tool = importlib.util.spec_from_file_location("agent_mail", CATALOG.parent / "agent-tools" / "agent-mail.py")
        am = importlib.util.module_from_spec(tool)
        tool.loader.exec_module(am)
        known = {imap: smtp for imap, smtp in am.PROVIDERS.values()}
        for p in spec["presets"]:
            self.assertTrue(set(p["values"]) <= set(fields), p)
            self.assertFalse(any(fields[k]["secret"] for k in p["values"]), "presets hold no secrets")
            imap, smtp = p["values"]["MAIL_IMAP_HOST"], p["values"]["MAIL_SMTP_HOST"]
            if imap in known:  # the tool's own fallback agrees with the button
                self.assertEqual(known[imap], smtp, p["label"])
            self.assertTrue(smtp.startswith("smtp.") and imap.startswith("imap."), p["label"])


if __name__ == "__main__":
    unittest.main()
