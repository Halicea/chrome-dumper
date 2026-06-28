// Chrome Debugger Protocol (CDP) module.
//
// Adds command types: debug_attach, debug_detach, debug_status,
//                     debug_get_body, debug_pause_continue
//
// When attached, forwards CDP events to the bridge as {type:"debug_event", ...}
// messages over the existing WebSocket (no id — they are unsolicited).
//
// The bridge fans these out to SSE subscribers on /events?tab=N.

const CDP_VERSION = "1.3";
const attached = new Map(); // tabId -> { network: bool, fetch: bool, console: bool, paused: Map<requestId, {url, method, resourceType}> }
const lastMouse = new Map(); // tabId -> { x, y }  last known cursor position (CSS px, viewport-relative)
let _listenerInstalled = false;

// CDP modifier bitmask: Alt=1, Ctrl=2, Meta/Cmd=4, Shift=8.
function _modifiers(msg) {
  let m = 0;
  if (msg.alt) m |= 1;
  if (msg.ctrl) m |= 2;
  if (msg.meta) m |= 4;
  if (msg.shift) m |= 8;
  return m;
}

// CDP `buttons` bitmask for a held button: left=1, right=2, middle=4.
function _buttonsMask(button) {
  return button === "right" ? 2 : button === "middle" ? 4 : 1;
}

// Draw a visible cursor overlay at (x, y) in the page so a human watching can
// see where the synthetic CDP cursor is (CDP input moves a *virtual* pointer
// that the browser doesn't render). On `clicked`, add a quick expanding ripple.
// Best-effort — never throws, never blocks the actual input.
function _cursorExpr(x, y, clicked) {
  const cl = clicked ? "true" : "false";
  return "(() => { const ID='__dumper_cursor__';" +
    " let c=document.getElementById(ID);" +
    " if(!c){ c=document.createElement('div'); c.id=ID;" +
    "  c.style.cssText='position:fixed;top:0;left:0;width:18px;height:18px;margin:-9px 0 0 -9px;" +
    "border:2px solid #00e5ff;border-radius:50%;box-shadow:0 0 0 1px rgba(0,0,0,.45),0 0 6px rgba(0,229,255,.85);" +
    "background:rgba(0,229,255,.15);pointer-events:none;z-index:2147483647;transition:transform .04s linear;will-change:transform;';" +
    "  const d=document.createElement('div');" +
    "  d.style.cssText='position:absolute;top:50%;left:50%;width:3px;height:3px;margin:-1.5px 0 0 -1.5px;background:#00e5ff;border-radius:50%;';" +
    "  c.appendChild(d); (document.body||document.documentElement).appendChild(c); }" +
    " c.style.transform='translate(' + (" + x + ") + 'px,' + (" + y + ") + 'px)';" +
    " if(" + cl + "){ const r=document.createElement('div');" +
    "  r.style.cssText='position:fixed;top:0;left:0;width:14px;height:14px;margin:-7px 0 0 -7px;" +
    "border:2px solid #ff1744;border-radius:50%;pointer-events:none;z-index:2147483647;opacity:.9;" +
    "transition:transform .35s ease-out,opacity .35s ease-out;';" +
    "  r.style.transform='translate(' + (" + x + ") + 'px,' + (" + y + ") + 'px) scale(1)';" +
    "  (document.body||document.documentElement).appendChild(r);" +
    "  requestAnimationFrame(()=>{ r.style.transform='translate(' + (" + x + ") + 'px,' + (" + y + ") + 'px) scale(3)'; r.style.opacity='0'; });" +
    "  setTimeout(()=>r.remove(),420); } })()";
}

async function _drawCursor(tabId, x, y, clicked) {
  try {
    await _sendCommand(tabId, "Runtime.evaluate", { expression: _cursorExpr(x, y, clicked) });
  } catch (e) { /* overlay is cosmetic; ignore failures */ }
}

function _sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// Ease-in-out: slow start, faster middle, slow finish — the "decelerate into the
// target" feel of a real cursor.
function _easeInOutCubic(t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; }

