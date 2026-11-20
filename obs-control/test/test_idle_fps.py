# SPDX-License-Identifier: Apache-2.0
"""The idle frame rate (``[video] idle_fps``) against the fake obs-websocket server."""
import asyncio
import json
import os
import time
import unittest
from unittest import mock

import helpers
from helpers import make_config
from malrunar_obs import takes as takes_plugin
from malrunar_obs.config import ENV_RESTORE, ConfigError
from malrunar_obs.controller import ProgramHolder
from malrunar_obs.idlefps import IdleFps
from test_api import ApiCase
from test_takes import SCENE, TakesCase

IDLE, ACTIVE = 6, 24
FPS_ONLY = {"fpsNumerator", "fpsDenominator"}


class IdleCase(ApiCase):
    """A controller in program control with ``idle_fps`` on and the program output running."""

    delay = 0.05
    idle_fps = IDLE

    def configure_obs(self, obs):
        obs.recording = True

    def setUp(self):
        super().setUp()
        for name, value in (("BACKOFF_FIRST", 0.1), ("BACKOFF_MAX", 0.2), ("LIVE_WAIT_SECONDS", 3.0)):
            patcher = mock.patch.object(IdleFps, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def start(self, **sections):
        sections.setdefault("features", {"program_control": True})
        video = {"idle_fps_delay": self.delay}
        if self.idle_fps:
            video["idle_fps"] = self.idle_fps
        sections["video"] = {**video, **sections.get("video", {})}
        return await super().start(**sections)

    # -- helpers
    @property
    def fps(self):
        return self.obs.video["fpsNumerator"] / self.obs.video["fpsDenominator"]

    @property
    def fps_changes(self):
        return [data for data in self.obs.names("SetVideoSettings")]

    async def until(self, condition, timeout=5.0, what="the condition"):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}")
            await asyncio.sleep(0.02)

    async def until_idle(self, timeout=5.0):
        await self.until(
            lambda: self.fps == IDLE and self.obs.recording and not self.controller.idle_fps.busy,
            timeout=timeout,
            what="the idle frame rate",
        )

    def index(self, kind, predicate=lambda data: True, start=0):
        for number, (name, data) in enumerate(self.obs.calls[start:], start):
            if name == kind and predicate(data):
                return number
        self.fail(f"no {kind} call")

    def assert_restored_before(self, scene, start=0):
        """The configured frame rate was set, and the output running again, before ``scene`` went on air."""
        restore = self.index("SetVideoSettings", lambda d: d.get("fpsNumerator") == ACTIVE, start)
        started = self.index("StartRecord", start=restore)
        live = self.index("SetCurrentProgramScene", lambda d: d["sceneName"] == scene, start)
        self.assertLess(started, live, f"{scene} went on air before the output was running at {ACTIVE} fps")
        self.assertEqual((self.fps, self.obs.recording), (ACTIVE, True))

    async def idle_then_live_call(self, client, path="/api/call/live", **kwargs):
        await self.until_idle()
        mark = len(self.obs.calls)
        self.heartbeat_now()
        response = await client.post(path, **kwargs)
        return response, mark


class OffByDefaultTests(IdleCase):
    idle_fps = 0

    async def test_nothing_changes_the_frame_rate_unless_idle_fps_is_set(self):
        client = await self.start()
        await asyncio.sleep(0.4)
        self.assertFalse(self.controller.idle_fps.enabled)
        self.assertIsNone(self.controller.idle_fps.task)
        self.assertEqual((self.obs.names("SetVideoSettings"), self.obs.names("StopRecord")), ([], []))
        self.assertNotIn("idle_fps", await (await client.get("/ready")).json())
        self.heartbeat_now()
        self.assertEqual((await client.post("/api/call/live")).status, 200)
        self.assertEqual(self.obs.current, "Live Feed")
        self.assertEqual(self.obs.names("SetVideoSettings"), [])

    async def test_prepare_is_a_harmless_no_op(self):
        client = await self.start()
        body = await (await client.post("/api/call/prepare")).json()
        self.assertEqual((body["ok"], body["idle_fps"]), (True, False))
        self.assertEqual(self.obs.names("SetVideoSettings"), [])

    async def test_prepare_needs_program_control(self):
        client = await self.start(features={"program_control": False})
        self.assertEqual((await client.post("/api/call/prepare")).status, 404)


