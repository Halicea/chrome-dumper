const WS_URL = "ws://127.0.0.1:8765";
const RECONNECT_MS = 2000;

// debugger.js is loaded by the manifest's background.scripts array (Firefox MV3
// uses an event page, not a service worker, so importScripts is unavailable).

let ws = null;
let reconnectTimer = null;

function setBadge(text, color) {
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setBadgeText({ text });
}

function connect() {
  clearTimeout(reconnectTimer);
  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    setBadge("ON", "#2e7d32");
    ws.send(JSON.stringify({ type: "hello", agent: "html-dumper-firefox", version: "0.1.0" }));
  };

  ws.onclose = () => {
    setBadge("OFF", "#c62828");
    scheduleReconnect();
  };

  ws.onerror = () => {
    try { ws.close(); } catch (_) {}
  };

  ws.onmessage = async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); }
    catch { return reply({ type: "error", error: "invalid_json" }); }
    await handle(msg);
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, RECONNECT_MS);
}

function reply(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}
// Expose reply for the debugger module (matches Chrome build's global).
self.reply = reply;

async function getTargetTab(msg) {
  if (msg.tabId) return chrome.tabs.get(msg.tabId);
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab;
}

function waitForComplete(tabId, timeoutMs = 30000) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (id, info) => {
      if (id === tabId && info.status === "complete") finish();
    };
    chrome.tabs.onUpdated.addListener(listener);
    const timer = setTimeout(finish, timeoutMs);
  });
}

async function dumpHtml(tabId) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId, allFrames: false },
    func: () => "<!DOCTYPE " + (document.doctype?.name || "html") + ">\n" +
                document.documentElement.outerHTML,
  });
  return result;
}

