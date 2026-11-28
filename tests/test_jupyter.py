import asyncio
import json

import httpx
import pytest
import uvicorn
import websockets

from colab_claude_mcp import config
from colab_claude_mcp.hub import WS_MAX_SIZE, Hub
from colab_claude_mcp.jupyter import OutputCollector, collapse_cr
from tests.fake_jupyter import FakeServer, FakeTab

TOKEN = "test-token"


class Harness:
    """A hub served on an ephemeral port, a fake Jupyter server, and fake JupyterLab tabs."""

    def __init__(self, tmp_path):
        self.server = FakeServer()
        self.hub = Hub(TOKEN, 0, 0, home=tmp_path)
        self.tabs: list[FakeTab] = []
        self.port = 0
        self._uvicorn: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    async def start(self):
        settings = uvicorn.Config(
            self.hub.app(),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            ws="websockets-sansio",
            ws_max_size=WS_MAX_SIZE,
        )
        self._uvicorn = uvicorn.Server(settings)
        self._task = asyncio.create_task(self._uvicorn.serve())
        while not self._uvicorn.started:
            await asyncio.sleep(0.01)
        self.port = self._uvicorn.servers[0].sockets[0].getsockname()[1]

    async def tab(self, **kwargs) -> FakeTab:
        bridge = self.hub.jupyter.bridge
        generation = bridge.generation
        tab = FakeTab(self.port, TOKEN, self.server, **kwargs)
        self.tabs.append(tab)
        await tab.connect()
        for _ in range(250):
            if bridge.generation > generation:
                break
            await asyncio.sleep(0.02)
        return tab

    async def call(self, op: str, **args) -> dict:
        return await self.hub.call(op, args)

    async def close(self):
        for tab in self.tabs:
            await tab.close()
        self._uvicorn.should_exit = True
        await self._task


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    await h.start()
    yield h
    await h.close()


async def connected(harness, **args) -> dict:
    await harness.tab()
    result = await harness.call("jupyter_connect", wait_seconds=5, **args)
    assert not result["is_error"], result["text"]
    return result


# ---- pure helpers ----------------------------------------------------------------------


def test_collapse_cr_shows_the_last_state_of_a_progress_line():
    assert collapse_cr("  0%\r 50%\r100%\ndone\n") == "100%\ndone\n"
    assert collapse_cr("abc\r\n") == "abc\n"
    assert collapse_cr("12345\rab") == "ab345"  # like a terminal: overwrite from the line start
    assert collapse_cr("step 1\r") == "step 1\r"  # kept, so that the next chunk still overwrites it
    assert collapse_cr(collapse_cr("step 1\r") + "step 2\n") == "step 2\n"


def test_output_collector_follows_jupyterlab_semantics():
    collector = OutputCollector()
    collector.add("stream", {"name": "stdout", "text": "a"})
    collector.add("stream", {"name": "stdout", "text": "b\n"})
    collector.add("display_data", {"data": {"text/plain": "1"}, "transient": {"display_id": "d"}})
    collector.add("update_display_data", {"data": {"text/plain": "2"}, "transient": {"display_id": "d"}})
    assert [o["output_type"] for o in collector.outputs] == ["stream", "display_data"]
    assert collector.outputs[0]["text"] == "ab\n" and collector.outputs[1]["data"] == {"text/plain": "2"}
    collector.add("clear_output", {"wait": True})
    assert len(collector.outputs) == 2  # wait=True clears only when the next output arrives
    collector.add("stream", {"name": "stdout", "text": "new\n"})
    assert collector.outputs == [{"output_type": "stream", "name": "stdout", "text": "new\n"}]


def test_jupyter_origins(monkeypatch):
    assert config.jupyter_origin_allowed("http://localhost:8899")
    assert config.jupyter_origin_allowed("http://127.0.0.1:8888")
    assert not config.jupyter_origin_allowed("https://evil.example")
    assert not config.jupyter_origin_allowed("")
    monkeypatch.setenv("COLAB_CLAUDE_JUPYTER_ORIGINS", "http://gpu12.example.edu:8899")
    assert config.jupyter_origin_allowed("http://gpu12.example.edu:8899")


