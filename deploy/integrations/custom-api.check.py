import importlib.machinery
import importlib.util
import json
import os
import sys


def finish(status, summary, items):
    print(json.dumps({"status": status, "summary": summary, "items": items}, ensure_ascii=False))
    raise SystemExit(0)


if not os.environ.get("API_BASE_URL"):
    finish("unconfigured", "адрес API не задан", [])

tool = os.environ.get("AGENT_HTTP_TOOL") or "/usr/local/bin/agent-http"  # the override is for tests
if not os.path.exists(tool):
    finish("fail", "инструмент не установлен", [{"level": "fail", "text": "Сохраните подключение ещё раз: инструмент agent-http не установлен"}])
loader = importlib.machinery.SourceFileLoader("agent_http", tool)
http = importlib.util.module_from_spec(importlib.util.spec_from_loader("agent_http", loader))
loader.exec_module(http)

items = [{"level": "ok", "text": "Инструмент agent-http установлен"}]
try:
    cfg = http.Config(os.environ)
except http.Fail as e:
    finish("fail", "настройки не подходят", items + [{"level": "fail", "text": str(e)}])

if cfg.origin[0] == "http":
    items.append({"level": "warn", "text": "Адрес без шифрования (http): секрет пойдёт по сети открыто"})
items.append({"level": "ok", "text": "Режим: чтение и изменение" if cfg.write else "Режим: только чтение"})

status = "ok"
try:
    # A plain GET of the base address changes nothing anywhere.
    r = http.do_request(cfg, "GET", "/", limit=200)
    code = r["status"]
    if code in (401, 403):
        status = "fail"
        items.append({"level": "fail", "text": "Сервер ответил %d: ключ или способ входа не подходят" % code})
    elif code >= 500:
        status = "warn"
        items.append({"level": "warn", "text": "Сервер ответил %d: проверьте, что адрес верный" % code})
    elif code in (404, 405):
        items.append({"level": "ok", "text": "Сервер отвечает (корневой адрес даёт %d — для многих API это нормально)" % code})
    else:
        items.append({"level": "ok", "text": "Сервер отвечает: HTTP %d" % code})
except http.Fail as e:
    status = "fail"
    items.append({"level": "fail", "text": str(e)})

if cfg.openapi and status != "fail":
    try:
        d = http.do_openapi(cfg, limit=100000)
        if d.get("format") == "openapi":
            n = len([x for x in d["operations"].splitlines() if x])
            items.append({"level": "ok", "text": "Описание API прочитано: %d методов" % n})
        else:
            items.append({"level": "warn", "text": "Описание API скачано, но это не OpenAPI-документ"})
    except http.Fail as e:
        status = "warn" if status == "ok" else status
        items.append({"level": "warn", "text": "Описание API: %s" % e})

finish(status, {"ok": "работает", "warn": "работает, но есть замечания", "fail": "не работает"}[status], items)
