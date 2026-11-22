"""The voice harness's one client: talk to the brain, which owns routing, threads and every runner.

One HTTP/SSE connection per turn, plus a session picker for `jarvis connect`. The brain decides whether the
work is a one-shot `pi` answer, a T3 thread, a minimal Claude/OpenCode turn, or a board task; this file only
carries the words and the events. Approvals and questions are spoken verbatim, never through a model.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from jarvis_core.home import brain_token
from jarvis_core.projects import projects_dir
from jarvis_core.sessions import AgentSession
from jarvis_voice.agent_adapter import WatchedAgentConfig, is_approval
from jarvis_voice.config import settings
from jarvis_voice.sessions import CodingSessionAdapter
from jarvis_voice.state import JarvisState


class BrainClientAdapter(CodingSessionAdapter):
    """The brain as the voice's agent: it takes the words, and reports what a runner did."""

    _DONE = {"run.completed", "run.failed", "run.cancelled"}

    def __init__(self, config: WatchedAgentConfig, state: JarvisState) -> None:
        super().__init__(config, state)
        self.base_url = settings.endpoint.rstrip("/")
        token = (settings.brain_api_key or "").strip() or brain_token()
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(15.0, read=None),
        )
        self._session_id: str | None = None  # the voice-side conversation id the brain keeps
        self._pinned: dict[str, str] | None = None  # `jarvis connect`: one runner session for this run
        self.runner = "t3"  # the client whose sessions this picker lists: t3, claude or opencode
        self._fresh = False  # "new session" for the next task
        self._thread_id: str | None = None  # the thread the brain last sent work to, for saying it back
        self._title: str | None = None
        self._explicit_cwd: str | None = None  # a folder picked by voice; not the launch folder by default
        self._run_id: str | None = None
        self._runs: asyncio.Queue[str] = asyncio.Queue()
        self._pending: dict[str, Any] | None = None
        self._pending_outbox: dict[str, Any] | None = None
        self._seen_outbox: set[str] = set()
        self._events_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await super().start()
        if self._events_task is None or self._events_task.done():
            self._events_task = asyncio.create_task(
                self._events_loop(), name=f"events-{self.config.id}"
            )

    async def stop(self) -> None:
        if self._events_task is not None:
            self._events_task.cancel()
            try:
                await self._events_task
            except asyncio.CancelledError:
                pass
            self._events_task = None
        await super().stop()
        await self._http.aclose()

    # ------------------------------------------------------------------ outbox dispatcher

    async def _events_loop(self) -> None:
        """The daemon's own stream: items no run is waiting on, spoken under the same mute rules."""
        await self._drain_outbox()
        while True:
            try:
                async with self._http.stream("GET", "/v1/events") as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line.removeprefix("data:").strip())
                        except json.JSONDecodeError:
                            continue
                        if event.get("event") == "outbox.queued" and event.get("mode", "ASSIST") == "ASSIST":
                            await self._handle_outbox(event.get("outbox") or {})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"[{self.config.id}] event stream dropped ({exc!r})")
            await asyncio.sleep(5.0)
            await self._drain_outbox()

    async def _drain_outbox(self) -> None:
        """On connect or reconnect, speak what was queued while this Mac was asleep."""
        try:
            resp = await self._http.get("/v1/outbox", params={"status": "queued", "limit": "50"})
            resp.raise_for_status()
            for row in resp.json().get("outbox", []):
                await self._handle_outbox(row)
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug(f"[{self.config.id}] outbox drain skipped ({exc!r})")

    async def _handle_outbox(self, row: dict[str, Any]) -> None:
        outbox_id = str(row.get("id") or "")
        if not outbox_id or outbox_id in self._seen_outbox:
            return
        self._seen_outbox.add(outbox_id)
        if len(self._seen_outbox) > 500:
            self._seen_outbox = set(list(self._seen_outbox)[-250:])
        text = str(row.get("summary_spoken") or "").strip()
        if not text:
            return
        if row.get("kind") == "approval_request":
            # Keep it queued until it is answered, so a reconnect or the morning brief repeats it.
            self._pending_outbox = row
            await self._surface(text)
            return
        await self._surface(text, is_report=True)
        try:
            resp = await self._http.post(f"/v1/outbox/{outbox_id}/spoken")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug(f"[{self.config.id}] could not acknowledge outbox {outbox_id}: {exc!r}")
            self._seen_outbox.discard(outbox_id)

    # ------------------------------------------------------------------ watcher

    async def _run_loop(self) -> None:
        while True:
            run_id = await self._runs.get()
            self._run_id = run_id
            while not await self._watch(run_id):
                await asyncio.sleep(5.0)  # the stream dropped; the run goes on in the brain, so look again
            self._run_id = None

    async def _watch(self, run_id: str) -> bool:
        try:
            async with self._http.stream("GET", f"/v1/runs/{run_id}/events") as resp:
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
            logger.debug(f"[{self.config.id}] event stream for {run_id} dropped ({exc!r})")
        try:
            resp = await self._http.get(f"/v1/runs/{run_id}")
            if resp.status_code == 404:
                await self._surface("The task was lost: the brain no longer knows this run.", is_report=True)
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
        if kind in ("approval.request", "question.request"):
            self._pending = event
            await self._surface(str(event.get("description") or "needs approval"))
        elif kind in ("approval.responded", "question.answered"):
            self._pending = None
            self._question_settled()
        elif kind == "session.switched":
            self._thread_id = str(event.get("session_id") or "") or self._thread_id
            self._title = str(event.get("title") or "") or self._title
        elif kind == "run.completed" and event.get("output"):
            self._pending = None
            await self._surface(str(event["output"]), is_report=True)
        elif kind == "run.failed":
            self._pending = None
            await self._surface(f"The turn failed. {event.get('error', '')}".strip(), is_report=True)
        # run.cancelled: the user stopped it, so it is not news.

    # ------------------------------------------------------------------ work

    async def send_task(self, task: str) -> bool:
        body: dict[str, Any] = {"input": task}
        if self._session_id:
            body["session_id"] = self._session_id
        if self._pinned:
            body["pinned"] = self._pinned
            body["session"] = "continuing"
        elif self._fresh:
            body["session"] = "new"
        if self._explicit_cwd:
            body["cwd"] = self._explicit_cwd
        self._fresh = False
        try:
            resp = await self._http.post("/v1/runs", json=body)
            resp.raise_for_status()
            payload = resp.json()
            run_id = payload["run_id"]
            self._session_id = payload.get("session_id") or run_id
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.error(f"Failed to send task to the brain: {exc}")
            return False
        if not self.connected:
            self._pinned = None  # a picked session is for this task; the brain keeps its own binding after it
            self._explicit_cwd = None
        await self._runs.put(run_id)
        return True

    async def answer(self, answer: str) -> bool:
        if self._pending is not None and self._run_id is not None:
            request = self._pending
            body: dict[str, Any] = {"answer": answer}
            if request.get("request_id"):
                body["request_id"] = request["request_id"]
            try:
                resp = await self._http.post(f"/v1/runs/{self._run_id}/answer", json=body)
                if resp.is_success:
                    self._pending = None
                    self._question_settled()
                return resp.is_success
            except httpx.HTTPError as exc:
                logger.error(f"Failed to answer the brain: {exc}")
                return False
        if self._pending_outbox is not None:
            outbox_id = str(self._pending_outbox.get("id") or "")
            try:
                resp = await self._http.post(
                    f"/v1/outbox/{outbox_id}/resolve",
                    json={"approved": is_approval(answer)},
                )
                if resp.is_success:
                    self._pending_outbox = None
                    self._question_settled()
                    return True
                if resp.status_code == 404:
                    self._pending_outbox = None
                    self._question_settled()
                return False
            except httpx.HTTPError as exc:
                logger.error(f"Failed to resolve the brain's outbox approval: {exc}")
                return False
        return False

    @property
    def busy(self) -> bool:
        return self._run_id is not None or not self._runs.empty()

    async def interrupt(self) -> bool:
        queued: list[str] = []
        while not self._runs.empty():
            queued.append(self._runs.get_nowait())
        run_ids = [r for r in (self._run_id, *queued) if r]
        stopped = False
        for run_id in run_ids:
            try:
                resp = await self._http.post(f"/v1/runs/{run_id}/stop")
                stopped = stopped or resp.is_success
            except httpx.HTTPError as exc:
                logger.error(f"Failed to stop the brain run {run_id}: {exc}")
        if stopped:
            self._pending = None
            self._question_settled()
        return stopped

    # ------------------------------------------------------------------ sessions

    @property
    def session_id(self) -> str | None:
        return (self._pinned or {}).get("session_id") or self._thread_id

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]:
        params: dict[str, str] = {"runner": self.runner}
        if cwd:
            params["project"] = self._project_name(cwd)
        resp = await self._http.get("/v1/sessions", params=params)
        resp.raise_for_status()
        return [
            AgentSession(
                id=str(s["id"]), title=str(s.get("title") or "(untitled)"),
                cwd=str(s.get("cwd") or ""), branch=str(s.get("branch") or ""),
                running=bool(s.get("running")), updated_at=float(s.get("updated_at") or 0.0),
            )
            for s in resp.json().get("sessions", [])
        ][:limit]

    async def pick_session(self, session: AgentSession, connected: bool = False) -> bool:
        if self.busy and not connected:
            return False
        self._pinned = {"runner": self.runner, "session_id": session.id}
        self._fresh = False
        self.connected = connected
        self.cwd = session.cwd or self.cwd
        self._explicit_cwd = session.cwd or None
        self._thread_id, self._title = session.id, session.title
        return True

    async def new_session(self, cwd: str | None = None) -> bool:
        if self.busy:
            return False
        self._pinned = None
        self._fresh = True
        if cwd:
            self.cwd = cwd
            self._explicit_cwd = cwd
        return True

    @staticmethod
    def _project_name(cwd: str) -> str:
        _, root = projects_dir()
        try:
            return str(Path(cwd).resolve().relative_to(root.resolve()))
        except ValueError:
            return Path(cwd).name