class ConfigTests(helpers.TempDir, unittest.TestCase):
    def test_defaults_are_off(self):
        config = make_config(self.tmp)
        self.assertEqual((config.video.idle_fps, config.video.idle_fps_delay), (0, 15.0))

    def test_settings_and_environment(self):
        config = make_config(
            self.tmp,
            env={"MALRUNAR_OBS_VIDEO_IDLE_FPS": "8", "MALRUNAR_OBS_VIDEO_IDLE_FPS_DELAY": "2.5"},
            features={"program_control": True},
        )
        self.assertEqual((config.video.idle_fps, config.video.idle_fps_delay), (8, 2.5))

    def test_invalid_values_name_the_setting(self):
        program = {"program_control": True}
        for video, features, message in (
            ({"idle_fps": 24}, program, "idle_fps (24) must be lower than fps (24)"),
            ({"idle_fps": 30}, program, "must be lower than fps"),
            ({"idle_fps": -1}, program, "must not be negative"),
            ({"idle_fps": 6, "idle_fps_delay": -1}, program, "must not be negative"),
            ({"idle_fps": 6}, {}, "needs features.program_control"),
        ):
            with self.subTest(video=video, features=features):
                with self.assertRaises(ConfigError) as caught:
                    make_config(self.tmp, video=video, features=features)
                self.assertIn(message, str(caught.exception))
                self.assertIn("idle_fps", str(caught.exception))


class ApplyTests(IdleCase):
    delay = 0.4

    async def test_the_stopped_scene_gets_the_idle_rate_with_the_fps_changed_alone(self):
        client = await self.start()
        started = time.monotonic()
        await self.until_idle()
        self.assertGreaterEqual(time.monotonic() - started, 0.3, "the delay applies first")
        kinds = [name for name, _ in self.obs.calls if name in ("StopRecord", "SetVideoSettings", "StartRecord")]
        self.assertEqual(kinds, ["StopRecord", "SetVideoSettings", "StartRecord"])
        (change,) = self.fps_changes
        self.assertEqual(set(change), FPS_ONLY)
        self.assertEqual((change["fpsNumerator"], change["fpsDenominator"]), (IDLE, 1))
        self.assertEqual((self.obs.video["baseWidth"], self.obs.video["outputWidth"]), (640, 640))
        self.assertEqual(self.obs.recording_at_canvas_changes, [False], "changed while the output was stopped")
        ready = (await (await client.get("/ready")).json())["idle_fps"]
        self.assertEqual((ready["state"], ready["enabled"], ready["fps"], ready["idle_fps"]), ("idle", True, ACTIVE, IDLE))
        self.assertEqual(ready["last_change"]["to_fps"], IDLE)
        self.assertGreaterEqual(ready["last_change"]["output_gap_seconds"], 0)

    async def test_scene_changes_restart_the_debounce(self):
        await self.start()
        await self.until_idle()
        await self.obs.switch_scene("Live Feed")  # another client: the safety net restores
        await self.until(lambda: self.fps == ACTIVE and self.obs.recording, what="the safety net")
        changes = len(self.fps_changes)
        for scene in ("Stopped", "Live Feed", "Stopped"):
            await self.obs.switch_scene(scene)
            await asyncio.sleep(0.2)  # shorter than the delay each time
        self.assertEqual((len(self.fps_changes), self.fps), (changes, ACTIVE))
        last_change = time.monotonic()
        await self.until_idle()
        self.assertGreaterEqual(time.monotonic() - last_change, 0.15)
        self.assertEqual(len(self.fps_changes), changes + 1)

    async def test_an_idle_rate_already_set_is_left_alone(self):
        await self.start()
        await self.until_idle()
        calls = len(self.obs.calls)
        self.controller.idle_fps.kick()
        await asyncio.sleep(0.8)
        self.assertEqual([n for n, _ in self.obs.calls[calls:] if n in ("StopRecord", "SetVideoSettings")], [])

