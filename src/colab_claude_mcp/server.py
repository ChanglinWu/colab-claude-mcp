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

"""The stdio MCP server that Claude Code starts for each session.

It holds no state. Every tool forwards to the hub, which is started on first use
and keeps running after this process exits.
"""

import asyncio
import os
import subprocess
import sys
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
import httpx
from mcp.types import ImageContent, TextContent
from pydantic import Field

from colab_claude_mcp import config, matlab_tools

INSTRUCTIONS = (
    f"""\
Drives a Google Colab notebook that is open in the user's browser.

Start with colab_connect. It reuses the Colab tab that is already connected and only
opens a new tab when none is connected, or when you pass new_notebook=true or a
notebook_url. Pass those only when the user asks for a new or a different notebook.

The connection outlives this session, so an earlier session's notebook is usually
still there: call colab_status to see its cells before adding to it.

run_code_cell returns text output and attaches plots as images. If a cell runs longer
than wait_seconds the call returns early and the cell keeps running; use wait_for_cell.

A live copy of the notebook is served at {config.mirror_url()} (the mirror page). Open
it in the built-in browser so the user can watch cells, run status and outputs there.
Choosing a GPU or other runtime type is done by the user in the Colab tab.
"""
    + f"""
JupyterLab (for example on the user's HPC, through an SSH tunnel): jupyter_connect opens a notebook
there through the user's logged-in JupyterLab tab, which the user links with the "Claude ⇄ Jupyter"
bookmark. Never ask for the Jupyter password. The cell tools then act on that notebook
(backend="jupyter"); it is saved on the server after each change, and jupyter_kernel interrupts or
restarts its kernel. Its mirror page is {config.jupyter_mirror_url()}. Or work in JupyterLab or Colab opened
in the built-in browser (load a `remote-notebooks` skill if available).
"""
    + (
        """
Colab and Jupyter accelerators are off (COLAB_CLAUDE_ACCEL=off): keep the work on the CPU, without
moving it to a GPU or TPU, unless the user asks for it.
"""
        if config.accel() == "off"
        else """
Colab and Jupyter accelerators (COLAB_CLAUDE_ACCEL=auto): when the task allows, use them without
being asked. Before heavy numeric work, check what the runtime has (torch.cuda.is_available(),
jax.devices(), or !nvidia-smi) and put the work on its GPU or TPU, with a CPU fallback in the code.
For CPU-bound work with independent pieces, use vectorized NumPy or a process pool
(concurrent.futures, joblib) over the cores you may use (len(os.sched_getaffinity(0)) on a shared
HPC node). When a task would clearly gain from a GPU or TPU and the runtime has none, say so
instead of waiting through a slow CPU run. Apple MPS is not available there. In your reply, say
which device the work ran on.
"""
    )
    + matlab_tools.INSTRUCTIONS
    + matlab_tools.accel_instructions()
)

mcp = FastMCP(name="colab-claude-mcp", instructions=INSTRUCTIONS)

CellId = Annotated[str, Field(description="The cell's ID, as shown by colab_status or get_cells.")]
WaitSeconds = Annotated[
    float,
    Field(description="How long to wait for the cell to finish before returning early.", ge=0, le=600),
]
CellIndex = Annotated[
    int | None,
    Field(description="Position to insert at (0 is the top). Omit to append at the end.", ge=0),
]
Backend = Annotated[
    Literal["colab", "jupyter"] | None,
    Field(
        description="Which notebook: 'colab', or 'jupyter' for the JupyterLab notebook of jupyter_connect "
        "(for example on an HPC). Default: the one this session connected to last."
    ),
]

_hub_lock = asyncio.Lock()

# The notebook this session connected to last; the cell tools act on it unless told otherwise.
_session: dict[str, str | None] = {"backend": None}

OLD_HUB = (
    "An older colab-claude-mcp hub without the JupyterLab backend is still running, because a Colab tab is "
    "connected to it. The new hub takes over once that tab is closed, or after `colab-claude-mcp stop` "
    "(which disconnects the Colab tab)."
)


async def _health(client: httpx.AsyncClient) -> dict | None:
    try:
        response = await client.get(config.hub_url() + "/health", timeout=1.5)
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    return data if data.get("name") == "colab-claude-mcp" else None


