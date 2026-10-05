#!/usr/bin/env python3
"""Control plane used by the persistent Claude Telegram channel.

The Telegram channel is the only ``getUpdates`` consumer. Before an inbound
message is delivered to the live Claude session it calls this helper. The
helper enforces the Neurobot topic/peer rules and either returns a normalized
Claude prompt or executes the message through the shared Codex App Server.

Claude's Telegram ``reply`` tool calls the same helper for outbound text so
machine routing blocks are dispatched to peer bots and removed from the
human-visible response.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

try:
    from . import bot, relogin
except ImportError:  # Direct deployment beside bot.py.
    import bot  # type: ignore
    import relogin  # type: ignore


CHANNEL_MODE_KEY = "channel_backend_mode"
CHANNEL_LAST_BACKEND_KEY = "channel_last_backend"
CHANNEL_PENDING_ROUTES_KEY = "channel_pending_routes"
CHANNEL_LATEST_ROUTE_KEY = "channel_latest_route_message_id"
CHANNEL_PENDING_RELOGIN_KEY = "channel_pending_relogin"
VALID_MODES = {"auto", "claude", "codex", "deepseek"}
# Codex and DeepSeek are both dispatched through bot.handle_message with
# EXECUTOR forced for the duration of that call (see handle_inbound below);
# each needs its own session-state key or switching between them would feed
# one backend's session id to the other. Codex keeps the original unsuffixed
# key for backward compatibility with sessions created before this suffix
# existed.
DEEPSEEK_SESSION_KEY_SUFFIX = ":deepseek"
LOCK_FILE = bot.STATE_DIR / "channel-gateway.lock"

# ---------------------------------------------------------------------------
# /relogin — re-authenticate this tenant's own Claude account from Telegram.
#
# WHY THIS EXISTS AND WHY IT IS SHAPED THIS WAY. When a tenant's claude.ai OAuth
# login expires, the persistent Remote Control session stops working and the only
# fix used to be an operator opening a terminal on the box and running /login.
# The people who own these sessions are developers with their own claude.ai
# subscriptions but no shell access, so they were blocked on an operator.
#
# This command lets such a person re-authenticate THEIR OWN account on
# infrastructure they ALREADY have legitimate, allowlisted access to. It is
# deliberately NOT self-service signup: Anthropic's legal-and-compliance terms
# state that OAuth "is intended exclusively for purchasers of Claude Free, Pro,
# Max, Team, and Enterprise subscription plans" and that "Anthropic does not
# permit third-party developers to offer Claude.ai login ... on behalf of their
# users", enforced since Jan-Feb 2026. Offering login to arbitrary strangers
# would fall under that prohibition; a known human re-authenticating their own
# subscription into their own Claude Code session is ordinary use.
#
# The allowlist gate below is what keeps that distinction true in code. Do not
# loosen it, and do not extend this into an onboarding funnel for new users —
# The allowlist and existing-account checks below enforce that boundary.
# ---------------------------------------------------------------------------
RELOGIN_COMMAND = "/relogin"
RELOGIN_CANCEL_COMMAND = "/cancel"
RELOGIN_SOCKET = bot.STATE_DIR / "relogin.sock"
# Lifecycle events only — never the pty output and never the pasted code.
RELOGIN_LOG = bot.STATE_DIR / "relogin.log"
RELOGIN_TIMEOUT_SECONDS = float(
    os.environ.get("NEUROBOT_RELOGIN_TIMEOUT", str(int(relogin.DEFAULT_SESSION_TIMEOUT)))
)
# Optional OAuth login_hint, purely a convenience so the person lands on the
# right account chooser. Omitted when unset.
RELOGIN_EMAIL = os.environ.get("NEUROBOT_CLAUDE_LOGIN_EMAIL", "").strip()
RELOGIN_NOT_LOGGED_IN_MESSAGE = (
    "⚠️ Не авторизован — отправьте /relogin, чтобы подключить свой аккаунт Claude."
)
RELOGIN_EXPIRED_MESSAGE = (
    "⚠️ Срок авторизации истёк — отправьте /relogin, чтобы переподключить свой "
    "аккаунт Claude."
)


@contextmanager
def state_lock() -> Iterator[None]:
    bot.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def current_mode(state: Dict[str, Any]) -> str:
    mode = str(state.get(CHANNEL_MODE_KEY) or "auto").lower()
    return mode if mode in VALID_MODES else "auto"


def direct_reply(text: str) -> Dict[str, Any]:
    return {"action": "reply", "text": text}


def drop() -> Dict[str, Any]:
    return {"action": "drop"}


def ensure_codex_session(state: Dict[str, Any]) -> None:
    session_key = "{}:{}".format(bot.TARGET_CHAT_ID, bot.TARGET_THREAD_ID)
    current = state.setdefault("sessions", {}).get(session_key)
    if not isinstance(current, str) or not current:
        state["sessions"][session_key] = bot.CODEX_DEFAULT_THREAD_ID
        bot.save_state(state)


# A tenant whose only backend is the persistent Claude session (NEUROBOT_EXECUTOR
# =claude, no Codex socket, no DeepSeek key) must not advertise /backend, /new or
# Codex/DeepSeek limits: those commands would be dead ends. Tenants configured
# with hybrid/codex/deepseek keep the full command set exactly as before.
def multi_backend_tenant() -> bool:
    return bot.EXECUTOR != "claude"


def format_limits() -> str:
    if not multi_backend_tenant():
        return "Claude: подписка не предоставляет точный pre-flight процент."
    try:
        status = bot.read_codex_rate_limit_status()
        codex = bot.format_rate_limit_status(status, not status["allowed"])
    except Exception as exc:
        codex = "⚠️ Codex: лимит недоступен ({})".format(str(exc)[:300])
    if bot.deepseek_configured():
        deepseek = "🟢 DeepSeek ({}): настроен, pay-as-you-go.".format(bot.DEEPSEEK_MODEL)
    else:
        deepseek = "⚪ DeepSeek: {}".format(bot.DEEPSEEK_NOT_CONFIGURED_MESSAGE)
    return (
        "Claude: подписка не предоставляет точный pre-flight процент.\n"
        + codex + "\n" + deepseek
    )


BACKEND_DETAIL_LABELS = {
    "auto": "Claude (постоянная сессия)",
    "claude": "Claude (постоянная сессия)",
    "codex": "Codex",
}


def backend_switch_detail(requested: str) -> str:
    if requested == "deepseek":
        return "DeepSeek ({})".format(bot.DEEPSEEK_MODEL)
    return BACKEND_DETAIL_LABELS.get(requested, requested)


def command_text(original: str) -> str:
    """Text with this bot's @mention removed, for command detection.

    The channel policy is requireMention: true, so a command usually arrives as
    "@TenantBot /relogin" — and bot.command_name() reads the FIRST token, which
    would be the mention. Stripping it first makes every command in this module
    work when mentioned as well as when sent as a reply to the bot (an implicit
    mention) or as "/cmd@TenantBot". A non-command message that merely starts
    with a slash still resolves to an unknown command and falls through.
    """
    return original.replace("@" + bot.BOT_USERNAME, "").strip()


def command_reply(
    original: str,
    state: Dict[str, Any],
    message: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    command = bot.command_name(command_text(original))
    if command == RELOGIN_COMMAND:
        if message is None:
            return direct_reply(
                "Команду /relogin можно выполнить только от вашего Telegram-аккаунта."
            )
        return start_relogin(message, state)
    if command in {"/backend", "/new"} and not multi_backend_tenant():
        return direct_reply(
            "У этого сотрудника один движок — Claude в постоянной сессии. "
            "Переключать нечего."
        )
    if command == "/backend":
        parts = original.split(maxsplit=1)
        requested = parts[1].strip().lower() if len(parts) > 1 else ""
        if requested not in VALID_MODES:
            return direct_reply("Использование: /backend auto|claude|codex|deepseek")
        if requested == "deepseek" and not bot.deepseek_configured():
            # Explicit, clear failure — never silently fall through to
            # another backend and never switch into a mode that would then
            # error on the next message.
            return direct_reply(
                "{} Заполните DEEPSEEK_API_KEY в {} и повторите /backend deepseek "
                "— рестарт сервиса не требуется.".format(
                    bot.DEEPSEEK_NOT_CONFIGURED_MESSAGE, bot.DEEPSEEK_ENV_FILE
                )
            )
        state[CHANNEL_MODE_KEY] = requested
        bot.save_state(state)
        return direct_reply(
            "Режим разработчика переключён: {} — {}.".format(
                requested, backend_switch_detail(requested)
            )
        )
    if command == "/status":
        mode = current_mode(state)
        last = state.get(CHANNEL_LAST_BACKEND_KEY) or "ещё не запускался"
        text = (
            "{} работает. Постоянная Claude Remote Control-сессия активна. "
            "Режим: {}; последний движок: {}.".format(
                bot.AGENT_DISPLAY_NAME, mode, last
            )
        )
        usage_summary = bot.summarize_backend_usage()
        if usage_summary:
            text = "{}\n{}".format(text, usage_summary)
        # Same advisory as the message path, so /status never claims everything
        # is fine while the session is actually unable to answer anything.
        login_warning = claude_login_warning()
        if login_warning:
            text = "{}\n{}".format(text, login_warning)
        return direct_reply(text)
    if command == "/limits":
        return direct_reply(format_limits())
    if command in {"/start", "/help"}:
        lines = ["Я — {}.".format(bot.AGENT_DISPLAY_NAME), ""]
        if multi_backend_tenant():
            lines.append("/backend auto|claude|codex|deepseek — выбрать движок")
        lines.append("/status — состояние постоянной сессии")
        lines.append("/limits — лимиты подписок")
        lines.append("/relogin — переавторизовать мой Claude-аккаунт по ссылке")
        if multi_backend_tenant():
            lines.append("/new — новый Codex/DeepSeek-контекст в соответствующем режиме")
        return direct_reply("\n".join(lines))
    if command == "/new":
        mode = current_mode(state)
        if mode not in {"codex", "deepseek"}:
            return direct_reply(
                "Claude работает в постоянной общей сессии. Новый контекст можно открыть "
                "из Remote Control; для нового Codex- или DeepSeek-потока сначала выберите "
                "/backend codex или /backend deepseek."
            )
        session_key = "{}:{}".format(bot.TARGET_CHAT_ID, bot.TARGET_THREAD_ID)
        if mode == "codex":
            bot.EXECUTOR = "codex"
            thread_id = bot.create_codex_session()
            state.setdefault("sessions", {})[session_key] = thread_id
            bot.save_state(state)
            return direct_reply("Новый Codex-контекст создан. Что нужно сделать?")
        if not bot.deepseek_configured():
            return direct_reply(bot.DEEPSEEK_NOT_CONFIGURED_MESSAGE)
        bot.EXECUTOR = "deepseek"
        thread_id = bot.create_deepseek_session()
        state.setdefault("sessions", {})[session_key + DEEPSEEK_SESSION_KEY_SUFFIX] = thread_id
        bot.save_state(state)
        return direct_reply("Новый DeepSeek-контекст создан. Что нужно сделать?")
    return None


def validate_human(message: Dict[str, Any], state: Dict[str, Any]) -> bool:
    sender = message.get("from") or {}
    return bot.human_sender_allowed(
        int(sender.get("id", 0)),
        str(sender.get("username") or ""),
        state,
    )


def claude_credentials_path() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(configured) if configured else Path(relogin.home_directory()) / ".claude"
    return base / ".credentials.json"


def claude_login_warning() -> Optional[str]:
    """Cheap "is this tenant logged in at all?" probe, or None when it looks fine.

    One small file read plus a json.loads per message — deliberately NOT
    `claude auth status`, which would spawn the CLI on every single message.

    Only the REFRESH token's expiry is treated as a problem. ``expiresAt`` is the
    access token and sits roughly four hours out; the CLI refreshes it silently
    in the background, so alarming on it would cry wolf several times a day.
    ``refreshTokenExpiresAt`` is ~28 days out and is the point where nobody can
    refresh anything any more and a human genuinely has to log in again.

    Anything unexpected (unreadable file, malformed JSON, missing/odd expiry)
    returns None: this is an advisory nicety, and it must never be the reason a
    message stops being delivered.
    """
    try:
        raw = claude_credentials_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        # Never logged in on this box, or logged out.
        return RELOGIN_NOT_LOGGED_IN_MESSAGE
    except OSError:
        return None
    try:
        oauth = (json.loads(raw) or {}).get("claudeAiOauth") or {}
    except ValueError:
        return None
    if not isinstance(oauth, dict) or not oauth.get("refreshToken"):
        return RELOGIN_NOT_LOGGED_IN_MESSAGE
    try:
        refresh_expires = float(oauth.get("refreshTokenExpiresAt") or 0) / 1000.0
    except (TypeError, ValueError):
        return None
    if refresh_expires and refresh_expires <= time.time():
        return RELOGIN_EXPIRED_MESSAGE
    return None


def relogin_sender_allowed(message: Dict[str, Any], state: Dict[str, Any]) -> bool:
    """Stricter than validate_human, on purpose.

    validate_human is satisfied by TELEGRAM_ALLOW_ALL_CHAT_MEMBERS, which both
    live tenants set — fine for chatting with the agent, far too wide for a
    command that starts an OAuth login against the tenant's own account. Require
    the sender to also be named explicitly in TELEGRAM_ALLOWED_USER_IDS, i.e. one
    of the specific, already-known humans this box was provisioned for. Reuses
    the same allowlist the rest of the bot enforces; adds no parallel notion of
    identity.
    """
    sender = message.get("from") or {}
    if not validate_human(message, state):
        return False
    return int(sender.get("id", 0)) in bot.ALLOWED_USER_IDS


def pending_relogin(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pending = state.get(CHANNEL_PENDING_RELOGIN_KEY)
    return pending if isinstance(pending, dict) and pending.get("user_id") else None


def clear_pending_relogin(state: Dict[str, Any], terminate: bool = True) -> None:
    pending = state.pop(CHANNEL_PENDING_RELOGIN_KEY, None)
    if terminate and isinstance(pending, dict):
        relogin.terminate(
            str(pending.get("socket") or RELOGIN_SOCKET), int(pending.get("pid") or 0)
        )
    bot.save_state(state)


def relogin_expired(pending: Dict[str, Any]) -> bool:
    try:
        return time.time() >= float(pending.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True


def start_relogin(message: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    if not relogin_sender_allowed(message, state):
        return direct_reply(
            "Переавторизация доступна только разработчикам из списка "
            "TELEGRAM_ALLOWED_USER_IDS этого сотрудника."
        )
    sender = message.get("from") or {}
    # Any earlier attempt — finished, abandoned or wedged — dies here, so a
    # stale pty can never interfere with this one.
    previous = pending_relogin(state) or {}
    relogin.terminate(str(RELOGIN_SOCKET), int(previous.get("pid") or 0))
    state.pop(CHANNEL_PENDING_RELOGIN_KEY, None)

    bot.STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        pid = relogin.start_supervisor(
            socket_path=str(RELOGIN_SOCKET),
            claude_bin=bot.CLAUDE_BIN,
            email=RELOGIN_EMAIL,
            session_timeout=RELOGIN_TIMEOUT_SECONDS,
            log_path=str(RELOGIN_LOG),
        )
    except OSError as exc:
        return direct_reply("Не удалось запустить вход: {}".format(str(exc)[:300]))

    status = relogin.wait_for_url(str(RELOGIN_SOCKET))
    url = status.get("url")
    if not url:
        relogin.terminate(str(RELOGIN_SOCKET), pid)
        return direct_reply(
            "Не удалось получить ссылку для входа: {}".format(
                str(status.get("detail") or status.get("state") or "нет ответа")[:300]
            )
        )

    minutes = max(1, int(RELOGIN_TIMEOUT_SECONDS // 60))
    state[CHANNEL_PENDING_RELOGIN_KEY] = {
        "user_id": int(sender.get("id", 0)),
        "socket": str(RELOGIN_SOCKET),
        "pid": pid,
        "started_at": time.time(),
        "expires_at": time.time() + RELOGIN_TIMEOUT_SECONDS,
    }
    bot.save_state(state)
    return direct_reply(
        "Откройте ссылку, войдите под своим аккаунтом Claude и пришлите мне "
        "следующим сообщением код, который покажет страница Anthropic "
        "(вида code#state).\n\n{}\n\nЖду код {} мин. "
        "Отмена — /cancel, начать заново — /relogin.".format(url, minutes)
    )


def submit_relogin_code(
    pending: Dict[str, Any],
    code: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    socket_path = str(pending.get("socket") or RELOGIN_SOCKET)
    try:
        result = relogin.submit_code(socket_path, code)
    except (OSError, ValueError) as exc:
        clear_pending_relogin(state)
        return direct_reply(
            "Сессия входа больше недоступна ({}). Запустите /relogin заново.".format(
                str(exc)[:200]
            )
        )
    finally:
        # The code itself is single-use and short-lived: it lives in the pty's
        # stdin and this local variable, and is never persisted anywhere.
        code = ""
    detail = str(result.get("detail") or "")
    # Whether it worked or not, the attempt is over: the pty is gone and the
    # code is burned, so never leave the sender stuck in "awaiting code".
    clear_pending_relogin(state)
    if result.get("ok"):
        text = (
            "Готово — вход выполнен. Постоянная сессия подхватит новые учётные "
            "данные без перезапуска."
        )
        # /relogin always repairs Claude, whatever backend the topic is switched
        # to. Someone who ran it from a non-Claude mode would otherwise see their
        # next test messages still answered by Codex/DeepSeek and conclude the
        # login had failed. Only say so — never switch the mode for them.
        #
        # Note this checks for the modes that actually divert away from Claude,
        # not simply "mode != claude": "auto" already routes to the persistent
        # Claude session (see handle_inbound), so pointing an auto-mode user at
        # /backend claude would be telling them to fix something that is not
        # broken.
        mode = current_mode(state)
        if multi_backend_tenant() and mode in {"codex", "deepseek"}:
            text += (
                "\n\nТекущий режим — {}. Если хотите проверить/использовать "
                "Claude, отправьте /backend claude.".format(mode)
            )
        return direct_reply(text)
    return direct_reply(
        "Вход не удался: {}\nПришлите /relogin, чтобы получить свежую ссылку — "
        "код одноразовый и быстро истекает.".format(detail or "неизвестная ошибка")
    )


def relogin_pending_reply(
    message: Dict[str, Any],
    original: str,
    state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Intercept the message that follows a /relogin, before normal routing.

    Returns None whenever this sender has no live relogin in flight, so ordinary
    traffic keeps its existing path untouched.
    """
    pending = pending_relogin(state)
    if not pending:
        return None
    sender = message.get("from") or {}
    if int(sender.get("id", 0)) != int(pending.get("user_id") or 0):
        # Someone else's message in the same topic is none of this flow's
        # business.
        return None
    if relogin_expired(pending):
        # Silently give up rather than swallowing a message the person sent
        # minutes later with something else in mind. They can rerun /relogin.
        clear_pending_relogin(state)
        return None

    text = command_text(original)
    command = bot.command_name(text)
    if command == RELOGIN_COMMAND:
        return start_relogin(message, state)
    if command == RELOGIN_CANCEL_COMMAND:
        clear_pending_relogin(state)
        return direct_reply("Переавторизация отменена.")
    if "#" not in text:
        # Anthropic's callback page always shows "<code>#<state>". Anything else
        # is far more likely a normal message than a code, so do not burn the
        # attempt on it — say what is expected and keep waiting.
        return direct_reply(
            "Жду код входа вида code#state со страницы Anthropic. "
            "/cancel — отменить, /relogin — новая ссылка."
        )
    return submit_relogin_code(pending, text.split()[0], state)


