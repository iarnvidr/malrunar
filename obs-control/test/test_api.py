# SPDX-License-Identifier: Apache-2.0
"""The HTTP API against a fake obs-websocket server."""
import asyncio
import json
import shutil
import subprocess
import unittest
from datetime import datetime

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

import helpers  # noqa: F401  (puts the package on sys.path)
from helpers import TempDir, make_config
from fake_obs import FakeObs
from malrunar_obs.config import Config
from malrunar_obs.controller import Controller, ProgramHolder

# Every method + path the previous (in-product) controller served.
LEGACY_ROUTES = {
    ("GET", "/"), ("GET", "/health"), ("GET", "/ready"), ("GET", "/api/program-preview.jpg"),
    ("GET", "/api/program-live.mjpg"), ("GET", "/media/3-minute-video.mp4"), ("GET", "/api/clips"),
    ("GET", "/api/input-levels"), ("POST", "/api/clips/upload"), ("POST", "/api/clips/{clip_id}/trim"),
    ("GET", "/clips/{clip_id}.mp4"), ("DELETE", "/api/clips/{clip_id}"), ("GET", "/api/recording/status"),
    ("GET", "/api/daily-fallback/status"), ("GET", "/api/daily-fallback/preview"),
    ("POST", "/api/daily-fallback/play"), ("POST", "/api/daily-fallback/cancel"), ("POST", "/api/scene/{key}"),
    ("POST", "/api/clip/{clip_id}/scene"), ("POST", "/api/clip/{clip_id}/play"), ("POST", "/api/call/live"),
    ("POST", "/api/call/stop"), ("POST", "/api/call/microphone"), ("POST", "/api/call/clip-audio"),
    ("POST", "/api/call/video-profile"), ("POST", "/api/recording/start"), ("POST", "/api/recording/stop"),
    ("POST", "/api/daily-fallback/ensure"), ("POST", "/api/sender-heartbeat"), ("POST", "/api/restart-live"),
    ("POST", "/api/restart-output"), ("POST", "/api/stop-output"), ("POST", "/api/start-output"),
}


