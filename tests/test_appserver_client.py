"""CodexAppServerClient against a fake App Server: a WebSocket server on a Unix
socket that speaks just enough JSON-RPC. Covers the handshake, framing
(masking, fragments, ping, close), requests/errors, and a whole Codex turn.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from neurobot.appserver_client import AppServerError, CodexAppServerClient  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def recv_exact(conn, n):
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def read_frame(conn):
    first, second = recv_exact(conn, 2)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", recv_exact(conn, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", recv_exact(conn, 8))[0]
    mask = recv_exact(conn, 4) if second & 0x80 else b""
    payload = recv_exact(conn, length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload, bool(second & 0x80)


def frame(opcode, payload, final=True):
    head = bytearray([(0x80 if final else 0) | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n <= 0xFFFF:
        head.append(126)
        head += struct.pack("!H", n)
    else:
        head.append(127)
        head += struct.pack("!Q", n)
    return bytes(head) + payload


class FakeServer:
    """handler(method, params, send) -> result dict, or raises ('error', code, msg)."""

    def __init__(self, handler, bad_accept=False, refuse=False, oversize_header=False):
        self.dir = tempfile.mkdtemp(prefix="appsrv-")
        self.path = Path(self.dir) / "s.sock"
        self.handler = handler
        self.bad_accept = bad_accept
        self.refuse = refuse
        self.oversize_header = oversize_header
        self.received = []
        self.unmasked_frames = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        self.sock.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            try:
                self.session(conn)
            except (EOFError, OSError):
                pass

    def session(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(1)
        key = [l for l in data.decode().split("\r\n") if l.lower().startswith("sec-websocket-key")][0].split(":", 1)[1].strip()
        if self.refuse:
            conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            return
        if self.oversize_header:
            conn.sendall(b"HTTP/1.1 101 Switching\r\nX: " + b"a" * 70000)
            return
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if self.bad_accept:
            accept = "wrong"
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())

        def send(msg, fragment=False, opcode=1):
            raw = msg if isinstance(msg, bytes) else json.dumps(msg).encode()
            if fragment:
                half = len(raw) // 2
                conn.sendall(frame(1, raw[:half], final=False))
                conn.sendall(frame(9, b"ping"))          # control frames may interleave
                conn.sendall(frame(0, raw[half:], final=True))
            else:
                conn.sendall(frame(opcode, raw))

        while True:
            opcode, payload, masked = read_frame(conn)
            if not masked:
                self.unmasked_frames += 1
            if opcode == 8:
                return
            if opcode != 1:
                continue
            msg = json.loads(payload)
            self.received.append(msg)
            if "id" in msg and "method" in msg:
                try:
                    result = self.handler(msg["method"], msg.get("params"), send)
                    send({"id": msg["id"], "result": result})
                except _Err as e:
                    send({"id": msg["id"], "error": {"code": e.code, "message": e.msg}})

    def close(self):
        self.sock.close()


class _Err(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg


def ok_handler(method, params, send):
    return {"echo": method}


class ClientTest(unittest.TestCase):
    def serve(self, handler=ok_handler, **kw):
        s = FakeServer(handler, **kw)
        self.addCleanup(s.close)
        return s

    def test_connects_initializes_and_sends_only_masked_frames(self):
        s = self.serve()
        with CodexAppServerClient(s.path, timeout=5) as c:
            self.assertEqual(c.request("ping/pong", None), {"echo": "ping/pong"})
        methods = [m.get("method") for m in s.received]
        self.assertEqual(methods[:2], ["initialize", "initialized"])
        self.assertEqual(s.unmasked_frames, 0, "RFC 6455: client frames must be masked")
        init = s.received[0]["params"]["clientInfo"]["name"]
        self.assertEqual(init, "neurobot_telegram_bridge")

    def test_a_server_that_refuses_or_fakes_the_handshake_is_rejected(self):
        for kw, text in (({"refuse": True}, "refused"), ({"bad_accept": True}, "invalid WebSocket handshake"), ({"oversize_header": True}, "Oversized")):
            s = self.serve(**kw)
            c = CodexAppServerClient(s.path, timeout=2)
            with self.assertRaises(AppServerError) as cm:
                c.connect()
            self.assertIn(text, str(cm.exception))
            self.assertIsNone(c.sock, "a failed connect must not leave a socket behind")

    def test_a_missing_socket_is_an_error(self):
        c = CodexAppServerClient(Path(tempfile.mkdtemp()) / "none.sock", timeout=1)
        with self.assertRaises(OSError):
            c.connect()
        with self.assertRaises(AppServerError):
            c.request("x", None)

    def test_an_error_reply_becomes_an_exception(self):
        def handler(method, params, send):
            if method == "boom":
                raise _Err(-32000, "nope")
            return {}

        s = self.serve(handler)
        with CodexAppServerClient(s.path, timeout=5) as c:
            with self.assertRaises(AppServerError) as cm:
                c.request("boom", None)
        self.assertIn("-32000: nope", str(cm.exception))

    def test_notifications_that_arrive_first_are_kept_and_server_requests_are_declined(self):
        def handler(method, params, send):
            if method == "work":
                send({"method": "note/progress", "params": {"n": 1}})
                send({"id": 99, "method": "approval/ask", "params": {}})
            return {"done": True}

        s = self.serve(handler)
        with CodexAppServerClient(s.path, timeout=5) as c:
            self.assertEqual(c.request("work", {}), {"done": True})
            self.assertEqual(c.pending[0]["method"], "note/progress")
        import time
        time.sleep(0.2)  # the decline is written just before the answer is read
        declined = [m for m in s.received if m.get("id") == 99]
        self.assertEqual(declined[0]["error"]["code"], -32601)

    def test_fragmented_messages_with_a_ping_in_between_are_reassembled(self):
        def handler(method, params, send):
            send({"method": "note/long", "params": {"text": "x" * 300}}, fragment=True)
            return {}

        s = self.serve(handler)
        with CodexAppServerClient(s.path, timeout=5) as c:
            c.request("go", None)
            self.assertEqual(len(c.pending[0]["params"]["text"]), 300)

    def test_large_payloads_use_the_extended_length_encodings(self):
        def handler(method, params, send):
            return {"len": len((params or {}).get("blob", ""))}

        s = self.serve(handler)
        with CodexAppServerClient(s.path, timeout=5) as c:
            self.assertEqual(c.request("big", {"blob": "y" * 200})["len"], 200)        # 16-bit length
            self.assertEqual(c.request("huge", {"blob": "z" * 70000})["len"], 70000)   # 64-bit length

    def test_garbage_and_non_object_json_are_rejected(self):
        for payload in (b"not json", b"[1, 2]"):
            def handler(method, params, send, payload=payload):
                if method == "bad":
                    send(payload)
                return {}

            s = self.serve(handler)
            with CodexAppServerClient(s.path, timeout=5) as c:
                with self.assertRaises(AppServerError):
                    c.request("bad", None)

    def test_a_close_frame_or_a_dropped_connection_ends_the_request(self):
        def handler(method, params, send):
            if method == "x":
                send(b"", opcode=8)
            return {}

        s = self.serve(handler)
        c = CodexAppServerClient(s.path, timeout=5)
        c.connect()
        with self.assertRaises(AppServerError):
            c.request("x", None)
        c.close()

    def test_close_is_safe_twice_and_after_the_server_went_away(self):
        s = self.serve()
        c = CodexAppServerClient(s.path, timeout=5)
        c.connect()
        c.connect()  # already connected: no-op
        s.close()
        c.close()
        c.close()


class ThreadsAndTurnsTest(unittest.TestCase):
    def serve(self, handler):
        s = FakeServer(handler)
        self.addCleanup(s.close)
        return s

    def test_read_thread_rate_limits_and_start_thread(self):
        seen = {}

        def handler(method, params, send):
            seen[method] = params
            if method == "thread/start":
                return {"thread": {"id": "t1"}}
            return {"ok": True}

        s = self.serve(handler)
        with CodexAppServerClient(s.path, timeout=5) as c:
            c.read_thread("t1", include_turns=True)
            c.read_account_rate_limits()
            self.assertEqual(c.start_thread(Path("/srv/x"), name="Анна"), "t1")
        self.assertEqual(seen["thread/read"], {"threadId": "t1", "includeTurns": True})
        self.assertEqual(seen["thread/start"]["approvalPolicy"], "never")
        self.assertEqual(seen["thread/name/set"], {"threadId": "t1", "name": "Анна"})

    def test_a_thread_without_an_id_is_an_error(self):
        s = self.serve(lambda m, p, send: {"thread": {}})
        with CodexAppServerClient(s.path, timeout=5) as c:
            with self.assertRaises(AppServerError):
                c.start_thread(Path("/x"))

    def turn_handler(self, events, turn_id="turn1"):
        def handler(method, params, send):
            if method == "turn/start":
                for e in events:
                    send(e)
                return {"turn": {"id": turn_id}} if turn_id else {}
            return {}
        return handler

    def completed(self, status="completed", items=None, error=None, turn="turn1", thread="t1"):
        t = {"id": turn, "status": status, "items": items or []}
        if error:
            t["error"] = error
        return {"method": "turn/completed", "params": {"threadId": thread, "turn": t}}

    def agent(self, text, turn="turn1", thread="t1"):
        return {"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": {"type": "agentMessage", "text": text}}}

    def test_a_turn_returns_the_last_agent_message(self):
        events = [
            self.agent("первый"),
            {"method": "item/completed", "params": {"threadId": "other", "turnId": "turn1", "item": {"type": "agentMessage", "text": "чужой"}}},
            self.agent("  итог  "),
            self.completed(),
        ]
        s = self.serve(self.turn_handler(events))
        with CodexAppServerClient(s.path, timeout=5) as c:
            self.assertEqual(c.run_turn("t1", "привет"), "итог")
        sent = [m for m in s.received if m.get("method") == "turn/start"][0]["params"]
        self.assertEqual(sent["input"][0]["text"], "привет")
        self.assertIn("thread/resume", [m.get("method") for m in s.received])

    def test_the_answer_may_come_only_with_the_completed_turn(self):
        events = [self.completed(items=[{"type": "agentMessage", "text": "в конце"}])]
        s = self.serve(self.turn_handler(events))
        with CodexAppServerClient(s.path, timeout=5) as c:
            self.assertEqual(c.run_turn("t1", "x"), "в конце")

    def test_a_turn_that_fails_or_answers_nothing_is_an_error(self):
        cases = [
            ([self.completed(status="failed", error={"message": "лимит"})], "лимит"),
            ([self.completed(status="interrupted")], "interrupted"),
            ([self.completed()], "without a final answer"),
        ]
        for events, text in cases:
            s = self.serve(self.turn_handler(events))
            with CodexAppServerClient(s.path, timeout=5) as c:
                with self.assertRaises(AppServerError) as cm:
                    c.run_turn("t1", "x")
            self.assertIn(text, str(cm.exception))

    def test_a_turn_without_an_id_is_an_error(self):
        s = self.serve(self.turn_handler([], turn_id=None))
        with CodexAppServerClient(s.path, timeout=5) as c:
            with self.assertRaises(AppServerError):
                c.run_turn("t1", "x")

    def test_a_completion_of_another_turn_is_ignored(self):
        events = [self.completed(turn="old"), self.agent("ответ"), self.completed()]
        s = self.serve(self.turn_handler(events))
        with CodexAppServerClient(s.path, timeout=5) as c:
            self.assertEqual(c.run_turn("t1", "x"), "ответ")

    def test_a_turn_that_never_finishes_times_out(self):
        s = self.serve(self.turn_handler([]))
        with CodexAppServerClient(s.path, timeout=1) as c:
            with self.assertRaises(AppServerError) as cm:
                c.run_turn("t1", "x")
        self.assertIn("exceeded", str(cm.exception))

    def test_notifications_buffered_during_requests_are_used_by_the_turn(self):
        # the completion arrives while "turn/start" is still being answered
        events = [self.agent("из буфера"), self.completed()]
        s = self.serve(self.turn_handler(events))
        with CodexAppServerClient(s.path, timeout=5) as c:
            self.assertEqual(c.run_turn("t1", "x"), "из буфера")
            self.assertEqual(len(c.pending), 0)


if __name__ == "__main__":
    unittest.main()
