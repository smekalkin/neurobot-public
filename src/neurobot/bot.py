#!/usr/bin/env python3
"""Telegram bridge between topic-based automation agents and their executor.

Executor is Codex App Server by default, Claude Code (headless CLI), or a
hybrid Claude→Codex dispatcher. See run_turn/create_session/
read_rate_limit_status below and claude_executor.py for the Claude side.
"""

import hashlib
import json
import logging
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from .appserver_client import CodexAppServerClient
    from .claude_executor import ClaudeExecutorClient
except ImportError:  # Allows direct execution: python3 src/neurobot/bot.py
    from appserver_client import CodexAppServerClient
    from claude_executor import ClaudeExecutorClient


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TARGET_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
TARGET_THREAD_ID = int(os.environ.get("TELEGRAM_THREAD_ID", "0"))
ALLOWED_USER_IDS: Set[int] = {
    int(value.strip())
    for value in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
    if value.strip()
}
ALLOW_ALL_CHAT_MEMBERS = os.environ.get(
    "TELEGRAM_ALLOW_ALL_CHAT_MEMBERS", "false"
).strip().lower() in ("1", "true", "yes", "on")
BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME", "NeurobotAgentBot").strip().lstrip("@")
WORKSPACE = Path(
    os.environ.get("AGENT_WORKSPACE", "/srv/neurobot-agent")
)
STATE_DIR = WORKSPACE / "state"
STATE_FILE = STATE_DIR / "bot-state.json"
# Analytics side-channel only — see log_backend_usage below. Lives next to
# STATE_FILE in the same already-writable STATE_DIR (not a new/separate
# path) specifically to avoid repeating an earlier misplaced-state-file
# headache. Append-only JSONL, never read-modify-write.
BACKEND_USAGE_LOG_FILE = STATE_DIR / "backend-usage.jsonl"
CODEX_HOME = os.environ.get("CODEX_HOME", str(WORKSPACE / ".codex"))
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT", "900"))
CODEX_APP_SERVER_SOCKET = Path(
    os.environ.get(
        "CODEX_APP_SERVER_SOCKET",
        str(Path(CODEX_HOME) / "app-server-control" / "app-server-control.sock"),
    )
)
CODEX_DEFAULT_THREAD_ID = os.environ.get("CODEX_DEFAULT_THREAD_ID", "").strip()
CODEX_THREAD_NAME = os.environ.get("CODEX_THREAD_NAME", "Neurobot Agent — Telegram").strip()

# Which backend runs this agent's turns. Telegram-side handling (routing,
# dedup, splitting, /start /new /status /limits) is identical either way —
# only the executor (this section + run_turn/create_session/
# read_rate_limit_status below) differs. See claude_executor.py for why the
# Claude backend's rate-limit check is a documented no-op.
EXECUTOR = os.environ.get("NEUROBOT_EXECUTOR", "codex").strip().lower()
HYBRID_EXECUTOR_ORDER = [
    value.strip().lower()
    for value in os.environ.get(
        "NEUROBOT_HYBRID_EXECUTOR_ORDER", "claude,codex"
    ).split(",")
    if value.strip()
]
HYBRID_CLAUDE_FALLBACK_PATTERN = re.compile(
    os.environ.get(
        "NEUROBOT_HYBRID_CLAUDE_FALLBACK_PATTERN",
        r"rate.?limit|usage.?limit|hit your limit|limit reached|too many requests|overloaded",
    ),
    re.IGNORECASE,
)
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude").strip()
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", str(CODEX_TIMEOUT)))
CLAUDE_DEFAULT_SESSION_ID = os.environ.get("CLAUDE_DEFAULT_SESSION_ID", "").strip()
CLAUDE_STATELESS = os.environ.get(
    "NEUROBOT_CLAUDE_STATELESS", "false"
).strip().lower() in ("1", "true", "yes", "on")
CLAUDE_EXTRA_ARGS = [
    value for value in os.environ.get("CLAUDE_EXTRA_ARGS", "").split(" ") if value.strip()
]
NEW_SESSION_SENTINEL = "__new__"

# DeepSeek backend: same `claude` CLI binary as the primary Claude executor,
# pointed at DeepSeek's Anthropic-compatible endpoint for one headless turn
# via ClaudeExecutorClient's env_overrides (see run_deepseek below). No new
# software — see api-docs.deepseek.com/quick_start/agent_integrations/claude_code/.
# The API key is deliberately NOT read from os.environ: it is read straight
# off disk on every call (read_deepseek_api_key) so that filling in
# DEEPSEEK_ENV_FILE takes effect immediately, without a service restart —
# unlike the other secrets in this file, which arrive via systemd
# EnvironmentFile= and are only resolved once, at process start.
DEEPSEEK_ENV_FILE = Path(
    os.environ.get("DEEPSEEK_ENV_FILE", "/etc/__unset__-deepseek.env")
)
DEEPSEEK_BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic"
).strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
DEEPSEEK_MODEL_LABEL = os.environ.get(
    "DEEPSEEK_MODEL_LABEL", "DeepSeek V4 Flash"
).strip()
DEEPSEEK_TIMEOUT = int(os.environ.get("DEEPSEEK_TIMEOUT", str(CODEX_TIMEOUT)))
DEEPSEEK_NOT_CONFIGURED_MESSAGE = "DeepSeek ещё не настроен — ключ API не задан."

# Codex has no per-turn model/effort introspection wired into
# appserver_client.py today (thread/read does return a modelProvider field at
# the thread level, but confirming its exact shape safely against the live
# production App Server socket was not completed — see handoff notes). Until
# that's verified, the Telegram model-indicator label for Codex is a static,
# operator-configured string rather than a dynamic read.
CODEX_MODEL_LABEL = os.environ.get("CODEX_MODEL_LABEL", "Codex").strip()

# Off by default because process_prompt/handle_message are shared by every
# Neurobot role and most deployments do not need a model label.
SHOW_MODEL_LABEL = os.environ.get(
    "NEUROBOT_SHOW_MODEL_LABEL", "false"
).strip().lower() in ("1", "true", "yes", "on")

# Where this Claude Code session's own transcript lives, for reading back the
# model/effort actually used on the latest turn (see describe_current_claude_model).
# Claude Code encodes the project cwd by replacing every non-alphanumeric
# character with "-" (e.g. /home/x/project -> -home-x-project); override via
# env if a deployment ever needs a different transcript root.
CLAUDE_TRANSCRIPT_DIR_OVERRIDE = os.environ.get("CLAUDE_TRANSCRIPT_DIR", "").strip()
AGENT_KEY = os.environ.get("NEUROBOT_AGENT_KEY", "").strip().lower()
AGENT_DISPLAY_NAME = os.environ.get(
    "NEUROBOT_AGENT_DISPLAY_NAME", "Neurobot Agent",
).strip()
AGENT_HELP_TEXT = os.environ.get(
    "NEUROBOT_AGENT_HELP_TEXT", "Помогу с рабочими задачами.",
).strip()
PEERS_FILE = Path(os.environ.get("NEUROBOT_PEERS_FILE", "/etc/neurobot/peers.json"))
PEOPLE_FILE_VALUE = os.environ.get("NEUROBOT_PEOPLE_FILE", "").strip()
PEOPLE_FILE = Path(PEOPLE_FILE_VALUE) if PEOPLE_FILE_VALUE else None
MAX_ROUTE_HOPS = int(os.environ.get("NEUROBOT_MAX_ROUTE_HOPS", "3"))
RATE_LIMIT_MIN_REMAINING_PERCENT = int(
    os.environ.get("CODEX_MIN_REMAINING_PERCENT", "10")
)
RATE_LIMIT_FAIL_CLOSED = os.environ.get(
    "CODEX_RATE_LIMIT_FAIL_CLOSED", "true"
).strip().lower() not in ("0", "false", "no", "off")
# Set on receipt, before any processing — Telegram's whitelist is fixed
# (👍 👎 ❤ 🔥 👀 🎉 etc.); empty disables it.
ACK_REACTION = os.environ.get("NEUROBOT_ACK_REACTION", "👀").strip()
DAILY_ENABLED = os.environ.get(
    "NEUROBOT_DAILY_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
DAILY_TIME = os.environ.get("NEUROBOT_DAILY_TIME", "09:00").strip()
DAILY_TIMEZONE = os.environ.get("NEUROBOT_DAILY_TIMEZONE", "Europe/Moscow").strip()
MANAGER_AUTONOMY_ENABLED = os.environ.get(
    "NEUROBOT_MANAGER_AUTONOMY_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
MANAGER_AUTONOMY_TIME = os.environ.get(
    "NEUROBOT_MANAGER_AUTONOMY_TIME", "11:00"
).strip()
MANAGER_AUTONOMY_TIMEZONE = os.environ.get(
    "NEUROBOT_MANAGER_AUTONOMY_TIMEZONE", "Europe/Moscow"
).strip()
MANAGER_AUTONOMY_MIN_REMAINING_PERCENT = int(
    os.environ.get("NEUROBOT_MANAGER_AUTONOMY_MIN_REMAINING_PERCENT", "80")
)
MANAGER_AUTONOMY_ALLOWED_TARGETS: Set[str] = {
    value.strip().lower()
    for value in os.environ.get(
        "NEUROBOT_MANAGER_AUTONOMY_ALLOWED_TARGETS", "marketing,developer"
    ).split(",")
    if value.strip()
}
MANAGER_AUTONOMY_STRATEGY_CONTEXT = os.environ.get(
    "NEUROBOT_MANAGER_AUTONOMY_STRATEGY_CONTEXT", ""
).strip()
CODEX_WATCH_ENABLED = os.environ.get(
    "NEUROBOT_CODEX_WATCH_ENABLED", "false"
).strip().lower() in ("1", "true", "yes", "on")
CODEX_WATCH_INTERVAL = max(
    1, int(os.environ.get("NEUROBOT_CODEX_WATCH_INTERVAL", "5"))
)
DELEGATION_PATTERN = re.compile(
    r'<telegram_task\s+target="([a-z0-9_-]+)">\s*(.*?)\s*</telegram_task>',
    re.IGNORECASE | re.DOTALL,
)
MESSAGE_PATTERN = re.compile(
    r'<telegram_message\s+target="([a-z0-9_-]+)">\s*(.*?)\s*</telegram_message>',
    re.IGNORECASE | re.DOTALL,
)
HUMAN_MESSAGE_PATTERN = re.compile(
    r'<telegram_human\s+username="@?([a-zA-Z0-9_]{5,32})">\s*(.*?)\s*</telegram_human>',
    re.IGNORECASE | re.DOTALL,
)
MAX_TELEGRAM_TEXT = 4000
MAX_TELEGRAM_DOCUMENT_BYTES = int(
    os.environ.get("NEUROBOT_MAX_DOCUMENT_BYTES", str(48 * 1024 * 1024))
)
ATTACHMENT_ROOTS = [
    (WORKSPACE / value.strip()).resolve()
    for value in os.environ.get(
        "NEUROBOT_ATTACHMENT_DIRS", "reports,exports,artifacts"
    ).split(",")
    if value.strip() and not Path(value.strip()).is_absolute()
]
LOCAL_MARKDOWN_LINK_PATTERN = re.compile(
    r"\[([^\]\n]+)\]\(([^)\n]+)\)"
)
TELEGRAM_FILE_GUIDANCE = (
    "Если создаёшь файл для пользователя Telegram, сохраняй его в каталоге "
    "reports, exports или artifacts внутри рабочего пространства. В финальном "
    "ответе обязательно добавь Markdown-ссылку на его абсолютный локальный путь: "
    "мост проверит путь и отправит файл документом в Telegram."
)


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("neurobot")


def parse_daily_time(
    value: str,
    variable_name: str = "NEUROBOT_DAILY_TIME",
) -> Tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("{} must use HH:MM format".format(variable_name)) from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise RuntimeError("{} must be a valid 24-hour time".format(variable_name))
    return hour, minute


def local_schedule_time(now: Optional[datetime] = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(ZoneInfo(DAILY_TIMEZONE))


def require_config() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TARGET_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    # TELEGRAM_THREAD_ID is optional: 0 means the group has no topics (or the
    # bot lives in its General topic), so messages carry no message_thread_id
    # and replies must not send one.
    if not ALLOW_ALL_CHAT_MEMBERS and not ALLOWED_USER_IDS:
        missing.append("TELEGRAM_ALLOWED_USER_IDS")
    if not AGENT_KEY:
        missing.append("NEUROBOT_AGENT_KEY")
    if not PEERS_FILE.is_file():
        missing.append("NEUROBOT_PEERS_FILE")
    if EXECUTOR not in {"codex", "claude", "hybrid", "deepseek"}:
        raise RuntimeError("NEUROBOT_EXECUTOR must be codex, claude, hybrid, or deepseek")
    if EXECUTOR == "hybrid" and (
        not HYBRID_EXECUTOR_ORDER
        or set(HYBRID_EXECUTOR_ORDER) != {"claude", "codex"}
        or len(HYBRID_EXECUTOR_ORDER) != 2
    ):
        raise RuntimeError(
            "NEUROBOT_HYBRID_EXECUTOR_ORDER must contain claude,codex once each"
        )
    if not 0 <= RATE_LIMIT_MIN_REMAINING_PERCENT <= 100:
        raise RuntimeError("CODEX_MIN_REMAINING_PERCENT must be between 0 and 100")
    if not 0 <= MANAGER_AUTONOMY_MIN_REMAINING_PERCENT < 100:
        raise RuntimeError(
            "NEUROBOT_MANAGER_AUTONOMY_MIN_REMAINING_PERCENT must be between 0 and 99"
        )
    if DAILY_ENABLED:
        if AGENT_KEY == "motivator" and (
            PEOPLE_FILE is None or not PEOPLE_FILE.is_file()
        ):
            missing.append("NEUROBOT_PEOPLE_FILE")
        parse_daily_time(DAILY_TIME)
        try:
            ZoneInfo(DAILY_TIMEZONE)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(
                "Unknown NEUROBOT_DAILY_TIMEZONE: {}".format(DAILY_TIMEZONE)
            ) from exc
    if MANAGER_AUTONOMY_ENABLED:
        if AGENT_KEY != "manager":
            raise RuntimeError(
                "NEUROBOT_MANAGER_AUTONOMY_ENABLED requires NEUROBOT_AGENT_KEY=manager"
            )
        if EXECUTOR != "codex":
            raise RuntimeError(
                "Manager autonomy requires NEUROBOT_EXECUTOR=codex"
            )
        if not MANAGER_AUTONOMY_ALLOWED_TARGETS:
            raise RuntimeError(
                "NEUROBOT_MANAGER_AUTONOMY_ALLOWED_TARGETS must not be empty"
            )
        parse_daily_time(
            MANAGER_AUTONOMY_TIME,
            "NEUROBOT_MANAGER_AUTONOMY_TIME",
        )
        try:
            ZoneInfo(MANAGER_AUTONOMY_TIMEZONE)
        except ZoneInfoNotFoundError as exc:
            raise RuntimeError(
                "Unknown NEUROBOT_MANAGER_AUTONOMY_TIMEZONE: {}".format(
                    MANAGER_AUTONOMY_TIMEZONE
                )
            ) from exc
    if CODEX_WATCH_ENABLED:
        if EXECUTOR != "codex":
            raise RuntimeError(
                "NEUROBOT_CODEX_WATCH_ENABLED requires NEUROBOT_EXECUTOR=codex"
            )
        if AGENT_KEY != "motivator":
            raise RuntimeError(
                "Codex-to-Telegram watcher currently supports only the motivator agent"
            )
        if not CODEX_DEFAULT_THREAD_ID:
            missing.append("CODEX_DEFAULT_THREAD_ID")
        if PEOPLE_FILE is None or not PEOPLE_FILE.is_file():
            missing.append("NEUROBOT_PEOPLE_FILE")
    if missing:
        raise RuntimeError("Missing required configuration: " + ", ".join(missing))



PEERS_BY_KEY: Dict[str, Dict[str, Any]] = {}
PEERS_BY_BOT_ID: Dict[int, Dict[str, Any]] = {}
PEOPLE_BY_USERNAME: Dict[str, Dict[str, Any]] = {}


def initialize_peers() -> None:
    raw = json.loads(PEERS_FILE.read_text(encoding="utf-8"))
    peers = raw.get("agents") if isinstance(raw, dict) else None
    if not isinstance(peers, dict):
        raise RuntimeError("Neurobot peers file must contain an agents object")
    PEERS_BY_KEY.clear()
    PEERS_BY_BOT_ID.clear()
    for key, value in peers.items():
        if not isinstance(value, dict):
            continue
        peer = dict(value)
        peer["key"] = str(key).lower()
        peer["bot_id"] = int(peer["bot_id"])
        peer["thread_id"] = int(peer["thread_id"])
        peer["username"] = str(peer["username"]).lstrip("@")
        PEERS_BY_KEY[peer["key"]] = peer
        PEERS_BY_BOT_ID[peer["bot_id"]] = peer
    if AGENT_KEY not in PEERS_BY_KEY:
        raise RuntimeError("Current agent is missing from Neurobot peers file")
    if MANAGER_AUTONOMY_ENABLED:
        invalid_targets = sorted(
            target
            for target in MANAGER_AUTONOMY_ALLOWED_TARGETS
            if target == AGENT_KEY or target not in PEERS_BY_KEY
        )
        if invalid_targets:
            raise RuntimeError(
                "Unknown manager autonomy targets: {}".format(
                    ", ".join(invalid_targets)
                )
            )


def initialize_people() -> None:
    PEOPLE_BY_USERNAME.clear()
    if PEOPLE_FILE is None:
        return
    raw = json.loads(PEOPLE_FILE.read_text(encoding="utf-8"))
    people = raw.get("people") if isinstance(raw, dict) else None
    if not isinstance(people, list):
        raise RuntimeError("Neurobot people file must contain a people array")
    for value in people:
        if not isinstance(value, dict):
            continue
        person = dict(value)
        username = str(person.get("username") or "").strip().lstrip("@").lower()
        if not re.fullmatch(r"[a-zA-Z0-9_]{5,32}", username):
            raise RuntimeError("Invalid Telegram username in people file")
        person["username"] = username
        if person.get("telegram_user_id") is not None:
            person["telegram_user_id"] = int(person["telegram_user_id"])
        if person.get("daily_time"):
            parse_daily_time(str(person["daily_time"]))
        if person.get("daily_timezone"):
            try:
                ZoneInfo(str(person["daily_timezone"]))
            except ZoneInfoNotFoundError as exc:
                raise RuntimeError(
                    "Unknown daily_timezone for @{}: {}".format(
                        username, person["daily_timezone"]
                    )
                ) from exc
        PEOPLE_BY_USERNAME[username] = person


def load_state() -> Dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"offset": 0, "sessions": {}}


def save_state(state: Dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def log_backend_usage(backend: str, model: str, message: Optional[Dict[str, Any]] = None) -> None:
    """Best-effort analytics side-channel: append one JSON line recording
    which backend/model actually handled a dispatched turn.

    This is purely for after-the-fact analysis. It must never affect
    the agent persona/context and must never break message
    delivery. Callers (channel_gateway.handle_inbound, at the same points
    that already set CHANNEL_LAST_BACKEND_KEY) call this once per turn that
    actually gets dispatched to a backend, not per raw inbound webhook.

    Append-only, one json.dumps call, one write — deliberately not a
    read-modify-write of the whole file. Any failure (disk, permissions,
    missing directory) is logged and swallowed here so it can never
    propagate out and prevent the real reply from being sent.
    """
    try:
        message = message or {}
        chat = message.get("chat") or {}
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "backend": backend,
            "model": model,
            "message_id": message.get("message_id"),
            "chat_id": chat.get("id"),
            "thread_id": message.get("message_thread_id"),
        }
        BACKEND_USAGE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with BACKEND_USAGE_LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False))
            handle.write("\n")
    except Exception:
        LOG.warning("Failed to append backend usage log entry", exc_info=True)