class ApiCase(TempDir, unittest.IsolatedAsyncioTestCase):
    scenes = ("Live Feed", "B-roll Video", "Stopped")

    async def start(self, plugins=(), obs_password="", env=None, **sections) -> TestClient:
        self.obs = await FakeObs(self.scenes, password=obs_password).start()
        self.addAsyncCleanup(self.obs.stop)
        sections.setdefault("obs", {})
        sections["obs"].update({"websocket_port": self.obs.port})
        if obs_password:
            sections["obs"]["websocket_password"] = obs_password
        config: Config = make_config(self.tmp, env=env, **sections)
        self.controller = Controller(config, list(plugins))
        self.controller.obs_client.url = self.obs.url
        client = TestClient(TestServer(self.controller.app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        await self.controller.startup_task  # the startup sequence must not race the test
        return client

    def heartbeat_now(self):
        self.controller.sender_last_seen = datetime.now().timestamp()


class RouteTests(ApiCase):
    async def test_every_legacy_route_is_served(self):
        await self.start()
        served = {
            (route.method, route.resource.canonical)
            for route in self.controller.app.router.routes()
            if route.method != "HEAD"
        }
        self.assertEqual(LEGACY_ROUTES - served, set())
        self.assertEqual(served - LEGACY_ROUTES, {("GET", "/api/scenes"), ("GET", "/api/whoami")})


class HealthTests(ApiCase):
    async def test_health_follows_the_output(self):
        client = await self.start()
        response = await client.get("/health")
        self.assertEqual(response.status, 503)
        self.assertFalse((await response.json())["healthy"])
        self.obs.recording = True
        response = await client.get("/health")
        body = await response.json()
        self.assertEqual((response.status, body["healthy"], body["scene"], body["obs"]), (200, True, "Live Feed", "32.2.0"))

    async def test_ready_does_not_depend_on_the_output(self):
        client = await self.start()
        body = await (await client.get("/ready")).json()
        self.assertEqual(body, {"ready": True, "obsVersion": "32.2.0", "rpcVersion": 1})

    async def test_ready_in_program_control_reports_gates_and_profiles(self):
        client = await self.start(features={"program_control": True, "video_profiles": True, "mode_label": "studio"})
        body = await (await client.get("/ready")).json()
        self.assertEqual(body["mode"], "studio")
        self.assertEqual(body["video_profile"], "360p")
        self.assertEqual(body["video_profiles"], ["360p", "540p", "720p"])
        self.assertEqual(body["program_scene"], "Stopped", "program control starts on the stopped scene")

    async def test_obs_down_is_a_503_not_a_crash(self):
        client = await self.start()
        await self.obs.stop()
        ready = await client.get("/ready")
        self.assertEqual(ready.status, 503)
        self.assertFalse((await ready.json())["ready"])
        switch = await client.post("/api/scene/live")
        self.assertEqual(switch.status, 503)
        self.assertFalse((await switch.json())["ok"])

    async def test_ready_extension_from_a_plugin(self):
        client = await self.start()
        self.controller.add_ready_info(lambda: {"extra": 1})
        self.assertEqual((await (await client.get("/ready")).json())["extra"], 1)

    async def test_scenes_listing(self):
        client = await self.start()
        body = await (await client.get("/api/scenes")).json()
        self.assertEqual(body["scenes"], ["Live Feed", "B-roll Video", "Stopped"])
        self.assertEqual(body["program"], "Live Feed")
        self.assertEqual(body["keys"], {"live": "Live Feed", "video": "B-roll Video"})


class AuthTests(ApiCase):
    async def test_controller_authenticates_to_obs_websocket(self):
        client = await self.start(obs_password="hunter2")
        self.assertEqual((await client.get("/ready")).status, 200)

    async def test_wrong_password_is_reported(self):
        client = await self.start(obs_password="hunter2")
        self.controller.obs_client._password = "wrong"
        response = await client.get("/ready")
        self.assertEqual(response.status, 503)

    async def test_missing_password_is_reported(self):
        client = await self.start(obs_password="hunter2")
        self.controller.obs_client._password = ""
        response = await client.get("/ready")
        self.assertEqual(response.status, 503)
        self.assertIn("password", (await response.json())["error"])


class DefaultModeTests(ApiCase):
    async def test_live_scene_switch(self):
        client = await self.start()
        self.obs.current = "B-roll Video"
        response = await client.post("/api/scene/live")
        self.assertEqual((response.status, await response.json()), (200, {"ok": True, "scene": "Live Feed"}))
        self.assertEqual(self.obs.current, "Live Feed")

    async def test_video_scene_needs_todays_broll(self):
        client = await self.start()
        response = await client.post("/api/scene/video")
        self.assertEqual(response.status, 409)
        self.assertFalse((await response.json())["ok"])
        path = self.controller.broll.path()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"video")
        response = await client.post("/api/scene/video")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.obs.current, "B-roll Video")
        settings = self.obs.names("SetInputSettings")[-1]
        self.assertEqual(settings["inputName"], "B-roll Source")
        self.assertEqual(settings["inputSettings"]["local_file"], str(path))

    async def test_unknown_scene_key_is_404(self):
        client = await self.start()
        self.assertEqual((await client.post("/api/scene/nothing")).status, 404)

    async def test_program_control_routes_are_404(self):
        client = await self.start()
        for method, path in [
            ("post", "/api/call/live"), ("post", "/api/call/stop"), ("post", "/api/call/microphone"),
            ("post", "/api/call/clip-audio"), ("post", "/api/call/video-profile"), ("get", "/api/input-levels"),
            ("delete", "/api/clips/abc"), ("post", "/api/clip/abc/play"),
        ]:
            response = await getattr(client, method)(path)
            self.assertEqual(response.status, 404, path)

    async def test_output_controls(self):
        client = await self.start()
        self.assertEqual((await client.post("/api/start-output")).status, 200)
        self.assertTrue(self.obs.recording)
        self.assertEqual((await client.post("/api/start-output")).status, 200)
        self.assertEqual(len(self.obs.names("StartRecord")), 1)
        self.assertEqual((await client.post("/api/stop-output")).status, 200)
        self.assertFalse(self.obs.recording)
        self.assertEqual((await client.post("/api/restart-output")).status, 200)
        self.assertTrue(self.obs.recording)

    async def test_restart_live_presses_the_refresh_button_then_falls_back_to_the_url(self):
        client = await self.start()
        await client.post("/api/restart-live")
        self.assertEqual(
            self.obs.names("PressInputPropertiesButton")[-1],
            {"inputName": "Camera Receiver", "propertyName": "refreshnocache"},
        )
        self.obs.fail["PressInputPropertiesButton"] = "no such property"
        self.assertEqual((await client.post("/api/restart-live")).status, 200)
        self.assertEqual(self.obs.names("SetInputSettings")[-1]["inputSettings"]["url"], self.controller.config.cameras[0].url)

    async def test_preview_jpeg(self):
        client = await self.start()
        response = await client.get("/api/program-preview.jpg")
        body = await response.read()
        self.assertEqual((response.status, response.content_type), (200, "image/jpeg"))
        self.assertTrue(body.startswith(b"\xff\xd8"))
        self.assertEqual(response.headers["X-OBS-Scene"], "Live Feed")
        self.assertEqual(self.obs.names("GetSourceScreenshot")[-1]["sourceName"], "Live Feed")

    async def test_mjpeg_stream_delivers_frames(self):
        client = await self.start()
        response = await client.get("/api/program-live.mjpg")
        self.assertEqual(response.status, 200)
        chunk = await asyncio.wait_for(response.content.read(200), 5)
        self.assertIn(b"--frame", chunk)
        response.close()

    async def test_console_page(self):
        client = await self.start(console={"title": "My <b>desk</b>", "sender_url": "https://send.example.test/?a=1&b=2"})
        text = await (await client.get("/")).text()
        self.assertIn("My &lt;b&gt;desk&lt;/b&gt;", text)
        self.assertIn("https://send.example.test/?a=1&amp;b=2", text)
        self.assertNotIn("__", text.replace("__proto__", ""))

    async def test_console_can_be_disabled(self):
        client = await self.start(console={"enabled": False})
        self.assertEqual((await client.get("/")).status, 404)

    async def test_heartbeat_starts_broll_and_reports_state(self):
        client = await self.start(broll={"auto": False})
        response = await client.post("/api/sender-heartbeat", json={})
        body = await response.json()
        self.assertEqual((response.status, body["state"], body["ready"]), (200, "idle", False))
        self.assertTrue(self.controller.sender_alive())


