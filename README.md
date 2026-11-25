# colab-claude-mcp

An MCP server that lets Claude drive a Google Colab notebook open in your browser.
It is a fork of Google's [colab-mcp](https://github.com/googlecolab/colab-mcp), reworked for
Claude Code and the Claude desktop app.

中文简介：这是 Google 官方 colab-mcp 的一个分支，为 Claude 重构。连接由一个常驻的本地进程保持，
所以换会话时不会重新打开 Colab 标签页；它还提供一个实时镜像页面，可以在 Claude 内置浏览器里
查看 notebook 的单元格、运行状态和输出。

## What is different from colab-mcp

| | colab-mcp | colab-claude-mcp |
|---|---|---|
| Colab tab | A new tab for every MCP server start (random port and token) | One tab stays connected across Claude sessions (fixed port, saved token, long-lived hub) |
| New notebook | Always | Only when asked: `colab_connect(new_notebook=true)` or a `notebook_url` |
| Watching progress | Only in the Colab tab | A live mirror page at `http://localhost:47822/`, made for Claude's built-in browser |
| Plots | Base64 inside a JSON string | Attached as images Claude can see, and saved to disk |
| Large outputs | Connection drops above 1 MB | No size limit; large HTML (for example a matplotlib animation) is summarised for Claude and shown in full on the mirror page |
| Long-running cells | The call blocks until the cell ends | The call returns after `wait_seconds`; `wait_for_cell` and `colab_status` report partial output |
| Tools | Appear after connecting (`tools/list_changed`) | Fixed list, available from the start of the session |

## Install

