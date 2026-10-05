import json, os, subprocess

keys=("GDRIVE_CLIENT_ID","GDRIVE_CLIENT_SECRET","GDRIVE_REFRESH_TOKEN")
if not all(os.environ.get(k) for k in keys):
    print(json.dumps({"status":"unconfigured","summary":"OAuth-данные заданы не полностью","items":[]},ensure_ascii=False)); raise SystemExit(0)
tool="/usr/local/bin/agent-gdrive"
if not os.path.exists(tool):
    print(json.dumps({"status":"fail","summary":"не установлен agent-gdrive","items":[]},ensure_ascii=False)); raise SystemExit(0)
p=subprocess.run(["python3",tool,"check"],capture_output=True,text=True,timeout=60)
try: d=json.loads(p.stdout.strip().splitlines()[-1])
except Exception: d={"ok":False,"error":(p.stderr or p.stdout)[-200:]}
if d.get("ok"):
    user=d.get("user") or {}; mode="чтение и запись" if os.environ.get("AGENTDESK_WRITE")=="1" else "только чтение"
    print(json.dumps({"status":"ok","summary":"работает, "+mode,"items":[{"level":"ok","text":"Аккаунт: "+(user.get("emailAddress") or user.get("displayName") or "подключён")}]},ensure_ascii=False))
else:
    print(json.dumps({"status":"fail","summary":"не удалось войти","items":[{"level":"fail","text":d.get("error") or "ошибка Google Drive"}]},ensure_ascii=False))
