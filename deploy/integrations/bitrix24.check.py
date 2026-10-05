import json, os, urllib.error, urllib.parse, urllib.request

# One card, two ways in: a REST webhook (BITRIX_WEBHOOK_URL) or a service user's
# login and password (B24_URL, B24_LOGIN, B24_PASSWORD). The connection holds the
# variables of the method it was saved with, so the check follows what is set.


def check_webhook():
    url = os.environ.get("BITRIX_WEBHOOK_URL", "").rstrip("/")
    if not url:
        print(json.dumps({"status": "unconfigured", "summary": "вебхук не задан", "items": []}))
        return

    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        # The address is typed into a form; file:// or ftp:// must never be opened from here.
        print(json.dumps({"status": "fail", "summary": "адрес должен начинаться с http:// или https://", "items": []}, ensure_ascii=False))
        return


    def call(method, params=None):
        data = urllib.parse.urlencode(params or {}).encode()
        try:
            return json.load(urllib.request.urlopen(urllib.request.Request(url + "/" + method + ".json", data=data), timeout=20))
        except urllib.error.HTTPError as e:
            try:
                return json.load(e)
            except Exception:
                return {"error": "HTTP %d" % e.code, "error_description": ""}
        except Exception as e:
            return {"error": "network", "error_description": str(e)[:120]}


    def err(r):
        return ((r.get("error") or "") + " " + (r.get("error_description") or "")).strip()


    items, status = [], "ok"
    u = call("user.current")
    if "result" in u:
        name = ((u["result"].get("NAME") or "") + " " + (u["result"].get("LAST_NAME") or "")).strip()
        items.append({"level": "ok", "text": "Вебхук работает от имени пользователя: " + (name or "?")})
    else:
        items.append({"level": "fail", "text": "user.current: " + err(u)})
        status = "fail"
    s = call("scope")
    if isinstance(s.get("result"), list):
        items.append({"level": "ok", "text": "Области вебхука: " + ", ".join(s["result"])})
    r = call("crm.lead.list", {"select[0]": "ID", "start": "-1"})
    if "result" in r:
        items.append({"level": "ok", "text": "Чтение CRM работает"})
    else:
        items.append({"level": "fail", "text": "Чтение CRM: " + err(r)})
        status = "fail"
    # Write probe: delete lead id 0, which never exists. A read-only user is
    # refused ("access denied"); a user who may write gets "not found". Nothing
    # is ever modified.
    low = err(call("crm.lead.delete", {"id": "0"})).lower()
    want_write = os.environ.get("AGENTDESK_WRITE") == "1"
    if any(x in low for x in ("access", "denied", "insufficient", "forbidden")):
        if want_write:
            items.append({"level": "warn", "text": "Запись включена, но у вебхука нет прав на запись: выдайте их в Bitrix24"})
            if status == "ok":
                status = "warn"
        else:
            items.append({"level": "ok", "text": "Запись запрещена (права только на чтение)"})
    elif "not found" in low or "не найден" in low:
        if want_write:
            items.append({"level": "ok", "text": "Запись разрешена (как вы и выбрали)"})
        else:
            items.append({"level": "fail", "text": "Запись РАЗРЕШЕНА, а подключение только для чтения: ограничьте роль служебного пользователя в Bitrix24"})
            if status == "ok":
                status = "warn"
    else:
        items.append({"level": "warn", "text": "Не удалось проверить запрет записи: " + (low or "неожиданный ответ")})
        if status == "ok":
            status = "warn"
    print(json.dumps({"status": status, "summary": {"ok": "работает, чтение и запись" if want_write else "работает, только чтение", "warn": "работает, но есть замечание", "fail": "не работает"}[status], "items": items}, ensure_ascii=False))


def check_login():
    url = os.environ.get("B24_URL", "").rstrip("/")
    if not (url and os.environ.get("B24_LOGIN") and os.environ.get("B24_PASSWORD")):
        print(json.dumps({"status": "unconfigured", "summary": "не заданы адрес, логин или пароль", "items": []}, ensure_ascii=False))
        return

    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        # The address is typed into a form; file:// or ftp:// must never be opened from here.
        print(json.dumps({"status": "fail", "summary": "адрес должен начинаться с http:// или https://", "items": []}, ensure_ascii=False))
        return
    items = [{"level": "ok", "text": "Логин и пароль заданы (значения скрыты)"}]
    status = "warn"
    try:
        r = urllib.request.urlopen(urllib.request.Request(url + "/", method="GET"), timeout=20)
        items.append({"level": "ok", "text": "Портал отвечает (HTTP %d)" % r.status})
    except urllib.error.HTTPError as e:
        items.append({"level": "ok" if e.code < 500 else "fail", "text": "Портал отвечает (HTTP %d)" % e.code})
        status = "warn" if e.code < 500 else "fail"
    except Exception as e:
        items.append({"level": "fail", "text": "Портал недоступен: %s" % str(e)[:120]})
        status = "fail"
    items.append({"level": "warn", "text": "Сам вход автоматически не проверяется (двухфакторная защита и капча ему мешают): проверьте вручную при первом использовании"})
    print(json.dumps({"status": status, "summary": "заданы, вход не проверен" if status == "warn" else "портал недоступен", "items": items}, ensure_ascii=False))


if os.environ.get("BITRIX_WEBHOOK_URL"):
    check_webhook()
elif os.environ.get("B24_URL") or os.environ.get("B24_LOGIN") or os.environ.get("B24_PASSWORD"):
    check_login()
else:
    print(json.dumps({"status": "unconfigured", "summary": "не заданы данные подключения", "items": []}, ensure_ascii=False))
