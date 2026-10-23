#!/usr/bin/env python3
"""Bounded document-to-PDF converter (LibreOffice headless), standard library only.

POST /convert with the document as the body and its file name in the X-Filename header; the
response is the PDF. GET /health answers "ok". Only the file name's extension is used, never the
name itself. One conversion runs at a time; other requests wait their turn, and /health is always
answered. LibreOffice runs only for the duration of a request, in its own process group, and the
whole group is killed on timeout. Nothing is kept between requests.

The service has no authentication of its own: keep it on a trusted network or behind an
authenticating proxy. The user a proxy names in a request header (forward_auth.py) is only logged; no
request is refused because of it, and nothing about the user is kept.

Settings (environment, all optional):
  MALRUNAR_OFFICE_PORT              listen port, default 8090
  MALRUNAR_OFFICE_BIND              listen address, default 127.0.0.1 (use 0.0.0.0 in a container)
  MALRUNAR_OFFICE_TIMEOUT_SECONDS   per-conversion limit, default 180
  MALRUNAR_OFFICE_MAX_BYTES         largest accepted document, default 209715200 (200 MiB)
  MALRUNAR_OFFICE_USER_HEADER, _GROUPS_HEADER, _NAME_HEADER, _EMAIL_HEADER, _TRUSTED_PROXIES,
  _PROXY_SECRET_HEADER, _PROXY_SECRET_FILE   identity from a proxy, see forward_auth.py and README.md

`convert.py --healthcheck` probes the local /health and exits 0 or 1 (used by the image HEALTHCHECK).
"""
import errno
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python3 -I does not add the script directory
import forward_auth  # noqa: E402

ALLOWED = frozenset({".pptx", ".ppt", ".odp", ".key", ".docx", ".doc", ".odt"})
SOFFICE = "soffice"          # tests replace this with a stub
COPY_BLOCK = 1024 * 1024
LINE_LIMIT = 4096            # longest chunk-size or trailer line accepted
TRAILER_LIMIT = 64           # most trailer lines accepted after the last chunk
_HEX = re.compile(rb"^[0-9a-fA-F]{1,16}$")


class ConfigError(ValueError):
    pass


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Config:
    port: int = 8090
    bind: str = "127.0.0.1"
    timeout: int = 180
    max_bytes: int = 200 * 1024 * 1024
    identity: forward_auth.IdentityConfig = field(default_factory=forward_auth.IdentityConfig, compare=False, repr=False)


def _integer(environ, name, default, low, high):
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip(), 10)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return value


def load_config(environ=None):
    environ = os.environ if environ is None else environ
    bind = (environ.get("MALRUNAR_OFFICE_BIND") or "127.0.0.1").strip()
    try:
        identity = forward_auth.IdentityConfig.from_env(environ, "MALRUNAR_OFFICE_")
    except forward_auth.ConfigError as error:
        raise ConfigError(str(error)) from None
    return Config(
        identity=identity,
        port=_integer(environ, "MALRUNAR_OFFICE_PORT", 8090, 1, 65535),
        bind=bind,
        timeout=_integer(environ, "MALRUNAR_OFFICE_TIMEOUT_SECONDS", 180, 1, 86400),
        max_bytes=_integer(environ, "MALRUNAR_OFFICE_MAX_BYTES", 200 * 1024 * 1024, 1, 1 << 40),
    )


def _copy_exact(rfile, handle, count):
    """Copy exactly count bytes from rfile to handle; a short read is a bad request."""
    while count:
        block = rfile.read(min(count, COPY_BLOCK))
        if not block:
            raise HttpError(400, "body ended before the announced length")
        handle.write(block)
        count -= len(block)


def _line(rfile):
    line = rfile.readline(LINE_LIMIT + 1)
    if len(line) > LINE_LIMIT:
        raise HttpError(400, "chunk header too long")
    return line


def read_body(rfile, headers, handle, max_bytes):
    """Write the request body to handle and return its size.

    Raises HttpError: 400 for a malformed length or chunk, 411 without any length, 413 when the
    body is larger than max_bytes (decided from the announced size before reading it).
    """
    encoding = (headers.get("Transfer-Encoding") or "").strip().lower()
    if encoding:
        if encoding != "chunked":
            raise HttpError(400, "unsupported Transfer-Encoding")
        total = 0
        while True:
            line = _line(rfile)
            if not line.endswith(b"\n"):
                raise HttpError(400, "malformed chunk header")
            size_text = line.split(b";", 1)[0].strip()
            if not _HEX.match(size_text):
                raise HttpError(400, "malformed chunk size")
            size = int(size_text, 16)
            if size == 0:
                for _ in range(TRAILER_LIMIT):
                    if _line(rfile).strip() == b"":
                        return total
                raise HttpError(400, "too many trailer lines")
            total += size
            if total > max_bytes:
                raise HttpError(413, "document too large")
            _copy_exact(rfile, handle, size)
            if _line(rfile).strip() != b"":
                raise HttpError(400, "chunk is not followed by CRLF")
    raw = headers.get("Content-Length")
    if raw is None:
        raise HttpError(411, "Content-Length or chunked Transfer-Encoding required")
    raw = raw.strip()
    if not raw.isascii() or not raw.isdigit():
        raise HttpError(400, "malformed Content-Length")
    length = int(raw)
    if length == 0:
        raise HttpError(400, "empty document")
    if length > max_bytes:
        raise HttpError(413, "document too large")
    _copy_exact(rfile, handle, length)
    return length


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError as error:
        if error.errno != errno.ESRCH:
            raise


