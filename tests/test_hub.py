import asyncio
from pathlib import Path
import re

import httpx
import pytest
import websockets

from colab_claude_mcp import hub as hub_module
from colab_claude_mcp.hub import Hub
from tests.fake_colab import FakeColab

TOKEN = "test-token"


class Harness:
    """A hub on an ephemeral port whose "browser" is a FakeColab."""

    def __init__(self, tmp_path):
        self.opened: list[str] = []
        self.tabs: list[FakeColab] = []
        self.connect_tabs = True
        self.loop = asyncio.get_running_loop()
        self.hub = Hub(TOKEN, 0, 0, home=tmp_path, open_browser=self._open)

    def _open(self, url, browser=None):
        self.opened.append(url)
        if self.connect_tabs:
            tab = FakeColab(self.hub.frontend.port, TOKEN)
            self.tabs.append(tab)
            asyncio.run_coroutine_threadsafe(tab.connect(), self.loop).result(5)
        return True

    async def close(self):
        for tab in self.tabs:
            await tab.close()
        await self.hub.frontend.stop()


@pytest.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    await h.hub.frontend.start()
    yield h
    await h.close()


async def test_connect_reuses_the_open_tab(harness):
    first = await harness.hub.call("connect", {"wait_seconds": 5})
    assert not first["is_error"] and "Connected to Colab" in first["text"]
    second = await harness.hub.call("connect", {"wait_seconds": 5})
    assert "reused" in second["text"]
    assert len(harness.opened) == 1
    assert f"mcpProxyPort={harness.hub.frontend.port}" in harness.opened[0]


async def test_new_notebook_opens_a_tab_and_replaces_the_old_one(harness):
    await harness.hub.call("connect", {"wait_seconds": 5})
    await harness.hub.call("add_code_cell", {"code": "x = 1"})
    result = await harness.hub.call("connect", {"new_notebook": True, "wait_seconds": 5})
    assert not result["is_error"]
    assert len(harness.opened) == 2
    await asyncio.sleep(0.2)
    assert harness.tabs[0].closed_code == 1000  # the old tab was told it was replaced
    assert harness.hub.frontend.is_connected
    assert [cell["id"] for cell in harness.hub.cells] == ["first"]  # the new notebook, not the old one


async def test_notebook_url_must_be_colab(harness):
    result = await harness.hub.call("connect", {"notebook_url": "https://example.com/x", "wait_seconds": 1})
    assert result["is_error"] and harness.opened == []
    ok = await harness.hub.call(
        "connect", {"notebook_url": "https://colab.research.google.com/drive/abc#scrollTo=1", "wait_seconds": 5}
    )
    assert not ok["is_error"]
    assert harness.opened[0].startswith("https://colab.research.google.com/drive/abc#mcpProxyToken=")


async def test_a_tab_waiting_for_sign_in_is_waited_for_not_duplicated(harness):
    harness.connect_tabs = False
    first = await harness.hub.call("connect", {"wait_seconds": 0.5})
    assert first["is_error"] and "Sign in" in first["text"]
    second = await harness.hub.call("connect", {"wait_seconds": 0.5})
    assert second["is_error"] and "no further tab" in second["text"]
    assert len(harness.opened) == 1

    async def sign_in_later():  # the user signs in; the same tab then connects
        await asyncio.sleep(0.3)
        tab = FakeColab(harness.hub.frontend.port, TOKEN)
        harness.tabs.append(tab)
        await tab.connect()

    task = asyncio.create_task(sign_in_later())
    third = await harness.hub.call("connect", {"wait_seconds": 5})
    await task
    assert not third["is_error"] and "Connected to Colab" in third["text"]
    assert len(harness.opened) == 1
    assert harness.hub.pending_tab_at is None


async def test_new_notebook_ignores_a_pending_tab(harness):
    harness.connect_tabs = False
    await harness.hub.call("connect", {"wait_seconds": 0.3})
    harness.connect_tabs = True
    result = await harness.hub.call("connect", {"new_notebook": True, "wait_seconds": 5})
    assert not result["is_error"] and len(harness.opened) == 2


async def test_disconnect_reason_is_recorded(harness):
    await harness.hub.call("connect", {"wait_seconds": 5})
    await harness.tabs[0].close()
    await asyncio.sleep(0.2)
    assert "code 1000" in harness.hub.events[-1]["text"]


async def test_add_run_returns_text_and_image(harness):
    await harness.hub.call("connect", {"wait_seconds": 5})
    result = await harness.hub.call("add_code_cell", {"code": "plot()\nmore()", "run": True})
    assert not result["is_error"]
    assert "ran: plot()" in result["text"] and "image 1: image/png, attached" in result["text"]
    assert len(result["images"]) == 1
    assert list((harness.hub.home / "outputs").glob("*.png"))
    state = harness.hub.snapshot()
    assert [cell["status"] for cell in state["cells"]] == ["idle", "done"]
    assert state["cells"][1]["source"] == "plot()\nmore()"


async def test_error_cell_is_reported(harness):
    await harness.hub.call("connect", {"wait_seconds": 5})
    result = await harness.hub.call("add_code_cell", {"code": "boom", "run": True})
    assert result["is_error"]
    assert "ZeroDivisionError: division by zero" in result["text"] and "\x1b" not in result["text"]
    assert harness.hub.snapshot()["cells"][1]["status"] == "error"