def _spawn_hub() -> None:
    log = open(config.home() / "hub.out", "ab")
    subprocess.Popen(
        [sys.executable, "-m", "colab_claude_mcp", "hub"],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,  # survive the end of this Claude session
        env=os.environ.copy(),
    )


async def ensure_hub(client: httpx.AsyncClient) -> None:
    """Make sure a usable hub is running, starting or replacing one if needed.

    A hub of another version is replaced, because the mirror page and the fixes live in it. But
    replacing a hub drops its Colab tab, and the Colab page does not reconnect by itself. So a hub
    that has a tab connected and speaks the same API is left alone until that tab goes away.
    """
    async with _hub_lock:
        health = await _health(client)
        if health and health.get("version") == config.VERSION:
            return
        if health and health.get("connected") and health.get("api", 1) == config.API_VERSION:
            return
        if health:  # an older hub with nothing to lose: replace it
            try:
                await client.post(config.hub_url() + "/api/shutdown", headers=_auth(), timeout=3)
            except httpx.HTTPError:
                pass
            for _ in range(40):
                if await _health(client) is None:
                    break
                await asyncio.sleep(0.25)
        _spawn_hub()
        for _ in range(80):
            await asyncio.sleep(0.25)
            health = await _health(client)
            if health and health.get("version") == config.VERSION:
                return
        raise RuntimeError(
            f"The colab-claude-mcp hub did not start. See {config.home() / 'hub.log'} "
            f"(ports {config.ws_port()} and {config.http_port()} must be free)."
        )


def _auth() -> dict:
    return {"Authorization": f"Bearer {config.load_token()}"}


