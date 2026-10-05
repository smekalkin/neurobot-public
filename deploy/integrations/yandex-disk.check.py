import json, os, urllib.error, urllib.request

tok = os.environ.get("YADISK_TOKEN", "")
if not tok:
    print(json.dumps({"status": "unconfigured", "summary": "токен не задан", "items": []}))
    raise SystemExit(0)


def call(method, path):
    req = urllib.request.Request("https://cloud-api.yandex.net/v1/disk" + path, method=method, headers={"Authorization": "OAuth " + tok})
    try:
        r = urllib.request.urlopen(req, timeout=20)
        body = r.read()
        return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"description": str(e)[:120]}


items, status = [], "ok"
want_write = os.environ.get("AGENTDESK_WRITE") == "1"
st, d = call("GET", "")
if st == 200:
    items.append({"level": "ok", "text": "Аккаунт: %s" % (d.get("user") or {}).get("login", "?")})
    st2, _ = call("GET", "/resources?path=disk%3A%2F&limit=1")
    if st2 == 200:
        items.append({"level": "ok", "text": "Чтение Диска работает"})
    else:
        items.append({"level": "fail", "text": "Чтение корня: HTTP %d" % st2})
        status = "fail"
else:
    items.append({"level": "fail", "text": "Сведения о Диске: HTTP %d %s" % (st, d.get("description", ""))})
    status = "fail"
# Permission probe: DELETE of a deliberately impossible path. Nothing exists
# there, so a writable token returns 404 while a read-only token is rejected.
st, d = call("DELETE", "/resources?path=disk%3A%2F__agentdesk_probe_nonexistent__&permanently=true")
if want_write and st in (404, 202, 204):
    items.append({"level": "ok", "text": "Токен разрешает запись"})
elif want_write and st in (401, 403):
    items.append({"level": "fail", "text": "Запись включена, но у токена нет права cloud_api:disk.write"})
    status = "fail"
elif not want_write and st in (401, 403):
    items.append({"level": "ok", "text": "Запись запрещена правами токена"})
elif not want_write and st in (404, 202, 204):
    items.append({"level": "warn", "text": "Подключение только для чтения, но токен имеет право записи"})
    if status == "ok": status = "warn"
else:
    items.append({"level": "warn", "text": "Не удалось проверить право записи: HTTP %d" % st})
    if status == "ok":
        status = "warn"
print(json.dumps({"status": status, "summary": {"ok": "работает, чтение и запись" if want_write else "работает, только чтение", "warn": "работает, но есть замечание", "fail": "не работает"}[status], "items": items}, ensure_ascii=False))
