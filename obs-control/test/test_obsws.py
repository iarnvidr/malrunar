# SPDX-License-Identifier: Apache-2.0
"""Output stop notifications can precede native encoder cleanup."""
import asyncio
import unittest
from unittest import mock

import helpers  # noqa: F401 (puts the package on sys.path)
from fake_obs import FakeObs
from malrunar_obs.obsws import ObsClient, ObsError


class StopBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.obs = await FakeObs().start()
        self.client = ObsClient(self.obs.url)

    async def asyncTearDown(self):
        await self.obs.stop()

    async def test_stream_stopped_event_precedes_encoder_completion(self):
        self.obs.streaming = True
        self.obs.delayed_stream_stop = True
        await self.client.stop_stream_and_wait()
        self.assertFalse(self.obs.streaming, "STOPPED alone does not mean encoder cleanup finished")
        self.assertEqual(len(self.obs.names("StopStream")), 1)
        self.assertGreater(len(self.obs.names("GetStreamStatus")), 1)

    async def test_record_stopped_event_precedes_encoder_completion(self):
        self.obs.recording = True
        self.obs.delayed_record_stop = True
        await self.client.stop_record_and_wait()
        self.assertFalse(self.obs.recording)
        self.assertEqual(len(self.obs.names("StopRecord")), 1)
        self.assertGreater(len(self.obs.names("GetRecordStatus")), 1)

    async def test_already_inactive_outputs_need_no_new_event(self):
        await self.client.stop_stream_and_wait()
        await self.client.stop_record_and_wait()
        self.assertEqual(len(self.obs.names("GetStreamStatus")), 1)
        self.assertEqual(len(self.obs.names("GetRecordStatus")), 1)

    async def test_stop_failure_is_not_hidden(self):
        self.obs.streaming = True
        self.obs.fail["StopStream"] = "encoder refused"
        with self.assertRaisesRegex(ObsError, "encoder refused"):
            await self.client.stop_stream_and_wait()

    async def test_missing_stopped_notification_has_a_deadline(self):
        self.obs.streaming = True
        self.obs.suppress_stop_events = True
        real_timeout = asyncio.timeout
        with mock.patch("malrunar_obs.obsws.asyncio.timeout", side_effect=lambda seconds: real_timeout(0.05 if seconds == 30 else seconds)):
            with self.assertRaisesRegex(ObsError, "encoder teardown"):
                await self.client.stop_stream_and_wait()


if __name__ == "__main__":
    unittest.main()
