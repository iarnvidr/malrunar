# SPDX-License-Identifier: Apache-2.0
"""The built-in takes plugin: off by default, classification, per-user state, uploads, the take workflow."""
import asyncio
import dataclasses
import io
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import textwrap
import unittest
import zlib
from unittest import mock

from aiohttp import FormData

import helpers
from helpers import CAMERA_URL, make_config
from malrunar_obs import takes as takes_plugin
from malrunar_obs.config import ConfigError
from malrunar_obs.plugins import load_plugins
from malrunar_obs.scenes import generate
from test_api import ApiCase

DOC = helpers.ROOT.parent / "docs" / "obs-control-api.md"
SCENE = "Take Studio"
ITEMS = [{"sourceName": "Take Slide", "sceneItemId": 1}, {"sourceName": "Take Camera", "sceneItemId": 2}]
ANN = {"Remote-User": "ann"}
BOB = {"Remote-User": "bob"}
TRUST = {"trusted_proxies": ["127.0.0.0/8"]}


def png_bytes(width=2, height=2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\0" + b"\xc8\xc8\xc8" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64


def pdf_bytes(pages: int) -> bytes:
    objects = ["<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>")
    objects += ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 180] >>"] * pages
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def upload_form(data: bytes, name="Deck", filename="slides.bin") -> FormData:
    form = FormData()
    form.add_field("name", name)
    form.add_field("file", io.BytesIO(data), filename=filename, content_type="application/octet-stream")
    return form


STUB = textwrap.dedent(
    '''\
    #!{python}
    import json, sys, time
    name, args = sys.argv[0].rsplit("/", 1)[-1], sys.argv[1:]
    if name == "ffprobe":
        print(json.dumps({{"format": {{"duration": "8.0"}}, "streams": [
            {{"codec_type": "video", "width": 1920, "height": 1080}}, {{"codec_type": "audio"}}]}}))
    elif "-listen" in args:  # the take listener: lives a little, then leaves its recording behind
        time.sleep(1.2)
        open(args[-1], "wb").write(b"mkv")
    elif "-filter_complex" in args:  # the assembly render
        print("out_time_us=1000000", flush=True)
        open(args[-1], "wb").write(b"assembled mp4")
    else:  # the remux
        open(args[-1], "wb").write(open(args[args.index("-i") + 1], "rb").read())
    '''
)


class TakesCase(ApiCase):
    scenes = ("Live Feed", "B-roll Video", "Stopped", SCENE)
    plugin_settings: dict = {}

    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for tool in ("ffmpeg", "ffprobe"):
            path = bin_dir / tool
            path.write_text(STUB.format(python=sys.executable))
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def start_takes(self, settings=None, **sections):
        sections.setdefault("plugin", {"takes": {**self.plugin_settings, **(settings or {})}})
        client = await self.start(plugins=[takes_plugin], **sections)
        self.obs.scene_items = {SCENE: ITEMS}
        self.takes = self.controller.takes
        return client

    @property
    def data_dir(self):
        return self.tmp / "recordings" / "takes"

    async def upload(self, client, data, headers=None, **kwargs):
        return await client.post("/api/takes/slides", data=upload_form(data, **kwargs), headers=headers)

    async def deck(self, client, data=None, headers=None) -> str:
        response = await self.upload(client, data or png_bytes(), headers)
        self.assertEqual(response.status, 201, await response.text())
        return (await response.json())["slides"]["id"]

    async def record(self, client, take_id="first-take", headers=None):
        started = await client.post("/api/takes/start", json={"take_id": take_id}, headers=headers)
        self.assertEqual(started.status, 200, await started.text())
        stopped = await client.post("/api/takes/stop", headers=headers)
        self.assertEqual(stopped.status, 200, await stopped.text())
        return (await stopped.json())["take"]

    async def render(self, client, items, headers=None):
        response = await client.post("/api/takes/assemble", json={"items": items, "name": "Intro"}, headers=headers)
        self.assertEqual(response.status, 202, await response.text())
        job_id = (await response.json())["job"]["job_id"]
        for _ in range(100):
            job = (await (await client.get(f"/api/takes/jobs/{job_id}", headers=headers)).json())["job"]
            if job["state"] != "running":
                return job
            await asyncio.sleep(0.1)
        self.fail("the assembly did not finish")


class DisabledByDefaultTests(ApiCase):
    async def test_the_routes_are_absent_unless_the_plugin_is_enabled(self):
        client = await self.start()
        for method, path in (("get", "/api/takes/status"), ("post", "/api/takes/arm"), ("get", "/api/takes/library")):
            self.assertEqual((await getattr(client, method)(path)).status, 404, path)
        self.assertFalse(hasattr(self.controller, "takes"))
        self.assertNotIn("takes", await (await client.get("/ready")).json())

    def test_nothing_is_loaded_and_no_scene_is_generated_by_default(self):
        config = make_config(self.tmp)
        self.assertEqual(load_plugins(config), [])
        generate(config, load_plugins(config))
        text = (self.tmp / "obs-config/basic/scenes/malrunar.json").read_text()
        self.assertNotIn(SCENE, text)
        self.assertNotIn("TrackIndex", (self.tmp / "obs-config/basic/profiles/malrunar/basic.ini").read_text())

    def test_the_feature_flag_and_the_module_name_enable_it_once(self):
        flag = make_config(self.tmp, features={"takes": True})
        self.assertEqual(load_plugins(flag), [takes_plugin])
        both = make_config(self.tmp, features={"takes": True}, plugins={"modules": ["malrunar_obs.takes"]})
        self.assertEqual(load_plugins(both), [takes_plugin])
        named = make_config(self.tmp, plugins={"modules": ["malrunar_obs.takes"]})
        self.assertEqual(load_plugins(named), [takes_plugin])
        self.assertEqual(load_plugins(make_config(self.tmp, env={"MALRUNAR_OBS_FEATURES_TAKES": "true"})), [takes_plugin])


