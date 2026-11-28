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

"""Turn notebook cell outputs into something small and readable.

Colab returns outputs in Jupyter's nbformat shape. A single plot is tens of KB of
base64 and a matplotlib animation is many MB of HTML, so raw outputs are a poor
thing to hand to a model. `normalize` flattens them into simple items; `render`
turns the items into text plus a few images.
"""

import base64
import binascii
import html as html_lib
import json
from pathlib import Path
import re

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
TAGS = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.S | re.I)
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif")
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}

MAX_TEXT = 20_000
MAX_IMAGES = 4
INLINE_HTML_LIMIT = 4_000


def _join(value) -> str:
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return "" if value is None else str(value)


def tool_text(result: dict) -> str:
    """Concatenated text blocks of an MCP tool result."""
    return "".join(block.get("text", "") for block in result.get("content", []) if block.get("type") == "text")


def extract_outputs(result: dict) -> list[dict]:
    """The nbformat outputs inside a run_code_cell tool result."""
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("outputs"), list):
        return structured["outputs"]
    try:
        data = json.loads(tool_text(result))
    except ValueError:
        return []
    outputs = data.get("outputs") if isinstance(data, dict) else None
    return outputs if isinstance(outputs, list) else []


def normalize(outputs: list[dict] | None) -> list[dict]:
    """Flatten nbformat outputs into items with a `kind`.

    Kinds: stream (name, text), text (text), image (mime, data), html (html),
    error (ename, evalue, traceback). Consecutive stream chunks are merged.
    """
    items: list[dict] = []
    for output in outputs or []:
        kind = output.get("output_type")
        if kind == "stream":
            name = output.get("name", "stdout")
            text = _join(output.get("text"))
            if items and items[-1]["kind"] == "stream" and items[-1]["name"] == name:
                items[-1]["text"] += text
            else:
                items.append({"kind": "stream", "name": name, "text": text})
        elif kind == "error":
            items.append(
                {
                    "kind": "error",
                    "ename": str(output.get("ename", "Error")),
                    "evalue": str(output.get("evalue", "")),
                    "traceback": ANSI.sub("", "\n".join(output.get("traceback") or [])),
                }
            )
        elif kind in ("display_data", "execute_result", "update_display_data"):
            data = output.get("data") or {}
            image = next((mime for mime in IMAGE_TYPES if mime in data), None)
            if image:
                items.append({"kind": "image", "mime": image, "data": _join(data[image]).replace("\n", "")})
            elif "text/html" in data:
                items.append({"kind": "html", "html": _join(data["text/html"]), "plain": _join(data.get("text/plain"))})
            elif "image/svg+xml" in data:
                items.append({"kind": "html", "html": _join(data["image/svg+xml"]), "plain": "[SVG image]"})
            elif "text/markdown" in data:
                items.append({"kind": "text", "text": _join(data["text/markdown"])})
            elif "text/plain" in data:
                items.append({"kind": "text", "text": _join(data["text/plain"])})
            elif data:
                items.append({"kind": "text", "text": f"[output of type {', '.join(data)}]"})
    return items


def has_error(items: list[dict]) -> bool:
    return any(item["kind"] == "error" for item in items)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n… [{len(text) - limit:,} characters omitted] …\n{text[-tail:]}"


def _html_as_text(item: dict) -> str:
    html = item["html"]
    size = len(html)
    if size > INLINE_HTML_LIMIT:
        frames = html.count("data:image/")
        detail = f", {frames} embedded images" if frames else ""
        plain = item.get("plain") or ""
        note = f" ({plain})" if plain and len(plain) < 200 else ""
        return f"[HTML output, {size / 1024:,.0f} KB{detail}{note} — shown in the notebook and on the mirror page]"
    text = html_lib.unescape(TAGS.sub(" ", html))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text or "[HTML output with no text]"


def save_images(items: list[dict], directory: Path, stem: str) -> list[Path]:
    """Write every image item to `directory` and return the paths, in order."""
    paths: list[Path] = []
    directory.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]", "_", stem) or "cell"
    count = 0
    for item in items:
        if item["kind"] != "image":
            continue
        count += 1
        path = directory / f"{safe_stem}-{count}.{EXTENSIONS.get(item['mime'], 'bin')}"
        try:
            path.write_bytes(base64.b64decode(item["data"]))
        except (binascii.Error, ValueError):
            continue
        paths.append(path)
    return paths


def render(
    items: list[dict],
    max_text: int = MAX_TEXT,
    max_images: int = MAX_IMAGES,
    image_paths: list[Path] | None = None,
) -> tuple[str, list[dict]]:
    """Render items as (text, images). `images` are {"mime", "data"} dicts, at most max_images."""
    parts: list[str] = []
    images: list[dict] = []
    image_count = 0
    paths = list(image_paths or [])
    for item in items:
        kind = item["kind"]
        if kind == "stream":
            text = ANSI.sub("", item["text"]).rstrip("\n")
            if text:
                parts.append(text if item["name"] == "stdout" else f"[{item['name']}]\n{text}")
        elif kind == "text":
            parts.append(item["text"].rstrip("\n"))
        elif kind == "html":
            parts.append(_html_as_text(item))
        elif kind == "error":
            traceback = item["traceback"].strip()
            parts.append(f"[error] {item['ename']}: {item['evalue']}" + (f"\n{traceback}" if traceback else ""))
        elif kind == "image":
            image_count += 1
            where = f", saved to {paths[image_count - 1]}" if image_count <= len(paths) else ""
            if len(images) < max_images:
                images.append({"mime": item["mime"], "data": item["data"]})
                parts.append(f"[image {image_count}: {item['mime']}, attached{where}]")
            else:
                parts.append(f"[image {image_count}: {item['mime']}, not attached{where}]")
    text = "\n".join(part for part in parts if part)
    return _clip(text, max_text), images


def summarize(items: list[dict], limit: int = 1_500) -> str:
    """Text-only rendering for listings; images are counted, not attached."""
    text, _ = render(items, max_text=limit, max_images=0)
    return text.replace(", not attached", "")