// Glide the cursor from (x0,y0) to (x1,y1) along an eased path with a slight
// bow (quadratic Bézier), dispatching many mouseMoved events with small delays
// so it looks like a human hand, not a teleport. Steps/duration scale with
// distance. Updates the visible cursor as it goes.
async function _smoothMoveTo(tabId, x0, y0, x1, y1, opts) {
  opts = opts || {};
  const dist = Math.hypot(x1 - x0, y1 - y0);
  if (dist < 2) { // already there
    await _sendCommand(tabId, "Input.dispatchMouseEvent", {
      type: "mouseMoved", x: x1, y: y1, button: opts.button || "none", buttons: opts.buttons || 0, modifiers: opts.modifiers || 0,
    });
    if (opts.drawCursor !== false) await _drawCursor(tabId, x1, y1, false);
    return;
  }
  const steps = opts.steps != null ? opts.steps : Math.max(10, Math.min(45, Math.round(dist / 11)));
  const durationMs = opts.durationMs != null ? opts.durationMs : Math.max(140, Math.min(600, dist * 1.15));
  const stepDelay = durationMs / steps;
  // Control point bowed perpendicular to the path for a gentle arc.
  const mx = (x0 + x1) / 2, my = (y0 + y1) / 2;
  let nx = -(y1 - y0), ny = (x1 - x0);
  const nl = Math.hypot(nx, ny) || 1; nx /= nl; ny /= nl;
  const arc = Math.min(36, dist * 0.10);
  const cx = mx + nx * arc, cy = my + ny * arc;
  for (let i = 1; i <= steps; i++) {
    const t = _easeInOutCubic(i / steps);
    const u = 1 - t;
    const x = u * u * x0 + 2 * u * t * cx + t * t * x1;
    const y = u * u * y0 + 2 * u * t * cy + t * t * y1;
    await _sendCommand(tabId, "Input.dispatchMouseEvent", {
      type: "mouseMoved", x, y, button: opts.button || "none", buttons: opts.buttons || 0, modifiers: opts.modifiers || 0,
    });
    if (opts.drawCursor !== false) await _drawCursor(tabId, x, y, false);
    if (i < steps) await _sleep(stepDelay);
  }
}

// Report what's under (x, y) so the caller can confirm the click landed where it
// meant to. Uses Runtime.evaluate (no domain enable needed) in the page realm.
async function _elementAt(tabId, x, y) {
  try {
    const expr =
      "(() => { const el = document.elementFromPoint(" + x + ", " + y + ");" +
      " if (!el) return null;" +
      " const cls = typeof el.className === 'string' ? el.className : null;" +
      " return { tag: el.tagName.toLowerCase(), id: el.id || null, cls: cls || null," +
      " href: el.href || null, text: (el.innerText || el.value || '').trim().slice(0, 120) }; })()";
    const r = await _sendCommand(tabId, "Runtime.evaluate", { expression: expr, returnByValue: true });
    return r && r.result ? r.result.value : null;
  } catch (e) {
    return null;
  }
}

function _state(tabId) {
  let s = attached.get(tabId);
  if (!s) { s = { network: false, fetch: false, console: false, paused: new Map() }; attached.set(tabId, s); }
  return s;
}

function _installEventListenerOnce() {
  if (_listenerInstalled) return;
  _listenerInstalled = true;
  chrome.debugger.onEvent.addListener((source, method, params) => {
    if (!source.tabId || !attached.has(source.tabId)) return;
    // Track Fetch.requestPaused so debug_pause_continue knows the requestId is live.
    if (method === "Fetch.requestPaused") {
      const st = _state(source.tabId);
      st.paused.set(params.requestId, {
        url: params.request?.url, method: params.request?.method,
        resourceType: params.resourceType,
      });
    }
    // Forward event to bridge. Unsolicited messages (no id) are routed by
    // bridge.handle_extension into the SSE fan-out.
    if (typeof self.reply === "function") {
      self.reply({
        type: "debug_event",
        tabId: source.tabId,
        method,
        params,
      });
    }
  });
  chrome.debugger.onDetach.addListener((source, reason) => {
    if (!source.tabId) return;
    attached.delete(source.tabId);
    if (typeof self.reply === "function") {
      self.reply({ type: "debug_event", tabId: source.tabId, method: "Inspector.detached", params: { reason } });
    }
  });
}