def run_soffice(source, workdir, timeout):
    """Convert source to workdir/out/<stem>.pdf. Returns the PDF path, or None if none was made.

    Raises subprocess.TimeoutExpired after killing LibreOffice's whole process group.
    """
    outdir = workdir / "out"
    command = [
        SOFFICE, "--headless", "--norestore", "--nolockcheck", "--nodefault",
        f"-env:UserInstallation={(workdir / 'profile').as_uri()}",
        "--convert-to", "pdf", "--outdir", str(outdir), str(source),
    ]
    process = subprocess.Popen(
        command, env={**os.environ, "HOME": str(workdir)},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        process.wait(timeout=timeout)
    except BaseException:
        _kill_group(process)
        process.wait()
        raise
    finally:
        # soffice launches soffice.bin; make sure nothing of the group outlives the request.
        _kill_group(process)
    pdf = outdir / (source.stem + ".pdf")
    return pdf if pdf.is_file() else None


class Handler(BaseHTTPRequestHandler):
    server_version = "malrunar-office-convert"
    sys_version = ""
    config = Config()
    gate = threading.Semaphore(1)

    def identity(self):
        """Who the proxy says the user is: anonymous unless the peer passes the trust check."""
        headers = getattr(self, "headers", None)
        return self.config.identity.resolve(self.client_address[0], dict(headers.items()) if headers else {})

    def log_message(self, fmt, *args):
        # One line per request: peer, user ("-" when anonymous), method, path without query, status.
        # Never the document name, the query, the secret or the identity headers.
        command = getattr(self, "command", None) or "-"
        target = (getattr(self, "path", "") or "").split("?", 1)[0]
        if command == "GET" and target == "/health":
            return
        path = target if target == "/convert" else "other"
        code = args[1] if len(args) > 1 else "-"
        print(f"office-convert: {self.client_address[0]} user={self.identity().user or '-'} "
              f"{command} {path} {code}", file=sys.stderr, flush=True)

    def _reply(self, status, body=b"", content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, b"ok")
        return self._reply(404, b"not found")

    def do_POST(self):
        if self.path != "/convert":
            return self._reply(404, b"not found")
        try:
            status, body, content_type = self._convert()
        except HttpError as error:
            return self._reply(error.status, error.message.encode())
        return self._reply(status, body, content_type)

    def _convert(self):
        config = self.config
        suffix = Path(self.headers.get("X-Filename", "")).suffix.lower()
        if suffix not in ALLOWED:
            raise HttpError(400, "unsupported document type")
        with self.gate:
            work = Path(tempfile.mkdtemp(prefix="convert-"))
            try:
                source = work / f"document{suffix}"
                with source.open("wb") as handle:
                    read_body(self.rfile, self.headers, handle, config.max_bytes)
                try:
                    pdf = run_soffice(source, work, config.timeout)
                except subprocess.TimeoutExpired:
                    raise HttpError(504, "conversion timed out") from None
                except OSError:
                    raise HttpError(500, "could not start LibreOffice") from None
                if pdf is None:
                    raise HttpError(422, "LibreOffice could not convert this document")
                return 200, pdf.read_bytes(), "application/pdf"
            finally:
                shutil.rmtree(work, ignore_errors=True)


def make_server(config):
    handler = type("ConfiguredHandler", (Handler,), {"config": config, "gate": threading.Semaphore(1)})
    server = ThreadingHTTPServer((config.bind, config.port), handler)
    server.daemon_threads = True
    return server


def healthcheck(config):
    host = "127.0.0.1" if config.bind in ("0.0.0.0", "", "::") else config.bind
    if ":" in host:
        host = f"[{host}]"
    try:
        with urllib.request.urlopen(f"http://{host}:{config.port}/health", timeout=3) as response:
            return response.status == 200 and response.read() == b"ok"
    except OSError:
        return False


def main(argv):
    try:
        config = load_config()
    except ConfigError as error:
        print(f"office-convert: {error}", file=sys.stderr)
        return 2
    if argv[1:] == ["--healthcheck"]:
        return 0 if healthcheck(config) else 1
    if argv[1:]:
        print("usage: convert.py [--healthcheck]", file=sys.stderr)
        return 2
    if shutil.which(SOFFICE) is None:
        print(f"office-convert: {SOFFICE} not found", file=sys.stderr)
        return 2
    server = make_server(config)
    print(f"office-convert: listening on {config.bind}:{config.port}", flush=True)
    warning = config.identity.startup_warning()
    if warning:
        print(f"office-convert: {warning}", file=sys.stderr, flush=True)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
