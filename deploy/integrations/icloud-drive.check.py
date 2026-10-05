import json, os, shutil, subprocess

if not os.environ.get("ICLOUD_RCLONE_CONFIG_B64"):
    print(json.dumps({"status":"unconfigured","summary":"конфигурация не задана","items":[]},ensure_ascii=False)); raise SystemExit(0)
if not shutil.which("rclone"):
    print(json.dumps({"status":"fail","summary":"на сервере не установлен rclone","items":[{"level":"fail","text":"Установите rclone версии с backend iclouddrive"}]},ensure_ascii=False)); raise SystemExit(0)
tool="/usr/local/bin/agent-icloud"
if not os.path.exists(tool):
    print(json.dumps({"status":"fail","summary":"не установлен agent-icloud","items":[]},ensure_ascii=False)); raise SystemExit(0)
p=subprocess.run(["python3",tool,"check"],capture_output=True,text=True,timeout=180)
try: d=json.loads(p.stdout.strip().splitlines()[-1])
except Exception: d={"ok":False,"error":(p.stderr or p.stdout)[-200:]}
if d.get("ok"):
    mode="чтение и запись" if os.environ.get("AGENTDESK_WRITE")=="1" else "только чтение"
    print(json.dumps({"status":"ok","summary":"работает, "+mode,"items":[{"level":"ok","text":"Сессия iCloud действительна"}]},ensure_ascii=False))
else:
    print(json.dumps({"status":"fail","summary":"iCloud недоступен","items":[{"level":"fail","text":d.get("error") or "ошибка rclone"}]},ensure_ascii=False))