class OutputStoppedTests(IdleCase):
    delay = 0.1

    def configure_obs(self, obs):
        obs.recording = False

    async def test_a_stopped_output_is_not_started_by_the_change(self):
        await self.start()
        await self.until(lambda: self.fps == IDLE, what="the idle frame rate")
        await asyncio.sleep(0.2)
        self.assertEqual((self.obs.names("StartRecord"), self.obs.names("StopRecord")), ([], []))
        self.assertFalse(self.obs.recording)


class BlockerTests(IdleCase):
    delay = 1.0

    async def assert_no_change_while(self, setup, release, wait=1.5):
        await self.until(lambda: self.controller.idle_fps.state == "active", what="the first plan")
        setup()
        self.controller.idle_fps.kick()
        await asyncio.sleep(wait)  # longer than the delay: the change had its chance
        self.assertEqual((self.fps_changes, self.fps), ([], ACTIVE), "nothing may change while the program is held")
        release()
        self.controller.idle_fps.kick()
        await self.until_idle()

    async def test_a_program_holder(self):
        await self.start()
        held = [True]
        self.controller.add_program_holder(ProgramHolder("studio", lambda: held[0]))
        await self.assert_no_change_while(lambda: None, lambda: held.__setitem__(0, False))

    async def test_a_playing_clip(self):
        await self.start()
        await self.assert_no_change_while(
            lambda: setattr(self.controller, "active_clip_input", "clip"),
            lambda: setattr(self.controller, "active_clip_input", None),
        )

    async def test_a_running_relay_capture(self):
        await self.start()
        recording = {"recording": True}

        async def relay(method, path, **kwargs):
            return dict(recording)

        self.controller.clips.relay_request = relay
        await self.assert_no_change_while(lambda: None, lambda: recording.update(recording=False))

    async def test_a_running_broll_capture(self):
        await self.start()
        await self.assert_no_change_while(
            lambda: setattr(self.controller.broll, "state", {"state": "recording"}),
            lambda: setattr(self.controller.broll, "state", {"state": "idle"}),
        )


class RestoreBeforeLiveTests(IdleCase):
    async def test_go_live(self):
        client = await self.start()
        response, mark = await self.idle_then_live_call(client)
        self.assertEqual(response.status, 200, await response.text())
        self.assert_restored_before("Live Feed", mark)
        await asyncio.sleep(0.4)  # the live scene stays at the configured rate
        self.assertEqual((self.fps, self.obs.current), (ACTIVE, "Live Feed"))

    async def test_clip_playback(self):
        client = await self.start()
        directory = self.controller.recordings_dir
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "c.mp4").write_bytes(b"x")
        (directory / "c.json").write_text(json.dumps({"display_name": "C", "created_at": "2026-01-01T00:00:00+00:00"}))
        self.heartbeat_now()
        with mock.patch("malrunar_obs.clips.probe_duration", mock.AsyncMock(return_value=30.0)):
            response, mark = await self.idle_then_live_call(client, "/api/clip/c/play", json={})
        self.assertEqual(response.status, 200, await response.text())
        self.assert_restored_before("Recorded · C", mark)

    async def test_broll_playback(self):
        client = await self.start()
        path = self.controller.broll.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        response, mark = await self.idle_then_live_call(client, "/api/daily-fallback/play")
        self.assertEqual(response.status, 200, await response.text())
        self.assert_restored_before("B-roll Video", mark)

    async def test_a_scene_switched_by_somebody_else_is_restored_by_the_safety_net(self):
        await self.start()
        await self.until_idle()
        await self.obs.switch_scene("Live Feed")
        await self.until(lambda: self.fps == ACTIVE and self.obs.recording, what="the restore")

    async def test_going_back_to_the_stopped_scene_applies_the_idle_rate_again(self):
        client = await self.start()
        response, _ = await self.idle_then_live_call(client)
        self.assertEqual(response.status, 200)
        self.assertEqual((await client.post("/api/call/stop")).status, 200)
        await self.until_idle()
        self.assertEqual(self.obs.current, "Stopped")


