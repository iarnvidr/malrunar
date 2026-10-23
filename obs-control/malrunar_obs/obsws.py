# SPDX-License-Identifier: Apache-2.0
"""A small obs-websocket (protocol v5) client: one connection per batch of requests."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import uuid
from typing import Any, AsyncIterator

import websockets


logger = logging.getLogger(__name__)


class ObsError(RuntimeError):
    """OBS refused or failed a request, or the WebSocket handshake failed."""


def auth_string(password: str, salt: str, challenge: str) -> str:
    """obs-websocket 5 authentication: base64(sha256(base64(sha256(password+salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsClient:
    def __init__(self, url: str, password: str | Any = "", *, open_timeout: float = 2, max_size: int = 2**22):
        self.url = url
        self._password = password  # a string, or a callable returning one (re-read on every connection)
        self.open_timeout = open_timeout
        self.max_size = max_size

    @property
    def password(self) -> str:
        return self._password() if callable(self._password) else self._password

    @contextlib.asynccontextmanager
    async def session(self, event_subscriptions: int = 0) -> AsyncIterator[Any]:
        """An identified WebSocket. Yields the raw socket after the Hello/Identify exchange."""
        logger.info("OBS websocket stage=open_handshake url=%s", self.url)
        async with websockets.connect(
            self.url, open_timeout=self.open_timeout, close_timeout=1, max_size=self.max_size
        ) as socket:
            logger.info("OBS websocket stage=receive_hello")
            hello = json.loads(await socket.recv())
            if hello.get("op") != 0:
                raise ObsError("OBS WebSocket did not send Hello")
            identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": event_subscriptions}
            authentication = hello.get("d", {}).get("authentication")
            if authentication:
                password = self.password
                if not password:
                    raise ObsError("OBS WebSocket needs a password and none is configured")
                identify["authentication"] = auth_string(
                    password, authentication["salt"], authentication["challenge"]
                )
            logger.info("OBS websocket stage=identify")
            await socket.send(json.dumps({"op": 1, "d": identify}))
            logger.info("OBS websocket stage=receive_identified")
            identified = json.loads(await socket.recv())
            if identified.get("op") != 2:
                raise ObsError("OBS WebSocket identification failed")
            yield socket

    @staticmethod
    async def ask(socket: Any, request_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        await socket.send(
            json.dumps(
                {"op": 6, "d": {"requestType": request_type, "requestId": request_id, "requestData": data or {}}}
            )
        )
        while True:
            response = json.loads(await socket.recv())
            if response.get("op") == 7 and response.get("d", {}).get("requestId") == request_id:
                break
        status = response["d"]["requestStatus"]
        if not status.get("result"):
            raise ObsError(f"{request_type}: {status.get('comment', 'failed')}")
        return response["d"].get("responseData") or {}

    async def stop_stream_and_wait(self) -> None:
        """Stop the take encoder and wait for libobs to finish disconnecting it.

        libobs signals stop before end_data_capture_thread finishes. The websocket
        STOPPED event alone is therefore insufficient: GetStreamStatus.outputActive
        reads obs_output_active(), cleared after stop_video/audio_encoders complete.
        See libobs/obs-output.c: obs_output_end_data_capture_internal and
        end_data_capture_thread in OBS 32.2.0.
        """
        await self._stop_output_and_wait("StopStream", "StreamStateChanged")

    async def stop_record_and_wait(self) -> None:
        """Wait for the program encoder to finish before changing video settings."""
        await self._stop_output_and_wait("StopRecord", "RecordStateChanged")

    async def _stop_output_and_wait(self, request_type: str, event_type: str) -> None:
        try:
            async with asyncio.timeout(30):
                await self._stop_output(request_type, event_type)
        except TimeoutError as exc:
            raise ObsError(f"{request_type} did not finish encoder teardown within 30s") from exc

    async def _stop_output(self, request_type: str, event_type: str) -> None:
        async with self.session(event_subscriptions=64) as socket:
            request_id = str(uuid.uuid4())
            await socket.send(json.dumps({"op": 6, "d": {
                "requestType": request_type, "requestId": request_id, "requestData": {}
            }}))
            response_ok = False
            stopped = False
            while not (response_ok and stopped):
                message = json.loads(await socket.recv())
                if message.get("op") == 5:
                    event = message.get("d", {})
                    if (event.get("eventType") == event_type
                            and event.get("eventData", {}).get("outputState") == "OBS_WEBSOCKET_OUTPUT_STOPPED"):
                        stopped = True
                elif message.get("op") == 7 and message.get("d", {}).get("requestId") == request_id:
                    status = message["d"]["requestStatus"]
                    if not status.get("result"):
                        if status.get("code") == 501:
                            # No new stop event is emitted for an inactive output.
                            # Still verify its native state rather than treating 501
                            # itself as proof of completed teardown.
                            stopped = True
                        else:
                            raise ObsError(f"{request_type}: {status.get('comment', 'failed')}")
                    response_ok = True
            status_type = "GetStreamStatus" if request_type == "StopStream" else "GetRecordStatus"
            while (await self.ask(socket, status_type)).get("outputActive") is not False:
                # State polling, bounded by the outer deadline; never a guessed
                # teardown delay or a retry of StopStream/StopRecord.
                await asyncio.sleep(0.05)

    async def request(self, *requests: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Send requests in order over one connection; the responses' data, in order."""
        async with self.session() as socket:
            results = []
            for request_type, data in requests:
                logger.info("OBS websocket stage=request request=%s", request_type)
                results.append(await self.ask(socket, request_type, data))
            return results
