import contextlib
import io
import json
import os
import plistlib
import re
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

import configure as c

APP_DIR = Path(__file__).resolve().parent.parent
EXAMPLE = json.loads((APP_DIR / "wrapper.config.example.json").read_text())


def png(path, size):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    header = struct.pack(">IIBBBBB", size[0], size[1], 8, 2, 0, 0, 0)
    row = b"\x00" + b"\x00\x00\x00" * size[0]
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                           + chunk(b"IDAT", zlib.compress(row * size[1])) + chunk(b"IEND", b""))


def run(**changes):
    raw = {**EXAMPLE, **changes}
    return c.normalise(raw, APP_DIR, env={})


class Valid(unittest.TestCase):
    def test_example_is_valid_and_matches_the_rust_fixture(self):
        app, icon = run()
        self.assertIsNone(icon)
        fixture = (APP_DIR / "src-tauri/core/tests/fixtures/app.json").read_text()
        self.assertEqual(json.loads(fixture), app)

    def test_identifier_comes_from_prefix_and_id(self):
        self.assertEqual(run(id="my-app")[0]["identifier"], "io.github.iarnvidr.my-app")
        self.assertEqual(run(id="x1", identifier_prefix="org.example")[0]["identifier"], "org.example.x1")

    def test_defaults(self):
        app, _ = c.normalise({"name": "A", "id": "aa", "url": "https://a.example/"}, APP_DIR, env={})
        self.assertEqual(app["version"], "0.1.0")
        self.assertEqual(app["internal_hosts"], [])
        self.assertEqual(app["window"], c.WINDOW_DEFAULTS)
        self.assertTrue(app["notifications"] and app["hide_on_close"])

    def test_loopback_http_is_allowed_for_testing(self):
        self.assertEqual(run(url="http://127.0.0.1:8080")[0]["url"], "http://127.0.0.1:8080/")
        self.assertEqual(run(url="http://localhost:3000/x")[0]["url"], "http://localhost:3000/x")
        self.assertEqual(run(url="http://[::1]:3000/")[0]["url"], "http://[::1]:3000/")

    def test_hosts_are_lower_cased_and_deduplicated(self):
        app, _ = run(internal_hosts=["Login.Example.com", "login.example.com", "*.Idp.example.net"])
        self.assertEqual(app["internal_hosts"], ["*.idp.example.net", "login.example.com"])

    def test_environment_overrides_the_file(self):
        app, _ = c.normalise(EXAMPLE, APP_DIR, env={"WRAPPER_NAME": "Other", "WRAPPER_ID": "other",
                                                      "WRAPPER_URL": "https://other.example/", "WRAPPER_VERSION": "2.0.1"})
        self.assertEqual((app["name"], app["identifier"], app["url"], app["version"]),
                         ("Other", "io.github.iarnvidr.other", "https://other.example/", "2.0.1"))

    def test_png_icon(self):
        with tempfile.TemporaryDirectory() as tmp:
            png(Path(tmp) / "i.png", (512, 512))
            _, icon = c.normalise({**EXAMPLE, "icon": "i.png"}, Path(tmp), env={})
            self.assertEqual(icon, Path(tmp) / "i.png")
            (Path(tmp) / "i.svg").write_text("<svg/>")
            self.assertTrue(c.normalise({**EXAMPLE, "icon": "i.svg"}, Path(tmp), env={})[1].name == "i.svg")


