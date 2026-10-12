"""AgentCore HTTP contract with bounded concurrency and SSE heartbeats.

Deploy behind AgentCore's IAM authentication, never as an unauthenticated public API.
No customer request body or exception detail is logged.
"""

import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tracaus.execution import execute, safe_chat

SLOTS = threading.BoundedSemaphore(2)
STATE_LOCK = threading.Lock()
ACTIVE = 0
MAX_REQUEST_BYTES = 96000


def events(payload, executor=execute):
    global ACTIVE
    mailbox = queue.Queue(maxsize=1)

    def perform():
        global ACTIVE
        try:
            mailbox.put(
                {
                    "event": "result",
                    "result": safe_chat(executor, payload)
                    if os.getenv("EXECUTION_PURPOSE") == "chat"
                    else executor(payload),
                }
            )
        except Exception:
            mailbox.put({"event": "error", "code": "EXECUTION_FAILED"})
        finally:
            with STATE_LOCK:
                ACTIVE -= 1
            SLOTS.release()

    if not SLOTS.acquire(blocking=False):
        raise RuntimeError("Host capacity exhausted")
    with STATE_LOCK:
        ACTIVE += 1
    try:
        threading.Thread(target=perform, daemon=True).start()
    except Exception:
        with STATE_LOCK:
            ACTIVE -= 1
        SLOTS.release()
        raise
    while True:
        try:
            event = mailbox.get(timeout=5)
        except queue.Empty:
            yield b": heartbeat\n\n"
            continue
        yield b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n"
        return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != "/ping":
            self.send_error(404)
            return
        with STATE_LOCK:
            body = json.dumps({"status": "HealthyBusy" if ACTIVE else "Healthy"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.connection.settimeout(10)
        if self.path != "/invocations":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (
                not 0 < length <= MAX_REQUEST_BYTES
                or self.headers.get("Transfer-Encoding")
                or self.headers.get_content_type() != "application/json"
            ):
                raise ValueError("Invalid request")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Invalid request")
        except Exception:
            self.send_error(400, "Invalid request")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for event in events(payload):
                self.wfile.write(f"{len(event):x}\r\n".encode() + event + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            self.close_connection = True  # The durable fence/deadline still govern the host task.


def main():
    if os.getenv("MODEL_API"):
        raise SystemExit("MODEL_API is set, but the AgentCore target is Bedrock-only; unset MODEL_API")
    bind = "0.0.0.0" if os.getenv("AGENTCORE_HOSTING") == "true" else "127.0.0.1"
    ThreadingHTTPServer((bind, 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
