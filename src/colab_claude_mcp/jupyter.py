# Copyright 2026 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Modified for colab-claude-mcp: new file.

"""JupyterLab backend: a notebook on a Jupyter server, driven through the user's logged-in browser tab.

A Jupyter server behind a password, such as JupyterLab on an HPC node reached through an SSH
tunnel, cannot be used from here without its credentials, and the user logs in in the browser.
So the JupyterLab tab is the bridge, the way the Colab tab is for Colab: a small script
(jupyter_bridge.js, loaded into the tab with a bookmarklet) connects to the hub and relays the
hub's REST requests and kernel websockets through the tab's own session, that is its cookies and
XSRF token. Claude never sees the password.

The notebook logic lives here, not in the browser: this module keeps the cells and outputs, saves
the .ipynb on the server through the contents API after each change, and runs cells over the
kernel's websocket (Jupyter messaging protocol 5.3, JSON frames, no websocket subprotocol).
"""

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import functools
import hashlib
import json
import logging
from pathlib import Path
import posixpath
import re
import secrets
import time
from urllib.parse import quote
import uuid

from starlette.websockets import WebSocket, WebSocketDisconnect

from colab_claude_mcp import config, outputs
from colab_claude_mcp.results import Run, err, ok

log = logging.getLogger(__name__)

# Operations shared with the Colab backend; the hub routes them here for backend="jupyter".
CELL_OPS = frozenset(
    {
        "get_cells",
        "add_code_cell",
        "add_text_cell",
        "update_cell",
        "run_code_cell",
        "wait_for_cell",
        "delete_cell",
        "move_cell",
    }
)

HELLO_TIMEOUT = 10.0
REQUEST_TIMEOUT = 60.0
SAVE_TIMEOUT = 180.0  # a notebook with large outputs travels through the tab and the SSH tunnel
MAX_WAIT_SECONDS = 600
SAVE_EVERY_SECONDS = 60.0  # while a cell runs, so that its output so far is kept on the server
FLUSH_SECONDS = 0.3  # how often streamed output is copied for the mirror page
CLOSE_REPLACED = 4000  # a newer JupyterLab tab took over
OUTPUT_TYPES = frozenset({"stream", "display_data", "execute_result", "error", "update_display_data", "clear_output"})
CELL_ID = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

NO_TAB = (
    "Ask the user to open JupyterLab in their browser, log in as usual, and click the 'Claude ⇄ Jupyter' "
    "bookmark in that tab; a 'Claude connected' badge then appears there. They make that bookmark once, "
    "from {setup}. Never ask for the Jupyter password."
)


class BridgeError(Exception):
    """The bridge, or the Jupyter server behind it, answered with an error."""


class BridgeGone(BridgeError):
    """No JupyterLab tab is connected, or it went away mid-request."""


class NotOpen(Exception):
    """No notebook has been opened with jupyter_connect."""


def no_tab_text() -> str:
    return NO_TAB.format(setup=config.jupyter_setup_url())


# ---- outputs -----------------------------------------------------------------------------


def collapse_cr(text: str) -> str:
    """Show carriage returns the way a terminal, and JupyterLab, do: each \\r goes back to the line start.

    Progress bars (tqdm, Keras) rewrite one line many times; without this the output would keep every
    state of the bar. A trailing \\r is kept, so that the next chunk still overwrites the line.
    """
    if "\r" not in text:
        return text
    text = re.sub(r"\r+\n", "\n", text)
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if "\r" not in line:
            continue
        trailing = line.endswith("\r")
        shown = ""
        for part in line.rstrip("\r").split("\r"):
            shown = part + shown[len(part) :]
        lines[index] = shown + ("\r" if trailing else "")
    return "\n".join(lines)


class OutputCollector:
    """Builds a code cell's nbformat outputs from the kernel's iopub messages, as JupyterLab does."""

    def __init__(self):
        self.outputs: list[dict] = []
        self._displays: dict[str, list[int]] = {}
        self._clear_on_next = False

    def add(self, msg_type: str, content: dict) -> None:
        if msg_type == "clear_output":
            if content.get("wait"):
                self._clear_on_next = True
            else:
                self._clear()
            return
        if msg_type == "update_display_data":
            display_id = (content.get("transient") or {}).get("display_id")
            for index in self._displays.get(display_id, []):
                self.outputs[index]["data"] = content.get("data") or {}
                self.outputs[index]["metadata"] = content.get("metadata") or {}
            return
        if self._clear_on_next:
            self._clear()
        if msg_type == "stream":
            name = str(content.get("name", "stdout"))
            text = str(content.get("text", ""))
            last = self.outputs[-1] if self.outputs else None
            if last and last["output_type"] == "stream" and last["name"] == name:
                last["text"] = collapse_cr(last["text"] + text)
            else:
                self.outputs.append({"output_type": "stream", "name": name, "text": collapse_cr(text)})
        elif msg_type in ("display_data", "execute_result"):
            output = {
                "output_type": msg_type,
                "data": content.get("data") or {},
                "metadata": content.get("metadata") or {},
            }
            if msg_type == "execute_result":
                output["execution_count"] = content.get("execution_count")
            self.outputs.append(output)
            display_id = (content.get("transient") or {}).get("display_id")
            if display_id:
                self._displays.setdefault(display_id, []).append(len(self.outputs) - 1)
        elif msg_type == "error":
            self.outputs.append(
                {
                    "output_type": "error",
                    "ename": str(content.get("ename", "Error")),
                    "evalue": str(content.get("evalue", "")),
                    "traceback": [str(line) for line in content.get("traceback") or []],
                }
            )

    def _clear(self) -> None:
        self.outputs = []
        self._displays = {}
        self._clear_on_next = False

    def has_error(self) -> bool:
        return any(output["output_type"] == "error" for output in self.outputs)


def items_of(cell_outputs: list[dict]) -> list[dict]:
    """Output items for replies and the mirror. After collapse_cr only a trailing \\r can be left."""
    items = outputs.normalize(cell_outputs)
    for item in items:
        if item["kind"] == "stream":
            item["text"] = item["text"].replace("\r", "")
    return items


