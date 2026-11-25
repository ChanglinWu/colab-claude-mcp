import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

from colab_claude_mcp import matlab_host, matlab_tools


@pytest.fixture
def sockdir(monkeypatch, tmp_path):
    # Unix socket paths are limited to about 104 bytes, so use a short directory in /tmp.
    directory = Path(tempfile.mkdtemp(prefix="ccm", dir="/tmp"))
    monkeypatch.setattr(matlab_host, "SOCKET_DIR", directory)
    monkeypatch.setenv("COLAB_CLAUDE_HOME", str(tmp_path / "home"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def _age(path: Path, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def _leftover(path: Path, age: float = 60) -> None:
    """A socket file that nothing listens on, as a killed MATLAB leaves it."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()
    _age(path, age)


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_dead_socket_is_removed(sockdir):
    path = sockdir / "sess"
    _leftover(path)
    assert matlab_host.stale_socket("sess") == path
    notes = matlab_host.clear_stale("sess")
    assert not path.exists()
    assert len(notes) == 1 and str(path) in notes[0]


def test_live_socket_is_kept(sockdir):
    path = sockdir / "live"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    _age(path, 60)
    try:
        assert matlab_host.stale_socket("live") is None
        assert matlab_host.clear_stale("live") == []
        assert path.exists()
    finally:
        server.close()


def test_young_socket_is_kept(sockdir):
    # a MATLAB that is still starting may not listen yet
    path = sockdir / "young"
    _leftover(path, age=0)
    assert matlab_host.stale_socket("young") is None
    assert path.exists()


def test_socket_of_a_running_matlab_is_kept(sockdir, monkeypatch):
    path = sockdir / "sess"
    _leftover(path)
    matlab_host.state_path().write_text(json.dumps({"name": "sess", "matlab_pid": 12345}))
    monkeypatch.setattr(matlab_host, "is_matlab_process", lambda pid: True)
    assert matlab_host.clear_stale("sess") == []
    assert path.exists() and matlab_host.state_path().exists()


def test_state_of_a_gone_matlab_is_removed(sockdir):
    for pid in (_dead_pid(), os.getpid()):  # gone, or the pid now belongs to another program
        matlab_host.state_path().write_text(json.dumps({"name": "sess", "matlab_pid": pid}))
        notes = matlab_host.clear_stale("sess")
        assert not matlab_host.state_path().exists()
        assert any(str(pid) in note for note in notes)


def test_nothing_to_clear(sockdir):
    assert matlab_host.clear_stale("sess") == []


async def test_connect_reports_why_the_host_exited(monkeypatch, tmp_path):
    monkeypatch.setenv("COLAB_CLAUDE_HOME", str(tmp_path))
    monkeypatch.setenv("COLAB_CLAUDE_MATLAB_SESSION", "colab_claude_mcp")
    fake = type("FakeEngineModule", (), {})()
    fake.find_matlab = lambda: ()
    monkeypatch.setattr(matlab_tools, "_import_engine", lambda: fake)
    monkeypatch.setattr(matlab_tools.matlab_host, "clear_stale", lambda name=None: [])
    monkeypatch.setattr(matlab_tools, "_engine", None)

    def spawn(desktop):
        log = matlab_tools.config.matlab_home() / "host.log"
        log.write_text(
            "2026-11-26 00:00:00,000 ERROR could not share MATLAB as colab_claude_mcp: "
            "MATLAB session 'colab_claude_mcp' already exists.\n"
        )
        return subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])

    monkeypatch.setattr(matlab_tools, "_spawn_host", spawn)
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="already exists"):
        await matlab_tools._connect(wait_seconds=120)
    assert time.monotonic() - start < 15  # not the full wait
