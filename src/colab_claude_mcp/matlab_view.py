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

"""Run records behind the MATLAB output page.

matlab_tools writes one JSON record per run next to the run's log and figures
(<home>/matlab/runs/<run id>.json), first when the run starts and again when it ends.
The hub reads them to serve the page at /matlab. Going through files means the page also
shows runs from earlier Claude sessions, and still works when the session that started a
run has ended before the run did.
"""

import json
import os
from pathlib import Path
import re
import time

RUN_ID = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{4}$")
FIGURE = re.compile(r"^run_\d{8}_\d{6}_[0-9a-f]{4}_fig\d+\.(png|pdf)$")
MAX_RUNS = 60
# A record still says "running" although MATLAB has finished the run (it wrote the figure
# list): the server that started it ended. Wait this long before taking over from it.
ORPHAN_SECONDS = 5

_cache: dict[Path, tuple[int, dict]] = {}


def runs_dir(matlab_dir: Path) -> Path:
    return matlab_dir / "runs"


def write_record(path: Path, record: dict) -> None:
    """Write a run record so that readers never see half of it."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_record(path: Path) -> tuple[int, dict] | None:
    """A record and its modification time, re-read only when the file changed."""
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _cache.pop(path, None)
        return None
    cached = _cache.get(path)
    if cached and cached[0] == mtime:
        return cached
    record = _read_json(path)
    if record is None:
        return None
    _cache[path] = (mtime, record)
    return mtime, record


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError, OSError):
        return False
    return True


def session_info(matlab_dir: Path) -> dict:
    """The MATLAB session that matlab-host keeps, as far as its state file says."""
    state = _read_json(matlab_dir / "session.json") or {}
    if not state or not _pid_alive(state.get("matlab_pid")):
        return {"running": False}
    keys = ("name", "mode", "release", "started")
    return {"running": True, **{key: state.get(key) for key in keys}, "pid": state.get("matlab_pid")}


def _resolve(record: dict, folder: Path, session_running: bool) -> dict:
    """Fill in the end of a run whose record was left at "running"."""
    if record.get("status") != "running":
        return record
    run_id = record.get("id", "")
    manifest = folder / f"{run_id}.figs"
    try:
        finished = manifest.stat().st_mtime
    except OSError:
        finished = None
    if finished is not None and time.time() - finished > ORPHAN_SECONDS:
        try:
            output = (folder / f"{run_id}.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            output = ""
        figures = [Path(line).name for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
        figures = [name for name in figures if FIGURE.match(name)]
        return {
            **record,
            "status": "ended",
            "finished": finished,
            "output": output,
            "figures": figures,
            "pdfs": [name[:-4] + ".pdf" for name in figures if (folder / (name[:-4] + ".pdf")).is_file()],
            "note": "The Claude session that started this run ended before it did, so errors are not shown; "
            "the output is from the run log.",
        }
    if finished is None and not session_running and record.get("session_kind") == "own":
        return {**record, "status": "lost", "note": "MATLAB stopped before this run ended."}
    return record


def runs_state(matlab_dir: Path, limit: int = MAX_RUNS) -> dict:
    """Session info and summaries of the most recent runs, oldest first."""
    folder = runs_dir(matlab_dir)
    session = session_info(matlab_dir)
    paths = sorted(folder.glob("run_*.json"))[-limit:] if folder.is_dir() else []
    runs = []
    for path in paths:
        read = _read_record(path)
        if read is None:
            continue
        mtime, record = read
        record = _resolve(record, folder, session["running"])
        runs.append(
            {
                "id": record.get("id"),
                "label": record.get("label", ""),
                "status": record.get("status", "running"),
                "started": record.get("started"),
                "finished": record.get("finished"),
                "rev": f"{mtime}:{record.get('status')}",
            }
        )
    return {"now": time.time(), "session": session, "runs": runs}


def run_detail(matlab_dir: Path, run_id: str) -> dict | None:
    """One run's full record: code, output, error and figure file names."""
    if not RUN_ID.match(run_id):
        return None
    folder = runs_dir(matlab_dir)
    read = _read_record(folder / f"{run_id}.json")
    if read is None:
        return None
    return _resolve(read[1], folder, session_info(matlab_dir)["running"])


def figure_path(matlab_dir: Path, name: str) -> Path | None:
    """A run figure's PNG or PDF file, or None for any other name."""
    if not FIGURE.match(name):
        return None
    path = runs_dir(matlab_dir) / name
    return path if path.is_file() else None
