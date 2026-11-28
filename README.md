# colab-claude-mcp

An MCP server that lets Claude drive a Google Colab notebook open in your browser, and a JupyterLab
notebook (for example on an HPC, through an SSH tunnel) through the JupyterLab tab you are logged in to.
It is a fork of Google's [colab-mcp](https://github.com/googlecolab/colab-mcp), reworked for
Claude Code and the Claude desktop app.

中文简介：这是 Google 官方 colab-mcp 的一个分支，为 Claude 重构。连接由一个常驻的本地进程保持，
所以换会话时不会重新打开 Colab 标签页；它还提供一个实时镜像页面，可以在 Claude 内置浏览器里
查看 notebook 的单元格、运行状态和输出。同样的方式也能驱动 JupyterLab（例如经 SSH 隧道访问的 HPC）：
在已登录的 JupyterLab 标签页里点一下书签，Claude 就通过这个标签页工作，不需要 Jupyter 密码。

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

## JupyterLab (for example on an HPC)

The same tools drive a notebook on a Jupyter server, such as JupyterLab on an HPC node that you reach
through an SSH tunnel (`ssh -N -L 8899:gpu12:8899 you@hpc` and `http://localhost:8899/lab`) and log in to
with a password. Claude never gets the password: it works through the JupyterLab tab you are logged in to,
the way it works through the Colab tab.

中文：用同一套工具驱动 Jupyter 服务器上的 notebook，例如经 SSH 隧道访问、用密码登录的 HPC 上的 JupyterLab。
Claude 拿不到也不需要密码：它通过你已登录的 JupyterLab 标签页工作，就像通过 Colab 标签页一样。
notebook 每次改动后都保存在服务器上；在 Claude 内置浏览器里打开 `http://localhost:47822/jupyter/` 即可实时查看。

### Use

1. Once: open `http://localhost:47822/jupyter/setup` (the hub must be running: any colab-claude-mcp tool
   starts it) and drag the *Claude ⇄ Jupyter* button to your browser's bookmarks bar.
2. Each time: open JupyterLab, log in as usual, and click the bookmark in that tab. A small
   *Claude ⇄ Jupyter: Claude connected* badge appears in its bottom right corner.
3. Ask Claude to work in JupyterLab. It calls `jupyter_connect`, which opens a notebook on the server
   (`claude/scratch.ipynb` unless it names one) and starts or reuses that notebook's kernel.
4. Watch on the Jupyter mirror, `http://localhost:47822/jupyter/`, in the built-in browser.

