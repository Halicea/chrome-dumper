"""
Programmatic client for the chrome-dumper bridge.

    from chrome_dumper_client import DumperClient

    with DumperClient() as d:
        d.open("https://example.com", wait=True)
        html = d.dump()["html"]
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

DEFAULT_BASE = "http://127.0.0.1:8766"


class DumperClient:
    def __init__(self, base_url: str = DEFAULT_BASE, timeout: float = 20.0,
                 session: Optional[str] = None) -> None:
        self.base_url = base_url.rstrip("/")
        # Target browser session (id or name). None = let the bridge pick when
        # exactly one session is connected.
        self.session = session
        self._http = httpx.Client(timeout=timeout)

    def __enter__(self) -> "DumperClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _params(self, timeout: Optional[float] = None) -> Optional[dict]:
        params: dict = {}
        if timeout is not None:
            params["timeout"] = str(timeout)
        if self.session:
            params["session"] = self.session
        return params or None

    def _cmd(self, payload: dict, timeout: Optional[float] = None) -> dict:
        params = self._params(timeout)
        try:
            r = self._http.post(f"{self.base_url}/cmd", json=payload, params=params)
        except httpx.ConnectError as e:
            raise RuntimeError(
                f"cannot reach bridge at {self.base_url} — is `make server` running? ({e})"
            ) from e
        if r.status_code == 503:
            raise RuntimeError(
                "extension not connected to bridge. Open chrome://extensions, "
                "make sure 'HTML Dumper' is enabled and reload it. The action "
                "icon badge should turn green (ON). If it stays OFF, click the "
                "extension's 'service worker' link to wake it and check devtools."
            )
        if r.status_code in (404, 409):
            # Bad/ambiguous session selector — surface the bridge's explanation.
            try:
                raise RuntimeError(r.json().get("error") or r.text)
            except ValueError:
                raise RuntimeError(r.text)
        if r.status_code == 504:
            raise RuntimeError("extension timed out responding (page may be loading or stuck)")
        r.raise_for_status()
        data = r.json()
        if data.get("type") == "error":
            raise RuntimeError(f"extension error: {data.get('error')}")
        return data

    def health(self) -> dict:
        r = self._http.get(f"{self.base_url}/health")
        r.raise_for_status()
        return r.json()

    def sessions(self) -> list[dict]:
        """List the browser sessions currently connected to the bridge."""
        r = self._http.get(f"{self.base_url}/sessions")
        r.raise_for_status()
        return r.json()["sessions"]

    def ping(self) -> dict:
        return self._cmd({"type": "ping"})

    def list_tabs(self) -> list[dict]:
        return self._cmd({"type": "list_tabs"})["tabs"]

    def open(self, url: str, active: bool = True, wait: bool = True) -> dict:
        return self._cmd({"type": "open", "url": url, "active": active, "waitForLoad": wait})

    def navigate(self, url: str, tab_id: Optional[int] = None, wait: bool = True) -> dict:
        p: dict = {"type": "navigate", "url": url, "waitForLoad": wait}
        if tab_id is not None:
            p["tabId"] = tab_id
        return self._cmd(p)

    def back(self, steps: int = 1, tab_id: Optional[int] = None, wait: bool = True) -> dict:
        p: dict = {"type": "back", "steps": steps, "waitForLoad": wait}
        if tab_id is not None:
            p["tabId"] = tab_id
        return self._cmd(p)

    def forward(self, steps: int = 1, tab_id: Optional[int] = None, wait: bool = True) -> dict:
        p: dict = {"type": "forward", "steps": steps, "waitForLoad": wait}
        if tab_id is not None:
            p["tabId"] = tab_id
        return self._cmd(p)

    def click(self, selector: Optional[str] = None, text: Optional[str] = None,
              nth: int = 0, tab_id: Optional[int] = None, wait: bool = False) -> dict:
        p: dict = {"type": "click", "nth": nth, "waitForLoad": wait}
        if selector:
            p["selector"] = selector
        if text:
            p["text"] = text
        if tab_id is not None:
            p["tabId"] = tab_id
        return self._cmd(p)

    def mouse_move(self, x: Optional[float] = None, y: Optional[float] = None, *,
                   dx: Optional[float] = None, dy: Optional[float] = None,
                   shift: bool = False, ctrl: bool = False,
                   alt: bool = False, meta: bool = False, tab_id: Optional[int] = None) -> dict:
        """Move the virtual cursor. Absolute: pass ``x``/``y`` (viewport CSS px,
        as seen in a screenshot). Relative: pass ``dx``/``dy`` to nudge from the
        last cursor position. Fires a real, trusted mousemove via CDP — triggers
        :hover. Returns the element currently under the cursor."""
        p: dict = {"type": "mouse_move", "shift": shift, "ctrl": ctrl, "alt": alt, "meta": meta}
        if dx is not None or dy is not None:
            p["dx"] = dx or 0
            p["dy"] = dy or 0
        else:
            p["x"] = x
            p["y"] = y
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def mouse_hide(self, *, tab_id: Optional[int] = None) -> dict:
        """Remove the visible cursor overlay drawn by the mouse commands."""
        p: dict = {"type": "mouse_hide"}
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def mouse_nudge(self, direction: str, step: float, *, tab_id: Optional[int] = None) -> dict:
        """Move the cursor ``step`` CSS px in a direction (up/down/left/right)
        relative to its current position."""
        dx = {"left": -step, "right": step}.get(direction, 0)
        dy = {"up": -step, "down": step}.get(direction, 0)
        return self.mouse_move(dx=dx, dy=dy, tab_id=tab_id)

    def mouse_click(self, x: float, y: float, *, button: str = "left", count: int = 1,
                    shift: bool = False, ctrl: bool = False, alt: bool = False,
                    meta: bool = False, wait: bool = False, tab_id: Optional[int] = None) -> dict:
        """Click at viewport coords (CSS px) with a real, trusted gesture
        (move → press → release). ``button`` is left|right|middle; ``count`` for
        double/triple click. ``wait`` waits for load if the click navigates."""
        p: dict = {"type": "mouse_click", "x": x, "y": y, "button": button, "count": count,
                   "shift": shift, "ctrl": ctrl, "alt": alt, "meta": meta, "waitForLoad": wait}
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def mouse_down(self, x: Optional[float] = None, y: Optional[float] = None, *,
                   button: str = "left", tab_id: Optional[int] = None) -> dict:
        """Press a mouse button (no release). Omit x/y to use the last cursor position."""
        p: dict = {"type": "mouse_down", "button": button}
        if x is not None: p["x"] = x
        if y is not None: p["y"] = y
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def mouse_up(self, x: Optional[float] = None, y: Optional[float] = None, *,
                 button: str = "left", tab_id: Optional[int] = None) -> dict:
        """Release a mouse button (no prior press). Omit x/y to use the last cursor position."""
        p: dict = {"type": "mouse_up", "button": button}
        if x is not None: p["x"] = x
        if y is not None: p["y"] = y
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def mouse_drag(self, x1: float, y1: float, x2: float, y2: float, *, steps: int = 10,
                   button: str = "left", tab_id: Optional[int] = None) -> dict:
        """Drag from (x1,y1) to (x2,y2) in viewport coords: press, move in steps, release."""
        p: dict = {"type": "mouse_drag", "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                   "steps": steps, "button": button}
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def key(self, key: str, *, shift: bool = False, ctrl: bool = False,
            alt: bool = False, meta: bool = False, selector: Optional[str] = None,
            wait: bool = False, tab_id: Optional[int] = None) -> dict:
        p: dict = {"type": "key", "key": key, "shift": shift, "ctrl": ctrl,
                   "alt": alt, "meta": meta, "waitForLoad": wait}
        if selector: p["selector"] = selector
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def tab_key(self, *, shift: bool = False, tab_id: Optional[int] = None) -> dict:
        return self.key("Tab", shift=shift, tab_id=tab_id)

    def focus(self, *, selector: Optional[str] = None, text: Optional[str] = None,
              nth: int = 0, tab_id: Optional[int] = None) -> dict:
        """Focus on an element identified by selector or text content.
        If the element isn't directly focusable, focuses on the first focusable parent."""
        p: dict = {"type": "focus", "nth": nth}
        if selector: p["selector"] = selector
        if text: p["text"] = text
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def select(self, *, selector: Optional[str] = None, text: Optional[str] = None,
               from_: Optional[str] = None, to: Optional[str] = None,
               rect: Optional[dict] = None, dispatch_mouse: bool = True,
               scroll: bool = True, focus: bool = True,
               tab_id: Optional[int] = None) -> dict:
        p: dict = {"type": "select", "dispatchMouse": dispatch_mouse, "scroll": scroll, "focus": focus}
        if selector: p["selector"] = selector
        if text: p["text"] = text
        if from_: p["from"] = from_
        if to: p["to"] = to
        if rect: p["rect"] = rect
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def select_clear(self, tab_id: Optional[int] = None) -> dict:
        p: dict = {"type": "select_clear"}
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def scroll(self, direction: str = "down", *, pages: Optional[float] = None,
               pixels: Optional[int] = None, to: Optional[str] = None,
               smooth: bool = True, tab_id: Optional[int] = None) -> dict:
        p: dict = {"type": "scroll", "direction": direction, "smooth": smooth}
        if pages is not None: p["pages"] = pages
        if pixels is not None: p["pixels"] = pixels
        if to: p["to"] = to
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def highlight(self, *, selector: Optional[str] = None, text: Optional[str] = None,
                  rect: Optional[dict] = None, all: bool = False, nth: int = 0,
                  color: str = "#ff1744", label: Optional[str] = None,
                  duration_ms: int = 0, scroll: bool = True,
                  tab_id: Optional[int] = None) -> dict:
        p: dict = {"type": "highlight", "all": all, "nth": nth, "color": color,
                   "durationMs": duration_ms, "scroll": scroll}
        if selector: p["selector"] = selector
        if text: p["text"] = text
        if rect: p["rect"] = rect
        if label: p["label"] = label
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def clear_highlights(self, tab_id: Optional[int] = None) -> dict:
        p: dict = {"type": "clear_highlights"}
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def type(self, value: str, *, selector: Optional[str] = None,
             placeholder: Optional[str] = None, label: Optional[str] = None,
             nth: int = 0, clear: bool = True, submit: bool = False,
             tab_id: Optional[int] = None, wait: bool = False) -> dict:
        p: dict = {"type": "type", "value": value, "nth": nth, "clear": clear,
                   "submit": submit, "waitForLoad": wait}
        if selector: p["selector"] = selector
        if placeholder: p["placeholder"] = placeholder
        if label: p["label"] = label
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def close_tab(self, tab_id: Optional[int] = None, tab_ids: Optional[list[int]] = None) -> dict:
        p: dict = {"type": "close"}
        if tab_ids:
            p["tabIds"] = tab_ids
        elif tab_id is not None:
            p["tabId"] = tab_id
        return self._cmd(p)

    def screenshot(self, *, format: str = "png", quality: int = 85,
                   rect: Optional[dict] = None, selector: Optional[str] = None,
                   text: Optional[str] = None, tab_id: Optional[int] = None) -> dict:
        p: dict = {"type": "screenshot", "format": format, "quality": quality}
        if rect: p["rect"] = rect
        if selector: p["selector"] = selector
        if text: p["text"] = text
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p, timeout=30.0)

    def resize(self, *, width: Optional[int] = None, height: Optional[int] = None,
               left: Optional[int] = None, top: Optional[int] = None,
               state: Optional[str] = None, half: Optional[str] = None,
               tab_id: Optional[int] = None) -> dict:
        p: dict = {"type": "resize"}
        if width is not None: p["width"] = width
        if height is not None: p["height"] = height
        if left is not None: p["left"] = left
        if top is not None: p["top"] = top
        if state: p["state"] = state
        if half: p["half"] = half
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def zoom(self, percent: Optional[float] = None, *, delta: Optional[float] = None,
             reset: bool = False, tab_id: Optional[int] = None) -> dict:
        """Page zoom (like Ctrl +/-). ``percent`` sets an absolute level (150 =
        150%); ``delta`` steps relative to the current level (+10 / -10);
        ``reset`` returns to 100%. With no argument, just reports the current
        zoom. Returns ``{percent, factor}``."""
        p: dict = {"type": "zoom"}
        if reset: p["reset"] = True
        elif percent is not None: p["percent"] = percent
        elif delta is not None: p["delta"] = delta
        if tab_id is not None: p["tabId"] = tab_id
        return self._cmd(p)

    def dump(self, tab_id: Optional[int] = None) -> dict:
        p: dict = {"type": "dump"}
        if tab_id is not None:
            p["tabId"] = tab_id
        return self._cmd(p)

    def js(self, code: str, *, args: Any = None, tab_id: Optional[int] = None,
           timeout: Optional[float] = None) -> dict:
        """Run caller-supplied JavaScript in a tab via CDP ``Runtime.evaluate`` —
        in the page's MAIN realm, in the live logged-in session (cookies + csrf are
        the page's), exempt from the page CSP.

        ``code`` is wrapped in an async IIFE, so it can ``await`` (e.g.
        ``await fetch(...)``) and ``return`` — the resolved value comes back under
        ``result`` (``{"ok":true,"result":...}``); a thrown error → ``{"ok":false,
        "error":...}``. The optional ``args`` JSON value is exposed as ``args``.

        This auto-attaches the Chrome debugger to the tab (the "being debugged"
        banner appears) and leaves it attached for reuse; call ``detach`` (or
        ``dumper detach``) when done. CAN write (save/archive/message) — unlike the
        read-only pipeline export. Use deliberately.

        (A second, no-attach best-effort variant exists as the bridge ``js``
        command — userScripts-based, synchronous-only — but CDP eval is the
        reliable path and what this method uses.)
        """
        p: dict = {"type": "debug_eval", "code": code}
        if args is not None:
            p["args"] = args
        if tab_id is not None:
            p["tabId"] = tab_id
        return self._cmd(p, timeout=timeout)

    def detach(self, tab_id: Optional[int] = None) -> dict:
        """Detach the Chrome debugger from a tab (removes the banner). Pairs with
        the auto-attach that ``js`` performs."""
        p: dict = {"type": "debug_detach"}
        if tab_id is not None:
            p["tabId"] = tab_id
        return self._cmd(p)