async def test_long_run_returns_early_then_wait_for_cell(harness):
    await harness.hub.call("connect", {"wait_seconds": 5})
    early = await harness.hub.call("add_code_cell", {"code": "slow", "run": True, "wait_seconds": 0.1})
    assert "still running" in early["text"]
    assert harness.hub.snapshot()["cells"][1]["status"] == "running"
    listing = await harness.hub.call("status", {})  # the tab still answers while a cell runs
    assert "[running" in listing["text"]
    done = await harness.hub.call("wait_for_cell", {"cell_id": "cell1", "wait_seconds": 5})
    assert "finished" in done["text"] and "ran: slow" in done["text"]


async def test_multi_megabyte_output_survives(harness):
    await harness.hub.call("connect", {"wait_seconds": 5})
    result = await harness.hub.call("add_code_cell", {"code": "big", "run": True})
    assert not result["is_error"] and "HTML output" in result["text"]
    assert len(result["text"]) < 2_000
    assert len(harness.hub.outputs["cell1"][1]["html"]) > 3_000_000


async def test_edit_operations_and_get_cells(harness):
    await harness.hub.call("connect", {"wait_seconds": 5})
    await harness.hub.call("add_text_cell", {"content": "# Title", "cell_index": 0})
    await harness.hub.call("add_code_cell", {"code": "a = 1"})
    await harness.hub.call("update_cell", {"cell_id": "cell2", "content": "a = 2"})
    await harness.hub.call("move_cell", {"cell_id": "cell2", "cell_index": 0})
    await harness.hub.call("delete_cell", {"cell_id": "first"})
    listing = await harness.hub.call("get_cells", {})
    assert listing["text"].index("a = 2") < listing["text"].index("# Title")
    assert "first" not in listing["text"]
    missing = await harness.hub.call("delete_cell", {"cell_id": "nope"})
    assert missing["is_error"]


async def test_calls_fail_cleanly_without_a_tab_and_after_it_closes(harness):
    result = await harness.hub.call("add_code_cell", {"code": "x"})
    assert result["is_error"] and "colab_connect" in result["text"]
    await harness.hub.call("connect", {"wait_seconds": 5})
    await harness.tabs[0].close()
    await asyncio.sleep(0.2)
    assert not harness.hub.frontend.is_connected
    assert "Not connected" in (await harness.hub.call("status", {}))["text"]
    # A later connect opens a tab again, because the old one is gone.
    again = await harness.hub.call("connect", {"wait_seconds": 5})
    assert not again["is_error"] and len(harness.opened) == 2


async def test_websocket_rejects_bad_token_and_foreign_origin(harness):
    port = harness.hub.frontend.port
    with pytest.raises(websockets.InvalidStatus):
        await websockets.connect(
            f"ws://localhost:{port}/?access_token=wrong",
            subprotocols=["mcp"],
            origin="https://colab.research.google.com",
        )
    with pytest.raises(websockets.InvalidStatus):
        await websockets.connect(
            f"ws://localhost:{port}/?access_token={TOKEN}", subprotocols=["mcp"], origin="https://evil.example"
        )


async def test_http_api(harness):
    await harness.hub.call("connect", {"wait_seconds": 5})
    await harness.hub.call("add_code_cell", {"code": "plot()", "run": True})
    transport = httpx.ASGITransport(app=harness.hub.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:47822") as client:
        assert "Colab mirror" in (await client.get("/")).text
        state = (await client.get("/state")).json()
        assert state["connected"] and len(state["cells"]) == 2
        items = (await client.get("/outputs/cell1")).json()["items"]
        assert [item["kind"] for item in items] == ["stream", "image"]
        assert (await client.post("/api/call", json={"op": "status"})).status_code == 403
        good = await client.post("/api/call", json={"op": "status"}, headers={"Authorization": f"Bearer {TOKEN}"})
        assert good.status_code == 200 and "Connected" in good.json()["text"]
        assert (await client.get("/state", headers={"Host": "evil.example"})).status_code == 403


async def test_the_mirror_pages_get_their_vendored_libraries(harness):
    here = Path(hub_module.__file__).parent
    page = (here / "mirror.html").read_text(encoding="utf-8")
    names = re.findall(r"/vendor/([^\"']+)", page)
    assert len(names) == 4, names  # KaTeX's style sheet and the three scripts
    css = next(here.glob("vendor/katex-*/katex.min.css"))
    fonts = sorted(set(re.findall(r"url\((fonts/[^)]+\.woff2)\)", css.read_text(encoding="utf-8"))))
    assert len(fonts) == 20
    transport = httpx.ASGITransport(app=harness.hub.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:47822") as client:
        for name in names + [str(css.parent.relative_to(here / "vendor") / font) for font in fonts]:
            response = await client.get("/vendor/" + name)
            assert response.status_code == 200 and response.content, name
            assert "immutable" in response.headers["cache-control"]
        script = await client.get("/vendor/" + next(name for name in names if name.endswith("katex.min.js")))
        assert script.headers["content-type"].startswith("text/javascript")
        assert (await client.get("/vendor/..%2Fhub.py")).status_code == 404
        assert (await client.get("/vendor/README.md")).status_code == 404  # scripts, styles and fonts only
        assert (
            await client.get("/vendor/katex-0.19.1/katex.min.js", headers={"Host": "evil.example"})
        ).status_code == 403