function _sendCommand(tabId, method, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId }, method, params || {}, (result) => {
      const err = chrome.runtime.lastError;
      if (err) reject(new Error(err.message)); else resolve(result);
    });
  });
}

async function _attach(tabId, { network = true, fetch = false, console = false, fetchPatterns = null } = {}) {
  _installEventListenerOnce();
  const st = _state(tabId);
  // attach is idempotent — debugger.attach throws if already attached
  if (!st._attached) {
    await new Promise((resolve, reject) => {
      chrome.debugger.attach({ tabId }, CDP_VERSION, () => {
        const err = chrome.runtime.lastError;
        if (err && !/already attached/i.test(err.message)) reject(new Error(err.message));
        else resolve();
      });
    });
    st._attached = true;
  }
  if (network && !st.network) {
    await _sendCommand(tabId, "Network.enable", {});
    st.network = true;
  }
  if (console && !st.console) {
    await _sendCommand(tabId, "Runtime.enable", {});
    st.console = true;
  }
  if (fetch && !st.fetch) {
    const patterns = fetchPatterns?.length
      ? { patterns: fetchPatterns.map(p => ({ urlPattern: p, requestStage: "Request" })) }
      : { patterns: [{ urlPattern: "*", requestStage: "Request" }] };
    await _sendCommand(tabId, "Fetch.enable", patterns);
    st.fetch = true;
  }
  return st;
}

async function _detach(tabId) {
  if (!attached.has(tabId)) return;
  await new Promise((resolve) => {
    chrome.debugger.detach({ tabId }, () => { chrome.runtime.lastError; resolve(); });
  });
  attached.delete(tabId);
}

