import json, os, subprocess

if not (os.environ.get("MAIL_USER") and os.environ.get("MAIL_PASSWORD")):
    print(json.dumps({"status": "unconfigured", "summary": "ящик не задан", "items": []}))
    raise SystemExit(0)
tool = "/usr/local/bin/agent-mail"
if not os.path.exists(tool):
    print(json.dumps({"status": "fail", "summary": "не установлен инструмент agent-mail", "items": [{"level": "fail", "text": "Сохраните доступ ещё раз: инструмент ставится при сохранении"}]}, ensure_ascii=False))
    raise SystemExit(0)
p = subprocess.run(["python3", tool, "check"], capture_output=True, text=True, timeout=90)
try:
    d = json.loads(p.stdout.strip().splitlines()[-1])
except Exception:
    d = {"ok": False, "error": (p.stderr or p.stdout).strip()[:200] or "нет ответа"}
if d.get("ok"):
    items = [
        {"level": "ok", "text": "Вход выполнен: %s" % d.get("user")},
        {"level": "ok", "text": "Папок: %s, писем во входящих: %s" % (d.get("folders"), d.get("inbox_messages"))},
        {"level": "ok", "text": "Чтение: только просмотр (EXAMINE), письма не меняются"},
    ]
    if os.environ.get("AGENTDESK_WRITE") == "1":
        items.append({"level": "ok", "text": "Отправка включена (после подтверждения человеком); вход в SMTP не проверяется, чтобы ничего не отправлять"})
    print(json.dumps({"status": "ok", "summary": "ящик читается" + (", отправка включена" if os.environ.get("AGENTDESK_WRITE") == "1" else ", только чтение"), "items": items}, ensure_ascii=False))
else:
    print(json.dumps({"status": "fail", "summary": "не удалось войти", "items": [{"level": "fail", "text": d.get("error") or "ошибка входа"}]}, ensure_ascii=False))
