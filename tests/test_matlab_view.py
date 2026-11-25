import base64
import os
import time

import httpx

from colab_claude_mcp import matlab_view
from colab_claude_mcp.hub import Hub
from tests.fake_colab import PNG

RUN = "run_20261125_160811_4d51"


def _record(matlab_dir, run_id=RUN, **fields):
    runs = matlab_view.runs_dir(matlab_dir)
    runs.mkdir(parents=True, exist_ok=True)
    record = {"id": run_id, "label": "x = 1", "code": "x = 1", "started": time.time() - 3, "status": "running"}
    record.update(fields)
    matlab_view.write_record(runs / f"{run_id}.json", record)
    return runs


def test_runs_state_lists_recent_runs_oldest_first(tmp_path):
    for n in range(5):
        _record(tmp_path, f"run_20261125_1608{n:02d}_abcd", status="finished", finished=time.time())
    state = matlab_view.runs_state(tmp_path, limit=3)
    assert [run["id"] for run in state["runs"]] == [f"run_20261125_1608{n:02d}_abcd" for n in (2, 3, 4)]
    assert state["session"] == {"running": False}
    assert all("output" not in run for run in state["runs"])  # summaries stay small


def test_a_run_the_starting_session_left_behind_is_completed_from_its_log(tmp_path):
    runs = _record(tmp_path)
    (runs / f"{RUN}.log").write_text("step 1\nstep 2\n")
    (runs / f"{RUN}_fig1.png").write_bytes(b"png")
    manifest = runs / f"{RUN}.figs"
    manifest.write_text(f"{runs / (RUN + '_fig1.png')}\n")
    assert matlab_view.run_detail(tmp_path, RUN)["status"] == "running"  # MATLAB just ended it: wait
    old = time.time() - matlab_view.ORPHAN_SECONDS - 1
    os.utime(manifest, (old, old))
    detail = matlab_view.run_detail(tmp_path, RUN)
    assert detail["status"] == "ended"
    assert detail["output"] == "step 1\nstep 2\n"
    assert detail["figures"] == [f"{RUN}_fig1.png"]


def test_a_run_whose_matlab_stopped_is_lost(tmp_path):
    _record(tmp_path, session_kind="own")
    assert matlab_view.run_detail(tmp_path, RUN)["status"] == "lost"


def test_session_info_needs_a_live_pid(tmp_path):
    (tmp_path / "session.json").write_text('{"name": "s", "mode": "headless", "matlab_pid": %d}' % os.getpid())
    assert matlab_view.session_info(tmp_path)["running"] is True
    (tmp_path / "session.json").write_text('{"name": "s", "matlab_pid": 999999999}')
    assert matlab_view.session_info(tmp_path) == {"running": False}


def test_only_run_ids_and_figure_names_are_accepted(tmp_path):
    runs = _record(tmp_path)
    (runs / f"{RUN}_fig1.png").write_bytes(b"png")
    assert matlab_view.figure_path(tmp_path, f"{RUN}_fig1.png") == runs / f"{RUN}_fig1.png"
    for name in ("../session.json", f"{RUN}.json", f"{RUN}_fig1.png/../x", "x.png"):
        assert matlab_view.figure_path(tmp_path, name) is None
    assert matlab_view.run_detail(tmp_path, "../" + RUN) is None


async def test_hub_serves_the_matlab_page(tmp_path):
    runs = _record(tmp_path / "matlab", status="finished", finished=time.time(), figures=[f"{RUN}_fig1.png"])
    (runs / f"{RUN}_fig1.png").write_bytes(base64.b64decode(PNG))
    hub = Hub("test-token", 0, 0, home=tmp_path)
    transport = httpx.ASGITransport(app=hub.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:47822") as client:
        assert "MATLAB output" in (await client.get("/matlab")).text
        state = (await client.get("/matlab/state")).json()
        assert [run["id"] for run in state["runs"]] == [RUN]
        detail = (await client.get(f"/matlab/run/{RUN}")).json()
        assert detail["status"] == "finished" and detail["code"] == "x = 1"
        image = await client.get(f"/matlab/file/{RUN}_fig1.png")
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        assert (await client.get("/matlab/file/session.json")).status_code == 404
        assert (await client.get("/matlab/run/nope")).status_code == 404
        foreign = await client.get("/matlab/state", headers={"Host": "evil.example"})
        assert foreign.status_code == 403


def test_pdfs_are_served_and_found_for_runs_left_behind(tmp_path):
    runs = _record(tmp_path / "matlab", pdf=True)
    (runs / f"{RUN}.log").write_text("")
    (runs / f"{RUN}_fig1.png").write_bytes(base64.b64decode(PNG))
    (runs / f"{RUN}_fig1.pdf").write_bytes(b"%PDF-1.4\n")
    manifest = runs / f"{RUN}.figs"
    manifest.write_text(f"{runs / (RUN + '_fig1.png')}\n")
    old = time.time() - matlab_view.ORPHAN_SECONDS - 1
    os.utime(manifest, (old, old))
    assert matlab_view.run_detail(tmp_path / "matlab", RUN)["pdfs"] == [f"{RUN}_fig1.pdf"]
    assert matlab_view.figure_path(tmp_path / "matlab", f"{RUN}_fig1.pdf") == runs / f"{RUN}_fig1.pdf"
    assert matlab_view.figure_path(tmp_path / "matlab", f"{RUN}_fig1.eps") is None


async def test_hub_serves_pdfs_as_pdf(tmp_path):
    runs = _record(tmp_path / "matlab", status="finished", finished=time.time())
    (runs / f"{RUN}_fig1.pdf").write_bytes(b"%PDF-1.4\n")
    transport = httpx.ASGITransport(app=Hub("test-token", 0, 0, home=tmp_path).app())
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:47822") as client:
        response = await client.get(f"/matlab/file/{RUN}_fig1.pdf")
        assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
