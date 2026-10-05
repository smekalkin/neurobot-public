#!/usr/bin/env python3
"""agent-task: tell the scheduler how a scheduled task went, and look at your tasks.

A task that agentdesk runs for you arrives as a message that names a run, like
`[Задача по расписанию «Сводка», запуск daily-1759395600] ...`. When you are
done, say so with the run id from that message:

  agent-task done RUN [note]        the task is finished; the note is a short result
  agent-task fail RUN [reason]      it could not be done; say why (it may be retried)
  agent-task list                   the tasks assigned to you and their schedules

Only your own run reports are written (to your own directory); nothing here
touches other agents or the server.
"""
import getpass
import json
import os
import re
import sys
import time

RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
MAX_NOTE = 500


def base_dir():
    return os.environ.get("AGENT_TASKS_DIR") or os.path.join("/var/lib/agentdesk-tasks", getpass.getuser())


def die(msg):
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    sys.exit(1)


def load_tasks():
    try:
        with open(os.path.join(base_dir(), "tasks.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data.get("tasks") if isinstance(data, dict) else None


def report(status, run_id, note):
    if not RUN_ID.match(run_id or ""):
        die("неверный номер запуска: берите его из сообщения о задаче")
    tasks = load_tasks()
    if tasks is not None and not any(run_id.startswith(str(t.get("id")) + "-") for t in tasks if isinstance(t, dict)):
        die("запуск %s не относится ни к одной вашей задаче" % run_id)
    directory = os.path.join(base_dir(), "reports")
    if not os.path.isdir(directory):
        die("каталог отчётов не найден: задачи для вас ещё не настроены")
    body = {"run": run_id, "status": status, "note": (note or "").strip()[:MAX_NOTE], "at": int(time.time())}
    final = os.path.join(directory, run_id + ".json")
    tmp = final + ".tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        os.replace(tmp, final)
    except OSError as e:
        die("не удалось записать отчёт: %s" % e)
    print(json.dumps({"ok": True, "run": run_id, "status": status}, ensure_ascii=False))


def describe(task):
    s = task.get("schedule") or {}
    kind = s.get("kind")
    when = {"cron": s.get("expr"), "interval": "каждые %s мин" % s.get("every_minutes"), "once": s.get("at")}.get(kind, "?")
    return {"id": task.get("id"), "name": task.get("name"), "when": when, "enabled": task.get("enabled", True)}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]
    if cmd in ("done", "fail"):
        if not rest:
            die("укажите номер запуска")
        report("done" if cmd == "done" else "failed", rest[0], " ".join(rest[1:]))
    elif cmd == "list":
        tasks = load_tasks()
        if tasks is None:
            die("задач для вас нет")
        print(json.dumps({"ok": True, "tasks": [describe(t) for t in tasks if isinstance(t, dict)]}, ensure_ascii=False, indent=1))
    else:
        die("неизвестная команда %s; доступны: done, fail, list" % cmd)


if __name__ == "__main__":
    main()