# ---- connecting ------------------------------------------------------------------------


async def test_connect_without_a_tab_explains_the_bookmark(harness):
    result = await harness.call("jupyter_connect", wait_seconds=0.2)
    assert result["is_error"]
    assert "bookmark" in result["text"] and "/jupyter/setup" in result["text"] and "password" in result["text"]


async def test_connect_creates_the_notebook_and_a_session(harness):
    result = await connected(harness)
    assert "Created claude/scratch.ipynb" in result["text"]
    assert "Python 3 (ipykernel)" in result["text"] and "torch (PyTorch (CUDA))" in result["text"]
    assert harness.server.files["claude"]["type"] == "directory"
    saved = harness.server.notebook("claude/scratch.ipynb")
    assert saved["cells"] == [] and saved["metadata"]["kernelspec"]["name"] == "python3"
    assert [s["path"] for s in harness.server.sessions] == ["claude/scratch.ipynb"]
    again = await harness.call("jupyter_connect", wait_seconds=5)
    assert "reused" in again["text"] and len(harness.server.sessions) == 1


async def test_connect_with_an_unknown_kernel_lists_the_kernels(harness):
    await harness.tab()
    result = await harness.call("jupyter_connect", kernel="nope", wait_seconds=5)
    assert result["is_error"] and "python3" in result["text"] and "torch" in result["text"]


async def test_a_logged_out_tab_is_reported(harness):
    harness.server.logged_in = False
    await harness.tab()
    result = await harness.call("jupyter_connect", wait_seconds=5)
    assert result["is_error"] and "logged out" in result["text"]


async def test_an_open_notebook_shares_its_kernel(harness):
    harness.server.save(
        "LeNet5.ipynb",
        {
            "cells": [
                {"cell_type": "code", "source": ["x = 1\n", "x"], "metadata": {}, "outputs": [], "execution_count": 3}
            ],
            "metadata": {"kernelspec": {"name": "torch"}},
            "nbformat": 4,
            "nbformat_minor": 4,
        },
    )
    session = harness.server.start_session("LeNet5.ipynb", "torch")
    harness.server.kernels[session["kernel"]["id"]].listeners.add(lambda message: None)  # the user's own tab
    result = await connected(harness, notebook_path="LeNet5.ipynb")
    assert "Opened LeNet5.ipynb" in result["text"] and "also open in JupyterLab" in result["text"]
    assert "Revert, not Overwrite" in result["text"]
    assert harness.hub.jupyter.kernel.id == session["kernel"]["id"] and len(harness.server.sessions) == 1
    cell = harness.hub.jupyter.cells[0]
    assert cell["source"] == "x = 1\nx" and len(cell["id"]) == 8  # an id was given, as nbformat 4.5 wants
    refused = await harness.call("jupyter_connect", notebook_path="LeNet5.ipynb", kernel="python3", wait_seconds=5)
    assert refused["is_error"] and "not replaced" in refused["text"]


