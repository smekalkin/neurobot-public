#!/usr/bin/env python3
"""Fleet-status pusher for the automation-agent fleet on this host.

Every PUSH_INTERVAL seconds, POSTs one JSON array (one entry per agent) to
agentdesk's POST /api/fleet/heartbeat, authenticated with this host's own
bearer token (AGENT_EXPORTER_TOKEN). There is no Prometheus involved and
nothing is exposed over HTTP for anyone to scrape — agentdesk never
reaches out to this host to ask for status; this host calls agentdesk.

Three backend types, each with a different data path:

  claude  - liveness from the tmux session + transcript freshness + bad-state
            strings in the pane; model/effort from the session JSONL
            transcript; limit/balance from GET /api/oauth/usage with the
            account's own OAuth token.
  codex   - liveness/model/effort from the App Server (thread/list) plus the
            rollout JSONL `thread_settings_applied` event; limit/balance from
            the App Server `account/rateLimits/read` method.
  deepseek- no persistent session; balance from GET /user/balance.

Everything is best-effort: any probe that fails degrades that agent's entry
to a probe_errors entry instead of taking the whole push down.

Run as root (needs to read per-tenant credential files and tmux sockets).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PUSH_INTERVAL = int(os.environ.get("AGENT_EXPORTER_PUSH_INTERVAL", "30"))
# Fleet is config-driven so the same script deploys unmodified to every host —
# see hosts/*.fleet.json. AGENT_EXPORTER_SERVER, if set, overrides the "server"
# label from the config file (rarely needed; the file is the source of truth).
CONFIG_PATH = os.environ.get(
    "AGENT_EXPORTER_CONFIG",
    str(Path(__file__).resolve().parent / "hosts" / "example.fleet.json"),
)

# Bad-state strings that mean "the process is alive but the agent is stuck".
# These are what today's incidents actually looked like on the pane.
BAD_STATES = [
    ("not_logged_in", re.compile(r"Not logged in|/login", re.I)),
    ("resume_prompt", re.compile(r"resume from summary|Do you want to resume", re.I)),
    ("limit_reached", re.compile(r"usage limit reached|rate limit|hit your (?:session|usage) limit", re.I)),
    ("error_state", re.compile(r"Credit balance is too low|API Error", re.I)),
]

# A genuinely stuck state is always the LAST thing that happened (nothing
# can render after it -- the CLI is frozen), but `claude --continue`
# restores the whole prior transcript, so a since-resolved incident's own
# message (e.g. "Not logged in - Run /login") stays visible on screen,
# unchanged, for as long as it takes new conversation to scroll it out of
# view. Confirmed live TWICE (sample-developer, demo-developer): both
# kept reporting not_logged_in for real, successful exchanges after a
# working relogin, purely because that old line hadn't scrolled off yet.
#
# A fixed trailing-line-count window (tried first) is the wrong fix: how
# many lines one exchange takes varies a lot, and a short reply right
# after the fix (confirmed live on demo) can leave the old message well
# within even a 10-line tail. What actually distinguishes "still stuck"
# from "resolved, just not scrolled off yet" is whether a genuine NEW
# assistant turn (a line starting with the "●" turn marker) has rendered
# since -- nothing can follow a truly frozen state, but any real recovery
# always shows at least one more reply. (A user-prompt "❯" line is NOT a
# reliable signal on its own: the session-limit menu's own current
# selection also renders as "❯ 1. Stop and wait..." -- confirmed against
# the real example-developer-two capture, which would have been a false
# negative if a bare "❯" counted as "resolved".)
TURN_MARKER = "●"


def pane_is_stale_match(lines: List[str], match_index: int) -> bool:
    """Whether a BAD_STATES match at lines[match_index] is old, resolved
    history rather than the pane's current live state -- true when a real
    assistant turn has rendered anywhere after it."""
    return any(line.strip().startswith(TURN_MARKER) for line in lines[match_index + 1 :])


RESETS_RE = re.compile(r"resets?\s+(.+?)\s*(?:[│|╮╯┃]|$)", re.I)


def limit_reset_from_pane(lines: List[str], match_index: int) -> str:
    """When the usage limit lifts, as the CLI prints it after "resets"
    (e.g. "12:50pm (UTC)"); "" if the pane doesn't say. The time sits on the
    matched line itself, or just below it in the limit menu."""
    for line in lines[match_index : match_index + 4]:
        m = RESETS_RE.search(line)
        if m:
            return m.group(1).strip()
    return ""


@dataclass
class Agent:
    tenant: str
    agent: str
    backend: str                       # claude | codex | deepseek
    unit: Optional[str] = None
    user: Optional[str] = None
    # tmux_socket/tmux_session apply ONLY to a persistent interactive
    # `claude --remote-control --channels ...` session (e.g. example-developer).
    # A headless per-turn `claude` backend driven by bot.py's own run_claude()
    # (e.g. Example's developer agent) has no tmux pane at all — leave both
    # unset for that case; collect() skips the tmux/bad-state probe accordingly
    # and falls back to unit liveness + transcript freshness only. Do not
    # treat a missing tmux pane as a probe error when neither field is set.
    tmux_socket: Optional[str] = None
    tmux_session: Optional[str] = None
    transcript_dir: Optional[str] = None
    credentials: Optional[str] = None   # claude .credentials.json
    codex_socket: Optional[str] = None
    codex_home: Optional[str] = None
    # Working directory used when agent-scheduler creates this logical
    # agent's dedicated Codex thread. Required for Codex scheduled tasks.
    workspace: Optional[str] = None
    deepseek_env: Optional[str] = None  # file containing DEEPSEEK_API_KEY=
    # Deployment facts that don't come from a live probe: which Telegram bot
    # (if any) fronts this agent, which tenant-memory store it uses, and
    # whether remote-control is meant to be on. `remote_control` is a
    # fallback only — for a claude backend with a tmux pane, collect() checks
    # the pane's own status bar for the live "/rc" marker instead, since that
    # actually toggles per-conversation (see the Example developer2 case:
    # explicitly turned off without any config change). Leave unset (None)
    # for "unknown", not False — False renders as a real, checked "off".
    telegram_bot: Optional[str] = None
    memory: Optional[str] = None
    remote_control: Optional[bool] = None
    # Mirrors NEUROBOT_SHOW_MODEL_LABEL in the tenant's env file — whether
    # Telegram replies get the superscript model/effort label. No live
    # signal for this (it's a plain env flag, not visible on the pane), so
    # unlike remote_control this is always the static config value.
    show_model_label: Optional[bool] = None
    # For a "hybrid" tenant with several sibling Agent entries sharing one
    # tenant name but different backends (e.g. example-developer: claude,
    # codex, deepseek) — only one backend is actually routing messages at a
    # time (Telegram's /backend command switches it), tracked in this JSON
    # file's "channel_backend_mode" key. Point every sibling at the same
    # file; collect() compares its own `backend` against that value to
    # decide whether IT is the active one.
    backend_state_file: Optional[str] = None
    # Presentation-only facts for the agentdesk UI's card/grouping view.
    # role is the agent's function (Разработчик/Менеджер/...), derived from
    # what we already know it does — not invented. project is the
    # office/company it belongs to (Example/Example/...) — the only grouping
    # that actually exists in this system; there is no finer-grained
    # per-agent "project" concept to draw on.
    role: Optional[str] = None
    project: Optional[str] = None
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class BotUnit:
    unit: str
    # Optional execution backend the wrapper service depends on.  A Telegram
    # process can be alive and polling while its Codex Remote Control daemon
    # is unavailable; reporting that bot as online is then misleading.
    backend_unit: Optional[str] = None
    # Codex subscription profile shared by this wrapper.  A missing/empty/
    # malformed auth.json means the operator must sign in, which is more
    # actionable than the generic "backend unavailable" state.
    codex_home: Optional[str] = None
    telegram_bot: Optional[str] = None
    backend: str = "codex"
    model: Optional[str] = None
    effort: Optional[str] = None
    remote_control: Optional[bool] = None
    role: Optional[str] = None
    project: Optional[str] = None


def load_fleet_config(path: str) -> "tuple[str, List[Agent], Dict[str, BotUnit]]":
    """Fleet/bot-unit config is per-host JSON (hosts/<name>.fleet.json) so the
    exact same agent_exporter.py deploys unmodified to every host — only the
    config file differs. Shape: {"server": str, "fleet": [Agent-fields...],
    "bot_units": {tenant: unit-string-or-BotUnit-fields}}. A bare string is
    shorthand for {"unit": <string>} (accepted for backward compatibility)."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    server = os.environ.get("AGENT_EXPORTER_SERVER") or data["server"]
    fleet = [Agent(**entry) for entry in data.get("fleet", [])]
    bot_units = {}
    for tenant, entry in data.get("bot_units", {}).items():
        if isinstance(entry, str):
            entry = {"unit": entry}
        bot_units[tenant] = BotUnit(**entry)
    return server, fleet, bot_units


