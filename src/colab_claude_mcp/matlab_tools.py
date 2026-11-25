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

"""MATLAB tools: run code in a local MATLAB session that stays open across Claude sessions.

The session is started by `colab-claude-mcp matlab-host` (see matlab_host.py) and shared under
a fixed name; each stdio server connects to it with the MATLAB Engine for Python. MATLAB Engine
for Python (the `matlabengine` package) is an optional dependency: without it these tools
explain how to install it and the Colab tools keep working.

Every engine call passes its own stdout and stderr buffers. The engine's default is to print to
sys.stdout, which here is the MCP stdio channel.
"""

import asyncio
import base64
from dataclasses import dataclass, field
import io
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.tools.tool import ToolResult
from mcp.types import ImageContent, TextContent
from pydantic import Field

from colab_claude_mcp import config, matlab_host

INSTALL_HINT = """\
MATLAB Engine for Python is not installed in this MCP server's environment, so the MATLAB tools
cannot run. Reinstall the server with the engine that matches the local MATLAB release, on
Python 3.14 or older (the engine does not support 3.15 yet), for example for R2026b:

  claude mcp remove colab-claude-mcp --scope user
  claude mcp add colab-claude-mcp --scope user -e COLAB_CLAUDE_BROWSER=chrome -- \\
    uvx --python 3.14 --with "matlabengine==26.2.*" git+https://github.com/ChanglinWu/colab-claude-mcp

matlabengine versions: R2026b = 26.2, R2026a = 26.1, R2025b = 25.2, R2025a = 25.1.
"""

INSTRUCTIONS = """
MATLAB (local): matlab_eval runs MATLAB code in a MATLAB session on this computer, with all
installed toolboxes (for finite element work, the PDE Toolbox: createpde / femodel, geometry,
generateMesh, solve, pdeplot). The session stays open across Claude sessions, so variables
survive between calls; matlab_connect starts it (headless by default, desktop=true opens the
MATLAB desktop so the user can watch). Figures made by a run are returned as images and saved
as PNG. Runs longer than wait_seconds keep going; use matlab_wait or matlab_interrupt. For long
code, write a .m file and pass it as script. matlab_status shows the session and workspace.
"""

MAX_IMAGES = 6
MAX_TEXT = 16_000


class MatlabUnavailable(RuntimeError):
    pass


@dataclass
class Job:
    id: str
    label: str
    log: Path
    figs_manifest: Path
    begin: Any
    run: Any
    finish: Any
    out: io.StringIO
    err: io.StringIO
    started: float = field(default_factory=time.time)
    reported: bool = False

    def done(self) -> bool:
        return self.finish.done()


_engine: Any = None
_engine_name: str | None = None
_lock = asyncio.Lock()
_job: Job | None = None


def _import_engine():
    try:
        import matlab.engine  # noqa: PLC0415 - optional dependency
    except ImportError as error:
        raise MatlabUnavailable(INSTALL_HINT) from error
    return matlab.engine


def _quote(text: str | Path) -> str:
    """A MATLAB single-quoted char literal."""
    return "'" + str(text).replace("'", "''") + "'"


def _io() -> dict:
    return {"stdout": io.StringIO(), "stderr": io.StringIO()}


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... [{omitted} characters omitted; full output in the run log] ...\n{text[-tail:]}"