# ---- the bridge: the JupyterLab tab's websocket ------------------------------------------


class Channel:
    """A websocket that the JupyterLab tab opened to the Jupyter server on the hub's behalf."""

    def __init__(
        self, conn: "_Conn", chan_id: str, on_message: Callable[[dict], None], on_close: Callable[[int], None]
    ):
        self.conn = conn
        self.id = chan_id
        self.on_message = on_message
        self.on_close = on_close
        self.closed = False

    async def send(self, data: dict) -> None:
        if self.closed:
            raise BridgeGone("The kernel connection is closed")
        await self.conn.send({"type": "ws_send", "chan": self.id, "data": json.dumps(data)})

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.conn.channels.pop(self.id, None)
        try:
            await self.conn.send({"type": "ws_close", "chan": self.id})
        except BridgeGone:
            pass

    def lost(self, code: int) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.on_close(code)
            except Exception:
                log.exception("channel close callback failed")


class _Conn:
    """One connected JupyterLab tab."""

    def __init__(self, ws: WebSocket, info: dict):
        self.ws = ws
        self.info = info
        self.pending: dict[int, asyncio.Future] = {}
        self.channels: dict[str, Channel] = {}
        self.alive = True
        self._next = 0
        self._lock = asyncio.Lock()

    def next_id(self) -> int:
        self._next += 1
        return self._next

    async def send(self, message: dict) -> None:
        if not self.alive:
            raise BridgeGone("The JupyterLab tab disconnected")
        async with self._lock:
            try:
                await self.ws.send_text(json.dumps(message))
            except Exception as exc:  # the socket closed under us
                raise BridgeGone("The JupyterLab tab disconnected") from exc

    async def request(self, message: dict, timeout: float) -> dict:
        request_id = self.next_id()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({**message, "id": request_id})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    def dispatch(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "reply":
            future = self.pending.get(message.get("id"))
            if future and not future.done():
                future.set_result(message)
        elif kind == "ws_msg":
            channel = self.channels.get(str(message.get("chan")))
            if channel is None or not isinstance(message.get("data"), str):
                return  # binary frames carry widget buffers, which are not used
            try:
                data = json.loads(message["data"])
            except ValueError:
                return
            try:
                channel.on_message(data)
            except Exception:
                log.exception("kernel message handler failed")
        elif kind == "ws_closed":
            channel = self.channels.pop(str(message.get("chan")), None)
            if channel:
                channel.lost(int(message.get("code") or 0))
        elif kind == "log":
            log.info("bridge: %s", str(message.get("text"))[:500])

    def close(self) -> None:
        self.alive = False
        for future in self.pending.values():
            if not future.done():
                future.set_exception(BridgeGone("The JupyterLab tab disconnected"))
        for channel in list(self.channels.values()):
            channel.lost(1001)
        self.channels.clear()


class Bridge:
    """Accepts the JupyterLab tab's websocket and relays requests through that tab's session."""

    def __init__(self, on_change: Callable[[bool], None] | None = None):
        self._conn: _Conn | None = None
        self.generation = 0  # increases with every accepted tab
        self.last_close = ""
        self._on_change = on_change

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    @property
    def info(self) -> dict:
        return self._conn.info if self._conn else {}

    async def wait_connected(self, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.is_connected:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.2, remaining))
        return True

    async def serve(self, ws: WebSocket) -> None:
        """Run one tab's connection until it closes. The websocket is accepted and its token checked."""
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT))
        except (TimeoutError, ValueError, WebSocketDisconnect, RuntimeError):
            await _close(ws, 1002, "Expected a hello message")
            return
        if not isinstance(hello, dict) or hello.get("type") != "hello":
            await _close(ws, 1002, "Expected a hello message")
            return
        keys = ("origin", "href", "base_url", "app_name", "app_version", "tree_path", "bridge")
        info = {key: str(hello.get(key) or "")[:500] for key in keys}
        if info["bridge"] != config.VERSION:
            # The tab runs a bridge script from an older hub: have it load the current one.
            try:
                await ws.send_text(json.dumps({"type": "reload"}))
            except Exception:
                pass
            await _close(ws, 1000, "Reloading the bridge")
            return
        conn = _Conn(ws, info)
        previous, self._conn = self._conn, conn
        self.generation += 1
        if previous is not None:
            # The newest tab wins, as with Colab.
            previous.close()
            await _close(previous.ws, CLOSE_REPLACED, "Replaced by a newer JupyterLab tab")
        try:
            await conn.send({"type": "welcome", "text": "Claude connected"})
        except BridgeGone:
            pass
        log.info("JupyterLab tab connected from %s (generation %s)", info["origin"], self.generation)
        self._notify(True)
        code = 1000
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    code = int(message.get("code") or 1000)
                    break
                text = message.get("text")
                if text is None:
                    continue
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    conn.dispatch(data)
        except (WebSocketDisconnect, RuntimeError) as exc:
            code = getattr(exc, "code", 1006)
        finally:
            conn.close()
            if self._conn is conn:
                self._conn = None
                self.last_close = f"code {code}"
                log.info("JupyterLab tab disconnected (%s)", self.last_close)
                self._notify(False)

    def _require(self) -> _Conn:
        if self._conn is None:
            raise BridgeGone("No JupyterLab tab is connected")
        return self._conn

    async def http(self, method: str, path: str, body=None, timeout: float = REQUEST_TIMEOUT) -> tuple[int, str]:
        """A request to the Jupyter server, made by the tab. Returns (status, body text)."""
        conn = self._require()
        message = {"type": "http", "method": method, "path": path, "body": None if body is None else json.dumps(body)}
        reply = await conn.request(message, timeout)
        status = int(reply.get("status") or 0)
        if status == 0:
            raise BridgeError(
                f"The JupyterLab tab could not reach the Jupyter server ({reply.get('error') or 'network error'}). "
                "Is the SSH tunnel still up?"
            )
        return status, str(reply.get("text") or "")

    async def open_channel(self, path: str, on_message, on_close) -> Channel:
        """Have the tab open a websocket to the Jupyter server, relayed through the hub connection."""
        conn = self._require()
        channel = Channel(conn, f"c{self.generation}-{conn.next_id()}", on_message, on_close)
        conn.channels[channel.id] = channel
        try:
            reply = await conn.request({"type": "ws_open", "chan": channel.id, "path": path}, 30)
        except BaseException:
            conn.channels.pop(channel.id, None)
            raise
        if not reply.get("ok"):
            conn.channels.pop(channel.id, None)
            raise BridgeError(f"The Jupyter server refused the kernel connection ({reply.get('error') or 'no reason'})")
        return channel

    def _notify(self, connected: bool) -> None:
        if self._on_change:
            try:
                self._on_change(connected)
            except Exception:
                log.exception("on_change callback failed")


