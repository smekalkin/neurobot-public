import json, os

# The agent's copy of the conversation: written by agentdesk to
# /var/lib/agentdesk-chat/<user>/<NAME>.jsonl. This check reads only that.
import getpass, glob
items, status = [], "ok"
if not os.environ.get("AGENT_CHAT_ENABLED"):
    print(json.dumps({"status": "unconfigured", "summary": "канал не подключён", "items": []}))
    raise SystemExit(0)
tool = "/usr/local/bin/agent-chat"
if not os.path.exists(tool):
    print(json.dumps({"status": "fail", "summary": "не установлен инструмент agent-chat", "items": [{"level": "fail", "text": "Сохраните подключение ещё раз: инструмент ставится при сохранении"}]}, ensure_ascii=False))
    raise SystemExit(0)
items.append({"level": "ok", "text": "Инструмент agent-chat установлен"})
files = glob.glob("/var/lib/agentdesk-chat/*/*.jsonl")
if files:
    items.append({"level": "ok", "text": "Копий переписки на сервере: %d" % len(files)})
else:
    items.append({"level": "warn", "text": "Переписка ещё не передана: она появится после первых сообщений боту"})
    status = "warn"
if os.environ.get("AGENTDESK_WRITE") == "1":
    if os.environ.get("MAX_BOT_TOKEN"):
        items.append({"level": "ok", "text": "Ответы разрешены: токен бота передан агенту"})
    else:
        items.append({"level": "warn", "text": "Ответы разрешены, но токен бота не передан: сохраните подключение ещё раз"})
        status = "warn"
else:
    items.append({"level": "ok", "text": "Только наблюдение: токен бота агенту не передан"})
print(json.dumps({"status": status, "summary": {"ok": "работает", "warn": "работает, но есть замечание", "fail": "не работает"}[status], "items": items}, ensure_ascii=False))
