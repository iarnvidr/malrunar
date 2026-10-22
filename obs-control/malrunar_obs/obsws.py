# SPDX-License-Identifier: Apache-2.0
"""A small obs-websocket (protocol v5) client: one connection per batch of requests."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import uuid
from typing import Any, AsyncIterator

import websockets


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
        async with websockets.connect(
            self.url, open_timeout=self.open_timeout, close_timeout=1, max_size=self.max_size
        ) as socket:
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
            await socket.send(json.dumps({"op": 1, "d": identify}))
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

    async def request(self, *requests: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Send requests in order over one connection; the responses' data, in order."""
        async with self.session() as socket:
            return [await self.ask(socket, request_type, data) for request_type, data in requests]
