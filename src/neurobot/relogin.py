#!/usr/bin/env python3
"""Supervise one ``claude auth login`` pty session for the Telegram /relogin flow.

Why this is a separate, detached process
----------------------------------------
``channel_gateway.py`` is invoked once per inbound Telegram update and exits
immediately (see callNeurobotGateway in the patched telegram plugin). The OAuth
login it drives spans *two* Telegram messages — the gateway must print an
authorize URL now and feed a pasted code into the very same still-running CLI
several minutes later. A pty opened by the gateway process would die with it, so
the pty is owned by this small supervisor, spawned detached (``setsid``) and
reachable over a per-tenant ``AF_UNIX`` socket for the rest of its bounded
lifetime.

``claude auth login --claudeai`` on a headless box selects Anthropic's hosted callback page
(``https://platform.claude.com/oauth/code/callback``) instead of a loopback
redirect, prints the authorize URL, and then parks on a ``Paste code here if
prompted >`` prompt waiting for the ``<authorization_code>#<state>`` string that
Anthropic's page shows the human. The prompt needs a real tty, hence pty.fork()
rather than subprocess pipes.

Deliberately stdlib-only and free of any ``bot`` import: this file is copied
beside ``channel_gateway.py`` into the agent's service directory and is re-executed
standalone by ``sys.executable``, so it must not depend on agent configuration
beyond what the gateway passes on argv.

SECURITY: the pasted authorization code is a short-lived single-use credential.
It is written to the pty and held in memory only. It is never written to the log
file, never returned to the caller, and is redacted out of any CLI output that is
surfaced back to Telegram (the tty can echo what was typed). The log below
records lifecycle events only — never the pty buffer.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
from typing import Any, Dict, Optional


# Overall lifetime of one login attempt. The person has to open a link on
# another device, sign in and copy a code back, so this is minutes, not
# seconds — but it is always bounded: an abandoned attempt must never leave a
# pty (or the CLI behind it) running indefinitely.
DEFAULT_SESSION_TIMEOUT = 600.0
# How long the gateway waits for the authorize URL to appear before giving up.
# Observed live: ~2-4s.
DEFAULT_URL_WAIT = 40.0
# How long the supervisor waits for the token exchange after the code is fed in.
# Observed live: ~1-2s for both success and the HTTP 400 failure.
DEFAULT_CODE_WAIT = 90.0
CONNECT_TIMEOUT = 15.0
MAX_BUFFER = 262144

ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]"
)
# Matches both the claude.ai (subscription) and Console authorize endpoints
# without pinning either host, so a future endpoint change does not silently
# break URL capture.
AUTHORIZE_RE = re.compile(r"https://\S*/oauth/authorize\?\S*")
PASTE_PROMPT = "Paste code here"
SUCCESS_MARKER = "Login successful"
FAILURE_MARKER = "Login failed:"
INTERRUPT_MARKER = "Login interrupted"

STATE_STARTING = "starting"
STATE_AWAITING_CODE = "awaiting_code"
STATE_FINISHED = "finished"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def extract_authorize_url(clean: str) -> Optional[str]:
    """Return the authorize URL only once it has been written in full.

    A naive search finds the URL mid-write and silently truncates it: the live
    PoC captured everything up to ``code_challenge_method=S256`` and lost the
    trailing ``&state=...``, which would have made the link unusable. Only accept
    a match that is followed by whitespace already present in the buffer, which
    proves the CLI finished writing the line.
    """
    best: Optional[str] = None
    for match in AUTHORIZE_RE.finditer(clean):
        end = match.end()
        if end >= len(clean) or not clean[end].isspace():
            continue
        candidate = match.group(0)
        if best is None or len(candidate) > len(best):
            best = candidate
    return best


def redact(text: str, secret: str) -> str:
    secret = (secret or "").strip()
    if not secret:
        return text
    for chunk in sorted({secret, secret.split("#", 1)[0]}, key=len, reverse=True):
        if len(chunk) >= 4:
            text = text.replace(chunk, "<redacted>")
    return text


def summarize(clean: str, secret: str = "") -> str:
    """Reduce raw CLI output to the one line worth showing a human.

    Each interesting line is cut at its marker, which also drops the
    ``Paste code here if prompted > `` prefix the failure message shares a line
    with — and with it anything the tty may have echoed of the pasted code.
    """
    picked = []
    for line in clean.split("\n"):
        line = line.strip()
        for marker in (SUCCESS_MARKER, FAILURE_MARKER, INTERRUPT_MARKER):
            index = line.find(marker)
            if index >= 0:
                picked.append(line[index:])
                break
    if picked:
        text = " ".join(picked[-2:])
    else:
        lines = [line.strip() for line in clean.split("\n") if line.strip()]
        text = lines[-1] if lines else ""
    return redact(text, secret)[:300]


def home_directory() -> str:
    home = os.environ.get("HOME", "").strip()
    if home and os.path.isdir(home):
        return home
    import pwd

    return pwd.getpwuid(os.getuid()).pw_dir


class Supervisor:
    def __init__(
        self,
        socket_path: str,
        claude_bin: str,
        email: str = "",
        session_timeout: float = DEFAULT_SESSION_TIMEOUT,
        log_path: str = "",
    ) -> None:
        self.socket_path = socket_path
        self.claude_bin = claude_bin
        self.email = (email or "").strip()
        self.session_timeout = session_timeout
        self.log_path = log_path
        self.home = home_directory()
        self.state = STATE_STARTING
        self.url: Optional[str] = None
        self.ok: Optional[bool] = None
        self.detail = ""
        self.buffer = ""
        self.eof = False
        self.stop = False
        self.child_pid = -1
        self.child_status: Optional[int] = None
        self.master_fd = -1
        self.server: Optional[socket.socket] = None

    # ---- lifecycle logging: events only, never pty output, never the code ----
    def log(self, message: str) -> None:
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(
                    "{} pid={} {}\n".format(
                        time.strftime("%Y-%m-%dT%H:%M:%S%z"), os.getpid(), message
                    )
                )
        except OSError:
            pass

    # ---------------------------------------------------------------- socket
    def bind(self) -> None:
        try:
            os.unlink(self.socket_path)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous_umask = os.umask(0o177)
        try:
            self.server.bind(self.socket_path)
        finally:
            os.umask(previous_umask)
        os.chmod(self.socket_path, 0o600)
        self.server.listen(4)

    # ------------------------------------------------------------------- pty
    def spawn(self) -> None:
        argv = [self.claude_bin, "auth", "login", "--claudeai"]
        if self.email:
            # Maps to the OAuth login_hint parameter; purely a convenience for
            # the human, it does not restrict which account may approve.
            argv += ["--email", self.email]
        env = dict(os.environ)
        env["HOME"] = self.home
        env["TERM"] = "xterm-256color"
        # No browser can be opened here, and an attempt to open one is what
        # makes the CLI pick a loopback redirect_uri. Clearing these keeps it on
        # the hosted-callback "paste the code" path this whole flow depends on.
        env.pop("DISPLAY", None)
        env.pop("BROWSER", None)
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(self.home)
                os.execvpe(argv[0], argv, env)
            except BaseException:
                pass
            os._exit(127)
        self.child_pid = pid
        self.master_fd = fd
        # A narrow tty would be free to wrap the (very long) authorize URL.
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 400, 0, 0))
        except OSError:
            pass
        self.log("spawned claude auth login pid={}".format(pid))

    def child_exited(self) -> bool:
        if self.child_status is not None:
            return True
        if self.child_pid <= 0:
            return False
        try:
            waited, status = os.waitpid(self.child_pid, os.WNOHANG)
        except ChildProcessError:
            self.child_status = -1
            return True
        if waited == self.child_pid:
            self.child_status = status
            return True
        return False

    def drain(self, timeout: float) -> None:
        if self.master_fd < 0 or self.eof:
            return
        try:
            ready, _, _ = select.select([self.master_fd], [], [], timeout)
        except (OSError, ValueError):
            self.eof = True
            return
        if not ready:
            return
        try:
            chunk = os.read(self.master_fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            # The pty master reports EIO/EOF once the CLI is gone.
            self.eof = True
            return
        self.buffer += chunk.decode("utf-8", "replace")
        if len(self.buffer) > MAX_BUFFER:
            self.buffer = self.buffer[-MAX_BUFFER:]
        self.refresh()

    def refresh(self) -> None:
        clean = strip_ansi(self.buffer)
        if self.url is None:
            self.url = extract_authorize_url(clean)
        if self.state == STATE_STARTING and self.url and PASTE_PROMPT in clean:
            self.state = STATE_AWAITING_CODE

    def finish(self, ok: bool, detail: str) -> None:
        self.state = STATE_FINISHED
        self.ok = ok
        self.detail = detail
        self.log("finished ok={} detail={}".format(ok, detail[:200]))

    # -------------------------------------------------------------- requests
    def status_payload(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "url": self.url,
            "ok": self.ok,
            "detail": self.detail,
        }

    def handle_code(self, code: str) -> Dict[str, Any]:
        code = code.strip()
        if self.state == STATE_FINISHED:
            return {"ok": bool(self.ok), "state": self.state, "detail": self.detail}
        if self.state != STATE_AWAITING_CODE:
            return {
                "ok": False,
                "state": self.state,
                "detail": "login prompt is not ready yet",
            }
        if not code:
            return {"ok": False, "state": self.state, "detail": "empty code"}
        try:
            os.write(self.master_fd, (code + "\r").encode("utf-8"))
        except OSError as exc:
            self.finish(False, "cannot reach the login prompt: {}".format(exc))
            return {"ok": False, "state": self.state, "detail": self.detail}
        self.log("code submitted")
        deadline = time.monotonic() + DEFAULT_CODE_WAIT
        while time.monotonic() < deadline:
            self.drain(0.5)
            clean = strip_ansi(self.buffer)
            if SUCCESS_MARKER in clean:
                # A successful login parks on "Press <enter> to continue"; send
                # it so the CLI exits on its own instead of being killed.
                try:
                    os.write(self.master_fd, b"\r")
                except OSError:
                    pass
                self.finish(True, summarize(clean, code))
                break
            if FAILURE_MARKER in clean or INTERRUPT_MARKER in clean:
                self.finish(False, summarize(clean, code))
                break
            if self.eof or self.child_exited():
                self.drain(0)
                clean = strip_ansi(self.buffer)
                ok = SUCCESS_MARKER in clean
                self.finish(ok, summarize(clean, code) or "login process exited")
                break
        else:
            self.finish(False, "timed out waiting for claude auth login to answer")
        return {"ok": bool(self.ok), "state": self.state, "detail": self.detail}

    def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        operation = str(request.get("op") or "")
        if operation == "status":
            return self.status_payload()
        if operation == "code":
            return self.handle_code(str(request.get("code") or ""))
        if operation == "abort":
            self.stop = True
            self.log("aborted by client")
            return {"ok": True, "state": "aborted"}
        return {"error": "unknown operation: {}".format(operation)}

    def accept_once(self) -> None:
        assert self.server is not None
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        conn.settimeout(CONNECT_TIMEOUT)
        try:
            raw = b""
            while b"\n" not in raw and len(raw) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                raw += chunk
            if not raw.strip():
                return
            response = self.handle_request(json.loads(raw.decode("utf-8")))
            conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
        except (OSError, ValueError) as exc:
            self.log("request error: {}".format(exc))
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # --------------------------------------------------------------- cleanup
    def cleanup(self) -> None:
        # The buffer can contain whatever the tty echoed at the paste prompt.
        self.buffer = ""
        if self.child_pid > 0 and not self.child_exited():
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    # pty.fork() puts the child in its own session, so this
                    # reaps the CLI and anything it spawned.
                    os.killpg(os.getpgid(self.child_pid), sig)
                except OSError:
                    break
                for _ in range(20):
                    if self.child_exited():
                        break
                    time.sleep(0.05)
                if self.child_exited():
                    break
        if self.master_fd >= 0:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = -1
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        self.log("cleaned up")

    def serve(self) -> int:
        self.bind()
        try:
            self.spawn()
            deadline = time.monotonic() + self.session_timeout
            while not self.stop and time.monotonic() < deadline:
                if self.state == STATE_FINISHED:
                    break
                watched = [self.server]
                if self.master_fd >= 0 and not self.eof:
                    watched.append(self.master_fd)
                try:
                    ready, _, _ = select.select(watched, [], [], 0.5)
                except OSError:
                    break
                if self.master_fd in ready:
                    self.drain(0)
                if self.server in ready:
                    self.accept_once()
                if self.state != STATE_AWAITING_CODE and self.eof and self.child_exited():
                    # The CLI died before it ever asked for a code.
                    self.finish(
                        False,
                        summarize(strip_ansi(self.buffer))
                        or "claude auth login exited before showing a login link",
                    )
            if self.state != STATE_FINISHED and not self.stop:
                self.log("session timed out after {}s".format(self.session_timeout))
        finally:
            self.cleanup()
        return 0


# ---------------------------------------------------------------------------
# Client side — imported by channel_gateway.py.
# ---------------------------------------------------------------------------
def request(socket_path: str, payload: Dict[str, Any], timeout: float = CONNECT_TIMEOUT) -> Dict[str, Any]:
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(socket_path)
        conn.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        raw = b""
        while b"\n" not in raw and len(raw) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            raw += chunk
    finally:
        try:
            conn.close()
        except OSError:
            pass
    if not raw.strip():
        raise OSError("empty response from relogin supervisor")
    return json.loads(raw.decode("utf-8"))


def start_supervisor(
    socket_path: str,
    claude_bin: str,
    email: str = "",
    session_timeout: float = DEFAULT_SESSION_TIMEOUT,
    log_path: str = "",
) -> int:
    argv = [
        sys.executable,
        os.path.abspath(__file__),
        "--serve",
        "--socket",
        str(socket_path),
        "--claude-bin",
        str(claude_bin),
        "--timeout",
        str(session_timeout),
    ]
    if email:
        argv += ["--email", email]
    if log_path:
        argv += ["--log", str(log_path)]
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Detach: this must outlive the one-shot gateway process that starts it.
        start_new_session=True,
        cwd=home_directory(),
    )
    return process.pid


def wait_for_url(socket_path: str, budget: float = DEFAULT_URL_WAIT) -> Dict[str, Any]:
    """Poll the supervisor until it has captured a complete authorize URL."""
    deadline = time.monotonic() + budget
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = request(socket_path, {"op": "status"}, timeout=5.0)
        except (OSError, ValueError):
            time.sleep(0.4)
            continue
        if last.get("url"):
            return last
        if last.get("state") == STATE_FINISHED:
            return last
        time.sleep(0.4)
    return last or {"state": "unavailable", "detail": "login helper did not start"}


def submit_code(socket_path: str, code: str, timeout: float = DEFAULT_CODE_WAIT + 15.0) -> Dict[str, Any]:
    return request(socket_path, {"op": "code", "code": code}, timeout=timeout)


def terminate(socket_path: str, pid: int = 0) -> None:
    """Best-effort teardown of any previous attempt.

    Belt and braces: ask politely over the socket, fall back to signalling the
    recorded pid, and always remove a stale socket file — a wedged supervisor
    from an abandoned attempt must never block a later legitimate one.
    """
    try:
        request(socket_path, {"op": "abort"}, timeout=3.0)
    except (OSError, ValueError):
        pass
    if pid and pid > 1:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            try:
                os.kill(pid, sig)
            except OSError:
                break
            time.sleep(0.3)
    try:
        os.unlink(socket_path)
    except OSError:
        pass


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--email", default="")
    parser.add_argument("--timeout", type=float, default=DEFAULT_SESSION_TIMEOUT)
    parser.add_argument("--log", default="")
    args = parser.parse_args(argv)
    supervisor = Supervisor(
        socket_path=args.socket,
        claude_bin=args.claude_bin,
        email=args.email,
        session_timeout=args.timeout,
        log_path=args.log,
    )
    return supervisor.serve()


if __name__ == "__main__":
    raise SystemExit(main())
