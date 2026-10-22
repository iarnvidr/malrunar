#!/usr/bin/env python3
"""A stand-in site for the macOS end-to-end test (standard library only).

    server.py PORT REPORT_FILE

GET anything returns a page that uses the standard Notification and badge APIs, then POSTs what
happened to /report. Requests and the report are appended to REPORT_FILE as JSON lines.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = b"""<!doctype html><meta charset="utf-8"><title>e2e</title><p>e2e page</p>
<script>
(async () => {
  const out = { permission: Notification.permission, internals: typeof window.__TAURI_INTERNALS__ };
  try {
    const n = new Notification('E2E title', { body: 'E2E body' });
    out.shown = await new Promise((resolve) => {
      n.onshow = () => resolve(true);
      n.onerror = () => resolve(false);
      setTimeout(() => resolve('timeout'), 8000);
    });
  } catch (error) { out.error = String(error); }
  try { await navigator.setAppBadge(2); await navigator.clearAppBadge(); out.badge = 'ok'; }
  catch (error) { out.badge = String(error); }
  await fetch('/report', { method: 'POST', body: JSON.stringify(out) });
})();
</script>
"""


def make_handler(report_path):
    def record(entry):
        with open(report_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            record({"get": self.path, "agent": self.headers.get("User-Agent", "")})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)

        def do_POST(self):  # noqa: N802
            body = self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 65536))
            try:
                record({"report": json.loads(body)})
            except ValueError:
                record({"report_invalid": body.decode("utf-8", "replace")})
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    return Handler


if __name__ == "__main__":
    port, report = int(sys.argv[1]), sys.argv[2]
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(report)).serve_forever()
