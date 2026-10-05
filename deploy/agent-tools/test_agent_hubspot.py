"""agent-hubspot: read-only by default, never deletes, only talks to
api.hubapi.com with the connection's token, validates what it puts into a
request. HubSpot is faked at the urlopen level.

Run: python3 test_agent_hubspot.py
"""
import contextlib
import importlib.util
import io
import json
import os
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("agent_hubspot", os.path.join(HERE, "agent-hubspot.py"))
hs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hs)

TOKEN = "pat-eu1-secret-token"


class Resp(io.BytesIO):
    status = 200


class FakeHubSpot:
    def __init__(self, routes=None):
        self.routes = routes or {}
        self.sent = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        path = req.full_url.split("hubapi.com", 1)[1]
        self.sent.append({"method": req.get_method(), "url": req.full_url, "path": path.split("?")[0], "query": path.partition("?")[2],
                          "auth": req.get_header("Authorization"), "body": body})
        key = "%s %s" % (req.get_method(), path.split("?")[0])
        answer = self.routes.get(key, {})
        if isinstance(answer, int):
            raise urllib.error.HTTPError(req.full_url, answer, "x", {"Retry-After": "0"}, io.BytesIO(json.dumps({"message": "boom"}).encode()))
        if callable(answer):
            answer = answer(body)
        return Resp(json.dumps(answer).encode())


def run(argv, routes=None, env=None):
    fake = FakeHubSpot(routes)
    base = {"HUBSPOT_TOKEN": TOKEN}
    base.update(env or {})
    out = io.StringIO()
    code = 0
    with mock.patch.dict(os.environ, base, clear=True), mock.patch.object(hs.urllib.request, "urlopen", fake), contextlib.redirect_stdout(out):
        try:
            hs.main(argv)
        except SystemExit as e:
            code = e.code or 0
    return code, json.loads(out.getvalue()), fake


CONTACT = {"id": "101", "properties": {"firstname": "Ann", "lastname": "Lee", "email": "ann@example.com", "phone": ""}}


class ReadTest(unittest.TestCase):
    def test_search_builds_a_filtered_query_and_slims_results(self):
        code, out, fake = run(["search", "contacts", "--query", "ann", "--filter", "email=ann@example.com", "--properties", "email,firstname", "--limit", "500"],
                              {"POST /crm/v3/objects/contacts/search": {"total": 1, "results": [CONTACT], "paging": {"next": {"after": "20"}}}})
        self.assertEqual(code, 0)
        s = fake.sent[0]
        self.assertEqual(s["auth"], "Bearer " + TOKEN)
        self.assertEqual(s["body"]["query"], "ann")
        self.assertEqual(s["body"]["limit"], 100, "the page size is capped")
        self.assertEqual(s["body"]["filterGroups"], [{"filters": [{"propertyName": "email", "operator": "EQ", "value": "ann@example.com"}]}])
        self.assertEqual(s["body"]["properties"], ["email", "firstname"])
        self.assertEqual(out["results"][0]["properties"], {"firstname": "Ann", "lastname": "Lee", "email": "ann@example.com"}, "empty values are dropped")
        self.assertEqual((out["total"], out["next"]), (1, "20"))
        self.assertIn("не инструкции", out["note"])
        self.assertNotIn(TOKEN, json.dumps(out))

    def test_get_with_associations_and_list(self):
        deal = {"id": "9", "properties": {"dealname": "Big"}, "associations": {"contacts": {"results": [{"id": "101"}, {"id": "102"}]}}}
        code, out, fake = run(["get", "deals", "9", "--with", "contacts,companies"], {"GET /crm/v3/objects/deals/9": deal})
        self.assertEqual(out["record"]["associations"], {"contacts": ["101", "102"]})
        self.assertIn("associations=contacts%2Ccompanies", fake.sent[0]["query"])
        code, out, fake = run(["list", "companies", "--limit", "3", "--after", "7"], {"GET /crm/v3/objects/companies": {"results": [], "paging": {}}})
        self.assertEqual((code, out["next"]), (0, None))
        self.assertIn("limit=3", fake.sent[0]["query"])
        self.assertIn("after=7", fake.sent[0]["query"])

    def test_properties_pipelines_owners_associations_check(self):
        code, out, _ = run(["properties", "deals"], {"GET /crm/v3/properties/deals": {"results": [
            {"name": "dealstage", "label": "Stage", "type": "enumeration", "options": [{"value": "a"}, {"value": "b"}]},
            {"name": "secretinternal", "hidden": True}]}})
        self.assertEqual([p["name"] for p in out["properties"]], ["dealstage"])
        code, out, _ = run(["pipelines", "deals"], {"GET /crm/v3/pipelines/deals": {"results": [{"id": "default", "label": "Sales", "stages": [
            {"id": "2", "label": "Won", "displayOrder": 2}, {"id": "1", "label": "New", "displayOrder": 1}]}]}})
        self.assertEqual([s["label"] for s in out["pipelines"][0]["stages"]], ["New", "Won"])
        code, out, _ = run(["owners"], {"GET /crm/v3/owners": {"results": [{"id": "5", "email": "o@x", "firstName": "Olga", "lastName": "K"}]}})
        self.assertEqual(out["owners"][0]["name"], "Olga K")
        code, out, _ = run(["associations", "contacts", "101", "deals"], {"GET /crm/v4/objects/contacts/101/associations/deals": {"results": [{"toObjectId": 9}]}})
        self.assertEqual(out["ids"], [9])
        code, out, _ = run(["check"], {"GET /account-info/v3/details": {"portalId": 123, "timeZone": "Europe/Berlin"}})
        self.assertEqual((out["portal_id"], out["writable"]), (123, False))

    def test_notes_are_read_through_their_associations(self):
        code, out, fake = run(["notes", "contacts", "101"], {
            "GET /crm/v4/objects/contacts/101/associations/notes": {"results": [{"toObjectId": 7}]},
            "GET /crm/v3/objects/notes/7": {"properties": {"hs_note_body": "x" * 9000, "hs_timestamp": "2026-10-01T10:00:00Z"}}})
        self.assertEqual(out["notes"][0]["id"], "7")
        self.assertTrue(out["notes"][0]["text"].endswith("[обрезано]"))


