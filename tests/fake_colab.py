"""A stand-in for the Colab page: an MCP server reached over the hub's websocket."""

import asyncio
import json

import websockets

from colab_claude_mcp.config import COLAB

PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
TOOLS = ["add_code_cell", "add_text_cell", "delete_cell", "get_cells", "move_cell", "run_code_cell", "update_cell"]


class FakeColab:
    def __init__(self, port: int, token: str):
        self.url = f"ws://localhost:{port}/?access_token={token}"
        self.cells: list[dict] = [{"cell_type": "code", "id": "first", "source": [], "outputs": []}]
        self.counter = 0
        self.ws = None
        self.task: asyncio.Task | None = None
        self.closed_code: int | None = None

    async def connect(self, origin: str = COLAB):
        self.ws = await websockets.connect(self.url, subprotocols=["mcp"], origin=origin, max_size=None)
        self.task = asyncio.create_task(self._serve())
        return self

    async def close(self):
        if self.ws:
            await self.ws.close()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)

    async def _serve(self):
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                if "id" in message and "method" in message:
                    asyncio.create_task(self._answer(message))
        except websockets.ConnectionClosed:
            pass
        self.closed_code = self.ws.close_code

    async def _answer(self, message):
        method, params = message["method"], message.get("params") or {}
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "ColabMCP", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [{"name": name, "description": name, "inputSchema": {"type": "object"}} for name in TOOLS]
            }
        elif method == "tools/call":
            try:
                payload = await self._call(params["name"], params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
            except KeyError as exc:
                result = {"content": [{"type": "text", "text": f"No cell {exc}"}], "isError": True}
        else:
            result = {}
        try:
            await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}))
        except websockets.ConnectionClosed:
            pass

    def _find(self, cell_id):
        for cell in self.cells:
            if cell["id"] == cell_id:
                return cell
        raise KeyError(cell_id)

    async def _call(self, name, args):
        if name in ("add_code_cell", "add_text_cell"):
            self.counter += 1
            cell_id = f"cell{self.counter}"
            code = name == "add_code_cell"
            cell = {
                "cell_type": "code" if code else "markdown",
                "id": cell_id,
                "source": (args["code"] if code else args["content"]).splitlines(keepends=True),
                "outputs": [],
            }
            self.cells.insert(args["cellIndex"], cell)
            return {"newCellId": cell_id}
        if name == "get_cells":
            keep = ("cell_type", "id", "source") + (("outputs",) if args.get("includeOutputs") else ())
            return {"cells": [{key: cell[key] for key in keep} for cell in self.cells]}
        if name == "update_cell":
            self._find(args["cellId"])["source"] = args["content"].splitlines(keepends=True)
            return {}
        if name == "delete_cell":
            self.cells.remove(self._find(args["cellId"]))
            return {}
        if name == "move_cell":
            cell = self._find(args["cellId"])
            self.cells.remove(cell)
            self.cells.insert(args["cellIndex"], cell)
            return {}
        if name == "run_code_cell":
            cell = self._find(args["cellId"])
            source = "".join(cell["source"])
            outputs = [{"output_type": "stream", "name": "stdout", "text": [f"ran: {source.splitlines()[0]}\n"]}]
            if "slow" in source:
                await asyncio.sleep(1.0)
            if "plot" in source:
                outputs.append({"output_type": "display_data", "data": {"text/plain": ["<Figure>"], "image/png": PNG}})
            if "big" in source:
                outputs.append(
                    {"output_type": "display_data", "data": {"text/html": ["<p>" + "x" * 3_000_000 + "</p>"]}}
                )
            if "boom" in source:
                outputs.append(
                    {
                        "output_type": "error",
                        "ename": "ZeroDivisionError",
                        "evalue": "division by zero",
                        "traceback": ["\x1b[0;31mZeroDivisionError\x1b[0m: division by zero"],
                    }
                )
            cell["outputs"] = outputs
            return {"outputs": outputs}
        raise KeyError(name)
