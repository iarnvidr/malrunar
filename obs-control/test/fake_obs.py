# SPDX-License-Identifier: Apache-2.0
"""A minimal obs-websocket (v5) server for tests: keeps scenes, inputs and the record state."""
import base64
import hashlib
import json
import struct

import websockets

# A 1x1 JPEG.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)


def bmp_image(width=64, height=36, luma=200):
    """An uncompressed 24 bit BMP of one grey level."""
    row = (width * 3 + 3) & ~3
    pixels = bytes([luma]) * (width * 3) + b"\0" * (row - width * 3)
    header = b"BM" + struct.pack("<IHHI", 54 + row * height, 0, 0, 54)
    header += struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, row * height, 2835, 2835, 0, 0)
    return header + pixels * height


class FakeObs:
    def __init__(self, scenes=("Live Feed", "B-roll Video", "Stopped"), password=""):
        self.scenes = list(scenes)
        self.inputs: dict[str, dict] = {}
        self.current = self.scenes[0]
        self.recording = False
        self.password = password
        self.calls: list[tuple[str, dict]] = []
        self.muted: dict[str, bool] = {}
        self.profile_parameters: dict[str, str] = {}
        self.video = {"baseWidth": 640, "baseHeight": 360, "outputWidth": 640, "outputHeight": 360}
        self.streaming = False
        self.delayed_stream_stop = False
        self.streaming_at_canvas_changes: list[bool] = []
        self.stream_service: dict = {}
        self.camera_luma = 200  # brightness of the BMP GetSourceScreenshot returns
        self.scene_items: dict[str, list[dict]] = {}  # scene -> [{"sourceName", "sceneItemId"}]
        self.fail: dict[str, str] = {}  # request type -> comment: make that request fail
        self.server = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def names(self, request_type):
        return [data for kind, data in self.calls if kind == request_type]

    async def start(self):
        self.server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, socket):
        try:
            await self._serve(socket)
        except websockets.ConnectionClosed:
            pass

    async def _serve(self, socket):
        hello = {"obsStudioVersion": "test", "obsWebSocketVersion": "5.0.0", "rpcVersion": 1}
        if self.password:
            hello["authentication"] = {"challenge": "challenge-value", "salt": "salt-value"}
        await socket.send(json.dumps({"op": 0, "d": hello}))
        identify = json.loads(await socket.recv())
        if self.password:
            secret = base64.b64encode(hashlib.sha256((self.password + "salt-value").encode()).digest()).decode()
            expected = base64.b64encode(hashlib.sha256((secret + "challenge-value").encode()).digest()).decode()
            if identify["d"].get("authentication") != expected:
                await socket.close(4009, "Authentication failed")
                return
        await socket.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        async for raw in socket:
            message = json.loads(raw)
            if message.get("op") != 6:
                continue
            data = message["d"]
            kind, request_data = data["requestType"], data.get("requestData", {})
            self.calls.append((kind, request_data))
            if kind in self.fail:
                result, response = False, None
                comment = self.fail[kind]
            else:
                result, response = True, self.respond(kind, request_data)
                comment = ""
            status = {"result": result, "code": 100 if result else 600}
            if comment:
                status["comment"] = comment
            reply = {"requestType": kind, "requestId": data["requestId"], "requestStatus": status}
            if response is not None:
                reply["responseData"] = response
            await socket.send(json.dumps({"op": 7, "d": reply}))

    def respond(self, kind, data):
        if kind == "GetVersion":
            return {"obsVersion": "32.2.0", "rpcVersion": 1}
        if kind == "GetRecordStatus":
            return {"outputActive": self.recording, "outputTimecode": "00:00:01.000"}
        if kind == "StartRecord":
            self.recording = True
        elif kind == "StopRecord":
            self.recording = False
        elif kind == "GetCurrentProgramScene":
            return {"currentProgramSceneName": self.current}
        elif kind == "SetCurrentProgramScene":
            if data["sceneName"] not in self.scenes:
                raise AssertionError(f"unknown scene {data['sceneName']}")
            self.current = data["sceneName"]
        elif kind == "GetSceneList":
            return {"scenes": [{"sceneName": name, "sceneIndex": i} for i, name in enumerate(reversed(self.scenes))]}
        elif kind == "CreateScene":
            self.scenes.append(data["sceneName"])
        elif kind == "RemoveScene":
            self.scenes.remove(data["sceneName"])
        elif kind == "CreateInput":
            self.inputs[data["inputName"]] = data.get("inputSettings", {})
            self.muted.setdefault(data["inputName"], False)
        elif kind == "RemoveInput":
            self.inputs.pop(data["inputName"], None)
        elif kind == "GetInputList":
            return {"inputs": [{"inputName": name} for name in self.inputs]}
        elif kind == "SetInputMute":
            self.muted[data["inputName"]] = data["inputMuted"]
        elif kind == "SetInputSettings":
            self.inputs.setdefault(data["inputName"], {}).update(data["inputSettings"])
        elif kind == "SetProfileParameter":
            self.profile_parameters[data["parameterName"]] = data["parameterValue"]
        elif kind == "GetSceneItemList":
            return {"sceneItems": self.scene_items.get(data["sceneName"], [{"sceneItemId": 1}])}
        elif kind == "GetVideoSettings":
            return dict(self.video)
        elif kind == "SetVideoSettings":
            self.streaming_at_canvas_changes.append(self.streaming)
            self.video.update(data)
        elif kind == "SetStreamServiceSettings":
            self.stream_service = data
        elif kind == "StartStream":
            self.streaming = True
        elif kind == "StopStream":
            if self.delayed_stream_stop:
                import asyncio
                asyncio.get_running_loop().call_later(0.15, setattr, self, "streaming", False)
            else:
                self.streaming = False
        elif kind == "GetStreamStatus":
            return {"outputActive": self.streaming}
        elif kind == "GetSourceScreenshot" and data.get("imageFormat") == "bmp":
            return {"imageData": "data:image/bmp;base64," + base64.b64encode(bmp_image(luma=self.camera_luma)).decode()}
        elif kind == "GetSourceScreenshot":
            return {"imageData": "data:image/jpeg;base64," + base64.b64encode(TINY_JPEG).decode()}
        return {}