async function handle(msg) {
  const id = msg.id ?? null;
  try {
    // Delegate debug_* commands to the (stub) debugger module.
    if (typeof msg.type === "string" && msg.type.startsWith("debug_") &&
        self.debugModule && typeof self.debugModule.handleDebugCommand === "function") {
      const r = await self.debugModule.handleDebugCommand(msg, getTargetTab);
      if (r) return reply({ id, ...r });
    }
    switch (msg.type) {
      case "ping":
        return reply({ id, type: "pong" });

      case "keepalive_ack":
        return;

      case "list_tabs": {
        const tabs = await chrome.tabs.query({});
        return reply({
          id, type: "tabs",
          tabs: tabs.map(t => ({ id: t.id, url: t.url, title: t.title, active: t.active, windowId: t.windowId })),
        });
      }

      case "open": {
        if (!msg.url) return reply({ id, type: "error", error: "missing_url" });
        const opts = { url: msg.url, active: msg.active !== false };
        if (msg.windowId) opts.windowId = msg.windowId;
        const tab = await chrome.tabs.create(opts);
        if (msg.waitForLoad) await waitForComplete(tab.id);
        return reply({ id, type: "opened", tabId: tab.id, url: tab.url || msg.url });
      }

      case "navigate": {
        if (!msg.url) return reply({ id, type: "error", error: "missing_url" });
        const target = await getTargetTab(msg);
        if (!target) return reply({ id, type: "error", error: "no_tab" });
        const tab = await chrome.tabs.update(target.id, { url: msg.url });
        if (msg.waitForLoad) await waitForComplete(tab.id);
        return reply({ id, type: "navigated", tabId: tab.id, url: msg.url });
      }

      case "back":
      case "forward": {
        const target = await getTargetTab(msg);
        if (!target) return reply({ id, type: "error", error: "no_tab" });
        const steps = Math.max(1, msg.steps || 1);
        const op = msg.type === "back" ? "goBack" : "goForward";
        try {
          for (let i = 0; i < steps; i++) {
            if (chrome.tabs[op]) {
              await chrome.tabs[op](target.id);
            } else {
              await chrome.scripting.executeScript({
                target: { tabId: target.id },
                args: [msg.type === "back" ? -1 : 1],
                func: (n) => history.go(n),
              });
            }
            if (msg.waitForLoad) await waitForComplete(target.id);
          }
        } catch (e) {
          return reply({ id, type: "error", error: `${msg.type}_failed: ${e.message || e}` });
        }
        const after = await chrome.tabs.get(target.id);
        return reply({ id, type: msg.type === "back" ? "went_back" : "went_forward",
                       tabId: target.id, url: after.url, steps });
      }

      case "key": {
        const tab = await getTargetTab(msg);
        if (!tab) return reply({ id, type: "error", error: "no_tab" });
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          args: [{
            key: msg.key,
            shift: !!msg.shift, ctrl: !!msg.ctrl, alt: !!msg.alt, meta: !!msg.meta,
            selector: msg.selector || null,
          }],
          func: ({ key, shift, ctrl, alt, meta, selector }) => {
            if (!key) return { ok: false, error: "missing_key" };
            const target = selector ? document.querySelector(selector) : (document.activeElement || document.body);
            if (selector && !target) return { ok: false, error: "selector_not_found" };

            if (key === "Tab") {
              const items = [...document.querySelectorAll(
                'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]),' +
                ' select:not([disabled]), textarea:not([disabled]),' +
                ' [tabindex]:not([tabindex="-1"]), [contenteditable=""], [contenteditable="true"]'
              )].filter(e => (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
              if (!items.length) return { ok: false, error: "no_focusables" };
              const cur = items.indexOf(document.activeElement);
              const step = shift ? -1 : 1;
              const next = items[((cur < 0 ? -1 : cur) + step + items.length) % items.length];
              try { next.focus({ focusVisible: true }); } catch { next.focus(); }
              return {
                ok: true, focused: {
                  tag: next.tagName.toLowerCase(),
                  id: next.id || null, name: next.name || null,
                  type: next.type || null,
                  text: (next.innerText || next.value || "").trim().slice(0, 120),
                },
              };
            }

            const make = (type) => new KeyboardEvent(type, {
              key, code: key.length === 1 ? "Key" + key.toUpperCase() : key,
              shiftKey: shift, ctrlKey: ctrl, altKey: alt, metaKey: meta,
              bubbles: true, cancelable: true,
            });
            target.dispatchEvent(make("keydown"));
            target.dispatchEvent(make("keypress"));
            target.dispatchEvent(make("keyup"));

            let activated = false;
            const activator = target;
            if ((key === "Enter" || key === " " || key === "Spacebar") && target && target !== document.body) {
              const tag = target.tagName?.toLowerCase();
              const role = target.getAttribute && target.getAttribute("role");
              const isAnchor = tag === "a" && target.href;
              const isButton = tag === "button" ||
                (tag === "input" && /^(submit|button|reset|checkbox|radio|image)$/i.test(target.type)) ||
                role === "button" || role === "link";
              if (key === "Enter" && target.form &&
                  (tag === "input" || tag === "textarea" || tag === "select")) {
                if (typeof target.form.requestSubmit === "function") target.form.requestSubmit();
                else target.form.submit();
                activated = true;
              } else if (isAnchor || isButton) {
                target.click();
                activated = true;
              }
            }

            let focusMoved = false;
            if (activated && activator) {
              const focusableSel =
                'a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]),' +
                ' select:not([disabled]), textarea:not([disabled]),' +
                ' [tabindex]:not([tabindex="-1"]), [contenteditable=""], [contenteditable="true"]';
              const visible = (e) => !!(e && (e.offsetWidth || e.offsetHeight || e.getClientRects().length));
              const firstFocusableIn = (root) => {
                if (!root) return null;
                if (root.matches?.(focusableSel) && visible(root)) return root;
                const list = root.querySelectorAll?.(focusableSel);
                if (!list) return null;
                for (const e of list) if (visible(e) && e !== activator) return e;
                return null;
              };
              const candidates = [];
              const controlsId = activator.getAttribute?.("aria-controls");
              if (controlsId) {
                for (const cid of controlsId.split(/\s+/)) {
                  const t = document.getElementById(cid);
                  if (t) candidates.push(t);
                }
              }
              const det = activator.closest?.("details[open]");
              if (det) candidates.push(det);
              const dialogs = document.querySelectorAll("dialog[open]");
              if (dialogs.length) candidates.push(dialogs[dialogs.length - 1]);

              for (const root of candidates) {
                const f = firstFocusableIn(root);
                if (f) {
                  try { f.focus({ focusVisible: true }); } catch { f.focus(); }
                  focusMoved = (document.activeElement === f);
                  break;
                }
              }
            }

            return {
              ok: true, key, activated, focusMoved,
              focused: focusMoved && document.activeElement ? {
                tag: document.activeElement.tagName.toLowerCase(),
                id: document.activeElement.id || null,
                text: (document.activeElement.innerText || document.activeElement.value || "").trim().slice(0, 120),
              } : null,
            };
          },
        });
        if (!result?.ok) return reply({ id, type: "error", error: result?.error || "key_failed" });
        if (msg.waitForLoad) await waitForComplete(tab.id);
        return reply({ id, type: "key_sent", tabId: tab.id, ...result });
      }

      case "select": {
        const tab = await getTargetTab(msg);
        if (!tab) return reply({ id, type: "error", error: "no_tab" });
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          args: [{
            selector: msg.selector || null,
            text: msg.text || null,
            from: msg.from || null,
            to: msg.to || null,
            rect: msg.rect || null,
            dispatchMouse: msg.dispatchMouse !== false,
            scroll: msg.scroll !== false,
          }],
          func: ({ selector, text, from, to, rect, dispatchMouse, scroll }) => {
            const sel = window.getSelection();
            sel.removeAllRanges();
            const range = document.createRange();

            if (selector) {
              const el = document.querySelector(selector);
              if (!el) return { ok: false, error: "selector_not_found" };
              range.selectNodeContents(el);
            } else if (from && to) {
              const a = document.querySelector(from);
              const b = document.querySelector(to);
              if (!a || !b) return { ok: false, error: "from_or_to_not_found" };
              range.setStartBefore(a);
              range.setEndAfter(b);
            } else if (text) {
              const needle = text;
              const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
              let node, found = null, offset = -1;
              while ((node = walker.nextNode())) {
                const i = node.nodeValue.indexOf(needle);
                if (i >= 0) { found = node; offset = i; break; }
              }
              if (!found) return { ok: false, error: "text_not_found" };
              range.setStart(found, offset);
              range.setEnd(found, offset + needle.length);
            } else if (rect) {
              const caretAt = (x, y) => {
                if (document.caretPositionFromPoint) {
                  const p = document.caretPositionFromPoint(x, y);
                  if (!p) return null;
                  const r = document.createRange();
                  r.setStart(p.offsetNode, p.offset);
                  r.setEnd(p.offsetNode, p.offset);
                  return r;
                }
                if (document.caretRangeFromPoint) return document.caretRangeFromPoint(x, y);
                return null;
              };
              const a = caretAt(rect.x1, rect.y1);
              const b = caretAt(rect.x2, rect.y2);
              if (!a || !b) return { ok: false, error: "no_caret_at_point" };
              range.setStart(a.startContainer, a.startOffset);
              range.setEnd(b.startContainer, b.startOffset);
            } else {
              return { ok: false, error: "no_target" };
            }

            if (scroll) {
              const r = range.getBoundingClientRect();
              if (r.top < 0 || r.bottom > window.innerHeight) {
                window.scrollTo({ top: window.scrollY + r.top - window.innerHeight / 3, behavior: "auto" });
              }
            }

            sel.addRange(range);

            let info = { text: sel.toString(), rect: null };
            const rr = range.getBoundingClientRect();
            info.rect = { x: rr.x, y: rr.y, width: rr.width, height: rr.height };
            if (dispatchMouse && rr.width > 0 && rr.height > 0) {
              const startX = rr.x + 1, startY = rr.y + rr.height / 2;
              const endX = rr.right - 1, endY = rr.y + rr.height / 2;
              const fire = (type, x, y, buttons) => {
                const target = document.elementFromPoint(x, y) || document.body;
                target.dispatchEvent(new MouseEvent(type, {
                  bubbles: true, cancelable: true, view: window,
                  clientX: x, clientY: y, button: 0, buttons,
                }));
              };
              fire("mousedown", startX, startY, 1);
              const steps = 6;
              for (let i = 1; i <= steps; i++) {
                fire("mousemove",
                     startX + (endX - startX) * i / steps,
                     startY + (endY - startY) * i / steps,
                     1);
              }
              fire("mouseup", endX, endY, 0);
            }
            return { ok: true, ...info };
          },
        });
        if (!result?.ok) return reply({ id, type: "error", error: result?.error || "select_failed" });
        return reply({ id, type: "selected", tabId: tab.id, ...result });
      }

      case "select_clear": {
        const tab = await getTargetTab(msg);
        if (!tab) return reply({ id, type: "error", error: "no_tab" });
        await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: () => window.getSelection()?.removeAllRanges(),
        });
        return reply({ id, type: "selection_cleared", tabId: tab.id });
      }

      case "scroll": {
        const tab = await getTargetTab(msg);
        if (!tab) return reply({ id, type: "error", error: "no_tab" });
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          args: [{
            direction: msg.direction || "down",
            pages: msg.pages,
            pixels: msg.pixels,
            to: msg.to || null,
            smooth: msg.smooth !== false,
          }],
          func: async ({ direction, pages, pixels, to, smooth }) => {
            const behavior = smooth ? "smooth" : "auto";

            const findScroller = () => {
              const root = document.scrollingElement || document.documentElement;
              if (root && root.scrollHeight > root.clientHeight + 1) return root;
              let best = null;
              const walk = (el) => {
                if (!el || el.nodeType !== 1) return;
                const cs = getComputedStyle(el);
                const oy = cs.overflowY;
                if ((oy === "auto" || oy === "scroll" || oy === "overlay") &&
                    el.scrollHeight > el.clientHeight + 1 &&
                    el.clientHeight > 200) {
                  if (!best || el.clientHeight > best.clientHeight) best = el;
                }
                for (const c of el.children) walk(c);
              };
              walk(document.body);
              return best || root;
            };

            const scroller = findScroller();
            const isRoot = scroller === document.scrollingElement ||
                           scroller === document.documentElement ||
                           scroller === document.body;
            const vh = isRoot ? window.innerHeight : scroller.clientHeight;

            const scrollTo = (top) => {
              if (isRoot) window.scrollTo({ top, behavior });
              else scroller.scrollTo({ top, behavior });
            };
            const scrollBy = (top) => {
              if (isRoot) window.scrollBy({ top, behavior });
              else scroller.scrollBy({ top, behavior });
            };

            if (to === "top") {
              scrollTo(0);
            } else if (to === "bottom") {
              scrollTo(scroller.scrollHeight);
            } else if (to) {
              const el = document.querySelector(to);
              if (!el) return { ok: false, error: "selector_not_found" };
              el.scrollIntoView({ block: "center", inline: "center", behavior });
            } else {
              const delta = pixels != null
                ? pixels
                : (pages != null ? pages : 0.5) * vh;
              const signed = direction === "up" ? -delta : delta;
              scrollBy(signed);
            }

            const readTop = () => isRoot ? window.scrollY : scroller.scrollTop;
            const startTop = readTop();
            const start = performance.now();
            let prev = startTop;
            let stableTicks = 0;
            const minWaitMs = behavior === "smooth" ? 250 : 0;
            const maxWaitMs = behavior === "smooth" ? 2000 : 200;
            while (performance.now() - start < maxWaitMs) {
              await new Promise(r => setTimeout(r, 50));
              const cur = readTop();
              if (cur !== prev) {
                stableTicks = 0;
                prev = cur;
              } else {
                stableTicks++;
                if (performance.now() - start >= minWaitMs &&
                    stableTicks >= 2 &&
                    cur !== startTop) break;
              }
            }

            return {
              ok: true,
              scrollX: isRoot ? window.scrollX : scroller.scrollLeft,
              scrollY: isRoot ? window.scrollY : scroller.scrollTop,
              viewport: { width: window.innerWidth, height: window.innerHeight },
              docHeight: scroller.scrollHeight,
              scrollerTag: scroller.tagName,
              scrollerId: scroller.id || null,
              scrollerClass: scroller.className || null,
            };
          },
        });
        if (!result?.ok) return reply({ id, type: "error", error: result?.error || "scroll_failed" });
        return reply({ id, type: "scrolled", tabId: tab.id, ...result });
      }

      case "highlight": {
        const tab = await getTargetTab(msg);
        if (!tab) return reply({ id, type: "error", error: "no_tab" });
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          args: [{
            selector: msg.selector || null,
            text: msg.text || null,
            rect: msg.rect || null,
            all: !!msg.all,
            nth: msg.nth || 0,
            color: msg.color || "#ff1744",
            label: msg.label || null,
            durationMs: msg.durationMs ?? 0,
            scroll: msg.scroll !== false,
          }],
          func: (opts) => {
            const HOST_ID = "__chrome_dumper_highlight_host__";
            let host = document.getElementById(HOST_ID);
            if (!host) {
              host = document.createElement("div");
              host.id = HOST_ID;
              Object.assign(host.style, {
                position: "fixed", left: "0", top: "0", width: "0", height: "0",
                pointerEvents: "none", zIndex: "2147483647",
              });
              document.documentElement.appendChild(host);
            }
            const targets = [];
            if (opts.rect) {
              targets.push({ rect: opts.rect, el: null });
            } else if (opts.selector || opts.text) {
              let els = [];
              if (opts.selector) els = [...document.querySelectorAll(opts.selector)];
              else {
                const needle = opts.text.trim().toLowerCase();
                els = [...document.querySelectorAll("a, button, [role=button], h1, h2, h3, h4, span, div, p, li")]
                  .filter(e => (e.innerText || "").trim().toLowerCase().includes(needle));
              }
              if (!els.length) return { ok: false, error: "not_found" };
              const chosen = opts.all ? els : [els[opts.nth] || els[0]];
              for (const el of chosen) targets.push({ el, rect: null });
            } else {
              return { ok: false, error: "no_target" };
            }

            if (opts.scroll && targets[0]?.el) {
              targets[0].el.scrollIntoView({ block: "center", inline: "center" });
            }

            const made = [];
            for (const t of targets) {
              const r = t.rect || t.el.getBoundingClientRect();
              const box = document.createElement("div");
              Object.assign(box.style, {
                position: "fixed",
                left: r.x + "px", top: r.y + "px",
                width: r.width + "px", height: r.height + "px",
                border: `2px solid ${opts.color}`,
                boxShadow: `0 0 0 2px ${opts.color}40, 0 0 8px ${opts.color}`,
                borderRadius: "2px",
                pointerEvents: "none",
                boxSizing: "border-box",
              });
              if (opts.label) {
                const tag = document.createElement("div");
                tag.textContent = opts.label;
                Object.assign(tag.style, {
                  position: "absolute", left: "0", top: "-20px",
                  background: opts.color, color: "white",
                  font: "12px/16px system-ui, sans-serif",
                  padding: "1px 6px", borderRadius: "2px",
                  whiteSpace: "nowrap",
                });
                box.appendChild(tag);
              }
              host.appendChild(box);
              made.push({ x: r.x, y: r.y, width: r.width, height: r.height });
            }

            if (opts.durationMs > 0) {
              const toRemove = [...made.keys()].map(i => host.children[host.children.length - made.length + i]);
              setTimeout(() => toRemove.forEach(n => n.remove()), opts.durationMs);
            }
            return { ok: true, count: made.length, rects: made };
          },
        });
        if (!result?.ok) return reply({ id, type: "error", error: result?.error || "highlight_failed" });
        return reply({ id, type: "highlighted", tabId: tab.id, ...result });
      }

      case "clear_highlights": {
        const tab = await getTargetTab(msg);
        if (!tab) return reply({ id, type: "error", error: "no_tab" });
        await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          func: () => document.getElementById("__chrome_dumper_highlight_host__")?.remove(),
        });
        return reply({ id, type: "highlights_cleared", tabId: tab.id });
      }

      case "type": {
        const tab = await getTargetTab(msg);
        if (!tab) return reply({ id, type: "error", error: "no_tab" });
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          args: [{
            selector: msg.selector || null,
            placeholder: msg.placeholder || null,
            label: msg.label || null,
            nth: msg.nth || 0,
            value: msg.value ?? "",
            clear: msg.clear !== false,
            submit: !!msg.submit,
          }],
          func: ({ selector, placeholder, label, nth, value, clear, submit }) => {
            const norm = (s) => (s || "").trim().toLowerCase();
            let candidates = [];
            if (selector) {
              candidates = [...document.querySelectorAll(selector)];
            } else if (placeholder) {
              const needle = norm(placeholder);
              candidates = [...document.querySelectorAll("input, textarea, [contenteditable]")]
                .filter(e => norm(e.placeholder || e.getAttribute("aria-placeholder")).includes(needle));
            } else if (label) {
              const needle = norm(label);
              candidates = [...document.querySelectorAll("input, textarea, select, [contenteditable]")]
                .filter(e => {
                  if (norm(e.getAttribute("aria-label")).includes(needle)) return true;
                  if (norm(e.name).includes(needle)) return true;
                  if (e.id) {
                    const lab = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
                    if (lab && norm(lab.innerText).includes(needle)) return true;
                  }
                  const wrap = e.closest("label");
                  if (wrap && norm(wrap.innerText).includes(needle)) return true;
                  return false;
                });
            } else {
              const a = document.activeElement;
              if (a && (a.matches("input, textarea") || a.isContentEditable)) candidates = [a];
            }
            const el = candidates[nth] || null;
            if (!el) return { ok: false, error: "field_not_found" };
            el.scrollIntoView({ block: "center" });
            el.focus();
            const isCE = el.isContentEditable;
            if (clear) {
              if (isCE) el.textContent = "";
              else el.value = "";
              el.dispatchEvent(new Event("input", { bubbles: true }));
            }
            if (!isCE) {
              const proto = el.tagName === "TEXTAREA"
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
              if (setter) setter.call(el, (clear ? "" : el.value) + value);
              else el.value = (clear ? "" : el.value) + value;
            } else {
              el.textContent = (clear ? "" : el.textContent) + value;
            }
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.dispatchEvent(new Event("change", { bubbles: true }));
            const info = {
              tag: el.tagName.toLowerCase(),
              name: el.name || null,
              id: el.id || null,
              value: isCE ? el.textContent : el.value,
            };
            if (submit) {
              const form = el.form;
              if (form && typeof form.requestSubmit === "function") form.requestSubmit();
              else if (form) form.submit();
              else {
                const ev = new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true });
                el.dispatchEvent(ev);
              }
            }
            return { ok: true, info };
          },
        });
        if (!result?.ok) return reply({ id, type: "error", error: result?.error || "type_failed" });
        if (msg.waitForLoad) await waitForComplete(tab.id);
        return reply({ id, type: "typed", tabId: tab.id, ...result.info });
      }

      case "close": {
        let ids;
        if (Array.isArray(msg.tabIds) && msg.tabIds.length) {
          ids = msg.tabIds;
        } else if (typeof msg.tabId === "number") {
          ids = [msg.tabId];
        } else {
          const tab = await getTargetTab(msg);
          if (!tab) return reply({ id, type: "error", error: "no_tab" });
          ids = [tab.id];
        }
        await chrome.tabs.remove(ids);
        return reply({ id, type: "closed", tabIds: ids });
      }

      case "click": {
        const tab = await getTargetTab(msg);
        if (!tab) return reply({ id, type: "error", error: "no_tab" });
        const [{ result }] = await chrome.scripting.executeScript({
          target: { tabId: tab.id },
          args: [{ selector: msg.selector || null, text: msg.text || null, nth: msg.nth || 0 }],
          func: ({ selector, text, nth }) => {
            let el = null;
            if (selector) {
              const all = document.querySelectorAll(selector);
              el = all[nth] || null;
            } else if (text) {
              const needle = text.trim().toLowerCase();
              const candidates = document.querySelectorAll("a, button, [role=button], input[type=submit], input[type=button]");
              const matches = [...candidates].filter(e => (e.innerText || e.value || "").trim().toLowerCase().includes(needle));
              el = matches[nth] || null;
            } else {
              const a = document.activeElement;
              if (a && a !== document.body) el = a;
            }
            if (!el) return { ok: false, error: "not_found" };
            el.scrollIntoView({ block: "center" });
            const info = {
              tag: el.tagName.toLowerCase(),
              href: el.href || null,
              text: (el.innerText || el.value || "").trim().slice(0, 200),
            };
            el.click();
            return { ok: true, info };
          },
        });
        if (!result?.ok) return reply({ id, type: "error", error: result?.error || "click_failed" });
        if (msg.waitForLoad) await waitForComplete(tab.id);
        return reply({ id, type: "clicked", tabId: tab.id, ...result.info });
      }

      case "screenshot": {
        const tab = await getTargetTab(msg);
        if (!tab) return reply({ id, type: "error", error: "no_tab" });
        const format = msg.format === "jpeg" ? "jpeg" : "png";
        const opts = { format };
        if (format === "jpeg") opts.quality = msg.quality ?? 85;
        try {
          let cropRect = msg.rect || null;
          let dpr = 1;
          if (msg.selector || msg.text || msg.rect) {
            const [{ result }] = await chrome.scripting.executeScript({
              target: { tabId: tab.id },
              args: [{ selector: msg.selector || null, text: msg.text || null }],
              func: ({ selector, text }) => {
                const dpr = window.devicePixelRatio || 1;
                if (!selector && !text) return { dpr };
                let el = null;
                if (selector) el = document.querySelector(selector);
                else if (text) {
                  const needle = text.trim().toLowerCase();
                  el = [...document.querySelectorAll("a, button, h1, h2, h3, h4, span, div, p, li, [role=button]")]
                    .find(e => (e.innerText || "").trim().toLowerCase().includes(needle)) || null;
                }
                if (!el) return { dpr, error: "target_not_found" };
                el.scrollIntoView({ block: "center", inline: "center" });
                const r = el.getBoundingClientRect();
                return { dpr, rect: { x: r.x, y: r.y, width: r.width, height: r.height } };
              },
            });
            if (result?.error) return reply({ id, type: "error", error: result.error });
            dpr = result.dpr || 1;
            if (result.rect) cropRect = result.rect;
            if (msg.selector || msg.text) await new Promise(r => setTimeout(r, 120));
          }

          const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, opts);
          return reply({
            id, type: "screenshot_result",
            tabId: tab.id, url: tab.url, title: tab.title, format,
            dataUrl, rect: cropRect, dpr,
          });
        } catch (e) {
          return reply({ id, type: "error", error: String(e?.message || e) });
        }
      }

      case "dump": {
        const tab = await getTargetTab(msg);
        if (!tab) return reply({ id, type: "error", error: "no_tab" });
        const html = await dumpHtml(tab.id);
        return reply({
          id, type: "dump_result",
          tabId: tab.id, url: tab.url, title: tab.title,
          html,
        });
      }

      default:
        return reply({ id, type: "error", error: "unknown_type", got: msg.type });
    }
  } catch (e) {
    reply({ id, type: "error", error: String(e?.message || e) });
  }
}

// Firefox MV3 event pages also unload after a period of idleness. The same
// keepalive trick — an alarm that wakes the page on a fixed cadence — keeps
// the WebSocket alive (or reconnects if it died) regardless of browser.
const KEEPALIVE_ALARM = "ws-keepalive";

function ensureAlive() {
  if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
    connect();
  } else if (ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify({ type: "keepalive", t: Date.now() })); } catch (_) {}
  }
}

chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((a) => { if (a.name === KEEPALIVE_ALARM) ensureAlive(); });

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