class TakeTests(IdleCase, TakesCase):
    idle_fps = IDLE

    async def start(self, **sections):
        sections.setdefault("plugin", {"takes": {}})
        return await IdleCase.start(self, plugins=[takes_plugin], **sections)

    async def test_arming_from_the_idle_rate_restores_it_in_the_same_restart_as_the_canvas(self):
        client = await self.start()
        self.obs.scene_items = {SCENE: [{"sourceName": "Take Slide", "sceneItemId": 1}, {"sourceName": "Take Camera", "sceneItemId": 2}]}
        await self.until_idle()
        mark = len(self.obs.calls)
        response = await client.post("/api/takes/arm", json={})
        self.assertEqual(response.status, 200, await response.text())
        stops = [n for n, _ in self.obs.calls[mark:] if n == "StopRecord"]
        self.assertEqual(len(stops), 1, "one restart for the canvas and the frame rate")
        change = self.obs.names("SetVideoSettings")[-1]
        self.assertEqual((change["fpsNumerator"], change["baseWidth"]), (ACTIVE, 1920))
        self.assertEqual((self.obs.current, self.fps, self.obs.recording), (SCENE, ACTIVE, True))
        await asyncio.sleep(0.4)  # armed: the program is held, the rate stays
        self.assertEqual(self.fps, ACTIVE)
        self.assertEqual((await client.post("/api/takes/disarm")).status, 200)
        await self.until_idle()  # back at the stopped scene: idle again
        self.assertEqual(self.obs.video["baseWidth"], 640)


class RaceTests(IdleCase):
    async def test_a_go_live_during_the_idle_change_waits_and_costs_one_restart_only(self):
        client = await self.start_held("StopRecord")
        await self.until(lambda: self.obs.held["StopRecord"].is_set(), what="the idle change to be under way")
        self.heartbeat_now()
        live = asyncio.create_task(client.post("/api/call/live"))
        await self.until(lambda: self.controller.idle_fps.live_waiting == 1, what="the go-live to queue")
        await asyncio.sleep(0.2)
        self.assertNotEqual(self.obs.current, "Live Feed", "the live scene must wait for the frame rate")
        self.obs.hold["StopRecord"].set()
        response = await live
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.obs.current, "Live Feed")
        self.assertEqual([d["fpsNumerator"] for d in self.fps_changes], [ACTIVE], "the queued go-live made the one change a restore")
        self.assert_restored_before("Live Feed")
        await asyncio.sleep(0.4)
        self.assertEqual((self.fps, len(self.fps_changes)), (ACTIVE, 1))

    async def start_held(self, kind):
        """Start with ``kind`` held in the fake OBS (an idle change blocks there)."""
        self.obs_hold = (kind, asyncio.Event(), asyncio.Event())
        client = await self.start()
        return client

    def configure_obs(self, obs):
        super().configure_obs(obs)
        hold = getattr(self, "obs_hold", None)
        if hold:
            kind, release, held = hold
            obs.hold[kind], obs.held[kind] = release, held

    async def test_a_live_switch_in_flight_stops_the_idle_change(self):
        client = await self.start(video={"idle_fps_delay": 0.4})
        await self.until(lambda: self.controller.idle_fps.state == "active", what="the first plan")
        gate, held = asyncio.Event(), asyncio.Event()
        self.obs.hold["SetCurrentProgramScene"], self.obs.held["SetCurrentProgramScene"] = gate, held
        self.heartbeat_now()
        live = asyncio.create_task(client.post("/api/call/live"))
        await self.until(held.is_set, what="the scene request")
        self.assertEqual(self.controller.idle_fps.live_switches, 1)
        await asyncio.sleep(0.9)  # longer than the delay: the idle change gets its turn and must refuse
        self.assertEqual((self.fps_changes, self.fps), ([], ACTIVE))
        gate.set()
        self.assertEqual((await live).status, 200)
        await asyncio.sleep(0.6)
        self.assertEqual((self.fps_changes, self.fps, self.obs.current), ([], ACTIVE, "Live Feed"))

    async def test_a_scene_switched_during_the_change_ends_restored(self):
        client = await self.start_held("StopRecord")
        await self.until(lambda: self.obs.held["StopRecord"].is_set(), what="the idle change to be under way")
        await self.obs.switch_scene("Live Feed")  # not through the controller
        self.obs.hold["StopRecord"].set()
        await self.until(
            lambda: [d["fpsNumerator"] for d in self.fps_changes][-1:] == [ACTIVE] and self.obs.recording and not self.controller.idle_fps.busy,
            what="the safety net to restore",
        )
        self.assertEqual(self.obs.current, "Live Feed")

    async def test_go_lives_in_parallel_cost_one_restore(self):
        client = await self.start()
        await self.until_idle()
        self.heartbeat_now()
        responses = await asyncio.gather(*(client.post("/api/call/live") for _ in range(4)))
        self.assertEqual([r.status for r in responses], [200] * 4)
        self.assertEqual([d["fpsNumerator"] for d in self.fps_changes], [IDLE, ACTIVE])
        self.assertEqual(len(self.obs.names("StopRecord")), 2)