class ClipTests(ApiCase):
    def make_clip(self, clip_id="clip-1", name="First clip"):
        directory = self.controller.recordings_dir
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{clip_id}.mp4").write_bytes(b"not really a video")
        (directory / f"{clip_id}.json").write_text(json.dumps({"display_name": name, "created_at": "2026-01-01T00:00:00+00:00"}))

    async def test_list_media_and_default_scene_name(self):
        client = await self.start()
        self.make_clip()
        body = await (await client.get("/api/clips")).json()
        clip = body["clips"][0]
        self.assertEqual((clip["id"], clip["display_name"], clip["scene_name"]), ("clip-1", "First clip", "Recorded · First clip"))
        self.assertEqual(clip["url"], "clips/clip-1.mp4")
        media = await client.get("/clips/clip-1.mp4")
        self.assertEqual(await media.read(), b"not really a video")
        self.assertEqual((await client.get("/clips/..%2Fx.mp4")).status, 404)
        self.assertEqual((await client.get("/clips/missing.mp4")).status, 404)

    async def test_clip_scene_switch_creates_the_scene_once(self):
        client = await self.start()
        self.make_clip()
        response = await client.post("/api/clip/clip-1/scene")
        self.assertEqual((await response.json()), {"ok": True, "scene": "Recorded · First clip"})
        self.assertEqual(self.obs.current, "Recorded · First clip")
        await client.post("/api/clip/clip-1/scene")
        self.assertEqual(len(self.obs.names("CreateScene")), 1)
        created = self.obs.names("CreateInput")[0]
        self.assertEqual(created["inputName"], "Recorded Source · clip-1")
        self.assertTrue(created["inputSettings"]["looping"])
        self.assertEqual((await client.post("/api/clip/nope/scene")).status, 404)

    async def test_clip_scene_names_use_the_configured_prefixes(self):
        client = await self.start(clips={"scene_prefix": "Clip", "source_prefix": "Clip src"})
        self.make_clip()
        await client.post("/api/clip/clip-1/scene")
        self.assertEqual(self.obs.current, "Clip · First clip")
        self.assertEqual(self.obs.names("CreateInput")[0]["inputName"], "Clip src · clip-1")

    async def test_restoring_scenes_on_startup(self):
        await self.start()
        self.make_clip()
        await self.controller.clips.restore_scenes()
        self.assertIn("Recorded · First clip", self.obs.scenes)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
    async def test_upload_trim_and_delete(self):
        client = await self.start(features={"program_control": True})
        video = self.tmp / "in.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10", "-f", "lavfi",
             "-i", "sine=frequency=440", "-t", "2", "-pix_fmt", "yuv420p", "-shortest", str(video)],
            check=True,
        )
        form = FormData()
        form.add_field("name", "Uploaded  one")
        form.add_field("file", video.read_bytes(), filename="in.mp4", content_type="video/mp4")
        response = await client.post("/api/clips/upload", data=form)
        body = await response.json()
        self.assertEqual(response.status, 201, body)
        clip = body["clip"]
        self.assertEqual(clip["display_name"], "Uploaded one")
        self.assertIn(clip["scene_name"], self.obs.scenes)
        # trim
        response = await client.post(f"/api/clips/{clip['id']}/trim", json={"start_seconds": 0.2, "end_seconds": 1.2, "name": "Cut"})
        trimmed = (await response.json())["clip"]
        self.assertEqual((response.status, trimmed["display_name"]), (201, "Cut"))
        bad = await client.post(f"/api/clips/{clip['id']}/trim", json={"start_seconds": 1, "end_seconds": 99})
        self.assertEqual(bad.status, 400)
        # play, then delete is refused while on air
        response = await client.post(f"/api/clip/{clip['id']}/play")
        self.assertEqual(response.status, 200, await response.text())
        self.assertAlmostEqual((await response.json())["duration"], 2, delta=0.5)
        self.assertEqual(self.obs.current, clip["scene_name"])
        refused = await client.delete(f"/api/clips/{clip['id']}")
        self.assertEqual(refused.status, 409)
        self.controller.clip_task.cancel()
        self.controller.active_clip_input = None
        self.obs.current = "Stopped"
        deleted = await client.delete(f"/api/clips/{clip['id']}")
        self.assertEqual((deleted.status, (await deleted.json())["deleted"]), (200, clip["id"]))
        self.assertNotIn(clip["scene_name"], self.obs.scenes)
        self.assertFalse((self.controller.recordings_dir / f"{clip['id']}.mp4").exists())

    @unittest.skipUnless(shutil.which("ffprobe"), "needs ffprobe")
    async def test_upload_rejects_a_non_video(self):
        client = await self.start()
        form = FormData()
        form.add_field("file", b"plain text", filename="a.mp4", content_type="video/mp4")
        response = await client.post("/api/clips/upload", data=form)
        self.assertEqual(response.status, 400)
        self.assertEqual(list(self.controller.recordings_dir.glob("*")), [])


