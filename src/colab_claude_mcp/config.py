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

"""Shared settings for the hub and the stdio server."""

import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

VERSION = "0.7.4"

# Version of the HTTP API between the stdio server and the hub. A hub with a tab connected is
# replaced only when this differs, so ordinary upgrades do not drop the tab.
API_VERSION = 1

COLAB = "https://colab.research.google.com"
COLAB_ALT_DOMAIN = "https://colab.google.com"
SCRATCH_PATH = "/notebooks/empty.ipynb"

# Fixed ports are what let an open Colab tab stay connected across Claude sessions.
DEFAULT_WS_PORT = 47821
DEFAULT_HTTP_PORT = 47822


def home() -> Path:
    path = Path(os.environ.get("COLAB_CLAUDE_HOME", Path.home() / ".colab-claude-mcp"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def ws_port() -> int:
    return int(os.environ.get("COLAB_CLAUDE_WS_PORT", DEFAULT_WS_PORT))


def http_port() -> int:
    return int(os.environ.get("COLAB_CLAUDE_HTTP_PORT", DEFAULT_HTTP_PORT))


def hub_url() -> str:
    return f"http://127.0.0.1:{http_port()}"


def mirror_url() -> str:
    return f"http://localhost:{http_port()}/"


def matlab_url() -> str:
    """The MATLAB output page, served by the hub next to the Colab mirror."""
    return f"http://localhost:{http_port()}/matlab"


def jupyter_mirror_url() -> str:
    """The mirror page of the JupyterLab notebook, served by the hub next to the Colab mirror."""
    return f"http://localhost:{http_port()}/jupyter/"


def jupyter_setup_url() -> str:
    """The page with the bookmarklet that links a JupyterLab tab to the hub."""
    return f"http://localhost:{http_port()}/jupyter/setup"


def jupyter_notebook() -> str:
    """Notebook that jupyter_connect opens when none is named, relative to the Jupyter server's root."""
    return os.environ.get("COLAB_CLAUDE_JUPYTER_NOTEBOOK", "claude/scratch.ipynb").strip().strip("/")


LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def jupyter_origin_allowed(origin: str) -> bool:
    """Whether a page at this origin may link its JupyterLab tab to the hub.

    JupyterLab reached through an SSH tunnel is served on localhost, so loopback origins are accepted.
    Others must be listed in COLAB_CLAUDE_JUPYTER_ORIGINS (comma-separated, for example
    "http://gpu12.example.edu:8899").
    """
    try:
        parts = urlsplit(origin)
        name = parts.hostname
    except ValueError:
        return False
    if parts.scheme in ("http", "https") and name in LOOPBACK_NAMES:
        return True
    extra = {item.strip().rstrip("/") for item in os.environ.get("COLAB_CLAUDE_JUPYTER_ORIGINS", "").split(",")}
    return bool(origin) and origin.rstrip("/") in extra - {""}


def browser() -> str | None:
    """Browser used to open Colab tabs: COLAB_CLAUDE_BROWSER, else BROWSER, else the OS default."""
    return os.environ.get("COLAB_CLAUDE_BROWSER") or os.environ.get("BROWSER") or None


def accel() -> str:
    """Parallel and accelerated computing: "auto" (the default) or "off".

    "auto": Claude uses a MATLAB parallel pool, a GPU or a Colab GPU/TPU when the task allows.
    "off": runs stay serial and on the CPU unless the user asks otherwise.
    """
    value = os.environ.get("COLAB_CLAUDE_ACCEL", "auto").strip().lower()
    return value if value in ("auto", "off") else "auto"


def load_token() -> str:
    """The secret shared by the Colab tab, the hub and the stdio server.

    It is kept on disk so that it survives hub restarts.
    """
    path = home() / "token"
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    path.touch(mode=0o600)
    path.write_text(token)
    path.chmod(0o600)
    return token


# --- MATLAB -------------------------------------------------------------------------------


def matlab_session() -> str:
    """Name of the shared MATLAB session that the MATLAB tools start and connect to."""
    return os.environ.get("COLAB_CLAUDE_MATLAB_SESSION", "colab_claude_mcp")


def matlab_home() -> Path:
    """Where MATLAB run logs, figures and the session state file are kept."""
    path = home() / "matlab"
    path.mkdir(parents=True, exist_ok=True)
    return path


def matlab_style() -> str:
    """Look of new MATLAB figures: "ieee" (IEEE Transactions, the default) or "matlab" (MATLAB's own)."""
    value = os.environ.get("COLAB_CLAUDE_MATLAB_STYLE", "ieee").strip().lower()
    return value if value in ("ieee", "matlab") else "ieee"


def matlab_options() -> str:
    """Extra MATLAB startup options, for example "-sd /path/to/project"."""
    return os.environ.get("COLAB_CLAUDE_MATLAB_OPTIONS", "")
