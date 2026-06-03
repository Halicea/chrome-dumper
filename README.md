# chrome-dumper

Chrome extension + bridge + client for driving a browser tab from a script or REPL: list/open/close tabs, click, type, press keys, scroll, highlight regions, and dump the live DOM.

## Layout

```
extension/           unpacked MV3 extension — load in chrome://extensions
firefox-extension/   unpacked MV3 extension for Firefox (about:debugging)
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

### Firefox

A parallel port lives in `firefox-extension/`. It speaks the same wire protocol against the same bridge — load it in Firefox via:

```bash
make firefox     # uses web-ext if installed, otherwise launches Firefox at about:debugging
```

…or manually: open `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on…** → pick `firefox-extension/manifest.json`.

Caveats vs. the Chrome build:

- `debug_*` commands (CDP attach, network capture, request pause/continue) are **not supported** on Firefox — there is no `browser.debugger` equivalent. The extension replies with `error: "debug_not_supported_on_firefox"` (and `debug_status` returns an empty list with `supported: false`).
- Everything else — `tabs`, `open`, `navigate`, `back/forward`, `click`, `type`, `key`, `select`, `scroll`, `highlight`, `dump`, `screenshot` — works identically.

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
| `screenshot [--format png\|jpeg] [--quality N] [--rect x,y,w,h \| --selector <css> \| --text <s>] [--out <path>] [--tab N]` | Capture the visible viewport, or just a region. `--selector` / `--text` scroll the element into view first, then crop. PNG by default; saved under `<out-dir>`. |
| `get <url>` | Open the URL, wait for load, dump |
| `wait [seconds]` | Client-side sleep, default 1 |
| `help` | (REPL only) show command list |
| `quit` / `exit` | (REPL only) leave |

CLI globals: `--base-url` (default `http://127.0.0.1:8766`, or `DUMPER_BASE_URL`), `--out-dir` (default `./dumps`, or `DUMPER_OUT_DIR`).

## HTTP API

The bridge exposes:

- `GET /health` → `{ "ok": true, "extension_connected": bool }`
- `POST /cmd` → body is a protocol message (below); query `?timeout=<s>` overrides the default 60s

Status codes: `503` = extension not connected; `504` = extension didn't respond in time; `400` = bad JSON.

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
- Only one extension instance is tracked; if Chrome reconnects, the latest wins.
- Highlights are positioned in viewport coordinates and do not follow scroll. Re-highlight after scrolling, or use `--duration`.
