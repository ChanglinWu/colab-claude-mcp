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

"""Keeps one shared MATLAB session alive across Claude sessions.

MATLAB started with -batch cannot be found by the engine, and MATLAB started with -nodesktop
exits when its stdin closes. So a small detached Python process starts MATLAB through the
engine, shares it under a fixed name, and waits until MATLAB exits. The stdio servers connect
to the shared session by name, the way they reach the Colab tab through the hub.
"""

import json
import logging
import os
from pathlib import Path
import shlex
import socket
import stat
import subprocess
import time

from colab_claude_mcp import config

MFILES = Path(__file__).parent / "mfiles"

# A shared MATLAB session listens on a Unix socket named after the session in /tmp (seen with
# R2026b on macOS: /tmp/<name>, i.e. /private/tmp/<name>). MATLAB removes it when it quits
# normally. A MATLAB that is killed, for example together with the app that started this
# server, leaves it behind; matlab.engine.find_matlab() then no longer lists the name, but
# shareEngine refuses it ("MATLAB session '<name>' already exists"), so no new session can be
# shared under that name until the file is removed.
SOCKET_DIR = Path("/tmp")
STALE_AGE = 10.0  # seconds; a younger socket may belong to a MATLAB that is still starting

log = logging.getLogger("colab_claude_mcp.matlab_host")


def state_path() -> Path:
    return config.matlab_home() / "session.json"


def read_state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def _alive(pid: int) -> bool:
    try:  # reap it if it is our child, or a finished MATLAB would linger as a zombie
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_matlab_process(pid: int) -> bool:
    """True if pid is a running MATLAB process (not some other process that reused the pid)."""
    if not _alive(pid):
        return False
    try:
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell; do not treat a live process as gone
    return "matlab" in command.lower()


def socket_path(name: str) -> Path | None:
    """The Unix socket of the shared MATLAB session called name (None on Windows)."""
    return None if os.name == "nt" else SOCKET_DIR / name


def _listening(path: Path) -> bool:
    """True if a process accepts connections on the Unix socket at path.

    Only a fallback: a live listener whose backlog is full refuses connections too.
    """
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _holders(path: Path) -> set[int] | None:
    """Processes that have the Unix socket at path open, or None if that cannot be told.

    The socket is looked up under both spellings: lsof matches the name it was bound with
    (MATLAB binds /tmp/<name>), not the resolved /private/tmp path.
    """
    names = {str(path)}
    try:
        names.add(str(path.resolve()))
    except OSError:
        pass
    table = Path("/proc/net/unix")  # Linux
    if table.exists():
        try:
            rows = table.read_text().splitlines()[1:]
        except OSError:
            return None
        return {0} if any(row.split() and row.split()[-1] in names for row in rows) else set()
    pids: set[int] = set()
    for name in names:
        try:
            out = subprocess.run(["lsof", "-t", "--", name], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        pids |= {int(word) for word in out.stdout.split() if word.isdigit()}
    return pids


def stale_socket(name: str) -> Path | None:
    """The socket left behind by a shared MATLAB session that no longer runs, or None.

    A socket counts as stale only if it is ours, at least STALE_AGE old, the MATLAB recorded in
    the state file for this name is gone, and no process has it open (or, where that cannot be
    told, nothing accepts connections on it).
    """
    path = socket_path(name)
    if path is None:
        return None
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        return None
    if time.time() - info.st_mtime < STALE_AGE:
        return None
    state = read_state()
    if state.get("name") == name and state.get("matlab_pid") and is_matlab_process(int(state["matlab_pid"])):
        return None
    holders = _holders(path)
    if holders is None:
        return None if _listening(path) else path
    return None if holders else path


def note(message: str) -> None:
    """Add a line to host.log from outside the host process (for example the stdio server)."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(config.matlab_home() / "host.log", "a") as handle:
            handle.write(f"{stamp} INFO {message}\n")
    except OSError:
        pass


def clear_stale(name: str | None = None) -> list[str]:
    """Remove what a killed MATLAB session left behind. Returns one line per thing removed.

    Safe to call at any time: a session that is running (listed state, or anything listening on
    its socket) is never touched.
    """
    name = name or config.matlab_session()
    notes: list[str] = []
    path = stale_socket(name)
    if path is not None:
        try:
            path.unlink()
            notes.append(f"Removed {path}, the socket of a MATLAB session {name!r} that no longer runs.")
        except OSError as error:
            notes.append(f"Could not remove the stale socket {path}: {error}.")
    state = read_state()
    pid = state.get("matlab_pid")
    if state.get("name", name) == name and pid and not is_matlab_process(int(pid)):
        state_path().unlink(missing_ok=True)
        notes.append(f"Removed {state_path()}: MATLAB pid {pid} recorded there is gone.")
    return notes


def run_host(desktop: bool) -> int:
    logging.basicConfig(
        filename=config.matlab_home() / "host.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    import matlab.engine  # noqa: PLC0415 - optional dependency

    name = config.matlab_session()
    if name in matlab.engine.find_matlab():
        log.info("a MATLAB session named %s is already shared; not starting another", name)
        return 0
    for line in clear_stale(name):
        log.info(line)

    options = ["-desktop"] if desktop else ["-nodesktop", "-nosplash"]
    options += shlex.split(config.matlab_options())
    log.info("starting MATLAB %s", " ".join(options))
    engine = matlab.engine.start_matlab(" ".join(options))
    pid = int(engine.feature("getpid"))
    engine.addpath(str(MFILES), nargout=0)
    if not desktop:
        # No windows pop up; figures are still drawn and exported as PNG.
        engine.eval("set(groot, 'defaultFigureVisible', 'off');", nargout=0)
    if config.matlab_style() == "ieee":
        engine.eval("ccmcp.style('auto');", nargout=0)
    try:
        engine.eval(f"matlab.engine.shareEngine('{name}');", nargout=0)
    except matlab.engine.MatlabExecutionError as error:
        # e.g. the name is taken by a session this host cannot see or clear; MATLAB has then
        # shared itself under a default name, which the tools would never find
        reason = str(error).strip().splitlines()[-1] if str(error).strip() else type(error).__name__
        log.error("could not share MATLAB as %s: %s", name, reason)
        engine.quit()
        return 1
    state = {
        "name": name,
        "mode": "desktop" if desktop else "headless",
        "matlab_pid": pid,
        "host_pid": os.getpid(),
        "release": engine.version(),
        "started": time.time(),
    }
    state_path().write_text(json.dumps(state))
    log.info("MATLAB %s shared as %s (pid %d)", state["release"], name, pid)

    try:
        while _alive(pid):
            time.sleep(2)
    finally:
        if read_state().get("matlab_pid") == pid:
            state_path().unlink(missing_ok=True)
        log.info("MATLAB (pid %d) exited", pid)
    return 0