class ErrorPathTests(IdleCase):
    delay = 0.5

    async def planned(self):
        """Started, and the idle change is waiting out its delay."""
        client = await self.start()
        await self.until(lambda: self.controller.idle_fps.state == "active", what="the first plan")
        return client

    async def test_a_failed_idle_change_leaves_the_output_running_and_is_retried(self):
        await self.planned()
        self.obs.fail_once["SetVideoSettings"] = "boom"
        with self.assertLogs("malrunar_obs", "ERROR") as logs:
            await self.until(lambda: self.obs.names("SetVideoSettings") and not self.controller.idle_fps.busy, what="the failed change")
            self.assertEqual((self.fps, self.obs.recording), (ACTIVE, True))
        self.assertTrue(any("boom" in line for line in logs.output))
        await self.until_idle()  # retried after the backoff

    async def test_a_stop_that_fails_changes_nothing(self):
        await self.planned()
        self.obs.fail["StopRecord"] = "encoder stuck"
        with self.assertLogs("malrunar_obs", "ERROR"):
            await self.until(lambda: self.controller.idle_fps.failures >= 1 and not self.controller.idle_fps.busy, what="the failed stop")
        self.assertEqual((self.fps, self.obs.recording, self.fps_changes), (ACTIVE, True, []))
        self.obs.fail.clear()
        await self.until_idle()

    async def test_a_start_that_fails_once_is_retried(self):
        await self.planned()
        self.obs.fail_once["StartRecord"] = "busy"
        await self.until_idle(timeout=8)
        self.assertGreaterEqual(len(self.obs.names("StartRecord")), 2)

    async def test_a_failed_restore_still_goes_live_and_the_safety_net_finishes_it(self):
        client = await self.start()
        await self.until_idle()
        self.obs.fail_once["SetVideoSettings"] = "boom"
        self.heartbeat_now()
        with self.assertLogs("malrunar_obs", "ERROR"):
            response = await client.post("/api/call/live")
        self.assertEqual((response.status, self.obs.current), (200, "Live Feed"))
        self.assertTrue(self.obs.recording, "the output runs again after the failed change")
        await self.until(lambda: self.fps == ACTIVE and self.obs.recording and not self.controller.idle_fps.busy, what="the retry")

    async def test_a_restore_that_takes_too_long_does_not_hold_up_the_go_live(self):
        client = await self.start()
        await self.until_idle()
        gate, held = asyncio.Event(), asyncio.Event()
        self.obs.hold["SetVideoSettings"], self.obs.held["SetVideoSettings"] = gate, held
        self.heartbeat_now()
        began = time.monotonic()
        with mock.patch.object(IdleFps, "LIVE_WAIT_SECONDS", 0.3):
            response = await client.post("/api/call/live")
        self.assertEqual((response.status, self.obs.current), (200, "Live Feed"))
        self.assertLess(time.monotonic() - began, 2.5)
        self.assertTrue(held.is_set())
        gate.set()  # the restore finishes in the background and always starts the output again
        await self.until(lambda: self.fps == ACTIVE and self.obs.recording and not self.controller.idle_fps.busy, what="the background restore")

    async def test_the_prepare_route_reports_a_failed_restore(self):
        client = await self.start()
        await self.until_idle()
        self.obs.fail["SetVideoSettings"] = "boom"
        with self.assertLogs("malrunar_obs", "ERROR"):
            response = await client.post("/api/call/prepare")
        self.assertEqual((response.status, (await response.json())["ok"]), (503, False))
        self.assertTrue(self.obs.recording)
        self.obs.fail.clear()


