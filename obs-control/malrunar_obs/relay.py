# SPDX-License-Identifier: Apache-2.0
"""UDP/MPEG-TS relay with an optional capture, run as ``python3 -m malrunar_obs.relay``.

Forwards the datagrams OBS sends (``[relay] port``) to a target (``[relay] target_host`` and
``target_port``, normally the media-bridge container) and, while a capture is active, also writes
them to a file. A small HTTP control API (``[relay] control_host`` and ``control_port``) starts and
stops captures; obs-control uses it for manual recordings and automatic b-roll. The control API has
no authentication: keep it on a private network (the default is 127.0.0.1).
"""
import json
import os
import re
import shutil
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import load_config

SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,100}$")
TARGET = ("127.0.0.1", 18096)
CAPTURE_DIR = Path("/tmp/malrunar-obs-captures")

state_lock = threading.Lock()


class Recording:
    """Disk work never holds the forwarding lock; one bounded writer per capture."""

    def __init__(self, capture_id, path, max_bytes=2 * 1024 * 1024):
        self.capture_id, self.path = capture_id, path
        self.started_at = time.time()
        self.max_bytes = max_bytes
        self.pending = deque()
        self.queued_bytes = self.bytes = 0
        self.stopping = False
        self.error = None
        self.condition = threading.Condition()
        self.done = threading.Event()
        threading.Thread(target=self._write, daemon=True).start()

    def _fail(self, message):
        with self.condition:
            self.error = self.error or message
            self.stopping = True
            self.pending.clear()
            self.queued_bytes = 0
            self.condition.notify_all()

    def offer(self, payload):
        with self.condition:
            if self.stopping:
                return
            if self.queued_bytes + len(payload) > self.max_bytes:
                self._fail("Recording queue full; recording stopped, live media continues")
                return
            self.pending.append(payload)
            self.queued_bytes += len(payload)
            self.condition.notify()

    def _write(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("xb") as handle:
                while True:
                    with self.condition:
                        self.condition.wait_for(lambda: self.pending or self.stopping)
                        if not self.pending:
                            break
                        payload = self.pending.popleft()
                        self.queued_bytes -= len(payload)
                    handle.write(payload)
                    with self.condition:
                        self.bytes += len(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception as exc:
            self._fail(f"Recording failed: {exc}")
        finally:
            self.done.set()

    def finish(self, timeout=5):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        if not self.done.wait(timeout):
            self._fail("Recording storage did not finish in time; live media continues")
        return self.snapshot()

    def snapshot(self):
        with self.condition:
            result = {
                "recording": not self.stopping,
                "capture_id": self.capture_id,
                "started_at": self.started_at,
                "bytes": self.bytes,
                "queued_bytes": self.queued_bytes,
                "duration": max(0, time.time() - self.started_at),
            }
            if self.error:
                result["error"] = self.error
            return result


active_capture: Recording | None = None
completed_captures: dict[str, Path] = {}


def state_payload() -> dict:
    with state_lock:
        if not active_capture:
            return {"recording": False}
        return active_capture.snapshot()


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "MalrunarObsRelay/1"

    def log_message(self, *_: object) -> None:
        return

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self.send_json(200, {"healthy": True})
            return
        if path == "/recording/status":
            self.send_json(200, state_payload())
            return

        match = re.fullmatch(r"/recording/file/([a-zA-Z0-9_-]+)", path)
        if match:
            capture_id = match.group(1)
            with state_lock:
                capture_path = completed_captures.get(capture_id)
            if not capture_path or not capture_path.is_file():
                self.send_json(404, {"error": "Capture not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(capture_path.stat().st_size))
            self.end_headers()
            with capture_path.open("rb") as handle:
                shutil.copyfileobj(handle, self.wfile, length=1024 * 1024)
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        global active_capture
        path = urlparse(self.path).path
        if path == "/recording/start":
            length = min(int(self.headers.get("Content-Length", "0")), 4096)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self.send_json(400, {"error": "Invalid JSON"})
                return
            capture_id = str(data.get("capture_id", ""))
            if not SAFE_ID.fullmatch(capture_id):
                self.send_json(400, {"error": "Invalid capture ID"})
                return
            with state_lock:
                if active_capture and not (active_capture.done.is_set() and active_capture.error):
                    self.send_json(409, {"error": "A recording is already active"})
                    return
                capture_path = CAPTURE_DIR / f"{capture_id}.ts"
                active_capture = Recording(capture_id, capture_path)
            print(f"Recording started: {capture_id}", flush=True)
            self.send_json(201, state_payload())
            return

        if path == "/recording/stop":
            with state_lock:
                if not active_capture:
                    self.send_json(409, {"error": "No recording is active"})
                    return
                capture = active_capture
            result = capture.finish()
            if result.get("error"):
                self.send_json(503, result)
                return
            with state_lock:
                if active_capture is capture:
                    active_capture = None
                completed_captures[capture.capture_id] = capture.path
            print(f"Recording stopped: {capture.capture_id}", flush=True)
            self.send_json(200, result)
            return

        self.send_json(404, {"error": "Not found"})

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        match = re.fullmatch(r"/recording/file/([a-zA-Z0-9_-]+)", path)
        if not match:
            self.send_json(404, {"error": "Not found"})
            return
        capture_id = match.group(1)
        with state_lock:
            capture_path = completed_captures.pop(capture_id, None)
        if capture_path and capture_path.is_file():
            capture_path.unlink()
        self.send_json(200, {"ok": True})


def forward(payload, outgoing):
    outgoing.sendto(payload, TARGET)
    with state_lock:
        capture = active_capture
    if capture:
        capture.offer(payload)


def main():
    global TARGET, CAPTURE_DIR
    config = load_config(require_cameras=False)
    relay = config.relay
    if not relay.target_host:
        raise SystemExit("obs-control relay: set [relay] target_host (MALRUNAR_OBS_RELAY_TARGET_HOST), the receiver of the stream")
    bind = (relay.bind_host, relay.port)
    control = (relay.control_host or relay.bind_host, relay.control_port)
    TARGET = (relay.target_host, relay.target_port)
    CAPTURE_DIR = Path(relay.capture_dir)
    control_server = ThreadingHTTPServer(control, ControlHandler)
    threading.Thread(target=control_server.serve_forever, daemon=True).start()
    incoming = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    incoming.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    incoming.bind(bind)
    outgoing = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    outgoing.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    print(f"Relaying udp://{bind[0]}:{bind[1]} to udp://{TARGET[0]}:{TARGET[1]}; control on http://{control[0]}:{control[1]}", flush=True)
    while True:
        payload, _ = incoming.recvfrom(65535)
        forward(payload, outgoing)


if __name__ == "__main__":
    main()