SERVER, FLEET, BOT_UNITS = load_fleet_config(CONFIG_PATH)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def run(cmd: List[str], timeout: int = 15, user: Optional[str] = None) -> Optional[str]:
    if user and user != "root":
        cmd = ["sudo", "-n", "-u", user] + cmd
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return out.stdout if out.returncode == 0 else None


def unit_active(unit: str) -> bool:
    out = run(["systemctl", "is-active", unit], timeout=10)
    return bool(out and out.strip() in ("active", "activating"))


def codex_logged_in(codex_home: Optional[str]) -> Optional[bool]:
    """Best-effort persistent-login check.

    None means the profile path is not configured; False is reserved for a
    configured auth.json that is absent, empty, malformed, or not an object.
    No credential content leaves the host.
    """
    if not codex_home:
        return None
    try:
        data = json.loads((Path(codex_home) / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and bool(data)


def is_active_backend(agent: Agent) -> Optional[bool]:
    """For a hybrid tenant with several sibling Agent entries (same tenant,
    different backend — e.g. example-developer: claude/codex/deepseek), only
    one backend is actually routing messages at a time; the rest exist as
    switchable capabilities, not concurrently-running alternatives. None
    (unknown) when the tenant isn't hybrid at all (no backend_state_file
    configured) or the file can't be read/parsed — never guess True/False
    from a missing signal."""
    if not agent.backend_state_file:
        return None
    try:
        with open(agent.backend_state_file, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    mode = state.get("channel_backend_mode")
    if not isinstance(mode, str):
        return None
    return mode == agent.backend


def tmux_capture(agent: Agent) -> Optional[str]:
    if not agent.tmux_socket or not agent.tmux_session:
        return None
    base = ["tmux"]
    if agent.tmux_socket != "default":
        base += ["-S", agent.tmux_socket]
    if run(base + ["has-session", "-t", agent.tmux_session], user=agent.user) is None:
        return None
    return run(base + ["capture-pane", "-p", "-t", agent.tmux_session], user=agent.user)


def recent_transcripts(directory: str, limit: int = 5) -> List[Path]:
    """Transcripts newest-first.

    More than one is needed because the newest file by mtime is often a
    short-lived side session (sub-agent, one-shot) whose entries are all
    synthetic; the real persistent session is then the next one down.
    """
    root = Path(directory)
    if not root.is_dir():
        return []
    found = []
    for path in root.glob("*/*.jsonl"):
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:limit]]


def tail_bytes(path: Path, size: int = 400_000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            total = handle.tell()
            handle.seek(max(0, total - size))
            return handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def http_json(url: str, headers: Dict[str, str], timeout: int = 20) -> Optional[Any]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


# --------------------------------------------------------------------------
# claude backend
# --------------------------------------------------------------------------

def claude_model_effort(agent: Agent) -> Dict[str, str]:
    """Live model + effort from the session's own JSONL transcript.

    Same technique the Telegram model-label feature already uses: Claude Code
    records `message.model` and a sibling `effort` key on every assistant
    turn, and effort can change mid-session via /effort, so this is read
    fresh rather than cached.
    """
    result = {"model": "", "effort": "", "transcript_age": ""}
    if not agent.transcript_dir:
        return result
    candidates = recent_transcripts(agent.transcript_dir)
    if not candidates:
        return result
    try:
        result["transcript_age"] = str(time.time() - candidates[0].stat().st_mtime)
    except OSError:
        pass
    for path in candidates:
        for line in reversed(tail_bytes(path).splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message")
            model = message.get("model") if isinstance(message, dict) else None
            # Claude Code writes synthetic assistant entries (interrupts,
            # local errors) carrying model "<synthetic>" and no effort — they
            # are not real turns, so keep walking back to a genuine one.
            if not model or str(model).startswith("<"):
                continue
            result["model"] = str(model)
            result["effort"] = str(entry.get("effort") or "")
            return result
    return result


def claude_model_from_pane(pane: str) -> str:
    """Best-effort model fallback for a brand-new interactive session.

    Claude does not write ``message.model`` to its transcript until the first
    assistant turn. The welcome header already shows the active model, e.g.
    ``Sonnet 5 · Claude Pro``. Reading that lets AgentDesk show the model
    immediately after provisioning without injecting a fake conversation.
    Once a real assistant turn exists, the transcript remains authoritative.
    """
    match = re.search(
        r"\b(Sonnet|Opus|Haiku)\s+([0-9]+(?:\.[0-9]+)?)\s+·\s+Claude\b",
        pane,
        re.I,
    )
    if not match:
        return ""
    family = match.group(1).lower()
    version = match.group(2).replace(".", "-")
    return "claude-{}-{}".format(family, version)


def claude_usage(agent: Agent) -> Dict[str, Any]:
    """Subscription quota via the same endpoint the CLI itself uses."""
    if not agent.credentials:
        return {}
    try:
        with open(agent.credentials, "r", encoding="utf-8") as handle:
            oauth = (json.load(handle) or {}).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return {}
    token = oauth.get("accessToken")
    if not token:
        return {}
    data = http_json(
        "https://api.anthropic.com/api/oauth/usage",
        {"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20"},
    )
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# codex backend
# --------------------------------------------------------------------------

# /opt/neurobot is the one path guaranteed identical on every host this
# exporter runs on (it's where bot.py itself is deployed from — see
# agent@.service and custom service units). A per-agent /srv/<agent> path is
# NOT guaranteed to exist on hosts with no tenant of that name.
sys.path.insert(0, "/opt/neurobot/src/neurobot")
try:
    from appserver_client import CodexAppServerClient  # type: ignore
except Exception:  # pragma: no cover - exporter still runs without codex
    CodexAppServerClient = None  # type: ignore


def codex_probe(agent: Agent) -> Dict[str, Any]:
    """Rate limits + thread status straight off the local App Server."""
    if CodexAppServerClient is None or not agent.codex_socket:
        return {}
    if not os.path.exists(agent.codex_socket):
        return {}
    out: Dict[str, Any] = {}
    try:
        with CodexAppServerClient(Path(agent.codex_socket), timeout=30) as client:
            out["rate_limits"] = client.request("account/rateLimits/read", None)
            out["threads"] = client.request("thread/list", {})
    except Exception:
        return out
    return out


def codex_model_effort(agent: Agent) -> Dict[str, str]:
    """Model + reasoning effort from the newest rollout JSONL.

    The App Server thread object exposes `modelProvider` but NOT the model or
    reasoning effort (verified against a live socket), so the rollout log's
    `thread_settings_applied` event is the only source for those two.
    """
    result = {"model": "", "effort": ""}
    if not agent.codex_home:
        return result
    sessions = Path(agent.codex_home) / "sessions"
    if not sessions.is_dir():
        return result
    best, best_mtime = None, -1.0
    for path in sessions.glob("*/*/*/rollout-*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    if best is None:
        return result
    for line in reversed(tail_bytes(best).splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = (json.loads(line).get("payload") or {})
        except json.JSONDecodeError:
            continue
        if payload.get("type") != "thread_settings_applied":
            continue
        settings = payload.get("thread_settings") or {}
        result["model"] = str(settings.get("model") or "")
        result["effort"] = str(settings.get("reasoning_effort") or "")
        break
    return result


# --------------------------------------------------------------------------
# deepseek backend
# --------------------------------------------------------------------------

def deepseek_balance(agent: Agent) -> Dict[str, Any]:
    if not agent.deepseek_env or not os.path.exists(agent.deepseek_env):
        return {}
    key = ""
    try:
        with open(agent.deepseek_env, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("DEEPSEEK_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        return {}
    if not key:
        return {}
    data = http_json("https://api.deepseek.com/user/balance",
                     {"Authorization": "Bearer " + key})
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# JSON collection — one dict per agent, matching agentdesk's fleet.Agent
# --------------------------------------------------------------------------

def _add_limit(limits: List[Dict[str, Any]], window: str, used_percent: float,
               resets_at: Optional[float] = None) -> None:
    entry: Dict[str, Any] = {"window": window, "used_percent": used_percent}
    if resets_at is not None:
        entry["resets_at"] = resets_at
    limits.append(entry)


def remote_control_from_pane(pane: str) -> Optional[bool]:
    """Whether Remote Control is live, read from the pane's status bar; None
    when the bar isn't showing so it can't be told.

    The bar ends in "/rc" (bare, or "/rc active") when it is on, and in
    "/rc failed" when the attempt to connect failed -- that one is OFF. The
    old check treated any "/rc" followed by whitespace as on, so "/rc failed"
    wrongly counted (found live on example-developer, 2026-09-23). An
    unrecognised word after "/rc" (e.g. a transient "connecting") is unknown.
    """
    if "bypass permissions" not in pane:
        return None
    for line in pane.splitlines():
        m = re.search(r"/rc(?:[ \t]+([A-Za-z]+))?[ \t]*$", line)
        if not m:
            continue
        word = (m.group(1) or "").lower()
        if word in ("", "active"):
            return True
        if word == "failed":
            return False
        return None
    return False


def bridge_connected(agent: Agent) -> Optional[bool]:
    """Read the running Claude process session file to determine whether
    Remote Control is connected. An empty bridgeSessionId means disconnected;
    a present identifier means connected. Return None when the state cannot be
    determined, allowing callers to fall back to terminal-pane inspection.
    """
    if not agent.tmux_socket or not agent.tmux_session:
        return None
    base = ["tmux"]
    if agent.tmux_socket != "default":
        base += ["-S", agent.tmux_socket]
    pid = run(base + ["display-message", "-p", "-t", agent.tmux_session, "#{pane_pid}"], user=agent.user)
    if not pid or not pid.strip().isdigit():
        return None
    root = None
    if agent.transcript_dir:
        root = Path(agent.transcript_dir).parent
    elif agent.credentials:
        root = Path(agent.credentials).parent
    if root is None:
        return None
    raw = run(["cat", str(root / "sessions" / (pid.strip() + ".json"))], user=agent.user)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return bool(data.get("bridgeSessionId"))


def collect_agents() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    for a in FLEET:
        up = unit_active(a.unit) if a.unit else True
        online = bool(up)
        model = effort = plan = ""
        bad_states: List[str] = []
        limit_reset = ""
        probe_errors: List[str] = []
        limits: List[Dict[str, Any]] = []
        balances: List[Dict[str, Any]] = []
        last_activity: Optional[float] = None

        # ---------------- claude ----------------
        remote_control: Optional[bool] = a.remote_control
        if a.backend == "claude":
            pane: Optional[str] = None
            # Only a persistent interactive session has a tmux pane to read.
            # A headless per-turn `claude` backend (no tmux_socket/session
            # configured) has nothing to capture — that is not a probe
            # failure, just a different deployment shape; fall back to unit
            # liveness (already in `online`) + transcript freshness below.
            if a.tmux_socket and a.tmux_session:
                pane = tmux_capture(a)
                if pane is None:
                    online = False
                    probe_errors.append("tmux")
                else:
                    pane_lines = pane.split("\n")
                    for state, pattern in BAD_STATES:
                        last_match = None
                        for i, line in enumerate(pane_lines):
                            if pattern.search(line):
                                last_match = i
                        if last_match is not None and not pane_is_stale_match(pane_lines, last_match):
                            bad_states.append(state)
                            online = False
                            if state == "limit_reached":
                                limit_reset = limit_reset_from_pane(pane_lines, last_match)
                    # The status bar shows a bare "/rc" (often truncated to
                    # just that on a narrow pane) or "/rc active" only when
                    # remote-control is actually on for THIS session — it
                    # toggles per-conversation independent of any config, so
                    # a live pane read is the only way to get this right
                    # (confirmed live: developer2 has it off with identical
                    # systemd/run-script config to developer, which has it on).
                    #
                    # Only trust this when the status bar is actually the
                    # thing on screen. Claude Code replaces it with a menu,
                    # spinner, or prompt during normal use (confirmed live:
                    # a mid-conversation multiple-choice menu on sample hid
                    # the bar entirely and briefly made remote-control look
                    # off) — no "/rc" then means "can't tell right now", not
                    # "off". "bypass permissions" is the stable anchor that's
                    # present whenever the real status bar is showing.
                    remote_control = remote_control_from_pane(pane)

            # The session file is the truth when it can answer; the pane read
            # above is only the fallback for CLIs that don't write one.
            bridge = bridge_connected(a)
            if bridge is not None:
                remote_control = bridge

            me = claude_model_effort(a)
            model, effort = me["model"], me["effort"]
            if not model and pane:
                model = claude_model_from_pane(pane)
            if me["transcript_age"]:
                last_activity = round(float(me["transcript_age"]), 1)

            usage = claude_usage(a)
            if not usage:
                probe_errors.append("usage_api")
            else:
                for window in ("five_hour", "seven_day"):
                    block = usage.get(window)
                    if isinstance(block, dict) and block.get("utilization") is not None:
                        resets_at = None
                        resets = block.get("resets_at")
                        if resets:
                            try:
                                resets_at = time.mktime(time.strptime(
                                    resets.split(".")[0], "%Y-%m-%dT%H:%M:%S"))
                            except ValueError:
                                pass
                        _add_limit(limits, window, float(block["utilization"]), resets_at)
                spend = usage.get("spend") or {}
                used = (spend.get("used") or {}) if isinstance(spend, dict) else {}
                if used.get("amount_minor") is not None:
                    exponent = used.get("exponent") or 2
                    balances.append({"kind": "spent",
                                      "usd": float(used["amount_minor"]) / (10 ** exponent)})
                extra = usage.get("extra_usage") or {}
                plan = "extra_usage_on" if extra.get("is_enabled") else "subscription"

        # ---------------- codex ----------------
        elif a.backend == "codex":
            logged_in = codex_logged_in(a.codex_home)
            if logged_in is False:
                online = False
                bad_states.append("not_logged_in")
            probe = codex_probe(a)
            rl = (probe.get("rate_limits") or {}).get("rateLimits") or {}
            if not rl:
                online = False
                probe_errors.append("app_server")
            else:
                plan = str(rl.get("planType") or "")
                primary = rl.get("primary") or {}
                if primary.get("usedPercent") is not None:
                    window = "w{}min".format(primary.get("windowDurationMins") or "?")
                    resets_at = float(primary["resetsAt"]) if primary.get("resetsAt") else None
                    _add_limit(limits, window, float(primary["usedPercent"]), resets_at)
                credits = rl.get("credits") or {}
                if credits.get("balance") is not None:
                    try:
                        balances.append({"kind": "credits", "usd": float(credits["balance"])})
                    except (TypeError, ValueError):
                        pass
                if rl.get("spendControlReached"):
                    online = False
                    bad_states.append("spend_control")

            threads = (probe.get("threads") or {}).get("data") or []
            if threads:
                newest = max(threads, key=lambda t: t.get("updatedAt") or 0)
                if newest.get("updatedAt"):
                    last_activity = round(time.time() - float(newest["updatedAt"]), 1)
            me = codex_model_effort(a)
            model, effort = me["model"], me["effort"]

        # ---------------- deepseek ----------------
        elif a.backend == "deepseek":
            data = deepseek_balance(a)
            if not data:
                online = False
                probe_errors.append("balance_api")
            else:
                available = bool(data.get("is_available"))
                online = available
                infos = data.get("balance_infos") or []
                for info in infos:
                    try:
                        balances.append({"kind": "total",
                                          "currency": str(info.get("currency") or ""),
                                          "usd": float(info.get("total_balance") or 0)})
                    except (TypeError, ValueError):
                        pass
                if not available:
                    bad_states.append("no_balance")
                model = "deepseek-v4-flash"

        active_backend = is_active_backend(a)
        out.append({
            "server": SERVER, "tenant": a.tenant, "agent": a.agent, "backend": a.backend,
            "up": bool(up), "online": online,
            "model": model, "effort": effort, "plan": plan,
            "telegram_bot": a.telegram_bot or "", "memory": a.memory or "",
            "remote_control": remote_control, "show_model_label": a.show_model_label,
            "active_backend": active_backend,
            "role": a.role or "", "project": a.project or "",
            "limits": limits, "balances": balances,
            "last_activity_seconds": last_activity,
            "bad_states": bad_states, "limit_reset": limit_reset,
            "probe_errors": probe_errors,
            # Connection facts a write-capable client (agentdesk) needs to
            # actually reach this agent -- e.g. to send a /model or /backend
            # switch -- as opposed to everything above, which is read-only
            # status. Sent here rather than duplicated into a second config
            # a human has to keep in sync with this file.
            "unit": a.unit or "", "os_user": a.user or "",
            "tmux_socket": a.tmux_socket or "", "tmux_session": a.tmux_session or "",
            "backend_state_file": a.backend_state_file or "",
            "task_capable": bool(
                (a.backend == "claude" and a.tmux_socket and a.tmux_session)
                or (a.backend == "codex" and a.codex_socket and a.workspace)
            ),
        })

    for tenant, bot in BOT_UNITS.items():
        bot_up = unit_active(bot.unit)
        backend_up = unit_active(bot.backend_unit) if bot.backend_unit else True
        logged_in = codex_logged_in(bot.codex_home)
        bad_states = []
        if not bot_up:
            bad_states.append("service_down")
        elif logged_in is False:
            bad_states.append("not_logged_in")
        elif not backend_up:
            bad_states.append("backend_down")
        out.append({
            "server": SERVER, "tenant": tenant, "agent": "telegram-bot", "backend": bot.backend,
            "up": bot_up, "online": bot_up and backend_up and logged_in is not False,
            "model": bot.model or "", "effort": bot.effort or "", "plan": "",
            "telegram_bot": bot.telegram_bot or "", "memory": "",
            "remote_control": bot.remote_control, "show_model_label": None, "active_backend": None,
            "role": bot.role or "", "project": bot.project or "",
            "task_capable": False,
            # A bot unit has no tmux pane or backend transcript to inspect,
            # but an inactive systemd service is itself a precise,
            # actionable failure. Report it explicitly so AgentDesk can say
            # "service stopped" instead of an unexplained "offline" and can
            # point the operator at the existing restart action.
            "bad_states": bad_states,
            "probe_errors": [],
        })

    return out


def push(heartbeat_url: str, token: str, agents: List[Dict[str, Any]]) -> None:
    body = json.dumps({"agents": agents}).encode("utf-8")
    req = urllib.request.Request(
        heartbeat_url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        sys.stderr.write("agent_exporter: push to {} failed: {}\n".format(heartbeat_url, exc))


def cycle(heartbeat_url: str, token: str) -> bool:
    """One collect-and-push round; reports whether anything was pushed.

    When collecting fails nothing is pushed. An empty push would replace what
    agentdesk last knew about this server with "no agents" and blank every
    status in the panel for a transient error; skipping it leaves the last
    known state, which the panel already shows as ageing ("last seen")."""
    try:
        agents = collect_agents()
    except Exception as exc:  # never let one bad probe kill the push loop
        sys.stderr.write("agent_exporter: collect failed: {}\n".format(exc))
        return False
    push(heartbeat_url, token, agents)
    return True


def main() -> None:
    if "--once" in sys.argv:
        sys.stdout.write(json.dumps(collect_agents(), indent=2, ensure_ascii=False))
        sys.stdout.write("\n")
        return

    agentdesk_url = os.environ.get("AGENTDESK_URL")
    token = os.environ.get("AGENT_EXPORTER_TOKEN")
    if not agentdesk_url or not token:
        sys.stderr.write("agent_exporter: AGENTDESK_URL and AGENT_EXPORTER_TOKEN must both be set\n")
        sys.exit(1)
    heartbeat_url = agentdesk_url.rstrip("/") + "/api/fleet/heartbeat"

    while True:
        cycle(heartbeat_url, token)
        time.sleep(PUSH_INTERVAL)


if __name__ == "__main__":
    main()
