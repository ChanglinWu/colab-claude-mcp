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
# Modified for colab-claude-mcp: new file (moved out of hub.py, shared with jupyter.py).

"""What the hub hands back to the stdio server, and the record of a cell run."""

import asyncio
from dataclasses import dataclass, field
import time


def ok(text: str, images: list[dict] | None = None) -> dict:
    return {"text": text, "images": images or [], "is_error": False}


def err(text: str) -> dict:
    return {"text": text, "images": [], "is_error": True}


@dataclass
class Run:
    cell_id: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    status: str = "running"  # running | done | error | failed
    error: str = ""
    task: asyncio.Task | None = None

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started
