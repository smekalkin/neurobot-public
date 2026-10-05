#!/usr/bin/env python3
"""Runs scheduled tasks for the agents on this server.

agentdesk (the panel) keeps the task definitions and writes, for every agent
that has tasks, /var/lib/agentdesk-tasks/<user>/tasks.json. This daemon -- one
per server, not one per agent -- reads those files and, when a task is due,
types its text into the agent's live tmux session, exactly like the operator
would. The schedule therefore keeps running when agentdesk is down, and the
work is spread over the servers the agents live on instead of one central
scheduler.

Layout (root is /var/lib/agentdesk-tasks, override AGENT_TASKS_ROOT):

  <user>/tasks.json        written by agentdesk (root-owned), the definitions
  <user>/trigger/<id>.*    "run now" requests, created by agentdesk (root-owned)
  <user>/reports/<run>.json  written by the agent itself via `agent-task`
  .state/<user>.json       this daemon's own bookkeeping (root only)

A run is one attempt series for one slot of one task:

  pending -> delivered -> done | failed        (the agent reported back)
          \\-> delivered   (terminal when the task does not wait for a report)
          \\-> failed | timeout | skipped | missed | cancelled

What is deliberate:
  * A task never starts while its previous run is still going (no pile-up).
  * Missed slots (daemon down, server off) run once if they are recent enough
    (catchup_minutes) and are otherwise recorded as "missed", never replayed in
    a burst.
  * Delivery only ever types into a session that sits at its normal prompt, so
    a task can never answer a question meant for a human. It is retried
    (retry.max_attempts, retry.interval_minutes) until the window closes.
  * Reports are read from the agent's own directory with O_NOFOLLOW and an
    ownership check: the agent is untrusted input here, root is the reader.
  * Everything is pushed to agentdesk (POST /api/fleet/tasks, bearer token of
    this server); agentdesk never has to reach out to ask.

Run as root (it types into other users' tmux sockets). Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from appserver_client import AppServerError, CodexAppServerClient
except ImportError:  # repository tests; deployed copy lives beside this file
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "neurobot"))
    from appserver_client import AppServerError, CodexAppServerClient

ROOT = Path(os.environ.get("AGENT_TASKS_ROOT", "/var/lib/agentdesk-tasks"))
TICK_SECONDS = int(os.environ.get("AGENT_SCHEDULER_TICK", "15"))
PUSH_SECONDS = int(os.environ.get("AGENT_SCHEDULER_PUSH_INTERVAL", "30"))

PROMPT_MARKER = "bypass permissions on"  # the status bar of an agent at its prompt
MAX_PROMPT_CHARS = 4000
MAX_REPORT_BYTES = 8192
KEEP_RUNS = 50              # finished runs remembered per agent
DEFAULT_WINDOW_MIN = 60     # how long delivery keeps being retried after the slot
DEFAULT_CATCHUP_MIN = 60    # how late a missed slot may still be run
DEFAULT_RETRY = {"max_attempts": 3, "interval_minutes": 5, "on_failure": True}

TERMINAL = {"done", "failed", "delivered_final", "skipped", "missed", "timeout", "cancelled"}
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
EXECUTORS = {"auto", "claude", "codex"}
CODEX_MIN_REMAINING_PERCENT = 10


# --------------------------------------------------------------------------
# cron
# --------------------------------------------------------------------------

class ScheduleError(ValueError):
    pass


_MONTHS = {n: i + 1 for i, n in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_DAYS = {n: i for i, n in enumerate("sun mon tue wed thu fri sat".split())}


def _field(text: str, lo: int, hi: int, names: Optional[Dict[str, int]] = None) -> Tuple[frozenset, bool]:
    """One cron field -> (allowed values, was it a bare '*')."""
    values = set()
    star = text == "*"
    for part in text.split(","):
        if not part:
            raise ScheduleError("empty list item in {!r}".format(text))
        step, stepped = 1, "/" in part
        if stepped:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError("bad step in {!r}".format(text))
            step = int(step_text)

        def num(token: str) -> int:
            token = token.lower()
            if names and token in names:
                return names[token]
            if not token.isdigit():
                raise ScheduleError("bad value {!r}".format(token))
            return int(token)

        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = num(a), num(b)
        else:
            start = num(part)
            end = hi if stepped else start      # "5/15" means 5, 20, 35, ...
        if start < lo or end > hi or start > end:
            raise ScheduleError("{!r} is outside {}-{}".format(text, lo, hi))
        values.update(range(start, end + 1, step))
    return frozenset(values), star


@dataclass(frozen=True)
class Cron:
    minutes: frozenset
    hours: frozenset
    dom: frozenset
    months: frozenset
    dow: frozenset          # 0 = Sunday
    dom_star: bool
    dow_star: bool

    @classmethod
    def parse(cls, expr: str) -> "Cron":
        parts = expr.split()
        if len(parts) != 5:
            raise ScheduleError("a cron expression has 5 fields, got {}".format(len(parts)))
        minutes, _ = _field(parts[0], 0, 59)
        hours, _ = _field(parts[1], 0, 23)
        dom, dom_star = _field(parts[2], 1, 31)
        months, _ = _field(parts[3], 1, 12, _MONTHS)
        dow_raw, dow_star = _field(parts[4], 0, 7, _DAYS)
        dow = frozenset(0 if d == 7 else d for d in dow_raw)
        return cls(minutes, hours, dom, months, dow, dom_star, dow_star)

    def day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.dom
        dow_ok = (d.isoweekday() % 7) in self.dow
        # Classic cron: when both day fields are restricted, either may match.
        if not self.dom_star and not self.dow_star:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def _times(self) -> List[Tuple[int, int]]:
        return sorted((h, m) for h in self.hours for m in self.minutes)

    def previous(self, now: datetime) -> Optional[datetime]:
        """The latest fire time <= now (same time zone as now)."""
        times = self._times()
        for back in range(0, 366 * 8):
            d = now.date() - timedelta(days=back)
            if not self.day_matches(d):
                continue
            for h, m in reversed(times):
                cand = datetime(d.year, d.month, d.day, h, m, tzinfo=now.tzinfo)
                if cand <= now:
                    return cand
        return None

    def following(self, now: datetime) -> Optional[datetime]:
        """The first fire time > now."""
        times = self._times()
        for ahead in range(0, 366 * 8):
            d = now.date() + timedelta(days=ahead)
            if not self.day_matches(d):
                continue
            for h, m in times:
                cand = datetime(d.year, d.month, d.day, h, m, tzinfo=now.tzinfo)
                if cand > now:
                    return cand
        return None


# --------------------------------------------------------------------------
# tasks and their slots
# --------------------------------------------------------------------------

@dataclass
class Task:
    id: str
    name: str
    prompt: str
    kind: str                      # cron | interval | once
    expr: str                      # cron expression
    every_minutes: int
    at: str                        # once: local "YYYY-MM-DDTHH:MM[:SS]"
    tz: str
    enabled: bool
    created_at: float
    max_attempts: int
    retry_interval: int
    retry_on_failure: bool
    window_minutes: int
    catchup_minutes: int
    report_timeout: int            # minutes to wait for the agent's report; 0 = do not wait
    executor: str                  # auto | claude | codex

    def signature(self) -> str:
        raw = json.dumps([self.kind, self.expr, self.every_minutes, self.at, self.tz], sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def parse_task(raw: Dict[str, Any], default_tz: str) -> Task:
    """Validate one task from tasks.json; raises ScheduleError with the reason."""
    if not isinstance(raw, dict):
        raise ScheduleError("a task must be an object")
    tid = str(raw.get("id") or "")
    if not TASK_ID_RE.match(tid):
        raise ScheduleError("bad task id {!r}".format(tid))
    prompt = str(raw.get("prompt") or "").strip()
    if not prompt:
        raise ScheduleError("task {} has no text".format(tid))
    sched = raw.get("schedule") or {}
    kind = str(sched.get("kind") or "")
    tz = str(raw.get("timezone") or default_tz or "UTC")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleError("unknown time zone {!r}".format(tz))
    expr, every, at = "", 0, ""
    if kind == "cron":
        expr = str(sched.get("expr") or "").strip()
        if Cron.parse(expr).following(datetime.now(ZoneInfo(tz))) is None:
            raise ScheduleError("the expression {!r} never fires".format(expr))
    elif kind == "interval":
        try:
            every = int(sched.get("every_minutes"))
        except (TypeError, ValueError):
            raise ScheduleError("interval needs every_minutes")
        if every < 1:
            raise ScheduleError("every_minutes must be at least 1")
        if every > 366 * 24 * 60:
            raise ScheduleError("every_minutes cannot be more than a year")
    elif kind == "once":
        at = str(sched.get("at") or "").strip()
        try:
            datetime.fromisoformat(at)
        except ValueError:
            raise ScheduleError("once needs a local time like 2026-10-03T09:00")
    else:
        raise ScheduleError("unknown schedule kind {!r}".format(kind))
    retry = dict(DEFAULT_RETRY)
    retry.update(raw.get("retry") or {})
    try:
        max_attempts = max(1, min(int(retry["max_attempts"]), 20))
        retry_interval = max(1, min(int(retry["interval_minutes"]), 24 * 60))
        window = max(1, min(int(raw.get("window_minutes") or DEFAULT_WINDOW_MIN), 7 * 24 * 60))
        catchup = max(0, min(int(raw.get("catchup_minutes") if raw.get("catchup_minutes") is not None else DEFAULT_CATCHUP_MIN), 30 * 24 * 60))
        report_timeout = max(0, min(int(raw.get("report_timeout_minutes") or 0), 7 * 24 * 60))
        created = float(raw.get("created_at") or 0)
    except (TypeError, ValueError):
        raise ScheduleError("a number in task {} is not a number".format(tid))
    executor = str(raw.get("executor") or "auto").strip().lower()
    if executor not in EXECUTORS:
        raise ScheduleError("unknown executor {!r}".format(executor))
    return Task(
        id=tid, name=str(raw.get("name") or tid)[:120], prompt=prompt, kind=kind, expr=expr,
        every_minutes=every, at=at, tz=tz, enabled=bool(raw.get("enabled", True)), created_at=created,
        max_attempts=max_attempts, retry_interval=retry_interval, retry_on_failure=bool(retry.get("on_failure", True)),
        window_minutes=window, catchup_minutes=catchup, report_timeout=report_timeout,
        executor=executor,
    )


def _local(ts: float, tz: str) -> datetime:
    return datetime.fromtimestamp(ts, ZoneInfo(tz))


def previous_slot(task: Task, now: float) -> Optional[float]:
    """The latest scheduled moment <= now, as epoch seconds; None if there is none."""
    if task.kind == "cron":
        prev = Cron.parse(task.expr).previous(_local(now, task.tz))
        return prev.timestamp() if prev else None
    if task.kind == "interval":
        step = task.every_minutes * 60
        anchor = task.created_at or now
        if now < anchor + step:
            return None
        return anchor + ((now - anchor) // step) * step
    at = datetime.fromisoformat(task.at).replace(tzinfo=ZoneInfo(task.tz)).timestamp()
    return at if at <= now else None


def next_slot(task: Task, now: float) -> Optional[float]:
    """The first scheduled moment > now; None when the task will never fire again."""
    if task.kind == "cron":
        nxt = Cron.parse(task.expr).following(_local(now, task.tz))
        return nxt.timestamp() if nxt else None
    if task.kind == "interval":
        step = task.every_minutes * 60
        anchor = task.created_at or now
        if now < anchor + step:
            return anchor + step
        return anchor + ((now - anchor) // step + 1) * step
    at = datetime.fromisoformat(task.at).replace(tzinfo=ZoneInfo(task.tz)).timestamp()
    return at if at > now else None


# --------------------------------------------------------------------------
# delivery: typing into the agent's tmux session
# --------------------------------------------------------------------------

def tmux_run(args: List[str], timeout: int = 15) -> Tuple[int, str]:
    try:
        out = subprocess.run(["tmux"] + args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, str(exc)
    return out.returncode, out.stdout if out.returncode == 0 else out.stderr


def flatten(text: str, limit: int = MAX_PROMPT_CHARS) -> str:
    """One line, no control characters: a newline typed into the CLI would
    submit half a message."""
    cleaned = "".join(" " if unicodedata.category(c).startswith("C") else c for c in text)
    return " ".join(cleaned.split())[:limit]


def user_home(user: str) -> str:
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return "/home/{}".format(user)


def deliver_tmux(socket: str, session: str, text: str, run=tmux_run) -> Tuple[bool, str]:
    base = [] if socket == "default" else ["-S", socket]
    code, pane = run(base + ["capture-pane", "-p", "-t", session])
    if code != 0:
        return False, "сессия Claude не найдена"
    if PROMPT_MARKER not in pane.lower():
        return False, "Claude занят: сессия не на обычной строке ввода"
    code, err = run(base + ["send-keys", "-t", session, "-l", "--", flatten(text)])
    if code != 0:
        return False, "не удалось ввести текст в сессию Claude: " + err.strip()[:120]
    code, err = run(base + ["send-keys", "-t", session, "Enter"])
    if code != 0:
        return False, "не удалось отправить Enter в Claude: " + err.strip()[:120]
    return True, ""


def deliver(user: str, unit: str, text: str, run=tmux_run) -> Tuple[bool, str]:
    """Type text into the agent's session. (True, "") on success, otherwise
    (False, why) -- and nothing was typed."""
    socket = "{}/.claude/{}.tmux.sock".format(user_home(user), unit)
    return deliver_tmux(socket, unit, text, run)


@dataclass(frozen=True)
class ExecutorRoute:
    backend: str
    tmux_socket: str = ""
    tmux_session: str = ""
    codex_socket: str = ""
    workspace: str = ""
    backend_state_file: str = ""


def load_executor_routes(tenant: str, config_path: Optional[str] = None) -> List[ExecutorRoute]:
    """Read the same per-host inventory as agent-exporter.

    The tenant is the logical agent; each matching fleet entry is one possible
    executor. No credential or token is read here, only local socket/session
    addresses and the Codex working directory.
    """
    path = config_path or os.environ.get("AGENT_EXPORTER_CONFIG", "")
    raw = read_json(Path(path)) if path else None
    if not isinstance(raw, dict):
        return []
    out = []
    for item in raw.get("fleet") or []:
        if not isinstance(item, dict) or str(item.get("tenant") or "") != tenant:
            continue
        backend = str(item.get("backend") or "").lower()
        if backend not in {"claude", "codex"}:
            continue
        route = ExecutorRoute(
            backend=backend,
            tmux_socket=str(item.get("tmux_socket") or ""),
            tmux_session=str(item.get("tmux_session") or ""),
            codex_socket=str(item.get("codex_socket") or ""),
            workspace=str(item.get("workspace") or ""),
            backend_state_file=str(item.get("backend_state_file") or ""),
        )
        if (backend == "claude" and route.tmux_socket and route.tmux_session) or (
            backend == "codex" and route.codex_socket and route.workspace
        ):
            out.append(route)
    return out


def active_executor(routes: List[ExecutorRoute]) -> str:
    for route in routes:
        if not route.backend_state_file:
            continue
        state = read_json(Path(route.backend_state_file))
        if isinstance(state, dict):
            mode = str(state.get("channel_backend_mode") or "").lower()
            if mode in {"claude", "codex"}:
                return mode
    return ""


def codex_limit_allowed(snapshot: Dict[str, Any]) -> Tuple[bool, str]:
    by_id = snapshot.get("rateLimitsByLimitId") or {}
    limits = by_id.get("codex") if isinstance(by_id, dict) else None
    if not isinstance(limits, dict):
        limits = snapshot.get("rateLimits") or {}
    if not isinstance(limits, dict) or not limits:
        return False, "Codex не сообщил остаток лимита"
    remaining = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if isinstance(window, dict) and "usedPercent" in window:
            try:
                remaining.append(100 - max(0, min(100, int(window["usedPercent"]))))
            except (TypeError, ValueError):
                pass
    individual = limits.get("individualLimit")
    if isinstance(individual, dict) and "remainingPercent" in individual:
        try:
            remaining.append(max(0, min(100, int(individual["remainingPercent"]))))
        except (TypeError, ValueError):
            pass
    credits = limits.get("credits") or {}
    if not remaining and isinstance(credits, dict) and credits.get("unlimited"):
        return True, ""
    if not remaining:
        return False, "Codex не сообщил измеримый остаток лимита"
    left = min(remaining)
    if limits.get("spendControlReached") or limits.get("rateLimitReachedType") or left < CODEX_MIN_REMAINING_PERCENT:
        return False, "у Codex осталось {}% лимита (минимум {}%)".format(left, CODEX_MIN_REMAINING_PERCENT)
    return True, ""


def compose_prompt(task: Task, run_id: str) -> str:
    return (
        "[Задача по расписанию «{name}», запуск {run}] {prompt} "
        "Когда закончишь, вызови `agent-task done {run} \"краткий итог\"`; "
        "если не получилось — `agent-task fail {run} \"причина\"`."
    ).format(name=task.name, run=run_id, prompt=task.prompt)


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------

def read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def read_report(directory: Path, run_id: str, uid: Optional[int]) -> Optional[Dict[str, Any]]:
    """The agent's own report for a run, or None. The directory belongs to the
    agent, so the file is only trusted if it is a plain, small file owned by the
    agent; symlinks are never followed (a link to a root-only file would
    otherwise be read by root and pushed out)."""
    if not RUN_ID_RE.match(run_id):
        return None
    path = directory / (run_id + ".json")
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_REPORT_BYTES:
            return None
        if uid is not None and st.st_uid != uid:
            return None
        data = json.loads(os.read(fd, MAX_REPORT_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if not isinstance(data, dict) or data.get("run") != run_id or data.get("status") not in ("done", "failed"):
        return None
    return {"status": data["status"], "note": str(data.get("note") or "")[:500]}


def tasks_version(raw: Any) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------

class AgentState:
    """One agent's definitions + bookkeeping for a tick."""

    def __init__(self, user: str):
        self.user = user
        self.unit = user
        self.tenant = user
        self.version = ""
        self.error = ""
        self.tasks: List[Task] = []
        self.invalid: List[str] = []
        self.state: Dict[str, Any] = {"tasks": {}, "runs": [], "seq": 0}
        self.dirty_state = False


