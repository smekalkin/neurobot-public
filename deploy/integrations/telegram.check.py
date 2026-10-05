import json, os, urllib.error, urllib.request

if not os.environ.get("AGENT_CHAT_ENABLED"):
    print(json.dumps({"status": "unconfigured", "summary": "канал не подключён", "items": []}, ensure_ascii=False))
    raise SystemExit(0)

items = []
tool = "/usr/local/bin/agent-chat"
if os.path.exists(tool):
    items.append({"level": "ok", "text": "Инструмент agent-chat установлен"})
else:
    items.append({"level": "fail", "text": "Сохраните подключение ещё раз: инструмент agent-chat не установлен"})

base = os.environ.get("AGENT_CHAT_DIR", "")
files = []
if base and os.path.isdir(base):
    files = [x for x in os.listdir(base) if x.endswith(".jsonl")]
items.append({"level": "ok" if files else "warn", "text": "Копий переписки на сервере: %d" % len(files)})
status = "ok" if os.path.exists(tool) and files else ("warn" if os.path.exists(tool) else "fail")
print(json.dumps({"status": status, "summary": {"ok": "работает", "warn": "подключено, сообщений пока нет", "fail": "не работает"}[status], "items": items}, ensure_ascii=False))
