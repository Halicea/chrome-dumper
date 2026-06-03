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
    def __init__(self, base_url: str = DEFAULT_BASE, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(timeout=timeout)

    def __enter__(self) -> "DumperClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _cmd(self, payload: dict, timeout: Optional[float] = None) -> dict:
        params = {"timeout": str(timeout)} if timeout is not None else None
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

    def dump(self, tab_id: Optional[int] = None) -> dict:
        p: dict = {"type": "dump"}
        if tab_id is not None:
            p["tabId"] = tab_id
        return self._cmd(p)
