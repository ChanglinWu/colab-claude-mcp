// colab-claude-mcp: the JupyterLab bridge.
//
// The "Claude ⇄ Jupyter" bookmarklet loads this script into a JupyterLab tab the user is logged in
// to. It connects to the colab-claude-mcp hub on this computer and relays the hub's requests to the
// Jupyter server through the tab's own session (its cookies and XSRF token), so Claude never needs
// the Jupyter password. It keeps no state of its own: the hub does the notebook work.
//
// Messages from the hub: http (a REST request), ws_open / ws_send / ws_close (a kernel websocket),
// welcome, reload, error. Messages to the hub: hello, reply, ws_msg, ws_closed.
(() => {
  "use strict";
  const HUB_PORT = __CCMCP_PORT__;
  const TOKEN = __CCMCP_TOKEN__;
  const VERSION = __CCMCP_VERSION__;
  const KEY = "__colabClaudeJupyterBridge";
  const hubBase = "http://" + location.hostname + ":" + HUB_PORT;

  const previous = window[KEY];
  if (previous && previous.version === VERSION) {
    previous.reconnect();
    return;
  }
  if (previous) previous.stop();

  // ---- badge ----------------------------------------------------------------------------
  const badge = document.createElement("div");
  badge.setAttribute("role", "status");
  badge.setAttribute("aria-live", "polite");
  badge.style.cssText =
    "position:fixed;right:14px;bottom:34px;z-index:2147483647;display:flex;align-items:center;gap:7px;" +
    "padding:4px 6px 4px 10px;border-radius:999px;background:rgba(32,33,36,.9);color:#fff;" +
    "font:12px/1.4 system-ui,-apple-system,'Segoe UI',sans-serif;box-shadow:0 1px 4px rgba(0,0,0,.3);" +
    "max-width:min(420px,calc(100vw - 28px))";
  const dot = document.createElement("span");
  dot.style.cssText = "width:8px;height:8px;border-radius:50%;flex:none;background:#f0a04b";
  const label = document.createElement("span");
  label.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
  const hide = document.createElement("button");
  hide.type = "button";
  hide.textContent = "×";
  hide.title = "Hide (the connection stays)";
  hide.setAttribute("aria-label", "Hide the Claude badge; the connection stays");
  hide.style.cssText =
    "border:0;background:transparent;color:#bbb;font:15px/1 system-ui,sans-serif;cursor:pointer;padding:0 4px";
  hide.addEventListener("click", () => { badge.style.display = "none"; });
  badge.append(dot, label, hide);
  (document.body || document.documentElement).appendChild(badge);
  const COLORS = { on: "#6cc08b", wait: "#f0a04b", off: "#f2867f" };
  function setBadge(state, text) {
    dot.style.background = COLORS[state] || COLORS.wait;
    label.textContent = "Claude ⇄ Jupyter: " + text;
    badge.title = label.textContent;
    if (state !== "on") badge.style.display = "flex";
  }

  // ---- the Jupyter page -----------------------------------------------------------------
  const configElement = document.getElementById("jupyter-config-data");
  if (!configElement) {
    setBadge("off", "this is not a JupyterLab page. Click the bookmark in your JupyterLab tab.");
    return;
  }
  // The page config also holds server settings (and can hold a token). Only these fields are used.
  let pageInfo = { baseUrl: "/", appName: "", appVersion: "", treePath: "" };
  try {
    const page = JSON.parse(configElement.textContent);
    pageInfo = {
      baseUrl: String(page.baseUrl || "/"),
      appName: String(page.appName || ""),
      appVersion: String(page.appVersion || ""),
      treePath: String(page.treePath || ""),
    };
  } catch (error) { /* keep the defaults */ }
  const base = new URL(pageInfo.baseUrl, location.origin);

  function xsrf() {
    const match = document.cookie.match(/(?:^|;\s*)_xsrf=([^;]*)/);
    return match ? match[1] : "";
  }

  // ---- relaying -------------------------------------------------------------------------
  const channels = new Map();
  let hub = null;
  let retry = 0;
  let timer = 0;
  let stopped = false;

  function send(message) {
    if (hub && hub.readyState === WebSocket.OPEN) hub.send(JSON.stringify(message));
  }

  async function relayHttp(message) {
    const headers = { "Content-Type": "application/json" };
    const token = xsrf();
    if (token) headers["X-XSRFToken"] = token;
    try {
      const response = await fetch(new URL(message.path, base), {
        method: message.method || "GET",
        headers,
        body: message.body == null ? undefined : message.body,
        credentials: "same-origin",
        cache: "no-store",
        redirect: "manual",
      });
      const redirected = response.type === "opaqueredirect"; // usually to the login page
      send({ type: "reply", id: message.id, status: redirected ? 302 : response.status,
             text: redirected ? "" : await response.text() });
    } catch (error) {
      send({ type: "reply", id: message.id, status: 0, error: String(error) });
    }
  }

  function openChannel(message) {
    const url = new URL(message.path, base);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    let opened = false;
    let socket;
    try {
      socket = new WebSocket(url);
    } catch (error) {
      send({ type: "reply", id: message.id, ok: false, error: String(error) });
      return;
    }
    socket.binaryType = "arraybuffer";
    channels.set(message.chan, socket);
    socket.onopen = () => {
      opened = true;
      send({ type: "reply", id: message.id, ok: true });
    };
    socket.onmessage = (event) => {
      // Binary frames carry widget buffers, which Claude does not use.
      if (typeof event.data === "string") send({ type: "ws_msg", chan: message.chan, data: event.data });
    };
    socket.onclose = (event) => {
      if (channels.get(message.chan) === socket) channels.delete(message.chan);
      if (!opened) send({ type: "reply", id: message.id, ok: false, error: "closed with code " + event.code });
      else send({ type: "ws_closed", chan: message.chan, code: event.code, reason: event.reason });
    };
  }

  function closeChannels() {
    for (const socket of channels.values()) {
      try { socket.close(); } catch (error) { /* already closed */ }
    }
    channels.clear();
  }

  function onHubMessage(event) {
    let message;
    try { message = JSON.parse(event.data); } catch (error) { return; }
    switch (message.type) {
      case "http": relayHttp(message); break;
      case "ws_open": openChannel(message); break;
      case "ws_send": {
        const socket = channels.get(message.chan);
        if (socket && socket.readyState === WebSocket.OPEN) socket.send(message.data);
        break;
      }
      case "ws_close": {
        const socket = channels.get(message.chan);
        channels.delete(message.chan);
        if (socket) socket.close();
        break;
      }
      case "welcome": setBadge("on", message.text || "Claude connected"); break;
      case "reload": reload(); break;
      case "error": stopped = true; setBadge("off", message.text || "refused by Claude's hub"); break;
      default: break;
    }
  }

  function connect() {
    clearTimeout(timer);
    if (stopped || hub) return;
    let socket;
    try {
      socket = new WebSocket(hubBase.replace(/^http/, "ws") + "/jupyter/ws?token=" + encodeURIComponent(TOKEN));
    } catch (error) {
      schedule();
      return;
    }
    hub = socket;
    socket.onopen = () => {
      retry = 0;
      setBadge("wait", "connecting…");
      send({
        type: "hello", origin: location.origin, href: location.href, base_url: base.pathname,
        app_name: pageInfo.appName, app_version: pageInfo.appVersion, tree_path: pageInfo.treePath,
        bridge: VERSION,
      });
    };
    socket.onmessage = onHubMessage;
    socket.onclose = (event) => {
      if (hub === socket) hub = null;
      closeChannels();
      if (event.code === 4000) {
        stopped = true;
        setBadge("off", "another JupyterLab tab took over. Click the bookmark to use this tab again.");
        return;
      }
      if (stopped) return;
      setBadge("wait", "Claude's hub is not running; retrying…");
      schedule();
    };
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(connect, Math.min(30000, 1000 * 2 ** Math.min(retry++, 5)));
  }

  function stop() {
    stopped = true;
    clearTimeout(timer);
    const socket = hub;
    hub = null;
    if (socket) {
      try { socket.close(1000, "stopped"); } catch (error) { /* already closed */ }
    }
    closeChannels();
    badge.remove();
  }

  function reload() {
    // The hub was upgraded: load its current bridge in place of this one.
    stop();
    delete window[KEY];
    const script = document.createElement("script");
    script.src = hubBase + "/jupyter/bridge.js?" + Date.now();
    document.head.appendChild(script);
  }

  window[KEY] = {
    version: VERSION,
    stop,
    reconnect() {
      stopped = false;
      retry = 0;
      badge.style.display = "flex";
      if (hub) setBadge("on", "Claude connected");
      else connect();
    },
  };
  setBadge("wait", "connecting…");
  connect();
})();
