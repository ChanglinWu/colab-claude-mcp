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

"""The hub: one long-lived local process that owns the Colab tab connection.

Claude Code starts a fresh stdio MCP server for every session. If that process
owned the websocket, every new session would need a new Colab tab. The hub keeps
the websocket instead, so the tab stays connected while sessions come and go.
It also keeps a copy of the notebook (cells, run status, outputs) and serves it
as a web page, the mirror, which can be opened in Claude's built-in browser.
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import time
import webbrowser

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route
import uvicorn

from colab_claude_mcp import config, matlab_view, outputs
from colab_claude_mcp.frontend import ColabFrontend, FrontendError, NotConnected

log = logging.getLogger(__name__)

LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
IDLE_EXIT_SECONDS = 2 * 60 * 60  # with no tab connected and no requests
MIRROR_ACTIVE_SECONDS = 15
SOURCE_REFRESH_SECONDS = 2
FULL_REFRESH_SECONDS = 10
MAX_WAIT_SECONDS = 600
PENDING_TAB_SECONDS = 15 * 60  # how long an opened tab that has not connected is waited for


def ok(text: str, images: list[dict] | None = None) -> dict:
    return {"text": text, "images": images or [], "is_error": False}


def err(text: str) -> dict:
    return {"text": text, "images": [], "is_error": True}


@dataclass
class Run:
    cell_id: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    status: str = "running"  # running | done | error | failed
    error: str = ""
    task: asyncio.Task | None = None

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started


class Hub:
    def __init__(
        self,
        token: str,
        ws_port: int,
        http_port: int,
        home: Path | None = None,
        open_browser=None,
    ):
        self.token = token
        self.http_port = http_port
        self.home = home or config.home()
        self.frontend = ColabFrontend(token, ws_port, on_change=self._frontend_changed)
        self.cells: list[dict] = []  # {"id", "type", "source"}
        self.outputs: dict[str, list[dict]] = {}
        self.cell_rev: dict[str, int] = {}
        self.runs: dict[str, Run] = {}
        self.events: deque[dict] = deque(maxlen=200)
        self.rev = 0
        self.notebook_url: str | None = None
        self.started = time.time()
        self.last_activity = time.time()
        self.last_mirror = 0.0
        self.pending_tab_at: float | None = None  # a tab was opened and has not connected yet
        self.pending_notebook: str | None = None
        self._digests: dict[str, str] = {}
        self._cells_digest = ""
        self._last_full_refresh = 0.0
        self._full_refresh_cost = 0.0
        self._refresh_lock = asyncio.Lock()
        self._open_browser_fn = open_browser or _open_browser
        self._server: uvicorn.Server | None = None
        self._tasks: set[asyncio.Task] = set()

    # ---- state -----------------------------------------------------------

    def _event(self, kind: str, text: str) -> None:
        self.events.append({"t": time.time(), "kind": kind, "text": text})
        self.rev += 1

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _frontend_changed(self, connected: bool) -> None:
        if connected:
            self.pending_tab_at = None
            self._event("connect", "Colab tab connected")
            self._spawn(self._safe_refresh(include_outputs=True))
        else:
            self._event("disconnect", f"Colab tab disconnected ({self.frontend.last_close})")

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

    async def _safe_refresh(self, include_outputs: bool = False) -> None:
        try:
            await self.refresh(include_outputs)
        except (FrontendError, asyncio.TimeoutError, ValueError) as exc:
            log.info("refresh failed: %s", exc)

    async def refresh(self, include_outputs: bool = False) -> None:
        """Copy the notebook's cells (and optionally their outputs) from the Colab tab."""
        if not self.frontend.is_connected:
            return
        async with self._refresh_lock:
            began = time.time()
            result = await self.frontend.call_tool("get_cells", {"includeOutputs": include_outputs}, timeout=120)
            data = json.loads(outputs.tool_text(result) or "{}")
            cells = []
            for cell in data.get("cells", []):
                cell_id = str(cell.get("id"))
                cells.append(
                    {
                        "id": cell_id,
                        "type": cell.get("cell_type", "code"),
                        "source": outputs._join(cell.get("source")),
                    }
                )
                if include_outputs and cell.get("cell_type", "code") == "code":
                    items = outputs.normalize(cell.get("outputs"))
                    # While a cell runs, the notebook may hold nothing yet; keep what we have.
                    if items or not self._running(cell_id):
                        self._set_outputs(cell_id, items)
            digest = hashlib.blake2b(json.dumps(cells).encode(), digest_size=16).hexdigest()
            if digest != self._cells_digest:
                self._cells_digest = digest
                self.cells = cells
                self.rev += 1
                live = {cell["id"] for cell in cells}
                for stale in [cell_id for cell_id in self.outputs if cell_id not in live]:
                    self.outputs.pop(stale, None)
                    self._digests.pop(stale, None)
                    self.cell_rev.pop(stale, None)
                    if not self._running(stale):
                        self.runs.pop(stale, None)
            if include_outputs:
                self._last_full_refresh = time.time()
                self._full_refresh_cost = time.time() - began

    # ---- background ------------------------------------------------------

    async def _background(self) -> None:
        while True:
            await asyncio.sleep(SOURCE_REFRESH_SECONDS)
            now = time.time()
            if self.frontend.is_connected:
                self.last_activity = now
                watching = now - self.last_mirror < MIRROR_ACTIVE_SECONDS
                running = any(run.finished is None for run in self.runs.values())
                if watching or running:
                    # A full refresh costs as much as the outputs are large; back off accordingly.
                    # While a cell runs, its partial output is the progress report, so poll faster.
                    base = SOURCE_REFRESH_SECONDS if running else FULL_REFRESH_SECONDS
                    interval = max(base, 20 * self._full_refresh_cost)
                    await self._safe_refresh(include_outputs=now - self._last_full_refresh >= interval)
            elif now - self.last_activity > IDLE_EXIT_SECONDS:
                log.info("idle with no Colab tab for %s s; exiting", IDLE_EXIT_SECONDS)
                self.shutdown()
                return

    # ---- operations (called by the stdio server) -------------------------

    def tab_url(self, notebook_url: str | None = None) -> str:
        base = notebook_url or (config.COLAB + config.SCRATCH_PATH)
        if not base.startswith((config.COLAB + "/", config.COLAB_ALT_DOMAIN + "/")):
            raise ValueError(f"Not a Colab notebook URL: {base}")
        base = base.split("#", 1)[0]
        return f"{base}#mcpProxyToken={self.token}&mcpProxyPort={self.frontend.port}"

    def status_text(self, include_outputs: bool = False) -> str:
        lines = []
        if self.frontend.is_connected:
            lines.append(f"Connected to a Colab tab. Notebook: {self.notebook_url or 'opened outside this server'}")
        else:
            lines.append("Not connected to Colab. Call colab_connect.")
        lines.append(f"Mirror page (open in the built-in browser to watch): {config.mirror_url()}")
        if not self.frontend.is_connected:
            return "\n".join(lines)
        lines.append(f"Cells ({len(self.cells)}):")
        for index, cell in enumerate(self.cells):
            lines.append(self._cell_line(index, cell))
            if include_outputs and self.outputs.get(cell["id"]):
                summary = outputs.summarize(self.outputs[cell["id"]])
                lines.extend("      " + line for line in summary.splitlines())
        return "\n".join(lines)

    def _cell_line(self, index: int, cell: dict) -> str:
        source_lines = cell["source"].splitlines() or [""]
        first = source_lines[0][:90]
        more = f" (+{len(source_lines) - 1} lines)" if len(source_lines) > 1 else ""
        status = self._cell_status(cell["id"])
        run = self.runs.get(cell["id"])
        timing = f" {run.elapsed:.0f}s" if run else ""
        state = "" if status == "idle" else f" [{status.replace('_', ' ')}{timing}]"
        return f"  [{index}] {cell['id']} {cell['type']}{state}: {first}{more}"

    async def op_connect(self, args: dict) -> dict:
        new_notebook = bool(args.get("new_notebook"))
        notebook_url = args.get("notebook_url") or None
        open_tab = args.get("open_tab", True)
        wait = min(float(args.get("wait_seconds", 60)), MAX_WAIT_SECONDS)
        if self.frontend.is_connected and not new_notebook and not notebook_url:
            await self._safe_refresh(include_outputs=True)
            return ok("Already connected; the open Colab tab was reused and no tab was opened.\n" + self.status_text())
        generation = self.frontend.generation
        # A tab opened a moment ago that has not connected is almost always waiting for a Google
        # sign-in. Opening another one would not help, so wait for that one instead.
        pending_for = time.time() - self.pending_tab_at if self.pending_tab_at else None
        waiting_on_pending = bool(
            open_tab
            and not new_notebook
            and not notebook_url
            and pending_for is not None
            and pending_for < PENDING_TAB_SECONDS
        )
        if waiting_on_pending:
            open_tab = False
        if open_tab:
            try:
                url = self.tab_url(notebook_url)
            except ValueError as exc:
                return err(str(exc))
            opened = await asyncio.to_thread(self._open_browser_fn, url, args.get("browser"))
            if not opened:
                return err(
                    "Could not open a browser. Open this URL in a browser that is signed in to Google, "
                    f"then call colab_connect with open_tab=false:\n{url}"
                )
            self._event("open", "Opened " + (notebook_url or "a new scratch notebook") + " in the browser")
            self.pending_tab_at = time.time()
            self.pending_notebook = notebook_url
        connected = await self.frontend.wait_connected(wait, after_generation=generation if open_tab else None)
        if not connected:
            if waiting_on_pending:
                return err(
                    f"The Colab tab opened {pending_for + wait:.0f} s ago has still not connected, so no further tab "
                    "was opened. It is usually waiting for a Google sign-in: ask the user to sign in there, then "
                    "call colab_connect again. If that tab was closed, call colab_connect with new_notebook=true."
                )
            if open_tab:
                return err(
                    f"Opened a Colab tab, but it did not connect within {wait:.0f} s. It is usually waiting for a "
                    "Google sign-in (look for a 'Sign in' prompt in it). Ask the user to sign in there, then call "
                    "colab_connect again; it will wait for that tab instead of opening another."
                )
            return err(f"No Colab tab connected within {wait:.0f} s.")
        if waiting_on_pending:
            self.notebook_url = self.pending_notebook or "new scratch notebook (not saved to Drive)"
            self.runs.clear()
        if open_tab:
            self.notebook_url = notebook_url or "new scratch notebook (not saved to Drive)"
            self.runs.clear()
        await self._safe_refresh(include_outputs=True)
        return ok("Connected to Colab.\n" + self.status_text())

    async def op_status(self, args: dict) -> dict:
        if self.frontend.is_connected:
            await self._safe_refresh(include_outputs=bool(args.get("include_outputs")))
        return ok(self.status_text(include_outputs=bool(args.get("include_outputs"))))

    async def op_get_cells(self, args: dict) -> dict:
        include = bool(args.get("include_outputs"))
        await self.refresh(include_outputs=include)
        start = int(args.get("start", 0))
        end = int(args["end"]) if args.get("end") is not None else len(self.cells) - 1
        blocks = []
        for index, cell in enumerate(self.cells):
            if not start <= index <= end:
                continue
            status = self._cell_status(cell["id"])
            block = f"[{index}] id={cell['id']} type={cell['type']} status={status}\n{cell['source']}"
            if include and self.outputs.get(cell["id"]):
                block += "\n--- output ---\n" + outputs.summarize(self.outputs[cell["id"]], limit=4_000)
            blocks.append(block)
        return ok("\n\n".join(blocks) if blocks else "The notebook has no cells in that range.")

    async def op_add_code_cell(self, args: dict) -> dict:
        index = await self._index(args.get("cell_index"))
        result = await self.frontend.call_tool(
            "add_code_cell",
            {"cellIndex": index, "language": args.get("language", "python"), "code": args["code"]},
            timeout=60,
        )
        cell_id = self._new_cell_id(result)
        await self._safe_refresh()
        if result.get("isError") or not cell_id:
            return err("Colab did not add the cell: " + outputs.tool_text(result))
        self._event("edit", f"Added code cell {cell_id} at index {index}")
        text = f"Added code cell {cell_id} at index {index}."
        if not args.get("run"):
            return ok(text)
        report = await self.op_run_code_cell({"cell_id": cell_id, "wait_seconds": args.get("wait_seconds", 120)})
        report["text"] = text + "\n" + report["text"]
        return report

    async def op_add_text_cell(self, args: dict) -> dict:
        index = await self._index(args.get("cell_index"))
        result = await self.frontend.call_tool(
            "add_text_cell", {"cellIndex": index, "content": args["content"]}, timeout=60
        )
        cell_id = self._new_cell_id(result)
        await self._safe_refresh()
        if result.get("isError") or not cell_id:
            return err("Colab did not add the cell: " + outputs.tool_text(result))
        self._event("edit", f"Added text cell {cell_id} at index {index}")
        return ok(f"Added text cell {cell_id} at index {index}.")

    async def op_update_cell(self, args: dict) -> dict:
        return await self._edit(
            "update_cell", {"cellId": args["cell_id"], "content": args["content"]}, f"Updated cell {args['cell_id']}"
        )

    async def op_delete_cell(self, args: dict) -> dict:
        return await self._edit("delete_cell", {"cellId": args["cell_id"]}, f"Deleted cell {args['cell_id']}")

    async def op_move_cell(self, args: dict) -> dict:
        return await self._edit(
            "move_cell",
            {"cellId": args["cell_id"], "cellIndex": int(args["cell_index"])},
            f"Moved cell {args['cell_id']} to index {args['cell_index']}",
        )

    async def op_run_code_cell(self, args: dict) -> dict:
        cell_id = args["cell_id"]
        run = self.runs.get(cell_id)
        if run is None or run.finished is not None:
            run = Run(cell_id)
            self.runs[cell_id] = run
            self._set_outputs(cell_id, [])
            self._event("run", f"Running cell {cell_id}")
            run.task = self._spawn(self._do_run(run))
        return await self._await_run(run, args.get("wait_seconds", 120))

    async def op_wait_for_cell(self, args: dict) -> dict:
        run = self.runs.get(args["cell_id"])
        if run is None:
            return err(f"Cell {args['cell_id']} has not been run through this server. Use run_code_cell.")
        return await self._await_run(run, args.get("wait_seconds", 120))

    async def op_call(self, args: dict) -> dict:
        """Pass a call straight to a tool of the Colab page (for tools this server does not wrap)."""
        result = await self.frontend.call_tool(args["tool"], args.get("arguments") or {}, timeout=MAX_WAIT_SECONDS)
        await self._safe_refresh()
        text = outputs._clip(outputs.tool_text(result), outputs.MAX_TEXT)
        return err(text) if result.get("isError") else ok(text or "Done.")

    async def op_frontend_tools(self, args: dict) -> dict:
        tools = [
            {"name": t.get("name"), "description": t.get("description"), "inputSchema": t.get("inputSchema")}
            for t in self.frontend.tools
        ]
        return ok(json.dumps(tools, indent=1))

    async def _index(self, requested) -> int:
        if requested is not None:
            return int(requested)
        await self.refresh()
        return len(self.cells)

    @staticmethod
    def _new_cell_id(result: dict) -> str | None:
        try:
            return json.loads(outputs.tool_text(result)).get("newCellId")
        except (ValueError, AttributeError):
            return None

    async def _edit(self, tool: str, arguments: dict, description: str) -> dict:
        result = await self.frontend.call_tool(tool, arguments, timeout=60)
        await self._safe_refresh()
        if result.get("isError"):
            return err(f"Colab refused {tool}: " + outputs.tool_text(result))
        self._event("edit", description)
        return ok(description + ".")

    async def _do_run(self, run: Run) -> None:
        try:
            result = await self.frontend.call_tool("run_code_cell", {"cellId": run.cell_id})
            items = outputs.normalize(outputs.extract_outputs(result))
            if result.get("isError") and not outputs.has_error(items):
                items.append(
                    {
                        "kind": "error",
                        "ename": "ColabError",
                        "evalue": outputs.tool_text(result)[:2_000],
                        "traceback": "",
                    }
                )
            run.status = "error" if outputs.has_error(items) else "done"
            self._set_outputs(run.cell_id, items)
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc) or type(exc).__name__
        finally:
            run.finished = time.time()
            self._event("run", f"Cell {run.cell_id} {run.status} after {run.elapsed:.1f} s")

    async def _await_run(self, run: Run, wait_seconds) -> dict:
        wait = max(0.0, min(float(wait_seconds), MAX_WAIT_SECONDS))
        if run.task and not run.task.done():
            await asyncio.wait({run.task}, timeout=wait)
        if run.finished is None:
            await self._safe_refresh(include_outputs=True)  # pick up the partial output
        items = self.outputs.get(run.cell_id, [])
        if run.finished is None:
            partial = outputs.summarize(items, limit=4_000)
            return ok(
                f"Cell {run.cell_id} is still running after {run.elapsed:.0f} s. "
                "It keeps running in Colab; call wait_for_cell to get the result."
                + (f"\nOutput so far:\n{partial}" if partial else "")
            )
        if run.status == "failed":
            return err(f"Cell {run.cell_id} could not be run: {run.error}")
        paths = outputs.save_images(items, self.home / "outputs", run.cell_id)
        text, images = outputs.render(items, image_paths=paths)
        head = f"Cell {run.cell_id} finished in {run.elapsed:.1f} s" + (
            " with an error." if run.status == "error" else "."
        )
        result = ok(head + ("\n" + text if text else "\n(no output)"), images)
        result["is_error"] = run.status == "error"
        return result

    async def call(self, op: str, args: dict) -> dict:
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            return err(f"Unknown operation: {op}")
        self.last_activity = time.time()
        try:
            return await handler(args)
        except NotConnected:
            return err("No Colab tab is connected. Call colab_connect first.")
        except KeyError as exc:
            return err(f"Missing argument: {exc.args[0]}")
        except asyncio.TimeoutError:
            return err("The Colab tab did not answer in time. It may be busy or in a background window.")
        except FrontendError as exc:
            return err(f"Colab reported an error: {exc}")

    # ---- HTTP ------------------------------------------------------------

    def snapshot(self) -> dict:
        cells = []
        for index, cell in enumerate(self.cells):
            run = self.runs.get(cell["id"])
            cells.append(
                {
                    "id": cell["id"],
                    "index": index,
                    "type": cell["type"],
                    "source": cell["source"],
                    "status": self._cell_status(cell["id"]),
                    "started": run.started if run else None,
                    "finished": run.finished if run else None,
                    "rev": self.cell_rev.get(cell["id"], 0),
                    "outputs": len(self.outputs.get(cell["id"], [])),
                }
            )
        return {
            "connected": self.frontend.is_connected,
            "rev": self.rev,
            "now": time.time(),
            "notebook": self.notebook_url,
            "cells": cells,
            "events": list(self.events)[-40:],
        }

    def app(self) -> Starlette:
        mirror = (Path(__file__).parent / "mirror.html").read_text(encoding="utf-8")
        matlab_page = (Path(__file__).parent / "matlab.html").read_text(encoding="utf-8")
        matlab_dir = self.home / "matlab"
        no_store = {"Cache-Control": "no-store"}

        def local(request: Request) -> bool:
            # Rejecting other Host values stops DNS-rebinding pages from reading the notebook.
            host = request.headers.get("host", "")
            name = host[: host.index("]") + 1] if host.startswith("[") and "]" in host else host.split(":")[0]
            return name in LOCAL_HOSTS

        def authorized(request: Request) -> bool:
            scheme, _, value = request.headers.get("authorization", "").partition(" ")
            return scheme.lower() == "bearer" and hmac.compare_digest(value.strip(), self.token)

        async def health(request: Request) -> Response:
            return JSONResponse(
                {
                    "name": "colab-claude-mcp",
                    "version": config.VERSION,
                    "api": config.API_VERSION,
                    "pid": os.getpid(),
                    "connected": self.frontend.is_connected,
                }
            )

        async def index(request: Request) -> Response:
            if not local(request):
                return Response(status_code=403)
            self.last_mirror = time.time()
            return HTMLResponse(mirror)

        async def state(request: Request) -> Response:
            if not local(request):
                return Response(status_code=403)
            self.last_mirror = time.time()
            return JSONResponse(self.snapshot(), headers={"Cache-Control": "no-store"})

        async def cell_outputs(request: Request) -> Response:
            if not local(request):
                return Response(status_code=403)
            cell_id = request.path_params["cell_id"]
            return JSONResponse(
                {"rev": self.cell_rev.get(cell_id, 0), "items": self.outputs.get(cell_id, [])},
                headers={"Cache-Control": "no-store"},
            )

        # The MATLAB output page. The runs are files written by the stdio servers (matlab_view).
        # Watching it keeps the hub from idling out, like using the Colab tools does.
        async def matlab_index(request: Request) -> Response:
            if not local(request):
                return Response(status_code=403)
            self.last_activity = time.time()
            return HTMLResponse(matlab_page)

        async def matlab_state(request: Request) -> Response:
            if not local(request):
                return Response(status_code=403)
            self.last_activity = time.time()
            return JSONResponse(await asyncio.to_thread(matlab_view.runs_state, matlab_dir), headers=no_store)

        async def matlab_run(request: Request) -> Response:
            if not local(request):
                return Response(status_code=403)
            detail = await asyncio.to_thread(matlab_view.run_detail, matlab_dir, request.path_params["run_id"])
            if detail is None:
                return Response(status_code=404)
            return JSONResponse(detail, headers=no_store)

        async def matlab_file(request: Request) -> Response:
            if not local(request):
                return Response(status_code=403)
            path = matlab_view.figure_path(matlab_dir, request.path_params["name"])
            if path is None:
                return Response(status_code=404)
            media = "application/pdf" if path.suffix == ".pdf" else "image/png"
            return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=3600"})

        async def api_call(request: Request) -> Response:
            if not local(request) or not authorized(request):
                return Response(status_code=403)
            body = await request.json()
            return JSONResponse(await self.call(str(body.get("op")), body.get("args") or {}))

        async def api_shutdown(request: Request) -> Response:
            if not local(request) or not authorized(request):
                return Response(status_code=403)
            asyncio.get_running_loop().call_later(0.2, self.shutdown)
            return JSONResponse({"ok": True})

        return Starlette(
            routes=[
                Route("/", index),
                Route("/health", health),
                Route("/state", state),
                Route("/outputs/{cell_id}", cell_outputs),
                Route("/matlab", matlab_index),
                Route("/matlab/state", matlab_state),
                Route("/matlab/run/{run_id}", matlab_run),
                Route("/matlab/file/{name}", matlab_file),
                Route("/api/call", api_call, methods=["POST"]),
                Route("/api/shutdown", api_shutdown, methods=["POST"]),
            ]
        )

    def shutdown(self) -> None:
        if self._server:
            self._server.should_exit = True

    async def serve(self) -> None:
        await self.frontend.start()
        self._server = uvicorn.Server(
            uvicorn.Config(self.app(), host="127.0.0.1", port=self.http_port, log_level="warning", access_log=False)
        )
        background = self._spawn(self._background())
        log.info("hub %s listening; mirror at %s", config.VERSION, config.mirror_url())
        try:
            await self._server.serve()
        finally:
            background.cancel()
            await self.frontend.stop()


def _open_browser(url: str, browser: str | None = None) -> bool:
    try:
        controller = webbrowser.get(browser) if browser else webbrowser.get()
    except webbrowser.Error:
        log.warning("browser %r is not available; using the default browser", browser)
        try:
            controller = webbrowser.get()
        except webbrowser.Error:
            return False
    try:
        return bool(controller.open_new_tab(url))
    except Exception:
        log.exception("could not open the browser")
        return False


def run_hub() -> int:
    """Run the hub in the foreground until it is stopped or idles out."""
    home = config.home()
    logging.basicConfig(
        filename=home / "hub.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    hub = Hub(config.load_token(), config.ws_port(), config.http_port(), home)
    try:
        asyncio.run(hub.serve())
    except OSError as exc:
        log.error("hub could not start: %s", exc)
        return 1
    except KeyboardInterrupt:
        pass
    return 0