class ProgramControlTests(ApiCase):
    async def start(self, **sections):
        sections.setdefault("features", {})
        sections["features"].setdefault("program_control", True)
        client = await super().start(**sections)
        self.obs.inputs["Camera Receiver"] = {}
        self.obs.current = "Stopped"
        return client

    async def test_live_needs_a_sender(self):
        client = await self.start()
        response = await client.post("/api/call/live")
        self.assertEqual(response.status, 409)
        self.assertEqual(self.obs.current, "Stopped")
        await client.post("/api/sender-heartbeat", json={"healthy": True})
        response = await client.post("/api/call/live")
        self.assertEqual((response.status, await response.json()), (200, {"ok": True, "scene": "Live Feed"}))
        self.assertEqual(self.obs.current, "Live Feed")

    async def test_scene_key_goes_through_the_same_checks(self):
        client = await self.start()
        self.assertEqual((await client.post("/api/scene/live")).status, 409)
        self.assertEqual((await client.post("/api/scene/video")).status, 404)
        await client.post("/api/sender-heartbeat", json={"healthy": True})
        self.assertEqual((await client.post("/api/scene/live")).status, 200)

    async def test_stop_switches_to_the_stopped_scene_and_mutes(self):
        client = await self.start()
        await client.post("/api/sender-heartbeat", json={"healthy": True})
        await client.post("/api/call/live")
        response = await client.post("/api/call/stop")
        self.assertEqual(await response.json(), {"ok": True, "scene": "Stopped", "audio_muted": True})
        self.assertEqual(self.obs.current, "Stopped")
        self.assertTrue(self.obs.muted["Camera Receiver"])

    async def test_stopped_scene_is_created_when_missing(self):
        self.scenes = ("Live Feed", "B-roll Video")
        client = await self.start()
        self.obs.current = "Live Feed"
        await client.post("/api/call/stop")
        self.assertIn("Stopped", self.obs.scenes)
        self.assertEqual(self.obs.names("CreateInput")[0]["inputName"], "Stopped Frame")

    async def test_microphone_gate_and_camera_unmuted_only_when_live(self):
        client = await self.start()
        await client.post("/api/sender-heartbeat", json={"healthy": True})
        await client.post("/api/call/live")
        response = await client.post("/api/call/microphone", json={"muted": True})
        self.assertEqual(await response.json(), {"ok": True, "muted": True})
        self.assertTrue(self.obs.muted["Camera Receiver"])
        await client.post("/api/call/microphone", json={"muted": False})
        self.assertFalse(self.obs.muted["Camera Receiver"])
        self.obs.current = "B-roll Video"  # an unselected camera stays silent
        await client.post("/api/call/microphone", json={"muted": False})
        self.assertTrue(self.obs.muted["Camera Receiver"])

    async def test_clip_audio_gate(self):
        client = await self.start()
        self.obs.inputs["Recorded Source · a"] = {}
        self.controller.active_clip_input = "Recorded Source · a"
        await client.post("/api/call/clip-audio", json={"muted": True})
        self.assertTrue(self.obs.muted["Recorded Source · a"])
        await client.post("/api/call/clip-audio", json={"muted": False})
        self.assertFalse(self.obs.muted["Recorded Source · a"])

    async def test_stale_sender_stops_the_live_program(self):
        client = await self.start(sender={"lease_seconds": 0.5})
        await client.post("/api/sender-heartbeat", json={"healthy": True})
        await client.post("/api/call/live")
        self.assertEqual(self.obs.current, "Live Feed")
        await asyncio.sleep(0.6)
        await self.controller._stop_stale_live_program()
        self.assertEqual(self.obs.current, "Stopped")

    async def test_lease_can_be_turned_off(self):
        client = await self.start(features={"program_control": True, "sender_lease": False})
        self.assertEqual((await client.post("/api/call/live")).status, 200)

    async def test_video_profile(self):
        client = await self.start(
            features={"program_control": True, "video_profiles": True},
            profiles={
                "360p": {"width": 640, "height": 360, "encoder_kbps": 1200},
                "540p": {"width": 960, "height": 540, "encoder_kbps": 2000,
                         "camera_query": {"scale": "75"}, "camera_query_drop": ["maxwidth"]},
            },
            cameras=[{"url": "https://camera.example.test/view?room=demo&maxwidth=640&scale=50"}],
        )
        self.obs.recording = True
        response = await client.post("/api/call/video-profile", json={"profile": "540p"})
        body = await response.json()
        self.assertEqual((response.status, body["profile"], body["width"], body["encoder_kbps"]), (200, "540p", 960, 2000))
        self.assertEqual(self.obs.names("SetVideoSettings")[-1], {"baseWidth": 960, "baseHeight": 540, "outputWidth": 960, "outputHeight": 540})
        self.assertEqual(self.obs.profile_parameters["FFVBitrate"], "2000")
        self.assertEqual(self.obs.profile_parameters["FFRescaleRes"], "960x540")
        camera = [d for d in self.obs.names("SetInputSettings") if d["inputName"] == "Camera Receiver"][-1]
        self.assertEqual(camera["inputSettings"]["url"], "https://camera.example.test/view?room=demo&scale=75")
        self.assertTrue(self.obs.recording, "the output is restarted after the change")
        self.assertEqual(json.loads((self.controller.state_dir / "video-profile.json").read_text()), {"profile": "540p"})
        self.assertEqual(self.controller.saved_video_profile(), "540p")
        ready = await (await client.get("/ready")).json()
        self.assertEqual(ready["video_profile"], "540p")
        bad = await client.post("/api/call/video-profile", json={"profile": "4k"})
        self.assertEqual(bad.status, 400)
        # a plain profile undoes the previous profile's query
        await client.post("/api/call/video-profile", json={"profile": "360p"})
        camera = [d for d in self.obs.names("SetInputSettings") if d["inputName"] == "Camera Receiver"][-1]
        self.assertEqual(camera["inputSettings"]["url"], "https://camera.example.test/view?room=demo&maxwidth=640&scale=50")

    async def test_profiles_without_query_settings_leave_the_camera_url_alone(self):
        client = await self.start(features={"program_control": True, "video_profiles": True})
        await client.post("/api/call/video-profile", json={"profile": "540p"})
        camera = [d for d in self.obs.names("SetInputSettings") if d["inputName"] == "Camera Receiver"][-1]
        self.assertEqual(camera["inputSettings"], {"width": 960, "height": 540})

    async def test_video_profile_route_needs_the_feature(self):
        client = await self.start()
        self.assertEqual((await client.post("/api/call/video-profile", json={"profile": "360p"})).status, 404)

    async def test_input_levels(self):
        client = await self.start()
        self.controller.levels.record(
            [{"inputName": "Camera Receiver", "inputLevelsMul": [[0.0, 0.5, 0.1], [0.0, 0.25, 0.5]]}], __import__("time").monotonic()
        )
        body = await (await client.get("/api/input-levels")).json()
        self.assertEqual(body["live_input"], {"peak_db": -6.0})
        self.assertIsNone(body["clip_input"])
        self.assertFalse(body["clip_on_air"])

    async def test_play_clip_blocks_then_returns_to_live(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("needs ffmpeg")
        client = await self.start()
        video = self.tmp / "c.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10", "-t", "0.4",
                        "-pix_fmt", "yuv420p", str(video)], check=True)
        directory = self.controller.recordings_dir
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copy(video, directory / "c.mp4")
        await client.post("/api/sender-heartbeat", json={"healthy": True})
        response = await client.post("/api/clip/c/play", json={})
        self.assertEqual(response.status, 200)
        self.assertEqual(self.obs.current, "Recorded · c")
        self.assertEqual(self.controller.active_clip_input, "Recorded Source · c")
        for _ in range(40):
            await asyncio.sleep(0.1)
            if self.obs.current == "Live Feed":
                break
        self.assertEqual(self.obs.current, "Live Feed")
        self.assertIsNone(self.controller.active_clip_input)


