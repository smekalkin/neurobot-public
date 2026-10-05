# agent-scheduler

Runs the agents' scheduled tasks **on the server the agents live on**. agentdesk
only keeps the definitions and shows the history; this daemon (one per server)
reads them and, when a task is due, submits its text to exactly one configured
executor of the logical agent. Claude is reached through its live tmux session;
Codex is reached through the local App Server socket. It keeps working when
agentdesk is down, and the load is spread over the servers instead of one
central scheduler.

Install: `agent_scheduler.py` to `/opt/agent-scheduler/`, `agent-scheduler.service`
to `/etc/systemd/system/`, `agent-task.py` (from `../agent-tools`) to
`/usr/local/bin/agent-task`; agentdesk does this automatically the first time a
task is assigned to an agent on a server (`office.EnsureScheduler`).

## Contract with agentdesk

Root: `/var/lib/agentdesk-tasks` (override `AGENT_TASKS_ROOT`).

| Path | Written by | Meaning |
| --- | --- | --- |
| `<user>/tasks.json` | agentdesk (root, 0644) | the agent's task definitions, see `example/tasks.json` |
| `<user>/trigger/<task-id>.<nonce>` | agentdesk (root) | "run now"; consumed (deleted) on the next tick |
| `<user>/reports/<run-id>.json` | the agent, via `agent-task` (user, 0700 dir) | `{"run","status":"done|failed","note"}` |
| `.state/<user>.json` | the scheduler (root, 0600) | what ran when; remembered across restarts |

Task fields: `id`, `name`, `prompt`, `schedule` (`cron` + `expr`, `interval` +
`every_minutes`, or `once` + `at` as local `YYYY-MM-DDTHH:MM`), `timezone`
(default: the file's), `enabled`, `created_at` (epoch), `retry`
(`max_attempts` 3, `interval_minutes` 5, `on_failure` true), `window_minutes`
(60: how long delivery is retried), `catchup_minutes` (60: how late a missed
slot may still run), `report_timeout_minutes` (0: do not wait for the agent's
report; otherwise wait that long, then retry or give up), and `executor`
(`auto`, `claude`, or `codex`). Old task files without `executor` use `auto`.

Executor addresses come from the same `AGENT_EXPORTER_CONFIG` fleet file used
by agent-exporter. Every matching `tenant` entry is one implementation of the
same logical agent. A Claude entry needs `tmux_socket` and `tmux_session`; a
Codex entry needs `codex_socket` and `workspace`. `auto` tries one executor at
a time (active backend first, then Claude, then Codex) and stops at the first
accepted delivery; it never fans a task out to both. Codex dispatch is refused
when the subscription has less than 10% remaining or its usage cannot be read.

Cron is the usual five fields (`*`, lists, ranges, steps, month/weekday names;
when both day-of-month and weekday are restricted either may match).

## What a run looks like

`pending` -> `delivered` -> `done` | `failed`, or `delivered_final` when the task
does not wait for a report; otherwise `failed`, `timeout`, `skipped` (the
previous run was still going), `missed` (too late to catch up) or `cancelled`
(the task was removed).

Claude text is only ever typed while the session shows its normal prompt, so a
task can never answer a question meant for a human. Codex rejects a second turn
while its dedicated scheduled-task thread is busy. A busy executor is retried
until the window closes. Reports are read from the agent's own directory without
following links and only if owned by the agent.

## Reporting back

Every 30 s the scheduler POSTs to `$AGENTDESK_URL/api/fleet/tasks` with the
server's bearer token (`AGENT_EXPORTER_TOKEN`, from `/etc/agent-exporter.env`):

```json
{"agents": [{"user": "exampleuser", "version": "<tasks.json version>", "error": "",
             "invalid": ["..."], "tasks": [{"id": "daily-digest", "next_run": 1759470000, "last_status": "done"}],
             "runs": [{"id": "daily-digest-1759456800", "task_id": "...", "executor": "claude", "status": "done", "...": "..."}]}]}
```

`version` lets agentdesk notice that an agent has an old schedule and send it
again. Runs are sent until a push succeeds; a run that changes is sent again.

Tests: `python3 -m unittest test_agent_scheduler` (and `test_agent_task` in
`../agent-tools`).
