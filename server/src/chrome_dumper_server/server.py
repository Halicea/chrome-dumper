"""
Bridge process.

- WebSocket on WS_PORT: the Chrome extension dials in.
- HTTP on HTTP_PORT: clients POST JSON commands; the bridge forwards them to
  the extension and returns the reply.

Endpoints:
  GET  /health         -> { ok, extension_connected }
  POST /cmd            -> body is a JSON command (see protocol in README);
                          returns the extension's reply or { error }.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import uuid
from typing import Optional

import websockets
from aiohttp import web
from PIL import Image

WS_HOST = os.environ.get("DUMPER_WS_HOST", "127.0.0.1")
WS_PORT = int(os.environ.get("DUMPER_WS_PORT", "8765"))
HTTP_HOST = os.environ.get("DUMPER_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("DUMPER_HTTP_PORT", "8766"))


class Bridge:
    def __init__(self) -> None:
        self.ext_ws: Optional[websockets.WebSocketServerProtocol] = None
        self.pending: dict[str, asyncio.Future] = {}
        # tabId -> set of asyncio.Queue (one per SSE subscriber)
        # None tabId in the set means "subscribe to all tabs"
        self.event_subs: dict[Optional[int], set[asyncio.Queue]] = {}

    def subscribe(self, tab_id: Optional[int]) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2048)
        self.event_subs.setdefault(tab_id, set()).add(q)
        return q

    def unsubscribe(self, tab_id: Optional[int], q: asyncio.Queue) -> None:
        s = self.event_subs.get(tab_id)
        if s:
            s.discard(q)
            if not s:
                self.event_subs.pop(tab_id, None)

    def _dispatch_event(self, msg: dict) -> None:
        tab_id = msg.get("tabId")
        targets = []
        if tab_id is not None:
            targets += list(self.event_subs.get(tab_id, ()))
        targets += list(self.event_subs.get(None, ()))
        for q in targets:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow subscriber — drop event

    async def handle_extension(self, ws) -> None:
        self.ext_ws = ws
        print(f"[+] extension connected: {ws.remote_address}")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mid = msg.get("id")
                if mid and mid in self.pending:
                    self.pending.pop(mid).set_result(msg)
                elif msg.get("type") == "hello":
                    print(f"[i] hello: {msg}")
                elif msg.get("type") == "keepalive":
                    pass  # heartbeat from extension to keep MV3 SW alive
                elif msg.get("type") == "debug_event":
                    self._dispatch_event(msg)
                else:
                    print(f"[?] unsolicited: {msg}")
        finally:
            print("[-] extension disconnected")
            if self.ext_ws is ws:
                self.ext_ws = None

    async def request(self, payload: dict, timeout: float = 60) -> dict:
        if self.ext_ws is None:
            raise RuntimeError("extension not connected")
        mid = payload.get("id") or uuid.uuid4().hex
        payload = {**payload, "id": mid}
        fut = asyncio.get_event_loop().create_future()
        self.pending[mid] = fut
        await self.ext_ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(mid, None)


async def _http_health(bridge: Bridge, _request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "extension_connected": bridge.ext_ws is not None})


def _crop_screenshot(resp: dict) -> dict:
    """Bridge-side crop: if `rect` is set on a screenshot_result, crop the dataUrl."""
    rect = resp.get("rect")
    data_url = resp.get("dataUrl")
    if not rect or not data_url or "," not in data_url:
        return resp
    dpr = float(resp.get("dpr") or 1)
    fmt = (resp.get("format") or "png").lower()
    header, b64 = data_url.split(",", 1)
    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    sx = max(0, int(round(rect["x"] * dpr)))
    sy = max(0, int(round(rect["y"] * dpr)))
    sw = max(1, int(round(rect["width"] * dpr)))
    sh = max(1, int(round(rect["height"] * dpr)))
    right = min(img.width, sx + sw)
    bottom = min(img.height, sy + sh)
    if right <= sx or bottom <= sy:
        return {**resp, "type": "error", "error": "crop_outside_viewport"}
    cropped = img.crop((sx, sy, right, bottom))
    buf = io.BytesIO()
    if fmt == "jpeg":
        cropped.convert("RGB").save(buf, format="JPEG", quality=85)
        mime = "image/jpeg"
    else:
        cropped.save(buf, format="PNG")
        mime = "image/png"
    new_url = f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"
    return {**resp, "dataUrl": new_url}


async def _http_cmd(bridge: Bridge, request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(payload, dict) or "type" not in payload:
        return web.json_response({"error": "missing_type"}, status=400)
    timeout = float(request.query.get("timeout", "60"))
    try:
        resp = await bridge.request(payload, timeout=timeout)
        if resp.get("type") == "screenshot_result" and resp.get("rect"):
            try:
                resp = await asyncio.get_event_loop().run_in_executor(None, _crop_screenshot, resp)
            except Exception as e:
                return web.json_response({"type": "error", "error": f"crop_failed: {e}"}, status=500)
        return web.json_response(resp)
    except RuntimeError as e:
        return web.json_response({"error": str(e)}, status=503)
    except asyncio.TimeoutError:
        return web.json_response({"error": "timeout"}, status=504)


async def _http_events(bridge: Bridge, request: web.Request) -> web.StreamResponse:
    """Server-Sent Events stream of debug_event messages.

    Query string:
      tab=<int>   subscribe to events for one tab only (default: all tabs)
    """
    tab_q = request.query.get("tab")
    tab_id: Optional[int] = int(tab_q) if tab_q else None
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
    await resp.prepare(request)
    q = bridge.subscribe(tab_id)
    try:
        # Initial comment so clients know the stream is open.
        await resp.write(b": connected\n\n")
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=20)
            except asyncio.TimeoutError:
                # Heartbeat — keeps proxies and clients from timing the stream out.
                try:
                    await resp.write(b": keepalive\n\n")
                except (ConnectionResetError, asyncio.CancelledError):
                    break
                continue
            try:
                await resp.write(f"data: {json.dumps(msg)}\n\n".encode())
            except (ConnectionResetError, asyncio.CancelledError):
                break
    finally:
        bridge.unsubscribe(tab_id, q)
    return resp


def _make_app(bridge: Bridge) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/health", lambda r: _http_health(bridge, r))
    app.router.add_post("/cmd", lambda r: _http_cmd(bridge, r))
    app.router.add_get("/events", lambda r: _http_events(bridge, r))
    return app


async def run(
    ws_host: str = WS_HOST, ws_port: int = WS_PORT,
    http_host: str = HTTP_HOST, http_port: int = HTTP_PORT,
) -> None:
    bridge = Bridge()
    # Screenshot data URLs can be several MB; default 1 MiB frame limit is too small.
    ws_server = await websockets.serve(
        bridge.handle_extension, ws_host, ws_port, max_size=64 * 1024 * 1024,
    )
    runner = web.AppRunner(_make_app(bridge))
    await runner.setup()
    site = web.TCPSite(runner, http_host, http_port)
    await site.start()
    print(f"WS  for extension: ws://{ws_host}:{ws_port}")
    print(f"HTTP for clients : http://{http_host}:{http_port}  (POST /cmd, GET /health)")
    try:
        await asyncio.Future()
    finally:
        ws_server.close()
        await ws_server.wait_closed()
        await runner.cleanup()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--ws-host", default=WS_HOST)
    p.add_argument("--ws-port", type=int, default=WS_PORT)
    p.add_argument("--http-host", default=HTTP_HOST)
    p.add_argument("--http-port", type=int, default=HTTP_PORT)
    args = p.parse_args()
    try:
        asyncio.run(run(args.ws_host, args.ws_port, args.http_host, args.http_port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
