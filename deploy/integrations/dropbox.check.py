import json, os, subprocess

if not os.environ.get("DROPBOX_ACCESS_TOKEN"):
    print(json.dumps({"status": "unconfigured", "summary": "токен не задан", "items": []}, ensure_ascii=False)); raise SystemExit(0)
tool = "/usr/local/bin/agent-dropbox"
if not os.path.exists(tool):
    print(json.dumps({"status": "fail", "summary": "не установлен agent-dropbox", "items": []}, ensure_ascii=False)); raise SystemExit(0)
p = subprocess.run(["python3", tool, "check"], capture_output=True, text=True, timeout=60)
try: d = json.loads(p.stdout.strip().splitlines()[-1])
except Exception: d = {"ok": False, "error": (p.stderr or p.stdout)[-200:]}
if d.get("ok"):
    mode = "чтение и запись" if os.environ.get("AGENTDESK_WRITE") == "1" else "только чтение"
    print(json.dumps({"status": "ok", "summary": "работает, " + mode, "items": [{"level": "ok", "text": "Аккаунт: " + (d.get("email") or d.get("name") or "подключён")}]}, ensure_ascii=False))
else:
    print(json.dumps({"status": "fail", "summary": "не удалось войти", "items": [{"level": "fail", "text": d.get("error") or "ошибка Dropbox"}]}, ensure_ascii=False))