def _tail(path: Path, limit: int = 4_000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def _spawn_host(desktop: bool) -> None:
    log = open(config.matlab_home() / "host.out", "ab")
    subprocess.Popen(
        [sys.executable, "-m", "colab_claude_mcp", "matlab-host"] + (["--desktop"] if desktop else []),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,  # survive the end of this Claude session
        env=os.environ.copy(),
    )


async def _find(engine_mod) -> tuple[str, ...]:
    return tuple(await asyncio.to_thread(engine_mod.find_matlab))


async def _wait_for(engine_mod, name: str, present: bool, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if (name in await _find(engine_mod)) == present:
            return True
        await asyncio.sleep(1)
    return False


async def _connect(
    desktop: bool = False, restart: bool = False, session: str | None = None, wait_seconds: float = 180
) -> tuple[Any, list[str]]:
    """Connect to the shared MATLAB session, starting it if needed. Returns the engine and notes."""
    global _engine, _engine_name
    engine_mod = _import_engine()
    own = config.matlab_session()
    name = session or own
    notes: list[str] = []
    async with _lock:
        names = await _find(engine_mod)
        state = matlab_host.read_state()
        if restart and name == own and name in names:
            if _job and not _job.done():
                raise RuntimeError(f"Run {_job.id} is still going. Use matlab_interrupt before restarting.")
            await _quit(force=False)
            names = await _find(engine_mod)
        if name not in names:
            if name != own:
                listed = ", ".join(names) or "none"
                raise RuntimeError(f"No shared MATLAB session named {name!r}. Shared sessions: {listed}.")
            _spawn_host(desktop)
            notes.append(f"Started MATLAB ({'desktop' if desktop else 'headless'}).")
            if not await _wait_for(engine_mod, name, True, wait_seconds):
                raise RuntimeError(
                    f"MATLAB did not start within {wait_seconds:.0f} s. "
                    f"See {config.matlab_home() / 'host.log'} and {config.matlab_home() / 'host.out'}."
                )
            _engine = None
        elif name == own and state.get("mode") and (state["mode"] == "desktop") != desktop:
            notes.append(
                f"MATLAB is already running in {state['mode']} mode; pass restart=true to switch "
                f"(that clears the workspace)."
            )
        if _engine is None or _engine_name != name:
            _engine = await asyncio.to_thread(engine_mod.connect_matlab, name)
            _engine_name = name
            await asyncio.to_thread(_engine.addpath, str(matlab_host.MFILES), nargout=0, **_io())
    return _engine, notes


async def _engine_for_use() -> tuple[Any, list[str]]:
    """The connected engine, reconnecting or starting MATLAB (headless) when needed."""
    engine_mod = _import_engine()
    name = _engine_name or config.matlab_session()
    if _engine is not None and name in await _find(engine_mod):
        return _engine, []
    return await _connect(session=None if name == config.matlab_session() else name)


async def _quit(force: bool) -> str:
    """Quit the shared session that matlab-host started."""
    global _engine
    engine_mod = _import_engine()
    name = config.matlab_session()
    state = matlab_host.read_state()
    if name not in await _find(engine_mod):
        return "MATLAB is not running."
    if force and state.get("matlab_pid"):
        try:
            os.kill(int(state["matlab_pid"]), 15)
        except ProcessLookupError:
            pass
    else:
        if _engine is None or _engine_name != name:
            _engine = await asyncio.to_thread(engine_mod.connect_matlab, name)
        try:
            _engine.eval("quit", nargout=0, background=True, **_io())
        except Exception:  # noqa: BLE001 - the session may already be going away
            pass
    _engine = None
    if await _wait_for(engine_mod, name, False, 60):
        return "MATLAB has quit."
    return "Asked MATLAB to quit, but it is still shared after 60 s (a run may still be going; try force=true)."


def _figures(job: Job) -> list[Path]:
    try:
        lines = job.figs_manifest.read_text().splitlines()
    except OSError:
        return []
    return [Path(line) for line in lines if line.strip() and Path(line).exists()]


def _report(job: Job) -> ToolResult:
    elapsed = time.time() - job.started
    error = ""
    interrupted = False
    try:
        job.run.result()
    except Exception as exc:  # noqa: BLE001 - MatlabExecutionError, InterruptedError and engine errors
        if isinstance(exc, InterruptedError) or "was interrupted" in str(exc):
            interrupted = True
        else:
            error = (job.err.getvalue().strip() or str(exc).strip()) or type(exc).__name__
    stderr = job.err.getvalue().strip()
    try:
        job.finish.result()
    except Exception as exc:  # noqa: BLE001
        stderr += f"\n(figure export failed: {exc})"

    output = job.out.getvalue()
    status = "was interrupted" if interrupted else ("failed" if error else "finished")
    lines = [f"MATLAB run {job.id} ({job.label}) {status} after {elapsed:.1f} s."]
    if output.strip():
        lines += ["", _clip(output.rstrip())]
    if stderr and not error:
        lines += ["", "stderr / warnings:", _clip(stderr, 4_000)]
    if error:
        lines += ["", "Error:", _clip(error, 6_000)]

    figures = _figures(job)
    images = []
    for path in figures[:MAX_IMAGES]:
        images.append(
            ImageContent(type="image", data=base64.b64encode(path.read_bytes()).decode(), mimeType="image/png")
        )
    if figures:
        lines += ["", f"Figures ({len(figures)}, {min(len(figures), MAX_IMAGES)} attached):"]
        lines += [f"  {path}" for path in figures]
    lines += ["", f"Log: {job.log}"]
    job.reported = True
    text = "\n".join(lines)
    if error:
        text = "ERROR: " + text
    return ToolResult(content=[TextContent(type="text", text=text)] + images)


def _running(job: Job) -> ToolResult:
    elapsed = time.time() - job.started
    partial = _tail(job.log)
    text = (
        f"MATLAB run {job.id} ({job.label}) is still running after {elapsed:.0f} s. "
        "Use matlab_wait to keep waiting or matlab_interrupt to stop it. (MATLAB usually hands over printed "
        "output only when a run ends; for progress on long runs, write to a file and read it.)"
    )
    if partial.strip():
        text += "\n\nOutput so far (end of the log):\n" + partial
    return ToolResult(content=[TextContent(type="text", text=text)])


async def _wait_job(job: Job, wait_seconds: float) -> ToolResult:
    deadline = time.monotonic() + wait_seconds
    while not job.done() and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
    return _report(job) if job.done() else _running(job)


def _error(text: str) -> ToolResult:
    return ToolResult(content=[TextContent(type="text", text="ERROR: " + text)])


def register(mcp: FastMCP) -> None:
    @mcp.tool
    async def matlab_connect(
        desktop: Annotated[
            bool,
            Field(
                description="Open the MATLAB desktop (the user can watch figures appear) instead of a headless session."
            ),
        ] = False,
        restart: Annotated[
            bool, Field(description="Quit the running session first (clears the workspace). Needed to switch mode.")
        ] = False,
        session: Annotated[
            str | None,
            Field(
                description="Connect to another shared MATLAB session by name, for example one the user shared from "
                "their own desktop with matlab.engine.shareEngine('name')."
            ),
        ] = None,
        wait_seconds: Annotated[float, Field(description="How long to wait for MATLAB to start.", ge=10, le=600)] = 180,
    ) -> ToolResult:
        """Start or connect to the local MATLAB session (it stays open across Claude sessions). Reports release,
        working folder and whether the PDE Toolbox is available."""
        try:
            engine, notes = await _connect(desktop, restart, session, wait_seconds)
            info = io.StringIO()
            await asyncio.to_thread(
                engine.eval,
                "fprintf('%s\\n%s\\n%d\\n', version, pwd, license('test','PDE_Toolbox') && ~isempty(ver('pde')));",
                nargout=0,
                stdout=info,
                stderr=io.StringIO(),
            )
        except MatlabUnavailable as error:
            return _error(str(error))
        except Exception as error:  # noqa: BLE001
            return _error(f"{type(error).__name__}: {error}")
        release, cwd, pde = (info.getvalue().splitlines() + ["", "", ""])[:3]
        state = matlab_host.read_state()
        mode = (
            state.get("mode", "shared by the user") if _engine_name == config.matlab_session() else "shared by the user"
        )
        lines = notes + [
            f"Connected to MATLAB {release} (session {_engine_name!r}, {mode}).",
            f"Working folder: {cwd}",
            f"PDE Toolbox: {'available' if pde.strip() == '1' else 'not available'}",
            f"Run logs and figures: {config.matlab_home() / 'runs'}",
        ]
        return ToolResult(content=[TextContent(type="text", text="\n".join(lines))])

    @mcp.tool
    async def matlab_eval(
        code: Annotated[
            str | None,
            Field(
                description="MATLAB statements to run in the base workspace (several lines allowed; no local functions)."
            ),
        ] = None,
        script: Annotated[
            str | None,
            Field(
                description="Path of a .m script to run instead of code (run() semantics; may define local functions)."
            ),
        ] = None,
        wait_seconds: Annotated[
            float, Field(description="How long to wait before returning early; the run keeps going.", ge=0, le=600)
        ] = 120,
        figures: Annotated[
            Literal["new", "all", "none"],
            Field(description="Which open figures to export and attach after the run: made by this run, all, or none."),
        ] = "new",
        close_figures: Annotated[
            bool | None,
            Field(description="Close exported figures afterwards. Default: yes when headless, no in desktop mode."),
        ] = None,
        resolution: Annotated[int, Field(description="PNG resolution in dpi.", ge=50, le=600)] = 110,
    ) -> ToolResult:
        """Run MATLAB code or a .m script in the shared local MATLAB session. Returns the printed output and
        attaches the figures it made as images. Starts MATLAB (headless) if it is not running."""
        global _job, _engine
        if bool(code) == bool(script):
            return _error("Pass exactly one of code or script.")
        if _job and not _job.done():
            return _error(f"Run {_job.id} is still going. Use matlab_wait or matlab_interrupt first.")
        if script:
            path = Path(script).expanduser().resolve()
            if not path.is_file() or path.suffix != ".m":
                return _error(f"Not a .m file: {path}")
            statement, label = f"run({_quote(path)});", path.name
        else:
            statement = code
            first = next((line.strip() for line in code.splitlines() if line.strip()), "")
            label = first[:60] + ("..." if len(first) > 60 else "")
        try:
            engine, notes = await _engine_for_use()
        except MatlabUnavailable as error:
            return _error(str(error))
        except Exception as error:  # noqa: BLE001
            return _error(f"Could not reach MATLAB: {type(error).__name__}: {error}")

        runs = config.matlab_home() / "runs"
        runs.mkdir(exist_ok=True)
        job_id = time.strftime("run_%Y%m%d_%H%M%S_") + secrets.token_hex(2)
        log = runs / f"{job_id}.log"
        (runs / f"{job_id}.m").write_text(statement if code else f"% {path}\n{statement}\n")
        if close_figures is None:  # keep figures open where the user can see them
            own = _engine_name == config.matlab_session()
            close_figures = own and matlab_host.read_state().get("mode") != "desktop"
        out, err = io.StringIO(), io.StringIO()
        try:
            begin = engine.eval(f"ccmcp.begin({_quote(log)});", nargout=0, background=True, **_io())
            run = engine.eval(statement, nargout=0, background=True, stdout=out, stderr=err)
            finish = engine.eval(
                f"ccmcp.finish({_quote(runs)}, {_quote(job_id)}, {_quote(figures)}, "
                f"{'true' if close_figures else 'false'}, {resolution});",
                nargout=0,
                background=True,
                **_io(),
            )
        except Exception as error:  # noqa: BLE001 - e.g. a stale connection after MATLAB restarted
            _engine = None
            return _error(f"Could not start the run (try again to reconnect): {type(error).__name__}: {error}")
        _job = Job(job_id, label, log, runs / f"{job_id}.figs", begin, run, finish, out, err)
        result = await _wait_job(_job, wait_seconds)
        if notes:
            result.content.insert(0, TextContent(type="text", text="\n".join(notes)))
        return result

    @mcp.tool
    async def matlab_wait(
        wait_seconds: Annotated[
            float, Field(description="How long to wait for the run to finish.", ge=0, le=600)
        ] = 120,
    ) -> ToolResult:
        """Wait for the MATLAB run started by matlab_eval to finish, then return its output and figures."""
        if _job is None:
            return _error("No MATLAB run was started in this session.")
        return await _wait_job(_job, wait_seconds)

    @mcp.tool
    async def matlab_interrupt() -> ToolResult:
        """Interrupt the MATLAB run that is still going (like Ctrl+C). The session and workspace are kept."""
        if _job is None or _job.done():
            return _error("No MATLAB run is going.")
        _job.run.cancel()
        return await _wait_job(_job, 30)

    @mcp.tool
    async def matlab_status() -> ToolResult:
        """Show whether MATLAB is running, its mode, the current run, the working folder and the workspace (whos)."""
        try:
            engine_mod = _import_engine()
        except MatlabUnavailable as error:
            return _error(str(error))
        names = await _find(engine_mod)
        own = config.matlab_session()
        state = matlab_host.read_state()
        lines = []
        if own in names:
            up = time.time() - state.get("started", time.time())
            lines.append(
                f"MATLAB session {own!r}: running ({state.get('mode', '?')}, {state.get('release', '?')}, "
                f"pid {state.get('matlab_pid', '?')}, up {up / 60:.0f} min)."
            )
        else:
            lines.append(f"MATLAB session {own!r}: not running (matlab_connect or matlab_eval starts it).")
        others = [name for name in names if name != own]
        if others:
            lines.append("Other shared MATLAB sessions: " + ", ".join(others))
        if _job and not _job.done():
            lines.append(f"Run {_job.id} ({_job.label}) is going, {time.time() - _job.started:.0f} s so far.")
            partial = _tail(_job.log, 2_000)
            if partial.strip():
                lines += ["Output so far:", partial]
        elif _job and not _job.reported:
            lines.append(f"Run {_job.id} finished; matlab_wait returns its output.")
        if (_engine_name or own) in names and not (_job and not _job.done()):
            try:
                engine, _ = await _engine_for_use()
                out = io.StringIO()
                future = engine.eval(
                    "fprintf('Working folder: %s\\nOpen figures: %d\\n', pwd, numel(findall(groot,'Type','figure'))); whos",
                    nargout=0,
                    background=True,
                    stdout=out,
                    stderr=io.StringIO(),
                )
                await asyncio.to_thread(future.result, 10)
                lines.append(_clip(out.getvalue().rstrip(), 6_000) or "Workspace is empty.")
            except TimeoutError:
                lines.append("MATLAB is busy (another session's run?), so the workspace could not be listed.")
            except Exception as error:  # noqa: BLE001
                lines.append(f"Could not query MATLAB: {type(error).__name__}: {error}")
        return ToolResult(content=[TextContent(type="text", text="\n".join(lines))])

    @mcp.tool
    async def matlab_stop(
        force: Annotated[
            bool, Field(description="Kill MATLAB instead of asking it to quit (for a hung session).")
        ] = False,
    ) -> ToolResult:
        """Quit the MATLAB session that these tools started. The workspace is lost. Only when the user asks."""
        global _job
        try:
            if _job and not _job.done() and not force:
                return _error(f"Run {_job.id} is still going. Use matlab_interrupt first, or force=true.")
            message = await _quit(force)
            _job = None
            return ToolResult(content=[TextContent(type="text", text=message)])
        except MatlabUnavailable as error:
            return _error(str(error))
