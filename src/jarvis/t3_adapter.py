from __future__ import annotations

import asyncio
import itertools
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus

from jarvis.agent_adapter import WatchedAgentConfig, describe_fields, form_answer, is_approval, question_fields
from jarvis.sessions import AgentSession, CodingSessionAdapter
from jarvis.soul import load_soul
from jarvis.state import JarvisState

T3_HOME = Path.home() / ".t3"
PROTOCOL = 2  # T3's ORCHESTRATION_PROTOCOL_VERSION; the server answers 426 to any other
_ACTIVE = {"preparing", "queued", "starting", "running", "waiting"}
_ASKS = {"approval_request", "user_input_request"}
PROJECTS_LABEL = load_soul().projects_dir  # as written in SOUL.md, e.g. "~/Projects"; also what the model sees
PROJECTS = Path(PROJECTS_LABEL).expanduser()  # every project of the user's lives here, one or two folders deep
SYNC_MASTER = "Sync with the master branch"


def find_project(projects: list[dict[str, Any]], cwd: str) -> dict[str, Any] | None:
    """The T3 project whose folder holds cwd; the deepest one when projects are nested."""
    folder = Path(cwd).expanduser().resolve()
    holding = [
        p for p in projects
        if p.get("workspaceRoot") and not p.get("archivedAt")
        and folder.is_relative_to(Path(p["workspaceRoot"]).expanduser().resolve())
    ]
    return max(holding, key=lambda p: len(p["workspaceRoot"]), default=None)


def _last_touched(repo: Path) -> float:
    """When git last wrote the repo's index (commit, checkout, add, pull); a new repo has only `.git`."""
    git = repo / ".git"
    return (git / "index").stat().st_mtime if (git / "index").exists() else git.stat().st_mtime


def list_projects(root: Path = PROJECTS, *, sort: bool = True) -> list[str]:
    """Git repos one or two folders under root, as relative paths ("HuggerHustle/platform-v2"), newest first."""
    found: list[str] = []
    for top in sorted(root.iterdir()) if root.is_dir() else []:
        if top.name.startswith(".") or not top.is_dir():
            continue
        if (top / ".git").exists():
            found.append(top.name)
            continue
        try:
            found += [f"{top.name}/{sub.name}" for sub in sorted(top.iterdir()) if (sub / ".git").exists()]
        except OSError:
            continue  # unreadable folder: not a project Jarvis can open
    return sorted(found, key=lambda folder: _last_touched(root / folder), reverse=True) if sort else found


def resolve_project(folder: str) -> Path | None:
    """A listed project's folder for the name the model gave, else None: the model never picks an arbitrary path.
    Case-blind, like the macOS file system."""
    listed = {f.lower(): f for f in list_projects(sort=False)}.get(
        folder.removeprefix(f"{PROJECTS_LABEL}/").strip("/").lower()
    )
    return PROJECTS / listed if listed else None


def recent_threads(snapshot: dict[str, Any], limit: int | None = 15) -> list[dict[str, Any]]:
    """Threads the user started in T3 (not imports or agent children), newest first, with their folder."""
    projects = {p["id"]: p for p in snapshot.get("projects", []) if p.get("workspaceRoot")}
    mine = [
        t for t in snapshot.get("threads", [])
        if t.get("createdBy") == "user" and not t.get("archivedAt") and t.get("projectId") in projects
    ]
    mine.sort(key=lambda t: t.get("updatedAt") or "", reverse=True)
    return [
        {
            "id": t["id"], "title": t.get("title") or "Untitled", "status": t.get("status") or "idle",
            "updatedAt": t.get("updatedAt") or "", "projectId": t["projectId"],
            "projectRoot": projects[t["projectId"]]["workspaceRoot"], "branch": t.get("branch") or "",
            "project": projects[t["projectId"]].get("title") or Path(projects[t["projectId"]]["workspaceRoot"]).name,
            "root": t.get("worktreePath") or projects[t["projectId"]]["workspaceRoot"],
            "worktree": bool(t.get("worktreePath")), "visitedAt": t.get("lastVisitedAt") or t.get("updatedAt") or "",
        }
        for t in mine[:limit]
    ]


def current_thread(snapshot: dict[str, Any], project_root: str, worktree_path: str | None) -> dict[str, Any] | None:
    """
    The thread a T3 project script runs in. T3 gives scripts only folders (T3CODE_PROJECT_ROOT, T3CODE_WORKTREE_PATH):
    a worktree belongs to its one thread; on the project root, take the thread viewed last, the one just clicked.
    """
    def same(a: str, b: str) -> bool:
        return Path(a).expanduser().resolve() == Path(b).expanduser().resolve()

    threads = recent_threads(snapshot, limit=None)
    if worktree_path:
        matches = [t for t in threads if t["worktree"] and same(t["root"], worktree_path)]
    else:
        matches = [t for t in threads if not t["worktree"] and same(t["root"], project_root)]
    return max(matches, key=lambda t: t["visitedAt"], default=None)


