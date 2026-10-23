"""Unit tests for convert.py with soffice replaced by a stub script (no LibreOffice needed).

Run: PYTHONPATH=<dir containing convert.py> python3 -m unittest discover -s office-convert/test
(the image smoke test runs them inside the image, against /opt/malrunar/office-convert).
"""
import contextlib
import http.client
import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.environ.get("MALRUNAR_OFFICE_CODE") or str(Path(__file__).resolve().parent.parent))
import convert  # noqa: E402

STUB = """#!/bin/sh
# stand-in for soffice: records its arguments, then behaves as MODE says
outdir=''; src=''
while [ $# -gt 0 ]; do
    case "$1" in
        --outdir) outdir="$2"; shift ;;
        --headless|--norestore|--nolockcheck|--nodefault|--convert-to|pdf|-env:*) ;;
        *) src="$1" ;;
    esac
    shift
done
mkdir -p "$outdir"
stem="$(basename "${src%.*}")"
case "$(cat "$STUB_MODE")" in
    ok) printf '%%PDF-1.4 stub of %s\\n' "$(basename "$src")" > "$outdir/$stem.pdf" ;;
    fail) ;;
    slow) sleep 300 & echo $! > "$STUB_PIDFILE"; wait ;;
    wait) while [ ! -e "$STUB_RELEASE" ]; do sleep 0.05; done
          printf '%%PDF-1.4 slow\\n' > "$outdir/$stem.pdf" ;;
esac
"""


class Headers(dict):
    def get(self, key, default=None):
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


def read(body, headers, max_bytes=1000):
    out = io.BytesIO()
    size = convert.read_body(io.BytesIO(body), Headers(headers), out, max_bytes)
    return size, out.getvalue()


class ReadBodyTests(unittest.TestCase):
    def status(self, body, headers, max_bytes=1000):
        with self.assertRaises(convert.HttpError) as caught:
            read(body, headers, max_bytes)
        return caught.exception.status

    def test_content_length(self):
        self.assertEqual(read(b"hello", {"Content-Length": "5"}), (5, b"hello"))

    def test_malformed_content_length_is_400(self):
        for value in ("abc", "-1", "1.5", "0x10", "", "5 5", "٥"):
            self.assertEqual(self.status(b"hello", {"Content-Length": value}), 400, value)

    def test_empty_body_is_400(self):
        self.assertEqual(self.status(b"", {"Content-Length": "0"}), 400)

    def test_missing_length_is_411(self):
        self.assertEqual(self.status(b"hello", {}), 411)

    def test_too_large_is_413_before_reading(self):
        self.assertEqual(self.status(b"x", {"Content-Length": "1001"}), 413)

    def test_short_body_is_400(self):
        self.assertEqual(self.status(b"abc", {"Content-Length": "10"}), 400)

    def test_chunked(self):
        body = b"3\r\nabc\r\n4;ext=1\r\ndefg\r\n0\r\nTrailer: x\r\n\r\n"
        self.assertEqual(read(body, {"Transfer-Encoding": "Chunked"}), (7, b"abcdefg"))

    def test_malformed_chunk_size_is_400(self):
        for size in (b"zz", b"", b"-1", b"0x3", b"+3", b" ", b"1" * 40):
            body = size + b"\r\nabc\r\n0\r\n\r\n"
            self.assertEqual(self.status(body, {"Transfer-Encoding": "chunked"}), 400, size)

    def test_chunk_without_crlf_is_400(self):
        self.assertEqual(self.status(b"3\r\nabcXX0\r\n\r\n", {"Transfer-Encoding": "chunked"}), 400)

    def test_truncated_chunk_is_400(self):
        self.assertEqual(self.status(b"9\r\nabc", {"Transfer-Encoding": "chunked"}), 400)

    def test_chunked_too_large_is_413(self):
        body = b"3e9\r\n" + b"x" * 1001 + b"\r\n0\r\n\r\n"
        self.assertEqual(self.status(body, {"Transfer-Encoding": "chunked"}), 413)

    def test_other_transfer_encoding_is_400(self):
        self.assertEqual(self.status(b"x", {"Transfer-Encoding": "gzip"}), 400)

    def test_overlong_chunk_header_is_400(self):
        self.assertEqual(self.status(b"1" * 10000 + b"\r\n", {"Transfer-Encoding": "chunked"}), 400)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = convert.load_config({})
        self.assertEqual((config.port, config.bind, config.timeout, config.max_bytes),
                         (8090, "127.0.0.1", 180, 200 * 1024 * 1024))

    def test_overrides(self):
        config = convert.load_config({
            "MALRUNAR_OFFICE_PORT": "9000", "MALRUNAR_OFFICE_BIND": "0.0.0.0",
            "MALRUNAR_OFFICE_TIMEOUT_SECONDS": "5", "MALRUNAR_OFFICE_MAX_BYTES": "1024"})
        self.assertEqual((config.port, config.bind, config.timeout, config.max_bytes),
                         (9000, "0.0.0.0", 5, 1024))

    def test_errors_name_the_setting(self):
        for name, value in (("MALRUNAR_OFFICE_PORT", "http"), ("MALRUNAR_OFFICE_PORT", "70000"),
                            ("MALRUNAR_OFFICE_TIMEOUT_SECONDS", "0"), ("MALRUNAR_OFFICE_MAX_BYTES", "-5")):
            with self.assertRaises(convert.ConfigError) as caught:
                convert.load_config({name: value})
            self.assertIn(name, str(caught.exception))


class ServerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.mode = tmp / "mode"
        self.pidfile = tmp / "pid"
        self.release = tmp / "release"
        stub = tmp / "soffice"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        os.environ.update(STUB_MODE=str(self.mode), STUB_PIDFILE=str(self.pidfile),
                          STUB_RELEASE=str(self.release))
        self.saved = convert.SOFFICE
        convert.SOFFICE = str(stub)
        self.set_mode("ok")
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        convert.SOFFICE = self.saved
        self.release.touch()
        self.tmp.cleanup()

    def set_mode(self, mode):
        self.mode.write_text(mode)

    def start(self, **settings):
        server = convert.make_server(convert.Config(port=0, **settings))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return server.server_address[1]

    def request(self, port, method, path, body=None, headers=None, timeout=30):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, response.getheader("Content-Type"), data


class ServerTests(ServerCase):
    def test_health_and_not_found(self):
        port = self.start()
        self.assertEqual(self.request(port, "GET", "/health")[::2], (200, b"ok"))
        self.assertEqual(self.request(port, "GET", "/nope")[0], 404)
        self.assertEqual(self.request(port, "POST", "/nope", b"x")[0], 404)

    def test_convert_every_allowed_type(self):
        port = self.start()
        for suffix in sorted(convert.ALLOWED):
            status, ctype, data = self.request(
                port, "POST", "/convert", b"doc", {"X-Filename": f"Quarterly Review{suffix.upper()}"})
            self.assertEqual((status, ctype), (200, "application/pdf"), suffix)
            self.assertTrue(data.startswith(b"%PDF"), suffix)

    def test_unsupported_or_missing_filename_is_400(self):
        port = self.start()
        for name in ("a.exe", "a", "", "a.pdf", "../../etc/passwd"):
            self.assertEqual(self.request(port, "POST", "/convert", b"doc", {"X-Filename": name})[0], 400, name)

    def test_malformed_length_over_the_wire_is_400(self):
        port = self.start()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(b"POST /convert HTTP/1.1\r\nHost: x\r\nX-Filename: a.docx\r\n"
                         b"Content-Length: banana\r\n\r\nabc")
            self.assertIn(b" 400 ", sock.recv(4096).split(b"\r\n")[0])

    def test_malformed_chunk_over_the_wire_is_400(self):
        port = self.start()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(b"POST /convert HTTP/1.1\r\nHost: x\r\nX-Filename: a.docx\r\n"
                         b"Transfer-Encoding: chunked\r\n\r\nxyz\r\nabc\r\n0\r\n\r\n")
            self.assertIn(b" 400 ", sock.recv(4096).split(b"\r\n")[0])

    def test_oversize_is_413(self):
        port = self.start(max_bytes=10)
        status = self.request(port, "POST", "/convert", b"x" * 11, {"X-Filename": "a.docx"})[0]
        self.assertEqual(status, 413)
        self.assertEqual(self.request(port, "POST", "/convert", b"x" * 10, {"X-Filename": "a.docx"})[0], 200)

    def test_conversion_failure_is_422(self):
        self.set_mode("fail")
        port = self.start()
        self.assertEqual(self.request(port, "POST", "/convert", b"doc", {"X-Filename": "a.odt"})[0], 422)

    def test_missing_soffice_is_500(self):
        convert.SOFFICE = "/nonexistent/soffice"
        port = self.start()
        self.assertEqual(self.request(port, "POST", "/convert", b"doc", {"X-Filename": "a.odt"})[0], 500)

    def test_timeout_is_504_and_kills_the_process_group(self):
        self.set_mode("slow")
        port = self.start(timeout=1)
        started = time.monotonic()
        status = self.request(port, "POST", "/convert", b"doc", {"X-Filename": "a.odt"})[0]
        self.assertEqual(status, 504)
        self.assertLess(time.monotonic() - started, 20)
        child = int(self.pidfile.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and Path(f"/proc/{child}").exists():
            time.sleep(0.05)
        state = ""
        try:
            state = Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            pass
        self.assertIn(state, ("", "Z"), "the stub's child process survived the timeout")
        self.set_mode("ok")
        self.assertEqual(self.request(port, "POST", "/convert", b"doc", {"X-Filename": "a.odt"})[0], 200)

    def test_health_answers_during_a_conversion_and_conversions_are_serial(self):
        self.set_mode("wait")
        port = self.start()
        results = []

        def post():
            results.append(self.request(port, "POST", "/convert", b"doc", {"X-Filename": "a.odt"})[0])

        threads = [threading.Thread(target=post) for _ in range(2)]
        for thread in threads:
            thread.start()
        time.sleep(0.5)
        started = time.monotonic()
        self.assertEqual(self.request(port, "GET", "/health", timeout=5)[0], 200)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(results, [])
        self.release.touch()
        for thread in threads:
            thread.join(30)
        self.assertEqual(results, [200, 200])

    def test_healthcheck_function(self):
        port = self.start()
        self.assertTrue(convert.healthcheck(convert.Config(port=port, bind="0.0.0.0")))
        self.assertFalse(convert.healthcheck(convert.Config(port=1, bind="127.0.0.1")))

    def test_work_directories_are_removed(self):
        port = self.start()
        before = set(Path(tempfile.gettempdir()).glob("convert-*"))
        self.request(port, "POST", "/convert", b"doc", {"X-Filename": "a.odt"})
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("convert-*")), before)


