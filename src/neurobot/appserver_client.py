#!/usr/bin/env python3
"""Small dependency-free client for the local Codex App Server.

The managed Codex daemon exposes JSON-RPC over a WebSocket carried by a
private Unix socket.  Keeping Telegram turns on that daemon makes them part of
the same thread that Codex Desktop sees through Remote Control.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional


class AppServerError(RuntimeError):
    """Raised when the local Codex App Server rejects or loses a request."""


class CodexAppServerClient:
    def __init__(self, socket_path: Path, timeout: int = 900) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.next_request_id = 1
        self.pending: Deque[Dict[str, Any]] = deque()

    def __enter__(self) -> "CodexAppServerClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def connect(self) -> None:
        if self.sock is not None:
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(min(self.timeout, 30))
        try:
            sock.connect(str(self.socket_path))
            self.sock = sock
            self._websocket_upgrade()
            self._initialize()
        except Exception:
            sock.close()
            self.sock = None
            raise

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()
        self.sock = None

    def _websocket_upgrade(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /rpc HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Key: {}\r\n\r\n"
        ).format(key)
        self._send_raw(request.encode("ascii"))
        response = self._read_http_headers()
        status_line = response.split("\r\n", 1)[0]
        if " 101 " not in status_line:
            raise AppServerError("Codex App Server refused WebSocket upgrade: " + status_line)
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        headers: Dict[str, str] = {}
        for line in response.split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        if headers.get("sec-websocket-accept") != expected:
            raise AppServerError("Codex App Server returned an invalid WebSocket handshake")

    def _read_http_headers(self) -> str:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            if len(data) > 65536:
                raise AppServerError("Oversized WebSocket handshake")
            data.extend(self._recv_exact(1))
        return bytes(data).decode("iso-8859-1")

    def _send_raw(self, data: bytes) -> None:
        if self.sock is None:
            raise AppServerError("Codex App Server socket is not connected")
        self.sock.sendall(data)

    def _recv_exact(self, length: int) -> bytes:
        if self.sock is None:
            raise AppServerError("Codex App Server socket is not connected")
        chunks = bytearray()
        while len(chunks) < length:
            chunk = self.sock.recv(length - len(chunks))
            if not chunk:
                raise AppServerError("Codex App Server closed the connection")
            chunks.extend(chunk)
        return bytes(chunks)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        # RFC 6455 requires every client frame to be masked.
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._send_raw(bytes(header) + masked)

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._recv_exact(2)
        final = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return final, opcode, payload

    def _recv_json(self) -> Dict[str, Any]:
        fragments = bytearray()
        text_started = False
        while True:
            final, opcode, payload = self._recv_frame()
            if opcode == 0x8:
                raise AppServerError("Codex App Server closed the WebSocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments = bytearray(payload)
                text_started = True
            elif opcode == 0x0 and text_started:
                fragments.extend(payload)
            else:
                continue
            if final:
                try:
                    message = json.loads(bytes(fragments).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AppServerError("Invalid JSON from Codex App Server") from exc
                if not isinstance(message, dict):
                    raise AppServerError("Unexpected Codex App Server message")
                return message

    def _send_json(self, message: Dict[str, Any]) -> None:
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_frame(0x1, encoded)

    def _handle_server_request(self, message: Dict[str, Any]) -> bool:
        if "id" not in message or "method" not in message:
            return False
        self._send_json(
            {
                "id": message["id"],
                "error": {
                    "code": -32601,
                    "message": "Neurobot cannot handle interactive server requests",
                },
            }
        )
        return True

    def request(self, method: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        request_id = self.next_request_id
        self.next_request_id += 1
        message: Dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send_json(message)
        while True:
            incoming = self._recv_json()
            if incoming.get("id") == request_id and "method" not in incoming:
                if incoming.get("error"):
                    error = incoming["error"]
                    raise AppServerError(
                        "{}: {}".format(error.get("code", "error"), error.get("message", "request failed"))
                    )
                result = incoming.get("result")
                return result if isinstance(result, dict) else {}
            if self._handle_server_request(incoming):
                continue
            self.pending.append(incoming)

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "neurobot_telegram_bridge",
                    "title": "Neurobot Telegram Bridge",
                    "version": "1.0.0",
                },
                "capabilities": {
                    "experimentalApi": False,
                    "requestAttestation": False,
                    "optOutNotificationMethods": [],
                    "mcpServerOpenaiFormElicitation": False,
                },
            },
        )
        self._send_json({"method": "initialized"})

    def read_thread(
        self, thread_id: str, include_turns: bool = False
    ) -> Dict[str, Any]:
        return self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
        )

    def read_account_rate_limits(self) -> Dict[str, Any]:
        """Return the current ChatGPT/Codex subscription rate-limit snapshot."""
        return self.request("account/rateLimits/read", None)

    def start_thread(self, cwd: Path, name: Optional[str] = None) -> str:
        result = self.request(
            "thread/start",
            {
                "cwd": str(cwd),
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
            },
        )
        thread = result.get("thread") or {}
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise AppServerError("Codex App Server did not return a thread id")
        if name:
            self.request("thread/name/set", {"threadId": thread_id, "name": name})
        return thread_id

    def run_turn(self, thread_id: str, prompt: str) -> str:
        turn_id = self.start_turn(thread_id, prompt)

        answers = []
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerError("Codex turn exceeded {} seconds".format(self.timeout))
            if self.sock is not None:
                self.sock.settimeout(min(remaining, 30))
            try:
                message = self.pending.popleft() if self.pending else self._recv_json()
            except socket.timeout:
                continue
            if self._handle_server_request(message):
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if params.get("threadId") != thread_id:
                continue
            if method == "item/completed" and params.get("turnId") == turn_id:
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    answers.append(str(item["text"]).strip())
            if method == "turn/completed":
                completed_turn = params.get("turn") or {}
                if completed_turn.get("id") != turn_id:
                    continue
                if not answers:
                    for item in completed_turn.get("items") or []:
                        if item.get("type") == "agentMessage" and item.get("text"):
                            answers.append(str(item["text"]).strip())
                status = completed_turn.get("status")
                if status != "completed":
                    error = completed_turn.get("error") or {}
                    detail = error.get("message") or status or "unknown error"
                    raise AppServerError("Codex turn failed: {}".format(detail))
                if not answers:
                    raise AppServerError("Codex App Server completed without a final answer")
                return answers[-1]

    def start_turn(self, thread_id: str, prompt: str) -> str:
        """Submit a turn and return immediately after the App Server accepts it.

        Scheduled jobs use this non-blocking half of run_turn: the App Server
        owns the turn after turn/start, so one long Codex job must not stop the
        scheduler from dispatching other agents' jobs.
        """
        self.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "approvalPolicy": "never",
            },
        )
        result = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "approvalPolicy": "never",
            },
        )
        turn = result.get("turn") or {}
        turn_id = str(turn.get("id") or "")
        if not turn_id:
            raise AppServerError("Codex App Server did not start a turn")
        return turn_id
