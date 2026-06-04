"""CDP / Chrome Debugger Protocol commands for the dumper CLI.

Adds subcommands:
  attach   [--tab N] [--no-network] [--fetch [--fetch-pattern P]…] [--console]
  detach   [--tab N]
  status
  listen   [--tab N] [--filter REGEX] [--method REGEX] [--include-body]
  console  [--tab N]                       (alias: listen --console-only)
  pause    [--tab N] [--url-pattern P]…    (interactive request interception)
  body     [--tab N] --request-id ID

Network events stream from the bridge's /events SSE endpoint.
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Iterator, Optional

import httpx

from .client import DumperClient


# ---------- argparse wiring ----------

def register(sub) -> None:
    """Add debug subcommands to the existing argparse subparser group."""
    p = sub.add_parser("attach", help="attach Chrome debugger to a tab")
    p.add_argument("--tab", type=int)
    p.add_argument("--no-network", action="store_true", help="don't enable Network domain")
    p.add_argument("--console", action="store_true", help="also enable Runtime (console logs)")
    p.add_argument("--fetch", action="store_true", help="enable Fetch domain (request interception)")
    p.add_argument("--fetch-pattern", action="append", default=None,
                   help="URL glob for Fetch (repeatable; default '*')")

    p = sub.add_parser("detach", help="detach Chrome debugger from a tab")
    p.add_argument("--tab", type=int)

    sub.add_parser("status", help="list tabs the debugger is currently attached to")

    p = sub.add_parser("listen", help="stream CDP events from an attached tab")
    p.add_argument("--tab", type=int)
    p.add_argument("--filter", help="regex applied to event URL (when present)")
    p.add_argument("--method", help="regex applied to CDP method name (e.g. Network.responseReceived)")
    p.add_argument("--include-body", action="store_true",
                   help="after responseReceived, fetch and inline the response body")
    p.add_argument("--raw", action="store_true",
                   help="print full JSON (default: condensed one-line summary)")

    p = sub.add_parser("body", help="fetch a response body by requestId")
    p.add_argument("--tab", type=int)
    p.add_argument("--request-id", required=True)

    p = sub.add_parser("pause", help="intercept requests (Fetch.requestPaused) — interactive")
    p.add_argument("--tab", type=int)
    p.add_argument("--url-pattern", action="append", default=None,
                   help="URL glob to match (repeatable; default '*')")
    p.add_argument("--auto-continue", action="store_true",
                   help="don't prompt — just log and continue every paused request")


# ---------- dispatch from __main__._dispatch ----------

def dispatch(args, d: DumperClient) -> bool:
    """Return True if this module handled the command."""
    cmd = args.cmd
    if cmd == "attach":
        out = d._cmd({
            "type": "debug_attach",
            "tabId": args.tab,
            "network": not args.no_network,
            "console": args.console,
            "fetch": args.fetch,
            "fetchPatterns": args.fetch_pattern,
        })
        print(json.dumps(out, indent=2))
        return True
    if cmd == "detach":
        print(json.dumps(d._cmd({"type": "debug_detach", "tabId": args.tab}), indent=2))
        return True
    if cmd == "status":
        print(json.dumps(d._cmd({"type": "debug_status"}), indent=2))
        return True
    if cmd == "body":
        out = d._cmd({"type": "debug_get_body", "tabId": args.tab, "requestId": args.request_id})
        print(json.dumps(out, indent=2))
        return True
    if cmd == "listen":
        _run_listen(d, args)
        return True
    if cmd == "pause":
        _run_pause(d, args)
        return True
    return False


# ---------- SSE client ----------

def _sse_events(base_url: str, tab_id: Optional[int],
                session: Optional[str] = None) -> Iterator[dict]:
    """Generator yielding {} events from the bridge's /events stream."""
    params = {}
    if tab_id is not None:
        params["tab"] = str(tab_id)
    if session:
        params["session"] = session
    url = f"{base_url.rstrip('/')}/events"
    with httpx.stream("GET", url, params=params, timeout=None) as r:
        r.raise_for_status()
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
            while "\n\n" in buf:
                event, buf = buf.split("\n\n", 1)
                data = []
                for line in event.splitlines():
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                if not data:
                    continue
                try:
                    yield json.loads("\n".join(data))
                except json.JSONDecodeError:
                    continue


# ---------- listen ----------

# Map of CDP method → fields to surface in condensed mode.
_NET_REQUEST = "Network.requestWillBeSent"
_NET_RESPONSE = "Network.responseReceived"
_NET_FINISHED = "Network.loadingFinished"
_NET_FAILED = "Network.loadingFailed"
_RUNTIME_LOG = "Runtime.consoleAPICalled"
_RUNTIME_EXC = "Runtime.exceptionThrown"


