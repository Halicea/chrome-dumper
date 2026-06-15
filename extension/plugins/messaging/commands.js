// messaging plugin — extension half (browser primitives + panel UI).
//
// All data work (matching the candidate, rendering the template, resolving the
// job) lives in the CLIENT half (client/.../plugins/messaging.py), which reads
// the local sourcing repo directly. This file only exposes browser primitives
// and the in-drawer panel; the bridge stays a pure relay. Nothing here sends.
//
// Primitives (invoked by the client over /cmd, or via panel actions):
//   messaging_scan         drawer identity + compose presence + tab url + saved job
//   inject_value           React-safe fill of a field {selector?, value}
//   messaging_set_status   write text into the panel's status line
//   messaging_set_jobs     populate the panel's job <select> {jobs, selected}
//   messaging_set_tab_job  persist the chosen job for this tab (chrome.storage.session)
//   messaging_mount        show the floating panel
//   messaging_unmount      remove the panel
//
// Panel actions with no matching command (insert_draft, select_job, panel_ready)
// are forwarded to the client as plugin_events; the client orchestrates the rest.

const PANEL_ID = "cd-messaging-panel";
const JOB_KEY = (tabId) => `mjob:${tabId}`; // per-tab job selection in storage.session

// LinkedIn Recruiter compose selectors. Inbox uses a <textarea>; the project
// pipeline / bulk pages use a contenteditable <div>. Best-effort — `messaging_scan`
// reports what it found so tuning is quick.
const SEL = {
  compose: [
    'textarea[name="message"]',
    'div[contenteditable="true"][role="textbox"]',
    '.msg-form__contenteditable[contenteditable="true"]',
    'textarea[aria-label*="message" i]',
    'div[contenteditable="true"]',
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
        url: location.href,
        composeFound: !!compose,
        composeTag: compose ? compose.tagName.toLowerCase() : null,
        composeEditable: compose ? !!compose.isContentEditable : null,
        profile_url,
        name,
        profileLinkCount: links.length,
      };
    },
  });
  return result || { composeFound: false, url: "" };
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

async function setPanelJobs(tabId, jobs, selected) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    args: [PANEL_ID, jobs || [], selected || ""],
    func: (panelId, jobs, selected) => {
      const host = document.getElementById(panelId);
      const sel = host && host.shadowRoot && host.shadowRoot.querySelector("[data-plugin-job]");
      if (!sel) return { ok: false, error: "no_panel" };
      sel.innerHTML = "";
      const ph = document.createElement("option");
      ph.value = "";
      ph.textContent = jobs.length ? "— select job —" : "(no jobs found)";
      sel.appendChild(ph);
      for (const j of jobs) {
        const o = document.createElement("option");
        o.value = j.slug;
        o.textContent = j.title || j.slug;
        if (j.slug === selected) o.selected = true;
        sel.appendChild(o);
      }
      sel.value = selected || "";
      return { ok: true, count: jobs.length, selected: sel.value };
    },
  });
  return result || { ok: false };
}

// Scrape the visible conversation thread. Uses stable data-test-* hooks (the
// CSS-module class hashes change between builds; these test hooks don't). Each
// message is sender-name → time → body in document order; direction is derived
// client-side from the matched candidate.
async function captureThread(tabId) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    func: () => {
      const clean = (s) => (s || "").replace(/\s+/g, " ").replace(/[•·]\s*$/, "").trim();
      // The whole conversation list — NOT `.a11y-message-thread`, which wraps a
      // single message (there is one per bubble, so it would scrape only one).
      const thread = document.querySelector("[data-test-messages-list]")
        || document.querySelector("[data-test-message-list]")
        || document;
      const nodes = thread.querySelectorAll(
        "[data-test-message-sender-name],[data-test-message-time],[data-test-rich-message-body],[data-test-attachment-id]");
      let sender = "", time = "";
      const messages = [];
      const lastSame = () => {
        const m = messages[messages.length - 1];
        return m && m.sender === sender ? m : null;
      };
      for (const n of nodes) {
        if (n.hasAttribute("data-test-message-sender-name")) sender = clean(n.textContent);
        else if (n.hasAttribute("data-test-message-time")) time = clean(n.textContent);
        else if (n.hasAttribute("data-test-rich-message-body")) {
          const text = clean(n.textContent);
          if (text) messages.push({ sender, time, text, attachments: [] });
        } else {
          // attachment card — download is a <button>, filename is in its aria-label
          const urn = n.getAttribute("data-test-attachment-id") || "";
          const btn = n.querySelector('button[aria-label*="Download" i]');
          let filename = "", ftype = "";
          if (btn) {
            const mm = /Download file:\s*(.+?),\s*Type:\s*([^,]+)/i.exec(btn.getAttribute("aria-label") || "");
            if (mm) { filename = clean(mm[1]); ftype = clean(mm[2]); }
          }
          const att = { urn, filename, type: ftype };
          const m = lastSame();
          if (m) m.attachments.push(att);
          else messages.push({ sender, time, text: "", attachments: [att] });
        }
      }
      const participants = [...new Set(messages.map((m) => m.sender).filter(Boolean))];
      const link = document.querySelector('.a11y-message-thread a[href*="/talent/profile/"]')
        || document.querySelector('a[href*="/talent/profile/"]');
      return { url: location.href, profile_url: link ? link.href : "", participants, messages };
    },
  });
  return result || { messages: [], participants: [] };
}