class Invalid(unittest.TestCase):
    def assertRejects(self, setting, **changes):
        with self.assertRaises(c.ConfigError) as caught:
            run(**changes)
        self.assertIn(setting, str(caught.exception))

    def test_urls(self):
        for url in ["http://app.example.com/", "ftp://app.example.com/", "file:///etc/passwd", "javascript:alert(1)",
                    "https://user:pw@app.example.com/", "https://", "", "https://a b.example/", "https://-bad.example/",
                    "https://app.example.com:99999/", "app.example.com", "http://127.0.0.1.example.com/"]:
            with self.subTest(url=url):
                self.assertRejects("url", url=url)

    def test_names_and_ids(self):
        for name in ["", " ", "a" * 41, "../x", "x/y", ".hidden", "Bad\u0000name", "<b>", "a&b"]:
            with self.subTest(name=name):
                self.assertRejects("name", name=name)
        for app_id in ["", "A", "Upper", "a", "-a", "a-", "1abc", "has space", "a.b", "a_b", "x" * 41]:
            with self.subTest(id=app_id):
                self.assertRejects("id", id=app_id)

    def test_prefix_and_identifier(self):
        for prefix in ["io", "IO.github.x", "io..x", "io.github.", ".io.x", "io.git hub"]:
            with self.subTest(prefix=prefix):
                self.assertRejects("identifier_prefix", identifier_prefix=prefix)
        self.assertRejects("id", id="app")

    def test_version(self):
        for version in ["1", "1.2", "v1.2.3", "1.2.3-beta", "01.2.3", 3]:
            with self.subTest(version=version):
                self.assertRejects("version", version=version)

    def test_internal_hosts(self):
        for hosts in [["localhost"], ["*.com."], ["https://login.example.com"], ["login.example.com/path"], ["*"],
                      ["192.0.2.1"], ["*.*.example.com"], [""], [5], "login.example.com"]:
            with self.subTest(hosts=hosts):
                self.assertRejects("internal_hosts", internal_hosts=hosts)

    def test_window(self):
        self.assertRejects("window.width", window={"width": 10})
        self.assertRejects("window.height", window={"height": "tall"})
        self.assertRejects("window.width", window={"width": True})
        self.assertRejects("window", window={"min_width": 5000, "width": 1000})
        self.assertRejects("window", window={"depth": 1})
        self.assertRejects("window", window=[])

    def test_flags_and_unknown_keys(self):
        self.assertRejects("notifications", notifications="yes")
        self.assertRejects("hide_on_close", hide_on_close=1)
        self.assertRejects("unknown", colour="red")

    def test_required_and_shape(self):
        for key in ("name", "id", "url"):
            raw = {k: v for k, v in EXAMPLE.items() if k != key}
            with self.assertRaises(c.ConfigError) as caught:
                c.normalise(raw, APP_DIR, env={})
            self.assertIn(key, str(caught.exception))
        with self.assertRaises(c.ConfigError):
            c.normalise([], APP_DIR, env={})

    def test_icons(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.assertRejects("icon", icon=str(tmp / "missing.png"))
            png(tmp / "small.png", (256, 256))
            png(tmp / "wide.png", (1024, 512))
            (tmp / "fake.png").write_bytes(b"not a png at all, but long enough to read a header from")
            (tmp / "i.gif").write_bytes(b"GIF89a")
            for name in ["small.png", "wide.png", "fake.png", "i.gif"]:
                with self.subTest(icon=name):
                    self.assertRejects("icon", icon=str(tmp / name))


class Outputs(unittest.TestCase):
    def test_files_are_written_and_consistent(self):
        app, _ = run(name="Tom's Notes")
        with tempfile.TemporaryDirectory() as tmp:
            tauri = Path(tmp)
            c.write_outputs(app, None, tauri_dir=tauri)
            self.assertEqual(json.loads((tauri / "generated/app.json").read_text()), app)
            overlay = json.loads((tauri / "generated/tauri.overlay.json").read_text())
            self.assertEqual(overlay["identifier"], app["identifier"])
            self.assertEqual(overlay["productName"], app["name"])
            self.assertNotIn("icon", overlay["bundle"])
            plist = (tauri / "Info.plist").read_text()
            parsed = plistlib.loads(plist.encode())
            self.assertIn("Tom's Notes", parsed["NSCameraUsageDescription"])
            # even a name the validator would refuse cannot break out of the XML
            hostile = plistlib.loads(c.info_plist({"name": "A & <b>"}).encode())
            self.assertIn("A & <b>", hostile["NSCameraUsageDescription"])
            self.assertIn("NSMicrophoneUsageDescription", parsed)

    def test_overlay_points_at_generated_icons(self):
        app, _ = run()
        overlay = c.tauri_overlay(app, icons=True)
        self.assertEqual(overlay["bundle"]["icon"][0], "generated/icons/32x32.png")
        self.assertEqual(len(overlay["bundle"]["icon"]), len(c.ICON_FILES))

    def test_the_base_tauri_configuration_lists_the_same_icons(self):
        base = json.loads((APP_DIR / "src-tauri/tauri.conf.json").read_text())
        self.assertEqual(base["bundle"]["icon"], [f"icons/{name}" for name in c.ICON_FILES])
        for name in c.ICON_FILES:
            self.assertTrue((APP_DIR / "src-tauri/icons" / name).is_file(), name)


class Command(unittest.TestCase):
    def test_exit_status_and_message(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps({**EXAMPLE, "url": "http://insecure.example/"}))
            self.assertEqual(c.main(["--config", str(bad), "--check"]), 2)
            self.assertEqual(c.main(["--config", str(Path(tmp) / "missing.json"), "--check"]), 2)
            bad.write_text("{not json")
            self.assertEqual(c.main(["--config", str(bad), "--check"]), 2)
            good = Path(tmp) / "good.json"
            good.write_text(json.dumps(EXAMPLE))
            self.assertEqual(c.main(["--config", str(good), "--check"]), 0)


class Hygiene(unittest.TestCase):
    def test_example_config_uses_reserved_example_domains_only(self):
        text = (APP_DIR / "wrapper.config.example.json").read_text()
        for host in re.findall(r"[a-z0-9.-]+\.(?:com|net|org)", text):
            self.assertRegex(host, r"example\.(com|net|org)$")


if __name__ == "__main__":
    unittest.main()
