"""
Bridge process.

- WebSocket on WS_PORT: the Chrome extension dials in.
- HTTP on HTTP_PORT: clients POST JSON commands; the bridge forwards them to
  the extension and returns the reply.

Multiple Chrome profiles can connect at once. Each extension instance
announces a stable `sessionId` (plus an optional human `name`) in its hello;
the bridge tracks one connection per session. Clients pick a target session
with `?session=<id|name>` (or the `X-Session` header). With exactly one
session connected, the selector is optional.

Endpoints:
  GET  /health         -> { ok, extension_connected, sessions: [...] }
  GET  /sessions       -> { sessions: [ { id, name, connected } ] }
  POST /cmd            -> body is a JSON command (see protocol in README);
                          returns the extension's reply or { error }.
                          ?session=<id|name> selects the target browser.
  GET  /events         -> SSE stream of debug_event messages.
                          ?session=<id|name> and ?tab=<int> filter the stream.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import time
import uuid
from collections import deque
from typing import Optional

import websockets
from aiohttp import web
from PIL import Image

WS_HOST = os.environ.get("DUMPER_WS_HOST", "127.0.0.1")
WS_PORT = int(os.environ.get("DUMPER_WS_PORT", "8765"))
HTTP_HOST = os.environ.get("DUMPER_HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("DUMPER_HTTP_PORT", "8766"))

# Self-contained status page served at GET / — polls /log.json once a second.
_STATUS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>chrome-dumper · live log</title><style>
 body{font:13px/1.45 system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}
 header{padding:10px 14px;background:#171a21;border-bottom:1px solid #262b36;position:sticky;top:0;z-index:1}
 h1{font-size:14px;margin:0 0 6px;font-weight:600}
 .sess{display:inline-block;margin:2px 6px 2px 0;padding:2px 8px;border-radius:10px;background:#222834;font-size:12px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#2e7d32;margin-right:5px}
 select{background:#222834;color:#e6e6e6;border:1px solid #333b49;border-radius:4px;padding:3px 6px}
 table{width:100%;border-collapse:collapse}
 th,td{text-align:left;padding:4px 10px;border-bottom:1px solid #1d222b;white-space:nowrap;vertical-align:top}
 th{color:#8b93a3;font-weight:500;font-size:11px;text-transform:uppercase}
 td.detail{white-space:normal;color:#aeb6c2;max-width:620px;overflow-wrap:anywhere}
 .err{color:#ff6b6b}.ok{color:#7bd88f}.name{color:#9ecbff}.muted{color:#6b7280}
 tbody tr:hover{background:#161a22}
</style></head><body>
<header>
 <h1>chrome-dumper · live log</h1>
 <div id="sessions" class="muted">connecting…</div>
 <div style="margin-top:6px">
   filter: <select id="filter"><option value="">all sessions</option></select>
   <span id="count" class="muted"></span>
   <label style="margin-left:12px"><input type="checkbox" id="paused"> pause</label>
 </div>
</header>
<table><thead><tr>
 <th>time</th><th>session</th><th>cmd</th><th>status</th><th>ms</th><th>detail</th>
</tr></thead><tbody id="rows"></tbody></table>
<script>
let after=0, filter="", paused=false, total=0;
const rows=document.getElementById('rows'), filterEl=document.getElementById('filter'),
      sessEl=document.getElementById('sessions'), countEl=document.getElementById('count'),
      pausedEl=document.getElementById('paused');
filterEl.onchange=()=>{filter=filterEl.value;after=0;total=0;rows.innerHTML='';};
pausedEl.onchange=()=>{paused=pausedEl.checked;};
const esc=s=>(s||'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
const fmt=ts=>new Date(ts*1000).toLocaleTimeString();
function addRow(e){
  const st=e.ok?'<span class=ok>ok</span>':'<span class=err>'+esc(e.status||'error')+'</span>';
  const tr=document.createElement('tr');
  tr.innerHTML='<td class=muted>'+fmt(e.ts)+'</td><td class=name>'+esc(e.name)+'</td><td>'
    +esc(e.type)+'</td><td>'+st+'</td><td class=muted>'+(e.ms!=null?Math.round(e.ms):'')
    +'</td><td class=detail>'+esc(e.detail)+'</td>';
  rows.insertBefore(tr, rows.firstChild);
  while(rows.children.length>800) rows.removeChild(rows.lastChild);
}
async function tick(){
  if(paused) return;
  try{
    const d=await (await fetch('/log.json?'+new URLSearchParams({after, session:filter}))).json();
    after=d.seq;
    sessEl.innerHTML = d.sessions.length
      ? d.sessions.map(s=>'<span class=sess><span class=dot></span>'+esc(s.name)+' <span class=muted>'+esc(s.id.slice(0,8))+'</span></span>').join('')
      : '<span class=muted>no browsers connected</span>';
    for(const s of d.sessions)
      if(![...filterEl.options].some(o=>o.value===s.id)){
        const o=document.createElement('option');o.value=s.id;o.textContent=s.name+' ('+s.id.slice(0,8)+')';filterEl.appendChild(o);
      }
    d.entries.forEach(addRow); total+=d.entries.length;
    countEl.textContent=total?('· '+total+' shown'):'';
  }catch(e){ sessEl.innerHTML='<span class=err>bridge unreachable</span>'; }
}
setInterval(tick,1000); tick();
</script></body></html>
"""