// Click the download button for a given attachment URN and resolve the saved
// file's path via the downloads API, so the client can relocate it into cv/.
async function downloadAttachment(tabId, urn) {
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId },
    args: [urn],
    func: (urn) => {
      const host = [...document.querySelectorAll("[data-test-attachment-id]")]
        .find((e) => e.getAttribute("data-test-attachment-id") === urn);
      if (!host) return { ok: false, error: "attachment_not_found" };
      const btn = host.querySelector('button[aria-label*="Download" i]');
      if (!btn) return { ok: false, error: "download_button_not_found" };
      btn.click();
      return { ok: true };
    },
  });
  if (!result || result.ok === false) return result || { ok: false, error: "click_failed" };
  // Wait for the browser download that the click kicks off to complete.
  const item = await new Promise((resolve) => {
    const timer = setTimeout(() => { chrome.downloads.onChanged.removeListener(onCh); resolve(null); }, 20000);
    function onCh(delta) {
      if (delta.state && delta.state.current === "complete") {
        chrome.downloads.search({ id: delta.id }, (items) => {
          clearTimeout(timer);
          chrome.downloads.onChanged.removeListener(onCh);
          resolve((items && items[0]) || null);
        });
      }
    }
    chrome.downloads.onChanged.addListener(onCh);
  });
  if (!item) return { ok: false, error: "download_timeout" };
  return { ok: true, path: item.filename, mime: item.mime, bytes: item.fileSize };
}

const PANEL_HTML = `
  <div class="card">
    <div class="hd"><span>✉️ Draft message</span><button class="x" data-plugin-action="unmount" title="close">×</button></div>
    <div class="row"><span class="lbl">Job</span>
      <select class="sel" data-plugin-action="select_job" data-plugin-job><option value="">…</option></select>
    </div>
    <button class="go" data-plugin-action="insert_draft">Insert draft</button>
    <button class="go2" data-plugin-action="capture">Capture thread</button>
    <button class="go2" data-plugin-action="resume">Get resume</button>
    <div class="status" data-plugin-status></div>
  </div>`;

const PANEL_CSS = `
  .card { font: 13px/1.45 -apple-system, system-ui, sans-serif; width: 256px; background: #fff;
          color: #1d2226; border: 1px solid #d0d5dd; border-radius: 10px;
          box-shadow: 0 8px 28px rgba(0,0,0,.18); overflow: hidden; }
  .hd { display:flex; align-items:center; justify-content:space-between; gap:8px;
        padding:10px 12px; font-weight:600; background:#f3f6f8; border-bottom:1px solid #e6e9ec; }
  .x { border:0; background:transparent; font-size:18px; line-height:1; cursor:pointer; color:#56687a; }
  .row { display:flex; align-items:center; gap:8px; padding:10px 12px 4px; }
  .lbl { color:#56687a; font-size:12px; min-width:26px; }
  .sel { flex:1; padding:5px 6px; border:1px solid #d0d5dd; border-radius:6px; font-size:12px;
         background:#fff; color:#1d2226; max-width:190px; }
  .go { margin:8px 12px 4px; width:calc(100% - 24px); padding:8px 12px; cursor:pointer;
        background:#0a66c2; color:#fff; border:0; border-radius:20px; font-weight:600; font-size:13px; }
  .go:hover { background:#004182; }
  .go2 { margin:4px 12px 8px; width:calc(100% - 24px); padding:7px 12px; cursor:pointer;
         background:#fff; color:#0a66c2; border:1px solid #0a66c2; border-radius:20px; font-weight:600; font-size:13px; }
  .go2:hover { background:#eaf1fb; }
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
      let selectedJob = null;
      try {
        const g = await chrome.storage.session.get(JOB_KEY(tab.id));
        selectedJob = g[JOB_KEY(tab.id)] || null;
      } catch (_) {}
      return { type: "messaging_scan_result", ok: true, selectedJob, ...r };
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
    // Scrape the visible conversation thread (the panel "Capture" button routes
    // to the client as a `capture` event, which calls this then dedups+stores).
    messaging_scrape_thread: async (msg, ctx) => {
      const tab = await ctx.getTargetTab(msg);
      if (!tab) return { ok: false, error: "no_tab" };
      const r = await captureThread(tab.id);
      return { type: "thread_scraped", ok: true, ...r };
    },
    // Download one attachment (by URN) via the browser; returns its saved path.
    messaging_download_attachment: async (msg, ctx) => {
      const tab = await ctx.getTargetTab(msg);
      if (!tab) return { ok: false, error: "no_tab" };
      const r = await downloadAttachment(tab.id, msg.urn || "");
      return { type: "attachment_downloaded", ...r };
    },
    messaging_set_jobs: async (msg, ctx) => {
      const tab = await ctx.getTargetTab(msg);
      if (!tab) return { ok: false, error: "no_tab" };
      const r = await setPanelJobs(tab.id, msg.jobs, msg.selected);
      return { type: "jobs_set", ...r };
    },
    messaging_set_tab_job: async (msg, ctx) => {
      const tab = await ctx.getTargetTab(msg);
      if (!tab) return { ok: false, error: "no_tab" };
      const key = JOB_KEY(tab.id);
      if (msg.job) await chrome.storage.session.set({ [key]: msg.job });
      else await chrome.storage.session.remove(key);
      return { type: "tab_job_set", ok: true, job: msg.job || null };
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