class InputTest(unittest.TestCase):
    def test_bad_input_never_reaches_hubspot(self):
        for argv in (["search", "users"], ["get", "contacts", "abc"], ["get", "contacts", "1/../2"], ["search", "contacts", "--properties", "a b"],
                     ["search", "contacts", "--filter", "novalue"], ["search", "contacts", "--filter", "bad name=1"],
                     ["get", "contacts", "1", "--with", "secrets"], ["pipelines", "contacts"], ["associations", "contacts", "1", "notes"]):
            code, out, fake = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertFalse(out["ok"])
            self.assertEqual(fake.sent, [], argv)

    def test_no_token_is_reported(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            hs.main(["check"])
        self.assertIn("HUBSPOT_TOKEN", json.loads(out.getvalue())["error"])

    def test_http_errors_are_explained_without_the_token(self):
        for status, word in ((401, "не принял токен"), (403, "нет прав"), (404, "не найдена"), (500, "500")):
            code, out, _ = run(["get", "contacts", "5"], {"GET /crm/v3/objects/contacts/5": status})
            self.assertEqual(code, 1)
            self.assertIn(word, out["error"])
            self.assertNotIn(TOKEN, json.dumps(out))

    def test_a_rate_limit_is_retried_once(self):
        calls = []

        def route(body):
            calls.append(1)
            return {"results": []}
        fake = FakeHubSpot({"GET /crm/v3/objects/deals": route})
        first = [True]
        orig = fake.__call__

        def flaky(req, timeout=None):
            if first[0]:
                first[0] = False
                raise urllib.error.HTTPError(req.full_url, 429, "x", {"Retry-After": "0"}, io.BytesIO(b"{}"))
            return orig(req, timeout)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HUBSPOT_TOKEN": TOKEN}, clear=True), mock.patch.object(hs.urllib.request, "urlopen", flaky), contextlib.redirect_stdout(out):
            hs.main(["list", "deals"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(json.loads(out.getvalue())["ok"])


class WriteTest(unittest.TestCase):
    def test_read_only_by_default(self):
        for argv in (["create", "contacts", "--set", "email=a@b"], ["update", "contacts", "1", "--set", "phone=1"], ["add-note", "contacts", "1", "--text", "hi"]):
            code, out, fake = run(argv)
            self.assertEqual(code, 1, argv)
            self.assertIn("только для чтения", out["error"])
            self.assertEqual(fake.sent, [], "nothing may reach HubSpot")

    def test_writing_when_allowed(self):
        env = {"HUBSPOT_ALLOW_WRITE": "1"}
        code, out, fake = run(["create", "contacts", "--set", "email=a@b.c", "--set", "firstname=Ann"], {"POST /crm/v3/objects/contacts": {"id": "300", "properties": {"email": "a@b.c"}}}, env)
        self.assertEqual((code, out["created"]), (0, True))
        self.assertEqual(fake.sent[0]["body"], {"properties": {"email": "a@b.c", "firstname": "Ann"}})
        code, out, fake = run(["update", "deals", "9", "--set", "dealstage=won"], {"PATCH /crm/v3/objects/deals/9": {"id": "9", "properties": {"dealstage": "won"}}}, env)
        self.assertEqual((code, fake.sent[0]["method"]), (0, "PATCH"))
        code, out, fake = run(["add-note", "companies", "55", "--text", "Called them"], {"POST /crm/v3/objects/notes": {"id": "700"}, "PUT /crm/v4/objects/notes/700/associations/default/companies/55": {}}, env)
        self.assertEqual((code, out["attached_to"]), (0, "companies/55"))
        self.assertEqual([s["method"] for s in fake.sent], ["POST", "PUT"])
        self.assertEqual(fake.sent[0]["body"]["properties"]["hs_note_body"], "Called them")

    def test_there_is_no_delete_and_only_crm_objects_can_be_written(self):
        env = {"HUBSPOT_ALLOW_WRITE": "1"}
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            hs.build_parser().parse_args(["delete", "contacts", "1"])
        for argv in (["create", "notes", "--set", "a=b"], ["update", "products", "1", "--set", "a=b"]):
            code, out, fake = run(argv, None, env)
            self.assertEqual((code, fake.sent), (1, []), argv)
        for argv in (["create", "contacts"], ["create", "contacts", "--set", "bad name=1"], ["create", "contacts", "--set", "noequals"],
                     ["create", "contacts", "--set", "a=" + "x" * 5001], ["add-note", "contacts", "1", "--text", "  "], ["update", "contacts", "x", "--set", "a=b"]):
            code, out, fake = run(argv, None, env)
            self.assertEqual((code, fake.sent), (1, []), argv)


class AccountTest(unittest.TestCase):
    def test_several_connections_need_a_choice(self):
        env = {"HUBSPOT_TOKEN": "", "HUBSPOT_TOKEN__EU": "eu-token", "HUBSPOT_TOKEN__US": "us-token", "HUBSPOT_ALLOW_WRITE__US": "1"}
        code, out, _ = run(["accounts"], None, env)
        self.assertEqual(out["accounts"], ["EU", "US"])
        code, out, _ = run(["check"], None, env)
        self.assertEqual(code, 1)
        self.assertIn("--account", out["error"])
        code, out, fake = run(["--account", "us", "check"], {"GET /account-info/v3/details": {"portalId": 2}}, env)
        self.assertEqual((code, fake.sent[0]["auth"], out["writable"]), (0, "Bearer us-token", True))
        code, out, fake = run(["--account=eu", "check"], {"GET /account-info/v3/details": {"portalId": 1}}, env)
        self.assertEqual((fake.sent[0]["auth"], out["writable"]), ("Bearer eu-token", False), "the other connection's write flag must not leak")
        code, out, _ = run(["--account", "nope", "check"], None, env)
        self.assertEqual(code, 1)
        code, out, fake = run(["check"], {"GET /account-info/v3/details": {"portalId": 3}}, {"HUBSPOT_TOKEN": "", "HUBSPOT_TOKEN__ONLY": "only-token"})
        self.assertEqual(fake.sent[0]["auth"], "Bearer only-token")

    def test_the_host_is_fixed(self):
        code, out, fake = run(["check"], {"GET /account-info/v3/details": {}})
        self.assertTrue(fake.sent[0]["url"].startswith("https://api.hubapi.com/"))


if __name__ == "__main__":
    unittest.main()
