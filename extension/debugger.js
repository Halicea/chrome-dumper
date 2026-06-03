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
let _listenerInstalled = false;

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
