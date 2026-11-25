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
import time

from colab_claude_mcp import config

MFILES = Path(__file__).parent / "mfiles"

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

    options = ["-desktop"] if desktop else ["-nodesktop", "-nosplash"]
    options += shlex.split(config.matlab_options())
    log.info("starting MATLAB %s", " ".join(options))
    engine = matlab.engine.start_matlab(" ".join(options))
    pid = int(engine.feature("getpid"))
    engine.addpath(str(MFILES), nargout=0)
    if not desktop:
        # No windows pop up; figures are still drawn and exported as PNG.
        engine.eval("set(groot, 'defaultFigureVisible', 'off');", nargout=0)
    engine.eval(f"matlab.engine.shareEngine('{name}');", nargout=0)
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