class SettingsAndSceneTests(helpers.TempDir, unittest.TestCase):
    def collection(self, **sections):
        config = make_config(self.tmp, plugins={"modules": ["malrunar_obs.takes"]}, **sections)
        generate(config, load_plugins(config))
        scenes = json.loads((self.tmp / "obs-config/basic/scenes/malrunar.json").read_text())
        profile = self.tmp / "obs-config/basic/profiles/malrunar"
        return scenes, profile

    def test_generated_scene_camera_audio_track_and_stream_profile(self):
        scenes, profile = self.collection()
        sources = {source["name"]: source for source in scenes["sources"]}
        self.assertIn({"name": SCENE}, scenes["scene_order"])
        self.assertEqual([item["name"] for item in sources[SCENE]["settings"]["items"]], ["Take Slide", "Take Camera"])
        camera = sources["Take Camera"]
        self.assertEqual(camera["mixers"], 2, "the take camera feeds audio track 2 only")
        self.assertEqual(camera["settings"]["url"], CAMERA_URL, "the first camera's URL by default")
        self.assertEqual((camera["settings"]["width"], camera["settings"]["height"]), (1920, 1080))
        self.assertTrue(camera["settings"]["shutdown"])
        self.assertEqual(sources["Take Slide"]["id"], "image_source")
        ini = (profile / "basic.ini").read_text()
        self.assertIn("TrackIndex=2", ini)
        self.assertIn("FFRescaleRes=640x360", ini, "the program output keeps the configured size")
        self.assertEqual(json.loads((profile / "service.json").read_text())["type"], "rtmp_custom")
        self.assertEqual(json.loads((profile / "streamEncoder.json").read_text())["rate_control"], "CRF")

    def test_own_camera_url_names_and_an_explicit_output_rescale(self):
        scenes, profile = self.collection(
            plugin={"takes": {"camera_url": "file:///srv/pages/take.html", "scene": "Prep", "width": 1280, "height": 720}},
            output={"rescale": "960x540"},
        )
        sources = {source["name"]: source for source in scenes["sources"]}
        self.assertIn("Prep", sources)
        self.assertEqual(sources["Take Camera"]["settings"]["local_file"], "/srv/pages/take.html")
        ini = (profile / "basic.ini").read_text()
        self.assertIn("FFRescaleRes=960x540", ini)

    def test_invalid_settings_name_the_setting(self):
        cases = {
            "unknown setting": ({"nope": 1}, "unknown setting"),
            "wrong type": ({"width": "wide"}, "width: expected an integer"),
            "bool is not a number": ({"max_upload_mb": True}, "max_upload_mb"),
            "bool setting": ({"check_camera": "yes"}, "check_camera: expected true or false"),
            "odd size": ({"width": 1921}, "even"),
            "range": ({"max_pdf_pages": 0}, "max_pdf_pages must be between"),
            "same names": ({"slide_source": "Take Camera"}, "three different names"),
            "both urls": ({"camera_url": "a", "camera_url_file": "b"}, "not both"),
            "empty name": ({"scene": " "}, "scene must not be empty"),
        }
        for label, (settings, message) in cases.items():
            with self.subTest(label):
                config = make_config(self.tmp, plugin={"takes": settings})
                with self.assertRaisesRegex(ConfigError, message):
                    takes_plugin.settings_from(config)

    def test_a_scene_name_clash_is_reported_when_generating(self):
        config = make_config(self.tmp, plugin={"takes": {"scene": "Stopped"}}, plugins={"modules": ["malrunar_obs.takes"]})
        with self.assertRaisesRegex(ValueError, "already exists"):
            generate(config, load_plugins(config))


class ClassificationTests(TakesCase):
    async def test_every_takes_route_is_registered_and_classified_operator(self):
        client = await self.start_takes()
        self.assertEqual(self.controller.unclassified_routes(), [])
        routes = self.takes.routes()
        self.assertGreaterEqual(len(routes), 15)
        for method, path, _ in routes:
            self.assertEqual(self.controller.route_capabilities[(method, path)], "operator", (method, path))
        served = {(r.method, r.resource.canonical) for r in self.controller.app.router.routes()}
        self.assertTrue({(method, path) for method, path, _ in routes} <= served)

    async def test_viewers_are_refused_and_operators_allowed(self):
        client = await self.start_takes(access={**TRUST, "groups": {"operator": ["ops"], "viewer": ["watch"]}})
        viewer = {"Remote-User": "vic", "Remote-Groups": "watch"}
        operator = {"Remote-User": "ann", "Remote-Groups": "ops"}
        for method, path in (("get", "/api/takes/status"), ("get", "/api/takes/library"), ("post", "/api/takes/arm"),
                             ("get", "/api/takes/abc-123.mp4"), ("get", "/api/takes/abc-123/audio.mp3"), ("delete", "/api/takes/abc-123"),
                             ("get", "/api/takes/slides/deck-abc/1"), ("post", "/api/takes/slides")):
            with self.subTest(path):
                self.assertEqual((await getattr(client, method)(path, headers=viewer)).status, 403)
                self.assertEqual((await getattr(client, method)(path)).status, 403, "anonymous")
        self.assertEqual((await client.get("/api/takes/status", headers=operator)).status, 200)
        self.assertEqual((await client.get("/api/takes/abc-123.mp4", headers=operator)).status, 404)
        self.assertEqual((await client.get("/api/takes/abc-123/audio.mp3", headers=operator)).status, 404)