if __name__ == "__main__":
    unittest.main()


SECRET_A = "first-secret-0123456789"
SECRET_B = "second-secret-0123456789"
IDENTITY_HEADERS = {"Remote-User": "ada", "Remote-Groups": "admins"}


class IdentityServerTests(ServerCase):
    """The same server with an identity configuration: the user is resolved and logged, never enforced."""

    def identity_port(self, env):
        config = convert.load_config({"MALRUNAR_OFFICE_PORT": "9", **env})
        return self.start(identity=config.identity)

    def logged(self, port, headers, path="/convert", body=b"x"):
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            status = self.request(port, "POST", path, body=body,
                                  headers={"X-Filename": "secret-name.docx", **headers})[0]
            time.sleep(0.1)
        return status, log.getvalue()

    def test_nothing_configured_ignores_headers_and_warns(self):
        config = convert.load_config({})
        self.assertIn("anonymous", config.identity.startup_warning())
        status, log = self.logged(self.start(), IDENTITY_HEADERS)
        self.assertEqual(status, 200)
        self.assertIn("user=- ", log)
        self.assertNotIn("ada", log)

    def test_spoofed_headers_from_an_untrusted_peer_are_ignored(self):
        port = self.identity_port({"MALRUNAR_OFFICE_TRUSTED_PROXIES": "192.0.2.0/24"})
        status, log = self.logged(port, IDENTITY_HEADERS)
        self.assertEqual(status, 200)  # not rejected, just anonymous
        self.assertIn("user=- ", log)

    def test_trusted_peer_user_is_logged_and_visible_to_the_handler(self):
        seen = []
        original = convert.Handler.identity
        convert.Handler.identity = lambda handler: seen.append(original(handler)) or seen[-1]
        self.addCleanup(setattr, convert.Handler, "identity", original)
        port = self.identity_port({"MALRUNAR_OFFICE_TRUSTED_PROXIES": "127.0.0.0/8"})
        status, log = self.logged(port, {**IDENTITY_HEADERS, "Remote-Email": "ada@example.invalid"})
        self.assertEqual(status, 200)
        self.assertIn("user=ada ", log)
        self.assertEqual(seen[0].user, "ada")
        self.assertEqual(seen[0].groups, ("admins",))
        for private in ("secret-name", "ada@example.invalid", "admins"):
            self.assertNotIn(private, log)

    def test_health_and_conversion_behave_as_before_for_any_identity(self):
        port = self.identity_port({"MALRUNAR_OFFICE_TRUSTED_PROXIES": "192.0.2.0/24"})
        self.assertEqual(self.request(port, "GET", "/health")[::2], (200, b"ok"))
        self.assertEqual(self.logged(port, {}, body=b"")[0], 400)

    def test_secret_header_wrong_secret_ignored_and_rotation(self):
        path = Path(self.tmp.name) / "proxy-secrets"
        path.write_text(f"# old\n{SECRET_A}\n")
        port = self.identity_port({"MALRUNAR_OFFICE_PROXY_SECRET_HEADER": "X-Proxy-Secret",
                                   "MALRUNAR_OFFICE_PROXY_SECRET_FILE": str(path)})
        def user_for(secret):
            _, log = self.logged(port, {**IDENTITY_HEADERS, "X-Proxy-Secret": secret})
            self.assertNotIn(secret, log)
            return log.split("user=")[1].split()[0]
        self.assertEqual(user_for(SECRET_A), "ada")
        self.assertEqual(user_for("wrong-secret-0123456789"), "-")
        self.assertEqual(user_for(SECRET_B), "-")
        path.write_text(f"{SECRET_A}\n{SECRET_B}\n")  # rotation: both accepted
        os.utime(path, ns=(time.time_ns() + 5_000_000_000,) * 2)
        self.assertEqual(user_for(SECRET_B), "ada")
        self.assertEqual(user_for(SECRET_A), "ada")
        path.write_text(f"{SECRET_B}\n")  # old one retired
        os.utime(path, ns=(time.time_ns() + 10_000_000_000,) * 2)
        self.assertEqual(user_for(SECRET_A), "-")
        self.assertEqual(user_for(SECRET_B), "ada")

    def test_both_configured_both_required(self):
        path = Path(self.tmp.name) / "proxy-secrets"
        path.write_text(SECRET_A + "\n")
        port = self.identity_port({"MALRUNAR_OFFICE_TRUSTED_PROXIES": "192.0.2.0/24",
                                   "MALRUNAR_OFFICE_PROXY_SECRET_HEADER": "X-Proxy-Secret",
                                   "MALRUNAR_OFFICE_PROXY_SECRET_FILE": str(path)})
        _, log = self.logged(port, {**IDENTITY_HEADERS, "X-Proxy-Secret": SECRET_A})  # right secret, wrong peer
        self.assertIn("user=- ", log)

    def test_bad_identity_setting_is_a_config_error(self):
        with self.assertRaises(convert.ConfigError) as caught:
            convert.load_config({"MALRUNAR_OFFICE_TRUSTED_PROXIES": "not-a-cidr"})
        self.assertIn("MALRUNAR_OFFICE_TRUSTED_PROXIES", str(caught.exception))