| Tool | What it does |
|---|---|
| `jupyter_connect` | Wait for a connected JupyterLab tab, open or create `notebook_path`, and start or reuse its kernel (`kernel` picks one of the server's kernels for a new notebook) |
| `jupyter_status` | Tab, notebook, kernel state, the server's kernels, and the cells with their run status |
| `jupyter_kernel` | `interrupt` the running cell, `restart` the kernel, or `shutdown` it to free memory and GPUs on the server |
| cell tools | `get_cells`, `add_code_cell`, `add_text_cell`, `update_cell`, `run_code_cell`, `wait_for_cell`, `delete_cell` and `move_cell` act on the notebook this Claude session connected to last; `backend="colab"` or `"jupyter"` picks one |

- The notebook is saved on the server after every change and every finished run, and every minute while a
  cell runs, so nothing needs saving at the end and the outputs stay on the HPC.
- Cell output streams in while a cell runs: a run that outlasts `wait_seconds` returns with its output so far.
  Progress bars (tqdm, Keras) keep only their latest state, as in JupyterLab.
- The kernel runs on the server. It keeps running when the tab closes, the hub restarts or the Claude session
  ends; the next `jupyter_connect` to the same notebook reuses it with its variables. Output that the kernel
  sends while no tab is connected is replayed by Jupyter when the tab comes back.
- If the notebook is also open in JupyterLab, both share the kernel, but that tab shows Claude's edits only
  after reloading from disk. When JupyterLab says the file changed on disk, choose **Revert** (load Claude's
  version), not Overwrite. Claude takes in what you saved from JupyterLab before its next change.
- After you reload the JupyterLab tab, click the bookmark again. If you click it in a second tab, that tab
  takes over. If the hub restarts, the tab reconnects by itself.

### How it works

```
Claude Code session ── stdio ──> colab-claude-mcp ── HTTP ──> hub (127.0.0.1:47822)
                                                                │ websocket /jupyter/ws (token, localhost origins only)
                                                                ▼
                                JupyterLab tab (logged in) running jupyter_bridge.js
                                                                │ the tab's own cookies and XSRF token
                                                                ▼
                                Jupyter server (localhost:8899 ── SSH tunnel ──> HPC node)
```

The bookmark loads `jupyter_bridge.js` from the hub into the JupyterLab page. The script relays the hub's REST
requests (contents, sessions, kernels) and kernel websockets through the page's own session. The notebook
logic is in the hub (`jupyter.py`): it reads and writes the `.ipynb` through the contents API and runs cells
with the Jupyter messaging protocol over the kernel's websocket.

| Variable | Default | Meaning |
|---|---|---|
| `COLAB_CLAUDE_JUPYTER_NOTEBOOK` | `claude/scratch.ipynb` | Notebook `jupyter_connect` opens when none is named, relative to JupyterLab's root folder |
| `COLAB_CLAUDE_JUPYTER_ORIGINS` | none | Extra page origins allowed to connect, comma-separated. JupyterLab on `localhost` / `127.0.0.1` is always allowed |

### Limits

- The JupyterLab tab has to stay open, as the Colab tab does; a reload disconnects it until you click the
  bookmark again.
- Without JupyterLab's real-time collaboration, an open JupyterLab tab does not update live (see above).
- `input()` is not supported, and widget binary buffers are not relayed (widgets still show their text form).
- JupyterLab served over `https` cannot load the bridge from the hub's plain `http` address.

## MATLAB (local)

The same server can also run MATLAB code in a MATLAB installed on this computer, with all your
toolboxes, for example finite element models with the PDE Toolbox.

中文：同一个 MCP 也能驱动本机 MATLAB（含全部工具箱，例如用 PDE Toolbox 做有限元仿真）。MATLAB 会话常驻，
换 Claude 会话后工作区变量还在；图会以图片返回并保存为 PNG。每次运行的代码、输出、报错和图都显示在输出页面
`http://localhost:47822/matlab` 上，在 Claude 内置浏览器里打开它，MATLAB 静默运行时它就是输出窗口。
新图默认使用 IEEE Transactions 风格：单栏 3.5 in，Times New Roman 9 pt（坐标轴标签 10 pt），线型与颜色同时区分，
灰度打印也能分清；macOS 处于深色模式时也保持浅色主题（只在本次 MATLAB 会话有效，不保存设置）；`ccmcp.style('matlab')` 可恢复 MATLAB 默认风格。投稿用图可加 `pdf=true`，同时导出嵌入字体的矢量 PDF。
默认 `COLAB_CLAUDE_ACCEL=auto`：任务允许时，Claude 会主动用并行池（`ccmcp.pool`，默认进程池，用满全部核心）或 GPU 计算，
Colab 里则用运行时自带的 GPU/TPU，并在回复里说明用了什么；设为 `off` 则一律串行、只用 CPU，除非你明确要求。
MATLAB 只支持 NVIDIA GPU，在 Apple 芯片的 Mac 上用不了 GPU（不支持 Metal/MPS）。

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
| `matlab_connect` | Start or connect to the MATLAB session. A new session is headless unless `desktop=true`, which opens the MATLAB desktop so you can watch; without `desktop` a running session is used as it is; `session` connects to a session you shared yourself |
| `matlab_eval` | Run `code` or a `.m` `script` in the base workspace. Returns printed output and attaches the figures the run made; returns early after `wait_seconds`; `pdf=true` also saves each figure as a vector PDF |
| `matlab_wait` | Wait for a run that is still going |
| `matlab_interrupt` | Interrupt the run (like Ctrl+C); the workspace is kept |
| `matlab_status` | Session, mode, current run, working folder, open figures and `whos` |
| `matlab_stop` | Quit the session (the workspace is lost) |

### Optional `matlab` skill

The server tells Claude to load a Claude Code skill named `matlab`, if one is available, before
MATLAB or Simulink work. That skill bundles MathWorks' MATLAB Agentic Toolkit and Simulink Agentic
Toolkit guidance (PDE Toolbox, parallel computing, Simulink and Simscape modeling, testing, code
review, documentation lookup matched to your release, and the other toolboxes) and adapts it to
these tools: for example `matlab_eval` instead of MathWorks' `evaluate_matlab_code`, and leaving the
shared base workspace intact. It is installed separately as a user skill in `~/.claude/skills/matlab/`
and is not shipped in this repository, since it contains MathWorks content. The server only
recommends it; without it the MATLAB tools work the same.

### Output page

`http://localhost:47822/matlab` shows the MATLAB runs like a notebook, newest last: each run's code
(or script), printed output, warnings, errors and figures, with its status and run time. Ask Claude to
open it in the built-in browser; with MATLAB headless, it is the output window. It follows the newest
run unless you untick *Follow newest run*, and links to the Colab mirror (which links back).

- Figures saved as PDF (`pdf=true`) get a *Download vector PDF* link under them.
- A run shows as running, with a timer, until it ends; then its output and figures appear. MATLAB hands
  printed output over only when a run ends, so there is no partial output.
- It is served by the hub, next to the Colab mirror. The MATLAB tools start the hub when needed, and the
  hub stays up while the page is open.
- The page reads the run records (`<run id>.json`) in `~/.colab-claude-mcp/matlab/runs/`, so runs from
  earlier Claude sessions are listed too (the last 60). If the Claude session that started a run ends
  first, MATLAB still finishes the run and the page completes it from its log, without the error text.
- An upgrade does not replace a hub that has a Colab tab connected. Until that tab is closed, an older
  hub without this page may keep running; `matlab_connect` says so.

### Plot style

New figures in the MATLAB session these tools start follow IEEE's graphics guidelines for journal
articles (IEEE Author Center: one column 3.5 in, two columns 7.16 in; type of about 9–10 pt at full
size; readable from a grayscale printout; at least 300 dpi for color and grayscale images):

| | |
|---|---|
| Size | 3.5 × 2.6 in, one column. Two columns: `figure('Units','inches','Position',[1 1 7.16 3])` |
| Type | Times New Roman 9 pt; axis labels and titles 10 pt, normal weight |
| Lines | 1.25 pt and opaque; axes 0.5 pt, boxed, ticks inside; grid lines (when on) 15 % black |
| Telling lines apart | color and line style change together: black solid, blue dashed, red dash-dot, wine dotted, olive solid, pink dashed, … |
| Colors | black, then `#0072B2 #CC3311 #882255 #999933 #CC79A7`: checked for color-vision deficiency (worst pair ΔE 9.6, OKLab ×100) and ≥ 3:1 contrast on white. Colors that look alike in grayscale (blue/red, olive/pink) never share a line style |
| PNG export | 300 dpi; figures wider than about 8 in get less, to keep the PNG under about 2400 px |
| Theme | light, also while macOS is in dark mode (MATLAB R2025a+ would give new figures its dark theme, on which the black text disappears). It is the session-only value of MATLAB's figure-theme setting, so nothing is saved; `ccmcp.style('matlab')` hands the theme back to the system |

- `ccmcp.style('matlab')` switches the session to MATLAB's defaults and `ccmcp.style('ieee')` back. The
  choice holds for the session, also when later Claude sessions reconnect. Set
  `COLAB_CLAUDE_MATLAB_STYLE=matlab` to start new sessions with MATLAB's defaults.
- Figures that are already open keep their look, and code that sets its own fonts, sizes or colors
  overrides the style.
- `pdeplot` applies its own `cool` colormap, which prints as an almost flat gray; pass
  `'ColorMap','parula'`.
- For papers, `pdf=true` also saves each figure as a vector PDF next to its PNG (`exportgraphics`
  with `'ContentType','vector'`, so fonts are embedded and even `pdeplot` fields stay vector; MATLAB's
  automatic choice would turn those into one raster image). The result lists each PDF with its size.
  Exports are cropped to their content, so a one-column figure is about 3.1 in wide: place it at its
  natural size to keep 9/10 pt type, or at `\columnwidth` to enlarge it about 12 %.
- In desktop mode, figures open as separate windows at their true size instead of docked tabs.
- A MATLAB desktop you shared yourself is not restyled; run `ccmcp.style('ieee')` in it for the style.

### Parallel and GPU computing

With `COLAB_CLAUDE_ACCEL=auto` (the default) the server tells Claude to use parallel and accelerated
computing when the task allows, without being asked, and to say in its reply what ran in parallel
or on which device. `COLAB_CLAUDE_ACCEL=off` keeps runs serial and on the CPU unless you ask.

MATLAB:

- `matlab_connect` and `matlab_status` report the cores, the Parallel Computing Toolbox, the open
  pool and the GPU; `ccmcp.compute` prints the same, `info = ccmcp.compute` returns it as a struct.
- `ccmcp.pool` reuses the open process pool or starts one (all cores; 15–20 s to start; it closes
  after 30 idle minutes, and later runs and Claude sessions reuse it). `ccmcp.pool('processes', n)`
  sets the size, `ccmcp.pool('threads')` starts a thread pool in about a second, and
  `ccmcp.pool('off')` shuts the pool down. Thread workers refuse many toolbox functions, PDE Toolbox
  geometry and meshing among them, so the default is a process pool.
- Claude vectorizes first (built-in functions already multithread on all cores) and uses `parfor` /
  `parfeval` for independent pieces (sweeps, Monte Carlo, many PDE solves) that would take more than
  about a minute serially. It seeds inside the loop and checks a small case against a serial run.
- `gpuArray` is used only when `ccmcp.compute` reports a GPU. MATLAB computes on NVIDIA GPUs only,
  so on an Apple-silicon Mac there is none: MATLAB has no Metal / MPS support.

Colab: Claude checks the runtime (`torch.cuda.is_available()`, `jax.devices()`, `!nvidia-smi`)
before heavy numeric work and puts it on the GPU or TPU, with a CPU fallback. The runtime type stays
your choice in the Colab tab; when a task would clearly gain from a GPU and the runtime has none,
Claude says so. Apple MPS does not apply there either, since Colab runs on Google's machines.

### How it works

MATLAB started with `-batch` cannot be found by the engine, and with `-nodesktop` it exits when its
stdin closes. So `colab-claude-mcp matlab-host` (started for you, detached) starts MATLAB through the
engine, shares it as `colab_claude_mcp`, and waits until MATLAB exits. Each Claude session connects to
that shared session by name, so variables, meshes and results survive between sessions, the way the
Colab tab survives through the hub.

Each run marks the figures that already exist, runs the code, then exports the new figures with
`exportgraphics` (helpers in `src/colab_claude_mcp/mfiles/+ccmcp`). In headless mode figures are
invisible and closed after export; in desktop mode they stay open.

If MATLAB is killed (for example together with the Claude app that started it), it leaves its
socket `/tmp/colab_claude_mcp` behind. `matlab.engine.find_matlab()` then no longer lists the
session, but MATLAB refuses to share a new one under that name ("MATLAB session 'colab_claude_mcp'
already exists"). So the server checks for such leftovers when it starts and again just before it
starts MATLAB:

- A socket is removed if it is at least 10 s old, the MATLAB recorded for it is gone, and no
  process has it open (checked with `lsof`, or `/proc/net/unix` on Linux).
- `matlab/session.json` is removed if the MATLAB pid recorded there is gone or now belongs to another
  program.
- What was removed is reported in `matlab_connect`'s reply and in `host.log`.
- If MATLAB still cannot be shared, `matlab_connect` returns the host's error right away instead of
  waiting out `wait_seconds`.

The Colab hub needs no such check: it is found through its health check, keeps no pid or lock
files, and the system frees its ports when it is killed. A killed hub does drop the Colab tab,
though.

To let Claude work in a MATLAB desktop you opened yourself, run
`matlab.engine.shareEngine('colab_claude_mcp')` in it (or any name, then `matlab_connect(session=...)`).

| Variable | Default | Meaning |
|---|---|---|
| `COLAB_CLAUDE_MATLAB_SESSION` | `colab_claude_mcp` | Name of the shared MATLAB session |
| `COLAB_CLAUDE_MATLAB_OPTIONS` | none | Extra MATLAB startup options, such as `-sd /path/to/project` |
| `COLAB_CLAUDE_MATLAB_STYLE` | `ieee` | Look of new figures: `ieee` (IEEE Transactions) or `matlab` (MATLAB's defaults) |
| `COLAB_CLAUDE_ACCEL` | `auto` | `auto`: Claude uses a MATLAB parallel pool, a GPU or a Colab GPU/TPU when the task allows; `off`: serial and on the CPU unless you ask |

Run code, logs (`.m`, `.log`) and figures (`.png`) are kept in `~/.colab-claude-mcp/matlab/runs/`;
the host logs to `~/.colab-claude-mcp/matlab/host.log`.

### Limits

- MATLAB runs one thing at a time. Runs from two Claude sessions queue in MATLAB.
- In desktop mode you see the figures (docked in the Figures window, left open), the workspace and the
  current folder change, but not the commands Claude runs or their printed output: the engine sends
  those back to Claude, not to the Command Window.
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
- JupyterLab: the bridge script carries the token, so the hub serves it only to pages on this machine
  (a browser marks a script that another site's page loads as cross-site, and that is refused). The tab's
  websocket must come from a localhost origin (or one in `COLAB_CLAUDE_JUPYTER_ORIGINS`) with the token.
  Your Jupyter password and login cookie never leave the browser; the bookmark holds no secret.

## Development

```bash
uv run --group dev pytest        # tests use a simulated Colab page and JupyterLab tab; no browser needed
uv run --group dev ruff check .
uv run colab-claude-mcp hub      # run the hub in the foreground
```

## License

Apache 2.0, as upstream. Original work © Google Inc.; see the header of each file for what changed.
This project is not affiliated with or endorsed by Google.