class SessionLookup(RuntimeError):
    """Raised when a request can't be matched to exactly one session.

    `status` is the HTTP status the bridge should return for it.
    """
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class Session:
    """One connected extension instance (one Chrome profile)."""
    def __init__(self, sid: str, name: str, ws) -> None:
        self.sid = sid
        self.name = name
        self.ws = ws
        self.pending: dict[str, asyncio.Future] = {}
        # tabId -> set of asyncio.Queue (one per SSE subscriber)
        # None tabId in the set means "subscribe to all tabs"
        self.event_subs: dict[Optional[int], set[asyncio.Queue]] = {}

    def info(self) -> dict:
        return {"id": self.sid, "name": self.name, "connected": True}


class Bridge:
    def __init__(self) -> None:
        # sessionId -> Session
        self.sessions: dict[str, Session] = {}
        # rolling log of commands for the status page (newest seq highest)
        self.log: deque = deque(maxlen=2000)
        self.log_seq = 0

    def record(self, session: "Session", payload: dict, ok: bool,
               status: Optional[str], ms: float) -> None:
        self.log_seq += 1
        self.log.append({
            "seq": self.log_seq,
            "ts": time.time(),
            "session": session.sid,
            "name": session.name,
            "type": payload.get("type"),
            "ok": ok,
            "status": status,
            "ms": round(ms, 1),
            "detail": _cmd_detail(payload),
        })

    # ---- session registry ----

    def register(self, sid: str, name: Optional[str], ws) -> Session:
        """Create a session, or rebind an existing id to a new socket (reconnect)."""
        existing = self.sessions.get(sid)
        if existing is not None:
            existing.ws = ws
            if name:
                existing.name = name
            return existing
        session = Session(sid, name or sid[:8], ws)
        self.sessions[sid] = session
        return session

    def unregister(self, session: Session) -> None:
        # Only drop it if this socket is still the current one for the id;
        # a reconnect may have already replaced it.
        if self.sessions.get(session.sid) is session:
            self.sessions.pop(session.sid, None)
        for fut in session.pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("extension disconnected"))
        session.pending.clear()

    def resolve(self, selector: Optional[str]) -> Session:
        """Find the target session by id or name.

        With no selector and exactly one session, that one is used. Otherwise a
        SessionLookup is raised carrying the right HTTP status.
        """
        if not self.sessions:
            raise SessionLookup("extension not connected", 503)
        if selector:
            if selector in self.sessions:
                return self.sessions[selector]
            matches = [s for s in self.sessions.values() if s.name == selector]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise SessionLookup(f"no session with id or name '{selector}'", 404)
            ids = ", ".join(s.sid for s in matches)
            raise SessionLookup(
                f"session name '{selector}' is ambiguous; use one of these ids: {ids}", 409)
        if len(self.sessions) == 1:
            return next(iter(self.sessions.values()))
        listing = ", ".join(f"{s.name} ({s.sid[:8]})" for s in self.sessions.values())
        raise SessionLookup(
            f"multiple sessions connected; specify ?session=<id|name>  [{listing}]", 409)

    # ---- event fan-out (per session) ----

    def subscribe(self, session: Session, tab_id: Optional[int]) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2048)
        session.event_subs.setdefault(tab_id, set()).add(q)
        return q

    def unsubscribe(self, session: Session, tab_id: Optional[int], q: asyncio.Queue) -> None:
        s = session.event_subs.get(tab_id)
        if s:
            s.discard(q)
            if not s:
                session.event_subs.pop(tab_id, None)

    def _dispatch_event(self, session: Session, msg: dict) -> None:
        tab_id = msg.get("tabId")
        targets = []
        if tab_id is not None:
            targets += list(session.event_subs.get(tab_id, ()))
        targets += list(session.event_subs.get(None, ()))
        for q in targets:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow subscriber — drop event

    # ---- extension socket ----

    async def handle_extension(self, ws) -> None:
        session: Optional[Session] = None
        print(f"[+] extension connected: {ws.remote_address}")
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "hello":
                    sid = msg.get("sessionId") or uuid.uuid4().hex
                    session = self.register(sid, msg.get("name"), ws)
                    print(f"[i] hello: session={session.name} ({session.sid}) "
                          f"agent={msg.get('agent')}")
                    continue
                if session is None:
                    # Pre-hello message (e.g. an older extension): give it an
                    # anonymous session so it's still addressable.
                    session = self.register(uuid.uuid4().hex, None, ws)
                mid = msg.get("id")
                if mid and mid in session.pending:
                    session.pending.pop(mid).set_result(msg)
                elif msg.get("type") == "keepalive":
                    pass  # heartbeat from extension to keep MV3 SW alive
                elif msg.get("type") == "debug_event":
                    self._dispatch_event(session, msg)
                else:
                    print(f"[?] unsolicited: {msg}")
        finally:
            if session is not None:
                print(f"[-] extension disconnected: {session.name} ({session.sid})")
                self.unregister(session)
            else:
                print("[-] extension disconnected (no hello received)")

    async def request(self, session: Session, payload: dict, timeout: float = 60) -> dict:
        mid = payload.get("id") or uuid.uuid4().hex
        payload = {**payload, "id": mid}
        fut = asyncio.get_event_loop().create_future()
        session.pending[mid] = fut
        await session.ws.send(json.dumps(payload))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            session.pending.pop(mid, None)


