"""Stand-ins for a Jupyter server and for a JupyterLab tab that runs the bridge script.

FakeTab speaks the bridge protocol to the hub over a real websocket, the way jupyter_bridge.js does,
and answers the relayed requests from a FakeServer instead of a real Jupyter server.
"""

import asyncio
import json
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

import websockets

from colab_claude_mcp.config import VERSION
from tests.fake_colab import PNG

ORIGIN = "http://localhost:8899"


def _msg(channel: str, msg_type: str, parent: dict, content: dict) -> dict:
    header = {"msg_id": uuid.uuid4().hex, "msg_type": msg_type}
    return {
        "channel": channel,
        "msg_type": msg_type,
        "header": header,
        "parent_header": parent,
        "metadata": {},
        "content": content,
    }


class FakeKernel:
    """Runs 'code' by keyword: plot, slow, forever (until interrupted), boom, progress."""

    def __init__(self, kernel_id: str, name: str):
        self.id = kernel_id
        self.name = name
        self.count = 0
        self.listeners: set = set()  # one per open websocket; iopub goes to all of them
        self.interrupted = asyncio.Event()
        self.executed: list[str] = []
        self._tasks: set[asyncio.Task] = set()

    def broadcast(self, message: dict) -> None:
        for listener in list(self.listeners):
            listener(message)

    def handle(self, message: dict, reply) -> None:
        if (message.get("header") or {}).get("msg_type") == "execute_request":
            task = asyncio.create_task(self._execute(message, reply))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _execute(self, request: dict, reply) -> None:
        parent = request["header"]
        code = request["content"]["code"]
        self.executed.append(code)
        self.count += 1
        count = self.count

        def iopub(msg_type: str, content: dict) -> None:
            self.broadcast(_msg("iopub", msg_type, parent, content))

        iopub("status", {"execution_state": "busy"})
        iopub("execute_input", {"code": code, "execution_count": count})
        first = code.splitlines()[0] if code else ""
        iopub("stream", {"name": "stdout", "text": f"ran: {first}\n"})
        status = "ok"
        if "progress" in code:
            for percent in (0, 25, 50, 75, 100):
                iopub("stream", {"name": "stderr", "text": f"\r{percent:3d}%"})
            iopub("stream", {"name": "stderr", "text": "\n"})
        if "plot" in code:
            iopub("display_data", {"data": {"text/plain": "<Figure>", "image/png": PNG}, "metadata": {}})
        if "slow" in code:
            await asyncio.sleep(1.0)
        if "forever" in code:
            self.interrupted.clear()
            await self.interrupted.wait()
            iopub(
                "error",
                {"ename": "KeyboardInterrupt", "evalue": "", "traceback": ["\x1b[0;31mKeyboardInterrupt\x1b[0m"]},
            )
            status = "error"
        if "boom" in code:
            traceback = ["\x1b[0;31mZeroDivisionError\x1b[0m: division by zero"]
            iopub("error", {"ename": "ZeroDivisionError", "evalue": "division by zero", "traceback": traceback})
            status = "error"
        reply(_msg("shell", "execute_reply", parent, {"status": status, "execution_count": count}))
        iopub("status", {"execution_state": "idle"})

    def restart(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self.count = 0
        self.broadcast(_msg("iopub", "status", {}, {"execution_state": "restarting"}))
        self.broadcast(_msg("iopub", "status", {}, {"execution_state": "idle"}))


class FakeServer:
    """The REST side of a Jupyter server: contents, sessions, kernel specs, interrupt and restart."""

    def __init__(self):
        self.files: dict[str, dict] = {"": {"type": "directory"}}
        self.sessions: list[dict] = []
        self.kernels: dict[str, FakeKernel] = {}
        self.clock = 0
        self.logged_in = True
        self.requests: list[tuple[str, str]] = []
        self.specs = {
            "default": "python3",
            "kernelspecs": {
                "python3": {"name": "python3", "spec": {"display_name": "Python 3 (ipykernel)", "language": "python"}},
                "torch": {"name": "torch", "spec": {"display_name": "PyTorch (CUDA)", "language": "python"}},
            },
        }

    def stamp(self) -> str:
        self.clock += 1
        return f"2026-11-28T10:{self.clock // 60:02d}:{self.clock % 60:02d}.000000Z"

    def save(self, path: str, notebook: dict) -> None:
        """Save a notebook the way the user's own JupyterLab tab would."""
        self.files[path] = {"type": "notebook", "content": notebook, "last_modified": self.stamp()}

    def notebook(self, path: str) -> dict:
        return self.files[path]["content"]

    def start_session(self, path: str, kernel_name: str) -> dict:
        kernel = FakeKernel(uuid.uuid4().hex, kernel_name)
        self.kernels[kernel.id] = kernel
        session = {
            "id": uuid.uuid4().hex,
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "type": "notebook",
            "kernel": {"id": kernel.id, "name": kernel_name, "execution_state": "idle"},
        }
        self.sessions.append(session)
        return session

    def http(self, method: str, path: str, body: str | None) -> tuple[int, str]:
        self.requests.append((method, path))
        if not self.logged_in:
            return 403, json.dumps({"message": "Forbidden"})
        url = urlsplit(path)
        route = unquote(url.path)
        query = parse_qs(url.query)
        data = json.loads(body) if body else None
        if route == "api/status":
            return 200, json.dumps({"kernels": len(self.kernels)})
        if route == "api/kernelspecs":
            return 200, json.dumps(self.specs)
        if route == "api/contents" or route.startswith("api/contents/"):
            return self._contents(method, route[len("api/contents") :].strip("/"), query, data)
        if route == "api/sessions":
            if method == "GET":
                for session in self.sessions:  # like Jupyter Server: how many websockets each kernel has
                    kernel = self.kernels.get(session["kernel"]["id"])
                    session["kernel"]["connections"] = len(kernel.listeners) if kernel else 0
                return 200, json.dumps(self.sessions)
            existing = next((s for s in self.sessions if s["path"] == data["path"]), None)
            return 201, json.dumps(existing or self.start_session(data["path"], data["kernel"]["name"]))
        parts = route.split("/")
        if len(parts) == 3 and parts[:2] == ["api", "sessions"] and method == "DELETE":
            for session in [s for s in self.sessions if s["id"] == parts[2]]:
                self.sessions.remove(session)
                self.kernels.pop(session["kernel"]["id"], None)
            return 204, ""
        if len(parts) == 4 and parts[:2] == ["api", "kernels"] and method == "POST":
            kernel = self.kernels.get(parts[2])
            if kernel is None:
                return 404, json.dumps({"message": "Kernel does not exist"})
            if parts[3] == "interrupt":
                kernel.interrupted.set()
                return 204, ""
            if parts[3] == "restart":
                kernel.restart()
                return 200, json.dumps({"id": kernel.id, "name": kernel.name})
        return 404, json.dumps({"message": f"No route for {method} {route}"})

    def _contents(self, method: str, path: str, query: dict, data: dict | None) -> tuple[int, str]:
        entry = self.files.get(path)
        if method == "GET":
            if entry is None:
                return 404, json.dumps({"message": f"No such file or directory: {path}"})
            model = {
                "name": path.rsplit("/", 1)[-1],
                "path": path,
                "type": entry["type"],
                "last_modified": entry.get("last_modified", "2026-01-01T00:00:00.000000Z"),
                "content": entry.get("content") if query.get("content", ["1"])[0] == "1" else None,
            }
            return 200, json.dumps(model)
        if method == "PUT":
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            if parent not in self.files:
                return 404, json.dumps({"message": f"No such directory: {parent}"})
            stamp = self.stamp()
            if data["type"] == "directory":
                self.files[path] = {"type": "directory", "last_modified": stamp}
            else:
                self.files[path] = {"type": "notebook", "content": data["content"], "last_modified": stamp}
            model = {"path": path, "type": data["type"], "last_modified": stamp, "content": None}
            return (201 if entry is None else 200), json.dumps(model)
        return 405, json.dumps({"message": "Method not allowed"})


class FakeTab:
    """A JupyterLab tab after the bookmark was clicked: relays the hub's requests to a FakeServer."""

    def __init__(self, port: int, token: str, server: FakeServer, origin: str = ORIGIN, bridge: str = VERSION):
        self.url = f"ws://localhost:{port}/jupyter/ws?token={token}"
        self.server = server
        self.origin = origin
        self.bridge = bridge
        self.ws = None
        self.received: list[dict] = []  # welcome, reload and error messages
        self.channels: dict[str, tuple[FakeKernel, object]] = {}
        self.closed_code: int | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

    async def connect(self) -> "FakeTab":
        self.ws = await websockets.connect(self.url, origin=self.origin, max_size=None)
        self._tasks = [asyncio.create_task(self._write()), asyncio.create_task(self._serve())]
        self.emit(
            {
                "type": "hello",
                "origin": self.origin,
                "href": self.origin + "/lab",
                "base_url": "/",
                "app_name": "JupyterLab",
                "app_version": "4.5.0",
                "tree_path": "",
                "bridge": self.bridge,
            }
        )
        return self

    def emit(self, message: dict) -> None:
        self._queue.put_nowait(json.dumps(message))  # one writer keeps the messages in order

    async def _write(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self.ws.send(text)
            except websockets.ConnectionClosed:
                return

    async def _serve(self) -> None:
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "http":
                    status, text = self.server.http(message["method"], message["path"], message.get("body"))
                    self.emit({"type": "reply", "id": message["id"], "status": status, "text": text})
                elif kind == "ws_open":
                    self._open(message)
                elif kind == "ws_send":
                    entry = self.channels.get(message["chan"])
                    if entry:
                        entry[0].handle(json.loads(message["data"]), entry[1])
                elif kind == "ws_close":
                    entry = self.channels.pop(message["chan"], None)
                    if entry:
                        entry[0].listeners.discard(entry[1])
                else:
                    self.received.append(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.closed_code = self.ws.close_code
            for kernel, listener in self.channels.values():
                kernel.listeners.discard(listener)
            self.channels.clear()

    def _open(self, message: dict) -> None:
        parts = urlsplit(message["path"]).path.split("/")
        kernel = self.server.kernels.get(parts[2]) if len(parts) == 4 and parts[3] == "channels" else None
        if kernel is None:
            self.emit({"type": "reply", "id": message["id"], "ok": False, "error": "closed with code 1006"})
            return
        chan = message["chan"]

        def listener(msg: dict) -> None:
            self.emit({"type": "ws_msg", "chan": chan, "data": json.dumps(msg)})

        kernel.listeners.add(listener)
        self.channels[chan] = (kernel, listener)
        self.emit({"type": "reply", "id": message["id"], "ok": True})

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        await asyncio.gather(*self._tasks[1:], return_exceptions=True)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