def t3_defaults() -> dict[str, Any]:
    """The user's own T3 settings: the model and runtime mode a thread started in the T3 window gets."""
    # shortcut: ignores per-project overrides in T3 settings; read them here if Jarvis ever targets such a project.
    try:
        return json.loads((T3_HOME / "userdata" / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class T3Adapter(CodingSessionAdapter):
    """
    Drives T3 Code through its app server's WebSocket (`/ws`), the same API the T3 window uses: Effect RPC
    messages as JSON. Jarvis watches one T3 thread at a time (its sessions), in the T3 project that holds `cwd`,
    so the user can watch and take over in T3. Auth: a bearer token from `t3 auth session issue`, run as the
    machine's owner.
    """

    def __init__(self, config: WatchedAgentConfig, state: JarvisState) -> None:
        super().__init__(config, state)
        base = (config.url or "http://127.0.0.1:3773").rstrip("/")
        self.ws_url = base.replace("http", "ws", 1) + f"/ws?orchestrationProtocol={PROTOCOL}"
        self._ws: ClientConnection | None = None
        self._token: str | None = None
        self._ids = itertools.count(1)
        self._calls: dict[str, asyncio.Future[dict[str, Any]]] = {}  # request id -> its Exit
        self._snapshots: dict[str, asyncio.Future[dict[str, Any]]] = {}  # request id -> its shell snapshot
        self._sub_id: str | None = None  # the thread subscription's request id
        self._thread_id: str | None = None
        self.title: str | None = None  # the watched thread's title, said back to the user; None until T3 names it
        self._fresh = False  # after new_session: the next task starts a thread instead of continuing the newest
        self._sequence: int | None = None  # last event seen, to resume after a reconnect
        self._runs: dict[str, str] = {}  # active run id -> status
        self._last_text: dict[str, str] = {}  # run id -> latest finished assistant text
        self._pending: dict[str, Any] | None = None  # the approval or question T3 is waiting on
        self._spoken: set[str] = set()  # request ids already read out, so a resync doesn't repeat them

    async def _issue_token(self) -> str:
        t3 = shutil.which("t3") or str(T3_HOME / "bin" / "t3")
        proc = await asyncio.create_subprocess_exec(
            t3, "auth", "session", "issue", "--scope", "orchestration:read", "--scope", "orchestration:operate",
            "--ttl", "30d", "--label", f"jarvis {self.config.id}", "--token-only",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"`t3 auth session issue` failed: {err.decode().strip()}")
        return out.decode().strip()

    def _connect(self) -> Any:
        return connect(self.ws_url, additional_headers={"Authorization": f"Bearer {self._token}"}, max_size=None)

    async def _run_loop(self) -> None:
        while True:
            wait = 10.0
            try:
                # One token for the whole run: a new one per retry would pile up sessions while T3 is closed.
                self._token = self._token or await self._issue_token()
                async with self._connect() as ws:
                    self._ws = ws
                    logger.info(f"Connected to T3 Code at {self.ws_url}")
                    if self._thread_id:
                        await self._subscribe()
                    async for raw in ws:
                        await self._on_message(ws, json.loads(raw))
            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                code = exc.response.status_code
                if code == 426:
                    logger.error(
                        f"[{self.config.id}] T3 Code changed its protocol (Jarvis speaks {PROTOCOL}); update the adapter."
                    )
                    wait = 300.0
                elif code in (401, 403):
                    self._token = None  # revoked or expired: issue a new one
                logger.debug(f"[{self.config.id}] T3 Code refused the connection ({code}); retrying.")
            except Exception as exc:
                logger.debug(f"[{self.config.id}] T3 Code unavailable ({exc!r}); retrying in {wait:.0f}s.")
            finally:
                self._ws = None
                self._sub_id = None  # it died with the connection; the next connect subscribes again
                for call in (*self._calls.values(), *self._snapshots.values()):
                    call.cancel()
                self._calls.clear()
                self._snapshots.clear()
                self._runs.clear()
                if self._pending is not None:
                    self._pending = None
                    self._question_settled()
            await asyncio.sleep(wait)

    async def _on_message(self, ws: ClientConnection, msg: dict[str, Any]) -> None:
        tag = msg.get("_tag")
        if tag == "Ping":
            await ws.send(json.dumps({"_tag": "Pong"}))
        elif tag == "Chunk":
            await ws.send(json.dumps({"_tag": "Ack", "requestId": msg["requestId"]}))  # no Ack, no next chunk
            if msg["requestId"] == self._sub_id:
                for item in msg.get("values", []):
                    await self._on_stream_item(item)
            elif snap := self._snapshots.get(msg["requestId"]):
                for item in msg.get("values", []):
                    if item.get("kind") == "snapshot" and not snap.done():
                        snap.set_result(item["snapshot"])
        elif tag == "Exit":
            if msg["requestId"] == self._sub_id:  # the subscription itself ended: start over
                raise RuntimeError(f"thread subscription ended: {json.dumps(msg.get('exit'))[:300]}")
            if (snap := self._snapshots.pop(msg["requestId"], None)) and not snap.done():
                snap.set_exception(RuntimeError(f"T3 shell subscription failed: {json.dumps(msg)[:300]}"))
            call = self._calls.pop(msg["requestId"], None)
            if call and not call.done():
                call.set_result(msg["exit"])

    async def _request(self, method: str, payload: dict[str, Any]) -> str:
        if self._ws is None:
            raise ConnectionError("T3 Code is not connected")
        request_id = str(next(self._ids))
        await self._ws.send(json.dumps({"_tag": "Request", "id": request_id, "tag": method, "payload": payload, "headers": []}))
        return request_id

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Send one RPC and wait for its result; None (logged) on failure."""
        try:
            request_id = await self._request(method, payload)
            future = asyncio.get_running_loop().create_future()
            self._calls[request_id] = future
            exit_ = await asyncio.wait_for(future, 30.0)
        except (ConnectionError, OSError, asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError) and (task := asyncio.current_task()) and task.cancelling():
                raise  # Jarvis is shutting down, not a dropped connection
            logger.error(f"[{self.config.id}] T3 {method} got no answer ({exc!r})")
            return None
        if exit_.get("_tag") != "Success":
            logger.error(f"[{self.config.id}] T3 {method} failed: {json.dumps(exit_)[:400]}")
            return None
        return exit_.get("value") or {}

    async def _command(self, command: dict[str, Any]) -> bool:
        payload = {"commandId": str(uuid.uuid4()), "threadId": self._thread_id, **command}
        return await self._call("orchestration.dispatchCommand", payload) is not None

    async def _unsubscribe(self) -> None:
        """Close the stream of the thread watched before, so its events stop coming over the socket."""
        old, self._sub_id = self._sub_id, None
        if old is not None and self._ws is not None:
            try:
                await self._ws.send(json.dumps({"_tag": "Interrupt", "id": old}))
            except OSError:
                pass

    async def _subscribe(self) -> None:
        await self._unsubscribe()
        payload: dict[str, Any] = {"threadId": self._thread_id}
        if self._sequence is not None:
            payload["afterSequence"] = self._sequence
        self._sub_id = await self._request("orchestration.subscribeThread", payload)

    async def _find_project(self, cwd: str) -> dict[str, Any] | None:
        return find_project((await self.shell_snapshot()).get("projects", []), cwd)

    async def shell_snapshot(self) -> dict[str, Any]:
        """T3's projects and threads, read over the open connection or a short-lived one before start_all."""
        if self._ws is not None:
            future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            request_id = str(next(self._ids))
            self._snapshots[request_id] = future
            try:
                await self._ws.send(
                    json.dumps(
                        {
                            "_tag": "Request",
                            "id": request_id,
                            "tag": "orchestration.subscribeShell",
                            "payload": {},
                            "headers": [],
                        }
                    )
                )
                return await asyncio.wait_for(future, 10.0)
            finally:
                self._snapshots.pop(request_id, None)
                if self._ws is not None:
                    try:
                        await self._ws.send(json.dumps({"_tag": "Interrupt", "id": request_id}))
                    except OSError:
                        pass
        self._token = self._token or await self._issue_token()
        async with asyncio.timeout(10.0), self._connect() as ws:
            request = {"_tag": "Request", "id": "1", "tag": "orchestration.subscribeShell", "payload": {}, "headers": []}
            await ws.send(json.dumps(request))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("_tag") == "Ping":
                    await ws.send(json.dumps({"_tag": "Pong"}))
                elif msg.get("_tag") == "Exit":
                    raise RuntimeError(f"T3 shell subscription failed: {json.dumps(msg)[:300]}")
                for item in msg.get("values", []) if msg.get("_tag") == "Chunk" else []:
                    if item.get("kind") == "snapshot":
                        return item["snapshot"]
        raise RuntimeError("T3 shell subscription closed before its snapshot")

    async def _on_stream_item(self, item: dict[str, Any]) -> None:
        if item.get("kind") == "snapshot":  # first item, and again if T3 can't resume from our sequence
            self._sequence = item.get("snapshotSequence")
            projection = item.get("projection") or {}
            self._runs = {r["id"]: r["status"] for r in projection.get("runs", []) if r.get("status") in _ACTIVE}
            for turn_item in projection.get("turnItems", []):
                await self._on_turn_item(turn_item)
        elif item.get("kind") == "event":
            self._sequence = max(self._sequence or 0, item.get("sequence") or 0)
            await self._on_event(item["event"])

    async def _on_event(self, event: dict[str, Any]) -> None:
        kind, payload = event.get("type"), event.get("payload") or {}
        if kind == "turn-item.updated":
            await self._on_turn_item(payload)
        elif kind == "runtime-request.updated" and payload.get("status") != "pending":
            self._settle(payload.get("id"))  # answered in the T3 window, expired or cancelled
        elif kind == "run.updated":
            run_id, status = payload.get("id") or event.get("runId"), payload.get("status")
            if not run_id:
                return  # no run to track
            if status in _ACTIVE:
                self._runs[run_id] = status
                return
            self._runs.pop(run_id, None)
            text = self._last_text.pop(run_id, "")
            if status == "completed" and text:
                await self._surface(text, is_report=True)
            elif status == "failed":
                await self._surface(f"The turn failed. {text}".strip(), is_report=True)
            # interrupted or cancelled: the user stopped it, here or in T3, so it is not news.

    async def _on_turn_item(self, item: dict[str, Any]) -> None:
        kind = item.get("type")
        if kind == "assistant_message" and item.get("status") == "completed" and item.get("runId") in self._runs:
            self._last_text[item["runId"]] = item.get("text") or ""
        elif kind in _ASKS:
            if item.get("status") != "waiting":
                self._settle(item.get("requestId"))
                return
            self._pending = item
            if item["requestId"] in self._spoken:
                return
            self._spoken.add(item["requestId"])
            if kind == "approval_request":
                what = item.get("prompt") or item.get("requestKind") or "a tool call"
                reason = f". {item['reason']}" if item.get("reason") else ""
                await self._surface(f"needs approval: {what}{reason}")
            else:
                await self._surface(f"asks: {describe_fields(question_fields(item.get('questions', [])))}")

    def _settle(self, request_id: str | None) -> None:
        if self._pending and self._pending.get("requestId") == request_id:
            self._pending = None
            self._question_settled()

    @property
    def session_id(self) -> str | None:
        return self._thread_id

    async def list_sessions(self, cwd: str | None = None, limit: int = 15) -> list[AgentSession]:
        """The user's T3 threads in the project holding cwd (every project when None), newest first."""
        snapshot = await self.shell_snapshot()
        project = find_project(snapshot.get("projects", []), cwd) if cwd else None
        if cwd and project is None:
            return []  # T3 has no project there yet
        return [
            AgentSession(
                id=t["id"], title=t["title"], cwd=t["projectRoot"], branch=t["branch"], running=t["status"] in _ACTIVE,
                updated_at=datetime.fromisoformat(t["updatedAt"]).timestamp() if t["updatedAt"] else 0.0,
            )
            for t in recent_threads(snapshot, limit=None)
            if project is None or t["projectId"] == project["id"]
        ][:limit]

    async def pick_session(self, session: AgentSession, connected: bool = False) -> bool:
        if self.busy:  # the running turn's result would never be told: Jarvis watches one thread
            return False
        self.attach(session.id, session.cwd, connected, title=session.title)
        if self._ws is not None:
            await self._subscribe()
        return True

    async def new_session(self, cwd: str | None = None) -> bool:
        """The next task starts a new thread in cwd. Nothing is sent now: a thread without a task is an empty one."""
        if self.busy or self.connected:
            return False
        await self._unsubscribe()
        self.attach(None, cwd or self.cwd)
        self._fresh = True
        return True

    async def send_task(self, task: str) -> bool:
        if self._ws is None:
            logger.warning(f"[{self.config.id}] Cannot send task; T3 Code is not connected.")
            return False
        if not self._thread_id and not self._fresh:
            await self._continue_newest()
        if self._thread_id:
            # "auto" is a send from the T3 window: start when idle, queue behind a stopping run, steer a running one.
            return await self._command({
                "type": "message.dispatch", "createdBy": "user", "creationSource": "web",
                "messageId": str(uuid.uuid4()), "text": task, "attachments": [],
                "deliveryIntent": "auto", "dispatchMode": {"type": "queue_after_active"},
            })
        return await self._launch(self.cwd, task)

    async def _continue_newest(self) -> None:
        """No thread yet: continue the newest idle one in cwd. A running one may be the user's own, typed in T3."""
        try:
            idle = [s for s in await self.list_sessions(self.cwd, limit=50) if not s.running]
        except Exception as exc:
            logger.warning(f"[{self.config.id}] Could not read T3 threads ({exc!r}); starting a new one.")
            return
        if idle:
            await self.pick_session(idle[0])
            logger.info(f"[{self.config.id}] Continuing T3 thread {self._thread_id} ({self.title})")

    async def _launch(self, cwd: str, task: str) -> bool:
        """Start a T3 thread in the project holding cwd (made first for a listed folder T3 doesn't know) and watch it.
        It syncs with master first, then does the task; T3 titles it from that message."""
        if self.connected:
            logger.error(f"[{self.config.id}] Not starting a thread: Jarvis is connected to {self._thread_id}.")
            return False
        try:
            project = await self._find_project(cwd)
        except Exception as exc:
            logger.error(f"[{self.config.id}] Could not read T3 projects: {exc!r}")
            return False
        if project is None and Path(cwd).expanduser().resolve().is_relative_to(PROJECTS.expanduser().resolve()):
            project = {"id": str(uuid.uuid4()), "workspaceRoot": cwd}
            created = await self._call("orchestration.dispatchCommand", {
                "type": "project.create", "commandId": str(uuid.uuid4()), "projectId": project["id"],
                "title": Path(cwd).name, "workspaceRoot": cwd,
            })
            if created is None:
                return False
        defaults = t3_defaults()
        model = (project or {}).get("defaultModelSelection") or defaults.get("defaultModelSelection")
        if not project or not model:
            logger.error(f"[{self.config.id}] No T3 project holds {cwd}, or T3 has no default model; set it in T3.")
            return False
        thread_id = str(uuid.uuid4())
        launched = await self._call("orchestration.launchThread", {
            "commandId": str(uuid.uuid4()), "threadId": thread_id, "projectId": project["id"],
            "title": "New thread", "generateTitle": True,  # T3's own placeholder, replaced by a title from the task
            "modelSelection": model, "runtimeMode": defaults.get("defaultRuntimeMode") or "auto",  # T3's own default
            "interactionMode": "default", "workspaceStrategy": {"type": "root"},
            "initialMessage": {"text": f"{SYNC_MASTER} first. Then: {task}", "attachments": []},
        })
        if launched is None:
            return False
        self.attach(launched.get("threadId") or thread_id, cwd)
        await self._subscribe()
        logger.info(f"Started T3 thread {self._thread_id} in {project['workspaceRoot']}")
        return True

    def attach(self, thread_id: str | None, root: str, connected: bool = False, title: str | None = None) -> None:
        """
        Watch this thread from now on (None: none until the next task); it gets no message. Subscribes on the next
        connect, or call _subscribe. connected: picked with `jarvis connect`, so Jarvis never leaves it this run.
        """
        # shortcut: Jarvis watches one thread; the previous one keeps running in T3 unwatched. Track several if needed.
        self._thread_id, self.cwd, self._sequence, self.connected = thread_id, root, None, connected
        self.title, self._fresh = title, False
        self._runs, self._last_text = {}, {}
        if self._pending:  # a question on the old thread can no longer be answered from here
            self._pending = None
            self._question_settled()

    @property
    def busy(self) -> bool:
        return bool(self._runs)

    async def answer(self, answer: str) -> bool:
        if self._pending is None:
            return False
        item = self._pending
        command: dict[str, Any] = {"type": "runtime-request.respond", "requestId": item["requestId"]}
        if item["type"] == "approval_request":
            command["decision"] = "accept" if is_approval(answer) else "decline"
        else:
            command["answers"] = form_answer(question_fields(item.get("questions", [])), answer)
        ok = await self._command(command)
        if ok:  # cleared only once T3 took it, so a network error leaves it answerable
            self._settle(item["requestId"])
        return ok

    async def interrupt(self) -> bool:
        running = [run_id for run_id, status in self._runs.items() if status != "queued"]
        if not running:
            return False
        stopped = False
        for run_id in running:
            stopped = await self._command({"type": "run.interrupt", "runId": run_id, "reason": "Stopped by the user."}) or stopped
        if stopped:
            self._pending = None
            self._question_settled()
            for run_id in running:  # not busy any more, so a correction sent next starts fresh
                self._runs.pop(run_id, None)
        return stopped
