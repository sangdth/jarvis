from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from jarvis.agent_adapter import WatchedAgentConfig, describe_fields, form_answer, is_approval
from jarvis.config import settings
from jarvis.sessions import AgentSession, CodingSessionAdapter
from jarvis.state import JarvisState


def _request_of(data: dict[str, Any]) -> dict[str, Any]:
    """OpenCode wraps requests in the event data (e.g. `data.form`); return the object holding id and sessionID."""
    return next((v for v in data.values() if isinstance(v, dict) and "id" in v and "sessionID" in v), data)


DEFAULT_URL = "http://127.0.0.1:4096"
_HANDLED_EVENTS = (
    "permission.asked",
    "permission.replied",
    "form.created",
    "form.replied",
    "form.cancelled",
    "session.text.ended",
    "session.execution.interrupted",
    "session.execution.succeeded",
    "session.execution.failed",
)


def discover_service() -> tuple[str, str] | None:
    """
    The OpenCode 2 service registered on this Mac (`opencode serve --service`): its url and password, or None.
    Trusted only as Hex trusts it: the user's own file, unreadable to others, naming a loopback url, so a planted
    file can never send the password off the machine. Not followed through a symlink, and not used once its process
    is gone: a crashed OpenCode leaves the file, and another program may hold the port by now.
    shortcut: service.json only, not service-<channel>.json; add the channels when a beta OpenCode is in use.
    """
    path = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "opencode" / "service.json"
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as file:
            info = os.fstat(file.fileno())
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                return None
            data = json.load(file)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    url, password, pid = data.get("url"), data.get("password"), data.get("pid")
    if not isinstance(url, str) or not isinstance(password, str) or not isinstance(pid, int):
        return None
    try:
        os.kill(pid, 0)  # signal 0 only checks that the process exists
    except PermissionError:
        pass  # it exists, under another user
    except OSError:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return None
    return url.rstrip("/"), password



