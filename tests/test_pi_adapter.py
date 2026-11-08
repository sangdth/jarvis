import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fakes import make
from jarvis import pi_adapter
from jarvis.pi_adapter import PiAdapter, install_extension, list_windows

HELLO = {"type": "hello", "session": "s1", "cwd": "/p/jarvis", "name": "", "first": "fix login", "busy": False}


class FakePi:
    """A Pi window's socket, as the extension serves it: hello on connect, then records both ways."""

    async def start(self, path: Path) -> None:
        self.received: asyncio.Queue[dict] = asyncio.Queue()
        self.writers: list[asyncio.StreamWriter] = []
        self.server = await asyncio.start_unix_server(self._client, path=str(path))

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        writer.write(json.dumps(HELLO).encode() + b"\n")
        while line := await reader.readline():
            await self.received.put(json.loads(line))

    async def say(self, record: dict) -> None:
        self.writers[-1].write(json.dumps(record).encode() + b"\n")
        await self.writers[-1].drain()

    async def stop(self) -> None:
        for w in self.writers:
            w.close()
        self.server.close()


async def until(check) -> None:
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


class TestPiAdapter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")  # a Unix socket path must stay short
        self.path = Path(self.tmp.name) / "1.sock"
        self.pi = FakePi()
        await self.pi.start(self.path)
        self.adapter = make(lambda cfg, state: PiAdapter(cfg, state, str(self.path)), "pi")
        await self.adapter.start()
        await until(lambda: self.adapter._writer is not None)

    async def asyncTearDown(self) -> None:
        await self.adapter.stop()
        await self.pi.stop()
        self.tmp.cleanup()

    async def test_task_goes_to_the_window_and_its_settled_turn_is_retold(self) -> None:
        self.assertTrue(await self.adapter.send_task("run the tests"))
        self.assertEqual(await self.pi.received.get(), {"type": "prompt", "text": "run the tests"})
        self.assertTrue(self.adapter.busy)
        await self.pi.say({"type": "busy"})
        await self.pi.say({"type": "settled", "aborted": False, "text": "All 12 tests pass."})
        await until(lambda: self.adapter._surface.await_count)
        self.adapter._surface.assert_awaited_once_with("All 12 tests pass.", is_report=True)
        self.assertFalse(self.adapter.busy)

    async def test_stop_sends_abort_and_the_stopped_turn_is_not_retold(self) -> None:
        await self.pi.say({"type": "busy"})
        await until(lambda: self.adapter.busy)
        self.assertTrue(await self.adapter.interrupt())
        self.assertEqual(await self.pi.received.get(), {"type": "abort"})
        self.assertFalse(self.adapter.busy)
        await self.pi.say({"type": "settled", "aborted": True, "text": "half done"})
        await asyncio.sleep(0.05)
        self.adapter._surface.assert_not_awaited()

    async def test_dialog_is_read_out_and_answered_in_the_window(self) -> None:
        self.assertFalse(await self.adapter.answer("yes"))  # nothing open
        await self.pi.say({"type": "prompt_start", "kind": "confirm", "title": "Allow rm -rf?"})
        await until(lambda: self.adapter._surface.await_count)
        self.adapter._surface.assert_awaited_once_with("is waiting for you in its window: Allow rm -rf?")
        self.assertEqual(await self.adapter.answer("yes"), "answer_in_pi_window")
        await self.pi.say({"type": "prompt_end"})
        await until(lambda: not self.adapter._dialog)
        self.adapter.state.active_agent_context.pop.assert_called_with("a1", None)  # speech check forgets it

    async def test_sessions_are_the_open_windows_and_a_pick_moves_to_that_socket(self) -> None:
        other = FakePi()
        await other.start(Path(self.tmp.name) / "2.sock")
        with patch.object(pi_adapter, "list_windows", new=lambda: list_windows(Path(self.tmp.name))):
            sessions = await self.adapter.list_sessions()
            self.assertEqual(await self.adapter.list_sessions("/elsewhere"), [])
        self.assertEqual([(s.id, s.title, s.cwd) for s in sessions], [("s1", "fix login", "/p/jarvis")] * 2)
        second = [{"session": "s1", "socket": str(Path(self.tmp.name) / "2.sock")}]
        with patch.object(pi_adapter, "list_windows", new=AsyncMock(return_value=second)):
            self.assertTrue(await self.adapter.pick_session(sessions[0], connected=True))
        await until(lambda: other.writers and self.adapter._writer is not None)
        self.assertTrue(await self.adapter.send_task("run the tests"))
        self.assertEqual(await other.received.get(), {"type": "prompt", "text": "run the tests"})  # the new window
        self.assertFalse(await self.adapter.new_session())  # a new Pi session is a window the user opens
        await other.stop()


class TestPiWindows(unittest.IsolatedAsyncioTestCase):
    async def test_lists_open_windows_and_deletes_dead_sockets(self) -> None:
        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            pi = FakePi()
            await pi.start(Path(tmp) / "1.sock")
            dead = Path(tmp) / "2.sock"
            dead.touch()  # a crashed window leaves its file behind
            windows = await list_windows(Path(tmp))
            await pi.stop()
        self.assertEqual(windows, [{**HELLO, "socket": str(Path(tmp) / "1.sock"), "mtime": windows[0]["mtime"]}])
        self.assertFalse(dead.exists())


class TestInstallExtension(unittest.TestCase):
    def test_copies_once_and_again_after_an_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "extensions"
            self.assertTrue(install_extension(target))
            self.assertFalse(install_extension(target))
            (target / "jarvis.ts").write_text("old", encoding="utf-8")
            self.assertTrue(install_extension(target))
            self.assertEqual((target / "jarvis.ts").read_text(), pi_adapter.EXTENSION.read_text())


if __name__ == "__main__":
    unittest.main()
