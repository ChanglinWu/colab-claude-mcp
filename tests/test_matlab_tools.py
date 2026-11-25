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