def summarize_backend_usage(max_lines: int = 500) -> str:
    """Best-effort one-line 'backend: count' summary over the most recent
    entries in BACKEND_USAGE_LOG_FILE, for the /status command.

    Reads at most max_lines from the tail of the file — cheap and bounded
    regardless of how large the append-only log has grown. Any failure
    (missing file, unreadable, corrupt line) yields "" so /status still
    replies without it; this must stay read-only and never touch the file.
    """
    try:
        if not BACKEND_USAGE_LOG_FILE.exists():
            return ""
        lines = [
            line
            for line in BACKEND_USAGE_LOG_FILE.read_text(encoding="utf-8").splitlines()[-max_lines:]
            if line.strip()
        ]
        counts: Dict[str, int] = {}
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            backend = entry.get("backend")
            if backend:
                counts[backend] = counts.get(backend, 0) + 1
        if not counts:
            return ""
        parts = ", ".join("{}={}".format(name, count) for name, count in sorted(counts.items()))
        return "Использование (посл. {} записей): {}.".format(len(lines), parts)
    except Exception:
        LOG.warning("Failed to summarize backend usage log", exc_info=True)
        return ""


def codex_answer_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def remember_codex_bridge_answer(state: Dict[str, Any], answer: str) -> None:
    """Prevent the watcher from re-sending an answer already handled inline.

    Telegram and scheduled turns are executed synchronously by this process.
    Their structured blocks are published by process_prompt itself, while the
    watcher later sees the same final App Server item. A bounded hash queue
    makes that later observation a no-op without relying on notification IDs.
    """
    if not CODEX_WATCH_ENABLED or EXECUTOR != "codex":
        return
    ignored = state.setdefault("codex_watcher", {}).setdefault(
        "ignored_answer_hashes", []
    )
    ignored.append(codex_answer_hash(answer))
    del ignored[:-200]
    save_state(state)


def codex_watch_thread_id(state: Dict[str, Any]) -> str:
    session_key = "{}:{}".format(TARGET_CHAT_ID, TARGET_THREAD_ID)
    return str(
        state.get("sessions", {}).get(session_key)
        or CODEX_DEFAULT_THREAD_ID
        or ""
    )


def read_codex_final_messages(thread_id: str) -> List[Tuple[str, str]]:
    """Return the final agent message from every completed turn."""
    with CodexAppServerClient(CODEX_APP_SERVER_SOCKET, CODEX_TIMEOUT) as client:
        result = client.read_thread(thread_id, include_turns=True)
    thread = result.get("thread") or {}
    messages: List[Tuple[str, str]] = []
    for turn in thread.get("turns") or []:
        if turn.get("status") != "completed":
            continue
        agent_items = [
            item
            for item in (turn.get("items") or [])
            if item.get("type") == "agentMessage"
            and item.get("id")
            and str(item.get("text") or "").strip()
        ]
        if agent_items:
            final_item = agent_items[-1]
            messages.append(
                (str(final_item["id"]), str(final_item["text"]).strip())
            )
    return messages


def codex_watch_entry(state: Dict[str, Any], thread_id: str) -> Dict[str, Any]:
    watcher = state.setdefault("codex_watcher", {})
    threads = watcher.setdefault("threads", {})
    entry = threads.setdefault(
        thread_id,
        {"initialized": False, "seen_item_ids": {}},
    )
    if not isinstance(entry.get("seen_item_ids"), dict):
        entry["seen_item_ids"] = {}
    return entry


def prune_codex_watch_items(entry: Dict[str, Any]) -> None:
    seen = entry["seen_item_ids"]
    if len(seen) > 1000:
        for item_id, _ in sorted(
            seen.items(), key=lambda pair: int(pair[1])
        )[:-800]:
            seen.pop(item_id, None)


def initialize_codex_watcher(state: Dict[str, Any]) -> None:
    """Baseline existing history once, so enabling the feature never replays it."""
    if not CODEX_WATCH_ENABLED:
        return
    thread_id = codex_watch_thread_id(state)
    if not thread_id:
        raise RuntimeError("Codex watcher has no thread id")
    entry = codex_watch_entry(state, thread_id)
    if entry.get("initialized"):
        return
    now = int(time.time())
    messages = read_codex_final_messages(thread_id)
    for item_id, _ in messages:
        entry["seen_item_ids"][item_id] = now
    entry["initialized"] = True
    entry["initialized_at"] = now
    prune_codex_watch_items(entry)
    save_state(state)
    LOG.info(
        "Initialized Codex watcher thread=%s baseline_items=%s",
        thread_id,
        len(messages),
    )


def record_external_human_message(
    state: Dict[str, Any], username: str, body: str
) -> None:
    person = PEOPLE_BY_USERNAME.get(username) or {}
    current = person_local_schedule_time(person)
    entries = state.setdefault("motivation_history", {}).setdefault(username, [])
    entries.append(
        {
            "local_date": current.date().isoformat(),
            "text": body,
            "source": "codex",
        }
    )
    del entries[:-14]


def poll_codex_watcher(state: Dict[str, Any]) -> int:
    """Forward structured human messages from direct Codex Desktop turns.

    Only the final message of completed turns is considered, and only
    <telegram_human> blocks addressed to configured people are allowed.
    Historical items, bridge-owned turns, unknown usernames and agent-routing
    blocks are never forwarded.
    """
    if not CODEX_WATCH_ENABLED:
        return 0
    thread_id = codex_watch_thread_id(state)
    if not thread_id:
        return 0
    entry = codex_watch_entry(state, thread_id)
    if not entry.get("initialized"):
        initialize_codex_watcher(state)
        return 0

    sent_count = 0
    now = int(time.time())
    watcher = state.setdefault("codex_watcher", {})
    ignored = watcher.setdefault("ignored_answer_hashes", [])
    for item_id, answer in read_codex_final_messages(thread_id):
        if item_id in entry["seen_item_ids"]:
            continue

        # Persist before the external side effect for at-most-once delivery.
        # On a known Telegram failure we remove the marker to allow a retry.
        entry["seen_item_ids"][item_id] = now
        prune_codex_watch_items(entry)
        save_state(state)

        answer_hash = codex_answer_hash(answer)
        if answer_hash in ignored:
            ignored.remove(answer_hash)
            save_state(state)
            LOG.debug("Ignored bridge-owned Codex item=%s", item_id)
            continue

        _, routes, human_messages = extract_outputs(answer)
        usernames = [username for username, _ in human_messages]
        invalid = (
            bool(routes)
            or not human_messages
            or len(usernames) != len(set(usernames))
            or any(username not in PEOPLE_BY_USERNAME for username in usernames)
        )
        if invalid:
            if human_messages or routes:
                LOG.warning(
                    "Rejected unsafe external Codex item=%s routes=%s usernames=%s",
                    item_id,
                    len(routes),
                    usernames,
                )
            continue

        try:
            for username, body in human_messages:
                send_text("@{}\n\n{}".format(username, body))
                record_external_human_message(state, username, body)
                sent_count += 1
            save_state(state)
            LOG.info(
                "Forwarded external Codex item=%s human_messages=%s",
                item_id,
                len(human_messages),
            )
        except Exception:
            entry["seen_item_ids"].pop(item_id, None)
            save_state(state)
            raise
    return sent_count


