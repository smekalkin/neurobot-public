import importlib.machinery
import importlib.util
import json
import os


def finish(status, summary, items):
    print(json.dumps({"status": status, "summary": summary, "items": items}, ensure_ascii=False))
    raise SystemExit(0)


if not os.environ.get("SLACK_BOT_TOKEN"):
    finish("unconfigured", "токен не задан", [])

tool = os.environ.get("AGENT_SLACK_TOOL") or "/usr/local/bin/agent-slack"  # the override is for tests
if not os.path.exists(tool):
    finish("fail", "инструмент не установлен", [{"level": "fail", "text": "Сохраните подключение ещё раз: инструмент agent-slack не установлен"}])
loader = importlib.machinery.SourceFileLoader("agent_slack", tool)
sl = importlib.util.module_from_spec(importlib.util.spec_from_loader("agent_slack", loader))
loader.exec_module(sl)

items = [{"level": "ok", "text": "Инструмент agent-slack установлен"}]
status = "ok"
try:
    auth, hdrs = sl.api("auth.test", post=True)
except sl.Fail as e:
    finish("fail", "не работает", items + [{"level": "fail", "text": str(e)}])
items.append({"level": "ok", "text": "Токен принят, рабочее пространство: %s, бот: %s" % (auth.get("team"), auth.get("user"))})

scopes = {s.strip() for s in (hdrs.get("X-OAuth-Scopes") or "").split(",") if s.strip()}
if scopes:
    for need in ("channels:read", "channels:history"):
        if need not in scopes:
            items.append({"level": "warn", "text": "У приложения нет права %s" % need})
            status = "warn"
    if os.environ.get("SLACK_ALLOW_WRITE") == "1" and "chat:write" not in scopes:
        items.append({"level": "warn", "text": "Запись разрешена, но у приложения нет права chat:write"})
        status = "warn"
try:
    d, _ = sl.api("conversations.list", {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": 200})
    member = [c for c in d.get("channels", []) if c.get("is_member")]
    if member:
        items.append({"level": "ok", "text": "Бот состоит в каналах: %d" % len(member)})
    else:
        items.append({"level": "warn", "text": "Бота ещё не пригласили ни в один канал (/invite @имя_бота)"})
        status = "warn"
except sl.Fail as e:
    items.append({"level": "warn", "text": str(e)})
    status = "warn"
items.append({"level": "ok", "text": "Режим: чтение и публикация" if os.environ.get("SLACK_ALLOW_WRITE") == "1" else "Режим: только чтение"})
finish(status, {"ok": "работает", "warn": "работает, но есть замечания"}[status], items)
