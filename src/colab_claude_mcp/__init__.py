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
# Modified for colab-claude-mcp: the entry point now selects between the stdio
# MCP server (default) and the long-lived hub.

"""colab-claude-mcp: drive a Google Colab notebook from Claude."""

import argparse
import json
import sys
import urllib.error
import urllib.request

from colab_claude_mcp import config

__version__ = config.VERSION


def _hub_request(path: str, post: bool = False) -> dict | None:
    request = urllib.request.Request(config.hub_url() + path, method="POST" if post else "GET")
    if post:
        request.add_header("Authorization", f"Bearer {config.load_token()}")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="colab-claude-mcp",
        description="MCP server that lets Claude drive a Google Colab notebook open in your browser.",
    )
    parser.add_argument("--version", action="version", version=config.VERSION)
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "hub", "status", "stop"],
        help="serve: stdio MCP server (default). hub: run the hub in the foreground. "
        "status: show whether the hub is running. stop: stop the hub.",
    )
    args = parser.parse_args(argv)

    if args.command == "serve":
        from colab_claude_mcp.server import run_stdio

        run_stdio()
        return 0
    if args.command == "hub":
        from colab_claude_mcp.hub import run_hub

        return run_hub()
    if args.command == "status":
        health = _hub_request("/health")
        if not health:
            print("hub: not running")
            return 1
        tab = "connected" if health.get("connected") else "not connected"
        print(f"hub: running (version {health.get('version')}, pid {health.get('pid')}); Colab tab: {tab}")
        print(f"mirror: {config.mirror_url()}")
        return 0
    # stop
    if _hub_request("/health") is None:
        print("hub: not running")
        return 0
    stopped = _hub_request("/api/shutdown", post=True)
    print("hub: stopping" if stopped else "hub: did not accept the stop request")
    return 0 if stopped else 1


if __name__ == "__main__":
    sys.exit(main())