TELEGRAM_RETRY_ATTEMPTS = int(os.environ.get("TELEGRAM_RETRY_ATTEMPTS", "3"))


def telegram(method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Any:
    """Call the Bot API. Retries transient network failures (URLError,
    timeouts) a few times with backoff — a real API rejection (HTTPError with
    a parsed Telegram response, or `ok: false`) is not retried, since retrying
    a permanent error just delays the same failure.
    """
    data = urllib.parse.urlencode(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.telegram.org/bot{}/{}".format(BOT_TOKEN, method),
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    last_exc: Optional[Exception] = None
    for attempt in range(1, TELEGRAM_RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("Telegram HTTP {}: {}".format(exc.code, body[:500])) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < TELEGRAM_RETRY_ATTEMPTS:
                LOG.warning(
                    "Telegram %s network error (attempt %s/%s): %s",
                    method, attempt, TELEGRAM_RETRY_ATTEMPTS, exc,
                )
                time.sleep(min(2 ** attempt, 10))
                continue
            raise RuntimeError(
                "Telegram {} failed after {} attempts: {}".format(method, attempt, exc)
            ) from exc
        else:
            if not result.get("ok"):
                raise RuntimeError(
                    "Telegram API error: {}".format(result.get("description", "unknown"))
                )
            return result.get("result")
    raise RuntimeError("Telegram {} failed: {}".format(method, last_exc))


def telegram_multipart(
    method: str,
    payload: Dict[str, Any],
    file_field: str,
    file_path: Path,
    timeout: int = 60,
) -> Any:
    # RFC 2046 limits multipart boundaries to 70 characters.
    boundary = "----neurobot{}".format(
        hashlib.sha256(os.urandom(16)).hexdigest()[:32]
    )
    boundary_bytes = boundary.encode("ascii")
    body = bytearray()
    for name, value in payload.items():
        body.extend(b"--" + boundary_bytes + b"\r\n")
        body.extend(
            'Content-Disposition: form-data; name="{}"\r\n\r\n'.format(
                str(name).replace('"', "")
            ).encode("utf-8")
        )
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    filename = file_path.name.replace('"', "")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body.extend(b"--" + boundary_bytes + b"\r\n")
    body.extend(
        'Content-Disposition: form-data; name="{}"; filename="{}"\r\n'.format(
            file_field.replace('"', ""), filename
        ).encode("utf-8")
    )
    body.extend("Content-Type: {}\r\n\r\n".format(content_type).encode("ascii"))
    body.extend(file_path.read_bytes())
    body.extend(b"\r\n--" + boundary_bytes + b"--\r\n")

    request = urllib.request.Request(
        "https://api.telegram.org/bot{}/{}".format(BOT_TOKEN, method),
        data=bytes(body),
        headers={"Content-Type": "multipart/form-data; boundary={}".format(boundary)},
    )
    last_exc: Optional[Exception] = None
    for attempt in range(1, TELEGRAM_RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                "Telegram HTTP {}: {}".format(exc.code, response_body[:500])
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < TELEGRAM_RETRY_ATTEMPTS:
                LOG.warning(
                    "Telegram %s upload network error (attempt %s/%s): %s",
                    method,
                    attempt,
                    TELEGRAM_RETRY_ATTEMPTS,
                    exc,
                )
                time.sleep(min(2 ** attempt, 10))
                continue
            raise RuntimeError(
                "Telegram {} upload failed after {} attempts: {}".format(
                    method, attempt, exc
                )
            ) from exc
        else:
            if not result.get("ok"):
                raise RuntimeError(
                    "Telegram API error: {}".format(
                        result.get("description", "unknown")
                    )
                )
            return result.get("result")
    raise RuntimeError("Telegram {} upload failed: {}".format(method, last_exc))


def path_is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_attachment_path(raw_target: str) -> Optional[Path]:
    target = urllib.parse.unquote(raw_target.strip())
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if urllib.parse.urlparse(target).scheme or target.startswith("#"):
        return None
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = WORKSPACE / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file():
        return None
    if not any(path_is_inside(resolved, root) for root in ATTACHMENT_ROOTS):
        return None
    try:
        size = resolved.stat().st_size
    except OSError:
        return None
    if size > MAX_TELEGRAM_DOCUMENT_BYTES:
        LOG.warning(
            "Skipped oversized Telegram attachment path=%s bytes=%s limit=%s",
            resolved,
            size,
            MAX_TELEGRAM_DOCUMENT_BYTES,
        )
        return None
    return resolved


def extract_local_attachments(text: str) -> Tuple[str, List[Path]]:
    attachments: List[Path] = []
    seen: Set[Path] = set()

    def replace_link(match: re.Match[str]) -> str:
        path = resolve_attachment_path(match.group(2))
        if path is None:
            return match.group(0)
        if path not in seen:
            seen.add(path)
            attachments.append(path)
        label = match.group(1).strip() or path.name
        return "{} — файл прикреплён ниже".format(label)

    return LOCAL_MARKDOWN_LINK_PATTERN.sub(replace_link, text), attachments


def send_document(file_path: Path, reply_to: Optional[int] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "chat_id": TARGET_CHAT_ID,
        "caption": "📎 {}".format(file_path.name),
    }
    if TARGET_THREAD_ID:
        payload["message_thread_id"] = TARGET_THREAD_ID
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = "true"
    return telegram_multipart("sendDocument", payload, "document", file_path)


def split_message(text: str) -> List[str]:
    text = text.strip() or "Готово, но ответ получился пустым."
    chunks: List[str] = []
    while len(text) > MAX_TELEGRAM_TEXT:
        cut = text.rfind("\n", 0, MAX_TELEGRAM_TEXT)
        if cut < MAX_TELEGRAM_TEXT // 2:
            cut = text.rfind(" ", 0, MAX_TELEGRAM_TEXT)
        if cut < MAX_TELEGRAM_TEXT // 2:
            cut = MAX_TELEGRAM_TEXT
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def send_text_to_thread(thread_id: int, text: str, reply_to: Optional[int] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "chat_id": TARGET_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    if reply_to:
        payload["reply_to_message_id"] = reply_to
        payload["allow_sending_without_reply"] = "true"
    return telegram("sendMessage", payload)


def send_text(text: str, reply_to: Optional[int] = None) -> Dict[str, Any]:
    return send_text_to_thread(TARGET_THREAD_ID, text, reply_to)


def edit_text(message_id: int, text: str) -> None:
    telegram(
        "editMessageText",
        {
            "chat_id": TARGET_CHAT_ID,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": "true",
        },
    )


def set_reaction(message_id: int, emoji: str) -> None:
    """Best-effort ack reaction, set the moment a message is accepted for
    processing — before the (possibly slow) rate-limit check and executor
    turn — so the sender has immediate confirmation the agent saw it, same
    as Claude Code's own Telegram channel does. Never raises: Telegram only
    accepts a fixed emoji whitelist, and a failed reaction should not stop
    the actual task from running.
    """
    if not emoji:
        return
    try:
        telegram(
            "setMessageReaction",
            {
                "chat_id": TARGET_CHAT_ID,
                "message_id": message_id,
                "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
            },
        )
    except Exception:
        LOG.warning("Failed to set ack reaction on message_id=%s", message_id, exc_info=True)


# --- Model indicator (Telegram header line) -------------------------------
#
# Every outgoing Telegram message (ack, live-edited progress, final answer)
# should show which executor/model/effort produced it. Codex and DeepSeek turns
# are already fully composed in this process (see process_prompt below), so
# their label is just prepended once, there. The persistent Claude backend
# is different: its replies are generated and sent by a separate live
# `claude --continue` process talking to Telegram through its own MCP
# `reply` tool, relayed via channel_gateway.handle_outbound — this module
# only sees the text after the fact, so its label has to be figured out
# independently (see describe_current_claude_model).

_MODEL_ID_LABELS = {
    "claude-sonnet-5": "Sonnet5",
    "claude-opus-5": "Opus5",
    "claude-haiku-5": "Haiku5",
}


def format_model_id(model_id: str) -> str:
    """"claude-sonnet-5" -> "Sonnet5"; falls back to a generic transform for
    any model id not in the static table above, so a future model bump
    doesn't silently break the label."""
    label = _MODEL_ID_LABELS.get(model_id)
    if label:
        return label
    parts = [part for part in model_id.split("-") if part and part.lower() != "claude"]
    return "".join(part.capitalize() for part in parts) or model_id


def _claude_transcript_dir() -> Path:
    if CLAUDE_TRANSCRIPT_DIR_OVERRIDE:
        return Path(CLAUDE_TRANSCRIPT_DIR_OVERRIDE)
    home = Path(os.environ.get("HOME") or str(Path.home()))
    # Claude Code encodes the project cwd as a directory name by replacing
    # every character that isn't [A-Za-z0-9] with "-".
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(WORKSPACE))
    return home / ".claude" / "projects" / encoded


def describe_current_claude_model() -> str:
    """Best-effort 'Claude <Model> <Effort>' label (e.g. "Claude Sonnet5
    High"), read live from the persistent session's own transcript.

    Why read the transcript instead of an env var: the model is fixed for
    the process lifetime, but effort can change mid-session via /effort, and
    Claude Code does not expose either through a hook/env var a plugin can
    read — the transcript (~/.claude/projects/<encoded-cwd>/*.jsonl) is the
    only place both are recorded, on every assistant turn ("model" inside
    the message, "effort" as a sibling key on the same JSONL entry). This is
    a plain local file read with no risk to the live session, so it is safe
    to do on every outbound message; it is intentionally NOT cached, since a
    cached value would go stale the moment the user runs /effort.

    Any failure (missing directory, empty transcript, unexpected format)
    falls back to the plain "Claude" label — labeling must never block or
    corrupt message delivery.
    """
    try:
        transcript_dir = _claude_transcript_dir()
        newest = max(
            transcript_dir.glob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            default=None,
        )
        if newest is None:
            return "Claude"
        with newest.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 200_000))
            tail = handle.read().decode("utf-8", errors="ignore")
        for line in reversed(tail.splitlines()):
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
            model_id = message.get("model") if isinstance(message, dict) else None
            if not model_id:
                continue
            label = "Claude {}".format(format_model_id(model_id))
            effort = entry.get("effort")
            if effort:
                label += " {}".format(str(effort).capitalize())
            return label
    except Exception:
        LOG.warning("Failed to read current Claude model/effort for label", exc_info=True)
    return "Claude"


def backend_model_label(backend: str, codex_thread_id: Optional[str] = None) -> str:
    """DeepSeek is a static/cheap label (fixed model, no effort concept).
    Codex is dynamic — see describe_current_codex_model, which needs the
    thread id currently in use to look up its rollout log. The Claude label
    is intentionally NOT handled here — see describe_current_claude_model,
    used from channel_gateway.handle_outbound for the persistent session
    instead."""
    if backend == "deepseek":
        return DEEPSEEK_MODEL_LABEL
    if backend == "codex":
        return describe_current_codex_model(codex_thread_id)
    return "Claude"


# Telegram plain messages have no real font-size control. Full Unicode
# superscript (letters + digits) is the agreed stand-in — these glyphs are
# physically smaller/raised in nearly every font, which reads noticeably
# smaller than small-caps did (an earlier iteration of this same idea; user
# tried it live and still found it too large). This is a purely cosmetic,
# output-only transform: it is applied to a *copy* of the label right before
# it's spliced into the message, never to the label text itself
# (describe_current_claude_model/backend_model_label keep returning plain
# "Claude Sonnet5 High" etc., which is what gets logged, tested, and
# reasoned about everywhere else).
_SUPERSCRIPT_LETTERS = {
    "a": "ᵃ", "b": "ᵇ", "c": "ᶜ", "d": "ᵈ", "e": "ᵉ", "f": "ᶠ", "g": "ᵍ",
    "h": "ʰ", "i": "ⁱ", "j": "ʲ", "k": "ᵏ", "l": "ˡ", "m": "ᵐ", "n": "ⁿ",
    "o": "ᵒ", "p": "ᵖ", "r": "ʳ", "s": "ˢ", "t": "ᵗ", "u": "ᵘ",
    "v": "ᵛ", "w": "ʷ", "x": "ˣ", "y": "ʸ", "z": "ᶻ",
    # "q" has no standard Unicode superscript code point — a known gap, left
    # unmapped on purpose rather than guessing at a visual substitute.
}
_SUPERSCRIPT_DIGITS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
}
_SUPERSCRIPT_TABLE: Dict[str, str] = {**_SUPERSCRIPT_LETTERS, **_SUPERSCRIPT_DIGITS}


def to_superscript(text: str) -> str:
    """Cosmetic Unicode transliteration for label headers — see comment
    above _SUPERSCRIPT_LETTERS. The superscript letter block is single-case,
    so the input is lowercased before mapping (this only affects which
    glyph is picked, not the underlying label text elsewhere). Anything
    without a mapping — space, punctuation like "(" ")" "-", the letter
    "q", non-Latin text — passes through unchanged rather than being
    dropped or guessed at.
    """
    return "".join(_SUPERSCRIPT_TABLE.get(char, char) for char in text.lower())


def with_model_label(
    backend: str, text: str, codex_thread_id: Optional[str] = None
) -> str:
    if not SHOW_MODEL_LABEL or not text.strip():
        return text
    # Single newline, not a blank-line paragraph break: the label already
    # reads as a header sitting right above the message, no extra gap needed.
    return "{}\n{}".format(
        to_superscript(backend_model_label(backend, codex_thread_id)), text
    )


def clean_prompt(text: str) -> str:
    text = text.strip()
    mention = "@" + BOT_USERNAME
    text = text.replace(mention, "").strip()
    if text.lower().startswith("/start"):
        return ""
    return text


def format_reset_time(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return "время сброса неизвестно"


def rate_window_label(window: Dict[str, Any]) -> str:
    duration = window.get("windowDurationMins")
    if duration == 300:
        return "5-часовой лимит"
    if duration == 10080:
        return "7-дневный лимит"
    if duration:
        return "лимит за {} мин.".format(duration)
    return "лимит"


def read_codex_rate_limit_status() -> Dict[str, Any]:
    with CodexAppServerClient(CODEX_APP_SERVER_SOCKET, min(CODEX_TIMEOUT, 30)) as client:
        response = client.read_account_rate_limits()

    by_id = response.get("rateLimitsByLimitId") or {}
    snapshot = by_id.get("codex") if isinstance(by_id, dict) else None
    if not isinstance(snapshot, dict):
        snapshot = response.get("rateLimits") or {}
    if not isinstance(snapshot, dict) or not snapshot:
        raise RuntimeError("Codex App Server did not return a rate-limit snapshot")

    measurements: List[Tuple[str, int, Optional[int]]] = []
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, dict) or "usedPercent" not in window:
            continue
        used = max(0, min(100, int(window["usedPercent"])))
        measurements.append(
            (rate_window_label(window), 100 - used, window.get("resetsAt"))
        )

    individual = snapshot.get("individualLimit")
    if isinstance(individual, dict) and "remainingPercent" in individual:
        remaining = max(0, min(100, int(individual["remainingPercent"])))
        measurements.append(("индивидуальный лимит", remaining, individual.get("resetsAt")))

    credits = snapshot.get("credits") or {}
    if not measurements and isinstance(credits, dict) and credits.get("unlimited"):
        return {
            "allowed": True,
            "remaining_percent": 100,
            "details": "лимит не ограничен",
            "plan_type": snapshot.get("planType") or "unknown",
        }
    if not measurements:
        raise RuntimeError("Codex App Server returned no measurable rate-limit windows")

    remaining_percent = min(item[1] for item in measurements)
    details = []
    for label, remaining, resets_at in measurements:
        text = "{}: осталось {}%".format(label, remaining)
        if resets_at:
            text += ", сброс {}".format(format_reset_time(resets_at))
        details.append(text)

    hard_block = bool(snapshot.get("spendControlReached")) or bool(
        snapshot.get("rateLimitReachedType")
    )
    return {
        "allowed": not hard_block and remaining_percent >= RATE_LIMIT_MIN_REMAINING_PERCENT,
        "remaining_percent": remaining_percent,
        "details": "; ".join(details),
        "plan_type": snapshot.get("planType") or "unknown",
        "rate_limit_reached_type": snapshot.get("rateLimitReachedType"),
    }


def read_codex_thread_meta(thread_id: str) -> Dict[str, Any]:
    with CodexAppServerClient(CODEX_APP_SERVER_SOCKET, min(CODEX_TIMEOUT, 30)) as client:
        return client.read_thread(thread_id, include_turns=False)


def describe_current_codex_model(thread_id: Optional[str]) -> str:
    """Best-effort dynamic 'Codex <model>[ <effort>]' label, mirroring
    describe_current_claude_model but for the Codex App Server.

    Codex has no per-turn model/effort field on the thread/read RPC result
    itself (that only carries modelProvider, e.g. "openai" — the vendor,
    not the specific model). What it DOES carry is a `path` to that
    thread's on-disk rollout JSONL log
    (<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-...-<threadId>.jsonl), and
    that log records a `turn_context` entry per turn with both the actual
    model id (e.g. "gpt-5.6-sol") and `collaboration_mode.settings.
    reasoning_effort`. Reads the tail of that file fresh on every call, same
    reasoning as the Claude transcript read: effort can change between
    turns and must not be cached.

    Falls back to the static CODEX_MODEL_LABEL on any failure (no thread
    id yet, App Server unreachable, rollout file missing/unreadable,
    unexpected format) — labeling must never block or corrupt message
    delivery, and this touches a live socket plus the agent's on-disk
    session log, both more failure-prone than Claude's plain local file
    read.
    """
    if not thread_id:
        return CODEX_MODEL_LABEL
    try:
        thread = read_codex_thread_meta(thread_id).get("thread") or {}
        rollout_path = thread.get("path")
        if not rollout_path:
            return CODEX_MODEL_LABEL
        with open(rollout_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 200_000))
            tail = handle.read().decode("utf-8", errors="ignore")
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "turn_context":
                continue
            payload = entry.get("payload") or {}
            model_id = payload.get("model")
            if not model_id:
                continue
            label = "Codex {}".format(model_id)
            settings = ((payload.get("collaboration_mode") or {}).get("settings")) or {}
            effort = settings.get("reasoning_effort")
            if effort:
                label += " {}".format(str(effort).capitalize())
            return label
    except Exception:
        LOG.warning("Failed to read current Codex model/effort for label", exc_info=True)
    return CODEX_MODEL_LABEL