class ArmingTests(TakesCase):
    async def test_canvas_change_waits_for_program_encoder_stop(self):
        client = await self.start_takes()
        self.obs.recording = True
        self.obs.delayed_record_stop = True
        response = await client.post("/api/takes/arm", json={})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.obs.recording_at_canvas_changes, [False])
        response = await client.post("/api/takes/disarm")
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.obs.recording_at_canvas_changes, [False, False])

    async def test_failed_encoder_stop_does_not_change_canvas(self):
        client = await self.start_takes()
        self.obs.recording = True
        self.obs.fail["StopRecord"] = "encoder failed"
        response = await client.post("/api/takes/arm", json={})
        self.assertEqual(response.status, 503)
        self.assertEqual(self.obs.recording_at_canvas_changes, [])

    async def test_failed_disarm_keeps_take_mode_reserved(self):
        client = await self.start_takes()
        await client.post("/api/takes/arm", json={})
        self.obs.fail["StopRecord"] = "encoder failed"
        response = await client.post("/api/takes/disarm")
        self.assertEqual(response.status, 503)
        self.assertTrue(self.takes.armed)
        self.assertEqual(self.obs.video["baseWidth"], 1920)
        self.obs.fail.clear()
        response = await client.post("/api/takes/disarm")
        self.assertEqual(response.status, 200, await response.text())
        self.assertFalse(self.takes.armed)

    async def test_arm_switches_canvas_and_scene_and_disarm_restores_them(self):
        client = await self.start_takes()
        self.obs.current = "B-roll Video"
        armed = await (await client.post("/api/takes/arm", json={"layout": "camera"})).json()
        self.assertTrue(armed["armed"])
        self.assertEqual(armed["layout"], "camera")
        self.assertEqual((self.obs.current, self.obs.video["baseWidth"], self.obs.video["baseHeight"]), (SCENE, 1920, 1080))
        self.assertTrue(self.obs.recording, "the program output is running again after the canvas change")
        self.assertTrue((await (await client.get("/ready")).json())["takes"]["armed"])
        disarmed = await (await client.post("/api/takes/disarm")).json()
        self.assertFalse(disarmed["armed"])
        self.assertEqual((self.obs.current, self.obs.video["baseWidth"], self.obs.video["baseHeight"]), ("B-roll Video", 640, 360))
        self.assertEqual(self.obs.video["outputWidth"], 640)

    async def test_layouts_are_validated_and_applied(self):
        client = await self.start_takes()
        self.assertEqual((await client.post("/api/takes/layout", json={"layout": "pip"})).status, 409, "not armed")
        await client.post("/api/takes/arm", json={})
        self.assertEqual((await client.post("/api/takes/layout", json={"layout": "zoom"})).status, 400)
        self.assertEqual((await client.post("/api/takes/arm", json={"layout": "zoom"})).status, 400)
        response = await client.post("/api/takes/layout", json={"layout": "slide"})
        self.assertEqual((await response.json())["layout"], "slide")
        enabled = {d["sceneItemId"]: d["sceneItemEnabled"] for d in self.obs.names("SetSceneItemEnabled")[-2:]}
        self.assertEqual(enabled, {1: True, 2: True})
        transform = self.obs.names("SetSceneItemTransform")[-2]["sceneItemTransform"]
        self.assertGreater(transform["positionX"], 1920, "the camera is moved off the canvas but stays active")
        await client.post("/api/takes/layout", json={"layout": "pip"})
        camera = self.obs.names("SetSceneItemTransform")[-2]["sceneItemTransform"]
        self.assertEqual((camera["boundsWidth"], camera["boundsHeight"]), (480.0, 270.0))
        self.assertEqual((camera["positionX"], camera["positionY"]), (1400.0, 770.0))

    async def test_the_slide_goes_to_the_slide_source(self):
        client = await self.start_takes()
        deck = await self.deck(client, JPEG)
        await client.post("/api/takes/arm", json={"layout": "pip", "slide": {"deck_id": deck, "page": 1}})
        self.assertTrue(self.obs.inputs["Take Slide"]["file"].endswith(f"{deck}/0001.jpg"))
        await client.post("/api/takes/slide", json={"slide": None})
        self.assertEqual(self.obs.inputs["Take Slide"]["file"], "")
        self.assertEqual((await client.post("/api/takes/slide", json={"slide": {"deck_id": "deck-nothere", "page": 1}})).status, 404)
        self.assertEqual((await client.post("/api/takes/slide", json={"slide": {"deck_id": "../x", "page": 1}})).status, 400)
        self.assertEqual((await client.post("/api/takes/slide", json={"slide": {"deck_id": deck, "page": 0}})).status, 400)
        await client.post("/api/takes/slide", json={"slide": {"deck_id": deck, "page": 1}})
        refused = await client.delete(f"/api/takes/slides/{deck}")
        self.assertEqual(refused.status, 409, "slides on the take scene stay")
        await client.post("/api/takes/disarm")
        self.assertEqual((await client.delete(f"/api/takes/slides/{deck}")).status, 200)

    async def test_a_failed_canvas_change_leaves_take_mode(self):
        client = await self.start_takes()
        self.obs.fail["SetVideoSettings"] = "no"
        response = await client.post("/api/takes/arm", json={})
        self.assertEqual(response.status, 503)
        self.assertFalse(self.takes.armed)
        self.assertFalse(self.controller.held_by())

    async def test_take_mode_holds_the_program_and_stop_wins(self):
        client = await self.start_takes(features={"program_control": True, "video_profiles": True})
        self.heartbeat_now()
        self.assertEqual((await client.post("/api/call/live")).status, 200)
        await client.post("/api/takes/arm", json={})
        refused = await client.post("/api/call/live")
        self.assertEqual((refused.status, (await refused.json())["error"]), (409, "Leave take mode before using the program"))
        self.assertEqual((await client.post("/api/call/video-profile", json={"profile": "540p"})).status, 409)
        await client.post("/api/takes/start", json={"take_id": "going-away"})
        self.assertEqual((await client.post("/api/call/stop")).status, 200)
        self.assertFalse(self.takes.armed)
        self.assertIsNone(self.takes.recording)
        self.assertEqual((self.obs.current, self.obs.video["baseWidth"]), ("Stopped", 640))
        library = await (await client.get("/api/takes/library")).json()
        self.assertEqual([take["take_id"] for take in library["takes"]], ["going-away"], "stopping keeps the take")

    async def test_disarm_returns_to_live_only_with_a_live_sender_in_program_control(self):
        client = await self.start_takes(features={"program_control": True})
        self.heartbeat_now()
        await client.post("/api/call/live")
        await client.post("/api/takes/arm", json={})
        await client.post("/api/takes/disarm")
        self.assertEqual(self.obs.current, "Live Feed")
        await client.post("/api/takes/arm", json={})
        self.controller.sender_last_seen = 0
        await client.post("/api/takes/disarm")
        self.assertEqual(self.obs.current, "Stopped")

    async def test_a_restart_never_leaves_the_take_scene_on_air(self):
        client = await self.start_takes()
        self.obs.current = SCENE
        self.obs.video.update(baseWidth=1920, baseHeight=1080, outputWidth=1920, outputHeight=1080)
        self.obs.streaming = True
        await self.takes.reset_on_startup(self.controller.app)
        await self.takes.reset_task
        self.assertEqual((self.obs.current, self.obs.video["baseWidth"], self.obs.streaming), ("Stopped", 640, False))