// Handle one debug_* command from the bridge. Returns the reply object
// (without id — caller adds it).
async function handleDebugCommand(msg, getTargetTab) {
  switch (msg.type) {
    case "debug_attach": {
      const tab = await getTargetTab(msg);
      if (!tab) return { type: "error", error: "no_tab" };
      const st = await _attach(tab.id, {
        network: msg.network !== false,
        fetch: !!msg.fetch,
        console: !!msg.console,
        fetchPatterns: msg.fetchPatterns || null,
      });
      return { type: "debug_attached", tabId: tab.id, domains: { network: st.network, fetch: st.fetch, console: st.console } };
    }
    case "debug_detach": {
      const tab = await getTargetTab(msg);
      if (!tab) return { type: "error", error: "no_tab" };
      await _detach(tab.id);
      return { type: "debug_detached", tabId: tab.id };
    }
    case "debug_status": {
      const list = [];
      for (const [tabId, st] of attached.entries()) {
        list.push({ tabId, network: st.network, fetch: st.fetch, console: st.console, pausedCount: st.paused.size });
      }
      return { type: "debug_status", attached: list };
    }
    case "debug_get_body": {
      const tab = await getTargetTab(msg);
      if (!tab) return { type: "error", error: "no_tab" };
      if (!msg.requestId) return { type: "error", error: "missing_requestId" };
      try {
        const r = await _sendCommand(tab.id, "Network.getResponseBody", { requestId: msg.requestId });
        return { type: "debug_body", tabId: tab.id, requestId: msg.requestId, body: r.body, base64Encoded: r.base64Encoded };
      } catch (e) {
        return { type: "error", error: `get_body_failed: ${e.message}` };
      }
    }
    case "debug_eval": {
      // Run caller-supplied JS in the page via CDP Runtime.evaluate. Unlike the
      // userScripts-based `js` command, this reliably supports async: the code is
      // wrapped in an async IIFE and evaluated with awaitPromise+returnByValue, so
      // `await fetch(...)` works and the resolved JSON value is returned. Runs in
      // the page's MAIN realm in the live session (cookies + csrf are the page's),
      // and is exempt from the page CSP (it's the debugger). Auto-attaches (shows
      // the "being debugged" banner); call debug_detach when done. CAN write.
      //   { type:"debug_eval", tabId?, code:"…", args?:<json> }
      const tab = await getTargetTab(msg);
      if (!tab) return { type: "error", error: "no_tab" };
      if (typeof msg.code !== "string" || !msg.code) return { type: "error", error: "missing 'code' (string)" };
      try {
        await _attach(tab.id, { network: false }); // idempotent; just need the debugger session
        const expression =
          "(async () => { const args = " + JSON.stringify(msg.args === undefined ? null : msg.args) + ";\n" +
          msg.code + "\n})()";
        const r = await _sendCommand(tab.id, "Runtime.evaluate", {
          expression, awaitPromise: true, returnByValue: true, userGesture: true,
        });
        if (r.exceptionDetails) {
          const ex = r.exceptionDetails;
          const emsg = (ex.exception && (ex.exception.description || ex.exception.value)) || ex.text || "eval exception";
          return { type: "debug_eval_result", tabId: tab.id, ok: false, error: String(emsg) };
        }
        return { type: "debug_eval_result", tabId: tab.id, ok: true, result: r.result ? r.result.value : null };
      } catch (e) {
        return { type: "error", error: `eval_failed: ${e.message || e}` };
      }
    }
    case "mouse_move": {
      // Move the (virtual) cursor to viewport CSS-px coords. Fires a real,
      // trusted mousemove via CDP, so :hover and page mousemove handlers react.
      // Absolute: pass x,y. Relative: pass dx and/or dy (added to the last known
      // cursor position) — used by the directional nudge commands.
      //   { type:"mouse_move", tabId?, x, y | dx, dy, shift?, ctrl?, alt?, meta? }
      const tab = await getTargetTab(msg);
      if (!tab) return { type: "error", error: "no_tab" };
      const relative = msg.dx != null || msg.dy != null;
      const prev = lastMouse.get(tab.id) || { x: 0, y: 0 };
      let x, y;
      if (relative) {
        x = Math.max(0, prev.x + Number(msg.dx || 0));
        y = Math.max(0, prev.y + Number(msg.dy || 0));
      } else {
        x = Number(msg.x); y = Number(msg.y);
      }
      if (!Number.isFinite(x) || !Number.isFinite(y)) return { type: "error", error: "missing x/y (numbers, viewport CSS px) or dx/dy" };
      try {
        await _attach(tab.id, { network: false }); // idempotent; just need the debugger session
        // Smooth, eased glide for absolute moves; relative nudges stay instant.
        if (!relative && msg.smooth !== false) {
          await _smoothMoveTo(tab.id, prev.x, prev.y, x, y, {
            modifiers: _modifiers(msg), drawCursor: msg.cursor !== false,
            durationMs: msg.durationMs, steps: msg.steps,
          });
        } else {
          await _sendCommand(tab.id, "Input.dispatchMouseEvent", {
            type: "mouseMoved", x, y, button: "none", buttons: 0, modifiers: _modifiers(msg),
          });
          if (msg.cursor !== false) await _drawCursor(tab.id, x, y, false);
        }
        lastMouse.set(tab.id, { x, y });
        const target = await _elementAt(tab.id, x, y);
        return { type: "mouse_moved", tabId: tab.id, x, y, target };
      } catch (e) {
        return { type: "error", error: `mouse_move_failed: ${e.message || e}` };
      }
    }

    case "mouse_click": {
      // Click at viewport CSS-px coords with a real, trusted gesture: move →
      // press → release. button: left|right|middle (default left); count for
      // double/triple click. waitForLoad waits if the click navigates.
      //   { type:"mouse_click", tabId?, x, y, button?, count?, waitForLoad?, shift?,… }
      const tab = await getTargetTab(msg);
      if (!tab) return { type: "error", error: "no_tab" };
      const x = Number(msg.x), y = Number(msg.y);
      if (!Number.isFinite(x) || !Number.isFinite(y)) return { type: "error", error: "missing x/y (numbers, viewport CSS px)" };
      const button = msg.button === "right" || msg.button === "middle" ? msg.button : "left";
      const count = Number.isFinite(Number(msg.count)) && Number(msg.count) >= 1 ? Number(msg.count) : 1;
      const mods = _modifiers(msg);
      const prev = lastMouse.get(tab.id) || { x, y };
      try {
        await _attach(tab.id, { network: false });
        // Glide to the target like a real cursor, then click.
        if (msg.smooth !== false) {
          await _smoothMoveTo(tab.id, prev.x, prev.y, x, y, {
            modifiers: mods, drawCursor: msg.cursor !== false,
            durationMs: msg.durationMs, steps: msg.steps,
          });
        } else {
          await _sendCommand(tab.id, "Input.dispatchMouseEvent", {
            type: "mouseMoved", x, y, button: "none", buttons: 0, modifiers: mods,
          });
          if (msg.cursor !== false) await _drawCursor(tab.id, x, y, false);
        }
        const target = await _elementAt(tab.id, x, y); // capture before the click navigates
        await _sendCommand(tab.id, "Input.dispatchMouseEvent", {
          type: "mousePressed", x, y, button, buttons: _buttonsMask(button), clickCount: count, modifiers: mods,
        });
        await _sendCommand(tab.id, "Input.dispatchMouseEvent", {
          type: "mouseReleased", x, y, button, buttons: 0, clickCount: count, modifiers: mods,
        });
        lastMouse.set(tab.id, { x, y });
        if (msg.cursor !== false) await _drawCursor(tab.id, x, y, true);
        if (msg.waitForLoad && typeof waitForComplete === "function") await waitForComplete(tab.id);
        return { type: "mouse_clicked", tabId: tab.id, x, y, button, count, target };
      } catch (e) {
        return { type: "error", error: `mouse_click_failed: ${e.message || e}` };
      }
    }

    case "mouse_down":
    case "mouse_up": {
      // Press or release a button at coords without the paired event — for
      // building custom gestures. Updates lastMouse.
      const tab = await getTargetTab(msg);
      if (!tab) return { type: "error", error: "no_tab" };
      const prev = lastMouse.get(tab.id) || { x: 0, y: 0 };
      const x = Number.isFinite(Number(msg.x)) ? Number(msg.x) : prev.x;
      const y = Number.isFinite(Number(msg.y)) ? Number(msg.y) : prev.y;
      const button = msg.button === "right" || msg.button === "middle" ? msg.button : "left";
      const down = msg.type === "mouse_down";
      try {
        await _attach(tab.id, { network: false });
        await _sendCommand(tab.id, "Input.dispatchMouseEvent", {
          type: down ? "mousePressed" : "mouseReleased",
          x, y, button, buttons: down ? _buttonsMask(button) : 0, clickCount: 1, modifiers: _modifiers(msg),
        });
        lastMouse.set(tab.id, { x, y });
        if (msg.cursor !== false) await _drawCursor(tab.id, x, y, down);
        return { type: down ? "mouse_pressed" : "mouse_released", tabId: tab.id, x, y, button };
      } catch (e) {
        return { type: "error", error: `mouse_${down ? "down" : "up"}_failed: ${e.message || e}` };
      }
    }

    case "mouse_wheel": {
      // Real, trusted wheel scroll via CDP at the cursor position (or x,y).
      // Unlike the JS `scroll` command (window.scrollBy), this dispatches a
      // wheel event the page actually receives — works on custom scroll
      // containers, virtualized lists, maps, etc.
      //   { type:"mouse_wheel", tabId?, x?, y?, deltaX?, deltaY? }  // +dy = down
      const tab = await getTargetTab(msg);
      if (!tab) return { type: "error", error: "no_tab" };
      const prev = lastMouse.get(tab.id) || { x: 0, y: 0 };
      const x = Number.isFinite(Number(msg.x)) ? Number(msg.x) : prev.x;
      const y = Number.isFinite(Number(msg.y)) ? Number(msg.y) : prev.y;
      const dx = Number(msg.deltaX || 0), dy = Number(msg.deltaY || 0);
      try {
        await _attach(tab.id, { network: false });
        await _sendCommand(tab.id, "Input.dispatchMouseEvent", {
          type: "mouseWheel", x, y, deltaX: dx, deltaY: dy, modifiers: _modifiers(msg),
        });
        lastMouse.set(tab.id, { x, y });
        if (msg.cursor !== false) await _drawCursor(tab.id, x, y, false);
        return { type: "mouse_scrolled", tabId: tab.id, x, y, deltaX: dx, deltaY: dy };
      } catch (e) {
        return { type: "error", error: `mouse_wheel_failed: ${e.message || e}` };
      }
    }

    case "mouse_hide": {
      // Remove the visible cursor overlay (cosmetic only).
      const tab = await getTargetTab(msg);
      if (!tab) return { type: "error", error: "no_tab" };
      try {
        await _attach(tab.id, { network: false });
        await _sendCommand(tab.id, "Runtime.evaluate", {
          expression: "(()=>{const e=document.getElementById('__dumper_cursor__');if(e)e.remove();})()",
        });
        return { type: "mouse_hidden", tabId: tab.id };
      } catch (e) {
        return { type: "error", error: `mouse_hide_failed: ${e.message || e}` };
      }
    }

    case "mouse_drag": {
      // Press at (x1,y1), move to (x2,y2) in steps, release. Real drag gesture.
      //   { type:"mouse_drag", tabId?, x1, y1, x2, y2, steps?, button? }
      const tab = await getTargetTab(msg);
      if (!tab) return { type: "error", error: "no_tab" };
      const x1 = Number(msg.x1), y1 = Number(msg.y1), x2 = Number(msg.x2), y2 = Number(msg.y2);
      if (![x1, y1, x2, y2].every(Number.isFinite)) return { type: "error", error: "missing x1/y1/x2/y2" };
      const button = msg.button === "right" || msg.button === "middle" ? msg.button : "left";
      const steps = Number.isFinite(Number(msg.steps)) && Number(msg.steps) >= 1 ? Number(msg.steps) : 10;
      const mods = _modifiers(msg);
      const mask = _buttonsMask(button);
      try {
        await _attach(tab.id, { network: false });
        await _sendCommand(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x: x1, y: y1, button: "none", buttons: 0, modifiers: mods });
        await _sendCommand(tab.id, "Input.dispatchMouseEvent", { type: "mousePressed", x: x1, y: y1, button, buttons: mask, clickCount: 1, modifiers: mods });
        for (let i = 1; i <= steps; i++) {
          const x = x1 + ((x2 - x1) * i) / steps;
          const y = y1 + ((y2 - y1) * i) / steps;
          await _sendCommand(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button, buttons: mask, modifiers: mods });
        }
        await _sendCommand(tab.id, "Input.dispatchMouseEvent", { type: "mouseReleased", x: x2, y: y2, button, buttons: 0, clickCount: 1, modifiers: mods });
        lastMouse.set(tab.id, { x: x2, y: y2 });
        if (msg.cursor !== false) await _drawCursor(tab.id, x2, y2, true);
        return { type: "mouse_dragged", tabId: tab.id, from: { x: x1, y: y1 }, to: { x: x2, y: y2 }, button, steps };
      } catch (e) {
        return { type: "error", error: `mouse_drag_failed: ${e.message || e}` };
      }
    }

    case "debug_pause_continue": {
      // action: "continue" (default) | "fail" | "fulfill"
      const tab = await getTargetTab(msg);
      if (!tab) return { type: "error", error: "no_tab" };
      const st = _state(tab.id);
      const rid = msg.requestId;
      if (!rid || !st.paused.has(rid)) return { type: "error", error: "unknown_requestId" };
      try {
        if (msg.action === "fail") {
          await _sendCommand(tab.id, "Fetch.failRequest", { requestId: rid, errorReason: msg.errorReason || "Aborted" });
        } else if (msg.action === "fulfill") {
          await _sendCommand(tab.id, "Fetch.fulfillRequest", {
            requestId: rid,
            responseCode: msg.responseCode || 200,
            responseHeaders: msg.responseHeaders || [],
            body: msg.body || "",
          });
        } else {
          await _sendCommand(tab.id, "Fetch.continueRequest", {
            requestId: rid,
            ...(msg.url ? { url: msg.url } : {}),
            ...(msg.method ? { method: msg.method } : {}),
            ...(msg.headers ? { headers: msg.headers } : {}),
            ...(msg.postData ? { postData: msg.postData } : {}),
          });
        }
        st.paused.delete(rid);
        return { type: "debug_pause_continued", tabId: tab.id, requestId: rid, action: msg.action || "continue" };
      } catch (e) {
        return { type: "error", error: `pause_continue_failed: ${e.message}` };
      }
    }
    default:
      return null; // not handled by this module
  }
}

// Export for background.js (service worker globals)
self.debugModule = { handleDebugCommand };
