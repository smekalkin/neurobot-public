#!/usr/bin/env python3
"""Headless Claude Code executor.

Codex talks to a local daemon over a Unix socket (see appserver_client.py).
Claude Code has no equivalent socket/App Server protocol — the closest
building block is the CLI itself in non-interactive mode (`claude -p
--output-format json`), which returns a single JSON result and a
`session_id` that a later call can resume with `--resume`. This client
wraps that CLI the same way CodexAppServerClient wraps the socket, so
bot.py can treat either backend through one interface (see EXECUTORS in
bot.py).

No account-level rate-limit introspection is available for Claude Code the
way Codex App Server exposes `read_account_rate_limits` — the CLI has no
`usage`/limits subcommand. `read_account_rate_limits()` here is therefore a
documented no-op (`allowed: True` always) rather than a real check. If the
underlying Claude subscription runs out mid-task, that surfaces as a normal
CLI/API error from `run_turn`, not as a pre-emptive block like Codex gets.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ClaudeExecutorError(RuntimeError):
    """Raised when the Claude Code CLI fails or returns an unusable result."""


class ClaudeExecutorClient:
    def __init__(
        self,
        workspace: Path,
        timeout: int = 900,
        claude_bin: str = "claude",
        extra_args: Optional[List[str]] = None,
        env_overrides: Optional[Dict[str, str]] = None,
    ) -> None:
        self.workspace = workspace
        self.timeout = timeout
        self.claude_bin = claude_bin
        self.extra_args = extra_args or []
        # Lets a caller point this same CLI binary at a different
        # Anthropic-compatible backend (e.g. DeepSeek's ANTHROPIC_BASE_URL)
        # for one client's turns without touching the process-wide
        # environment other executors/sessions rely on. None (the default)
        # preserves the previous behavior exactly: inherit os.environ as-is.
        self.env_overrides = env_overrides

    def __enter__(self) -> "ClaudeExecutorClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        return None

    def start_thread(self, cwd: Path, name: Optional[str] = None) -> str:
        """Mirrors CodexAppServerClient.start_thread(): returns a fresh session id.

        Claude Code has no "create an empty session" call — a session only
        exists once a turn has run in it. We spend one cheap turn to mint the
        id, matching the synchronous "give me a session id now" contract that
        bot.py's create_*_session() callers expect (e.g. the /new command).
        """
        session_id = str(uuid.uuid4())
        note = "New conversation started."
        if name:
            note += " ({})".format(name)
        self._run(note, session_id=session_id, resume=False, cwd=cwd)
        return session_id

    def run_turn(self, thread_id: str, prompt: str) -> str:
        answer, _ = self._run(prompt, session_id=thread_id, resume=True, cwd=self.workspace)
        return answer

    def run_turn_new_session(self, prompt: str, cwd: Optional[Path] = None) -> Tuple[str, str]:
        """Like run_turn, but for when the caller has no existing session id yet."""
        session_id = str(uuid.uuid4())
        return self._run(prompt, session_id=session_id, resume=False, cwd=cwd or self.workspace)

    def read_account_rate_limits(self) -> Dict[str, Any]:
        return {
            "unavailable": True,
            "planType": "n/a (claude)",
            "details": (
                "Claude Code CLI does not expose account usage/rate-limit data — "
                "this check is a no-op for the Claude executor."
            ),
        }

    def _run(
        self,
        prompt: str,
        *,
        session_id: str,
        resume: bool,
        cwd: Path,
    ) -> Tuple[str, str]:
        args = [self.claude_bin, "-p", "--output-format", "json"]
        args += ["--resume", session_id] if resume else ["--session-id", session_id]
        # User-scope settings.json enables the Telegram channel plugin
        # (enabledPlugins.telegram) for the persistent --remote-control
        # session. That scope applies to every `claude` invocation for this
        # OS user, including this headless one-shot -p call — so without
        # excluding it, every DeepSeek/backend turn spawns a second bun
        # server.ts poller competing for the same bot token as the primary
        # channel, which can silently prevent the primary channel from polling.
        # Headless one-shot calls never need channels of their own.
        args += ["--setting-sources", "project,local"]
        args += self.extra_args
        env = None
        if self.env_overrides:
            env = dict(os.environ)
            env.update(self.env_overrides)
        try:
            proc = subprocess.run(
                args,
                input=prompt,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClaudeExecutorError(
                "claude CLI timed out after {}s".format(self.timeout)
            ) from exc
        except OSError as exc:
            raise ClaudeExecutorError("Could not launch claude CLI: {}".format(exc)) from exc

        # Even on a non-zero exit, --output-format json still writes a
        # structured payload to stdout describing what actually went wrong
        # (e.g. an upstream API error like "402 Insufficient Balance" from a
        # non-Anthropic backend such as DeepSeek). That is far more useful
        # than proc.stderr, which can contain only an unrelated warning —
        # e.g. Claude Code always prints a "claude.ai connectors are
        # disabled" notice on stderr whenever ANTHROPIC_AUTH_TOKEN overrides
        # the default auth (exactly what env_overrides does for DeepSeek),
        # regardless of whether the actual request succeeded or failed. A
        # a failure can be misreported if this method raises on returncode
        # before looking at the structured stdout payload.
        try:
            payload: Optional[Dict[str, Any]] = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = None

        if proc.returncode != 0:
            if isinstance(payload, dict) and (payload.get("result") or payload.get("is_error")):
                detail = payload.get("result") or json.dumps(payload)
                raise ClaudeExecutorError(
                    "claude exited with code {}: {}".format(
                        proc.returncode, str(detail)[:2000]
                    )
                )
            raise ClaudeExecutorError(
                "claude exited with code {}: {}".format(
                    proc.returncode, proc.stderr.strip()[:2000]
                )
            )
        if payload is None:
            raise ClaudeExecutorError(
                "Could not parse claude JSON output: {}".format(proc.stdout[:500])
            )
        if payload.get("is_error"):
            raise ClaudeExecutorError(
                "Claude Code reported an error: {}".format(json.dumps(payload)[:1000])
            )
        answer = payload.get("result")
        returned_session_id = payload.get("session_id") or session_id
        if not answer:
            raise ClaudeExecutorError(
                "Claude Code returned an empty result: {}".format(json.dumps(payload)[:500])
            )
        return answer, returned_session_id