class SlideUploadTests(TakesCase):
    async def test_png_and_jpeg_become_one_page_decks(self):
        client = await self.start_takes()
        png = await (await self.upload(client, png_bytes(), name="  Opening   slide ")).json()
        self.assertEqual({k: png["slides"][k] for k in ("name", "kind", "pages", "extension")},
                         {"name": "Opening slide", "kind": "image", "pages": 1, "extension": "png"})
        jpeg = await (await self.upload(client, JPEG, filename="../../etc/passwd")).json()
        self.assertEqual(jpeg["slides"]["extension"], "jpg")
        library = await (await client.get("/api/takes/library")).json()
        self.assertEqual(len(library["slides"]), 2)
        image = await client.get(f"/api/takes/slides/{png['slides']['id']}/1")
        self.assertEqual((image.status, image.headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(await image.read(), png_bytes())
        self.assertEqual((await client.get(f"/api/takes/slides/{png['slides']['id']}/2")).status, 404)
        stored = sorted(p.relative_to(self.data_dir).parts[0] for p in self.data_dir.rglob("*") if p.is_file())
        self.assertEqual(set(stored), {"shared"}, "no identities configured: one shared area")

    async def test_file_type_and_size_limits(self):
        client = await self.start_takes({"max_upload_mb": 1})
        for data, status in ((b"just text", 415), (b"GIF89a....", 415), (b"MZ\x90\0", 415), (b"", 400),
                             (png_bytes() + b"\0" * (1024 * 1024), 413)):
            with self.subTest(data[:8], status=status):
                self.assertEqual((await self.upload(client, data)).status, status)
        self.assertEqual((await client.post("/api/takes/slides", data=FormData())).status, 400, "no file at all")
        tight = png_bytes() + b"\0" * (1024 * 1024 - len(png_bytes()))
        self.assertEqual((await self.upload(client, tight)).status, 201, "exactly at the limit")
        leftovers = [p.name for p in (self.data_dir / "shared" / "slides").iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [], "failed uploads leave nothing behind")
        self.assertEqual(len(list((self.data_dir / "shared" / "slides").iterdir())), 1)

    async def test_names_from_the_client_never_become_paths(self):
        client = await self.start_takes()
        response = await self.upload(client, png_bytes(), name="../../../tmp/evil", filename="../../x.png")
        deck = (await response.json())["slides"]["id"]
        self.assertRegex(deck, r"^deck-[0-9a-f]{12}$")
        files = {p.relative_to(self.data_dir).as_posix() for p in self.data_dir.rglob("*") if p.is_file()}
        self.assertEqual(files, {f"shared/slides/{deck}/0001.png", f"shared/slides/{deck}/deck.json"})
        for bad in ("..", "%2e%2e", "..%2f..%2fetc", "deck-abc%2f..%2f..", "DECK-UPPER", "a" * 100, "deck-a/b"):
            for path in (f"/api/takes/slides/{bad}/1", f"/api/takes/{bad}.mp4"):
                with self.subTest(path):
                    # (the client may normalise "../" into another route's path: refused with 405 there)
                    self.assertIn((await client.get(path)).status, (404, 405))
            self.assertIn((await client.delete(f"/api/takes/slides/{bad}")).status, (404, 405))
            self.assertIn((await client.delete(f"/api/takes/{bad}")).status, (404, 405))
        for page in ("0", "1000", "-1", "x", "1.5"):
            self.assertEqual((await client.get(f"/api/takes/slides/{deck}/{page}")).status, 404)
        self.assertEqual((await client.get(f"/api/takes/slides/{deck}/../../../../etc/passwd")).status, 404)

    @unittest.skipUnless(shutil.which("pdftoppm") and shutil.which("pdfinfo"), "needs poppler-utils")
    async def test_pdf_pages_become_slides(self):
        client = await self.start_takes({"max_pdf_pages": 3})
        deck = (await (await self.upload(client, pdf_bytes(3), name="Plan")).json())["slides"]
        self.assertEqual((deck["kind"], deck["pages"], deck["extension"]), ("pdf", 3, "png"))
        for page in (1, 2, 3):
            image = await client.get(f"/api/takes/slides/{deck['id']}/{page}")
            self.assertEqual(image.status, 200)
            self.assertTrue((await image.read()).startswith(b"\x89PNG"))
        self.assertEqual((await client.get(f"/api/takes/slides/{deck['id']}/4")).status, 404)
        too_long = await self.upload(client, pdf_bytes(4))
        self.assertEqual(too_long.status, 400)
        self.assertIn("limit is 3", (await too_long.json())["error"])
        broken = await self.upload(client, b"%PDF-1.4\nthis is not a pdf\n")
        self.assertEqual(broken.status, 400)
        folders = [p.name for p in (self.data_dir / "shared" / "slides").iterdir()]
        self.assertEqual(folders, [deck["id"]], "failed PDFs leave no partial deck or render directory")

    async def test_pdf_without_poppler_is_a_clear_error(self):
        client = await self.start_takes()
        with mock.patch.dict(os.environ, {"PATH": str(self.tmp / "empty")}):
            response = await self.upload(client, pdf_bytes(1))
        self.assertEqual(response.status, 503)
        self.assertIn("poppler", (await response.json())["error"])
        self.assertEqual(list((self.data_dir / "shared" / "slides").iterdir()), [])


class PerUserTests(TakesCase):
    async def test_slides_and_takes_are_kept_apart_per_identity(self):
        client = await self.start_takes(access=TRUST)
        ann_deck = await self.deck(client, png_bytes(), ANN)
        await client.post("/api/takes/arm", json={"slide": {"deck_id": ann_deck, "page": 1}}, headers=ANN)
        await self.record(client, "ann-take", ANN)
        await client.post("/api/takes/disarm", headers=ANN)
        bob_deck = await self.deck(client, JPEG, BOB)
        ann_library = await (await client.get("/api/takes/library", headers=ANN)).json()
        bob_library = await (await client.get("/api/takes/library", headers=BOB)).json()
        self.assertEqual([d["id"] for d in ann_library["slides"]], [ann_deck])
        self.assertEqual([t["take_id"] for t in ann_library["takes"]], ["ann-take"])
        self.assertEqual([d["id"] for d in bob_library["slides"]], [bob_deck])
        self.assertEqual(bob_library["takes"], [])
        self.assertEqual((await client.get(f"/api/takes/slides/{ann_deck}/1", headers=BOB)).status, 404)
        self.assertEqual((await client.get("/api/takes/ann-take.mp4", headers=BOB)).status, 404)
        self.assertEqual((await client.get("/api/takes/ann-take/audio.mp3", headers=BOB)).status, 404)
        self.assertEqual((await client.get("/api/takes/ann-take.mp4", headers=ANN)).status, 200)
        self.assertEqual((await client.delete("/api/takes/ann-take", headers=BOB)).status, 404)
        self.assertEqual((await client.delete(f"/api/takes/slides/{ann_deck}", headers=BOB)).status, 404)
        self.assertEqual(
            (await client.post("/api/takes/arm", json={"slide": {"deck_id": ann_deck, "page": 1}}, headers=BOB)).status, 404
        )
        self.assertTrue((self.data_dir / "users" / "ann" / "takes" / "ann-take.mp4").is_file())
        self.assertTrue((self.data_dir / "users" / "bob" / "slides" / bob_deck / "0001.jpg").is_file())
        self.assertFalse((self.data_dir / "shared").exists())

    async def test_one_user_at_a_time_holds_take_mode(self):
        client = await self.start_takes(access=TRUST)
        await client.post("/api/takes/arm", json={"layout": "camera"}, headers=ANN)
        status = await (await client.get("/api/takes/status", headers=BOB)).json()
        self.assertEqual((status["armed"], status["mine"], status["layout"]), (True, False, None))
        for method, path, body in (("post", "/api/takes/arm", {}), ("post", "/api/takes/disarm", None),
                                   ("post", "/api/takes/layout", {"layout": "pip"}), ("post", "/api/takes/slide", {"slide": None}),
                                   ("post", "/api/takes/start", {"take_id": "bobs-take"})):
            response = await getattr(client, method)(path, json=body, headers=BOB)
            self.assertEqual((path, response.status), (path, 409))
            self.assertIn("another user", (await response.json())["error"])
        self.assertTrue(self.takes.armed)
        self.assertEqual((await client.post("/api/takes/disarm", headers=ANN)).status, 200)
        self.assertEqual((await client.post("/api/takes/arm", json={}, headers=BOB)).status, 200)

    async def test_recording_and_jobs_belong_to_their_user(self):
        client = await self.start_takes(access=TRUST)
        await client.post("/api/takes/arm", json={}, headers=ANN)
        await client.post("/api/takes/start", json={"take_id": "ann-take"}, headers=ANN)
        self.assertIsNone((await (await client.get("/api/takes/status", headers=BOB)).json())["recording"])
        self.assertEqual((await client.post("/api/takes/stop", headers=BOB)).status, 409)
        self.assertEqual((await client.post("/api/takes/stop", headers=ANN)).status, 200)
        await client.post("/api/takes/disarm", headers=ANN)
        job = await self.render(client, [{"type": "take", "take_id": "ann-take"}], ANN)
        self.assertEqual(job["state"], "done", job)
        response = await client.post("/api/takes/assemble", json={"items": [{"type": "take", "take_id": "ann-take"}]}, headers=BOB)
        self.assertEqual(response.status, 400, "Bob has no such take")
        self.assertEqual((await client.get(f"/api/takes/jobs/{job['job_id']}", headers=BOB)).status, 404)
        self.assertEqual((await client.get(f"/api/takes/jobs/{job['job_id']}", headers=ANN)).status, 200)
        record = json.loads((self.tmp / "recordings" / f"{job['clip_id']}.json").read_text())
        self.assertEqual(record["owner"], "ann")

    async def test_directory_names_are_safe_for_any_identity(self):
        client = await self.start_takes(access=TRUST)
        for user in ("..", ".", "../evil", "a/b", "x\\y", "ann@example.invalid", "björn"):
            with self.subTest(user=user):
                response = await self.upload(client, png_bytes(), {"Remote-User": user})
                if response.status == 403:
                    continue  # the proxy layer refused this user name altogether
                self.assertEqual(response.status, 201)
        users = self.data_dir / "users"
        for child in users.iterdir():
            self.assertNotIn("/", child.name)
            self.assertNotIn(child.name, ("", ".", ".."))
            self.assertEqual(child.resolve().parent, users.resolve())
        outside = [p for p in self.tmp.rglob("deck.json") if self.data_dir.resolve() not in p.resolve().parents]
        self.assertEqual(outside, [])

    async def test_an_identity_is_required_once_identities_are_configured(self):
        client = await self.start_takes(access={"identity_header": "Remote-User"})  # no trust: nothing enforced, no user
        self.assertEqual((await client.get("/api/takes/status")).status, 403)
        client = await self.start_takes(access=TRUST)
        self.assertEqual((await client.get("/api/takes/status")).status, 403)
        self.assertEqual((await client.get("/api/takes/status", headers=ANN)).status, 200)


class RecordingTests(TakesCase):
    async def test_a_take_is_recorded_listed_served_and_deleted(self):
        client = await self.start_takes()
        self.assertEqual((await client.post("/api/takes/start", json={"take_id": "too-early"})).status, 409)
        await client.post("/api/takes/arm", json={})
        for body in ({"take_id": "a"}, {"take_id": "UPPER-case"}, {"take_id": "../escape"}, {"take_id": None}):
            self.assertEqual((await client.post("/api/takes/start", json=body)).status, 400, body)
        started = await (await client.post("/api/takes/start", json={"take_id": "first-take"})).json()
        self.assertEqual(started["recording"]["take_id"], "first-take")
        self.assertTrue(self.obs.streaming)
        self.assertEqual(self.obs.stream_service["streamServiceSettings"]["server"], "rtmp://127.0.0.1:1935/take")
        self.assertTrue(self.obs.stream_service["streamServiceSettings"]["key"].startswith("first-take-"))
        self.assertEqual((await client.post("/api/takes/start", json={"take_id": "second-take"})).status, 409)
        self.assertEqual((await client.delete("/api/takes/first-take")).status, 404, "not finished yet")
        stopped = await (await client.post("/api/takes/stop")).json()
        take = stopped["take"]
        self.assertEqual((take["take_id"], take["ok"], take["has_audio"], take["width"]), ("first-take", True, True, 1920))
        self.assertFalse(self.obs.streaming)
        self.assertEqual((await client.post("/api/takes/stop")).status, 409)
        self.assertEqual((await client.post("/api/takes/start", json={"take_id": "first-take"})).status, 409, "ids are not reused")
        self.assertEqual(await (await client.get("/api/takes/first-take.mp4")).read(), b"mkv")
        audio = await client.get("/api/takes/first-take/audio.mp3")
        self.assertEqual((audio.status, audio.headers["Content-Type"]), (200, "audio/mpeg"))
        self.assertEqual(await audio.read(), b"mkv")
        cached = self.data_dir / "shared" / "takes" / "first-take.mp3"
        self.assertTrue(cached.is_file())
        cached.write_bytes(b"cached-mp3")
        self.assertEqual(await (await client.get("/api/takes/first-take/audio.mp3")).read(), b"cached-mp3")
        library = await (await client.get("/api/takes/library")).json()
        self.assertEqual([t["take_id"] for t in library["takes"]], ["first-take"])
        self.assertEqual(library["takes"][0]["url"], "api/takes/first-take.mp4")
        names = sorted(p.name for p in (self.data_dir / "shared" / "takes").iterdir())
        self.assertEqual(names, ["first-take.json", "first-take.mp3", "first-take.mp4"], "only the take, its metadata, and cached audio remain")
        self.assertEqual((await client.delete("/api/takes/first-take")).status, 200)
        self.assertEqual((await client.get("/api/takes/first-take.mp4")).status, 404)

    async def test_a_generated_take_id_is_used_when_none_is_given(self):
        client = await self.start_takes()
        await client.post("/api/takes/arm", json={})
        started = await (await client.post("/api/takes/start")).json()
        self.assertRegex(started["recording"]["take_id"], r"^take-[0-9a-f]{12}$")
        await client.post("/api/takes/stop")

    async def test_a_black_camera_picture_refuses_to_record(self):
        client = await self.start_takes()
        await client.post("/api/takes/arm", json={})
        self.obs.camera_luma = 0
        response = await client.post("/api/takes/start", json={"take_id": "dark-take"})
        self.assertEqual(response.status, 409)
        self.assertIn("black", (await response.json())["error"])
        self.assertFalse(self.obs.streaming)
        await client.post("/api/takes/disarm")

    async def test_the_camera_check_can_be_switched_off(self):
        client = await self.start_takes({"check_camera": False})
        await client.post("/api/takes/arm", json={})
        self.obs.camera_luma = 0
        self.assertEqual((await client.post("/api/takes/start", json={"take_id": "dark-take"})).status, 200)
        await client.post("/api/takes/stop")

    async def test_obs_refusing_the_stream_does_not_leave_a_recording(self):
        client = await self.start_takes()
        await client.post("/api/takes/arm", json={})
        self.obs.fail["StartStream"] = "no encoder"
        response = await client.post("/api/takes/start", json={"take_id": "no-stream"})
        self.assertEqual(response.status, 503)
        self.assertIsNone(self.takes.recording)
        self.assertEqual([p.name for p in (self.data_dir / "shared" / "takes").iterdir()], [])

    async def test_the_watchdog_ends_a_take_that_runs_too_long(self):
        client = await self.start_takes()
        self.takes.s = dataclasses.replace(self.takes.s, max_take_seconds=1)
        await client.post("/api/takes/arm", json={})
        await client.post("/api/takes/start", json={"take_id": "long-take"})
        await asyncio.sleep(3)
        self.assertIsNone(self.takes.recording)
        take = (await (await client.get("/api/takes/library")).json())["takes"][0]
        self.assertEqual((take["take_id"], take["auto_stopped"]), ("long-take", True))

    async def test_disarm_waits_for_obs_stream_inactive_before_restoring_canvas(self):
        client = await self.start_takes()
        self.obs.delayed_stream_stop = True
        await client.post("/api/takes/arm", json={})
        await client.post("/api/takes/start", json={"take_id": "kept-take"})
        response = await client.post("/api/takes/disarm")
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(len(self.obs.names("StopStream")), 1, "subscribe and stop once, so STOPPED is not lost")
        payload = await response.json()
        self.assertFalse(self.obs.streaming)
        self.assertEqual(self.obs.video["baseWidth"], 640)
        self.assertTrue(self.obs.streaming_at_canvas_changes)
        self.assertFalse(self.obs.streaming_at_canvas_changes[-1], "canvas restore must wait until OBS reports outputActive false")
        finished = payload["finished_take"]
        self.assertEqual((finished["take_id"], finished["ok"]), ("kept-take", True))
        self.assertTrue((self.data_dir / "shared" / "takes" / "kept-take.mp4").is_file())


class AssemblyTests(TakesCase):
    async def ready(self):
        client = await self.start_takes()
        deck = await self.deck(client)
        await client.post("/api/takes/arm", json={})
        await self.record(client, "first-take")
        await client.post("/api/takes/disarm")
        return client, deck

    async def test_an_assembly_becomes_an_ordinary_clip(self):
        client, deck = await self.ready()
        job = await self.render(client, [
            {"type": "take", "take_id": "first-take", "start": 1, "end": 4},
            {"type": "still", "deck_id": deck, "page": 1, "seconds": 2},
        ])
        self.assertEqual((job["state"], job["progress"], job["total_seconds"]), ("done", 1.0, 5.0))
        clip = job["clip"]
        self.assertRegex(clip["id"], r"^take-[0-9a-f]{16}$")
        self.assertEqual(clip["display_name"], "Intro")
        self.assertEqual((self.tmp / "recordings" / f"{clip['id']}.mp4").read_bytes(), b"assembled mp4")
        listed = await (await client.get("/api/clips")).json()
        self.assertIn(clip["id"], [c["id"] for c in listed["clips"]])
        self.assertIn(clip["scene_name"], self.obs.scenes)
        self.assertEqual(clip["scene_name"].split(" · ")[0], "Recorded")
        self.assertEqual((await client.post(f"/api/clip/{clip['id']}/scene")).status, 200, "playable with the clip routes")
        self.assertEqual(self.obs.current, clip["scene_name"])
        self.assertEqual([p.name for p in (self.tmp / "recordings").iterdir() if p.name.startswith(".")], [])

    async def test_the_clip_is_played_into_the_program_in_program_control_too(self):
        client = await self.start_takes(features={"program_control": True})
        await client.post("/api/takes/arm", json={})
        await self.record(client, "first-take")
        await client.post("/api/takes/disarm")
        job = await self.render(client, [{"type": "take", "take_id": "first-take"}])
        self.heartbeat_now()
        with mock.patch("malrunar_obs.clips.probe_duration", new=mock.AsyncMock(return_value=8.0)):
            played = await client.post(f"/api/clip/{job['clip']['id']}/play")
        self.assertEqual(played.status, 200, await played.text())
        self.assertEqual(self.obs.current, job["clip"]["scene_name"])

    async def test_invalid_timelines_are_refused(self):
        client, deck = await self.ready()
        take = {"type": "take", "take_id": "first-take"}
        cases = {
            "empty": [],
            "not a list": "x",
            "not objects": ["x"],
            "too many": [take] * 61,
            "unknown type": [{"type": "video"}],
            "missing take": [{"type": "take", "take_id": "nothing-here"}],
            "traversal": [{"type": "take", "take_id": "../../first-take"}],
            "trim backwards": [{**take, "start": 3, "end": 2}],
            "negative start": [{**take, "start": -1}],
            "trim past the end": [{**take, "start": 9}],
            "text instead of a number": [{**take, "start": "soon"}],
            "list instead of a number": [{**take, "end": [1]}],
            "boolean": [{**take, "start": True}],
            "missing slide": [{"type": "still", "deck_id": "deck-nothere", "page": 1, "seconds": 2}],
            "slide traversal": [{"type": "still", "deck_id": "../x", "page": 1, "seconds": 2}],
            "still too short": [{"type": "still", "deck_id": deck, "page": 1, "seconds": 0.1}],
            "still too long": [{"type": "still", "deck_id": deck, "page": 1, "seconds": 500}],
            "still without length": [{"type": "still", "deck_id": deck, "page": 1}],
            "still page": [{"type": "still", "deck_id": deck, "page": 7, "seconds": 2}],
        }
        for label, items in cases.items():
            with self.subTest(label):
                response = await client.post("/api/takes/assemble", json={"items": items})
                self.assertEqual(response.status, 400, await response.text())
        for literal in ("NaN", "Infinity", "-Infinity"):
            response = await client.post(
                "/api/takes/assemble", data=f'{{"items": [{{"type": "take", "take_id": "first-take", "start": {literal}}}]}}',
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response.status, 400, literal)
        self.assertEqual((await client.post("/api/takes/assemble", data="not json")).status, 400)

    async def test_the_command_trims_scales_and_joins(self):
        client, deck = await self.ready()
        items = [
            {"type": "take", "take_id": "first-take", "start": 1, "end": 4, "_duration": 8.0, "_has_audio": True},
            {"type": "take", "take_id": "first-take", "_duration": 8.0, "_has_audio": False},
            {"type": "still", "deck_id": deck, "page": 1, "seconds": 2},
        ]
        command, total = self.takes.assembly_command(None, items, self.tmp / "out.mp4")
        self.assertEqual(total, 3 + 8 + 2)
        text = " ".join(command)
        take = self.data_dir / "shared" / "takes" / "first-take.mp4"
        self.assertIn(f"-ss 1.000 -to 4.000 -i {take}", text)
        self.assertIn("anullsrc=r=48000:cl=stereo", text)
        self.assertIn(f"-loop 1 -framerate 24 -t 2.000 -i {self.data_dir}/shared/slides/{deck}/0001.png", text)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("scale=1920:1080:force_original_aspect_ratio=decrease", graph)
        self.assertIn("concat=n=3:v=1:a=1[v][a]", graph)
        self.assertEqual(command[-1], str(self.tmp / "out.mp4"))
        self.assertEqual(command[:3], ["nice", "-n", "10"])

    async def test_one_render_at_a_time_and_none_while_recording(self):
        client, deck = await self.ready()
        await client.post("/api/takes/arm", json={})
        await client.post("/api/takes/start", json={"take_id": "second-take"})
        response = await client.post("/api/takes/assemble", json={"items": [{"type": "take", "take_id": "first-take"}]})
        self.assertEqual(response.status, 409)
        await client.post("/api/takes/stop")
        self.takes.jobs["busy"] = {"state": "running", "_owner": None}
        response = await client.post("/api/takes/assemble", json={"items": [{"type": "take", "take_id": "first-take"}]})
        self.assertEqual(response.status, 409)
        self.assertEqual((await client.post("/api/takes/start", json={"take_id": "third-take"})).status, 409)

    async def test_a_failing_render_reports_the_error(self):
        client, deck = await self.ready()
        with mock.patch.object(self.takes, "assembly_command",
                               return_value=([sys.executable, "-c", "import sys; sys.stderr.write('bad filter'); sys.exit(1)"], 1.0)):
            job = await self.render(client, [{"type": "take", "take_id": "first-take"}])
        self.assertEqual((job["state"], job["error"]), ("error", "bad filter"))
        self.assertEqual([p.name for p in (self.tmp / "recordings").iterdir() if p.suffix == ".mp4"], [])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class RealFfmpegTests(ApiCase):
    """The real tools, without the stubs: a prepared take plus a slide render to a 1080p clip with audio."""

    scenes = TakesCase.scenes

    def ffmpeg(self, *args):
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)

    async def test_a_take_and_a_still_are_joined_into_one_1080p_clip(self):
        client = await self.start(plugins=[takes_plugin])
        self.obs.scene_items = {SCENE: ITEMS}
        takes_dir = self.tmp / "recordings" / "takes" / "shared" / "takes"
        takes_dir.mkdir(parents=True)
        self.ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x180:rate=24", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000",
                    "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(takes_dir / "real-take.mp4"))
        picture = self.tmp / "slide.png"
        self.ffmpeg("-f", "lavfi", "-i", "color=c=blue:size=400x400", "-frames:v", "1", str(picture))
        deck = (await (await client.post("/api/takes/slides", data=upload_form(picture.read_bytes()))).json())["slides"]["id"]
        response = await client.post("/api/takes/assemble", json={"name": "Real", "items": [
            {"type": "take", "take_id": "real-take", "start": 0.5, "end": 2.5},
            {"type": "still", "deck_id": deck, "page": 1, "seconds": 1},
        ]})
        self.assertEqual(response.status, 202, await response.text())
        job_id = (await response.json())["job"]["job_id"]
        for _ in range(300):
            job = (await (await client.get(f"/api/takes/jobs/{job_id}")).json())["job"]
            if job["state"] != "running":
                break
            await asyncio.sleep(0.2)
        self.assertEqual(job["state"], "done", job)
        info = await takes_plugin.probe(self.tmp / "recordings" / f"{job['clip_id']}.mp4")
        self.assertEqual((info["width"], info["height"], info["has_audio"]), (1920, 1080, True))
        self.assertAlmostEqual(info["duration"], 3.0, delta=0.6)


class DocumentationTests(unittest.TestCase):
    @unittest.skipUnless(DOC.is_file(), "the docs are not part of the image")
    def test_every_takes_route_and_setting_is_documented(self):
        text = DOC.read_text(encoding="utf-8")
        section = text.split("## Takes (built-in plugin)")[1].split("\n## ")[0]
        missing = []
        for method, path in {(m, p) for m, p, _ in takes_plugin.Takes.routes(mock.MagicMock())}:
            documented = re.sub(r"\{(\w+)\}", r"<\1>", path)
            if "| `%s %s`" % (method, documented) not in section:
                missing.append((method, path))
        for key in takes_plugin.SETTINGS_DEFAULTS:
            if f"| `{key}` |" not in section:
                missing.append(key)
        self.assertEqual(missing, [])
        self.assertIn("MALRUNAR_OBS_FEATURES_TAKES", section)


if __name__ == "__main__":
    unittest.main()
