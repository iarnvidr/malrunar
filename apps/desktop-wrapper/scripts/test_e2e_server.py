"""The stand-in site of the macOS end-to-end test, exercised without a browser."""
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "e2e"))
import server  # noqa: E402


class StandInSite(unittest.TestCase):
    def test_serves_the_page_and_records_the_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "r.jsonl"
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(str(report)))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            try:
                with urllib.request.urlopen(base + "/") as response:
                    self.assertIn(b"new Notification(", response.read())
                request = urllib.request.Request(base + "/report", data=b'{"shown": true}', method="POST")
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 204)
                urllib.request.urlopen(urllib.request.Request(base + "/report", data=b"{bad", method="POST"))
            finally:
                httpd.shutdown()
            lines = [json.loads(line) for line in report.read_text().splitlines()]
            self.assertEqual(lines[0]["get"], "/")
            self.assertEqual(lines[1], {"report": {"shown": True}})
            self.assertIn("report_invalid", lines[2])


if __name__ == "__main__":
    unittest.main()
