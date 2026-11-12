import asyncio
import json
import unittest
from unittest import mock

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from jarvis import meet_voice
from jarvis.meet_voice import MeetVoice

TEST_PORT = 47199


class TestMeetVoice(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.enterContext(mock.patch.object(meet_voice, "PORT", TEST_PORT))
        self.voice = MeetVoice()
        self.voice.rate = 24000
        await self.voice.start()

    async def asyncTearDown(self) -> None:
        await self.voice.stop()

    async def test_meet_tab_gets_rate_then_speech(self) -> None:
        async with connect(f"ws://127.0.0.1:{TEST_PORT}", origin=meet_voice.ORIGIN) as ws:
            self.assertEqual(json.loads(await ws.recv()), {"rate": 24000})
            self.voice.send(b"\x01\x00\x02\x00")
            self.assertEqual(await asyncio.wait_for(ws.recv(), 1), b"\x01\x00\x02\x00")

    async def test_other_sites_cannot_listen(self) -> None:
        with self.assertRaises(InvalidStatus):
            async with connect(f"ws://127.0.0.1:{TEST_PORT}", origin="https://evil.example"):
                pass


if __name__ == "__main__":
    unittest.main()
