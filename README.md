# chrome-dumper

Chrome extension + bridge + client for driving a browser tab from a script or REPL: list/open/close tabs, click, type, press keys, scroll, highlight regions, and dump the live DOM.

## Layout

```
extension/           unpacked MV3 extension — load in chrome://extensions
server/              uv project: bridge (WS for the extension + HTTP control API)
client/              uv project: `dumper` CLI + DumperClient library
Makefile             convenience targets
```

## Install the extension

Option A — **isolated Chrome window** (recommended; doesn't touch your real profile):

```bash
make chrome
```

This launches Chrome with `--user-data-dir=./.chrome-profile --load-extension=./extension` so the extension is pre-installed in a throwaway profile. Override the binary with `CHROME=/path/to/chrome`. Wipe the profile with `make clean-chrome`.

Option B — **install into your normal Chrome**:

1. Open `chrome://extensions`, enable **Developer mode**.
2. Click **Load unpacked** and pick the `extension/` folder.
3. The action badge shows `OFF` (red) until the bridge is running, then `ON` (green).

Either way, the extension auto-reconnects to `ws://127.0.0.1:8765` every ~2s, and an alarm keeps the MV3 service worker alive so the WS doesn't die after 30s of idle.

> On Chrome/Chromium 137+ the `--load-extension` switch may be disabled. `make chrome` passes the opt-out flag, but if the badge never appears, fall back to **Load unpacked** (Option B) with the dir `make chrome` prints.

## Run

```bash
make sync     # uv sync both projects
make server   # terminal 1: start the bridge
make client   # terminal 2: launch the REPL
```

One-shot CLI use:

```bash
cd client
uv run dumper tabs
uv run dumper get https://example.com         # open + dump in one shot
uv run dumper click --text "Sign in" --wait   # click a link and await navigation
```

Or use the library: `from chrome_dumper_client import DumperClient`.

## Architecture

```
   Chrome (extension SW) ── ws://127.0.0.1:8765 ──► bridge (server)
                                                       │
                                                       ▼
                          http://127.0.0.1:8766/cmd  ◄── any client
```

The extension dials into the bridge. Clients POST JSON commands to the bridge's HTTP API; the bridge forwards them over the WS and returns the extension's reply.

## REPL

`uv run dumper` (or `make client`) with no subcommand drops into a line-based REPL.

- **Tab-completion** on commands and flags (GNU readline / libedit).
- **History** persisted to `~/.dumper_history` (override with `DUMPER_HISTFILE`).
- **Empty Enter** repeats the last command (prints `(repeating: …)`).
- **Bare number** sets a repeat count for the next command:
  ```
  dumper> 5
  dumper [5x]> scroll
  --- 1/5 --- …
  ```
  Counter resets to 1 after the command runs, on parse errors, and on `--help`.
- **Ctrl-C** during a command aborts and returns to the prompt; Ctrl-D / `quit` / `exit` leaves the REPL.

## CLI / REPL command reference

| Command | What it does |
|---|---|
| `health` | Bridge status + whether the extension is connected |
| `ping` | Round-trip ping to the extension |
| `tabs` | List open tabs (id, title, url; `*` marks active) |
| `open <url> [--no-wait]` | Open a new tab |
| `nav <url> [--tab N] [--no-wait]` | Navigate an existing tab (active by default) |
| `close [--tab N \| <id>…]` | Close active tab, one id, or many |
| `click [--selector <css> \| --text <s>] [--nth N] [--tab N] [--wait]` | Click. No target → click currently focused element. `--wait` only if the click navigates. |
| `mouse-move <x> <y> [--instant] [--duration MS] [--tab N]` | Move the cursor to viewport coords (CSS px, the same space `screenshot` uses). **Glides** along a human-like eased path (slows into the target); `--instant` jumps. Real, trusted `mousemove`s via CDP — triggers `:hover`. Returns the element under the cursor. |
| `mouse-click <x> <y> [--button left\|right\|middle] [--count N \| --double] [--instant] [--duration MS] [--wait] [--tab N]` | Real, trusted click — glides to the target, then press → release. `--instant` to jump. |
| `mouse-drag <x1> <y1> <x2> <y2> [--steps N] [--button B] [--tab N]` | Press at p1, move to p2 in steps, release. |
| `mouse-up\|down\|left\|right [N] [--less \| --more] [--tab N]` | Nudge the cursor in a direction, relative to its current position. Default 10px; `--less` 1px, `--more` 100px, or an explicit `N`. |
| `mouse-scroll [up\|down\|left\|right] [N] [--less \| --more] [--at X Y] [--tab N]` | Real wheel scroll via CDP at the cursor (or `--at X Y`). A trusted wheel event — scrolls custom containers/virtualized lists the JS `scroll` can't. Default 300px; `--less` 100, `--more` 700. |
| `mouse-hide [--tab N]` | Remove the visible cursor overlay. |

The `mouse-*` commands draw a **visible cursor** (a teal ring) into the page at the synthetic pointer's position — CDP input moves a virtual pointer the browser doesn't render, so this overlay shows where it is; clicks add a red ripple. It clears on navigation/reload, or with `mouse-hide`. The wire protocol accepts `"cursor": false` on any `mouse_*` message to suppress it.

To **click an element by description** ("the Edit JD button, top right"), there's no separate command — drive it from an LLM/agent: `screenshot` the viewport, look at the image, estimate the target as a fraction of the viewport, multiply by `innerWidth`/`innerHeight` (get them via `js`), then `mouse-click` at those coords. The bundled `chrome-dumper` subagent documents this flow.
| `type <value> [--selector \| --placeholder \| --label]  [--nth N] [--no-clear] [--submit] [--wait] [--tab N]` | Type into a field. With no target, types into the focused element. `--submit` presses Enter / submits the form. `input` is an alias. |
| `key <KeyName> [--shift] [--ctrl] [--alt] [--meta] [--selector <css>] [--wait] [--tab N]` | Press a key (`Enter`, `Escape`, `ArrowDown`, `a`, …). `Tab` does focus traversal (synthetic Tab events don't move focus in Chrome). |
| `tab [--shift]` | Shortcut for `key Tab [--shift]` |
| `enter [--selector <css>] [--wait] [--tab N]` | Shortcut for `key Enter` |
| `space [--selector <css>] [--wait] [--tab N]` | Shortcut for `key " "` |
| `select --selector <css> \| --text <s> \| --rect x1,y1,x2,y2 \| --from <css> --to <css>  [--no-mouse] [--no-scroll] [--tab N]` | Select text via Selection API (drag-like). Also fires `mousedown`/`mousemove`/`mouseup` so page handlers see the gesture. |
| `select-clear [--tab N]` | Clear current text selection |
| `scroll [up\|down] [--pages F] [--pixels N] [--to top\|bottom\|<css>] [--no-smooth] [--tab N]` | Scroll. Default: down half a viewport, smooth. |
| `highlight --selector <css> \| --text <s> \| --rect x,y,w,h  [--all] [--nth N] [--color #hex] [--label <s>] [--duration MS] [--no-scroll] [--tab N]` | Draw a red rectangle over a region (overlay div, doesn't affect layout). Persistent unless `--duration` set. |
| `clear-highlights [--tab N]` | Remove all overlays |
| `dump [--tab N]` | Dump live DOM (`document.documentElement.outerHTML`) to `<out-dir>/<tabId>_<title>.html` |
| `js '<code>' [--tab N]` | Run JS in the page via CDP (`Runtime.evaluate`, async IIFE — `return` a JSON value). Auto-attaches the debugger; e.g. `js 'return {w: innerWidth, h: innerHeight}'`. |
| `screenshot [--format png\|jpeg] [--quality N] [--rect x,y,w,h \| --selector <css> \| --text <s>] [--out <path>] [--tab N]` | Capture the visible viewport, or just a region. `--selector` / `--text` scroll the element into view first, then crop. PNG by default; saved under `<out-dir>`. |
| `resize [WxH \| preset] [--width N] [--height N] [--left N] [--top N] [--max\|--full\|--min\|--normal] [--tab N]` | Resize/reposition the browser window. Presets: `phone`, `tablet`, `laptop`, `desktop`, `hd`/`1080p`, `half-left`, `half-right`, plus `max`/`full`/`min`. Returns the actual resulting bounds (the OS may clamp). |
| `zoom [PCT] [--in [PCT] \| --out [PCT] \| --reset] [--tab N]` | Page zoom (like Ctrl +/-), via `chrome.tabs.setZoom`. `zoom 150` sets 150%; `--in`/`--out` step relative (default 10 pts); `--reset` → 100%; no arg reports current. Clamped to ~25–500%. |
| `get <url>` | Open the URL, wait for load, dump |
| `wait [seconds]` | Client-side sleep, default 1 |
| `help` | (REPL only) show command list |
| `quit` / `exit` | (REPL only) leave |

CLI globals: `--base-url` (default `http://127.0.0.1:8766`, or `DUMPER_BASE_URL`), `--session <id|name>` (or `DUMPER_SESSION`), `--out-dir` (default `./dumps`, or `DUMPER_OUT_DIR`).

## Sessions (multiple browsers at once)

Each Chrome **profile** that loads the extension is a separate session, so you can drive several browsers from one bridge. A profile is Chrome's isolation boundary, so each session has its own cookies and logins — you sign in once per profile.

- The extension generates a stable id per profile (stored in `chrome.storage.local`) and a human **name** you set in its popup ("work", "test", …).
- Pick a target with `--session <id|name>` on the CLI, `?session=` / `X-Session` on the HTTP API, or `DumperClient(session=...)`. When only one browser is connected the selector is optional.
- `dumper sessions` lists what's connected; in the REPL, `use <name>` sets the target for later commands (`use -` clears it).
- `dumper --session work spawn` launches a browser for that session on demand (runs `make chrome SESSION=work` detached and waits for it to connect). Handy from the REPL when the session you `use`d isn't up yet.

```bash
dumper sessions                       # see connected browsers
dumper --session work spawn           # launch the "work" browser if it isn't running
dumper --session work open https://intranet.local
dumper --session test open https://staging.example.com
```

## HTTP API

The bridge exposes:

- `GET /` (or `/status`) → a tiny live-log page: connected sessions + a stream of commands clients send, filterable by session. Open `http://127.0.0.1:8766/` in a browser.
- `GET /log.json?session=<id|name>&after=<seq>` → JSON feed behind that page (sessions + recent commands)
- `GET /health` → `{ "ok": true, "extension_connected": bool, "sessions": [ { "id", "name", "connected" } ] }`
- `GET /sessions` → `{ "sessions": [ { "id", "name", "connected" } ] }`
- `POST /cmd` → body is a protocol message (below); `?session=<id|name>` picks the browser, `?timeout=<s>` overrides the default 60s
- `GET /events` → SSE stream of CDP `debug_event`s; `?session=<id|name>` and `?tab=<int>` filter it

Status codes: `503` = no extension connected; `504` = extension didn't respond in time; `404` = unknown session selector; `409` = ambiguous (e.g. multiple browsers, no selector given); `400` = bad JSON.

## Wire protocol (JSON over WS)

Every request has an `id`; the matching response echoes it. Requests (server → extension):

```jsonc
{ "id": "...", "type": "ping" }
{ "id": "...", "type": "list_tabs" }
{ "id": "...", "type": "open",     "url": "https://...", "active": true, "waitForLoad": true }
{ "id": "...", "type": "navigate", "url": "https://...", "tabId": 123,   "waitForLoad": true }
{ "id": "...", "type": "close",    "tabId": 123 }                 // or "tabIds": [1,2,3]; omit → active tab
{ "id": "...", "type": "click",    "selector": "a.next", "nth": 0, "waitForLoad": true }
{ "id": "...", "type": "click",    "text": "Sign in", "waitForLoad": true }
// click with no selector/text → clicks document.activeElement
{ "id": "...", "type": "type",     "value": "hello", "selector": "input[name=q]",
                                   "clear": true, "submit": true, "waitForLoad": true }
// type alternatives: "placeholder": "Search", "label": "Email"; omit all → focused field
{ "id": "...", "type": "key",      "key": "Enter", "shift": false, "ctrl": false,
                                   "alt": false, "meta": false, "selector": "...", "waitForLoad": true }
{ "id": "...", "type": "select",   "selector": "p.intro" }
// alternatives: "text": "substring", "from": "<css>", "to": "<css>", "rect": { "x1","y1","x2","y2" }
{ "id": "...", "type": "select_clear" }
{ "id": "...", "type": "scroll",   "direction": "down", "pages": 0.5 }   // or "pixels": 800, "to": "top"|"bottom"|<css>
{ "id": "...", "type": "highlight","selector": ".result", "all": true, "color": "#ff1744",
                                   "label": "hits", "durationMs": 3000 }
{ "id": "...", "type": "clear_highlights" }
{ "id": "...", "type": "dump",     "tabId": 123 }                 // tabId optional → active tab
{ "id": "...", "type": "screenshot", "format": "png", "quality": 85, "tabId": 123 }   // visible viewport
// crop options (any one): "rect": { "x","y","width","height" }, "selector": "<css>", "text": "<s>"
{ "id": "...", "type": "mouse_move",  "x": 400, "y": 300 }                 // viewport CSS px; optional shift/ctrl/alt/meta
// relative nudge: { "type": "mouse_move", "dx": -10, "dy": 0 }  (from the last cursor position)
{ "id": "...", "type": "mouse_click", "x": 400, "y": 300, "button": "left", "count": 1, "waitForLoad": false }
{ "id": "...", "type": "mouse_down",  "x": 400, "y": 300, "button": "left" }   // x/y optional → last cursor pos
{ "id": "...", "type": "mouse_up",    "x": 400, "y": 300, "button": "left" }
{ "id": "...", "type": "mouse_drag",  "x1": 100, "y1": 100, "x2": 400, "y2": 300, "steps": 10, "button": "left" }
{ "id": "...", "type": "mouse_wheel", "deltaY": 300, "x": 400, "y": 300 }   // +deltaY = down; x/y optional → last cursor pos
{ "id": "...", "type": "mouse_hide" }                       // remove the visible cursor overlay
// any mouse_* accepts "cursor": false to suppress the visible-cursor overlay
{ "id": "...", "type": "zoom",        "percent": 150 }     // or "delta": +10 / -10, "reset": true; omit all → report

// mouse_* go through CDP Input.dispatchMouseEvent (real, trusted events) — auto-attaches the debugger (shows the banner)
```

Responses (extension → server):

```jsonc
{ "id": "...", "type": "pong" }
{ "id": "...", "type": "tabs",         "tabs": [ { "id", "url", "title", "active", "windowId" } ] }
{ "id": "...", "type": "opened",       "tabId": 123, "url": "..." }
{ "id": "...", "type": "navigated",    "tabId": 123, "url": "..." }
{ "id": "...", "type": "closed",       "tabIds": [123] }
{ "id": "...", "type": "clicked",      "tabId": 123, "tag": "a", "href": "...", "text": "..." }
{ "id": "...", "type": "typed",        "tabId": 123, "tag": "input", "id": "...", "name": "...", "value": "..." }
{ "id": "...", "type": "key_sent",     "tabId": 123, "key": "Enter" }
            // for Tab: also includes "focused": { tag, id, name, type, text }
{ "id": "...", "type": "selected",     "tabId": 123, "text": "...", "rect": { "x","y","width","height" } }
{ "id": "...", "type": "selection_cleared", "tabId": 123 }
{ "id": "...", "type": "scrolled",     "tabId": 123, "scrollX", "scrollY", "viewport", "docHeight" }
{ "id": "...", "type": "highlighted",  "tabId": 123, "count": 3, "rects": [ { "x","y","width","height" } ] }
{ "id": "...", "type": "highlights_cleared", "tabId": 123 }
{ "id": "...", "type": "dump_result",  "tabId": 123, "url": "...", "title": "...", "html": "<!DOCTYPE html>..." }
{ "id": "...", "type": "screenshot_result", "tabId": 123, "url": "...", "title": "...", "format": "png", "dataUrl": "data:image/png;base64,...", "rect": null }
            // "rect" is the CSS-px region that was cropped, or null for full viewport
{ "id": "...", "type": "mouse_moved",   "tabId": 123, "x": 400, "y": 300, "target": { "tag", "id", "cls", "href", "text" } }
{ "id": "...", "type": "mouse_clicked", "tabId": 123, "x": 400, "y": 300, "button": "left", "count": 1, "target": { … } }
{ "id": "...", "type": "mouse_dragged", "tabId": 123, "from": { "x","y" }, "to": { "x","y" }, "button": "left", "steps": 10 }
{ "id": "...", "type": "mouse_scrolled","tabId": 123, "x": 400, "y": 300, "deltaX": 0, "deltaY": 300 }
{ "id": "...", "type": "zoomed",        "tabId": 123, "percent": 150, "factor": 1.5 }
{ "id": "...", "type": "error",        "error": "..." }
```

## Driving from your own program

Python:

```python
from chrome_dumper_client import DumperClient

with DumperClient() as d:
    d.open("https://example.com", wait=True)
    d.type("claude", placeholder="Search", submit=True, wait=True)
    print(d.dump()["html"][:200])
```

Any other language: POST the JSON above to `http://127.0.0.1:8766/cmd`.

## Notes / limits

- The dump is `document.documentElement.outerHTML` — post-JS live DOM, not the raw HTTP body.
- `chrome://` pages and the Chrome Web Store cannot be scripted or screenshotted (Chrome restriction).
- `screenshot` without crop captures only the **visible viewport**, not the full page. With `--selector` / `--text` the element is scrolled into view first, but content outside the viewport is still inaccessible. For full-page capture you'd need the `chrome.debugger` API (heavier, shows the automation banner) or scroll-and-stitch.
- Synthetic `KeyboardEvent`s don't move focus or trigger native browser shortcuts; `key Tab` is special-cased to walk the focusable list itself. Other keys go to the page's JS handlers.
- One session is tracked per Chrome profile (keyed by a persistent id); a reconnect rebinds the same session. Multiple profiles = multiple concurrent sessions (see [Sessions](#sessions-multiple-browsers-at-once)).
- Highlights are positioned in viewport coordinates and do not follow scroll. Re-highlight after scrolling, or use `--duration`.
