from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from loguru import logger

from jarvis.agent_adapter import BaseAgentAdapter, WatchedAgentConfig, describe_fields, form_answer, is_approval
from jarvis.config import settings
from jarvis.state import JarvisState


def _request_of(data: dict[str, Any]) -> dict[str, Any]:
    """OpenCode wraps requests in the event data (e.g. `data.form`); return the object holding id and sessionID."""
    return next((v for v in data.values() if isinstance(v, dict) and "id" in v and "sessionID" in v), data)



class OpenCodeSSEAdapter(BaseAgentAdapter):
    """
    Talks to an OpenCode v2 server (`opencode serve`): events over SSE at `/api/event`, actions over `/api`.
    Auth: HTTP Basic, user `opencode`, password from OPENCODE_SERVER_PASSWORD.
    """

    def __init__(
        self,
        config: WatchedAgentConfig,
        state: JarvisState,
    ) -> None:
        super().__init__(config, state)
        self.base_url = (config.url or "http://127.0.0.1:4096").rstrip("/")
        auth = ("opencode", settings.opencode_server_password) if settings.opencode_server_password else None
        self._http = httpx.AsyncClient(base_url=self.base_url, auth=auth, timeout=10.0)
        self._session_id: str | None = None  # latest session seen, or the one Jarvis created
        self._pending_permission: dict[str, Any] | None = None
        self._pending_form: dict[str, Any] | None = None  # a question OpenCode is waiting on
        self._spoken: set[str] = set()  # request ids already read out, so a resync doesn't repeat them
        self._last_text: dict[str, str] = {}  # session id -> latest finished assistant text
        self._running = False  # a task Jarvis sent has not finished yet
        self._stopped = False  # Jarvis stopped the turn; its interrupted event is not news

    async def _run_loop(self) -> None:
        last_error: str | None = None  # logged once; the same failure every 10 s stays quiet
        while True:
            try:
                # The server sends a heartbeat every 15 s; 45 s of silence means the stream is dead.
                timeout = httpx.Timeout(10.0, read=45.0)
                async with self._http.stream("GET", "/api/event", timeout=timeout) as response:
                    response.raise_for_status()
                    logger.info(f"Connected to OpenCode event stream at {self.base_url}/api/event")
                    last_error = None
                    await self._resync()
                    async for line in response.aiter_lines():
                        raw_data = line.removeprefix("data:").strip() if line.startswith("data:") else ""
                        if not raw_data:
                            continue
                        try:
                            event = json.loads(raw_data)
                        except json.JSONDecodeError:
                            continue
                        await self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Reconnect if `opencode serve` is not started yet, dropped the stream, or went silent
                if repr(exc) != last_error:
                    last_error = repr(exc)
                    logger.debug(f"[{self.config.id}] OpenCode stream unavailable ({exc!r}); retrying every 10s.")
                await asyncio.sleep(10.0)

    async def _resync(self) -> None:
        """Events are not replayed, so pick up permissions and questions that were asked while disconnected."""
        for path, on_request in (("/api/permission/request", self._on_permission), ("/api/form", self._on_form)):
            resp = await self._http.get(path)
            resp.raise_for_status()
            for request in resp.json().get("data", []):
                await on_request(request)

    async def _on_permission(self, request: dict[str, Any]) -> None:
        self._pending_permission = request
        self._session_id = self._session_id or request.get("sessionID")
        if request["id"] not in self._spoken:
            self._spoken.add(request["id"])
            what = " ".join([request.get("action", "")] + request.get("resources", [])).strip()
            await self._surface(f"needs approval: {what or 'a tool call'}")

    async def _on_form(self, form: dict[str, Any]) -> None:
        self._pending_form = form
        self._session_id = self._session_id or form.get("sessionID")
        if form["id"] not in self._spoken:
            self._spoken.add(form["id"])
            await self._surface(f"asks: {describe_fields(form.get('fields', []))}")

    async def _handle_event(self, event: dict[str, Any]) -> None:
        ev_type = event.get("type", "")
        data = event.get("data") or {}
        session_id = data.get("sessionID")
        # One session: the first seen, or the one Jarvis created. Sub-agents run in child sessions,
        # and their "done" is not the turn being done.
        self._session_id = self._session_id or session_id
        if ev_type.startswith("session.") and session_id and session_id != self._session_id:
            return

        if ev_type == "permission.asked":
            await self._on_permission(_request_of(data))
        elif ev_type == "form.created":
            await self._on_form(_request_of(data))
        elif ev_type in ("permission.replied", "form.replied", "form.cancelled"):
            # Answered somewhere else (e.g. the OpenCode UI): stop holding it as pending.
            request_id = data.get("requestID") or _request_of(data).get("id")
            if self._pending_permission and self._pending_permission.get("id") == request_id:
                self._pending_permission = None
                self._question_settled()
            if self._pending_form and self._pending_form.get("id") == request_id:
                self._pending_form = None
                self._question_settled()
        elif ev_type == "session.text.ended" and session_id:
            self._last_text[session_id] = data.get("text", "")
        elif ev_type == "session.execution.interrupted" and session_id:
            if self._stopped:  # Jarvis stopped it; a task sent since then is still running
                self._stopped = False
            else:
                self._running = False  # stopped in the OpenCode UI
            self._last_text.pop(session_id, None)
        elif ev_type in ("session.execution.succeeded", "session.execution.failed") and session_id:
            self._running = False
            text = self._last_text.pop(session_id, "")
            if text:
                await self._surface(("The turn failed. " if ev_type.endswith("failed") else "") + text, is_report=True)

    async def send_task(self, task: str) -> bool:
        try:
            if not self._session_id:
                # No session seen yet: start one in the folder `opencode serve` runs in.
                resp = await self._http.post("/api/session", json={"title": "Jarvis"})
                resp.raise_for_status()
                self._session_id = resp.json()["data"]["id"]
            resp = await self._http.post(f"/api/session/{self._session_id}/prompt", json={"text": task})
            self._running = resp.is_success
            return resp.is_success
        except httpx.HTTPError as exc:
            logger.error(f"Failed to send task to OpenCode: {exc}")
            return False

    async def answer(self, answer: str) -> bool:
        try:
            # Clear a request only once OpenCode took the reply, so a network error leaves it answerable.
            if self._pending_permission is not None:
                request = self._pending_permission
                resp = await self._http.post(
                    f"/api/session/{request['sessionID']}/permission/{request['id']}/reply",
                    json={"decision": "once" if is_approval(answer) else "reject"},
                )
                if resp.is_success:
                    self._pending_permission = None
                    self._question_settled()
                return resp.is_success
            if self._pending_form is not None:
                form = self._pending_form
                resp = await self._http.post(
                    f"/api/session/{form['sessionID']}/form/{form['id']}/reply",
                    json={"answer": form_answer(form.get("fields", []), answer)},
                )
                if resp.is_success:
                    self._pending_form = None
                    self._question_settled()
                return resp.is_success
            return False
        except httpx.HTTPError as exc:
            logger.error(f"Failed to answer OpenCode: {exc}")
            return False

    @property
    def busy(self) -> bool:
        return self._running

    async def interrupt(self) -> bool:
        if not self._session_id:
            return False
        try:
            resp = await self._http.post(f"/api/session/{self._session_id}/interrupt")
            if resp.is_success:
                self._question_settled()
                self._stopped, self._running = self._running, False
            return resp.is_success
        except httpx.HTTPError as exc:
            logger.error(f"Failed to interrupt OpenCode: {exc}")
            return False

    async def stop(self) -> None:
        await super().stop()
        await self._http.aclose()