class ProgramHolderTests(ApiCase):
    async def start(self, **sections):
        sections.setdefault("features", {"program_control": True})
        client = await super().start(**sections)
        self.obs.current = "Stopped"
        self.held = True
        self.released = []

        async def release(neutral):
            self.held = False
            self.released.append(neutral)

        self.controller.add_program_holder(
            ProgramHolder("demo", lambda: self.held, release, message="The demo plugin has the program")
        )
        await client.post("/api/sender-heartbeat", json={"healthy": True})
        return client

    async def test_holder_blocks_live_and_clip_play_and_profile_changes(self):
        client = await self.start(features={"program_control": True, "video_profiles": True})
        response = await client.post("/api/call/live")
        self.assertEqual((response.status, (await response.json())["error"]), (409, "The demo plugin has the program"))
        self.assertEqual((await client.post("/api/clip/x/play")).status, 404)
        profile = await client.post("/api/call/video-profile", json={"profile": "540p"})
        self.assertEqual((profile.status, (await profile.json())["error"]), (409, "The demo plugin has the program"))

    async def test_stop_always_wins_and_releases_the_holder(self):
        client = await self.start()
        response = await client.post("/api/call/stop")
        self.assertEqual(response.status, 200)
        self.assertEqual(self.released, [True])
        self.assertEqual(self.obs.current, "Stopped")
        self.assertEqual((await client.post("/api/call/live")).status, 200)

    async def test_extra_live_scenes_keep_the_camera_audible(self):
        client = await self.start()
        self.held = False
        self.obs.inputs["Camera Receiver"] = {}
        self.obs.scenes.append("Plugin Scene")
        self.obs.current = "Plugin Scene"
        await client.post("/api/call/microphone", json={"muted": False})
        self.assertTrue(self.obs.muted["Camera Receiver"])
        self.controller.extra_live_scenes.add("Plugin Scene")
        await client.post("/api/call/microphone", json={"muted": False})
        self.assertFalse(self.obs.muted["Camera Receiver"])