class ReadyAndPrepareTests(IdleCase):
    delay = 0.8

    async def test_prepare_restores_ahead_of_the_go_live_and_idles_again_after_the_delay(self):
        client = await self.start()
        await self.until_idle()
        body = await (await client.post("/api/call/prepare")).json()
        self.assertEqual((body["ok"], body["idle_fps"], body["state"]), (True, True, "active"))
        self.assertEqual((self.fps, self.obs.recording), (ACTIVE, True))
        self.assertEqual(self.obs.current, "Stopped", "prepare does not go live")
        self.heartbeat_now()
        mark = len(self.obs.calls)
        await client.post("/api/call/live")
        self.assertEqual(len([n for n, _ in self.obs.calls[mark:] if n == "StopRecord"]), 0, "the go-live finds the rate restored")

    async def test_a_prepared_call_that_never_goes_live_falls_back_to_idle(self):
        client = await self.start()
        await self.until_idle()
        await client.post("/api/call/prepare")
        self.assertEqual(self.fps, ACTIVE)
        await self.until_idle()

    async def test_the_sender_returning_restores_the_rate_in_the_background(self):
        client = await self.start()
        await self.until_idle()
        self.controller.sender_last_seen = 0
        mark = len(self.obs.calls)
        response = await client.post("/api/sender-heartbeat", json={"healthy": True})
        self.assertEqual(response.status, 200)
        await self.until(
            lambda: any(d.get("fpsNumerator") == ACTIVE for n, d in self.obs.calls[mark:] if n == "SetVideoSettings"),
            what="the warm-up",
        )
        self.assertEqual(self.obs.current, "Stopped")


class StartupStateTests(IdleCase):
    def configure_obs(self, obs):
        super().configure_obs(obs)
        obs.video["fpsNumerator"] = IDLE  # OBS booted from a profile that still holds the idle rate

    async def test_a_stopped_scene_at_the_idle_rate_is_left_alone(self):
        client = await self.start()
        await self.until(lambda: self.controller.idle_fps.state == "idle", what="the state to be read")
        await asyncio.sleep(0.4)
        self.assertEqual((self.obs.names("SetVideoSettings"), self.obs.names("StopRecord")), ([], []))

    async def test_a_go_live_from_an_unread_state_reads_it_and_restores(self):
        client = await self.start()
        self.controller.idle_fps.state = "unknown"
        self.heartbeat_now()
        mark = len(self.obs.calls)
        self.assertEqual((await client.post("/api/call/live")).status, 200)
        self.index("GetVideoSettings", start=mark)  # the unread state was read first
        self.assert_restored_before("Live Feed", mark)

    async def test_a_live_scene_found_at_the_idle_rate_is_restored(self):
        restore = {"program_audio_muted": False, "clip_audio_muted": False, "sender_healthy": True}
        with mock.patch.dict(os.environ, {ENV_RESTORE: json.dumps(restore)}):
            client = await self.start()
            self.obs.current = "Live Feed"
            self.controller.idle_fps.kick()
            await self.until(lambda: self.fps == ACTIVE and self.obs.recording, what="the restore")
        self.assertEqual(self.obs.current, "Live Feed")


if __name__ == "__main__":
    unittest.main()
