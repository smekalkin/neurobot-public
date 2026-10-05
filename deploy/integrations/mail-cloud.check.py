import base64, json, os, urllib.error, urllib.request
import xml.etree.ElementTree as ET

user, pw = os.environ.get("MAILCLOUD_USER", ""), os.environ.get("MAILCLOUD_PASSWORD", "")
if not user or not pw:
    print(json.dumps({"status": "unconfigured", "summary": "логин или пароль не заданы", "items": []}))
    raise SystemExit(0)
auth = "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()


def call(method, path, headers=None, body=None):
    req = urllib.request.Request("https://webdav.cloud.mail.ru" + path, method=method, data=body,
                                 headers=dict({"Authorization": auth}, **(headers or {})))
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return 0, str(e)[:120].encode()


items, status = [], "ok"
st, body = call("PROPFIND", "/", {"Depth": "1", "Content-Type": "application/xml"},
                b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>')
if st in (200, 207):
    try:
        n = len(ET.fromstring(body).findall("{DAV:}response")) - 1  # minus the root itself
    except Exception:
        n = 1
    items.append({"level": "ok", "text": "Вход выполнен, чтение Облака работает"})
    if n <= 0:
        items.append({"level": "warn", "text": "Видимых папок нет: откройте служебному ящику нужные папки с правом «Просмотр»"})
        status = "warn"
elif st == 401:
    items.append({"level": "fail", "text": "Логин или пароль не приняты (нужен пароль для внешнего приложения с доступом «Облако»)"})
    status = "fail"
else:
    items.append({"level": "fail", "text": "Облако не отвечает: HTTP %d" % st})
    status = "fail"
if status != "fail":
    # Nothing is created or changed: DELETE of a path that cannot exist.
    st, _ = call("DELETE", "/__agentdesk_probe_nonexistent__")
    if os.environ.get("AGENTDESK_WRITE") == "1":
        items.append({"level": "ok", "text": "Запись включена по вашему выбору: менять файлы можно только в папках с правом «Редактирование»"})
    elif st in (401, 403):
        items.append({"level": "ok", "text": "Запись запрещена (только чтение)"})
    else:
        items.append({"level": "warn", "text": "Запрет записи не подтверждён: держите доступ к папкам только «Просмотр»"})
        status = "warn"
print(json.dumps({"status": status, "summary": {"ok": "работает, чтение и запись" if os.environ.get("AGENTDESK_WRITE") == "1" else "работает, только чтение", "warn": "работает, но есть замечание", "fail": "не работает"}[status], "items": items}, ensure_ascii=False))
