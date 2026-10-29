# SPDX-License-Identifier: Apache-2.0
import configparser
import json
import os
import stat
import unittest

import helpers  # noqa: F401  (puts the package on sys.path)
from helpers import CAMERA_URL, TempDir, make_config
from malrunar_obs.scenes import build_collection, color_value, generate, launch_args, websocket_password


def sources_by_name(collection):
    return {source["name"]: source for source in collection["sources"]}


class GenerateTests(TempDir, unittest.TestCase):
    def collection(self, config, plugins=()):
        generate(config, plugins)
        path = self.tmp / "obs-config" / "basic" / "scenes" / f"{config.obs.collection}.json"
        return json.loads(path.read_text())

    def test_default_collection(self):
        config = make_config(self.tmp, media={"file": "/media/loop.mp4"})
        collection = self.collection(config)
        names = sources_by_name(collection)
        self.assertEqual([s["name"] for s in collection["scene_order"]], ["Live Feed", "B-roll Video", "Stopped"])
        self.assertEqual(collection["current_program_scene"], "Live Feed")
        camera = names["Camera Receiver"]
        self.assertEqual(camera["id"], "browser_source")
        self.assertEqual(camera["settings"]["url"], CAMERA_URL)
        self.assertEqual((camera["settings"]["width"], camera["settings"]["height"], camera["settings"]["fps"]), (640, 360, 24))
        self.assertTrue(camera["settings"]["restart_when_active"])
        self.assertEqual(names["B-roll Source"]["settings"]["local_file"], "/media/loop.mp4")
        self.assertEqual(names["Stopped Frame"]["settings"]["color"], 0xFF000000)
        scene = names["Live Feed"]
        self.assertEqual(scene["id"], "scene")
        self.assertEqual(scene["settings"]["items"][0]["source_uuid"], camera["uuid"])
        self.assertEqual(scene["settings"]["items"][0]["bounds"], {"x": 640.0, "y": 360.0})
        self.assertEqual(len({s["uuid"] for s in collection["sources"]}), len(collection["sources"]))

    def test_every_name_comes_from_the_config(self):
        config = make_config(
            self.tmp,
            cameras=[
                {"key": "main", "scene": "Main", "source": "Main cam", "url": "http://m/"},
                {"key": "side", "scene": "Side", "source": "Side cam", "url": "http://s/", "restart_when_active": False,
                 "shutdown_when_hidden": True, "width": 1280, "height": 720},
            ],
            media={"key": "filler", "scene": "Filler", "source": "Filler src"},
            stopped={"scene": "Off", "source": "Off frame", "color": "#102030"},
            obs={"profile": "P", "collection": "C"},
            features={"program_control": True},
        )
        collection = self.collection(config)
        self.assertEqual([s["name"] for s in collection["scene_order"]], ["Off", "Main", "Side", "Filler"])
        self.assertEqual(collection["name"], "C")
        self.assertEqual(collection["current_scene"], "Off")
        names = sources_by_name(collection)
        side = names["Side cam"]["settings"]
        self.assertFalse(side["restart_when_active"])
        self.assertTrue(side["shutdown"])
        self.assertEqual((side["width"], side["height"]), (1280, 720))
        self.assertEqual(names["Off frame"]["settings"]["color"], color_value("#102030"))
        self.assertTrue((self.tmp / "obs-config" / "basic" / "profiles" / "P" / "basic.ini").is_file())
        text = json.dumps(collection)
        self.assertNotIn("Live Feed", text, "default scene names must not leak into a custom config")

    def test_restart_when_active_defaults_follow_program_control(self):
        simple = sources_by_name(self.collection(make_config(self.tmp)))["Camera Receiver"]["settings"]
        self.assertTrue(simple["restart_when_active"])
        controlled = make_config(self.tmp, features={"program_control": True})
        self.assertFalse(sources_by_name(self.collection(controlled))["Camera Receiver"]["settings"]["restart_when_active"])
        explicit = make_config(self.tmp, features={"program_control": True}, cameras=[{"url": "http://x/", "restart_when_active": True}])
        self.assertTrue(sources_by_name(self.collection(explicit))["Camera Receiver"]["settings"]["restart_when_active"])

    def test_color_value(self):
        self.assertEqual(color_value("#000000"), 4278190080)
        self.assertEqual(color_value("#ff0000"), 0xFF0000FF)
        with self.assertRaises(ValueError):
            color_value("red")

    def test_file_url_becomes_a_local_file_browser_source(self):
        config = make_config(self.tmp, cameras=[{"url": "file:///usr/share/x/camera%20one.html"}])
        camera = sources_by_name(self.collection(config))["Camera Receiver"]["settings"]
        self.assertTrue(camera["is_local_file"])
        self.assertEqual(camera["local_file"], "/usr/share/x/camera one.html")

    def test_profile_and_output_settings(self):
        config = make_config(
            self.tmp,
            output={"url": "udp://bridge:18096?pkt_size=1316", "video_bitrate_kbps": 2500, "gop": 60, "rescale": "640x360"},
            video={"width": 1280, "height": 720, "fps": 30},
        )
        generate(config)
        ini = configparser.RawConfigParser()
        ini.optionxform = str
        ini.read(self.tmp / "obs-config" / "basic" / "profiles" / "malrunar" / "basic.ini")
        self.assertEqual(ini["AdvOut"]["FFURL"], "udp://bridge:18096?pkt_size=1316")
        self.assertEqual(ini["AdvOut"]["FFFormat"], "mpegts")
        self.assertEqual(ini["AdvOut"]["FFVBitrate"], "2500")
        self.assertIn("keyint=60", ini["AdvOut"]["FFVCustom"])
        self.assertEqual(ini["AdvOut"]["FFRescaleRes"], "640x360")
        self.assertEqual((ini["Video"]["BaseCX"], ini["Video"]["BaseCY"], ini["Video"]["FPSNum"]), ("1280", "720", "30"))

    def test_first_run_flags_and_user_preferences_survive_regeneration(self):
        config = make_config(self.tmp)
        generate(config)
        root = self.tmp / "obs-config"
        user = configparser.RawConfigParser()
        user.optionxform = str
        user.read(root / "user.ini")
        self.assertEqual(user["Basic"]["ConfigOnNewProfile"], "false")
        user["General"]["FirstRun"] = "false"
        user.add_section("BasicWindow")
        user["BasicWindow"]["PreviewEnabled"] = "false"
        with (root / "user.ini").open("w") as handle:
            user.write(handle)
        generate(config)
        user = configparser.RawConfigParser()
        user.optionxform = str
        user.read(root / "user.ini")
        self.assertEqual(user["General"]["FirstRun"], "true")
        self.assertEqual(user["BasicWindow"]["PreviewEnabled"], "false")

    def _user_ini(self):
        user = configparser.RawConfigParser()
        user.optionxform = str
        user.read(self.tmp / "obs-config" / "user.ini")
        return user

    def test_preview_option_turns_the_window_preview_off_and_keeps_other_preferences(self):
        generate(make_config(self.tmp))
        root = self.tmp / "obs-config"
        user = self._user_ini()
        self.assertFalse(user.has_option("BasicWindow", "PreviewEnabled"))  # unset: OBS decides
        user.add_section("BasicWindow")
        user["BasicWindow"]["PreviewEnabled"] = "true"
        user["BasicWindow"]["SnapDistance"] = "10"
        with (root / "user.ini").open("w") as handle:
            user.write(handle)
        generate(make_config(self.tmp, obs={"preview": False}))
        user = self._user_ini()
        self.assertEqual(user["BasicWindow"]["PreviewEnabled"], "false")
        self.assertEqual(user["BasicWindow"]["SnapDistance"], "10")
        self.assertEqual(user["General"]["FirstRun"], "true")
        generate(make_config(self.tmp, obs={"preview": True}))
        self.assertEqual(self._user_ini()["BasicWindow"]["PreviewEnabled"], "true")

    def test_websocket_password_is_generated_once_and_kept(self):
        config = make_config(self.tmp)
        generate(config)
        ws = self.tmp / "obs-config" / "plugin_config" / "obs-websocket" / "config.json"
        first = json.loads(ws.read_text())
        self.assertTrue(first["auth_required"])
        self.assertGreaterEqual(len(first["server_password"]), 20)
        self.assertEqual(stat.S_IMODE(os.stat(ws).st_mode), 0o600)
        self.assertEqual(websocket_password(config), first["server_password"])
        generate(config)
        self.assertEqual(json.loads(ws.read_text())["server_password"], first["server_password"])

    def test_websocket_password_from_config_and_auth_off(self):
        generate(make_config(self.tmp, obs={"websocket_password": "s3cret"}))
        ws = self.tmp / "obs-config" / "plugin_config" / "obs-websocket" / "config.json"
        self.assertEqual(json.loads(ws.read_text())["server_password"], "s3cret")
        off = make_config(self.tmp, obs={"websocket_auth": False})
        generate(off)
        self.assertFalse(json.loads(ws.read_text())["auth_required"])
        self.assertEqual(websocket_password(off), "")

    def test_launch_args(self):
        args = launch_args(make_config(self.tmp, output={"autostart": True}, obs={"profile": "P", "collection": "C"}))
        self.assertIn("--startrecording", args)
        self.assertEqual(args[args.index("--profile") + 1], "P")
        self.assertEqual(args[args.index("--scene") + 1], "Live Feed")
        self.assertNotIn("--startrecording", launch_args(make_config(self.tmp, output={"autostart": False})))


