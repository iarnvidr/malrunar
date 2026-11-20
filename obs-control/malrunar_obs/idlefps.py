# SPDX-License-Identifier: Apache-2.0
"""A low frame rate while nothing is on air (``[video] idle_fps``).

With no call active the program is the stopped scene (one colour source), yet OBS still renders and
encodes every frame at ``video.fps``; on a host without a GPU that is a large share of a CPU for a
black picture. This module lowers the frame rate while the stopped scene is on air and nothing holds
the program, and puts the configured rate back **before** any live scene (camera, clip, take, b-roll)
goes on air.

OBS can change its frame rate only while no output is running, so every change is: stop the program
output and wait for the encoder, ``SetVideoSettings`` with the frame rate only (canvas and output
size stay as they are), start the output again. The output is therefore down for a short time at each
change; the restore on the way to a live scene is the one a caller waits for. ``POST
/api/call/prepare`` (and the sender lease coming back) run that restore ahead of the go-live.

Concurrency rules (all on one asyncio loop):

* every change runs under ``Controller.video_lock``; this module never takes ``clip_lock`` (the lock
  order everywhere is ``clip_lock`` then ``video_lock``);
* ``live()`` wraps the request that puts a live scene on air. Its fast path (frame rate already
  restored, no change running) only counts the switch; otherwise it restores under the lock first.
  The idle change checks, under the lock and before its first ``await``, that no switch is counted or
  waiting, and a switch that arrives later waits for the lock, so a go-live and an idle change can
  never interleave;
* a go-live waits at most ``LIVE_WAIT_SECONDS`` for the restore; after that it goes on air anyway and
  the restore finishes in the background (it always restarts the output).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Controller

log = logging.getLogger("malrunar_obs")

ACTIVE, IDLE, OTHER, UNKNOWN = "active", "idle", "other", "unknown"
PLAN_RESTORE, PLAN_APPLY, PLAN_HELD = "restore", "apply", "held"
RESULT_DONE, RESULT_RETRY, RESULT_ABORT, RESULT_FAILED = "done", "retry", "abort", "failed"
# Broll states in which a capture of the program output is running (a restart would cut it).
BROLL_CAPTURING = {"starting", "waiting", "recording"}


class IdleFps:
    LIVE_WAIT_SECONDS = 10.0  # the most a go-live waits for the restore
    RECHECK_SECONDS = 60.0  # re-plan even without a scene event (self-heals a missed event)
    START_ATTEMPTS = 4
    START_WAIT_SECONDS = 3.0
    BACKOFF_FIRST, BACKOFF_MAX = 15.0, 300.0

    def __init__(self, ctl: "Controller"):
        self.ctl = ctl
        video = ctl.config.video
        self.active_fps: int = video.fps
        self.idle_fps: int = video.idle_fps
        self.delay: float = video.idle_fps_delay
        self.enabled: bool = bool(self.idle_fps) and ctl.program_control
        self.state = UNKNOWN
        self.busy = False  # a change is running (set while holding video_lock)
        self.live_switches = 0  # live-scene requests in flight
        self.live_waiting = 0  # live-scene requests waiting for a restore
        self.last_change: dict[str, Any] | None = None
        self.failures = 0
        self.task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._background: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ plumbing
    def kick(self) -> None:
        """Something that may change the plan happened (a program scene change, a restore)."""
        self._wake.set()

    def start(self) -> None:
        if self.enabled and self.task is None:
            self.task = asyncio.create_task(self._worker())

    async def shutdown(self) -> None:
        tasks = [t for t in (self.task, *self._background) if t is not None and not t.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def ready_info(self) -> dict[str, Any]:
        return {
            "idle_fps": {
                "enabled": True,
                "fps": self.active_fps,
                "idle_fps": self.idle_fps,
                "state": self.state,
                "last_change": self.last_change,
            }
        }

    def active_fps_for_canvas(self) -> int | None:
        """The frame rate a canvas change may set at the same time (None: leave it alone)."""
        return self.active_fps if self.enabled else None

    def invalidate(self) -> None:
        """Something else changed the video settings: read them again before relying on the state."""
        if self.enabled:
            self.state = UNKNOWN

    # ------------------------------------------------------------------ the obs side
    @staticmethod
    def _matches(actual: float, wanted: int) -> bool:
        return abs(actual - wanted) < 0.01

    async def _read_fps(self) -> float:
        (video,) = await self.ctl.obs(("GetVideoSettings", {}))
        return video["fpsNumerator"] / (video.get("fpsDenominator") or 1)

    def _classify(self, actual: float) -> str:
        if self._matches(actual, self.active_fps):
            return ACTIVE
        if self._matches(actual, self.idle_fps):
            return IDLE
        return OTHER

    async def _sync_state(self) -> str:
        self.state = self._classify(await self._read_fps())
        return self.state

    async def _output_active(self) -> bool:
        (status,) = await self.ctl.obs(("GetRecordStatus", {}))
        return bool(status.get("outputActive"))

    async def _start_output(self) -> bool:
        """Start the program output again and wait until it is active (bounded)."""
        for _attempt in range(self.START_ATTEMPTS):
            try:
                if not await self._output_active():
                    await self.ctl.obs(("StartRecord", {}))
                deadline = time.monotonic() + self.START_WAIT_SECONDS
                while time.monotonic() < deadline:
                    if await self._output_active():
                        return True
                    await asyncio.sleep(0.1)
            except Exception as exc:
                log.warning("idle fps: starting the program output failed: %s", exc)
            await asyncio.sleep(0.5)
        return False

    async def _change(self, target: int) -> bool:
        """Set the frame rate (the caller holds ``video_lock`` and ``busy`` is set). Never raises.

        Stop the output and wait for the encoder, SetVideoSettings (frame rate only), start the
        output again. Whatever fails, the output is started again and the state is re-read."""
        ctl = self.ctl
        began = time.monotonic()
        output_down_at = None
        error: BaseException | None = None
        try:
            was_active = await self._output_active()
            if was_active:
                output_down_at = time.monotonic()
                await ctl.obs_client.stop_record_and_wait()
            # A go-live that queued while the output stopped must not wait for a second change.
            if target != self.active_fps and (self.live_waiting or self.live_switches):
                target = self.active_fps
            await ctl.obs(("SetVideoSettings", {"fpsNumerator": target, "fpsDenominator": 1}))
            self.state = self._classify(await self._read_fps())
            if self.state != (ACTIVE if target == self.active_fps else IDLE):
                raise RuntimeError(f"OBS reports a different frame rate than the {target} fps that was set")
        except asyncio.CancelledError:
            error = asyncio.CancelledError()
            raise
        except Exception as exc:
            error = exc
            self.state = UNKNOWN
            log.error("idle fps: changing the frame rate to %s failed: %s", target, exc)
        finally:
            restarted = True
            if output_down_at is not None:
                restarted = await self._start_output()
                if not restarted:
                    log.error("idle fps: the program output did not start again after the frame rate change")
        took = time.monotonic() - began
        if error is None and restarted:
            gap = (time.monotonic() - output_down_at) if output_down_at is not None else 0.0
            self.last_change = {"to_fps": target, "seconds": round(took, 2), "output_gap_seconds": round(gap, 2)}
            log.info("idle fps: now %s fps (took %.2fs, program output down %.2fs)", target, took, gap)
            self.failures = 0
            if target == self.active_fps:
                self.kick()  # the stopped scene may still be on air: plan the idle frame rate again
            return True
        self.failures += 1
        return False

    # ------------------------------------------------------------------ restore (before a live scene)
    async def _restore_locked(self) -> bool:
        self.busy = True
        try:
            try:
                state = await self._sync_state()
            except Exception as exc:
                log.error("idle fps: could not read the video settings: %s", exc)
                self.state = UNKNOWN
                return False
            if state == ACTIVE:
                return True
            return await self._change(self.active_fps)
        finally:
            self.busy = False

    async def _restore(self) -> bool:
        async with self.ctl.video_lock:
            return await self._restore_locked()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def ensure_active(self, wait: float | None = None) -> bool:
        """Make sure the configured frame rate is set. Never raises; False: could not (logged)."""
        if not self.enabled or (self.state == ACTIVE and not self.busy):
            return True
        self.live_waiting += 1
        try:
            task = self._spawn(self._restore())
            done, _ = await asyncio.wait({task}, timeout=wait)
            if not done:
                log.warning("idle fps: the frame rate restore takes longer than %.0fs; continuing without it", wait)
                return False
            return task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("idle fps: restoring the frame rate failed: %s", exc)
            return False
        finally:
            self.live_waiting -= 1

    @contextlib.asynccontextmanager
    async def live(self) -> AsyncIterator[None]:
        """Wrap the request that puts a live scene on air (see the module notes)."""
        if not self.enabled:
            yield
            return
        if not (self.state == ACTIVE and not self.busy):
            await self.ensure_active(wait=self.LIVE_WAIT_SECONDS)
        self.live_switches += 1
        try:
            yield
        finally:
            self.live_switches -= 1

    def warm_up(self) -> None:
        """Restore the frame rate in the background (a call is about to start)."""
        if self.enabled and not (self.state == ACTIVE and not self.busy):
            self._spawn(self.ensure_active())

    # ------------------------------------------------------------------ idle (the stopped scene)
    def _holds_program(self) -> bool:
        ctl = self.ctl
        clip_playing = ctl.clip_task is not None and not ctl.clip_task.done()
        return bool(ctl.held_by() or ctl.active_clip_input or clip_playing)

    def _switching(self) -> bool:
        ctl = self.ctl
        return bool(self.live_switches or self.live_waiting or ctl.clip_lock.locked())

    async def _capturing(self) -> bool:
        """A relay capture or a b-roll capture depends on the program output: do not restart it."""
        ctl = self.ctl
        if ctl.broll.state.get("state") in BROLL_CAPTURING:
            return True
        try:
            return bool((await ctl.clips.relay_request("GET", "/recording/status")).get("recording"))
        except Exception:
            return False  # no relay answering: nothing is being captured through it

    async def _apply(self) -> str:
        ctl = self.ctl
        async with ctl.video_lock:
            # No await between acquiring the lock and ``busy = True``: a live switch that starts now
            # either was counted above (we retry) or finds ``busy`` and waits for the lock.
            if self._switching():
                return RESULT_RETRY
            if self._holds_program():
                return RESULT_ABORT
            self.busy = True
            try:
                (scene,) = await ctl.obs(("GetCurrentProgramScene", {}))
                if scene.get("currentProgramSceneName") != ctl.stopped_scene:
                    return RESULT_ABORT
                if await self._capturing():
                    return RESULT_RETRY
                state = await self._sync_state()
                if state == IDLE:
                    return RESULT_DONE
                return RESULT_DONE if await self._change(self.idle_fps) else RESULT_FAILED
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("idle fps: could not apply the idle frame rate: %s", exc)
                self.state = UNKNOWN
                return RESULT_FAILED
            finally:
                self.busy = False

    async def _plan(self) -> str | None:
        ctl = self.ctl
        (scene,) = await ctl.obs(("GetCurrentProgramScene", {}))
        stopped = scene.get("currentProgramSceneName") == ctl.stopped_scene
        if self.state in (UNKNOWN, OTHER) and not self.busy:
            await self._sync_state()
        holds = self._holds_program()
        quiet = stopped and not holds
        if self.state == ACTIVE:
            # Held at the stopped scene (a take leaving, a clip ending): nothing announces the release.
            return PLAN_APPLY if quiet else (PLAN_HELD if stopped else None)
        if self.state == IDLE:
            return None if quiet else PLAN_RESTORE
        if self.state == OTHER:  # somebody set a rate that is neither of ours
            return PLAN_APPLY if quiet else PLAN_RESTORE
        return None

    async def _kicked_within(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._wake.wait(), seconds)
            return True
        except asyncio.TimeoutError:
            return False

    def _backoff(self) -> float:
        return min(self.BACKOFF_MAX, self.BACKOFF_FIRST * 2 ** max(0, self.failures - 1))

    async def _worker(self) -> None:
        ctl = self.ctl
        if ctl.startup_task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(ctl.startup_task)  # the startup sequence owns the output until it is done
        while True:
            self._wake.clear()
            recheck = self.RECHECK_SECONDS
            try:
                plan = await self._plan()
                if plan == PLAN_RESTORE:
                    if not await self._restore():
                        recheck = self._backoff()
                elif plan == PLAN_HELD:
                    recheck = max(self.delay, 1.0)
                elif plan == PLAN_APPLY:
                    if await self._kicked_within(self.delay):
                        continue  # a scene change: debounce again
                    result = await self._apply()
                    if result == RESULT_RETRY:
                        recheck = max(self.delay, 1.0)
                    elif result == RESULT_FAILED:
                        recheck = self._backoff()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.failures += 1
                recheck = self._backoff()
                log.warning("idle fps: %s (retrying in %.0fs)", exc, recheck)
            await self._kicked_within(recheck)