class Scheduler:
    def __init__(self, root: Path = ROOT, now: Callable[[], float] = time.time,
                 send: Optional[Callable[..., Tuple[bool, str]]] = None,
                 uid_of: Optional[Callable[[str], Optional[int]]] = None):
        self.root = root
        self.now = now
        self.send = send
        self.uid_of = uid_of or self._uid_of
        self._cache: Dict[str, Tuple[float, Any]] = {}

    # -- loading ---------------------------------------------------------

    @staticmethod
    def _uid_of(user: str) -> Optional[int]:
        try:
            import pwd
            return pwd.getpwnam(user).pw_uid
        except (KeyError, ImportError):
            return None

    def users(self) -> List[str]:
        try:
            names = sorted(p.name for p in self.root.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError:
            return []
        return [n for n in names if USER_RE.match(n) and (self.root / n / "tasks.json").is_file()]

    def load(self, user: str) -> AgentState:
        agent = AgentState(user)
        raw = read_json(self.root / user / "tasks.json")
        if not isinstance(raw, dict):
            agent.error = "tasks.json is unreadable"
        else:
            agent.version = str(raw.get("version") or tasks_version(raw))
            agent.unit = str(raw.get("unit") or user)
            agent.tenant = str(raw.get("tenant") or agent.unit or user)
            default_tz = str(raw.get("timezone") or "UTC")
            for item in raw.get("tasks") or []:
                try:
                    agent.tasks.append(parse_task(item, default_tz))
                except ScheduleError as exc:
                    agent.invalid.append(str(exc))
        state = read_json(self.root / ".state" / (user + ".json"))
        if isinstance(state, dict) and isinstance(state.get("runs"), list) and isinstance(state.get("tasks"), dict):
            agent.state = state
            agent.state.setdefault("seq", 0)
        return agent

    def save(self, agent: AgentState) -> None:
        if agent.dirty_state:
            write_json_atomic(self.root / ".state" / (agent.user + ".json"), agent.state)
            agent.dirty_state = False

    # -- runs ------------------------------------------------------------

    def _new_run(self, agent: AgentState, task: Task, slot: float, kind: str, status: str = "pending", reason: str = "") -> Dict[str, Any]:
        agent.state["seq"] += 1
        run = {
            "id": "{}-{}{}".format(task.id, int(slot), "-m{}".format(agent.state["seq"]) if kind == "manual" else ""),
            "task_id": task.id, "task_name": task.name, "slot": slot, "kind": kind,
            "status": status, "attempts": 0, "next_attempt_at": slot, "reason": reason, "note": "",
            "started_at": None, "delivered_at": None, "finished_at": None, "seq": agent.state["seq"], "pushed": False,
        }
        if status in TERMINAL:
            run["finished_at"] = self.now()
        agent.state["runs"].append(run)
        agent.dirty_state = True
        return run

    @staticmethod
    def _touch(agent: AgentState, run: Dict[str, Any]) -> None:
        agent.state["seq"] += 1
        run["seq"] = agent.state["seq"]
        run["pushed"] = False
        agent.dirty_state = True

    def _finish(self, agent: AgentState, run: Dict[str, Any], status: str, reason: str = "", note: str = "") -> None:
        run["status"] = status
        run["finished_at"] = self.now()
        if reason:
            run["reason"] = reason
        if note:
            run["note"] = note
        self._touch(agent, run)

    def _active_run(self, agent: AgentState, task_id: str) -> Optional[Dict[str, Any]]:
        for run in agent.state["runs"]:
            if run["task_id"] == task_id and run["status"] not in TERMINAL:
                return run
        return None

    def _codex(self, agent: AgentState, route: ExecutorRoute, text: str) -> Tuple[bool, str]:
        socket_path = Path(route.codex_socket)
        if not socket_path.exists():
            return False, "сессия Codex не найдена"
        workspace = Path(route.workspace)
        if not workspace.is_dir():
            return False, "рабочий каталог Codex не найден: {}".format(route.workspace)
        try:
            with CodexAppServerClient(socket_path, timeout=30) as client:
                allowed, reason = codex_limit_allowed(client.read_account_rate_limits())
                if not allowed:
                    return False, reason
                executors = agent.state.setdefault("executors", {})
                codex = executors.setdefault("codex", {})
                thread_id = str(codex.get("thread_id") or "")
                if not thread_id:
                    thread_id = client.start_thread(workspace, "{} · scheduled".format(agent.tenant))
                    codex["thread_id"] = thread_id
                    agent.dirty_state = True
                try:
                    client.start_turn(thread_id, text)
                except AppServerError as exc:
                    # A deleted/expired thread is recoverable. A busy thread is
                    # not: creating another would run the same logical agent in
                    # parallel and defeat the single-executor guarantee.
                    low = str(exc).lower()
                    if "not found" not in low and "does not exist" not in low:
                        raise
                    thread_id = client.start_thread(workspace, "{} · scheduled".format(agent.tenant))
                    codex["thread_id"] = thread_id
                    agent.dirty_state = True
                    client.start_turn(thread_id, text)
            return True, ""
        except (AppServerError, OSError, ValueError) as exc:
            return False, "Codex не принял задачу: {}".format(str(exc)[:240])

    def _dispatch(self, agent: AgentState, task: Task, text: str) -> Tuple[bool, str, str]:
        # Tests and embedders may provide the old delivery seam. It represents
        # one Claude executor and keeps the scheduler engine independently
        # testable without real tmux/App Server sockets.
        if self.send is not None:
            ok, reason = self.send(agent.user, agent.unit, text)
            return ok, reason, task.executor if task.executor != "auto" else "claude"

        routes = load_executor_routes(agent.tenant)
        if not routes:
            # Backward-compatible fallback for servers whose exporter config
            # predates executor inventory. It can only address standard Claude.
            if task.executor == "codex":
                return False, "исполнитель Codex не настроен для этого агента", "codex"
            ok, reason = deliver(agent.user, agent.unit, text)
            return ok, reason, "claude"

        order = [task.executor] if task.executor != "auto" else []
        if task.executor == "auto":
            preferred = active_executor(routes)
            if preferred:
                order.append(preferred)
            order.extend(["claude", "codex"])
        order = list(dict.fromkeys(order))
        problems = []
        for backend in order:
            candidates = [r for r in routes if r.backend == backend]
            if not candidates:
                problems.append("{}: не настроен".format(backend))
                continue
            for route in candidates:
                if backend == "claude":
                    ok, reason = deliver_tmux(route.tmux_socket, route.tmux_session, text)
                else:
                    ok, reason = self._codex(agent, route, text)
                if ok:
                    return True, "", backend
                problems.append("{}: {}".format(backend, reason))
            if task.executor != "auto":
                break
        return False, "; ".join(problems)[:600] or "нет доступного исполнителя", task.executor

    # -- one tick for one agent -------------------------------------------

    def tick_agent(self, agent: AgentState) -> None:
        now = self.now()
        by_id = {t.id: t for t in agent.tasks}
        # Runs of tasks that no longer exist are cancelled, not left dangling.
        for run in agent.state["runs"]:
            if run["status"] not in TERMINAL and run["task_id"] not in by_id:
                self._finish(agent, run, "cancelled", "задача удалена")
        for gone in [t for t in agent.state["tasks"] if t not in by_id]:
            del agent.state["tasks"][gone]
            agent.dirty_state = True
        for task in agent.tasks:
            self._schedule(agent, task, now)
        self._triggers(agent, by_id, now)
        for run in list(agent.state["runs"]):
            task = by_id.get(run["task_id"])
            if run["status"] not in TERMINAL and task is not None:
                self._advance(agent, task, run, now)
        self._trim(agent)

    def _schedule(self, agent: AgentState, task: Task, now: float) -> None:
        mem = agent.state["tasks"].setdefault(task.id, {})
        slot = previous_slot(task, now)
        sig = task.signature()
        if mem.get("sig") != sig:
            # What is owed from before this moment? For a brand-new task, the
            # slots after it was created; for a changed schedule, nothing.
            if mem.get("sig") is None and task.created_at:
                owed_after = previous_slot(task, task.created_at)
            else:
                owed_after = slot
            mem.update(sig=sig, last_slot=owed_after or 0.0, finished=False)
            agent.dirty_state = True
        if not task.enabled:
            if slot is not None and mem.get("last_slot") != slot:
                mem["last_slot"] = slot       # nothing is owed for time spent disabled
                agent.dirty_state = True
            return
        if slot is None or slot <= mem.get("last_slot", 0.0) or mem.get("finished"):
            return
        mem["last_slot"] = slot
        if task.kind == "once":
            mem["finished"] = True
        agent.dirty_state = True
        if self._active_run(agent, task.id):
            self._new_run(agent, task, slot, "scheduled", "skipped", "предыдущий запуск ещё не завершён")
        elif now - slot > task.catchup_minutes * 60:
            self._new_run(agent, task, slot, "scheduled", "missed", "сервер или планировщик были недоступны")
        else:
            self._new_run(agent, task, slot, "scheduled")

    def _triggers(self, agent: AgentState, by_id: Dict[str, Task], now: float) -> None:
        directory = self.root / agent.user / "trigger"
        try:
            files = sorted(directory.iterdir())
        except OSError:
            return
        for path in files:
            task = by_id.get(path.name.split(".", 1)[0])
            try:
                path.unlink()
            except OSError:
                continue
            if task is None:
                continue
            if self._active_run(agent, task.id):
                self._new_run(agent, task, now, "manual", "skipped", "предыдущий запуск ещё не завершён")
            else:
                self._new_run(agent, task, now, "manual")

    def _advance(self, agent: AgentState, task: Task, run: Dict[str, Any], now: float) -> None:
        if run["status"] == "delivered":
            self._await_report(agent, task, run, now)
            return
        if now < run["next_attempt_at"]:
            return
        if now > run["slot"] + task.window_minutes * 60 and run["attempts"] > 0:
            self._finish(agent, run, "failed", run["reason"] or "окно запуска закрыто")
            return
        run["attempts"] += 1
        if run["started_at"] is None:
            run["started_at"] = now
        ok, why, executor = self._dispatch(agent, task, compose_prompt(task, run["id"]))
        run["executor"] = executor
        if ok:
            run["delivered_at"] = now
            run["reason"] = ""
            if task.report_timeout > 0:
                run["status"] = "delivered"
                self._touch(agent, run)
            else:
                self._finish(agent, run, "delivered_final")
            self.save(agent)    # remember it now: after a crash the text must not be typed twice
            return
        run["reason"] = why
        if run["attempts"] >= task.max_attempts or now + task.retry_interval * 60 > run["slot"] + task.window_minutes * 60:
            self._finish(agent, run, "failed", why)
        else:
            run["next_attempt_at"] = now + task.retry_interval * 60
            self._touch(agent, run)

    def _await_report(self, agent: AgentState, task: Task, run: Dict[str, Any], now: float) -> None:
        report = read_report(self.root / agent.user / "reports", run["id"], self.uid_of(agent.user))
        if report:
            # A valid report is consumed exactly once. Keeping one file per
            # completed scheduled run forever would otherwise grow this
            # user-writable directory without bound.
            self._forget_report(agent, run)
            if report["status"] == "done":
                self._finish(agent, run, "done", note=report["note"])
                return
            if task.retry_on_failure and run["attempts"] < task.max_attempts:
                run.update(status="pending", next_attempt_at=now + task.retry_interval * 60, reason="агент сообщил: " + report["note"])
                self._touch(agent, run)
                return
            self._finish(agent, run, "failed", "агент сообщил о неудаче", report["note"])
            return
        if now > run["delivered_at"] + task.report_timeout * 60:
            if run["attempts"] < task.max_attempts:
                run.update(status="pending", next_attempt_at=now, reason="агент не ответил за {} мин".format(task.report_timeout))
                self._touch(agent, run)
            else:
                self._finish(agent, run, "timeout", "агент не сообщил о результате")

    def _forget_report(self, agent: AgentState, run: Dict[str, Any]) -> None:
        try:
            (self.root / agent.user / "reports" / (run["id"] + ".json")).unlink()
        except OSError:
            pass

    @staticmethod
    def _trim(agent: AgentState) -> None:
        runs = agent.state["runs"]
        finished = [r for r in runs if r["status"] in TERMINAL]
        drop = {id(r) for r in [r for r in finished if r["pushed"]][: max(0, len(finished) - KEEP_RUNS)]}
        # If agentdesk cannot be reached for a long time, still bound what is kept.
        drop |= {id(r) for r in finished[: max(0, len(finished) - KEEP_RUNS * 10)]}
        if drop:
            agent.state["runs"] = [r for r in runs if id(r) not in drop]
            agent.dirty_state = True

    # -- the whole server -------------------------------------------------

    def tick(self) -> List[AgentState]:
        agents = []
        for user in self.users():
            agent = self.load(user)
            if not agent.error:
                try:
                    self.tick_agent(agent)
                except Exception as exc:  # one agent's problem must not stop the others
                    sys.stderr.write("agent_scheduler: {}: {}\n".format(user, exc))
                    agent.error = str(exc)
            self.save(agent)
            agents.append(agent)
        return agents

    # -- what agentdesk is told ------------------------------------------

    def report(self, agents: List[AgentState]) -> Dict[str, Any]:
        now = self.now()
        out = []
        for agent in agents:
            tasks = []
            for task in agent.tasks:
                mem = agent.state["tasks"].get(task.id, {})
                last = [r for r in agent.state["runs"] if r["task_id"] == task.id]
                tasks.append({
                    "id": task.id,
                    "next_run": None if (not task.enabled or mem.get("finished")) else next_slot(task, now),
                    "last_status": last[-1]["status"] if last else "",
                })
            out.append({
                "user": agent.user, "version": agent.version, "error": agent.error,
                "invalid": agent.invalid, "tasks": tasks,
                "runs": [{k: v for k, v in r.items() if k != "pushed"} for r in agent.state["runs"] if not r["pushed"]],
            })
        return {"agents": out}

    def mark_pushed(self, agents: List[AgentState], payload: Dict[str, Any]) -> None:
        sent = {(a["user"], r["id"]): r["seq"] for a in payload["agents"] for r in a["runs"]}
        for agent in agents:
            for run in agent.state["runs"]:
                if sent.get((agent.user, run["id"])) == run["seq"]:
                    run["pushed"] = True
                    agent.dirty_state = True
            self.save(agent)


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def push(url: str, token: str, payload: Dict[str, Any], opener=urllib.request.urlopen) -> bool:
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with opener(req, timeout=15) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        sys.stderr.write("agent_scheduler: push to {} failed: {}\n".format(url, exc))
        return False


def run_once(scheduler: Scheduler, url: Optional[str], token: Optional[str], force_push: bool = False,
             opener=urllib.request.urlopen) -> bool:
    agents = scheduler.tick()
    if not (url and token) or not agents:
        return False
    payload = scheduler.report(agents)
    if push(url, token, payload, opener):
        scheduler.mark_pushed(agents, payload)
        return True
    return False


def main() -> None:
    url_base = os.environ.get("AGENTDESK_URL", "").rstrip("/")
    token = os.environ.get("AGENT_EXPORTER_TOKEN", "")
    url = url_base + "/api/fleet/tasks" if url_base else None
    scheduler = Scheduler(root=ROOT)
    if "--once" in sys.argv:
        agents = scheduler.tick()
        sys.stdout.write(json.dumps(scheduler.report(agents), ensure_ascii=False, indent=1) + "\n")
        return
    last_push = 0.0
    while True:
        try:
            agents = scheduler.tick()
            if url and token and time.time() - last_push >= PUSH_SECONDS:
                payload = scheduler.report(agents)
                if push(url, token, payload):
                    scheduler.mark_pushed(agents, payload)
                last_push = time.time()
        except Exception as exc:  # never let a bad tick kill the daemon
            sys.stderr.write("agent_scheduler: tick failed: {}\n".format(exc))
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
