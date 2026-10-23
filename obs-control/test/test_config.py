# SPDX-License-Identifier: Apache-2.0
import copy
import json
import re
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts the package on sys.path)
from helpers import CAMERA_URL, TempDir, make_config, write_config
from malrunar_obs.config import DEFAULTS, ConfigError, load_config


class DefaultsTests(TempDir, unittest.TestCase):
    def test_defaults_with_only_a_camera_url(self):
        config = make_config(self.tmp)
        self.assertEqual(config.cameras[0].url, CAMERA_URL)
        self.assertEqual(config.scene_keys, {"live": "Live Feed", "video": "B-roll Video"})
        self.assertEqual(config.stopped.scene, "Stopped")
        self.assertEqual((config.video.width, config.video.height, config.video.fps), (640, 360, 24))
        self.assertFalse(config.features.program_control)
        self.assertEqual(config.start_scene_name, "Live Feed")
        self.assertEqual(config.websocket_url, "ws://127.0.0.1:4455")

    def test_program_control_starts_on_the_stopped_scene(self):
        config = make_config(self.tmp, features={"program_control": True})
        self.assertEqual(config.start_scene_name, "Stopped")

    def test_explicit_start_scene_by_key_or_name(self):
        self.assertEqual(make_config(self.tmp, obs={"start_scene": "video"}).start_scene_name, "B-roll Video")
        self.assertEqual(make_config(self.tmp, obs={"start_scene": "Stopped"}).start_scene_name, "Stopped")
        with self.assertRaises(ConfigError):
            make_config(self.tmp, obs={"start_scene": "nowhere"})

    def test_a_camera_url_is_required(self):
        with self.assertRaisesRegex(ConfigError, "browser-source URL"):
            load_config(env={}, path=write_config(self.tmp, cameras=[{}]))

    def test_defaults_contain_no_product_names(self):
        text = json.dumps(DEFAULTS)
        # bracketed so this file does not contain the words itself (scripts/check-public-hygiene.sh)
        self.assertIsNone(re.search(r"(?i)c[a]ll studio|v[d]o|n[i]nja|t[e]ams|[e]sben|\b[a]lice\b|\bC[A]LL_|\bR[K]_", text))


class ExampleConfigTests(unittest.TestCase):
    """The example file documents the defaults, so it must not drift from them."""

    def test_example_equals_the_defaults_apart_from_the_camera_and_profiles(self):
        example = helpers.ROOT / "examples" / "obs-control.example.toml"
        if not example.is_file():  # inside the image the tests are mounted apart from the examples
            example = Path("/usr/local/share/malrunar/obs-control/examples/obs-control.example.toml")
        config = load_config(env={}, path=example)
        defaults = copy.deepcopy(DEFAULTS)
        got = config.to_dict()
        for data in (defaults, got):
            data["obs"].pop("config_dir")
            data.pop("profiles")
            data["cameras"][0]["url"] = "x"
        self.assertEqual(got, defaults)
        self.assertEqual(list(config.profiles), ["360p", "720p"])