async def test_a_kernel_left_running_by_an_earlier_hub_is_reused(harness):
    session = harness.server.start_session("claude/scratch.ipynb", "python3")  # no tab is connected to it
    harness.server.files["claude"] = {"type": "directory"}
    harness.server.save("claude/scratch.ipynb", {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
    result = await connected(harness)
    assert "variables from earlier runs are still there" in result["text"] and "also open" not in result["text"]
    assert harness.hub.jupyter.kernel.id == session["kernel"]["id"]


# ---- cells and runs --------------------------------------------------------------------


async def test_run_returns_text_and_image_and_saves_the_outputs(harness):
    await connected(harness)
    result = await harness.call("add_code_cell", backend="jupyter", code="plot()", run=True)
    assert not result["is_error"], result["text"]
    assert "ran: plot()" in result["text"] and "image 1: image/png, attached" in result["text"]
    assert len(result["images"]) == 1 and list((harness.hub.home / "outputs").glob("jupyter-*.png"))
    saved = harness.server.notebook("claude/scratch.ipynb")["cells"][0]
    assert saved["execution_count"] == 1
    assert [o["output_type"] for o in saved["outputs"]] == ["stream", "display_data"]
    state = harness.hub.jupyter.snapshot()
    assert state["kind"] == "jupyter" and state["cells"][0]["status"] == "done"


async def test_an_error_is_reported_without_ansi_codes(harness):
    await connected(harness)
    result = await harness.call("add_code_cell", backend="jupyter", code="boom", run=True)
    assert result["is_error"] and "ZeroDivisionError: division by zero" in result["text"]
    assert "\x1b" not in result["text"]


async def test_a_long_run_returns_early_with_its_output_so_far(harness):
    await connected(harness)
    early = await harness.call("add_code_cell", backend="jupyter", code="slow", run=True, wait_seconds=0.2)
    assert "still running" in early["text"] and "ran: slow" in early["text"]
    cell_id = harness.hub.jupyter.cells[0]["id"]
    status = await harness.call("jupyter_status")
    assert "[running" in status["text"]
    done = await harness.call("wait_for_cell", cell_id=cell_id, wait_seconds=5)  # routed by its cell id
    assert "finished" in done["text"] and not done["is_error"]


async def test_interrupt_stops_a_running_cell(harness):
    await connected(harness)
    early = await harness.call("add_code_cell", backend="jupyter", code="forever", run=True, wait_seconds=0.2)
    assert "still running" in early["text"]
    stopped = await harness.call("jupyter_kernel", action="interrupt")
    assert not stopped["is_error"]
    cell_id = harness.hub.jupyter.cells[0]["id"]
    result = await harness.call("wait_for_cell", backend="jupyter", cell_id=cell_id, wait_seconds=5)
    assert result["is_error"] and "KeyboardInterrupt" in result["text"]


async def test_restart_ends_a_running_cell(harness):
    await connected(harness)
    await harness.call("add_code_cell", backend="jupyter", code="forever", run=True, wait_seconds=0.1)
    cell_id = harness.hub.jupyter.cells[0]["id"]
    restarted = await harness.call("jupyter_kernel", action="restart")
    assert "variables are gone" in restarted["text"]
    result = await harness.call("wait_for_cell", backend="jupyter", cell_id=cell_id, wait_seconds=5)
    assert result["is_error"] and "restarted" in result["text"]


async def test_progress_bars_keep_only_their_last_state(harness):
    await connected(harness)
    result = await harness.call("add_code_cell", backend="jupyter", code="progress", run=True)
    assert "100%" in result["text"] and "25%" not in result["text"]


async def test_edits_and_get_cells(harness):
    await connected(harness)
    await harness.call("add_text_cell", backend="jupyter", content="# Title")
    await harness.call("add_code_cell", backend="jupyter", code="a = 1")
    code_id = harness.hub.jupyter.cells[1]["id"]
    await harness.call("update_cell", backend="jupyter", cell_id=code_id, content="a = 2")
    await harness.call("move_cell", backend="jupyter", cell_id=code_id, cell_index=0)
    listing = await harness.call("get_cells", backend="jupyter")
    assert listing["text"].index("a = 2") < listing["text"].index("# Title")
    saved = [cell["source"] for cell in harness.server.notebook("claude/scratch.ipynb")["cells"]]
    assert saved == ["a = 2", "# Title"]
    await harness.call("delete_cell", backend="jupyter", cell_id=code_id)
    assert [c["source"] for c in harness.server.notebook("claude/scratch.ipynb")["cells"]] == ["# Title"]
    missing = await harness.call("delete_cell", backend="jupyter", cell_id="nope")
    assert missing["is_error"] and "get_cells" in missing["text"]


async def test_a_change_saved_in_jupyterlab_is_kept(harness):
    await connected(harness)
    await harness.call("add_code_cell", backend="jupyter", code="mine = 1")
    notebook = json.loads(json.dumps(harness.server.notebook("claude/scratch.ipynb")))
    notebook["cells"].append({"cell_type": "markdown", "id": "user0001", "metadata": {}, "source": "added by the user"})
    harness.server.save("claude/scratch.ipynb", notebook)  # the user's JupyterLab tab saved it
    await harness.call("add_code_cell", backend="jupyter", code="mine = 2")
    sources = [cell["source"] for cell in harness.server.notebook("claude/scratch.ipynb")["cells"]]
    assert sources == ["mine = 1", "added by the user", "mine = 2"]
    assert any(event["kind"] == "reload" for event in harness.hub.jupyter.events)


async def test_a_newer_tab_takes_over_and_the_kernel_follows(harness):
    first = (await connected(harness)) and harness.tabs[0]
    second = await harness.tab()
    await asyncio.sleep(0.2)
    assert first.closed_code == 4000
    result = await harness.call("add_code_cell", backend="jupyter", code="after", run=True)
    assert not result["is_error"] and "ran: after" in result["text"]
    assert second.channels  # the kernel websocket now goes through the new tab


async def test_shutdown_then_run_starts_a_new_kernel(harness):
    await connected(harness)
    await harness.call("add_code_cell", backend="jupyter", code="x = 1")
    old = harness.hub.jupyter.kernel.id
    result = await harness.call("jupyter_kernel", action="shutdown")
    assert "stays on the server" in result["text"] and not harness.server.kernels
    cell_id = harness.hub.jupyter.cells[0]["id"]
    rerun = await harness.call("run_code_cell", backend="jupyter", cell_id=cell_id)
    assert not rerun["is_error"] and harness.hub.jupyter.kernel.id != old


async def test_a_bridge_from_an_older_hub_is_told_to_reload(harness):
    tab = FakeTab(harness.port, TOKEN, harness.server, bridge="0.0.1")
    harness.tabs.append(tab)
    await tab.connect()
    await asyncio.sleep(0.3)
    assert {"type": "reload"} in tab.received and not harness.hub.jupyter.bridge.is_connected


async def test_cell_tools_default_to_jupyter_only_when_colab_is_not_connected(harness):
    assert not harness.hub._for_jupyter("get_cells", {})  # nothing open: Colab, as before
    await connected(harness)
    assert harness.hub._for_jupyter("get_cells", {})
    assert not harness.hub._for_jupyter("get_cells", {"backend": "colab"})
    assert not harness.hub._for_jupyter("status", {})


# ---- security and pages ----------------------------------------------------------------


async def test_the_tab_websocket_needs_the_token_and_a_local_origin(harness):
    with pytest.raises(websockets.InvalidStatus):
        await websockets.connect(
            f"ws://localhost:{harness.port}/jupyter/ws?token={TOKEN}", origin="https://evil.example"
        )
    async with websockets.connect(
        f"ws://localhost:{harness.port}/jupyter/ws?token=wrong", origin="http://localhost:8899"
    ) as ws:
        message = json.loads(await ws.recv())
        assert message["type"] == "error"
        with pytest.raises(websockets.ConnectionClosed):
            await ws.recv()
        assert ws.close_code == 4003
    assert not harness.hub.jupyter.bridge.is_connected


async def test_pages_and_the_bridge_script(harness):
    transport = httpx.ASGITransport(app=harness.hub.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:47822") as client:
        page = await client.get("/jupyter/")
        assert "<title>Jupyter mirror</title>" in page.text
        assert '"Colab mirror"' in page.text  # the link back to the Colab mirror is not renamed
        assert page.status_code == 200 and "Jupyter mirror" in page.text
        assert (await client.get("/jupyter", follow_redirects=True)).status_code == 200
        state = (await client.get("/jupyter/state")).json()
        assert state["kind"] == "jupyter" and not state["connected"]
        setup = await client.get("/jupyter/setup")
        assert "javascript:" in setup.text and TOKEN not in setup.text
        script = await client.get("/jupyter/bridge.js", headers={"Sec-Fetch-Site": "same-site"})
        assert script.status_code == 200 and json.dumps(TOKEN) in script.text and "__CCMCP_" not in script.text
        cross = await client.get("/jupyter/bridge.js", headers={"Sec-Fetch-Site": "cross-site"})
        assert cross.status_code == 403
        assert (await client.get("/jupyter/state", headers={"Host": "evil.example"})).status_code == 403
        colab = await client.get("/")
        assert "Colab mirror" in colab.text and 'href="/jupyter/"' in colab.text