class AccessTests(ApiCase):
    ACCESS = {"identity_header": "X-Forwarded-User", "trusted_proxies": ["127.0.0.0/8"]}

    async def test_no_request_is_rejected_for_its_identity(self):
        client = await self.start(access=self.ACCESS)
        self.assertEqual((await client.get("/api/clips", headers={"X-Forwarded-User": "mallory"})).status, 200)
        self.assertEqual((await client.get("/api/clips")).status, 403, "trust is configured: anonymous is refused")

    async def test_per_identity_broll_uses_the_trusted_header(self):
        client = await self.start(access=self.ACCESS, features={"program_control": True})
        self.assertEqual((await client.get("/api/daily-fallback/status")).status, 403)
        self.assertEqual((await client.get("/api/daily-fallback/status?user=ann")).status, 403)
        ok = await client.get("/api/daily-fallback/status", headers={"X-Forwarded-User": "ann"})
        self.assertEqual((ok.status, (await ok.json())["state"]), (200, "idle"))
        path = self.controller.broll.path(user="bob")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
        bob = await client.get("/api/daily-fallback/status", headers={"X-Forwarded-User": "bob"})
        self.assertTrue((await bob.json())["ready"])
        ann = await client.get("/api/daily-fallback/status", headers={"X-Forwarded-User": "ann"})
        self.assertFalse((await ann.json())["ready"])
        eve = await client.get("/api/daily-fallback/status", headers={"X-Forwarded-User": "eve"})
        self.assertEqual(eve.status, 200, "any identity the proxy passes gets its own b-roll")

    async def test_identity_cannot_escape_the_broll_directory(self):
        client = await self.start(access=self.ACCESS, features={"program_control": True})
        for name in ("..", "../x", "a/b"):
            path = self.controller.broll.path(user=name)
            self.assertEqual(path.parent.parent, self.controller.broll.directory)
            response = await client.get("/api/daily-fallback/status", headers={"X-Forwarded-User": name})
            self.assertEqual(response.status, 200)

    async def test_heartbeat_user_comes_from_the_header(self):
        client = await self.start(access=self.ACCESS, features={"program_control": True})
        await client.post("/api/sender-heartbeat", json={"healthy": True, "user": "mallory"}, headers={"X-Forwarded-User": "ann"})
        self.assertEqual(self.controller.sender_user, "ann")

    async def test_heartbeat_body_user_is_ignored_without_a_header(self):
        client = await self.start(features={"program_control": True})
        await client.post("/api/sender-heartbeat", json={"healthy": True, "user": "ann"})
        self.assertIsNone(self.controller.sender_user)

    async def test_simple_mode_has_no_per_identity_broll(self):
        client = await self.start(access=self.ACCESS)
        self.assertEqual((await client.get("/api/daily-fallback/status", headers={"X-Forwarded-User": "eve"})).status, 200)
        self.assertEqual((await client.get("/api/daily-fallback/status", headers={"X-Forwarded-User": "eve"})).status, 200)
        self.assertEqual(self.controller.broll.path(), self.controller.broll.path(user=None))

    async def test_no_header_means_no_identity(self):
        client = await self.start(features={"program_control": True})
        self.assertEqual((await client.get("/api/daily-fallback/status")).status, 200)

    async def test_ensure_in_program_control_needs_the_live_scene(self):
        client = await self.start(features={"program_control": True}, access=self.ACCESS)
        response = await client.post("/api/daily-fallback/ensure", headers={"X-Forwarded-User": "ann"})
        self.assertEqual(response.status, 409)
        self.assertIn("select Live", (await response.json())["error"])

    async def test_preview_url_is_configurable(self):
        client = await self.start(broll={"preview_url": "api/obs/daily-fallback/preview"})
        path = self.controller.broll.path()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")
        self.assertEqual((await (await client.get("/api/daily-fallback/status")).json())["url"], "api/obs/daily-fallback/preview")

    async def test_broll_can_be_disabled(self):
        client = await self.start(broll={"enabled": False})
        body = await (await client.get("/api/daily-fallback/status")).json()
        self.assertEqual((body["enabled"], body["state"]), (False, "disabled"))
        self.assertEqual((await client.post("/api/daily-fallback/ensure")).status, 409)


if __name__ == "__main__":
    unittest.main()