async def _close(ws: WebSocket, code: int, reason: str) -> None:
    try:
        await ws.close(code=code, reason=reason)
    except Exception:
        pass


# ---- kernels -----------------------------------------------------------------------------


def _message(msg_type: str, content: dict, session: str, channel: str = "shell") -> dict:
    return {
        "header": {
            "msg_id": uuid.uuid4().hex,
            "msg_type": msg_type,
            "username": "claude",
            "session": session,
            "date": datetime.now(UTC).isoformat(),
            "version": "5.3",
        },
        "parent_header": {},
        "metadata": {},
        "content": content,
        "channel": channel,
        "buffers": [],
    }


class Kernel:
    """One kernel's websocket, opened through the bridge. Messages are routed by their parent's id."""

    def __init__(self, bridge: Bridge, kernel_id: str, name: str, on_state: Callable[[str], None] | None = None):
        self.bridge = bridge
        self.id = kernel_id
        self.name = name
        # Kept across reconnects: the server holds back what the kernel sends while no tab is
        # connected and replays it when the same session id comes back.
        self.client_session = uuid.uuid4().hex
        self.state = "unknown"
        self.channel: Channel | None = None
        self.handlers: dict[str, Callable[[dict], None]] = {}
        self._on_state = on_state
        self._open_lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self.channel is not None and not self.channel.closed

    async def ensure_open(self) -> None:
        async with self._open_lock:
            if self.is_open:
                return
            path = f"api/kernels/{quote(self.id)}/channels?session_id={self.client_session}"
            self.channel = await self.bridge.open_channel(path, self._on_message, self._on_close)

    async def close(self) -> None:
        self.handlers.clear()
        if self.channel is not None:
            await self.channel.close()

    def _on_close(self, code: int) -> None:
        log.info("kernel %s channel closed (code %s)", self.id, code)

    def _on_message(self, message: dict) -> None:
        msg_type = message.get("msg_type") or (message.get("header") or {}).get("msg_type")
        if message.get("channel") == "iopub" and msg_type == "status":
            state = (message.get("content") or {}).get("execution_state")
            if state and state != self.state:
                self.state = state
                if self._on_state:
                    self._on_state(state)
        parent = (message.get("parent_header") or {}).get("msg_id")
        handler = self.handlers.get(parent) if parent else None
        if handler:
            handler(message)

    async def execute(self, code: str, handler: Callable[[dict], None]) -> str:
        await self.ensure_open()
        content = {
            "code": code,
            "silent": False,
            "store_history": True,
            "user_expressions": {},
            "allow_stdin": False,
            "stop_on_error": True,
        }
        message = _message("execute_request", content, self.client_session)
        msg_id = message["header"]["msg_id"]
        self.handlers[msg_id] = handler
        try:
            await self.channel.send(message)
        except BaseException:
            self.handlers.pop(msg_id, None)
            raise
        return msg_id


@dataclass
class _RunControl:
    collector: OutputCollector = field(default_factory=OutputCollector)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    msg_id: str = ""
    reply: dict | None = None
    idle: bool = False
    execution_count: int | None = None
    abort: str = ""


# ---- the notebook ------------------------------------------------------------------------


def _contents(path: str, **query: str) -> str:
    url = "api/contents/" + quote(path.strip("/"), safe="/")
    return url + ("?" + "&".join(f"{key}={value}" for key, value in query.items()) if query else "")


def _session_path(session: dict) -> str:
    return str(session.get("path") or (session.get("notebook") or {}).get("path") or "")


def _http_error(method: str, path: str, status: int, text: str) -> str:
    if status in (401, 403):
        return (
            f"The Jupyter server refused {method} {path} (HTTP {status}): the JupyterLab tab is probably logged "
            "out. Ask the user to log in there again and click the 'Claude ⇄ Jupyter' bookmark."
        )
    detail = text[:300]
    try:
        detail = json.loads(text).get("message") or detail
    except (ValueError, AttributeError):
        pass
    return f"{method} {path} failed with HTTP {status}: {detail}"


def _save_note(problem: str | None) -> str:
    return f"\nWARNING: the notebook could not be saved on the server: {problem}" if problem else ""


