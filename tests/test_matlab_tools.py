from colab_claude_mcp import matlab_tools


def test_quote_doubles_single_quotes():
    assert matlab_tools._quote("/tmp/it's.m") == "'/tmp/it''s.m'"


def test_clip_keeps_head_and_tail():
    text = "a" * 1000 + "b" * 30_000 + "c" * 1000
    clipped = matlab_tools._clip(text)
    assert clipped.startswith("a") and clipped.endswith("c")
    assert "characters omitted" in clipped and len(clipped) < matlab_tools.MAX_TEXT + 200


def test_tools_registered_without_engine_import():
    from colab_claude_mcp.server import mcp  # importing must not need matlabengine

    assert mcp is not None


async def test_connect_notes_mode_only_when_a_mode_is_asked_for(monkeypatch):
    evals = []

    class FakeEngine:
        def addpath(self, *args, **kwargs):
            pass

        def eval(self, code, **kwargs):
            evals.append(code)

    fake = type("FakeEngineModule", (), {})()
    fake.find_matlab = lambda: ("colab_claude_mcp",)
    fake.connect_matlab = lambda name: FakeEngine()
    monkeypatch.setenv("COLAB_CLAUDE_MATLAB_SESSION", "colab_claude_mcp")
    monkeypatch.delenv("COLAB_CLAUDE_MATLAB_STYLE", raising=False)
    monkeypatch.setattr(matlab_tools, "_import_engine", lambda: fake)
    monkeypatch.setattr(matlab_tools.matlab_host, "read_state", lambda: {"mode": "desktop"})
    monkeypatch.setattr(matlab_tools, "_engine", None)

    # matlab_eval's automatic connect and a bare matlab_connect() take the session as it is.
    assert (await matlab_tools._connect())[1] == []
    assert (await matlab_tools._connect(desktop=True))[1] == []
    notes = (await matlab_tools._connect(desktop=False))[1]
    assert len(notes) == 1 and "already running in desktop mode" in notes[0]
    # connecting applies the IEEE plot style; 'auto' keeps a style chosen in the session
    assert evals == ["ccmcp.style('auto');"]


def test_matlab_style_setting(monkeypatch):
    from colab_claude_mcp import config

    monkeypatch.delenv("COLAB_CLAUDE_MATLAB_STYLE", raising=False)
    assert config.matlab_style() == "ieee"
    monkeypatch.setenv("COLAB_CLAUDE_MATLAB_STYLE", " MATLAB ")
    assert config.matlab_style() == "matlab"
    monkeypatch.setenv("COLAB_CLAUDE_MATLAB_STYLE", "nature")  # unknown: keep the default
    assert config.matlab_style() == "ieee"