class OpenCodeSSEAdapter(CodingSessionAdapter):
    """
    Talks to an OpenCode v2 server: events over SSE at `/api/event`, actions over `/api`. Auth: HTTP Basic, user
    `opencode`. With OPENCODE_SERVER_PASSWORD or a custom `url`, that server; otherwise the service OpenCode
    registered (`discover_service`). That one serves every folder, so Jarvis keeps to the sessions in `cwd`.
    """

    def __init__(
        self,
        config: WatchedAgentConfig,
        state: JarvisState,
    ) -> None:
        super().__init__(config, state)
        self.base_url = (config.url or DEFAULT_URL).rstrip("/")
        # A password or url set by the user pins their own `opencode serve`; otherwise find the registered service.
        self._pinned = bool(settings.opencode_server_password) or config.url not in (None, DEFAULT_URL)
        # The folder Jarvis's sessions live in, set once on the shared service; None: the server serves one folder.
        self._directory: str | None = None
        self._ours: dict[str, bool] = {}  # session id -> in self._directory, looked up once
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
                self._use_service()  # each attempt: a restarted OpenCode registers a new port
                # The server sends a heartbeat every 15 s; 45 s of silence means the stream is dead.
                timeout = httpx.Timeout(10.0, read=45.0)
                async with self._http.stream("GET", "/api/event", timeout=timeout) as response:
                    response.raise_for_status()
                    logger.info(f"Connected to OpenCode event stream at {self.base_url}/api/event")
                    last_error = None
                    await self._resync()
                    async for line in response.aiter_lines():
                        raw_data = line.removeprefix("data:").strip() if line.startswith("data:") else ""
                        if not raw_data or not any(e in raw_data for e in _HANDLED_EVENTS):
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

    def _use_service(self) -> None:
        """Point at the registered OpenCode service, unless .env or agents.json pins a server."""
        found = None if self._pinned else discover_service()
        if found is None:
            return
        url, password = found
        self.base_url = self._http.base_url = url
        self._http.auth = ("opencode", password)
        self._directory = self.cwd

    def _scope(self) -> dict[str, str]:
        """The header that scopes a listing to Jarvis's folder on the shared service (it serves `$HOME` by default)."""
        return {"x-opencode-directory": self._directory} if self._directory else {}

    async def _is_ours(self, session_id: str) -> bool:
        """
        Whether a session belongs to Jarvis. The shared service streams every session on the Mac, and its requests
        name only a session id; without this, Jarvis would read out and approve tool calls in other projects.
        """
        if self._directory is None or session_id == self._session_id:
            return True
        if session_id not in self._ours:
            try:
                resp = await self._http.get(f"/api/session/{session_id}")
                resp.raise_for_status()
                folder = resp.json()["data"]["location"]["directory"]
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                logger.debug(f"[{self.config.id}] Could not look up session {session_id} ({exc!r}); ignored.")
                return False  # not cached: a passing network error must not hide a session for good
            if len(self._ours) >= 256:
                self._ours.pop(next(iter(self._ours)))
            self._ours[session_id] = folder == self._directory
        return self._ours[session_id]

    async def _resync(self) -> None:
        """Events are not replayed, so pick up permissions and questions that were asked while disconnected."""
        seen: dict[str, set[str]] = {}
        for path, on_request in (("/api/permission/request", self._on_permission), ("/api/form", self._on_form)):
            resp = await self._http.get(path, headers=self._scope())
            resp.raise_for_status()
            active: set[str] = set()
            for request in resp.json().get("data", []):
                if self._session_id and request.get("sessionID") != self._session_id:
                    continue
                active.add(request["id"])
                await on_request(request)
            seen[path] = active
        if self._pending_permission and self._pending_permission.get("id") not in seen["/api/permission/request"]:
            self._pending_permission = None
            self._question_settled()
        if self._pending_form and self._pending_form.get("id") not in seen["/api/form"]:
            self._pending_form = None
            self._question_settled()
        if self._running and self._session_id:
            try:
                resp = await self._http.get(f"/api/session/{self._session_id}", headers=self._scope())
                resp.raise_for_status()
                data = resp.json().get("data")
                time_info = data.get("time") if isinstance(data, dict) else None
                if isinstance(time_info, dict) and (time_info.get("idle") or not time_info.get("busy")):
                    self._running = False
            except httpx.HTTPError as exc:
                logger.debug(f"[{self.config.id}] Could not check session {self._session_id} status ({exc!r}).")

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
        if ev_type not in _HANDLED_EVENTS:
            return
        data = event.get("data") or {}
        session_id = data.get("sessionID") or _request_of(data).get("sessionID")
        if session_id and not await self._is_ours(session_id):
            return  # another folder's session on the shared service
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
            if ev_type.endswith("failed"):
                await self._surface(f"The turn failed. {text}".strip(), is_report=True)
            elif text:
                await self._surface(text, is_report=True)

    async def _create_session(self) -> None:
        """Start a session in the agent's folder (a pinned server uses its own) and send tasks to it."""
        body: dict[str, Any] = {"title": "Jarvis"}
        if self._directory:  # the shared service would put it in $HOME; an absolute path is required
            body["location"] = {"directory": self._directory}
        resp = await self._http.post("/api/session", json=body)
        resp.raise_for_status()
        self._session_id = resp.json()["data"]["id"]

    async def send_task(self, task: str) -> bool:
        try:
            if not self._session_id:  # no session seen yet
                await self._create_session()
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

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]:
        self._use_service()  # before start_all (`jarvis connect`) the watch loop has not found the service yet
        headers = {"x-opencode-directory": cwd} if cwd else {}  # no header: every folder the service serves
        resp = await self._http.get("/api/session", headers=headers)
        resp.raise_for_status()
        found = [
            AgentSession(
                id=s["id"],
                title=s.get("title") or "(untitled)",
                cwd=(s.get("location") or {}).get("directory") or self.cwd,
                updated_at=((s.get("time") or {}).get("updated") or 0) / 1000,
            )
            for s in resp.json().get("data", [])
        ]
        return sorted(found, key=lambda s: s.updated_at, reverse=True)[:limit]

    def _move_to(self, folder: str) -> None:
        """Tasks and events from now on belong to this folder: forget what was asked and looked up in the old one."""
        self.cwd = folder
        if self._directory is not None:  # the shared service; a pinned server serves one folder of its own
            self._directory = folder
        self._ours = {}
        if self._pending_permission or self._pending_form:
            self._pending_permission = self._pending_form = None
            self._question_settled()

    async def pick_session(self, session: AgentSession, connected: bool = False) -> bool:
        if self.busy:  # the running turn's result would come back from a session tasks no longer go to
            return False
        self._move_to(session.cwd)
        self._session_id, self.connected = session.id, connected
        try:
            await self._resync()  # a question that session is already waiting on is read out now
        except httpx.HTTPError as exc:
            logger.debug(f"[{self.config.id}] Resync after picking {session.id} failed ({exc!r}).")
        return True

    async def new_session(self, cwd: str | None = None) -> bool:
        if self.busy:
            return False
        self._move_to(cwd or self.cwd)
        self._session_id = None  # if making one fails, the next task makes it; it never goes to the old one
        try:
            # Made now, not with the next task: an event from another session in the folder would latch first.
            await self._create_session()
        except httpx.HTTPError as exc:
            logger.error(f"Failed to start an OpenCode session: {exc}")
            return False
        return True

    async def stop(self) -> None:
        await super().stop()
        await self._http.aclose()
