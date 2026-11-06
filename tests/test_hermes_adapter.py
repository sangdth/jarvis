import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from fakes import make
from jarvis.hermes_adapter import HermesAdapter


class TestHermesAdapter(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.adapter = make(HermesAdapter, "hermes")

    async def asyncTearDown(self) -> None:
        await self.adapter._http.aclose()

    async def test_tasks_share_one_session(self) -> None:
        post = AsyncMock(return_value=MagicMock(json=MagicMock(side_effect=[{"run_id": "run_1"}, {"run_id": "run_2"}])))
        with patch.object(self.adapter._http, "post", new=post):
            self.assertTrue(await self.adapter.send_task("Check my calendar"))
            self.assertTrue(await self.adapter.send_task("Move it to 3pm"))
        self.assertEqual(post.call_args_list[0].kwargs["json"], {"input": "Check my calendar"})
        self.assertEqual(post.call_args_list[1].kwargs["json"], {"input": "Move it to 3pm", "session_id": "run_1"})
        self.assertEqual(self.adapter._runs.qsize(), 2)

    async def test_approval_is_read_out_and_answered_once(self) -> None:
        await self.adapter._handle_event(
            {"event": "approval.request", "run_id": "run_1", "request_id": "apr_1", "command": "rm -rf build"}
        )
        self.adapter._surface.assert_awaited_once_with("needs approval: rm -rf build")
        post = AsyncMock(return_value=MagicMock(is_success=True))
        with patch.object(self.adapter._http, "post", new=post):
            self.assertTrue(await self.adapter.answer("yes, go ahead"))
            self.assertFalse(await self.adapter.answer("yes"))  # nothing pending any more
        self.assertEqual(post.call_args.args[0], "/v1/runs/run_1/approval")
        self.assertEqual(post.call_args.kwargs["json"], {"choice": "once", "request_id": "apr_1"})

    async def test_stop_reaches_a_queued_run_after_the_one_before_ends(self) -> None:
        post = AsyncMock(return_value=MagicMock(json=MagicMock(side_effect=[{"run_id": "run_1"}, {"run_id": "run_2"}])))
        with patch.object(self.adapter._http, "post", new=post):
            await self.adapter.send_task("X")
            await self.adapter.send_task("Y")
        watched = []

        async def watch(run_id: str) -> bool:
            watched.append(run_id)
            if run_id == "run_2":
                await asyncio.sleep(3600)  # still running
            return True

        with patch.object(self.adapter, "_watch", new=watch):
            loop = asyncio.create_task(self.adapter._run_loop())
            await asyncio.sleep(0.05)
            self.assertEqual(watched, ["run_1", "run_2"])
            self.assertEqual(self.adapter._run_id, "run_2")  # run_1 ending did not wipe it
            self.assertTrue(self.adapter.busy)
            loop.cancel()

    async def test_stop_also_cancels_queued_runs(self) -> None:
        self.adapter._run_id = "run_1"
        await self.adapter._runs.put("run_2")
        post = AsyncMock(return_value=MagicMock(is_success=True))
        with patch.object(self.adapter._http, "post", new=post):
            self.assertTrue(await self.adapter.interrupt())
        self.assertEqual([c.args[0] for c in post.await_args_list], ["/v1/runs/run_1/stop", "/v1/runs/run_2/stop"])
        self.assertTrue(self.adapter._runs.empty())  # the queued run will not be watched or retold

    async def test_failed_approval_reply_keeps_the_request(self) -> None:
        await self.adapter._handle_event({"event": "approval.request", "run_id": "run_1", "command": "rm x"})
        with patch.object(self.adapter._http, "post", new=AsyncMock(side_effect=httpx.ConnectError("down"))):
            self.assertFalse(await self.adapter.answer("yes"))
        self.assertIsNotNone(self.adapter._pending_approval)

    async def test_dropped_stream_falls_back_to_run_status(self) -> None:
        get = AsyncMock(return_value=MagicMock(json=MagicMock(return_value={"status": "completed", "output": "Done."})))
        with (
            patch.object(self.adapter._http, "stream", side_effect=httpx.ReadError("dropped")),
            patch.object(self.adapter._http, "get", new=get),
        ):
            self.assertTrue(await self.adapter._watch("run_1"))
        get.assert_awaited_once_with("/v1/runs/run_1")
        self.adapter._surface.assert_awaited_once_with("Done.", is_report=True)

        get.return_value = MagicMock(json=MagicMock(return_value={"status": "running"}))
        with (
            patch.object(self.adapter._http, "stream", side_effect=httpx.ReadError("dropped")),
            patch.object(self.adapter._http, "get", new=get),
        ):
            self.assertFalse(await self.adapter._watch("run_1"))  # still running: look again later

        self.adapter._surface.reset_mock()
        with (
            patch.object(self.adapter._http, "stream", side_effect=httpx.ReadError("dropped")),
            patch.object(self.adapter._http, "get", new=AsyncMock(return_value=MagicMock(status_code=404))),
        ):
            self.assertTrue(await self.adapter._watch("run_1"))  # Hermes forgot the run: stop waiting on it
        self.adapter._surface.assert_awaited_once()

    async def test_run_end_events(self) -> None:
        await self.adapter._handle_event({"event": "run.cancelled", "run_id": "run_1"})
        self.adapter._surface.assert_not_awaited()  # the user stopped it
        await self.adapter._handle_event({"event": "run.completed", "run_id": "run_1", "output": "Booked."})
        self.adapter._surface.assert_awaited_with("Booked.", is_report=True)
        await self.adapter._handle_event({"event": "run.failed", "run_id": "run_1", "error": "no network"})
        self.adapter._surface.assert_awaited_with("The turn failed. no network", is_report=True)


if __name__ == "__main__":
    unittest.main()