def remember_pending_route(
    state: Dict[str, Any],
    message: Dict[str, Any],
    routed: Dict[str, Any],
) -> None:
    message_id = str(message.get("message_id") or "")
    if not message_id:
        return
    pending = state.setdefault(CHANNEL_PENDING_ROUTES_KEY, {})
    pending[message_id] = routed
    state[CHANNEL_LATEST_ROUTE_KEY] = message_id
    if len(pending) > 100:
        for old_id in list(pending)[:-80]:
            pending.pop(old_id, None)
    bot.save_state(state)


def handle_inbound(payload: Dict[str, Any]) -> Dict[str, Any]:
    message = payload.get("message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if int(chat.get("id", 0)) != bot.TARGET_CHAT_ID:
        return drop()
    if int(message.get("message_thread_id", 0)) != bot.TARGET_THREAD_ID:
        return drop()

    original = str(message.get("text") or message.get("caption") or "").strip()
    if not original:
        return drop()

    state = bot.load_state()
    if not sender.get("is_bot"):
        if not validate_human(message, state):
            return drop()
        # Checked before command routing and before any backend: while a
        # /relogin is in flight this sender's next message is the OAuth code,
        # not a prompt. Returns None (and changes nothing) when nothing is
        # pending, which is the normal case.
        relogin_response = relogin_pending_reply(message, original, state)
        if relogin_response is not None:
            return relogin_response
        response = command_reply(original, state, message)
        if response is not None:
            return response

    mode = current_mode(state)
    if mode == "codex":
        bot.EXECUTOR = "codex"
        ensure_codex_session(state)
        state[CHANNEL_LAST_BACKEND_KEY] = "codex"
        bot.save_state(state)
        bot.log_backend_usage("codex", bot.backend_model_label("codex"), message)
        # Reuse the mature Codex path for validation, limits, attachments,
        # placeholder editing, peer routing and result delivery.
        bot.handle_message(message, state)
        return {"action": "handled", "backend": "codex"}
    if mode == "deepseek":
        if not bot.deepseek_configured():
            # Defense in depth: /backend deepseek already refuses to switch
            # into this mode without a key (see command_reply), but if the
            # key file is emptied out again after the fact, fail loudly and
            # revert instead of erroring on every subsequent message.
            state[CHANNEL_MODE_KEY] = "auto"
            bot.save_state(state)
            return direct_reply(
                "{} Режим переключён обратно на auto. Заполните {} и снова "
                "выберите /backend deepseek.".format(
                    bot.DEEPSEEK_NOT_CONFIGURED_MESSAGE, bot.DEEPSEEK_ENV_FILE
                )
            )
        bot.EXECUTOR = "deepseek"
        state[CHANNEL_LAST_BACKEND_KEY] = "deepseek"
        bot.save_state(state)
        bot.log_backend_usage("deepseek", bot.backend_model_label("deepseek"), message)
        bot.handle_message(
            message, state, session_key_suffix=DEEPSEEK_SESSION_KEY_SUFFIX
        )
        return {"action": "handled", "backend": "deepseek"}

    # Everything below this point goes to the persistent Claude session. If that
    # session has no usable login, delivery silently vanishes: the message is
    # handed to the live session via mcp.notification and simply never answered,
    # with no acknowledgement and no visible error. Say so instead.
    #
    # Placement matters. This sits AFTER the pending-code interception and
    # command_reply — /relogin exists precisely for the logged-out state and must
    # keep working here — and AFTER the codex/deepseek branches above, which
    # return earlier and have their own separate authentication. It also sits
    # BEFORE any routing bookkeeping, so bailing out cannot leave a half-recorded
    # pending route behind.
    login_warning = claude_login_warning()
    if login_warning:
        return direct_reply(login_warning)

    routed: Optional[Dict[str, Any]] = None
    if sender.get("is_bot"):
        routed = bot.accept_routed_message(message, original, state)
        if not routed:
            return drop()
        prompt = bot.build_routed_prompt(routed)
        remember_pending_route(state, message, routed)
    else:
        prompt = bot.clean_prompt(original)
    if not prompt:
        return direct_reply("Напишите задачу после упоминания бота.")

    state[CHANNEL_LAST_BACKEND_KEY] = "claude"
    bot.save_state(state)
    bot.log_backend_usage("claude", bot.describe_current_claude_model(), message)
    return {
        "action": "claude",
        "backend": "claude",
        "prompt": prompt,
        "meta": {
            "message_thread_id": str(message.get("message_thread_id") or ""),
            "routed": bool(routed),
        },
    }


def pending_route_for_reply(
    state: Dict[str, Any],
    reply_to: Optional[str],
) -> Optional[Dict[str, Any]]:
    pending = state.setdefault(CHANNEL_PENDING_ROUTES_KEY, {})
    key = str(reply_to or state.get(CHANNEL_LATEST_ROUTE_KEY) or "")
    routed = pending.pop(key, None) if key else None
    if key and state.get(CHANNEL_LATEST_ROUTE_KEY) == key:
        state.pop(CHANNEL_LATEST_ROUTE_KEY, None)
    return routed


def handle_outbound(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = str(payload.get("text") or "")
    reply_to = str(payload.get("reply_to") or "") or None
    state = bot.load_state()
    visible, routes, human_messages = bot.extract_outputs(text)
    if human_messages:
        raise RuntimeError("Developer channel does not allow telegram_human blocks")

    routed_ids = []
    for kind, target, body in routes:
        if kind == "task":
            routed_ids.append(bot.dispatch_task(target, body))
        elif kind == "message":
            routed_ids.append(bot.dispatch_message(target, body))

    inbound_route = pending_route_for_reply(state, reply_to)
    if inbound_route:
        result_body = visible or "Задача обработана; дальнейшее действие передано сотруднику."
        bot.return_routed_response(inbound_route, result_body)
    bot.save_state(state)

    if not visible and routed_ids:
        visible = "Задача передана сотруднику."
    # Every text this function returns is what the persistent Claude session
    # itself just sent via its `reply` tool (see server.ts: the `reply` tool
    # calls this gateway for every outbound Telegram message, ack included).
    # Labeling here — not per-message-type — covers the ack and the final
    # answer in one place. describe_current_claude_model() is read fresh
    # from the session transcript on every call, so it tracks a mid-session
    # /effort change on the very next message.
    #
    # Known gap: interim edit_message progress updates do not go through
    # this gateway and are therefore not labeled.
    if visible and bot.SHOW_MODEL_LABEL:
        # Single newline, not a blank-line paragraph break — see
        # bot.with_model_label for the matching Codex/DeepSeek spacing.
        visible = "{}\n{}".format(
            bot.to_superscript(bot.describe_current_claude_model()), visible
        )
    return {
        "action": "send",
        "text": visible,
        "routed_count": len(routed_ids),
        "returned_route": bool(inbound_route),
    }


def handle(payload: Dict[str, Any]) -> Dict[str, Any]:
    operation = str(payload.get("operation") or "")
    bot.require_config()
    bot.initialize_peers()
    with state_lock():
        if operation == "inbound":
            return handle_inbound(payload)
        if operation == "outbound":
            return handle_outbound(payload)
    raise RuntimeError("Unknown channel gateway operation: {}".format(operation))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        result = handle(payload)
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    except Exception as exc:
        print("channel gateway error: {}".format(exc), file=sys.stderr)
        json.dump(
            {"action": "error", "error": str(exc)[:1000]},
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