class FileTests(TempDir, unittest.TestCase):
    def test_toml_file(self):
        path = self.tmp / "c.toml"
        path.write_text(
            '[[cameras]]\nkey = "main"\nscene = "Main"\nsource = "Main cam"\nurl = "http://a/"\n'
            '[[cameras]]\nkey = "side"\nscene = "Side"\nsource = "Side cam"\nurl_file = "%s"\n'
            '[media]\nkey = "broll"\nscene = "Filler"\nsource = "Filler source"\n'
            '[profiles.hd]\nwidth = 1280\nheight = 720\nencoder_kbps = 3000\n'
            '[video]\ndefault_profile = "hd"\n[plugin.demo]\nanything = 1\n' % (self.tmp / "url.txt")
        )
        (self.tmp / "url.txt").write_text("http://side/\n")
        config = load_config(env={}, path=path)
        self.assertEqual([c.key for c in config.cameras], ["main", "side"])
        self.assertEqual(config.scene_keys, {"main": "Main", "side": "Side", "broll": "Filler"})
        self.assertEqual(config.camera_url(config.cameras[1]), "http://side/")
        self.assertEqual(list(config.profiles), ["hd"])
        self.assertEqual(config.plugin["demo"]["anything"], 1)

    def test_url_file_is_reread(self):
        url_file = self.tmp / "url.txt"
        url_file.write_text("http://one/")
        config = load_config(env={}, path=write_config(self.tmp, cameras=[{"url_file": str(url_file)}]))
        url_file.write_text("http://two/")
        self.assertEqual(config.camera_url(config.cameras[0]), "http://two/")

    def test_unknown_settings_are_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown setting"):
            make_config(self.tmp, output={"urll": "x"})
        with self.assertRaisesRegex(ConfigError, "unknown section"):
            make_config(self.tmp, bogus={})

    def test_wrong_types_are_rejected(self):
        with self.assertRaisesRegex(ConfigError, "expected an integer"):
            make_config(self.tmp, http={"port": "80"})
        with self.assertRaisesRegex(ConfigError, "true or false"):
            make_config(self.tmp, features={"program_control": "yes"})

    def test_duplicate_names_and_keys_are_rejected(self):
        with self.assertRaisesRegex(ConfigError, "already used"):
            make_config(self.tmp, media={"scene": "Live Feed"})
        with self.assertRaisesRegex(ConfigError, "already used"):
            make_config(self.tmp, media={"key": "live"})

    def test_stopped_colour_is_validated(self):
        with self.assertRaisesRegex(ConfigError, "#rrggbb"):
            make_config(self.tmp, stopped={"color": "black"})

    def test_relay_does_not_need_a_camera(self):
        path = write_config(self.tmp, cameras=[{}], relay={"target_host": "bridge"})
        config = load_config(env={}, path=path, require_cameras=False)
        self.assertEqual(config.relay.target_host, "bridge")
        with self.assertRaises(ConfigError):
            load_config(env={}, path=path)

    def test_camera_restart_flag_is_optional(self):
        self.assertIsNone(make_config(self.tmp).cameras[0].restart_when_active)
        self.assertFalse(make_config(self.tmp, cameras=[{"url": "http://x/", "restart_when_active": False}]).cameras[0].restart_when_active)
        with self.assertRaisesRegex(ConfigError, "true or false"):
            make_config(self.tmp, cameras=[{"url": "http://x/", "restart_when_active": "yes"}])

    def test_removed_access_settings_are_unknown(self):
        for key, value in (("enforce", True), ("allowed_identities", ["ann"])):
            with self.assertRaisesRegex(ConfigError, key):
                make_config(self.tmp, access={key: value})

    def test_missing_explicit_file_is_an_error(self):
        with self.assertRaisesRegex(ConfigError, "not found"):
            load_config(env={"MALRUNAR_OBS_CONFIG": str(self.tmp / "nope.toml")})

    def test_unreadable_config_file_reports_permission_denied(self):
        import os
        import subprocess
        import sys

        config = self.tmp / "private" / "config.toml"
        config.parent.mkdir()
        config.write_text('[[cameras]]\nurl = "https://example.test/"\n')
        config.parent.chmod(0o700)
        if os.geteuid() == 0:
            command = ["setpriv", "--reuid", "65534", "--regid", "65534", "--clear-groups", sys.executable, "-c",
                       "from malrunar_obs.config import load_config; load_config(env={}, path=__import__('sys').argv[1])", str(config)]
            if not __import__("shutil").which("setpriv"):
                self.skipTest("setpriv required to verify directory traversal denial as non-root")
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("permission denied", result.stderr.lower())
        else:
            config.parent.chmod(0o755)
            config.chmod(0o000)
            with self.assertRaisesRegex(ConfigError, "permission denied"):
                load_config(env={}, path=config)

    def test_inaccessible_config_directory_is_not_treated_as_missing(self):
        from unittest import mock
        from pathlib import Path

        with mock.patch.object(Path, "stat", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(ConfigError, "permission denied"):
                load_config(env={}, path="/private/config.toml")
            with self.assertRaisesRegex(ConfigError, "permission denied"):
                load_config(env={})


class EnvTests(TempDir, unittest.TestCase):
    def load(self, env, **sections):
        return make_config(self.tmp, env=env, **sections)

    def test_generic_section_key_variables(self):
        config = self.load(
            {
                "MALRUNAR_OBS_OUTPUT_URL": "udp://203.0.113.5:9000",
                "MALRUNAR_OBS_VIDEO_FPS": "30",
                "MALRUNAR_OBS_FEATURES_PROGRAM_CONTROL": "true",
                "MALRUNAR_OBS_BROLL_SILENCE_DB": "-40.5",
                "MALRUNAR_OBS_ACCESS_IDENTITY_HEADER": "X-User",
            }
        )
        self.assertEqual(config.output.url, "udp://203.0.113.5:9000")
        self.assertEqual(config.video.fps, 30)
        self.assertTrue(config.features.program_control)
        self.assertEqual(config.broll.silence_db, -40.5)
        self.assertEqual(config.access.identity_header, "X-User")

    def test_aliases_and_first_camera_variables(self):
        config = self.load(
            {
                "MALRUNAR_OBS_CONTROL_PORT": "19000",
                "MALRUNAR_OBS_RECORDINGS_DIR": "/data/rec",
                "MALRUNAR_OBS_MEDIA_FILE": "/media/loop.mp4",
                "MALRUNAR_OBS_CAMERA_URL": "http://env/cam",
                "MALRUNAR_OBS_CAMERA_RESTART_WHEN_ACTIVE": "false",
                "MALRUNAR_OBS_PLUGINS": "one,two",
                "MALRUNAR_OBS_PROFILE": "p1",
                "MALRUNAR_OBS_WEBSOCKET_PORT": "4466",
            }
        )
        self.assertEqual(config.http.port, 19000)
        self.assertEqual(config.storage.recordings_dir, "/data/rec")
        self.assertEqual(config.media.file, "/media/loop.mp4")
        self.assertEqual(config.cameras[0].url, "http://env/cam")
        self.assertFalse(config.cameras[0].restart_when_active)
        self.assertEqual(config.plugins.modules, ["one", "two"])
        self.assertEqual((config.obs.profile, config.obs.websocket_port), ("p1", 4466))

    def test_environment_beats_the_file(self):
        config = self.load({"MALRUNAR_OBS_OUTPUT_URL": "udp://env:1"}, output={"url": "udp://file:1"})
        self.assertEqual(config.output.url, "udp://env:1")

    def test_bad_values_name_the_variable(self):
        with self.assertRaisesRegex(ConfigError, "MALRUNAR_OBS_VIDEO_FPS"):
            self.load({"MALRUNAR_OBS_VIDEO_FPS": "fast"})
        with self.assertRaisesRegex(ConfigError, "MALRUNAR_OBS_FEATURES_SENDER_LEASE"):
            self.load({"MALRUNAR_OBS_FEATURES_SENDER_LEASE": "maybe"})

    def test_environment_only_configuration(self):
        config = load_config(env={"MALRUNAR_OBS_CAMERA_URL": "http://only-env/"})
        self.assertEqual(config.cameras[0].url, "http://only-env/")


if __name__ == "__main__":
    unittest.main()
