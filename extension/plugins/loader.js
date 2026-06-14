// chrome-dumper plugin SDK — loader + registry.
//
// Imported by background.js during the service worker's synchronous startup
// (mirrors the debugger.js pattern: importScripts at top level, register onto
// `self`, reach `reply`/`getTargetTab`/`chrome` as worker globals). It provides:
//
//   • self.pluginRegistry  — plugins self-register command handlers here
//   • self.makePluginCtx() — helper bundle handed to each command handler
//   • self.mountPluginPanel / unmountPluginPanel — in-page shadow-DOM UI host
//   • a chrome.runtime.onMessage bridge so panel button clicks invoke the
//     plugin command `${plugin}_${action}`
//
// A plugin is a folder under extension/plugins/<id>/ whose commands.js calls
// self.pluginRegistry.register({...}). To add one: create the folder, append an
// importScripts line at the bottom of this file, and reload the extension.
//
// MV3 note: service workers can only importScripts files packaged in the
// extension, and only during synchronous startup — hence the static list at the
// bottom rather than filesystem discovery. "Registration" happens at SW boot.

(function () {
  const commands = new Map(); // type -> { plugin, handler }
  const panels = new Map();   // pluginId -> panel def
  const metas = [];           // [{ id, name, match, commands:[types] }]

  function globToRegExp(glob) {
    const esc = glob
      .replace(/[.+^${}()|[\]\\]/g, "\\$&")
      .replace(/\*/g, ".*")
      .replace(/\?/g, ".");
    return new RegExp("^" + esc + "$");
  }
  self.pluginMatches = function (globs, url) {
    return (globs || []).some((g) => {
      try { return globToRegExp(g).test(url || ""); } catch { return false; }
    });
  };

  self.pluginRegistry = {
    register(def) {
      if (!def || !def.id) throw new Error("plugin.register: missing id");
      const types = [];
      if (def.commands) {
        for (const [type, handler] of Object.entries(def.commands)) {
          if (commands.has(type)) {
            console.warn(`[plugin] ${def.id}: command "${type}" overrides an existing one`);
          }
          commands.set(type, { plugin: def.id, handler });
          types.push(type);
        }
      }
      if (def.panel) panels.set(def.id, { plugin: def.id, ...def.panel });
      metas.push({ id: def.id, name: def.name || def.id, match: def.match || [], commands: types });
      console.log(`[plugin] registered "${def.id}" — ${types.length} command(s)`);
    },
    has(type) { return commands.has(type); },
    async run(msg, ctx) {
      const entry = commands.get(msg.type);
      if (!entry) throw new Error("unknown plugin command: " + msg.type);
      return entry.handler(msg, ctx);
    },
    list() { return metas.map((m) => ({ ...m })); },
    panelFor(id) { return panels.get(id); },
  };

  // Helpers handed to every command handler. Resolved lazily so the background
  // globals (getTargetTab, waitForComplete) are defined by call time.
  self.makePluginCtx = function () {
    return {
      chrome,
      getTargetTab: (msg) => getTargetTab(msg),
      waitForComplete: (id, ms) => waitForComplete(id, ms),
      exec: (opts) => chrome.scripting.executeScript(opts),
      registry: self.pluginRegistry,
      log: (...a) => console.log("[plugin]", ...a),
    };
  };

  // --- in-page panel host ---------------------------------------------------
  // Injects a shadow-DOM overlay (ISOLATED world, so chrome.runtime is
  // available) carrying the plugin's HTML. Clicks on [data-plugin-action]
  // elements message the SW, which runs `${plugin}_${action}` and returns a
  // result the panel writes into [data-plugin-status].
  self.mountPluginPanel = async function (tabId, def) {
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target: { tabId },
      args: [def.plugin, def.panelId, def.html || "", def.css || ""],
      func: (plugin, panelId, html, css) => {
        const prev = document.getElementById(panelId);
        if (prev) prev.remove();
        const host = document.createElement("div");
        host.id = panelId;
        host.style.cssText = "position:fixed;top:84px;right:24px;z-index:2147483647;";
        const shadow = host.attachShadow({ mode: "open" });
        const style = document.createElement("style");
        style.textContent = css;
        const wrap = document.createElement("div");
        wrap.innerHTML = html;
        shadow.append(style, wrap);
        (document.body || document.documentElement).appendChild(host);
        shadow.addEventListener("click", (e) => {
          const t = e.target.closest("[data-plugin-action]");
          if (!t) return;
          const action = t.getAttribute("data-plugin-action");
          const statusEl = shadow.querySelector("[data-plugin-status]");
          if (statusEl) statusEl.textContent = "working…";
          try {
            chrome.runtime.sendMessage({ type: "__plugin_action", plugin, action }, (resp) => {
              if (chrome.runtime.lastError) {
                if (statusEl) statusEl.textContent = "error: " + chrome.runtime.lastError.message;
                return;
              }
              if (statusEl) {
                statusEl.textContent = resp && resp.ok
                  ? (resp.message || "done")
                  : ("error: " + ((resp && resp.error) || "failed"));
              }
            });
          } catch (err) {
            if (statusEl) statusEl.textContent = "error: " + String(err);
          }
        });
        return { ok: true };
      },
    });
    return result || { ok: true };
  };

  self.unmountPluginPanel = async function (tabId, panelId) {
    await chrome.scripting.executeScript({
      target: { tabId },
      args: [panelId],
      func: (id) => { const e = document.getElementById(id); if (e) e.remove(); return { ok: true }; },
    });
    return { ok: true };
  };

  // Panel button clicks. Two destinations:
  //   • a registered command `${plugin}_${action}` exists → run it in the SW
  //     (extension-handled: pure browser actions like closing the panel).
  //   • otherwise → hand off to the client over the bridge's event rail as a
  //     `plugin_event` (client-handled: anything needing local data/filesystem).
  //     The client does the work and calls back with its own commands.
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || msg.type !== "__plugin_action") return;
    const tabId = sender.tab && sender.tab.id;
    const cmdType = `${msg.plugin}_${msg.action}`;
    (async () => {
      try {
        if (self.pluginRegistry.has(cmdType)) {
          const r = await self.pluginRegistry.run(
            { type: cmdType, tabId, ...(msg.payload || {}) },
            self.makePluginCtx()
          );
          sendResponse({ ok: !(r && r.ok === false), ...(r || {}) });
        } else {
          if (typeof reply === "function") {
            reply({ type: "plugin_event", plugin: msg.plugin, action: msg.action, tabId, payload: msg.payload || null });
          }
          sendResponse({ ok: true, message: "handed to client…" });
        }
      } catch (e) {
        sendResponse({ ok: false, error: String((e && e.message) || e) });
      }
    })();
    return true; // keep the channel open for the async sendResponse
  });

  // Introspection command so clients can see what's loaded: POST {type:"list_plugins"}.
  self.pluginRegistry.register({
    id: "core",
    name: "plugin core",
    commands: {
      list_plugins: async () => ({ type: "plugins", ok: true, plugins: self.pluginRegistry.list() }),
    },
  });

  // --- bundled plugins (static list — MV3 can't discover at runtime) ---------
  try { importScripts("plugins/messaging/commands.js"); }
  catch (e) { console.error("[plugin] messaging load failed:", e); }
})();
