from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from loguru import logger

from jarvis.agent_adapter import BaseAgentAdapter, WatchedAgentConfig, is_approval
from jarvis.config import settings
from jarvis.state import JarvisState


class HermesAdapter(BaseAgentAdapter):
    """
    Drives Hermes Agent through its API server (`hermes gateway`, API_SERVER_ENABLED=true).
    Each task is a run in one ongoing Hermes session. Hermes's own guardian model decides which commands
    need a human; those arrive as `approval.request`. A question Hermes asks ends its turn, so the user's
    reply goes back as the next task in the same session.
    """

    _DONE = {"run.completed", "run.failed", "run.cancelled"}

    def __init__(self, config: WatchedAgentConfig, state: JarvisState) -> None:
        super().__init__(config, state)
        self.base_url = (config.url or "http://127.0.0.1:8642").rstrip("/")
        headers = {"Authorization": f"Bearer {settings.hermes_api_key}"} if settings.hermes_api_key else {}
        self._http = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=10.0)
        self._session_id: str | None = None  # Hermes names a session after its first run
        self._run_id: str | None = None  # the run in progress
        self._runs: asyncio.Queue[str] = asyncio.Queue()
        self._pending_approval: dict[str, Any] | None = None

    async def _run_loop(self) -> None:
        while True:
            run_id = await self._runs.get()
            self._run_id = run_id  # set and cleared only here, so a queued task can't be wiped by the run before it
            while not await self._watch(run_id):
                await asyncio.sleep(5.0)  # stream dropped mid-run; the run goes on in Hermes, so look again
            self._run_id = None

    async def _watch(self, run_id: str) -> bool:
        """Follow one run's events; True once it finished. On a dropped stream, ask the run's status instead."""
        try:
            async with self._http.stream("GET", f"/v1/runs/{run_id}/events", timeout=httpx.Timeout(10.0, read=None)) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line.removeprefix("data:"))
                    except json.JSONDecodeError:
                        continue
                    await self._handle_event(event)
                    if event.get("event") in self._DONE:
                        return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[{self.config.id}] Hermes event stream for {run_id} dropped ({exc!r}).")
        try:
            resp = await self._http.get(f"/v1/runs/{run_id}")
            if resp.status_code == 404:  # gone (e.g. Hermes restarted): no result will ever come
                await self._surface("The task was lost: Hermes no longer knows this run.", is_report=True)
                return True
            resp.raise_for_status()
            run = resp.json()
        except httpx.HTTPError:
            return False
        if run.get("status") in ("completed", "failed", "cancelled"):
            await self._handle_event({**run, "event": f"run.{run['status']}"})
            return True
        return False

    async def _handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("event", "")
        if kind == "approval.request":
            self._pending_approval = event
            what = event.get("command") or event.get("description") or "a tool call"
            await self._surface(f"needs approval: {what}")
        elif kind == "approval.responded":
            self._pending_approval = None  # answered here or in another Hermes client
            self._question_settled()
        elif kind == "run.completed" and event.get("output"):
            await self._surface(event["output"], is_report=True)
        elif kind == "run.failed":
            await self._surface(f"The turn failed. {event.get('error', '')}".strip(), is_report=True)
        # run.cancelled: the user stopped it, so it is not news.

    async def send_task(self, task: str) -> bool:
        body: dict[str, Any] = {"input": task}
        if self._session_id:
            body["session_id"] = self._session_id
        try:
            resp = await self._http.post("/v1/runs", json=body)
            resp.raise_for_status()
            run_id = resp.json()["run_id"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.error(f"Failed to send task to Hermes: {exc}")
            return False
        self._session_id = self._session_id or run_id  # a new session is keyed by its first run
        await self._runs.put(run_id)
        return True

    async def answer(self, answer: str) -> bool:
        if self._pending_approval is None:
            return False
        request = self._pending_approval
        body: dict[str, Any] = {"choice": "once" if is_approval(answer) else "deny"}
        if request.get("request_id"):
            body["request_id"] = request["request_id"]
        try:
            resp = await self._http.post(f"/v1/runs/{request['run_id']}/approval", json=body)
            if resp.is_success:  # cleared only once Hermes took it, so a network error leaves it answerable
                self._pending_approval = None
                self._question_settled()
            return resp.is_success
        except httpx.HTTPError as exc:
            logger.error(f"Failed to answer Hermes: {exc}")
            return False

    @property
    def busy(self) -> bool:
        return self._run_id is not None or not self._runs.empty()

    async def interrupt(self) -> bool:
        """Stop the run in progress and every run queued behind it (Hermes queues them per session)."""
        queued = []
        while not self._runs.empty():
            queued.append(self._runs.get_nowait())
        run_ids = [r for r in (self._run_id, *queued) if r]
        if not run_ids:
            return False
        stopped = False
        for run_id in run_ids:
            try:
                resp = await self._http.post(f"/v1/runs/{run_id}/stop")
                stopped = stopped or resp.is_success
            except httpx.HTTPError as exc:
                logger.error(f"Failed to stop Hermes run {run_id}: {exc}")
        if stopped:
            self._question_settled()
        return stopped

    async def stop(self) -> None:
        await super().stop()
        await self._http.aclose()