class VendoredVectorTests(unittest.TestCase):
    """The shared vectors (identical in every component) against the vendored forward_auth.py."""

    def test_vectors(self):
        doc = json.loads((Path(__file__).resolve().parent / "forward-auth-vectors.json").read_text(encoding="utf-8"))
        fa = convert.forward_auth
        self.assertGreaterEqual(len(doc["cases"]), 30)
        for case in doc["cases"]:
            with self.subTest(case["name"]), tempfile.TemporaryDirectory() as tmp:
                env = dict(case["config"])
                if "secret_file_contents" in case:
                    secrets = Path(tmp, "secrets")
                    secrets.write_text(case["secret_file_contents"], encoding="utf-8")
                    env[doc["prefix"] + "PROXY_SECRET_FILE"] = str(secrets)
                table = case.get("expand", {})
                def expand(value):
                    return "a" * table[value] if isinstance(value, str) and value in table else value
                headers = {k: expand(v) for k, v in case["request"]["headers"].items()}
                expected = {k: v if isinstance(v, list) else expand(v) for k, v in case["expected"].items()}
                got = fa.IdentityConfig.from_env(env, doc["prefix"]).resolve(case["request"]["peer"], headers)
                self.assertEqual({"user": got.user, "name": got.name, "email": got.email,
                                  "groups": list(got.groups), "trusted": got.trusted}, expected)
