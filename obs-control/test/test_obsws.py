# SPDX-License-Identifier: Apache-2.0
"""OBS output-stop event barrier tests."""
import json
import unittest
from unittest import mock

from malrunar_obs.obsws import ObsClient


class FakeSocket:
    def __init__(self, incoming):
        self.incoming = iter(incoming)
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))

    async def recv(self):
        return json.dumps(next(self.incoming))


def event(state):
    return {"op": 5, "d": {"eventType": "StreamStateChanged", "eventData": {"outputState": state}}}


def response(request_id, result=True):
    return {"op": 7, "d": {"requestId": request_id, "requestStatus": {"result": result}}}


class StopBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, incoming):
        socket = FakeSocket(incoming)
        client = ObsClient("ws://unused")

        class Session:
            async def __aenter__(self): return socket
            async def __aexit__(self, *args): return False

        with mock.patch.object(client, "session", return_value=Session()) as session:
            await client.stop_stream_and_wait()
        session.assert_called_once_with(event_subscriptions=64)
        self.assertEqual(socket.sent[0]["op"], 6)
        self.assertEqual(socket.sent[0]["d"]["requestType"], "StopStream")
        return socket.sent[0]["d"]["requestId"]

    async def test_event_before_response_is_consumed(self):
        socket = FakeSocket([])
        client = ObsClient("ws://unused")
        class Session:
            async def __aenter__(self): return socket
            async def __aexit__(self, *args): return False
        with mock.patch.object(client, "session", return_value=Session()):
            # The request ID is generated at send, so enqueue the matching response then
            # prepend the event in a socket that can observe the outgoing request.
            original_send = socket.send
            async def send(value):
                await original_send(value)
                rid = socket.sent[-1]["d"]["requestId"]
                socket.incoming = iter([event("OBS_WEBSOCKET_OUTPUT_STOPPED"), response(rid)])
            socket.send = send
            await client.stop_stream_and_wait()
        self.assertEqual(socket.sent[0]["d"]["requestType"], "StopStream")

    async def test_output_active_false_is_not_a_stop_barrier(self):
        # A stopped event is still required even if status-like data says inactive.
        socket = FakeSocket([])
        client = ObsClient("ws://unused")
        class Session:
            async def __aenter__(self): return socket
            async def __aexit__(self, *args): return False
        with mock.patch.object(client, "session", return_value=Session()):
            async def send(value):
                await FakeSocket.send(socket, value)
                rid = socket.sent[-1]["d"]["requestId"]
                socket.incoming = iter([{"op": 7, "d": {"requestId": rid, "requestStatus": {"result": True}, "responseData": {"outputActive": False}}}, event("OBS_WEBSOCKET_OUTPUT_STOPPED")])
            socket.send = send
            await client.stop_stream_and_wait()


if __name__ == "__main__":
    unittest.main()
