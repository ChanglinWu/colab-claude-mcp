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
# Modified for colab-claude-mcp: rewritten from websocket_server.py and session.py.
# The server now uses a fixed port and token, accepts reconnects, lets the newest
# Colab tab replace the previous one, and has no message size limit.

"""WebSocket endpoint that a Colab browser tab connects to.

The Colab page is the MCP *server* (it owns the notebook tools); this side is the
MCP client. A connection lasts as long as the tab stays open and this process runs.
"""

import asyncio
from collections.abc import Callable
import hmac
import json
import logging
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.typing import Subprotocol

from colab_claude_mcp.config import COLAB, COLAB_ALT_DOMAIN, VERSION

PROTOCOL_VERSION = "2025-06-18"
HANDSHAKE_TIMEOUT = 20.0

log = logging.getLogger(__name__)


class FrontendError(Exception):
    """The Colab page answered a request with an error."""


class NotConnected(FrontendError):
    """No Colab tab is connected, or the tab went away mid-request."""


class _Connection:
    """JSON-RPC over one websocket to one Colab tab."""

    def __init__(self, ws: ServerConnection):
        self.ws = ws
        self.tools: list[dict] = []
        self.server_info: dict = {}
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0

    async def request(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            await self.ws.send(json.dumps(message))
            response = await asyncio.wait_for(future, timeout)
        except ConnectionClosed as exc:
            raise NotConnected("The Colab tab disconnected") from exc
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            raise FrontendError(error.get("message") if isinstance(error, dict) else str(error))
        return response.get("result") or {}

    async def notify(self, method: str) -> None:
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "method": method}))

    async def read_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    log.warning("ignoring a non-JSON message from Colab")
                    continue
                await self._dispatch(message)
        except ConnectionClosed:
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(NotConnected("The Colab tab disconnected"))

    async def _dispatch(self, message: dict) -> None:
        request_id = message.get("id")
        if "method" not in message:
            future = self._pending.get(request_id)
            if future and not future.done():
                future.set_result(message)
            return
        method = message["method"]
        if request_id is None:
            if method == "notifications/tools/list_changed":
                asyncio.get_running_loop().create_task(self.refresh_tools())
            return
        # A request from the Colab page to us. Only ping is supported.
        if method == "ping":
            reply = {"jsonrpc": "2.0", "id": request_id, "result": {}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        await self.ws.send(json.dumps(reply))

    async def refresh_tools(self) -> None:
        try:
            result = await self.request("tools/list", timeout=HANDSHAKE_TIMEOUT)
            self.tools = result.get("tools", [])
        except Exception as exc:  # the tab may be closing
            log.info("could not refresh the Colab tool list: %s", exc)


class ColabFrontend:
    """Accepts the Colab tab's websocket and exposes its notebook tools."""

    def __init__(
        self,
        token: str,
        port: int,
        host: str = "localhost",
        on_change: Callable[[bool], None] | None = None,
    ):
        self.token = token
        self.port = port
        self.host = host
        self.generation = 0  # increases with every accepted tab
        self._on_change = on_change
        self._conn: _Connection | None = None
        self._connected = asyncio.Event()
        self._server: websockets.Server | None = None

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    @property
    def tools(self) -> list[dict]:
        return self._conn.tools if self._conn else []

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handler,
            host=self.host,
            port=self.port,
            subprotocols=[Subprotocol("mcp")],
            origins=[COLAB, COLAB_ALT_DOMAIN],
            process_request=self._validate_authorization,
            max_size=None,  # cell outputs (plots, animations) are often many MB
        )
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("waiting for a Colab tab on ws://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def wait_connected(self, timeout: float, after_generation: int | None = None) -> bool:
        """Wait for a tab. With after_generation, wait for a tab newer than that one."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self.is_connected and (after_generation is None or self.generation > after_generation):
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.2, remaining))

    async def call_tool(self, name: str, arguments: dict | None = None, timeout: float | None = None) -> dict:
        conn = self._conn
        if conn is None:
            raise NotConnected("No Colab tab is connected")
        return await conn.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)

    def _validate_authorization(self, websocket: ServerConnection, request: Request):
        query = parse_qs(urlsplit(request.path).query)
        candidates = list(query.get("access_token", []))
        header = request.headers.get("Authorization")
        if header:
            scheme, _, value = header.partition(" ")
            if scheme.lower() != "bearer" or not value:
                return Response(400, "Invalid authorization header", Headers([]))
            candidates.append(value.strip())
        if not candidates:
            return Response(401, "Missing authorization", Headers([]))
        if any(hmac.compare_digest(candidate, self.token) for candidate in candidates):
            return None
        return Response(403, "Bad authorization token", Headers([]))

    async def _handler(self, ws: ServerConnection) -> None:
        conn = _Connection(ws)
        reader = asyncio.get_running_loop().create_task(conn.read_loop())
        try:
            init = await conn.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "colab-claude-mcp", "version": VERSION},
                },
                timeout=HANDSHAKE_TIMEOUT,
            )
            conn.server_info = init.get("serverInfo", {})
            await conn.notify("notifications/initialized")
            await conn.refresh_tools()
        except Exception as exc:
            log.warning("handshake with the Colab tab failed: %s", exc)
            reader.cancel()
            return

        previous, self._conn = self._conn, conn
        self.generation += 1
        self._connected.set()
        log.info("Colab tab connected (generation %s, %s tools)", self.generation, len(conn.tools))
        if previous is not None:
            # The newest tab wins, so that "open a new notebook" switches over to it.
            await previous.ws.close(code=1000, reason="Replaced by a newer Colab tab")
        self._notify(True)
        try:
            await reader
        finally:
            if self._conn is conn:
                self._conn = None
                self._connected.clear()
                log.info("Colab tab disconnected")
                self._notify(False)

    def _notify(self, connected: bool) -> None:
        if self._on_change:
            try:
                self._on_change(connected)
            except Exception:
                log.exception("on_change callback failed")
