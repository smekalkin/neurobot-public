import importlib.machinery
import importlib.util
import json
import os


def finish(status, summary, items):
    print(json.dumps({"status": status, "summary": summary, "items": items}, ensure_ascii=False))
    raise SystemExit(0)


if not os.environ.get("HUBSPOT_TOKEN"):
    finish("unconfigured", "токен не задан", [])

tool = os.environ.get("AGENT_HUBSPOT_TOOL") or "/usr/local/bin/agent-hubspot"  # the override is for tests
if not os.path.exists(tool):
    finish("fail", "инструмент не установлен", [{"level": "fail", "text": "Сохраните подключение ещё раз: инструмент agent-hubspot не установлен"}])
loader = importlib.machinery.SourceFileLoader("agent_hubspot", tool)
hs = importlib.util.module_from_spec(importlib.util.spec_from_loader("agent_hubspot", loader))
loader.exec_module(hs)

items = [{"level": "ok", "text": "Инструмент agent-hubspot установлен"}]
status = "ok"
try:
    _, d = hs.call("GET", "/account-info/v3/details")
    items.append({"level": "ok", "text": "Токен принят, портал HubSpot: %s" % d.get("portalId")})
except hs.Fail as e:
    finish("fail", "не работает", items + [{"level": "fail", "text": str(e)}])

for obj, label in (("contacts", "контакты"), ("companies", "компании"), ("deals", "сделки"), ("tickets", "обращения")):
    try:
        hs.call("GET", "/crm/v3/objects/%s" % obj, {"limit": 1})
        items.append({"level": "ok", "text": "Чтение: %s доступны" % label})
    except hs.Fail as e:
        if "403" in str(e):
            items.append({"level": "warn", "text": "Чтение: %s недоступны (у приложения нет права)" % label})
            status = "warn"
        else:
            items.append({"level": "fail", "text": str(e)})
            status = "fail"
items.append({"level": "ok", "text": "Режим: чтение, создание и изменение" if os.environ.get("HUBSPOT_ALLOW_WRITE") == "1" else "Режим: только чтение"})
finish(status, {"ok": "работает", "warn": "работает, но есть замечания", "fail": "не работает"}[status], items)