def _select_session(bridge: Bridge, request: web.Request) -> Session:
    selector = request.query.get("session") or request.headers.get("X-Session")
    return bridge.resolve(selector)


async def _http_health(bridge: Bridge, _request: web.Request) -> web.Response:
    sessions = [s.info() for s in bridge.sessions.values()]
    return web.json_response({
        "ok": True,
        "extension_connected": len(sessions) > 0,
        "sessions": sessions,
    })


async def _http_sessions(bridge: Bridge, _request: web.Request) -> web.Response:
    return web.json_response({"sessions": [s.info() for s in bridge.sessions.values()]})


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
    try:
        session = _select_session(bridge, request)
    except SessionLookup as e:
        return web.json_response({"error": str(e)}, status=e.status)
    timeout = float(request.query.get("timeout", "60"))
    t0 = time.monotonic()
    try:
        resp = await bridge.request(session, payload, timeout=timeout)
        ok = resp.get("type") != "error"
        bridge.record(session, payload, ok,
                      None if ok else str(resp.get("error")), (time.monotonic() - t0) * 1000)
        if resp.get("type") == "screenshot_result" and resp.get("rect"):
            try:
                resp = await asyncio.get_event_loop().run_in_executor(None, _crop_screenshot, resp)
            except Exception as e:
                return web.json_response({"type": "error", "error": f"crop_failed: {e}"}, status=500)
        return web.json_response(resp)
    except RuntimeError as e:
        bridge.record(session, payload, False, "no_extension", (time.monotonic() - t0) * 1000)
        return web.json_response({"error": str(e)}, status=503)
    except asyncio.TimeoutError:
        bridge.record(session, payload, False, "timeout", (time.monotonic() - t0) * 1000)
        return web.json_response({"error": "timeout"}, status=504)


async def _http_events(bridge: Bridge, request: web.Request) -> web.StreamResponse:
    """Server-Sent Events stream of debug_event messages.

    Query string:
      session=<id|name>   pick the source browser (optional if only one)
      tab=<int>           subscribe to events for one tab only (default: all tabs)
    """
    try:
        session = _select_session(bridge, request)
    except SessionLookup as e:
        return web.json_response({"error": str(e)}, status=e.status)
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
    q = bridge.subscribe(session, tab_id)
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
        bridge.unsubscribe(session, tab_id, q)
    return resp


def _cmd_detail(payload: dict) -> str:
    """A short human summary of a command for the status log."""
    for k in ("url", "selector", "text", "value", "placeholder", "label", "to", "key", "requestId"):
        v = payload.get(k)
        if v:
            return f"{k}={str(v)[:80]}"
    if payload.get("tabIds"):
        return f"tabs={payload['tabIds']}"
    if payload.get("tabId"):
        return f"tab={payload['tabId']}"
    return ""


async def _http_log(bridge: Bridge, request: web.Request) -> web.Response:
    """JSON feed for the status page: connected sessions + recent commands.

    Query string:
      session=<id|name>   only commands for this session (default: all)
      after=<seq>         only commands newer than this seq (for incremental polls)
    """
    sel = request.query.get("session") or ""
    try:
        after = int(request.query.get("after", "0"))
    except ValueError:
        after = 0
    entries = [e for e in bridge.log
               if e["seq"] > after and (not sel or sel in (e["session"], e["name"]))]
    return web.json_response({
        "sessions": [s.info() for s in bridge.sessions.values()],
        "entries": entries,
        "seq": bridge.log_seq,
    })


async def _http_status(_request: web.Request) -> web.Response:
    return web.Response(text=_STATUS_HTML, content_type="text/html")


def _make_app(bridge: Bridge) -> web.Application:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/", _http_status)
    app.router.add_get("/status", _http_status)
    app.router.add_get("/log.json", lambda r: _http_log(bridge, r))
    app.router.add_get("/health", lambda r: _http_health(bridge, r))
    app.router.add_get("/sessions", lambda r: _http_sessions(bridge, r))
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