class JupyterSide:
    """The notebook on a Jupyter server that Claude works in, driven through the bridge."""

    def __init__(self, home: Path):
        self.home = home
        self.bridge = Bridge(on_change=self._bridge_changed)
        self.path: str | None = None
        self.nb: dict = {}  # the notebook's top level without its cells (metadata, nbformat)
        self.cells: list[dict] = []  # nbformat cells, each with an id and its source as one string
        self.outputs: dict[str, list[dict]] = {}  # output items per code cell, for replies and the mirror
        self.cell_rev: dict[str, int] = {}
        self.runs: dict[str, Run] = {}
        self.events: deque[dict] = deque(maxlen=200)
        self.rev = 0
        self.kernel: Kernel | None = None
        self.kernel_display = ""
        self.kernelspecs: dict[str, str] = {}  # name -> display name
        self.default_kernel = ""
        self.session_id: str | None = None
        self.reused = False  # the notebook's kernel was already running when it was opened
        self.shared = False  # and another client, such as a JupyterLab tab, was connected to it
        self.last_modified: str | None = None
        self.last_saved: float | None = None
        self._dirty: set[str] = set()  # code cells whose outputs changed since the last save
        self._controls: dict[str, _RunControl] = {}
        self._digests: dict[str, str] = {}
        self._flush_cells: set[str] = set()
        self._flush_handle: asyncio.TimerHandle | None = None
        self._edit_lock = asyncio.Lock()
        self._save_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ---- state -----------------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.path is not None

    def owns(self, cell_id) -> bool:
        return any(cell["id"] == cell_id for cell in self.cells) or cell_id in self.runs

    def _event(self, kind: str, text: str) -> None:
        self.events.append({"t": time.time(), "kind": kind, "text": text})
        self.rev += 1

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _bridge_changed(self, connected: bool) -> None:
        if connected:
            self._event("connect", f"JupyterLab tab connected ({self.bridge.info.get('origin') or 'unknown origin'})")
            if self.kernel is not None:
                self._spawn(self._reopen_kernel())
        else:
            self._event("disconnect", f"JupyterLab tab disconnected ({self.bridge.last_close})")

    async def _reopen_kernel(self) -> None:
        try:
            await self.kernel.ensure_open()  # the server then replays what the kernel sent meanwhile
        except (BridgeError, TimeoutError) as exc:
            log.info("could not reopen the kernel connection: %s", exc)

    def _set_outputs(self, cell_id: str, items: list[dict]) -> None:
        digest = hashlib.blake2b(json.dumps(items, sort_keys=True).encode(), digest_size=16).hexdigest()
        if self._digests.get(cell_id) == digest:
            return
        self._digests[cell_id] = digest
        self.outputs[cell_id] = items
        self.cell_rev[cell_id] = self.cell_rev.get(cell_id, 0) + 1
        self.rev += 1

    def _running(self, cell_id: str) -> bool:
        run = self.runs.get(cell_id)
        return bool(run and run.finished is None)

    def _cell_status(self, cell_id: str) -> str:
        run = self.runs.get(cell_id)
        if run:
            return run.status
        return "has_output" if self.outputs.get(cell_id) else "idle"

    def _find(self, cell_id) -> dict | None:
        return next((cell for cell in self.cells if cell["id"] == cell_id), None)

    def _require_open(self) -> None:
        if self.path is None:
            raise NotOpen()

    def _new_id(self, taken: set[str] | None = None) -> str:
        used = set(taken or ()) | {cell["id"] for cell in self.cells}
        while True:
            cell_id = secrets.token_hex(4)
            if cell_id not in used:
                return cell_id

    # ---- the notebook file -----------------------------------------------------------

    def _load(self, path: str, notebook: dict, last_modified: str | None) -> None:
        notebook = dict(notebook)
        seen: set[str] = set()
        assigned = False
        cells = []
        for raw in notebook.pop("cells", None) or []:
            cell = dict(raw)
            cell["source"] = outputs._join(cell.get("source"))
            cell.setdefault("metadata", {})
            cell_id = str(cell.get("id") or "")
            if not CELL_ID.match(cell_id) or cell_id in seen:
                cell_id = self._new_id(seen)
                cell["id"] = cell_id
                assigned = True
            seen.add(cell_id)
            if cell.get("cell_type") == "code":
                cell["outputs"] = list(cell.get("outputs") or [])
                cell.setdefault("execution_count", None)
            cells.append(cell)
        if assigned:  # cell ids are part of nbformat 4.5
            notebook["nbformat"] = 4
            notebook["nbformat_minor"] = max(5, int(notebook.get("nbformat_minor") or 0))
        self.path = path
        self.nb = notebook
        self.cells = cells
        self.last_modified = last_modified
        live = {cell["id"] for cell in cells}
        for cell in cells:
            if cell.get("cell_type") == "code" and cell["id"] not in self._controls:
                self._set_outputs(cell["id"], items_of(cell["outputs"]))
        for stale in [cell_id for cell_id in self.outputs if cell_id not in live]:
            self.outputs.pop(stale, None)
            self._digests.pop(stale, None)
            self.cell_rev.pop(stale, None)
            if not self._running(stale):
                self.runs.pop(stale, None)
        self.rev += 1

    def _notebook_json(self) -> dict:
        notebook = dict(self.nb)
        notebook.setdefault("metadata", {})
        notebook.setdefault("nbformat", 4)
        notebook.setdefault("nbformat_minor", 5)
        cells = []
        for cell in self.cells:
            item = dict(cell)
            if item.get("cell_type") != "code":
                item.pop("outputs", None)
                item.pop("execution_count", None)
            cells.append(item)
        notebook["cells"] = cells
        return notebook

    async def _request(self, method: str, path: str, body=None, timeout: float = REQUEST_TIMEOUT):
        status, text = await self.bridge.http(method, path, body, timeout)
        if status in (200, 201, 204):
            return json.loads(text) if text else None
        raise BridgeError(_http_error(method, path, status, text))

    async def _get_notebook(self, path: str) -> dict | None:
        status, text = await self.bridge.http(
            "GET", _contents(path, type="notebook", content="1"), timeout=SAVE_TIMEOUT
        )
        if status == 404:
            return None
        if status != 200:
            raise BridgeError(_http_error("GET", path, status, text))
        return json.loads(text)

    async def _stat(self, path: str) -> dict | None:
        status, text = await self.bridge.http("GET", _contents(path, content="0"))
        if status == 404:
            return None
        if status != 200:
            raise BridgeError(_http_error("GET", path, status, text))
        return json.loads(text)

    async def _ensure_dirs(self, directory: str) -> None:
        parts = [part for part in directory.split("/") if part]
        for depth in range(1, len(parts) + 1):
            sub = "/".join(parts[:depth])
            if await self._stat(sub) is None:
                await self._request("PUT", _contents(sub), {"type": "directory"})

    async def _merge_from_disk(self) -> None:
        """Take the version on the server (someone saved it, for example from JupyterLab), keeping our new outputs."""
        model = await self._get_notebook(self.path)
        if model is None:
            return
        ours = {cell["id"]: cell for cell in self.cells}
        keep = self._dirty | set(self._controls)
        self._load(self.path, model.get("content") or {}, model.get("last_modified"))
        for cell in self.cells:
            mine = ours.get(cell["id"])
            if cell["id"] in keep and mine is not None and cell.get("cell_type") == "code":
                cell["outputs"] = mine.get("outputs") or []
                cell["execution_count"] = mine.get("execution_count")
                if cell["id"] not in self._controls:
                    self._set_outputs(cell["id"], items_of(cell["outputs"]))
        self._event("reload", f"Reloaded {self.path}: it was changed outside (for example saved in JupyterLab)")

    async def _sync(self) -> None:
        """Before a change: take in a newer version saved by someone else, so it is not overwritten."""
        async with self._save_lock:
            current = await self._stat(self.path)
            if current is not None and self.last_modified and current.get("last_modified") != self.last_modified:
                await self._merge_from_disk()

    async def _save(self) -> str | None:
        """Write the notebook to the server. Returns why that failed, or None."""
        async with self._save_lock:
            if self.path is None:
                return None
            try:
                current = await self._stat(self.path)
                if current is not None and self.last_modified and current.get("last_modified") != self.last_modified:
                    await self._merge_from_disk()
                model = await self._request(
                    "PUT",
                    _contents(self.path),
                    {"type": "notebook", "format": "json", "content": self._notebook_json()},
                    timeout=SAVE_TIMEOUT,
                )
            except (BridgeError, TimeoutError) as exc:
                problem = str(exc) or "the JupyterLab tab did not answer in time"
                self._event("save", f"Could not save {self.path}: {problem}")
                return problem
            self.last_modified = (model or {}).get("last_modified")
            self.last_saved = time.time()
            self._dirty.clear()
            return None

    # ---- kernel ----------------------------------------------------------------------

    async def _load_kernelspecs(self) -> None:
        specs = await self._request("GET", "api/kernelspecs") or {}
        self.default_kernel = str(specs.get("default") or "")
        self.kernelspecs = {
            name: str(((entry or {}).get("spec") or {}).get("display_name") or name)
            for name, entry in (specs.get("kernelspecs") or {}).items()
        }

    def _notebook_kernel(self) -> str:
        name = str(((self.nb.get("metadata") or {}).get("kernelspec") or {}).get("name") or "")
        return name if name in self.kernelspecs else self.default_kernel or next(iter(self.kernelspecs), "python3")

    async def _attach(self, kernel_name: str | None) -> str | None:
        """Find or start the notebook's session and connect to its kernel. Returns why not, or None."""
        sessions = await self._request("GET", "api/sessions") or []
        existing = next((session for session in sessions if _session_path(session) == self.path), None)
        if existing is not None:
            current = str((existing.get("kernel") or {}).get("name") or "")
            if kernel_name and current and kernel_name != current:
                return (
                    f"{self.path} already has a running {current} kernel (it may be open in JupyterLab), so it was "
                    f"not replaced by {kernel_name}. Omit kernel to use it, or open another notebook."
                )
            session = existing
        else:
            body = {
                "path": self.path,
                "name": posixpath.basename(self.path),
                "type": "notebook",
                "kernel": {"name": kernel_name or self._notebook_kernel()},
            }
            session = await self._request("POST", "api/sessions", body) or {}
        info = session.get("kernel") or {}
        if not info.get("id"):
            raise BridgeError("The Jupyter server did not start a kernel for the notebook")
        self.session_id = session.get("id")
        ours = 1 if self.kernel is not None and self.kernel.id == info["id"] and self.kernel.is_open else 0
        if self.kernel is None or self.kernel.id != info["id"]:
            if self.kernel is not None:
                await self.kernel.close()
            self.kernel = Kernel(self.bridge, str(info["id"]), str(info.get("name") or ""), self._kernel_state)
        self.kernel.state = str(info.get("execution_state") or self.kernel.state)
        self.kernel_display = self.kernelspecs.get(self.kernel.name, self.kernel.name)
        self.reused = existing is not None
        # The server counts the websockets on a kernel; any besides ours are other clients, such as a
        # JupyterLab tab with the notebook open.
        self.shared = self.reused and int(info.get("connections") or 0) - ours > 0
        await self.kernel.ensure_open()
        return None

    async def _ensure_kernel(self) -> str:
        """Make sure the notebook's kernel can be reached. Returns a note if a new kernel had to be started."""
        if not self.bridge.is_connected:
            raise BridgeGone("No JupyterLab tab is connected")
        if self.kernel is not None:
            try:
                await self.kernel.ensure_open()
                return ""
            except BridgeGone:
                raise
            except BridgeError as exc:
                log.info("kernel %s cannot be reached: %s", self.kernel.id, exc)
        # Gone: shut down, culled for idleness, or lost in a restart of the server. Start it again.
        name = self.kernel.name if self.kernel else None
        self.kernel = None
        if not self.kernelspecs:
            await self._load_kernelspecs()
        problem = await self._attach(name if name in self.kernelspecs else None)
        if problem:
            raise BridgeError(problem)
        self._event("kernel", f"Started a new {self.kernel_display} kernel for {self.path}")
        return "The notebook's kernel was gone, so a new one was started: variables from earlier runs are lost."

    def _kernel_state(self, state: str) -> None:
        if state in ("restarting", "dead"):
            what = "died" if state == "dead" else "restarted"
            for control in self._controls.values():
                if not control.done.is_set():
                    control.abort = control.abort or (
                        f"The kernel {what} while the cell ran (for example it ran out of memory, or it was "
                        "restarted in JupyterLab); its variables are lost."
                    )
                    control.done.set()
        self.rev += 1

    def _abort_runs(self, reason: str) -> None:
        for control in self._controls.values():
            if not control.done.is_set():
                control.abort = reason
                control.done.set()

    # ---- runs ------------------------------------------------------------------------

    def _on_run_message(self, cell_id: str, control: _RunControl, message: dict) -> None:
        channel = message.get("channel")
        msg_type = message.get("msg_type") or (message.get("header") or {}).get("msg_type")
        content = message.get("content") or {}
        if channel == "iopub":
            if msg_type == "status" and content.get("execution_state") == "idle":
                control.idle = True
            elif msg_type == "execute_input":
                control.execution_count = content.get("execution_count")
            elif msg_type in OUTPUT_TYPES:
                control.collector.add(msg_type, content)
                self._schedule_flush(cell_id)
        elif channel == "shell" and msg_type == "execute_reply":
            control.reply = content
        if control.idle and control.reply is not None:
            control.done.set()

    def _schedule_flush(self, cell_id: str) -> None:
        self._flush_cells.add(cell_id)
        if self._flush_handle is None:
            self._flush_handle = asyncio.get_running_loop().call_later(FLUSH_SECONDS, self._flush)

    def _flush(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        for cell_id in list(self._flush_cells):
            control = self._controls.get(cell_id)
            if control is not None:
                self._set_outputs(cell_id, items_of(control.collector.outputs))
        self._flush_cells.clear()

    def _store(self, cell_id: str, control: _RunControl, execution_count=None) -> None:
        cell = self._find(cell_id)
        if cell is None and cell_id not in self.runs:
            return  # deleted, or another notebook was opened meanwhile
        if cell is not None and cell.get("cell_type") == "code":
            cell["outputs"] = [dict(output) for output in control.collector.outputs]
            if execution_count is not None:
                cell["execution_count"] = execution_count
            self._dirty.add(cell_id)
        self._set_outputs(cell_id, items_of(control.collector.outputs))

    async def _finish_run(self, run: Run, control: _RunControl) -> None:
        try:
            while not control.done.is_set():
                try:
                    await asyncio.wait_for(control.done.wait(), SAVE_EVERY_SECONDS)
                except TimeoutError:
                    self._store(run.cell_id, control)  # keep the output so far on the server
                    await self._save()
            reply = control.reply or {}
            if control.abort:
                run.status, run.error = "failed", control.abort
            elif reply.get("status") == "aborted" and not control.collector.has_error():
                run.status, run.error = "failed", "The kernel skipped the cell because an earlier queued cell failed"
            else:
                failed = reply.get("status") == "error" or control.collector.has_error()
                run.status = "error" if failed else "done"
            self._store(run.cell_id, control, reply.get("execution_count") or control.execution_count)
            await self._save()
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc) or type(exc).__name__
        finally:
            if self.kernel is not None:
                self.kernel.handlers.pop(control.msg_id, None)
            if self._controls.get(run.cell_id) is control:
                self._controls.pop(run.cell_id)
            run.finished = time.time()
            self._event("run", f"Cell {run.cell_id} {run.status} after {run.elapsed:.1f} s")

    async def _await_run(self, run: Run, wait_seconds) -> dict:
        wait = max(0.0, min(float(wait_seconds), MAX_WAIT_SECONDS))
        if run.task and not run.task.done():
            await asyncio.wait({run.task}, timeout=wait)
        self._flush()
        items = self.outputs.get(run.cell_id, [])
        if run.finished is None:
            partial = outputs.summarize(items, limit=4_000)
            return ok(
                f"Cell {run.cell_id} is still running after {run.elapsed:.0f} s. It keeps running in the kernel; "
                "call wait_for_cell to get the result, or jupyter_kernel(action='interrupt') to stop it."
                + (f"\nOutput so far:\n{partial}" if partial else "")
            )
        if run.status == "failed":
            partial = outputs.summarize(items, limit=4_000)
            return err(f"Cell {run.cell_id} did not finish: {run.error}" + (f"\nOutput:\n{partial}" if partial else ""))
        paths = outputs.save_images(items, self.home / "outputs", f"jupyter-{run.cell_id}")
        text, images = outputs.render(items, image_paths=paths)
        head = f"Cell {run.cell_id} finished in {run.elapsed:.1f} s" + (
            " with an error." if run.status == "error" else "."
        )
        result = ok(head + ("\n" + text if text else "\n(no output)"), images)
        result["is_error"] = run.status == "error"
        return result

    # ---- operations (called through the hub) -----------------------------------------

    async def op_connect(self, args: dict) -> dict:
        wait = min(float(args.get("wait_seconds", 60)), MAX_WAIT_SECONDS)
        if not await self.bridge.wait_connected(wait):
            return err(f"No JupyterLab tab connected within {wait:.0f} s. " + no_tab_text())
        status, text = await self.bridge.http("GET", "api/status")
        if status != 200:
            return err(_http_error("GET", "api/status", status, text))
        requested = str(args.get("notebook_path") or "").strip().strip("/")
        path = requested or self.path or config.jupyter_notebook()
        if not path.endswith(".ipynb"):
            return err(f"notebook_path must name a .ipynb file, not {path!r}.")
        kernel_name = args.get("kernel") or None
        async with self._edit_lock:
            await self._load_kernelspecs()
            if kernel_name and kernel_name not in self.kernelspecs:
                names = ", ".join(f"{name} ({label})" for name, label in self.kernelspecs.items())
                return err(f"There is no kernel named {kernel_name!r} on this server. Kernels: {names}.")
            if path == self.path and not kernel_name and self.kernel is not None:
                await self._sync()
                note = await self._ensure_kernel()
                return ok(
                    "Already connected; the open notebook was reused.\n"
                    + (note + "\n" if note else "")
                    + self.status_text()
                )
            if self.path != path:
                self._abort_runs(
                    "Another notebook was opened; the cell may still run in its kernel, but its output is no longer collected"
                )
            model = await self._get_notebook(path)
            created = model is None
            if created:
                name = kernel_name or self.default_kernel or next(iter(self.kernelspecs), "python3")
                spec = {"name": name, "display_name": self.kernelspecs.get(name, name), "language": "python"}
                notebook = {"cells": [], "metadata": {"kernelspec": spec}, "nbformat": 4, "nbformat_minor": 5}
                await self._ensure_dirs(posixpath.dirname(path))
                self.runs.clear()
                self._load(path, notebook, None)
                problem = await self._save()
                if problem:
                    return err(f"Could not create {path} on the server: {problem}")
            else:
                self.runs.clear()
                self._load(path, model.get("content") or {}, model.get("last_modified"))
            problem = await self._attach(kernel_name)
            if problem:
                return err(problem)
        self._event("open", f"{'Created' if created else 'Opened'} {path} with the {self.kernel_display} kernel")
        lines = [f"Connected to JupyterLab. {'Created' if created else 'Opened'} {path}."]
        if self.shared:
            lines.append(
                "The notebook is also open in JupyterLab, and Claude shares its running kernel (and variables) "
                "with that tab. The tab shows Claude's edits only after it reloads the notebook from disk: tell "
                "the user to choose Revert, not Overwrite, when JupyterLab says the file changed on disk."
            )
        elif self.reused:
            lines.append("The notebook's kernel was already running, so variables from earlier runs are still there.")
        return ok("\n".join(lines) + "\n" + self.status_text())

    async def op_status(self, args: dict) -> dict:
        if self.bridge.is_connected and self.path is not None:
            try:
                await self._sync()
            except (BridgeError, TimeoutError) as exc:
                log.info("status: could not check the notebook on the server: %s", exc)
        return ok(self.status_text(include_outputs=bool(args.get("include_outputs"))))

    async def op_get_cells(self, args: dict) -> dict:
        self._require_open()
        await self._sync()
        include = bool(args.get("include_outputs"))
        start = int(args.get("start", 0))
        end = int(args["end"]) if args.get("end") is not None else len(self.cells) - 1
        blocks = []
        for index, cell in enumerate(self.cells):
            if not start <= index <= end:
                continue
            block = f"[{index}] id={cell['id']} type={cell['cell_type']} status={self._cell_status(cell['id'])}\n{cell['source']}"
            if include and self.outputs.get(cell["id"]):
                block += "\n--- output ---\n" + outputs.summarize(self.outputs[cell["id"]], limit=4_000)
            blocks.append(block)
        return ok("\n\n".join(blocks) if blocks else "The notebook has no cells in that range.")

    def _insert_at(self, requested) -> int:
        return len(self.cells) if requested is None else max(0, min(int(requested), len(self.cells)))

    async def _add(self, cell_type: str, source: str, requested_index) -> tuple[dict, int, str | None]:
        self._require_open()
        async with self._edit_lock:
            await self._sync()
            index = self._insert_at(requested_index)
            cell = {"cell_type": cell_type, "id": self._new_id(), "metadata": {}, "source": source}
            if cell_type == "code":
                cell.update(outputs=[], execution_count=None)
            self.cells.insert(index, cell)
            self.rev += 1
            problem = await self._save()
        self._event("edit", f"Added {'code' if cell_type == 'code' else 'text'} cell {cell['id']} at index {index}")
        return cell, index, problem

    async def op_add_code_cell(self, args: dict) -> dict:
        cell, index, problem = await self._add("code", args["code"], args.get("cell_index"))
        text = f"Added code cell {cell['id']} at index {index}." + _save_note(problem)
        if not args.get("run"):
            return ok(text)
        report = await self.op_run_code_cell({"cell_id": cell["id"], "wait_seconds": args.get("wait_seconds", 120)})
        report["text"] = text + "\n" + report["text"]
        return report

    async def op_add_text_cell(self, args: dict) -> dict:
        cell, index, problem = await self._add("markdown", args["content"], args.get("cell_index"))
        return ok(f"Added text cell {cell['id']} at index {index}." + _save_note(problem))

    async def _edit(self, cell_id: str, change: Callable[[dict], None], description: str) -> dict:
        self._require_open()
        async with self._edit_lock:
            await self._sync()
            cell = self._find(cell_id)
            if cell is None:
                return err(f"No cell {cell_id} in {self.path}. Call get_cells to see the cell IDs.")
            change(cell)
            self.rev += 1
            problem = await self._save()
        self._event("edit", description)
        return ok(description + "." + _save_note(problem))

    async def op_update_cell(self, args: dict) -> dict:
        content = args["content"]
        return await self._edit(
            args["cell_id"], lambda cell: cell.update(source=content), f"Updated cell {args['cell_id']}"
        )

    async def op_delete_cell(self, args: dict) -> dict:
        # Look self.cells up when the change runs: _sync may have replaced the list by then.
        return await self._edit(
            args["cell_id"], lambda cell: self.cells.remove(cell), f"Deleted cell {args['cell_id']}"
        )

    async def op_move_cell(self, args: dict) -> dict:
        target = int(args["cell_index"])

        def move(cell: dict) -> None:
            self.cells.remove(cell)
            self.cells.insert(max(0, min(target, len(self.cells))), cell)

        return await self._edit(args["cell_id"], move, f"Moved cell {args['cell_id']} to index {target}")

    async def op_run_code_cell(self, args: dict) -> dict:
        cell_id = args["cell_id"]
        run = self.runs.get(cell_id)
        if run is None or run.finished is not None:
            self._require_open()
            async with self._edit_lock:
                await self._sync()
                cell = self._find(cell_id)
                if cell is None:
                    return err(f"No cell {cell_id} in {self.path}. Call get_cells to see the cell IDs.")
                if cell.get("cell_type") != "code":
                    return err(f"Cell {cell_id} is a {cell.get('cell_type')} cell; only code cells run.")
                note = await self._ensure_kernel()
                if note:
                    self._event("kernel", note)
                run = Run(cell_id)
                control = _RunControl()
                self.runs[cell_id] = run
                self._controls[cell_id] = control
                cell["outputs"], cell["execution_count"] = [], None
                self._set_outputs(cell_id, [])
                try:
                    control.msg_id = await self.kernel.execute(
                        cell["source"], functools.partial(self._on_run_message, cell_id, control)
                    )
                except BridgeError as exc:
                    self._controls.pop(cell_id, None)
                    run.status, run.error, run.finished = "failed", str(exc), time.time()
                    return err(f"Cell {cell_id} could not be run: {exc}")
                self._event("run", f"Running cell {cell_id}")
                run.task = self._spawn(self._finish_run(run, control))
        return await self._await_run(run, args.get("wait_seconds", 120))

    async def op_wait_for_cell(self, args: dict) -> dict:
        run = self.runs.get(args["cell_id"])
        if run is None:
            return err(f"Cell {args['cell_id']} has not been run through this server. Use run_code_cell.")
        return await self._await_run(run, args.get("wait_seconds", 120))

    async def op_kernel(self, args: dict) -> dict:
        self._require_open()
        action = str(args.get("action") or "")
        if action not in ("interrupt", "restart", "shutdown"):
            return err("action must be interrupt, restart or shutdown.")
        if self.kernel is None:
            if action != "restart":
                return ok("No kernel is running for this notebook.")
            await self._ensure_kernel()
            return ok(f"Started a new {self.kernel_display} kernel for {self.path}.")
        kernel_path = f"api/kernels/{quote(self.kernel.id)}"
        if action == "interrupt":
            await self._request("POST", kernel_path + "/interrupt")
            self._event("kernel", "Interrupted the kernel")
            return ok("Interrupted the kernel. A running cell stops with KeyboardInterrupt; wait_for_cell reports it.")
        if action == "restart":
            self._abort_runs("The kernel was restarted (jupyter_kernel); its variables are lost.")
            await self._request("POST", kernel_path + "/restart", timeout=120)
            self._event("kernel", "Restarted the kernel")
            return ok("Restarted the kernel: all variables are gone. Cells and outputs stay in the notebook.")
        # shutdown
        self._abort_runs("The kernel was shut down (jupyter_kernel).")
        if self.session_id:
            await self._request("DELETE", f"api/sessions/{quote(self.session_id)}")
        else:
            await self._request("DELETE", kernel_path)
        await self.kernel.close()
        self.kernel, self.session_id = None, None
        self._event("kernel", "Shut down the kernel")
        return ok(
            f"Shut down the kernel of {self.path}. The notebook stays on the server; running a cell starts a new kernel."
        )

    async def call(self, op: str, args: dict) -> dict:
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            return err(f"Unknown operation: jupyter_{op}")
        try:
            return await handler(args)
        except NotOpen:
            return err("No JupyterLab notebook is open. Call jupyter_connect first.")
        except BridgeGone:
            return err("No JupyterLab tab is connected, or it went away. " + no_tab_text())
        except BridgeError as exc:
            return err(str(exc))
        except TimeoutError:
            return err(
                "The JupyterLab tab did not answer in time. The tab may have been put to sleep by the browser, "
                "or the SSH tunnel may be down."
            )
        except KeyError as exc:
            return err(f"Missing argument: {exc.args[0]}")

    # ---- reports and the mirror ------------------------------------------------------

    def status_text(self, include_outputs: bool = False) -> str:
        info = self.bridge.info
        lines = []
        if self.bridge.is_connected:
            app = " ".join(part for part in (info.get("app_name") or "JupyterLab", info.get("app_version")) if part)
            lines.append(
                f"Connected to {app} at {info.get('origin') or 'an unknown address'} through the user's browser tab."
            )
        else:
            lines.append("No JupyterLab tab is connected. " + no_tab_text())
        if self.path is not None:
            saved = time.strftime(", last saved %H:%M:%S", time.localtime(self.last_saved)) if self.last_saved else ""
            lines.append(f"Notebook: {self.path} on the Jupyter server (saved after each change{saved}).")
            if self.kernel is not None:
                shared = ", also open in JupyterLab" if self.shared else ""
                lines.append(f"Kernel: {self.kernel_display or self.kernel.name} ({self.kernel.state}{shared}).")
            else:
                lines.append("Kernel: none running; running a cell starts one.")
        if self.kernelspecs:
            lines.append(
                "Kernels on the server: " + ", ".join(f"{name} ({label})" for name, label in self.kernelspecs.items())
            )
        lines.append(f"Mirror page (open in the built-in browser to watch): {config.jupyter_mirror_url()}")
        if self.path is None:
            lines.append("No notebook is open yet: call jupyter_connect.")
            return "\n".join(lines)
        lines.append(f"Cells ({len(self.cells)}):")
        for index, cell in enumerate(self.cells):
            source_lines = cell["source"].splitlines() or [""]
            more = f" (+{len(source_lines) - 1} lines)" if len(source_lines) > 1 else ""
            status = self._cell_status(cell["id"])
            run = self.runs.get(cell["id"])
            timing = f" {run.elapsed:.0f}s" if run else ""
            state = "" if status == "idle" else f" [{status.replace('_', ' ')}{timing}]"
            lines.append(f"  [{index}] {cell['id']} {cell.get('cell_type')}{state}: {source_lines[0][:90]}{more}")
            if include_outputs and self.outputs.get(cell["id"]):
                lines.extend("      " + line for line in outputs.summarize(self.outputs[cell["id"]]).splitlines())
        return "\n".join(lines)

    def snapshot(self) -> dict:
        cells = []
        for index, cell in enumerate(self.cells):
            run = self.runs.get(cell["id"])
            cells.append(
                {
                    "id": cell["id"],
                    "index": index,
                    "type": cell.get("cell_type", "code"),
                    "source": cell["source"],
                    "status": self._cell_status(cell["id"]),
                    "started": run.started if run else None,
                    "finished": run.finished if run else None,
                    "rev": self.cell_rev.get(cell["id"], 0),
                    "outputs": len(self.outputs.get(cell["id"], [])),
                }
            )
        origin = str(self.bridge.info.get("origin") or "")
        where = origin.split("//", 1)[-1]
        kernel = f"{self.kernel_display or self.kernel.name} · {self.kernel.state}" if self.kernel else None
        return {
            "kind": "jupyter",
            "connected": self.bridge.is_connected,
            "rev": self.rev,
            "now": time.time(),
            "notebook": f"{self.path} on {where}" if self.path and where else self.path,
            "kernel": kernel,
            "cells": cells,
            "events": list(self.events)[-40:],
        }