def format_rate_limit_status(status: Dict[str, Any], blocked: bool = False) -> str:
    prefix = "⛔ Задача не запущена. " if blocked else "📊 "
    return (
        prefix
        + "Лимит ({plan}): {details}. Порог остановки — менее {threshold}%."
    ).format(
        plan=status.get("plan_type", "unknown"),
        details=status.get("details", "данные отсутствуют"),
        threshold=RATE_LIMIT_MIN_REMAINING_PERCENT,
    )


def run_codex(prompt: str, session_id: Optional[str]) -> Tuple[str, Optional[str]]:
    target_session = (
        None
        if session_id == NEW_SESSION_SENTINEL
        else session_id or CODEX_DEFAULT_THREAD_ID
    )
    LOG.info("Starting App Server turn session=%s", target_session or "new")
    with CodexAppServerClient(CODEX_APP_SERVER_SOCKET, CODEX_TIMEOUT) as client:
        if not target_session:
            target_session = client.start_thread(WORKSPACE, CODEX_THREAD_NAME)
        answer = client.run_turn(target_session, prompt)
    return answer, target_session


def create_codex_session() -> str:
    with CodexAppServerClient(CODEX_APP_SERVER_SOCKET, CODEX_TIMEOUT) as client:
        return client.start_thread(WORKSPACE, CODEX_THREAD_NAME)


def run_claude(prompt: str, session_id: Optional[str]) -> Tuple[str, Optional[str]]:
    target_session = None
    if not CLAUDE_STATELESS:
        target_session = (
            None
            if session_id == NEW_SESSION_SENTINEL
            else session_id or CLAUDE_DEFAULT_SESSION_ID
        )
    LOG.info("Starting Claude Code turn session=%s", target_session or "new")
    with ClaudeExecutorClient(
        WORKSPACE, CLAUDE_TIMEOUT, CLAUDE_BIN, CLAUDE_EXTRA_ARGS
    ) as client:
        if not target_session:
            answer, target_session = client.run_turn_new_session(prompt)
        else:
            answer = client.run_turn(target_session, prompt)
    return answer, None if CLAUDE_STATELESS else target_session


def create_claude_session() -> str:
    if CLAUDE_STATELESS:
        return NEW_SESSION_SENTINEL
    with ClaudeExecutorClient(WORKSPACE, CLAUDE_TIMEOUT, CLAUDE_BIN, CLAUDE_EXTRA_ARGS) as client:
        return client.start_thread(WORKSPACE, CODEX_THREAD_NAME)


def read_claude_rate_limit_status() -> Dict[str, Any]:
    with ClaudeExecutorClient(WORKSPACE, CLAUDE_TIMEOUT, CLAUDE_BIN, CLAUDE_EXTRA_ARGS) as client:
        info = client.read_account_rate_limits()
    return {
        "allowed": True,
        "remaining_percent": 100,
        "details": info["details"],
        "plan_type": info["planType"],
        "rate_limit_reached_type": None,
    }


def read_deepseek_api_key() -> str:
    """Read DEEPSEEK_API_KEY straight off disk, every call.

    Deliberately bypasses os.environ: the systemd EnvironmentFile= mechanism
    only resolves a file's contents once, at unit start, so anything relying
    on inherited env would need a service restart every time the key file is
    edited. Reading the file directly here means filling in
    DEEPSEEK_ENV_FILE takes effect on the very next /backend deepseek call —
    no restart required. Returns "" (not configured) on any I/O error,
    missing file, or missing key — never raises.
    """
    try:
        text = DEEPSEEK_ENV_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    found = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == "DEEPSEEK_API_KEY":
            # Last assignment wins, matching standard .env conventions — a
            # human appending a new "DEEPSEEK_API_KEY=..." line rather than
            # editing the placeholder in place should still work.
            found = value.strip()
    return found


def deepseek_configured() -> bool:
    return bool(read_deepseek_api_key())


def deepseek_client(timeout: Optional[int] = None) -> ClaudeExecutorClient:
    api_key = read_deepseek_api_key()
    if not api_key:
        raise RuntimeError(DEEPSEEK_NOT_CONFIGURED_MESSAGE)
    env_overrides = {
        "ANTHROPIC_BASE_URL": DEEPSEEK_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_MODEL": DEEPSEEK_MODEL,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": DEEPSEEK_MODEL,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": DEEPSEEK_MODEL,
    }
    return ClaudeExecutorClient(
        WORKSPACE,
        timeout or DEEPSEEK_TIMEOUT,
        CLAUDE_BIN,
        CLAUDE_EXTRA_ARGS,
        env_overrides=env_overrides,
    )


def run_deepseek(prompt: str, session_id: Optional[str]) -> Tuple[str, Optional[str]]:
    target_session = None if session_id == NEW_SESSION_SENTINEL else session_id
    LOG.info("Starting DeepSeek turn session=%s", target_session or "new")
    with deepseek_client() as client:
        if not target_session:
            answer, target_session = client.run_turn_new_session(prompt)
        else:
            answer = client.run_turn(target_session, prompt)
    return answer, target_session


def create_deepseek_session() -> str:
    with deepseek_client() as client:
        return client.start_thread(WORKSPACE, "Neurobot Agent — DeepSeek")


def read_deepseek_rate_limit_status() -> Dict[str, Any]:
    if not deepseek_configured():
        return {
            "allowed": False,
            "remaining_percent": 0,
            "details": DEEPSEEK_NOT_CONFIGURED_MESSAGE,
            "plan_type": "deepseek-api",
            "rate_limit_reached_type": "not_configured",
        }
    return {
        "allowed": True,
        "remaining_percent": 100,
        "details": "DeepSeek: pay-as-you-go API, точный pre-flight остаток не предоставляется.",
        "plan_type": "deepseek-api ({})".format(DEEPSEEK_MODEL),
        "rate_limit_reached_type": None,
    }


