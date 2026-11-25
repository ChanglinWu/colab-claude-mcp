from colab_claude_mcp import outputs
from tests.fake_colab import PNG


def test_normalize_merges_streams_and_picks_richest_type():
    items = outputs.normalize(
        [
            {"output_type": "stream", "name": "stdout", "text": ["a\n"]},
            {"output_type": "stream", "name": "stdout", "text": "b\n"},
            {"output_type": "display_data", "data": {"text/plain": ["<Figure>"], "image/png": PNG}},
            {"output_type": "execute_result", "data": {"text/plain": ["42"]}},
            {"output_type": "error", "ename": "E", "evalue": "bad", "traceback": ["\x1b[31mline\x1b[0m"]},
        ]
    )
    assert [item["kind"] for item in items] == ["stream", "image", "text", "error"]
    assert items[0]["text"] == "a\nb\n"
    assert items[3]["traceback"] == "line"
    assert outputs.has_error(items)


def test_render_attaches_limited_images_and_summarizes_large_html():
    items = [{"kind": "image", "mime": "image/png", "data": PNG}] * 6
    items.append(
        {"kind": "html", "html": "<div>" + "<img src='data:image/png;base64,AAAA'>" * 500 + "</div>", "plain": ""}
    )
    text, images = outputs.render(items)
    assert len(images) == outputs.MAX_IMAGES
    assert "image 6: image/png, not attached" in text
    assert "HTML output" in text and "500 embedded images" in text
    assert len(text) < 1_000


def test_render_small_html_as_text_and_clips_long_text():
    text, _ = outputs.render([{"kind": "html", "html": "<b>hi</b> <script>x()</script>there", "plain": ""}])
    assert text == "hi there"
    text, _ = outputs.render([{"kind": "stream", "name": "stdout", "text": "y" * 50_000}])
    assert len(text) < 21_000 and "characters omitted" in text


def test_save_images(tmp_path):
    paths = outputs.save_images([{"kind": "image", "mime": "image/png", "data": PNG}], tmp_path, "a/b")
    assert [path.name for path in paths] == ["a_b-1.png"]
    assert paths[0].read_bytes().startswith(b"\x89PNG")


def test_extract_outputs_tolerates_garbage():
    assert outputs.extract_outputs({"content": [{"type": "text", "text": "not json"}]}) == []
