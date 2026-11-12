import asyncio
import json
import unittest
from unittest import mock

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from jarvis import meet_voice
from jarvis.meet_voice import MeetVoice


class TestMeetVoice(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.enterContext(mock.patch.object(meet_voice, "PORT", 0))
        self.voice = MeetVoice()
        self.voice.rate = 24000
        await self.voice.start()
        assert self.voice._server is not None
        self.port = self.voice._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        await self.voice.stop()

    async def test_meet_tab_gets_rate_then_speech_and_disconnects_while_silent(self) -> None:
        async with connect(f"ws://127.0.0.1:{self.port}", origin=meet_voice.ORIGIN) as ws:
            self.assertEqual(json.loads(await ws.recv()), {"rate": 24000})
            self.voice.send(b"\x01\x00\x02\x00")
            self.assertEqual(await asyncio.wait_for(ws.recv(), 1), b"\x01\x00\x02\x00")
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.voice._clients), 0)

    async def test_other_sites_cannot_listen(self) -> None:
        with self.assertRaises(InvalidStatus):
            async with connect(f"ws://127.0.0.1:{self.port}", origin="https://evil.example"):
                pass


if __name__ == "__main__":
    unittest.main()