async def hub_call(op: str, args: dict, timeout: float = 90) -> ToolResult:
    async with httpx.AsyncClient() as client:
        await ensure_hub(client)
        response = await client.post(
            config.hub_url() + "/api/call",
            json={"op": op, "args": args},
            headers=_auth(),
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
    content: list = [TextContent(type="text", text=data.get("text") or "")]
    for image in data.get("images", []):
        content.append(ImageContent(type="image", data=image["data"], mimeType=image["mime"]))
    if data.get("is_error"):
        # Keep images (a cell can plot and then fail) but make the failure obvious.
        content[0] = TextContent(type="text", text="ERROR: " + (data.get("text") or "unknown error"))
    return ToolResult(content=content)


async def jupyter_call(op: str, args: dict, timeout: float = 90) -> ToolResult:
    """A JupyterLab operation. An older hub kept alive by its Colab tab does not know them."""
    result = await hub_call(op, args, timeout)
    first = result.content[0] if result.content else None
    if isinstance(first, TextContent) and first.text.startswith("ERROR: Unknown operation: jupyter_"):
        return ToolResult(content=[TextContent(type="text", text="ERROR: " + OLD_HUB)])
    return result


def backend_for(backend: str | None) -> str | None:
    """The notebook a cell tool acts on: the one asked for, else this session's last; None lets the hub pick."""
    return backend or _session["backend"]


async def cell_call(op: str, args: dict, backend: str | None, timeout: float = 90) -> ToolResult:
    chosen = backend_for(backend)
    if chosen == "jupyter":
        return await jupyter_call("jupyter_" + op, args, timeout)
    return await hub_call(op, {**args, "backend": chosen} if chosen else args, timeout)


@mcp.tool
async def colab_connect(
    new_notebook: Annotated[
        bool,
        Field(description="Open a fresh scratch notebook in a new tab. Only when the user asks for a new notebook."),
    ] = False,
    notebook_url: Annotated[
        str | None,
        Field(description="Open this existing Colab notebook (a colab.research.google.com URL) in a new tab."),
    ] = None,
    open_tab: Annotated[
        bool,
        Field(
            description="Set false to wait for a tab that is already open (for example after the user signed in) "
            "instead of opening another one."
        ),
    ] = True,
    wait_seconds: Annotated[float, Field(description="How long to wait for the tab to connect.", ge=1, le=600)] = 60,
) -> ToolResult:
    """Connect to Colab. Reuses the already connected tab; opens a tab only if none is connected or one is asked for.

    Another way, which some users prefer: they sign in to Colab in Claude's built-in browser, and Claude works
    in that page with the browser tools, so every edit and run shows live. If a `remote-notebooks` skill is
    available, load it for that route.
    """
    _session["backend"] = "colab"
    args = {
        "new_notebook": new_notebook,
        "notebook_url": notebook_url,
        "open_tab": open_tab,
        "wait_seconds": wait_seconds,
        "browser": config.browser(),
    }
    return await hub_call("connect", args, timeout=wait_seconds + 30)


@mcp.tool
async def colab_status(
    include_outputs: Annotated[bool, Field(description="Also show a text summary of each cell's output.")] = False,
) -> ToolResult:
    """Show whether Colab is connected, the notebook's cells with their run status, and the mirror page URL."""
    return await hub_call("status", {"include_outputs": include_outputs})


@mcp.tool
async def get_cells(
    start: Annotated[int, Field(description="First cell index (inclusive).", ge=0)] = 0,
    end: Annotated[int | None, Field(description="Last cell index (inclusive). Omit for the last cell.", ge=0)] = None,
    include_outputs: Annotated[bool, Field(description="Include a text summary of each cell's output.")] = False,
    backend: Backend = None,
) -> ToolResult:
    """Read the full source of the notebook's cells, optionally with their outputs."""
    args = {"start": start, "end": end, "include_outputs": include_outputs}
    return await cell_call("get_cells", args, backend, timeout=150)


@mcp.tool
async def add_code_cell(
    code: Annotated[str, Field(description="The code for the new cell.")],
    cell_index: CellIndex = None,
    run: Annotated[bool, Field(description="Run the cell right after adding it.")] = False,
    language: Annotated[
        Literal["python", "r", "julia"], Field(description="Colab only; in JupyterLab the notebook's kernel decides.")
    ] = "python",
    wait_seconds: WaitSeconds = 120,
    backend: Backend = None,
) -> ToolResult:
    """Add a code cell (appended by default) and optionally run it. Returns the new cell's ID and any output."""
    args = {"code": code, "cell_index": cell_index, "run": run, "language": language, "wait_seconds": wait_seconds}
    return await cell_call("add_code_cell", args, backend, timeout=wait_seconds + 90)


@mcp.tool
async def add_text_cell(
    content: Annotated[str, Field(description="Markdown (LaTeX allowed) for the new cell.")],
    cell_index: CellIndex = None,
    backend: Backend = None,
) -> ToolResult:
    """Add a text (Markdown) cell, appended by default."""
    return await cell_call("add_text_cell", {"content": content, "cell_index": cell_index}, backend)


@mcp.tool
async def update_cell(
    cell_id: CellId,
    content: Annotated[str, Field(description="The new content, replacing the old content entirely.")],
    backend: Backend = None,
) -> ToolResult:
    """Replace the content of an existing cell."""
    return await cell_call("update_cell", {"cell_id": cell_id, "content": content}, backend)


@mcp.tool
async def run_code_cell(cell_id: CellId, wait_seconds: WaitSeconds = 120, backend: Backend = None) -> ToolResult:
    """Run a code cell. Returns its text output and attaches plots as images; returns early if it runs long."""
    args = {"cell_id": cell_id, "wait_seconds": wait_seconds}
    return await cell_call("run_code_cell", args, backend, timeout=wait_seconds + 60)


@mcp.tool
async def wait_for_cell(cell_id: CellId, wait_seconds: WaitSeconds = 120, backend: Backend = None) -> ToolResult:
    """Wait for a cell started with run_code_cell to finish, then return its output."""
    args = {"cell_id": cell_id, "wait_seconds": wait_seconds}
    return await cell_call("wait_for_cell", args, backend, timeout=wait_seconds + 60)


@mcp.tool
async def delete_cell(cell_id: CellId, backend: Backend = None) -> ToolResult:
    """Delete a cell."""
    return await cell_call("delete_cell", {"cell_id": cell_id}, backend)


@mcp.tool
async def move_cell(
    cell_id: CellId,
    cell_index: Annotated[int, Field(description="The index to move the cell to.", ge=0)],
    backend: Backend = None,
) -> ToolResult:
    """Move a cell to another position."""
    return await cell_call("move_cell", {"cell_id": cell_id, "cell_index": cell_index}, backend)


@mcp.tool
async def jupyter_connect(
    notebook_path: Annotated[
        str | None,
        Field(
            description="Notebook on the Jupyter server, relative to the folder JupyterLab was started in, for "
            "example 'claude/lenet_check.ipynb' (opened if it exists, created with its folders if not). Default: "
            "the notebook already open, else claude/scratch.ipynb. Pick a name that says what the work is."
        ),
    ] = None,
    kernel: Annotated[
        str | None,
        Field(
            description="Kernel name, as listed in the reply (for example a conda environment's kernel). Default: "
            "the notebook's own kernel, else the server's default."
        ),
    ] = None,
    wait_seconds: Annotated[
        float, Field(description="How long to wait for a JupyterLab tab to connect.", ge=0, le=600)
    ] = 30,
) -> ToolResult:
    """Connect to JupyterLab (for example on the user's HPC) through the user's browser tab and open a notebook there.

    The user logs in to JupyterLab in the browser as usual and clicks the 'Claude ⇄ Jupyter' bookmark in
    that tab; the tab then relays the requests, so Claude never needs, and must never ask for, the
    Jupyter password. If no tab is connected, the reply says what to ask the user. Afterwards the cell
    tools (add_code_cell, run_code_cell, get_cells, ...) act on this notebook. Its kernel runs on the
    server and keeps running between sessions. The notebook is saved on the server after every change, so
    nothing needs saving at the end. The user watches on the Jupyter mirror page. If they also have the
    notebook open in JupyterLab, that tab shows Claude's edits only after it reloads from disk: when
    JupyterLab says the file changed on disk, they must choose Revert, not Overwrite. Long runs:
    run_code_cell returns early, the cell keeps running, and wait_for_cell picks it up, also from a later
    session. On a shared HPC node, use only the cores and GPUs given to the job.

    Another way, which some users prefer: they open JupyterLab itself in Claude's built-in browser (for
    example http://localhost:8899/lab/tree/<notebook>) and log in there, and Claude works in that page with
    the browser tools. Every edit and run then shows live, with nothing to Revert and no bookmark. If a
    `remote-notebooks` skill is available, load it for that route.
    """
    _session["backend"] = "jupyter"
    args = {"notebook_path": notebook_path, "kernel": kernel, "wait_seconds": wait_seconds}
    return await jupyter_call("jupyter_connect", args, timeout=wait_seconds + 120)


@mcp.tool
async def jupyter_status(
    include_outputs: Annotated[bool, Field(description="Also show a text summary of each cell's output.")] = False,
) -> ToolResult:
    """Show whether a JupyterLab tab is connected, the open notebook, its kernel and cells, and the mirror URL."""
    return await jupyter_call("jupyter_status", {"include_outputs": include_outputs})


@mcp.tool
async def jupyter_kernel(
    action: Annotated[
        Literal["interrupt", "restart", "shutdown"],
        Field(
            description="interrupt: stop the running cell (KeyboardInterrupt). restart: clear all variables. "
            "shutdown: stop the kernel and free its memory and GPU on the server; the notebook stays."
        ),
    ],
) -> ToolResult:
    """Interrupt, restart or shut down the kernel of the JupyterLab notebook opened with jupyter_connect."""
    return await jupyter_call("jupyter_kernel", {"action": action}, timeout=150)


async def _ensure_viewer() -> str | None:
    """Start the hub, which also serves the MATLAB output page. Returns why the page is unavailable, or None."""
    try:
        async with httpx.AsyncClient() as client:
            await ensure_hub(client)
            response = await client.get(config.hub_url() + "/matlab/state", timeout=5)
    except (RuntimeError, httpx.HTTPError) as error:
        return f"the hub did not start ({error}); see {config.home() / 'hub.log'}"
    if response.status_code == 200:
        return None
    # ensure_hub keeps an older hub that has a Colab tab connected, so as not to drop the tab.
    return (
        "an older hub without this page is still running because a Colab tab is connected to it; the new hub "
        "takes over once that tab is closed (or after `colab-claude-mcp stop`, which disconnects the tab)"
    )


matlab_tools.register(mcp, _ensure_viewer)


def _housekeeping() -> None:
    """Clear what a killed MATLAB session left behind, before any tool runs.

    The hub needs nothing of the kind: it is found through its health check, keeps no pid or lock
    files, and the system frees its ports when it is killed.
    """
    try:
        from colab_claude_mcp import matlab_host  # noqa: PLC0415

        for line in matlab_host.clear_stale():
            matlab_host.note(f"startup check: {line}")
    except Exception:  # noqa: BLE001 - never stop the server from starting
        pass


def run_stdio() -> None:
    _housekeeping()
    mcp.run(show_banner=False)