def normalize_hybrid_session(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return {
            "mode": str(value.get("mode") or "auto").lower(),
            "claude": None if CLAUDE_STATELESS else value.get("claude"),
            "codex": value.get("codex"),
            "last_backend": value.get("last_backend"),
        }
    # A legacy single-backend state may contain a Claude session id. Preserve
    # it only for stateful deployments; stateless mode deliberately relies on
    # shared durable memory so Telegram and Claude Desktop never contend for
    # one live conversation.
    return {
        "mode": "auto",
        "claude": None if CLAUDE_STATELESS else value or None,
        "codex": None,
        "last_backend": "claude" if value else None,
    }


def hybrid_backend_order(session: Dict[str, Any]) -> List[str]:
    mode = str(session.get("mode") or "auto").lower()
    if mode in {"claude", "codex"}:
        return [mode]
    return list(HYBRID_EXECUTOR_ORDER)


def claude_failure_allows_fallback(error: Exception) -> bool:
    return bool(HYBRID_CLAUDE_FALLBACK_PATTERN.search(str(error)))


def run_hybrid(prompt: str, session_value: Any) -> Tuple[str, Dict[str, Any]]:
    session = normalize_hybrid_session(session_value)
    errors: List[str] = []
    backends = hybrid_backend_order(session)
    fallback_used = False

    for index, backend in enumerate(backends):
        try:
            if backend == "claude":
                claude_session = session.get("claude")
                answer, new_session = run_claude(prompt, claude_session)
                session["claude"] = new_session
            else:
                limit_status = read_codex_rate_limit_status()
                if not limit_status["allowed"]:
                    raise RuntimeError(format_rate_limit_status(limit_status, blocked=True))
                codex_session = session.get("codex")
                answer, new_session = run_codex(prompt, codex_session)
                session["codex"] = new_session
            session["last_backend"] = backend
            if fallback_used:
                answer = (
                    "🔄 Лимит Claude исчерпан — задачу подхватил Codex.\n\n" + answer
                )
            return answer, session
        except Exception as exc:
            errors.append("{}: {}".format(backend, str(exc)[:1000]))
            is_last = index == len(backends) - 1
            if (
                backend != "claude"
                or is_last
                or session.get("mode") != "auto"
                or not claude_failure_allows_fallback(exc)
            ):
                raise
            fallback_used = True
            LOG.warning("Claude limit failure; falling back to Codex: %s", exc)

    raise RuntimeError("No hybrid backend available: {}".format("; ".join(errors)))


# Single seam bot.py calls through — everything above this point is
# executor-specific, everything below (routing, dedup, Telegram I/O,
# commands) is identical regardless of EXECUTOR.
def run_turn(prompt: str, session_id: Any) -> Tuple[str, Any]:
    if EXECUTOR == "hybrid":
        return run_hybrid(prompt, session_id)
    if EXECUTOR == "claude":
        return run_claude(prompt, session_id)
    if EXECUTOR == "deepseek":
        return run_deepseek(prompt, session_id)
    return run_codex(prompt, session_id)


# codex/deepseek run as a single headless `claude -p` subprocess.run() call
# (see ClaudeExecutorClient._run / CodexAppServerClient.run_turn) — one
# blocking call with no intermediate output at all, unlike the interactive
# --channels Claude session which can call reply/edit_message as it goes.
# Without this wrapper the Telegram placeholder sent at the top of
# process_prompt sits untouched for the entire turn, which is
# indistinguishable from a hang to the person watching Telegram.
HEARTBEAT_INTERVAL_SECONDS = float(os.environ.get("NEUROBOT_HEARTBEAT_INTERVAL", "15"))


def run_turn_with_heartbeat(
    prompt: str,
    session_id: Any,
    placeholder_id: int,
    sessions: Dict[str, Any],
    session_key: str,
) -> Tuple[str, Any]:
    """run_turn(), but edits the Telegram placeholder every ~15s while the
    blocking executor call is in flight so the chat doesn't go dark."""
    stop = threading.Event()

    def _tick() -> None:
        start = time.monotonic()
        while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            elapsed = int(time.monotonic() - start)
            try:
                edit_text(
                    placeholder_id,
                    with_model_label(
                        EXECUTOR,
                        "⏳ Ещё работаю... ({}с)".format(elapsed),
                        sessions.get(session_key),
                    ),
                )
            except Exception:
                LOG.exception(
                    "Heartbeat edit_text failed for placeholder_id=%s", placeholder_id
                )

    thread = threading.Thread(target=_tick, daemon=True)
    thread.start()
    try:
        return run_turn(prompt, session_id)
    finally:
        # Stop and join *before* the caller's own final edit_text, so a
        # trailing heartbeat tick can never race the real answer and
        # clobber it back to "⏳ Ещё работаю...".
        stop.set()
        thread.join(timeout=5)


def create_session() -> Any:
    if EXECUTOR == "hybrid":
        return {
            "mode": "auto",
            "claude": NEW_SESSION_SENTINEL,
            "codex": NEW_SESSION_SENTINEL,
            "last_backend": None,
        }
    if EXECUTOR == "claude":
        return create_claude_session()
    if EXECUTOR == "deepseek":
        return create_deepseek_session()
    return create_codex_session()


def read_rate_limit_status() -> Dict[str, Any]:
    if EXECUTOR == "hybrid":
        # Claude has no pre-flight subscription meter. Auto mode must be
        # allowed to try Claude; Codex is checked immediately before fallback.
        return read_claude_rate_limit_status()
    if EXECUTOR == "claude":
        return read_claude_rate_limit_status()
    if EXECUTOR == "deepseek":
        return read_deepseek_rate_limit_status()
    return read_codex_rate_limit_status()


def format_hybrid_limit_status() -> str:
    claude = read_claude_rate_limit_status()
    try:
        codex = read_codex_rate_limit_status()
        codex_text = format_rate_limit_status(codex, not codex["allowed"])
    except Exception as exc:
        codex_text = "⚠️ Codex: лимит недоступен ({})".format(str(exc)[:300])
    return "Claude: {}\n{}".format(claude["details"], codex_text)


def command_name(text: str) -> str:
    first = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ""
    return first.split("@", 1)[0]



def command_target(text: str) -> str:
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return first.split("@", 1)[1].lower() if "@" in first else ""


def parse_routed_message(text: str) -> Optional[Dict[str, Any]]:
    lines = text.strip().splitlines()
    if not lines:
        return None
    command = command_name(lines[0])
    if command not in ("/task", "/result", "/message", "/reply"):
        return None
    if command_target(lines[0]) != BOT_USERNAME.lower():
        return None
    headers: Dict[str, str] = {}
    body_start = len(lines)
    for index, line in enumerate(lines[1:], start=1):
        if not line.strip():
            body_start = index + 1
            break
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    task_id = headers.get("task-id", "")
    from_agent = headers.get("from-agent", "").lower()
    try:
        hop = int(headers.get("hop", "1"))
        part_text = headers.get("part", "1/1")
        part_number, part_total = (int(value) for value in part_text.split("/", 1))
    except ValueError:
        return None
    body = "\n".join(lines[body_start:]).strip()
    if (
        not task_id
        or not from_agent
        or not body
        or hop < 1
        or hop > MAX_ROUTE_HOPS
        or part_number < 1
        or part_total < 1
        or part_number > part_total
        or part_total > 100
    ):
        return None
    return {
        "kind": command.lstrip("/"),
        "task_id": task_id,
        "from_agent": from_agent,
        "reply_agent": headers.get("reply-agent", from_agent).lower(),
        "hop": hop,
        "part": part_number,
        "part_total": part_total,
        "body": body,
    }


def extract_outputs(
    answer: str,
) -> Tuple[str, List[Tuple[str, str, str]], List[Tuple[str, str]]]:
    routes: List[Tuple[int, str, str, str]] = []
    human_messages: List[Tuple[int, str, str]] = []
    for match in DELEGATION_PATTERN.finditer(answer):
        body = match.group(2).strip()
        if body:
            routes.append((match.start(), "task", match.group(1).lower(), body))
    for match in MESSAGE_PATTERN.finditer(answer):
        body = match.group(2).strip()
        if body:
            routes.append((match.start(), "message", match.group(1).lower(), body))
    for match in HUMAN_MESSAGE_PATTERN.finditer(answer):
        body = match.group(2).strip()
        if body:
            human_messages.append(
                (match.start(), match.group(1).lower(), body)
            )
    routes.sort(key=lambda item: item[0])
    human_messages.sort(key=lambda item: item[0])
    visible = DELEGATION_PATTERN.sub("", answer)
    visible = MESSAGE_PATTERN.sub("", visible)
    visible = HUMAN_MESSAGE_PATTERN.sub("", visible).strip()
    return (
        visible,
        [(kind, target, body) for _, kind, target, body in routes],
        [(username, body) for _, username, body in human_messages],
    )


def extract_routes(answer: str) -> Tuple[str, List[Tuple[str, str, str]]]:
    visible, routes, _ = extract_outputs(answer)
    return visible, routes


def extract_delegations(answer: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Backward-compatible task-only view used by older integrations."""
    visible, routes = extract_routes(answer)
    return visible, [
        (target, body) for kind, target, body in routes if kind == "task"
    ]


def split_route_body(text: str, limit: int = 3000) -> List[str]:
    text = text.strip()
    chunks: List[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks or ["Результат пуст."]


def send_routed_message(
    kind: str,
    peer: Dict[str, Any],
    task_id: str,
    reply_agent: str,
    hop: int,
    body: str,
) -> None:
    parts = split_route_body(body)
    for index, part in enumerate(parts, start=1):
        message = (
            "/{kind}" + chr(64) + peer["username"] + "\n"
            "Task-ID: {task_id}\n"
            "From-Agent: {from_agent}\n"
            "Reply-Agent: {reply_agent}\n"
            "Hop: {hop}\n"
            "Part: {part_number}/{part_total}\n\n"
            "{body}"
        ).format(
            kind=kind,
            task_id=task_id,
            from_agent=AGENT_KEY,
            reply_agent=reply_agent,
            hop=hop,
            part_number=index,
            part_total=len(parts),
            body=part,
        )
        send_text_to_thread(peer["thread_id"], message)


def dispatch_task(target_key: str, body: str, hop: int = 1) -> str:
    peer = PEERS_BY_KEY.get(target_key)
    if not peer or target_key == AGENT_KEY:
        raise RuntimeError("Unknown or invalid target agent: {}".format(target_key))
    if hop > MAX_ROUTE_HOPS:
        raise RuntimeError("Maximum routing depth exceeded")
    task_id = "{}-{}-{}".format(AGENT_KEY, int(time.time()), os.urandom(3).hex())
    send_routed_message("task", peer, task_id, AGENT_KEY, hop, body)
    return task_id


def dispatch_message(target_key: str, body: str, hop: int = 1) -> str:
    peer = PEERS_BY_KEY.get(target_key)
    if not peer or target_key == AGENT_KEY:
        raise RuntimeError("Unknown or invalid target agent: {}".format(target_key))
    if hop > MAX_ROUTE_HOPS:
        raise RuntimeError("Maximum routing depth exceeded")
    message_id = "{}-{}-{}".format(AGENT_KEY, int(time.time()), os.urandom(3).hex())
    send_routed_message("message", peer, message_id, AGENT_KEY, hop, body)
    return message_id


def dispatch_result(target_key: str, task_id: str, body: str, hop: int) -> None:
    peer = PEERS_BY_KEY.get(target_key)
    if not peer:
        raise RuntimeError("Unknown result target agent: {}".format(target_key))
    if hop > MAX_ROUTE_HOPS:
        raise RuntimeError("Maximum routing depth exceeded")
    send_routed_message("result", peer, task_id, AGENT_KEY, hop, body)


def dispatch_reply(target_key: str, message_id: str, body: str, hop: int) -> None:
    peer = PEERS_BY_KEY.get(target_key)
    if not peer:
        raise RuntimeError("Unknown reply target agent: {}".format(target_key))
    if hop > MAX_ROUTE_HOPS:
        raise RuntimeError("Maximum routing depth exceeded")
    send_routed_message("reply", peer, message_id, AGENT_KEY, hop, body)


def rate_limit_block_message() -> Optional[str]:
    try:
        limit_status = read_rate_limit_status()
        if not limit_status["allowed"]:
            return format_rate_limit_status(limit_status, blocked=True)
    except Exception:
        LOG.exception("Failed to verify rate limits before turn")
        if RATE_LIMIT_FAIL_CLOSED:
            return (
                "⛔ Задача не запущена: не удалось проверить остаток лимита. "
                "Включён безопасный режим; повторите запрос позже или используйте /limits."
            )
    return None


def return_routed_response(routed: Dict[str, Any], body: str) -> None:
    if routed["kind"] == "task":
        dispatch_result(
            routed["reply_agent"],
            routed["task_id"],
            body,
            routed["hop"],
        )
    elif routed["kind"] == "message":
        dispatch_reply(
            routed["reply_agent"],
            routed["task_id"],
            body,
            routed["hop"],
        )


def process_prompt(
    prompt: str,
    state: Dict[str, Any],
    session_key: str,
    reply_to: Optional[int] = None,
    routed: Optional[Dict[str, Any]] = None,
    placeholder_text: str = "⏳ Задача получена, анализирую...",
    required_message_targets: Optional[Set[str]] = None,
    required_human_usernames: Optional[Set[str]] = None,
    captured_human_messages: Optional[List[Tuple[str, str]]] = None,
    allowed_task_targets: Optional[Set[str]] = None,
    max_task_routes: Optional[int] = None,
    allowed_message_targets: Optional[Set[str]] = None,
    captured_visible_answers: Optional[List[str]] = None,
    captured_routes: Optional[List[Tuple[str, str, str]]] = None,
) -> bool:
    limit_block = rate_limit_block_message()
    if limit_block:
        send_text(limit_block, reply_to)
        if routed and routed["kind"] in ("task", "message"):
            try:
                return_routed_response(routed, limit_block)
            except Exception:
                LOG.exception("Failed to return rate-limit rejection to sender")
        return False

    sessions = state.setdefault("sessions", {})
    placeholder = send_text(
        with_model_label(EXECUTOR, placeholder_text, sessions.get(session_key)),
        reply_to,
    )
    placeholder_id = int(placeholder["message_id"])
    try:
        executor_prompt = "{}\n\n{}".format(prompt, TELEGRAM_FILE_GUIDANCE)
        answer, new_session_id = run_turn_with_heartbeat(
            executor_prompt, sessions.get(session_key), placeholder_id, sessions, session_key
        )
        remember_codex_bridge_answer(state, answer)
        if new_session_id:
            sessions[session_key] = new_session_id
            save_state(state)

        visible_answer, routes, human_messages = extract_outputs(answer)
        if required_message_targets is not None:
            actual_targets = [
                target for kind, target, _ in routes if kind == "message"
            ]
            if (
                set(actual_targets) != required_message_targets
                or len(actual_targets) != len(required_message_targets)
            ):
                raise RuntimeError(
                    "Motivator must return exactly one telegram_message for: {}".format(
                        ", ".join(sorted(required_message_targets))
                    )
                )
        if required_human_usernames is not None:
            actual_usernames = [username for username, _ in human_messages]
            if (
                set(actual_usernames) != required_human_usernames
                or len(actual_usernames) != len(required_human_usernames)
            ):
                exact_blocks = "\n".join(
                    '<telegram_human username="{username}">Личное сообщение для '
                    "{username}</telegram_human>".format(username=username)
                    for username in sorted(required_human_usernames)
                )
                correction_prompt = (
                    "Предыдущий ответ не опубликован: мост не нашёл обязательные "
                    "telegram_human-блоки. Повтори подготовленные личные сообщения, "
                    "но теперь верни строго по одному блоку для каждого указанного "
                    "username и не используй telegram_message или telegram_task.\n\n"
                    "Точная структура обязательных блоков:\n{blocks}\n\n"
                    "Замени текст-заглушку внутри каждого блока на короткое персональное "
                    "сообщение по уже переданным профилям. Не пиши ничего вне блоков."
                ).format(blocks=exact_blocks)
                corrected_answer, corrected_session_id = run_turn(
                    correction_prompt,
                    new_session_id or sessions.get(session_key),
                )
                remember_codex_bridge_answer(state, corrected_answer)
                if corrected_session_id:
                    sessions[session_key] = corrected_session_id
                    save_state(state)
                visible_answer, routes, human_messages = extract_outputs(
                    corrected_answer
                )
                actual_usernames = [username for username, _ in human_messages]
                if (
                    set(actual_usernames) != required_human_usernames
                    or len(actual_usernames) != len(required_human_usernames)
                ):
                    raise RuntimeError(
                        "Motivator must return exactly one telegram_human for: {}".format(
                            ", ".join(
                                "@{}".format(username)
                                for username in sorted(required_human_usernames)
                            )
                        )
                    )
            if routes:
                raise RuntimeError(
                    "Motivator human round must not contain agent routing blocks"
                )
        elif human_messages:
            raise RuntimeError(
                "Direct human messages are allowed only in a configured motivation round"
            )
        task_routes = [
            (target, body)
            for kind, target, body in routes
            if kind == "task"
        ]
        message_routes = [
            (target, body)
            for kind, target, body in routes
            if kind == "message"
        ]
        if allowed_task_targets is not None:
            unexpected_task_targets = sorted(
                {
                    target
                    for target, _ in task_routes
                    if target not in allowed_task_targets
                }
            )
            if unexpected_task_targets:
                raise RuntimeError(
                    "Task targets are not allowed in this turn: {}".format(
                        ", ".join(unexpected_task_targets)
                    )
                )
        if max_task_routes is not None and len(task_routes) > max_task_routes:
            raise RuntimeError(
                "This turn allows at most {} delegated task(s)".format(
                    max_task_routes
                )
            )
        if allowed_message_targets is not None:
            unexpected_message_targets = sorted(
                {
                    target
                    for target, _ in message_routes
                    if target not in allowed_message_targets
                }
            )
            if unexpected_message_targets:
                raise RuntimeError(
                    "Message targets are not allowed in this turn: {}".format(
                        ", ".join(unexpected_message_targets)
                    )
                )
        attachments: List[Path] = []
        if required_human_usernames is None:
            visible_answer, attachments = extract_local_attachments(visible_answer)
        if captured_visible_answers is not None:
            captured_visible_answers.append(visible_answer)
        if captured_routes is not None:
            captured_routes.extend(routes)
        if required_human_usernames is not None:
            # Replace the temporary progress message with the first real
            # personal message. This leaves no separate "round started" or
            # "message processed" noise in the Telegram topic.
            first_username, first_body = human_messages[0]
            edit_text(
                placeholder_id,
                "@{}\n\n{}".format(first_username, first_body),
            )
            for username, body in human_messages[1:]:
                send_text("@{}\n\n{}".format(username, body))
            visible_answer = ""
        else:
            visible_answer = (
                visible_answer
                or "Сообщение обработано; маршрутизатор выполняет передачу."
            )
            chunks = split_message(visible_answer)
            edit_text(
                placeholder_id,
                with_model_label(EXECUTOR, chunks[0], sessions.get(session_key)),
            )
            for chunk in chunks[1:]:
                send_text(chunk)

        for attachment in attachments:
            try:
                send_document(attachment, reply_to)
            except Exception:
                LOG.exception("Failed to send Telegram attachment path=%s", attachment)
                send_text(
                    "⚠️ Не удалось прикрепить файл `{}`. Попробуйте запросить его ещё раз.".format(
                        attachment.name
                    ),
                    reply_to,
                )

        next_hop = routed["hop"] + 1 if routed else 1
        for route_kind, target_key, body in routes:
            peer = PEERS_BY_KEY.get(target_key)
            if route_kind == "task":
                route_id = dispatch_task(target_key, body, next_hop)
                label = "Задача"
            else:
                route_id = dispatch_message(target_key, body, next_hop)
                label = "Сообщение"
            send_text(
                "📤 {} `{}` доставлено в топик сотрудника @{}.".format(
                    label, route_id, peer["username"]
                )
            )
        if required_human_usernames is None:
            for username, body in human_messages:
                send_text("@{}\n\n{}".format(username, body))
        if captured_human_messages is not None:
            captured_human_messages.extend(human_messages)

        if routed and routed["kind"] in ("task", "message"):
            return_routed_response(routed, visible_answer)
            peer = PEERS_BY_KEY.get(routed["reply_agent"])
            if peer:
                label = "Результат" if routed["kind"] == "task" else "Ответ"
                send_text(
                    "📤 {} `{}` отправлен в топик сотрудника @{}.".format(
                        label, routed["task_id"], peer["username"]
                    )
                )
        return True
    except Exception as exc:
        LOG.exception("Failed to process message")
        error_text = "❌ Не удалось обработать задачу: {}".format(str(exc)[:1000])
        try:
            edit_text(placeholder_id, error_text)
        except Exception:
            LOG.exception(
                "Also failed to edit placeholder message_id=%s with the error",
                placeholder_id,
            )
            try:
                send_text(error_text)
            except Exception:
                LOG.exception("Also failed to send a fallback error message")
        return False


def motivation_targets(
    target_usernames: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    targets: List[Dict[str, Any]] = []
    if PEOPLE_BY_USERNAME:
        for username, person in PEOPLE_BY_USERNAME.items():
            if person.get("motivation_enabled", True) is False:
                continue
            if target_usernames is not None and username not in target_usernames:
                continue
            targets.append(
                {
                    "key": username,
                    "username": username,
                    "display_name": str(person.get("display_name") or username),
                    "role": str(person.get("role") or "сотрудник"),
                    "description": str(person.get("description") or ""),
                    "motivation_goal": str(person.get("motivation_goal") or ""),
                    "cautions": str(person.get("cautions") or ""),
                    "motivation_style": str(person.get("motivation_style") or ""),
                    "news_interests": str(person.get("news_interests") or ""),
                    "allow_mild_profanity": bool(
                        person.get("allow_mild_profanity", False)
                    ),
                    "daily_time": str(person.get("daily_time") or DAILY_TIME),
                }
            )
        return targets
    for key, peer in PEERS_BY_KEY.items():
        if key == AGENT_KEY or peer.get("motivation_enabled", True) is False:
            continue
        targets.append(
            {
                "key": key,
                "display_name": str(peer.get("display_name") or key),
                "role": str(peer.get("role") or "сотрудник"),
                "description": str(peer.get("description") or ""),
                "motivation_style": str(peer.get("motivation_style") or ""),
            }
        )
    return targets


def recent_motivation_history(
    state: Optional[Dict[str, Any]],
    target_usernames: Set[str],
) -> Dict[str, List[Dict[str, Any]]]:
    if not state:
        return {}
    history = state.get("motivation_history", {})
    if not isinstance(history, dict):
        return {}
    return {
        username: list(history.get(username, []))[-7:]
        for username in target_usernames
        if isinstance(history.get(username), list)
    }


def build_motivation_prompt(
    current: datetime,
    state: Optional[Dict[str, Any]] = None,
    target_usernames: Optional[Set[str]] = None,
) -> str:
    targets = motivation_targets(target_usernames)
    if not targets:
        raise RuntimeError("No motivation targets are configured")
    profiles = json.dumps(targets, ensure_ascii=False, indent=2)
    selected_keys = {str(target["key"]) for target in targets}
    history = json.dumps(
        recent_motivation_history(state, selected_keys),
        ensure_ascii=False,
        indent=2,
    )
    if PEOPLE_BY_USERNAME:
        delivery_instruction = (
            "Для каждого человека создай ровно один блок "
            '<telegram_human username="USERNAME">...</telegram_human>.\n'
            "Эти блоки будут опубликованы отдельными сообщениями в общем топике Motivator. "
            "Не добавляй @username внутрь текста: мост добавит упоминание сам.\n"
        )
        forbidden_instruction = (
            "Используй биографию только как внутренний контекст. Не пересказывай публично возраст, "
            "нехватку заказов, сомнения, импульсивность или другие чувствительные детали. "
            "Не называй человека ленивым, не упоминай вредные привычки и не используй профиль "
            "как средство давления. Мягкое крепкое слово допустимо только если в профиле "
            "allow_mild_profanity=true, оно звучит дружески и не направлено против человека. "
            "Не используй telegram_task и telegram_message."
        )
    else:
        delivery_instruction = (
            "Для каждого сотрудника создай ровно один блок "
            '<telegram_message target="KEY">...</telegram_message>.\n'
        )
        forbidden_instruction = (
            "Не используй telegram_task. Не отправляй повторное сообщение тому же сотруднику."
        )
    return (
        "Подготовь ежедневное личное сообщение от обычного живого коллеги.\n"
        "Локальная дата: {date}.\n\n"
        "Профили сотрудников:\n{profiles}\n\n"
        "Последние сообщения, которые нельзя повторять или близко перефразировать:\n"
        "{history}\n\n"
        "{delivery_instruction}"
        "Внутри каждого блока:\n"
        "- обратись к сотруднику лично по имени;\n"
        "- пиши коротко и естественно, как человек в общем чате, без служебных пояснений;\n"
        "- не добавляй фразы вроде «это дружеский привет», «это не рабочее поручение» "
        "или описания того, зачем ты написал;\n"
        "- меняй приём: иногда просто подкол, иногда вопрос о делах, иногда короткая "
        "поддержка по роли, иногда смешное наблюдение;\n"
        "- допустимы добрые подколы в духе «Чего не работаешь?» или "
        "«Хватит валяться! Боты кушать хотят 😄», если они подходят человеку; "
        "это должна быть очевидная шутка, а не реальное обвинение в безделье;\n"
        "- не обязательно задавать вопрос, хвалить, мотивировать или добавлять шутку "
        "в каждом сообщении.\n\n"
        "По умолчанию не используй новости и не запускай web search. Новостной повод "
        "добавляй лишь изредка, когда он действительно уместен и такой приём не встречался "
        "в недавней истории. Тогда используй только свежий проверяемый факт за последние "
        "7 дней из надёжного первичного источника и добавь прямую ссылку. Не выбирай "
        "трагедии, политику или тревожные темы и ничего не выдумывай.\n\n"
        "{forbidden_instruction} "
        "Верни только обязательные блоки. Не пиши ничего до, после или между ними."
    ).format(
        date=current.date().isoformat(),
        profiles=profiles,
        history=history,
        delivery_instruction=delivery_instruction,
        forbidden_instruction=forbidden_instruction,
    )


def person_schedule_time(person: Dict[str, Any]) -> Tuple[int, int]:
    return parse_daily_time(str(person.get("daily_time") or DAILY_TIME))


def person_local_schedule_time(
    person: Dict[str, Any],
    now: Optional[datetime] = None,
) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    timezone_name = str(person.get("daily_timezone") or DAILY_TIMEZONE)
    return current.astimezone(ZoneInfo(timezone_name))


def person_daily_run_due(
    username: str,
    person: Dict[str, Any],
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    if person.get("motivation_enabled", True) is False:
        return False
    current = person_local_schedule_time(person, now)
    if (current.hour, current.minute) < person_schedule_time(person):
        return False
    scheduled_runs = state.get("scheduled_runs", {})
    personal = scheduled_runs.get("daily_motivation_people", {}).get(username, {})
    if personal.get("local_date") == current.date().isoformat():
        return False
    # Migration guard: an older all-at-once scheduler may already have
    # completed today's round before per-person schedules were deployed.
    legacy = scheduled_runs.get("daily_motivation", {})
    if (
        not personal
        and legacy.get("local_date") == current.date().isoformat()
        and str(legacy.get("status", "")).startswith("completed")
    ):
        return False
    return True


def due_motivation_usernames(
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> List[str]:
    due = [
        username
        for username, person in PEOPLE_BY_USERNAME.items()
        if person_daily_run_due(username, person, state, now)
    ]
    return sorted(
        due,
        key=lambda username: person_schedule_time(PEOPLE_BY_USERNAME[username]),
    )


def daily_run_due(state: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    if not DAILY_ENABLED or AGENT_KEY != "motivator":
        return False
    if PEOPLE_BY_USERNAME:
        return bool(due_motivation_usernames(state, now))
    current = local_schedule_time(now)
    hour, minute = parse_daily_time(DAILY_TIME)
    if (current.hour, current.minute) < (hour, minute):
        return False
    last_run = state.get("scheduled_runs", {}).get("daily_motivation", {})
    return last_run.get("local_date") != current.date().isoformat()


def run_motivation_round(
    state: Dict[str, Any],
    reply_to: Optional[int] = None,
    now: Optional[datetime] = None,
    target_usernames: Optional[Set[str]] = None,
) -> bool:
    if AGENT_KEY != "motivator":
        send_text("Команда доступна только агенту «Мотиватор».", reply_to)
        return False
    current = local_schedule_time(now)
    session_key = "{}:{}".format(TARGET_CHAT_ID, TARGET_THREAD_ID)
    target_keys = {
        str(target["key"]) for target in motivation_targets(target_usernames)
    }
    captured_human_messages: List[Tuple[str, str]] = []
    try:
        prompt = build_motivation_prompt(
            current,
            state=state,
            target_usernames=target_usernames,
        )
    except Exception as exc:
        LOG.exception("Failed to build motivation prompt")
        send_text(
            "❌ Не удалось подготовить ежедневный обход: {}".format(str(exc)[:500]),
            reply_to,
        )
        return False
    success = process_prompt(
        prompt,
        state,
        session_key,
        reply_to=reply_to,
        placeholder_text="☀️ Готовлю ежедневные личные сообщения сотрудникам...",
        required_message_targets=None if PEOPLE_BY_USERNAME else target_keys,
        required_human_usernames=target_keys if PEOPLE_BY_USERNAME else None,
        captured_human_messages=captured_human_messages,
    )
    if success and captured_human_messages:
        history = state.setdefault("motivation_history", {})
        for username, body in captured_human_messages:
            entries = history.setdefault(username, [])
            entries.append(
                {
                    "local_date": current.date().isoformat(),
                    "text": body,
                }
            )
            del entries[:-14]
        save_state(state)
    return success


def record_daily_run(
    state: Dict[str, Any],
    current: datetime,
    status: str,
) -> None:
    state.setdefault("scheduled_runs", {})["daily_motivation"] = {
        "local_date": current.date().isoformat(),
        "status": status,
        "updated_at": int(time.time()),
    }
    save_state(state)


def record_person_daily_run(
    state: Dict[str, Any],
    username: str,
    current: datetime,
    status: str,
) -> None:
    state.setdefault("scheduled_runs", {}).setdefault(
        "daily_motivation_people", {}
    )[username] = {
        "local_date": current.date().isoformat(),
        "status": status,
        "updated_at": int(time.time()),
    }
    save_state(state)


def maybe_run_daily_initiative(
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    if not daily_run_due(state, now):
        return False
    if PEOPLE_BY_USERNAME:
        any_success = False
        for username in due_motivation_usernames(state, now):
            person = PEOPLE_BY_USERNAME[username]
            current = person_local_schedule_time(person, now)
            record_person_daily_run(state, username, current, "started")
            LOG.info(
                "Starting personal motivation date=%s username=@%s time=%s timezone=%s",
                current.date().isoformat(),
                username,
                person.get("daily_time") or DAILY_TIME,
                person.get("daily_timezone") or DAILY_TIMEZONE,
            )
            success = run_motivation_round(
                state,
                now=current,
                target_usernames={username},
            )
            record_person_daily_run(
                state,
                username,
                current,
                "completed" if success else "blocked_or_failed",
            )
            any_success = any_success or success
        return any_success
    current = local_schedule_time(now)
    record_daily_run(state, current, "started")
    LOG.info(
        "Starting daily motivation round date=%s timezone=%s",
        current.date().isoformat(),
        DAILY_TIMEZONE,
    )
    success = run_motivation_round(state, now=current)
    record_daily_run(state, current, "completed" if success else "blocked_or_failed")
    return success


def manager_autonomy_local_time(
    now: Optional[datetime] = None,
) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(ZoneInfo(MANAGER_AUTONOMY_TIMEZONE))


def manager_autonomy_run_due(
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    if not MANAGER_AUTONOMY_ENABLED or AGENT_KEY != "manager":
        return False
    current = manager_autonomy_local_time(now)
    hour, minute = parse_daily_time(
        MANAGER_AUTONOMY_TIME,
        "NEUROBOT_MANAGER_AUTONOMY_TIME",
    )
    if (current.hour, current.minute) < (hour, minute):
        return False
    last_run = state.get("scheduled_runs", {}).get(
        "daily_manager_autonomy", {}
    )
    return last_run.get("local_date") != current.date().isoformat()


def record_manager_autonomy_run(
    state: Dict[str, Any],
    current: datetime,
    status: str,
    remaining_percent: Optional[int] = None,
) -> None:
    entry: Dict[str, Any] = {
        "local_date": current.date().isoformat(),
        "status": status,
        "updated_at": int(time.time()),
    }
    if remaining_percent is not None:
        entry["remaining_percent"] = remaining_percent
    state.setdefault("scheduled_runs", {})["daily_manager_autonomy"] = entry
    save_state(state)


def recent_manager_autonomy_history(
    state: Dict[str, Any],
) -> List[Dict[str, Any]]:
    history = state.get("manager_autonomy_history", [])
    if not isinstance(history, list):
        return []
    return [
        item for item in history[-7:] if isinstance(item, dict)
    ]


def build_manager_autonomy_prompt(
    current: datetime,
    remaining_percent: int,
    state: Dict[str, Any],
) -> str:
    history = json.dumps(
        recent_manager_autonomy_history(state),
        ensure_ascii=False,
        indent=2,
    )
    allowed_targets = ", ".join(
        sorted(MANAGER_AUTONOMY_ALLOWED_TARGETS)
    )
    strategy_context = MANAGER_AUTONOMY_STRATEGY_CONTEXT or (
        "Дополнительный рыночный приоритет владельцем не задан."
    )
    return (
        "Запусти один автономный управленческий цикл для продвижения компании, "
        "которую ты представляешь.\n"
        "Локальная дата и время: {current}.\n"
        "Остаток лимита Codex: {remaining}% — автономная работа разрешена.\n\n"
        "Сначала прочитай AGENTS.md, подтверждённые файлы в knowledge/ и учти "
        "текущий контекст этой Manager-сессии. Не придумывай факты о клиентах, "
        "выручке, загрузке, результатах, сроках или доступных ресурсах.\n\n"
        "Обязательный стратегический контекст от владельца:\n"
        "{strategy_context}\n"
        "Следуй указанному порядку рынков. Переходи ко второму рынку, только если "
        "первый уже достаточно проработан в недавних инициативах, временно заблокирован "
        "или проверяемые данные показывают, что конкретная гипотеза там слабее. "
        "За один цикл выбирай один рынок и одну географию, не распыляй задачу между "
        "несколькими странами.\n\n"
        "Выбери ровно один действительно полезный следующий шаг, который способен "
        "продвинуть компанию: улучшить позиционирование, привлечение лидов, конверсию, "
        "упаковку услуг, продажи либо создать конкретный технический инструмент для "
        "этой цели. Не создавай работу ради работы.\n\n"
        "Разрешены два варианта:\n"
        "1. Коротко предложить основателям один вариант и понятный следующий шаг — "
        "без делегирования.\n"
        "2. Поставить ровно одну ограниченную задачу одному агенту через "
        '<telegram_task target="TARGET">...</telegram_task>. '
        "Разрешённые TARGET: {targets}.\n\n"
        "Маркетологу поручай проверяемые исследования, позиционирование, контент, SEO, "
        "PPC или outreach. Разработчику поручай только конкретный технический результат, "
        "непосредственно помогающий продвижению или продажам; не поручай абстрактный "
        "рефакторинг. В задаче укажи название, цель, контекст, ожидаемый результат, "
        "критерии приёмки и ограничения. Не выдумывай дедлайны.\n\n"
        "В видимой части ответа напиши 3–7 коротких строк: что выбрано, почему это "
        "полезно и что произойдёт дальше. Не используй telegram_message или "
        "telegram_human. Не создавай больше одного telegram_task.\n\n"
        "Последние автономные инициативы — не повторяй их и не перефразируй близко:\n"
        "{history}"
    ).format(
        current=current.strftime("%Y-%m-%d %H:%M %Z"),
        remaining=remaining_percent,
        targets=allowed_targets,
        strategy_context=strategy_context,
        history=history,
    )


def run_manager_autonomy(
    state: Dict[str, Any],
    current: datetime,
    remaining_percent: int,
) -> bool:
    session_key = "{}:{}".format(TARGET_CHAT_ID, TARGET_THREAD_ID)
    visible_answers: List[str] = []
    routes: List[Tuple[str, str, str]] = []
    success = process_prompt(
        build_manager_autonomy_prompt(current, remaining_percent, state),
        state,
        session_key,
        placeholder_text="🚀 Ищу следующий полезный шаг для компании...",
        allowed_task_targets=MANAGER_AUTONOMY_ALLOWED_TARGETS,
        max_task_routes=1,
        allowed_message_targets=set(),
        captured_visible_answers=visible_answers,
        captured_routes=routes,
    )
    if success:
        history = state.setdefault("manager_autonomy_history", [])
        history.append(
            {
                "local_date": current.date().isoformat(),
                "remaining_percent": remaining_percent,
                "visible_answer": (visible_answers[0] if visible_answers else "")[:4000],
                "tasks": [
                    {
                        "target": target,
                        "body": body[:6000],
                    }
                    for kind, target, body in routes
                    if kind == "task"
                ],
            }
        )
        del history[:-14]
        save_state(state)
    return success


def maybe_run_manager_autonomy(
    state: Dict[str, Any],
    now: Optional[datetime] = None,
) -> bool:
    if not manager_autonomy_run_due(state, now):
        return False
    current = manager_autonomy_local_time(now)
    record_manager_autonomy_run(state, current, "checking_limit")
    try:
        limit_status = read_rate_limit_status()
    except Exception:
        LOG.exception("Failed to check manager autonomy limit")
        record_manager_autonomy_run(state, current, "limit_unavailable")
        return False

    remaining_percent = int(limit_status.get("remaining_percent", 0))
    if (
        not limit_status.get("allowed", False)
        or remaining_percent <= MANAGER_AUTONOMY_MIN_REMAINING_PERCENT
    ):
        LOG.info(
            "Skipped manager autonomy date=%s remaining=%s threshold=>%s",
            current.date().isoformat(),
            remaining_percent,
            MANAGER_AUTONOMY_MIN_REMAINING_PERCENT,
        )
        record_manager_autonomy_run(
            state,
            current,
            "skipped_low_limit",
            remaining_percent,
        )
        return False

    LOG.info(
        "Starting manager autonomy date=%s remaining=%s threshold=>%s",
        current.date().isoformat(),
        remaining_percent,
        MANAGER_AUTONOMY_MIN_REMAINING_PERCENT,
    )
    success = run_manager_autonomy(state, current, remaining_percent)
    record_manager_autonomy_run(
        state,
        current,
        "completed" if success else "blocked_or_failed",
        remaining_percent,
    )
    return success


def person_for_sender(
    sender_id: int,
    sender_username: str,
    state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    username = sender_username.strip().lstrip("@").lower()
    if username and username in PEOPLE_BY_USERNAME:
        person = PEOPLE_BY_USERNAME[username]
        configured_id = person.get("telegram_user_id")
        if configured_id is not None and int(configured_id) != sender_id:
            return None
        bindings = state.setdefault("people_user_ids", {})
        bound_id = bindings.get(username)
        if bound_id is not None and int(bound_id) != sender_id:
            return None
        if bound_id is None:
            bindings[username] = sender_id
            save_state(state)
        return person
    for bound_username, bound_id in state.get("people_user_ids", {}).items():
        if int(bound_id) == sender_id:
            return PEOPLE_BY_USERNAME.get(bound_username)
    for person in PEOPLE_BY_USERNAME.values():
        if person.get("telegram_user_id") == sender_id:
            return person
    return None


def human_sender_allowed(
    sender_id: int,
    sender_username: str,
    state: Dict[str, Any],
) -> bool:
    # The message has already been verified to originate in TARGET_CHAT_ID.
    # Bot senders take the separate authenticated peer-routing branch, so this
    # switch applies only to ordinary Telegram users in the configured group.
    if ALLOW_ALL_CHAT_MEMBERS and sender_id > 0:
        return True
    if sender_id in ALLOWED_USER_IDS:
        return True
    return person_for_sender(sender_id, sender_username, state) is not None


def accept_routed_message(
    message: Dict[str, Any],
    original: str,
    state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Validate, reassemble and deduplicate one peer-bot route.

    This seam is shared by the long-poll bridge and the persistent Claude
    channel gateway. Returning ``None`` means the message must not reach an
    executor (invalid, duplicate, or a multipart route still being assembled).
    """
    sender = message.get("from") or {}
    sender_id = int(sender.get("id", 0))
    thread_id = int(message.get("message_thread_id", 0))
    peer = PEERS_BY_BOT_ID.get(sender_id)
    routed = parse_routed_message(original)
    if (
        not peer
        or not routed
        or routed["from_agent"] != peer["key"]
        or thread_id != TARGET_THREAD_ID
    ):
        LOG.warning("Ignored unauthorized bot sender id=%s thread=%s", sender_id, thread_id)
        return None

    route_key = "{}:{}:{}".format(routed["kind"], routed["task_id"], sender_id)
    processed = state.setdefault("processed_routes", {})
    if route_key in processed:
        LOG.info("Ignored duplicate routed message %s", route_key)
        return None

    if routed["part_total"] > 1:
        pending_routes = state.setdefault("pending_routes", {})
        pending = pending_routes.setdefault(
            route_key,
            {
                "kind": routed["kind"],
                "task_id": routed["task_id"],
                "from_agent": routed["from_agent"],
                "reply_agent": routed["reply_agent"],
                "hop": routed["hop"],
                "part_total": routed["part_total"],
                "parts": {},
                "updated_at": int(time.time()),
            },
        )
        if (
            pending.get("from_agent") != routed["from_agent"]
            or pending.get("reply_agent") != routed["reply_agent"]
            or int(pending.get("hop", 0)) != routed["hop"]
            or int(pending.get("part_total", 0)) != routed["part_total"]
        ):
            LOG.warning("Rejected inconsistent route chunks for %s", route_key)
            pending_routes.pop(route_key, None)
            save_state(state)
            return None
        pending["parts"][str(routed["part"])] = routed["body"]
        pending["updated_at"] = int(time.time())
        save_state(state)
        if len(pending["parts"]) < routed["part_total"]:
            LOG.info(
                "Stored route chunk %s/%s for %s",
                routed["part"],
                routed["part_total"],
                route_key,
            )
            return None
        routed["body"] = "\n\n".join(
            pending["parts"][str(index)]
            for index in range(1, routed["part_total"] + 1)
        )
        pending_routes.pop(route_key, None)

    processed[route_key] = int(time.time())
    if len(processed) > 500:
        for old_key, _ in sorted(processed.items(), key=lambda item: item[1])[:-400]:
            processed.pop(old_key, None)
    save_state(state)
    return routed


def build_routed_prompt(routed: Dict[str, Any]) -> str:
    """Turn an authenticated peer route into an executor-facing prompt."""
    if routed["kind"] == "task":
        return (
            "Получено реальное поручение через Telegram от сотрудника {from_agent}.\n"
            "Task-ID: {task_id}.\n"
            "Выполни поручение по своей роли. Ответ будет автоматически возвращён отправителю.\n"
            "Сотрудник может работать на другом сервере и через другой исполнитель, поэтому итоговый "
            "ответ должен содержать полный самодостаточный результат. Локальный путь к файлу "
            "можно указать только дополнительно, но он не заменяет сам результат.\n\n"
            "{body}"
        ).format(**routed)
    if routed["kind"] == "result":
        return (
            "Получен реальный результат через Telegram от сотрудника {from_agent}.\n"
            "Task-ID: {task_id}.\n"
            "Проверь результат, обнови статус и определи следующее действие.\n\n"
            "{body}"
        ).format(**routed)
    if routed["kind"] == "message":
        return (
            "Получено личное дружеское сообщение через Telegram от сотрудника {from_agent}.\n"
            "Message-ID: {task_id}.\n"
            "Это не рабочее поручение. Ответь коротко, естественно и по-человечески. "
            "Можно рассказать, как дела, поддержать шутку или задать один встречный вопрос. "
            "Ответ будет автоматически возвращён отправителю.\n\n"
            "{body}"
        ).format(**routed)
    return (
        "Получен личный ответ через Telegram от сотрудника {from_agent}.\n"
        "Message-ID: {task_id}.\n"
        "Кратко отметь ответ в своём топике. Не отправляй новое личное сообщение "
        "автоматически и не создавай бесконечный диалог.\n\n"
        "{body}"
    ).format(**routed)


def handle_message(
    message: Dict[str, Any],
    state: Dict[str, Any],
    session_key_suffix: str = "",
) -> None:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = int(chat.get("id", 0))
    thread_id = int(message.get("message_thread_id", 0))
    sender_id = int(sender.get("id", 0))
    sender_username = str(sender.get("username") or "")
    if chat_id != TARGET_CHAT_ID:
        return

    original = (message.get("text") or message.get("caption") or "").strip()
    if not original:
        return

    routed: Optional[Dict[str, Any]] = None
    if sender.get("is_bot"):
        routed = accept_routed_message(message, original, state)
        if not routed:
            return
    else:
        if thread_id != TARGET_THREAD_ID or not human_sender_allowed(
            sender_id, sender_username, state
        ):
            LOG.warning("Ignored unauthorized sender id=%s thread=%s", sender_id, thread_id)
            return

    inbound_message_id = message.get("message_id")
    if inbound_message_id is not None:
        set_reaction(int(inbound_message_id), ACK_REACTION)

    command = command_name(original)
    # session_key_suffix keeps backends that share this handler (Codex,
    # DeepSeek — both reached via the channel gateway forcing EXECUTOR and
    # calling this function directly, see channel_gateway.handle_inbound)
    # from clobbering each other's session id under the same state key.
    session_key = "{}:{}{}".format(TARGET_CHAT_ID, TARGET_THREAD_ID, session_key_suffix)
    sessions = state.setdefault("sessions", {})
    required_human_usernames: Optional[Set[str]] = None

    if not routed and command in ("/start", "/help"):
        send_text(
            "Я — {}. {}\n\nКоманды:\n/new — начать новый контекст\n"
            "/status — проверить состояние\n/limits — показать остаток лимита\n"
            "/backend auto|claude|codex — выбрать движок (для hybrid)\n"
            "/motivate — запустить личный обход сейчас (только Мотиватор)".format(
                AGENT_DISPLAY_NAME, AGENT_HELP_TEXT
            ),
            message.get("message_id"),
        )
        return
    if not routed and command == "/backend":
        if EXECUTOR != "hybrid":
            send_text(
                "Переключение движка недоступно: агент работает через {}.".format(
                    EXECUTOR
                ),
                message.get("message_id"),
            )
            return
        requested = original.split(maxsplit=1)[1].strip().lower() if len(
            original.split(maxsplit=1)
        ) > 1 else ""
        if requested not in {"auto", "claude", "codex"}:
            send_text(
                "Использование: /backend auto|claude|codex",
                message.get("message_id"),
            )
            return
        hybrid_session = normalize_hybrid_session(sessions.get(session_key))
        hybrid_session["mode"] = requested
        sessions[session_key] = hybrid_session
        save_state(state)
        send_text(
            "Режим разработчика переключён: {}.".format(requested),
            message.get("message_id"),
        )
        return
    if not routed and command == "/new":
        try:
            sessions[session_key] = create_session()
            save_state(state)
            send_text(
                "Новый контекст создан. Что нужно сделать?",
                message.get("message_id"),
            )
        except Exception as exc:
            LOG.exception("Failed to create session")
            send_text(
                "❌ Не удалось создать новый контекст: {}".format(str(exc)[:1000]),
                message.get("message_id"),
            )
        return
    if not routed and command == "/status":
        current_session = sessions.get(session_key)
        status = "активен" if current_session else "новый"
        backend_status = ""
        if EXECUTOR == "hybrid":
            hybrid_session = normalize_hybrid_session(current_session)
            backend_status = " Режим: {mode}; последний движок: {last}.".format(
                mode=hybrid_session.get("mode") or "auto",
                last=hybrid_session.get("last_backend") or "ещё не запускался",
            )
        schedule = ""
        if AGENT_KEY == "motivator":
            if PEOPLE_BY_USERNAME:
                personal_times = ", ".join(
                    "@{} — {} ({})".format(
                        username,
                        person.get("daily_time") or DAILY_TIME,
                        person.get("daily_timezone") or DAILY_TIMEZONE,
                    )
                    for username, person in PEOPLE_BY_USERNAME.items()
                    if person.get("motivation_enabled", True) is not False
                )
                schedule = " Расписание: {}{}.".format(
                    "включено: " if DAILY_ENABLED else "выключено: ",
                    personal_times,
                )
            else:
                schedule = " Расписание: {} в {} ({}).".format(
                    "включено" if DAILY_ENABLED else "выключено",
                    DAILY_TIME,
                    DAILY_TIMEZONE,
                )
        elif AGENT_KEY == "manager" and MANAGER_AUTONOMY_ENABLED:
            last_autonomy = state.get("scheduled_runs", {}).get(
                "daily_manager_autonomy", {}
            )
            last_status = str(last_autonomy.get("status") or "ещё не запускался")
            schedule = (
                " Автономное продвижение: включено в {time} ({timezone}), "
                "только при остатке >{threshold}%; сегодня: {status}."
            ).format(
                time=MANAGER_AUTONOMY_TIME,
                timezone=MANAGER_AUTONOMY_TIMEZONE,
                threshold=MANAGER_AUTONOMY_MIN_REMAINING_PERCENT,
                status=last_status,
            )
        send_text(
            "{} работает. Контекст: {}.{}{}".format(
                AGENT_DISPLAY_NAME, status, backend_status, schedule
            ),
            message.get("message_id"),
        )
        return
    if not routed and command == "/limits":
        try:
            if EXECUTOR == "hybrid":
                response_text = format_hybrid_limit_status()
            else:
                limit_status = read_rate_limit_status()
                response_text = format_rate_limit_status(
                    limit_status, not limit_status["allowed"]
                )
            send_text(response_text, message.get("message_id"))
        except Exception:
            LOG.exception("Failed to read rate limits")
            send_text(
                "⚠️ Не удалось получить текущий лимит.",
                message.get("message_id"),
            )
        return
    if not routed and command == "/motivate":
        if AGENT_KEY != "motivator":
            send_text(
                "Команда доступна только агенту «Мотиватор».",
                message.get("message_id"),
            )
            return
        current = local_schedule_time()
        record_daily_run(state, current, "started_manual")
        success = run_motivation_round(
            state,
            reply_to=message.get("message_id"),
            now=current,
        )
        record_daily_run(
            state,
            current,
            "completed_manual" if success else "blocked_or_failed_manual",
        )
        return

    if routed:
        prompt = build_routed_prompt(routed)
    else:
        prompt = clean_prompt(original)
        if AGENT_KEY == "motivator":
            person = person_for_sender(sender_id, sender_username, state)
            if person:
                required_human_usernames = {str(person["username"])}
                prompt = (
                    "Сообщение в общем топике Motivator от @{username}, имя: {display_name}.\n"
                    "Ответь лично этому человеку, коротко, тепло и естественно. Можно поддержать "
                    "разговор одной уместной шуткой или одним встречным вопросом. Верни ответ "
                    "ровно в одном telegram_human-блоке для @{username} и не пиши ничего вне "
                    "блока. Используй профиль "
                    "только как внутренний контекст и не пересказывай чувствительные детали публично.\n"
                    "Профиль: {profile}\n\n"
                    "Сообщение:\n{text}"
                ).format(
                    username=person["username"],
                    display_name=person.get("display_name") or person["username"],
                    profile=json.dumps(person, ensure_ascii=False),
                    text=prompt,
                )
    if not prompt:
        send_text("Напишите задачу после упоминания бота.", message.get("message_id"))
        return

    process_prompt(
        prompt,
        state,
        session_key,
        reply_to=message.get("message_id"),
        routed=routed,
        required_human_usernames=required_human_usernames,
    )


def main() -> None:
    require_config()
    initialize_peers()
    initialize_people()
    state = load_state()
    me = telegram("getMe")
    LOG.info("Starting bot @%s for chat=%s thread=%s", me.get("username"), TARGET_CHAT_ID, TARGET_THREAD_ID)
    initialize_codex_watcher(state)
    next_codex_watch = time.monotonic()
    while True:
        try:
            maybe_run_daily_initiative(state)
            maybe_run_manager_autonomy(state)
            if CODEX_WATCH_ENABLED and time.monotonic() >= next_codex_watch:
                poll_codex_watcher(state)
                next_codex_watch = time.monotonic() + CODEX_WATCH_INTERVAL
            long_poll_timeout = min(30, CODEX_WATCH_INTERVAL) if CODEX_WATCH_ENABLED else 30
            updates = telegram(
                "getUpdates",
                {
                    "offset": int(state.get("offset", 0)),
                    "timeout": long_poll_timeout,
                    "allowed_updates": json.dumps(["message", "edited_message"]),
                },
                timeout=long_poll_timeout + 10,
            )
            for update in updates:
                state["offset"] = int(update["update_id"]) + 1
                save_state(state)
                message = update.get("message") or update.get("edited_message")
                if message:
                    handle_message(message, state)
        except Exception:
            LOG.exception("Polling iteration failed")
            time.sleep(5)


if __name__ == "__main__":
    main()
