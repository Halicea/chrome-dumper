// messaging plugin — extension half (browser primitives + panel UI).
//
// All data work (matching the candidate, rendering the template) lives in the
// CLIENT half (client/.../plugins/messaging.py), which reads the local sourcing
// repo directly. This file only exposes browser primitives and the in-drawer
// panel; the bridge stays a pure relay. Nothing here ever sends a message.
//
// Primitives (invoked by the client over /cmd, or by the panel):
//   messaging_scan        read drawer identity + compose presence
//   inject_value          React-safe fill of a field {selector?, value}
//   messaging_set_status  write text into the panel's status line
//   messaging_mount       show the floating panel
//   messaging_unmount     remove the panel
//
// The panel's "Insert draft" button has NO matching command here, so the loader
// forwards it to the client as a plugin_event; the client orchestrates
// scan → render → inject by calling the primitives above.

const PANEL_ID = "cd-messaging-panel";

// LinkedIn Recruiter InMail drawer selectors. Best-effort — run `messaging_scan`
// against a live drawer and adjust. The scan reports what it found so tuning is
// quick.
const SEL = {
  compose: [
    'textarea[name="message"]',
    'div[contenteditable="true"][role="textbox"]',
    '.msg-form__contenteditable[contenteditable="true"]',
    'textarea[aria-label*="message" i]',
    'textarea',
  ],
  profileLink: 'a[href*="/talent/profile/"], a[href*="linkedin.com/in/"], a[href*="/in/"]',
  nameEl: 'a[href*="/talent/profile/"], header h1, h1, h2',
};

async function scanDrawer(tabId) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    args: [SEL],
    func: (sel) => {
      const compose = sel.compose.map((s) => document.querySelector(s)).find(Boolean) || null;
      const links = Array.from(document.querySelectorAll(sel.profileLink))
        .map((a) => a.href)
        .filter(Boolean);
      const profile_url = links.find((h) => /\/talent\/profile\//.test(h))
        || links.find((h) => /\/in\//.test(h))
        || "";
      let name = "";
      const nameEl = document.querySelector(sel.nameEl);
      if (nameEl) name = (nameEl.textContent || "").trim().replace(/\s+/g, " ").slice(0, 120);
      return {
        composeFound: !!compose,
        composeTag: compose ? compose.tagName.toLowerCase() : null,
        composeEditable: compose ? !!compose.isContentEditable : null,
        profile_url,
        name,
        profileLinkCount: links.length,
      };
    },
  });
  return result || { composeFound: false };
}

// React-safe value injection. <textarea>/<input> need the native value setter so
// React's controlled state updates; contenteditable uses execCommand insertText.
async function injectValueImpl(tabId, selector, value) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    args: [selector, SEL.compose, value],
    func: (selector, composeSels, val) => {
      const el = selector
        ? document.querySelector(selector)
        : composeSels.map((s) => document.querySelector(s)).find(Boolean);
      if (!el) return { ok: false, error: "no_element" };
      el.focus();
      if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
        const proto = el instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
        setter.call(el, val);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return { ok: true, tag: el.tagName, mode: "value-setter" };
      }
      if (el.isContentEditable) {
        const selObj = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(el);
        selObj.removeAllRanges();
        selObj.addRange(range);
        const ok = document.execCommand("insertText", false, val);
        if (!ok) {
          el.textContent = val;
          el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: val }));
        }
        return { ok: true, tag: el.tagName, mode: ok ? "execCommand" : "textContent" };
      }
      return { ok: false, error: "unsupported_element", tag: el.tagName };
    },
  });
  return result || { ok: false, error: "no_result" };
}

async function setPanelStatus(tabId, text) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    args: [PANEL_ID, String(text || "")],
    func: (panelId, txt) => {
      const host = document.getElementById(panelId);
      const el = host && host.shadowRoot && host.shadowRoot.querySelector("[data-plugin-status]");
      if (el) el.textContent = txt;
      return { ok: !!el };
    },
  });
  return result || { ok: false };
}

const PANEL_HTML = `
  <div class="card">
    <div class="hd"><span>✉️ Draft message</span><button class="x" data-plugin-action="unmount" title="close">×</button></div>
    <div class="bd">Open a candidate's InMail compose drawer, then insert the matched draft. Nothing is sent — you review and click Send.</div>
    <button class="go" data-plugin-action="insert_draft">Insert draft</button>
    <div class="status" data-plugin-status></div>
  </div>`;

const PANEL_CSS = `
  .card { font: 13px/1.45 -apple-system, system-ui, sans-serif; width: 248px; background: #fff;
          color: #1d2226; border: 1px solid #d0d5dd; border-radius: 10px;
          box-shadow: 0 8px 28px rgba(0,0,0,.18); overflow: hidden; }
  .hd { display:flex; align-items:center; justify-content:space-between; gap:8px;
        padding:10px 12px; font-weight:600; background:#f3f6f8; border-bottom:1px solid #e6e9ec; }
  .x { border:0; background:transparent; font-size:18px; line-height:1; cursor:pointer; color:#56687a; }
  .bd { padding:10px 12px; color:#56687a; }
  .go { margin:0 12px 10px; width:calc(100% - 24px); padding:8px 12px; cursor:pointer;
        background:#0a66c2; color:#fff; border:0; border-radius:20px; font-weight:600; font-size:13px; }
  .go:hover { background:#004182; }
  .status { padding:0 12px 12px; min-height:14px; color:#0a66c2; font-size:12px; white-space:pre-wrap; }`;

self.pluginRegistry.register({
  id: "messaging",
  name: "LinkedIn draft messaging",
  match: ["*://*.linkedin.com/talent/*", "*://*.linkedin.com/*"],
  panel: { panelId: PANEL_ID, html: PANEL_HTML, css: PANEL_CSS },
  commands: {
    messaging_scan: async (msg, ctx) => {
      const tab = await ctx.getTargetTab(msg);
      if (!tab) return { ok: false, error: "no_tab" };
      const r = await scanDrawer(tab.id);
      return { type: "messaging_scan_result", ok: true, ...r };
    },
    inject_value: async (msg, ctx) => {
      const tab = await ctx.getTargetTab(msg);
      if (!tab) return { ok: false, type: "error", error: "no_tab" };
      const r = await injectValueImpl(tab.id, msg.selector || null, msg.value ?? "");
      return { type: "value_injected", ...r };
    },
    messaging_set_status: async (msg, ctx) => {
      const tab = await ctx.getTargetTab(msg);
      if (!tab) return { ok: false, error: "no_tab" };
      const r = await setPanelStatus(tab.id, msg.text);
      return { type: "status_set", ...r };
    },
    messaging_mount: async (msg, ctx) => {
      const tab = await ctx.getTargetTab(msg);
      if (!tab) return { ok: false, error: "no_tab" };
      const r = await self.mountPluginPanel(tab.id, { plugin: "messaging", panelId: PANEL_ID, html: PANEL_HTML, css: PANEL_CSS });
      return { type: "panel_mounted", ...r, message: "panel mounted" };
    },
    messaging_unmount: async (msg, ctx) => {
      const tab = await ctx.getTargetTab(msg);
      if (!tab) return { ok: false, error: "no_tab" };
      await self.unmountPluginPanel(tab.id, PANEL_ID);
      return { type: "panel_unmounted", ok: true };
    },
  },
});