def _condense(ev: dict) -> str:
    method = ev.get("method", "?")
    p = ev.get("params", {}) or {}
    tab = ev.get("tabId")
    if method == _NET_REQUEST:
        req = p.get("request", {})
        return f"[tab {tab}] → {p.get('requestId','')} {req.get('method','GET')} {req.get('url','')}"
    if method == _NET_RESPONSE:
        r = p.get("response", {})
        return f"[tab {tab}] ← {p.get('requestId','')} {r.get('status','')} {r.get('mimeType','')} {r.get('url','')}"
    if method == _NET_FAILED:
        return f"[tab {tab}] ✗ {p.get('requestId','')} {p.get('errorText','')} (type={p.get('type','')})"
    if method == _NET_FINISHED:
        return f"[tab {tab}] ✓ {p.get('requestId','')} {p.get('encodedDataLength', '?')}B"
    if method == _RUNTIME_LOG:
        args = p.get("args", []) or []
        parts = []
        for a in args[:6]:
            v = a.get("value")
            if v is None:
                v = a.get("description") or a.get("type")
            parts.append(str(v))
        return f"[tab {tab}] console.{p.get('type','log')}: {' '.join(parts)}"
    if method == _RUNTIME_EXC:
        e = (p.get("exceptionDetails") or {}).get("text") or "exception"
        return f"[tab {tab}] ⚠ {e}"
    return f"[tab {tab}] {method}"


def _event_url(ev: dict) -> str:
    p = ev.get("params") or {}
    return (p.get("request") or {}).get("url") or (p.get("response") or {}).get("url") or p.get("url") or ""


def _run_listen(d: DumperClient, args) -> None:
    url_re = re.compile(args.filter) if args.filter else None
    method_re = re.compile(args.method) if args.method else None
    bodies_pending: dict[str, dict] = {}  # requestId -> response event waiting for finished

    print(f"# listening on {d.base_url}/events  (tab={args.tab or 'all'})  — Ctrl-C to stop",
          file=sys.stderr)
    try:
        for ev in _sse_events(d.base_url, args.tab, d.session):
            if method_re and not method_re.search(ev.get("method", "")):
                continue
            if url_re and not url_re.search(_event_url(ev)):
                continue
            if args.raw:
                print(json.dumps(ev))
            else:
                print(_condense(ev))
            sys.stdout.flush()

            if args.include_body:
                method = ev.get("method")
                params = ev.get("params") or {}
                if method == _NET_RESPONSE:
                    bodies_pending[params.get("requestId")] = ev
                elif method == _NET_FINISHED:
                    rid = params.get("requestId")
                    resp_ev = bodies_pending.pop(rid, None)
                    if resp_ev:
                        try:
                            body = d._cmd({"type": "debug_get_body", "tabId": ev.get("tabId"),
                                           "requestId": rid}, timeout=10.0)
                            print(f"  body[{rid}] base64={body.get('base64Encoded')} "
                                  f"len={len(body.get('body') or '')}")
                            if not body.get("base64Encoded"):
                                # Truncate noisy bodies in condensed mode
                                snippet = (body.get("body") or "")[:1000]
                                if snippet:
                                    print("  | " + snippet.replace("\n", "\n  | "))
                        except Exception as e:
                            print(f"  body[{rid}] error: {e}")
    except KeyboardInterrupt:
        print("\n# stopped", file=sys.stderr)
    except httpx.HTTPError as e:
        print(f"\n# stream error: {e}", file=sys.stderr)


# ---------- pause (interactive) ----------

def _run_pause(d: DumperClient, args) -> None:
    # Ensure Fetch domain is enabled.
    patterns = args.url_pattern or ["*"]
    d._cmd({"type": "debug_attach", "tabId": args.tab, "network": True,
            "fetch": True, "fetchPatterns": patterns})

    print(f"# pausing requests on tab={args.tab or 'active'}  patterns={patterns}",
          file=sys.stderr)
    print("# at each prompt: [c]ontinue, [a]bort, [f]ail, q=quit  (default c)", file=sys.stderr)

    try:
        for ev in _sse_events(d.base_url, args.tab, d.session):
            if ev.get("method") != "Fetch.requestPaused":
                continue
            p = ev.get("params") or {}
            rid = p.get("requestId")
            req = p.get("request") or {}
            print(f"\nPAUSED {req.get('method','GET')} {req.get('url','')}")
            print(f"   resourceType={p.get('resourceType','')}  requestId={rid}")
            if args.auto_continue:
                action = "c"
            else:
                try:
                    action = input("> ").strip().lower() or "c"
                except EOFError:
                    action = "c"
            if action in ("q", "quit", "exit"):
                break
            cmd = {"c": "continue", "a": "fail", "f": "fail"}.get(action, "continue")
            error_reason = "Aborted" if action in ("a", "f") else None
            try:
                d._cmd({
                    "type": "debug_pause_continue",
                    "tabId": ev.get("tabId"),
                    "requestId": rid,
                    "action": cmd,
                    **({"errorReason": error_reason} if error_reason else {}),
                }, timeout=10.0)
            except Exception as e:
                print(f"  error: {e}")
    except KeyboardInterrupt:
        print("\n# stopped", file=sys.stderr)