class PluginExtensionTests(TempDir, unittest.TestCase):
    def test_extend_collection_adds_scenes_sources_and_profile_settings(self):
        class Plugin:
            @staticmethod
            def extend_collection(builder, config):
                builder.add_source("Extra Frame", "color_source_v3", {"color": 1, "width": 10, "height": 10})
                builder.add_scene("Extra", ["Extra Frame"])
                builder.profile_ini.setdefault("AdvOut", {})["TrackIndex"] = "2"
                builder.profile_files["extra.json"] = "{}"

        config = make_config(self.tmp)
        generate(config, [Plugin])
        root = self.tmp / "obs-config"
        collection = json.loads((root / "basic" / "scenes" / "malrunar.json").read_text())
        self.assertIn({"name": "Extra"}, collection["scene_order"])
        self.assertIn("Extra Frame", sources_by_name(collection))
        profile = root / "basic" / "profiles" / "malrunar"
        self.assertEqual((profile / "extra.json").read_text(), "{}")
        ini = configparser.RawConfigParser()
        ini.optionxform = str
        ini.read(profile / "basic.ini")
        self.assertEqual(ini["AdvOut"]["TrackIndex"], "2")
        self.assertEqual(ini["AdvOut"]["FFFormat"], "mpegts")

    def test_duplicate_or_unknown_sources_are_errors(self):
        builder = build_collection(make_config(self.tmp))
        with self.assertRaises(ValueError):
            builder.add_source("Live Feed", "scene", {})
        with self.assertRaises(ValueError):
            builder.add_scene("New", ["No such source"])


if __name__ == "__main__":
    unittest.main()