You need [uv](https://docs.astral.sh/uv/) and a browser that is signed in to Google.

Claude Code:

```bash
claude mcp add colab-claude-mcp --scope user -- uvx git+https://github.com/ChanglinWu/colab-claude-mcp
```

To open Colab in a specific browser instead of the system default, set `COLAB_CLAUDE_BROWSER`
(any name Python's `webbrowser` module knows, such as `chrome`, `firefox` or `safari`):

```bash
claude mcp add colab-claude-mcp --scope user -e COLAB_CLAUDE_BROWSER=chrome -- uvx git+https://github.com/ChanglinWu/colab-claude-mcp
```

Other clients (`mcp.json` style):

```json
{
  "mcpServers": {
    "colab-claude-mcp": {
      "command": "uvx",
      "args": ["git+https://github.com/ChanglinWu/colab-claude-mcp"],
      "env": { "COLAB_CLAUDE_BROWSER": "chrome" }
    }
  }
}
```

## Use

Ask Claude to connect to Colab. The first time, a Colab tab opens in your browser and connects
back to your machine. After that:

- Later sessions reuse that tab. No new tab opens while it stays open.
- Ask for "a new Colab notebook" (or give a notebook URL) to switch; the newest tab becomes the active one.
- Ask Claude to open the mirror page in its built-in browser to watch along.
- Pick the runtime type (CPU, GPU) in the Colab tab yourself.

### Tools

| Tool | What it does |
|---|---|
| `colab_connect` | Connect, reusing the open tab. `new_notebook` or `notebook_url` opens a tab; `open_tab=false` waits for one that is already open |
| `colab_status` | Connection state, cells with run status, optional output summaries, mirror URL |
| `get_cells` | Full source of cells, optionally with outputs |
| `add_code_cell` | Add a code cell (appended by default); `run=true` runs it |
| `add_text_cell` | Add a Markdown cell |
| `update_cell` | Replace a cell's content |
| `run_code_cell` | Run a cell; returns text and attaches plots as images |
| `wait_for_cell` | Wait for a cell that is still running |
| `delete_cell`, `move_cell` | Remove or reorder cells |

## How it works

```
Claude Code session ── stdio ──> colab-claude-mcp (stateless, one per session)
                                        │ HTTP on 127.0.0.1:47822 (token)
                                        ▼
Built-in browser ── GET / ──────> hub (one long-lived process) <── websocket on localhost:47821 ── Colab tab
   (mirror page)                  keeps cells, run status, outputs
```

The stdio server starts the hub on first use. The hub keeps running after the session ends, which
is what keeps the tab connected. It exits by itself after two hours with no tab and no requests.

```bash
uvx --from git+https://github.com/ChanglinWu/colab-claude-mcp colab-claude-mcp status   # is the hub running?
uvx --from git+https://github.com/ChanglinWu/colab-claude-mcp colab-claude-mcp stop     # stop it
```

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `COLAB_CLAUDE_BROWSER` | `BROWSER`, else the system default | Browser that opens Colab tabs |
| `COLAB_CLAUDE_WS_PORT` | `47821` | Port the Colab tab connects to |
| `COLAB_CLAUDE_HTTP_PORT` | `47822` | Port of the mirror page and the local API |
| `COLAB_CLAUDE_HOME` | `~/.colab-claude-mcp` | Token, logs (`hub.log`) and saved output images (`outputs/`) |

## Limits

- The Colab page does not reconnect by itself. If the hub stops (reboot, `stop`), or the tab is closed,
  reloaded or signed out of Google, the next connect opens a new tab. An upgrade does not replace a hub
  that has a tab connected; the new version takes over once that tab is gone.
- If a newly opened tab is waiting for a Google sign-in, later connects wait for that tab (for up to
  15 minutes) instead of opening more.
- Disconnects are recorded with their close code in the mirror page's Activity list and in `hub.log`.
- One notebook at a time. The most recently connected tab is the active one.
- The tab uses whichever Google account that browser opens Colab with.
- The mirror page is read-only and shows what the hub knows: cells and outputs as of the last sync
  (every 2 s while a cell runs or the page is open).
- The Colab page decides which notebook operations exist; this server wraps the seven it offers today.

## MATLAB (local)

The same server can also run MATLAB code in a MATLAB installed on this computer, with all your
toolboxes, for example finite element models with the PDE Toolbox.

中文：同一个 MCP 也能驱动本机 MATLAB（含全部工具箱，例如用 PDE Toolbox 做有限元仿真）。MATLAB 会话常驻，
换 Claude 会话后工作区变量还在；图会以图片返回并保存为 PNG。

### Install

The MATLAB tools need MATLAB Engine for Python (`matlabengine`) in the server's environment, at the
version that matches your MATLAB release, on Python 3.14 or older (the engine does not support 3.15):

```bash
claude mcp add colab-claude-mcp --scope user -e COLAB_CLAUDE_BROWSER=chrome -- uvx --python 3.14 --with "matlabengine==26.2.*" git+https://github.com/ChanglinWu/colab-claude-mcp
```

| MATLAB | `matlabengine` |
|---|---|
| R2026b | `26.2.*` |
| R2026a | `26.1.*` |
| R2025b | `25.2.*` |
| R2025a | `25.1.*` |

Without the engine the Colab tools work as before and the MATLAB tools say how to install it.

### Tools

| Tool | What it does |
|---|---|
| `matlab_connect` | Start or connect to the MATLAB session. Headless by default; `desktop=true` opens the MATLAB desktop so you can watch; `session` connects to a session you shared yourself |
| `matlab_eval` | Run `code` or a `.m` `script` in the base workspace. Returns printed output and attaches the figures the run made; returns early after `wait_seconds` |
| `matlab_wait` | Wait for a run that is still going |
| `matlab_interrupt` | Interrupt the run (like Ctrl+C); the workspace is kept |
| `matlab_status` | Session, mode, current run, working folder, open figures and `whos` |
| `matlab_stop` | Quit the session (the workspace is lost) |

### How it works

MATLAB started with `-batch` cannot be found by the engine, and with `-nodesktop` it exits when its
stdin closes. So `colab-claude-mcp matlab-host` (started for you, detached) starts MATLAB through the
engine, shares it as `colab_claude_mcp`, and waits until MATLAB exits. Each Claude session connects to
that shared session by name, so variables, meshes and results survive between sessions, the way the
Colab tab survives through the hub.

Each run marks the figures that already exist, runs the code, then exports the new figures with
`exportgraphics` (helpers in `src/colab_claude_mcp/mfiles/+ccmcp`). In headless mode figures are
invisible and closed after export; in desktop mode they stay open.

To let Claude work in a MATLAB desktop you opened yourself, run
`matlab.engine.shareEngine('colab_claude_mcp')` in it (or any name, then `matlab_connect(session=...)`).

| Variable | Default | Meaning |
|---|---|---|
| `COLAB_CLAUDE_MATLAB_SESSION` | `colab_claude_mcp` | Name of the shared MATLAB session |
| `COLAB_CLAUDE_MATLAB_OPTIONS` | none | Extra MATLAB startup options, such as `-sd /path/to/project` |

Run code, logs (`.m`, `.log`) and figures (`.png`) are kept in `~/.colab-claude-mcp/matlab/runs/`;
the host logs to `~/.colab-claude-mcp/matlab/host.log`.

### Limits

- MATLAB runs one thing at a time. Runs from two Claude sessions queue in MATLAB.
- MATLAB hands printed output to the engine when a run ends, so a run that is still going shows no
  partial output. For progress on long runs, write to a file.
- `code` is run with `eval`: several lines are fine, local functions are not. Use `script` for those.
- Runs use a MATLAB diary for their log, which replaces a diary you had on in the same session.
- MATLAB in the cloud is not supported: MATLAB Online has no API for this, and installing MATLAB in
  Colab needs a license that can be used there.

## Security

- The hub listens on localhost only. The websocket accepts only Colab origins and requires the token.
- The local API requires the token (`~/.colab-claude-mcp/token`, readable only by you).
- The mirror page needs no token but answers only requests addressed to `localhost`, so other websites
  cannot read it. Other programs running as you on this machine can.

## Development

```bash
uv run --group dev pytest        # tests use a simulated Colab page; no browser needed
uv run --group dev ruff check .
uv run colab-claude-mcp hub      # run the hub in the foreground
```

## License

Apache 2.0, as upstream. Original work © Google Inc.; see the header of each file for what changed.
This project is not affiliated with or endorsed by Google.
