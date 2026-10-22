# SPDX-License-Identifier: Apache-2.0
"""Small pure functions: obs-websocket authentication, b-roll span maths, URL profiles, relay capture."""
import base64
import hashlib
import json
import socket
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

import helpers  # noqa: F401  (puts the package on sys.path)
from helpers import TempDir
from malrunar_obs import relay
from malrunar_obs.clips import clean_display_name, slugify
from malrunar_obs.controller import camera_profile_url
from malrunar_obs.daily_video import _intersect, exclude_voice_activity, usable_duration
from malrunar_obs.levels import level_db
from malrunar_obs.obsws import auth_string


class AuthStringTests(unittest.TestCase):
    def test_matches_the_published_algorithm(self):
        secret = base64.b64encode(hashlib.sha256(b"supersecretpassword" + b"salt").digest()).decode()
        expected = base64.b64encode(hashlib.sha256((secret + "challenge").encode()).digest()).decode()
        self.assertEqual(auth_string("supersecretpassword", "salt", "challenge"), expected)


class UrlProfileTests(unittest.TestCase):
    def test_no_query_settings_leave_the_url_alone(self):
        url = "https://x.example.test/v?a=1&maxwidth=640"
        self.assertEqual(camera_profile_url(url, {}), url)

    def test_query_is_replaced_and_dropped(self):
        profile = {"camera_query": {"scale": "50", "videobitrate": "1000"}, "camera_query_drop": ["maxwidth"]}
        self.assertEqual(
            camera_profile_url("https://x.example.test/v?a=1&maxwidth=640&scale=100&blank=", profile),
            "https://x.example.test/v?a=1&blank=&scale=50&videobitrate=1000",
        )


class BrollMathTests(unittest.TestCase):
    def test_usable_duration_subtracts_the_joins(self):
        self.assertAlmostEqual(usable_duration([("a", [(0, 10), (20, 30)])]), 19.65)
        self.assertEqual(usable_duration([]), 0.0)

    def test_voice_markers_cut_spans(self):
        spans = exclude_voice_activity([(0, 30)], [10], minimum=3, padding=1)
        self.assertEqual(spans, [(0, 9), (11, 30)])
        self.assertEqual(exclude_voice_activity([(0, 5)], [2.5], minimum=3, padding=1), [])

    def test_intersect(self):
        self.assertEqual(_intersect([(0, 10)], [(5, 20)], 3), [(5, 10)])
        self.assertEqual(_intersect([(0, 10)], [(9, 20)], 3), [])


class MiscTests(unittest.TestCase):
    def test_level_db(self):
        self.assertEqual(level_db([]), -100.0)
        self.assertEqual(level_db([[0, 0, 0.0]]), -100.0)
        self.assertEqual(level_db([[0, 0, 1.0], [0, 0, 0.1]]), 0.0)

    def test_names(self):
        self.assertEqual(clean_display_name("  a   b \n"), "a b")
        self.assertEqual(clean_display_name("   "), "Recorded clip")
        self.assertEqual(len(clean_display_name("x" * 100)), 60)
        self.assertEqual(slugify("Hello, World!"), "hello-world")
        self.assertEqual(slugify("!!!"), "clip")


class RelayTests(TempDir, unittest.TestCase):
    def test_capture_control_api_and_forwarding(self):
        relay.CAPTURE_DIR = self.tmp / "captures"
        target = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        target.bind(("127.0.0.1", 0))
        target.settimeout(5)
        relay.TARGET = ("127.0.0.1", target.getsockname()[1])
        server = ThreadingHTTPServer(("127.0.0.1", 0), relay.ControlHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        outgoing = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(outgoing.close)
        self.addCleanup(target.close)
        self.addCleanup(server.server_close)

        def call(method, path, body=None):
            request = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None)
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()

        self.assertEqual(json.loads(call("GET", "/recording/status")[1]), {"recording": False})
        self.assertEqual(call("POST", "/recording/start", {"capture_id": "../bad"})[0], 400)
        self.assertEqual(call("POST", "/recording/stop")[0], 409)
        self.assertEqual(call("POST", "/recording/start", {"capture_id": "cap-1"})[0], 201)
        self.assertEqual(call("POST", "/recording/start", {"capture_id": "cap-2"})[0], 409)
        relay.forward(b"x" * 1316, outgoing)
        self.assertEqual(target.recv(2000), b"x" * 1316)
        time.sleep(0.2)
        status, body = call("POST", "/recording/stop")
        self.assertEqual((status, json.loads(body)["bytes"]), (200, 1316))
        status, body = call("GET", "/recording/file/cap-1")
        self.assertEqual((status, body), (200, b"x" * 1316))
        self.assertEqual(call("DELETE", "/recording/file/cap-1")[0], 200)
        self.assertEqual(call("GET", "/recording/file/cap-1")[0], 404)
        relay.active_capture = None


if __name__ == "__main__":
    unittest.main()
