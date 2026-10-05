import json
import os
import urllib.error
import urllib.parse
import urllib.request

url = os.environ.get("MCP_URL", "").strip()
if not url:
    print(json.dumps({"status": "unconfigured", "summary": "адрес MCP-сервера не задан", "items": []}, ensure_ascii=False))
    raise SystemExit(0)


def finish(status, summary, items):
    print(json.dumps({"status": status, "summary": summary, "items": items}, ensure_ascii=False))
    raise SystemExit(0)


u = urllib.parse.urlsplit(url)
if u.scheme != "https" or not u.hostname:
    finish("fail", "адрес должен начинаться с https://", [{"level": "fail", "text": "MCP-сервер подключается только по https://"}])

headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": "agentdesk-check/1"}
token = os.environ.get("MCP_TOKEN", "")
if token:
    name = os.environ.get("MCP_AUTH_HEADER") or "Authorization"
    scheme = os.environ.get("MCP_AUTH_SCHEME") or "Bearer"
    headers[name] = token if scheme == "-" else "%s %s" % (scheme, token)


def rpc(method, params=None, rid=1, session=None):
    body = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    h = dict(headers)
    if session:
        h["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
    resp = urllib.request.urlopen(req, timeout=20)
    raw = resp.read(2 * 1024 * 1024).decode("utf-8", "replace")
    sid = resp.headers.get("Mcp-Session-Id") or session
    if rid is None:
        return None, sid
    if "text/event-stream" in (resp.headers.get("Content-Type") or ""):
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if msg.get("id") == rid:
                    return msg, sid
        return None, sid
    return json.loads(raw), sid


items = []
if os.environ.get("MCP_TRANSPORT") == "sse":
    # The legacy transport: the endpoint opens an event stream.
    try:
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream", "User-Agent": "agentdesk-check/1", **{k: v for k, v in headers.items() if k not in ("Content-Type", "Accept")}})
        resp = urllib.request.urlopen(req, timeout=10)
        ctype = resp.headers.get("Content-Type") or ""
        if "text/event-stream" in ctype:
            finish("ok", "работает", [{"level": "ok", "text": "Сервер отдаёт поток событий (SSE)"}])
        finish("warn", "работает, но есть замечания", [{"level": "warn", "text": "Сервер ответил, но это не SSE (Content-Type: %s)" % ctype[:60]}])
    except urllib.error.HTTPError as e:
        text = "Сервер ответил %d: токен не подходит" % e.code if e.code in (401, 403) else "Сервер ответил %d" % e.code
        finish("fail", "не работает", [{"level": "fail", "text": text}])
    except Exception as e:
        finish("fail", "не работает", [{"level": "fail", "text": "Нет связи с сервером: %s" % str(e)[:150]}])

try:
    init, sid = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "agentdesk-check", "version": "1"}})
except urllib.error.HTTPError as e:
    if e.code in (401, 403):
        text = "Сервер ответил %d: токен не подходит" % e.code
    elif e.code in (404, 405):
        text = "Сервер ответил %d: возможно, нужен тип подключения SSE" % e.code
    else:
        text = "Сервер ответил %d" % e.code
    finish("fail", "не работает", [{"level": "fail", "text": text}])
except Exception as e:
    finish("fail", "не работает", [{"level": "fail", "text": "Нет связи с сервером: %s" % str(e)[:150]}])
result = (init or {}).get("result") if isinstance(init, dict) else None
if not isinstance(result, dict):
    finish("fail", "не работает", [{"level": "fail", "text": "Сервер не ответил на initialize как MCP-сервер"}])
info = result.get("serverInfo") or {}
items.append({"level": "ok", "text": "MCP-сервер отвечает: %s %s" % (info.get("name") or "?", info.get("version") or "")})
status = "ok"
try:
    rpc("notifications/initialized", rid=None, session=sid)
    tools, _ = rpc("tools/list", {}, rid=2, session=sid)
    n = len(((tools or {}).get("result") or {}).get("tools") or [])
    items.append({"level": "ok" if n else "warn", "text": "Инструментов: %d" % n})
    if not n:
        status = "warn"
except Exception as e:
    status = "warn"
    items.append({"level": "warn", "text": "Список инструментов не получен: %s" % str(e)[:120]})
finish(status, {"ok": "работает", "warn": "работает, но есть замечания"}[status], items)
