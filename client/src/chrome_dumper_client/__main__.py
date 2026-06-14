"""CLI for the chrome-dumper bridge.

Run with a subcommand for one-shot use:
    dumper tabs
    dumper get https://example.com

Run with no subcommand to enter a REPL where each line is the same command:
    $ dumper
    dumper> tabs
    dumper> open https://example.com
    dumper> click --text "Sign in"
    dumper> quit
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from .client import DEFAULT_BASE, DumperClient
from . import debug as debug_module
from . import messaging as messaging_module


# Convenient window-size presets (width, height in CSS px). State-only presets
# ("max", "full", "min") map to window states and are handled separately.
_RESIZE_PRESETS: dict[str, tuple[int, int]] = {
    "phone": (414, 896),
    "tablet": (834, 1112),
    "laptop": (1366, 768),
    "desktop": (1440, 900),
    "hd": (1920, 1080),
    "1080p": (1920, 1080),
}


def _parse_resize(args) -> dict:
    """Turn resize CLI args into kwargs for DumperClient.resize()."""
    kw: dict = {"tab_id": args.tab}
    if args.state:
        kw["state"] = args.state
    size = (args.size or "").strip().lower()
    if size in ("max", "maximize", "maximized"):
        kw["state"] = "maximized"
    elif size in ("full", "fullscreen"):
        kw["state"] = "fullscreen"
    elif size in ("min", "minimize", "minimized"):
        kw["state"] = "minimized"
    elif size in ("left", "half-left"):
        kw["half"] = "left"
    elif size in ("right", "half-right"):
        kw["half"] = "right"
    elif size in _RESIZE_PRESETS:
        kw["width"], kw["height"] = _RESIZE_PRESETS[size]
    elif size:
        sep = "x" if "x" in size else ("," if "," in size else None)
        if not sep:
            raise ValueError(f"bad size {args.size!r}: use WxH or a preset")
        w, h = size.split(sep, 1)
        kw["width"], kw["height"] = int(w), int(h)
    if args.width is not None: kw["width"] = args.width
    if args.height is not None: kw["height"] = args.height
    if args.left is not None: kw["left"] = args.left
    if args.top is not None: kw["top"] = args.top
    return kw


class _ReplExit(Exception):
    pass


class _ParserError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that raises instead of exiting — so REPL can recover."""
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _ParserError(message)


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)[:60] or "page"


def _write_dump(resp: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{resp['tabId']}_{_safe(resp.get('title') or 'page')}.html"
    path.write_text(resp["html"], encoding="utf-8")
    return path


def _build_parser(for_repl: bool = False) -> _Parser:
    prog = "dumper>" if for_repl else "dumper"
    p = _Parser(prog=prog, add_help=not for_repl)
    if not for_repl:
        p.add_argument("--base-url", default=os.environ.get("DUMPER_BASE_URL", DEFAULT_BASE))
        p.add_argument("--session", default=os.environ.get("DUMPER_SESSION"),
                       help="target browser session by id or name "
                            "(optional when only one is connected)")
        p.add_argument("--out-dir", default=os.environ.get("DUMPER_OUT_DIR", "dumps"))
        # Using --wait-server (not --wait) because per-command --wait flags exist.
        p.add_argument("--wait", "--wait-server", dest="wait_server",
                       action="store_true",
                       help="if the bridge isn't reachable, wait for one instead of "
                            "auto-starting (default is to spawn `chrome-dumper-server`)")
        p.add_argument("--wait-timeout", type=float, default=30.0,
                       help="seconds to wait for the bridge (default 30)")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("health")
    sub.add_parser("ping")
    sub.add_parser("tabs")
    sub.add_parser("sessions", help="list browser sessions connected to the bridge")
    s = sub.add_parser("spawn", help="launch a Chrome for the current session (runs `make chrome`)")
    s.add_argument("--timeout", type=float, default=30.0,
                   help="seconds to wait for it to connect (default 30; 0 = don't wait)")
    if not for_repl:
        s = sub.add_parser("load", help="run commands from a file (# = comment)")
        s.add_argument("path")

    s = sub.add_parser("open"); s.add_argument("url"); s.add_argument("--no-wait", action="store_true")
    s = sub.add_parser("nav");  s.add_argument("url"); s.add_argument("--tab", type=int); s.add_argument("--no-wait", action="store_true")

    for name in ("back", "forward"):
        s = sub.add_parser(name, help=f"navigate {name} in tab history")
        s.add_argument("--tab", type=int)
        s.add_argument("--steps", type=int, default=1, help="number of history entries (default 1)")
        s.add_argument("--no-wait", action="store_true")

    s = sub.add_parser("click")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--selector")
    g.add_argument("--text")
    s.add_argument("--nth", type=int, default=0)
    s.add_argument("--tab", type=int)
    s.add_argument("--wait", action="store_true", help="wait for page load (use when click triggers navigation)")

    s = sub.add_parser("wait", help="sleep N seconds (default 1)")
    s.add_argument("seconds", nargs="?", type=float, default=1.0)

    s = sub.add_parser("key")
    s.add_argument("key", help="key name e.g. Tab, Enter, Escape, ArrowDown, a")
    s.add_argument("--shift", action="store_true")
    s.add_argument("--ctrl", action="store_true")
    s.add_argument("--alt", action="store_true")
    s.add_argument("--meta", action="store_true")
    s.add_argument("--selector", help="dispatch on this element instead of activeElement")
    s.add_argument("--wait", action="store_true")
    s.add_argument("--tab", type=int)

    s = sub.add_parser("tab", help="press Tab key (use --shift for Shift+Tab)")
    s.add_argument("--shift", action="store_true")
    s.add_argument("--tab", type=int, dest="tab_id")

    s = sub.add_parser("focus")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--selector")
    g.add_argument("--text")
    s.add_argument("--nth", type=int, default=0)
    s.add_argument("--tab", type=int)

    for alias in ("enter", "space"):
        s = sub.add_parser(alias, help=f"press {alias.capitalize()} key (alias for `key {alias.capitalize()}`)")
        s.add_argument("--selector")
        s.add_argument("--wait", action="store_true")
        s.add_argument("--tab", type=int)

    # `input` is an alias of `type` (since `type` is a builtin name and people expect "input").
    for name in ("type", "input"):
        s = sub.add_parser(name)
        s.add_argument("value", help="text to type")
        g = s.add_mutually_exclusive_group()
        g.add_argument("--selector"); g.add_argument("--placeholder"); g.add_argument("--label")
        s.add_argument("--nth", type=int, default=0)
        s.add_argument("--no-clear", action="store_true")
        s.add_argument("--submit", action="store_true")
        s.add_argument("--tab", type=int)
        s.add_argument("--wait", action="store_true")

    s = sub.add_parser("select")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--selector", help="select all text in this element")
    g.add_argument("--text", help="select first occurrence of this substring")
    g.add_argument("--rect", help="x1,y1,x2,y2 viewport coords (drag from p1 to p2)")
    s.add_argument("--from", dest="from_", help="(with --to) selector of the start element")
    s.add_argument("--to", help="(with --from) selector of the end element")
    s.add_argument("--no-mouse", action="store_true", help="don't dispatch synthetic mouse events")
    s.add_argument("--no-scroll", action="store_true")
    s.add_argument("--no-focus", action="store_true", help="don't focus on the selected element")
    s.add_argument("--tab", type=int)

    s = sub.add_parser("select-clear"); s.add_argument("--tab", type=int)

    s = sub.add_parser("scroll")
    s.add_argument("direction", nargs="?", default="down", choices=["up", "down"])
    s.add_argument("--pages", type=float, help="viewport fractions to scroll (default 0.5)")
    s.add_argument("--pixels", type=int, help="explicit pixel amount (overrides --pages)")
    s.add_argument("--to", help='"top" | "bottom" | CSS selector to scroll into view')
    s.add_argument("--no-smooth", action="store_true")
    s.add_argument("--tab", type=int)

    s = sub.add_parser("highlight")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--selector")
    g.add_argument("--text")
    g.add_argument("--rect", help="x,y,width,height in CSS pixels (viewport coords)")
    s.add_argument("--all", action="store_true", help="highlight every match")
    s.add_argument("--nth", type=int, default=0)
    s.add_argument("--color", default="#ff1744")
    s.add_argument("--label")
    s.add_argument("--duration", type=int, default=0, help="auto-clear after N ms (0 = persistent)")
    s.add_argument("--no-scroll", action="store_true")
    s.add_argument("--tab", type=int)

    s = sub.add_parser("clear-highlights"); s.add_argument("--tab", type=int)

    s = sub.add_parser("dump"); s.add_argument("--tab", type=int)
    s = sub.add_parser("screenshot")
    s.add_argument("--format", choices=["png", "jpeg"], default="png")
    s.add_argument("--quality", type=int, default=85, help="jpeg only, 1-100")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--rect", help="x,y,width,height viewport coords (CSS px)")
    g.add_argument("--selector", help="crop to this element's bounding rect")
    g.add_argument("--text", help="crop to first element containing this text")
    s.add_argument("--out", help="explicit output file (default: <out-dir>/<tabId>_<title>.<ext>)")
    s.add_argument("--tab", type=int)
    s = sub.add_parser("close")
    s.add_argument("--tab", type=int)
    s.add_argument("tabs", nargs="*", type=int, help="tab ids (overrides --tab)")
    s = sub.add_parser("get");  s.add_argument("url")

    s = sub.add_parser("resize", help="resize/reposition the browser window")
    s.add_argument("size", nargs="?",
                   help="WxH (e.g. 1280x800) or a preset: " + ", ".join(_RESIZE_PRESETS))
    s.add_argument("--width", type=int)
    s.add_argument("--height", type=int)
    s.add_argument("--left", type=int, help="window x position")
    s.add_argument("--top", type=int, help="window y position")
    s.add_argument("--max", dest="state", action="store_const", const="maximized",
                   help="maximize the window")
    s.add_argument("--full", dest="state", action="store_const", const="fullscreen",
                   help="fullscreen the window")
    s.add_argument("--min", dest="state", action="store_const", const="minimized",
                   help="minimize the window")
    s.add_argument("--normal", dest="state", action="store_const", const="normal",
                   help="restore to a normal window")
    s.add_argument("--tab", type=int)

    # CDP / Chrome Debugger Protocol commands (separate module).
    debug_module.register(sub)

    # messaging plugin (LinkedIn draft-assist) — client half (separate module).
    messaging_module.register(sub)

    if for_repl:
        s = sub.add_parser("use", help="target a session by id or name for later commands ('use -' to clear)")
        s.add_argument("name")
        sub.add_parser("help")
        sub.add_parser("quit")
        sub.add_parser("exit")
        sub.add_parser("clear", help="clear this session's command log (readline arrow-up history is unaffected)")
        s = sub.add_parser("save", help="save the session command log to a file")
        s.add_argument("path")
        s = sub.add_parser("load", help="run commands from a file, one per line (# = comment)")
        s.add_argument("path")
    return p


_HELP = """commands:
  health                       bridge status
  sessions                     list connected browser sessions (* = current target)
  use <id|name>                target a session for later commands ('use -' to clear)
  spawn [--timeout N]          launch a Chrome for the current session (runs `make chrome`)
                               waits up to N s for it to connect (N=0 → don't wait)
  ping                         ping the extension
  tabs                         list open tabs
  open <url> [--no-wait]       open a new tab
  nav  <url> [--tab N] [--no-wait]
  save <path>                  write this session's commands to a file
  load <path>                  run commands from a file (# = comment, blank lines skipped)
  clear                        clear the session command log (arrow-up history untouched)
  click [--selector <css> | --text <s>] [--nth N] [--tab N] [--wait]
                               no target → click the currently focused element
                               --wait if the click navigates and you need the load to complete
  wait [seconds]               sleep client-side; default 1.0
  select --selector <css> | --text <s> | --rect x1,y1,x2,y2 | --from <css> --to <css>
                               [--no-mouse] [--no-scroll] [--no-focus] [--tab N]
                               highlight text via Selection API; also fires mousedown/move/up
                               automatically focuses on element (use --no-focus to disable)
  select-clear [--tab N]       clear current selection
  scroll [up|down] [--pages F] [--pixels N] [--to top|bottom|<css>] [--no-smooth] [--tab N]
                               default: down half a viewport
  key <KeyName> [--shift] [--ctrl] [--alt] [--meta] [--selector <css>] [--wait] [--tab N]
  tab [--shift]                shortcut for `key Tab [--shift]`
  focus [--selector <css> | --text <s>] [--nth N] [--tab N]
         focus on element by selector or text; focuses on first focusable parent if not directly focusable
  enter [--selector <css>] [--wait] [--tab N]    shortcut for `key Enter`
  space [--selector <css>] [--wait] [--tab N]    shortcut for `key " "`
  type <value> | input <value> [--selector <css> | --placeholder <s> | --label <s>]
                               [--nth N] [--no-clear] [--submit] [--tab N] [--wait]
  highlight --selector <css> | --text <s> | --rect x,y,w,h
            [--all] [--nth N] [--color #hex] [--label <s>]
            [--duration MS] [--no-scroll] [--tab N]
  clear-highlights [--tab N]
  dump [--tab N]               dump live DOM, save under out-dir
  screenshot [--format png|jpeg] [--quality 1-100]
             [--rect x,y,w,h | --selector <css> | --text <s>]
             [--out <path>] [--tab N]
                               capture visible viewport (or a region); save under out-dir
  close [--tab N | <id> ...]   close active tab, one id, or many
  get  <url>                   open + dump in one shot
  help                         show this
  quit | exit | Ctrl-D         leave the REPL
"""


def _dispatch(args: argparse.Namespace, d: DumperClient, out_dir: Path) -> None:
    if args.cmd in ("quit", "exit"):
        raise _ReplExit()
    if args.cmd == "help":
        print(_HELP); return
    # CDP / debug commands live in their own module.
    if debug_module.dispatch(args, d):
        return
    # messaging draft-assist lives in its own module.
    if messaging_module.dispatch(args, d):
        return
    if args.cmd == "health":
        print(json.dumps(d.health(), indent=2))
    elif args.cmd == "sessions":
        rows = d.sessions()
        if not rows:
            print("(no sessions connected)")
        for s in rows:
            cur = "*" if d.session in (s["id"], s["name"]) else " "
            print(f" {cur} {(s['name'] or ''):<16}  {s['id']}")
    elif args.cmd == "use":
        d.session = None if args.name == "-" else args.name
        print(f"targeting session: {d.session or '(auto — single session)'}")
    elif args.cmd == "spawn":
        _spawn_chrome(d, args.timeout)
    elif args.cmd == "ping":
        print(json.dumps(d.ping(), indent=2))
    elif args.cmd == "tabs":
        for t in d.list_tabs():
            flag = "*" if t.get("active") else " "
            print(f" {flag} {t['id']:>5}  {(t.get('title') or '')[:60]:<60}  {t.get('url','')}")
    elif args.cmd == "open":
        print(json.dumps(d.open(args.url, wait=not args.no_wait), indent=2))
    elif args.cmd == "nav":
        print(json.dumps(d.navigate(args.url, tab_id=args.tab, wait=not args.no_wait), indent=2))
    elif args.cmd == "back":
        print(json.dumps(d.back(steps=args.steps, tab_id=args.tab, wait=not args.no_wait), indent=2))
    elif args.cmd == "forward":
        print(json.dumps(d.forward(steps=args.steps, tab_id=args.tab, wait=not args.no_wait), indent=2))
    elif args.cmd == "click":
        print(json.dumps(d.click(selector=args.selector, text=args.text, nth=args.nth,
                                 tab_id=args.tab, wait=args.wait), indent=2))
    elif args.cmd == "wait":
        time.sleep(max(0.0, args.seconds))
    elif args.cmd == "key":
        print(json.dumps(d.key(
            args.key, shift=args.shift, ctrl=args.ctrl, alt=args.alt, meta=args.meta,
            selector=args.selector, wait=args.wait, tab_id=args.tab,
        ), indent=2))
    elif args.cmd == "tab":
        print(json.dumps(d.tab_key(shift=args.shift, tab_id=args.tab_id), indent=2))
    elif args.cmd == "focus":
        print(json.dumps(d.focus(selector=args.selector, text=args.text, nth=args.nth,
                                 tab_id=args.tab), indent=2))
    elif args.cmd in ("enter", "space"):
        key_name = "Enter" if args.cmd == "enter" else " "
        print(json.dumps(d.key(
            key_name, selector=args.selector, wait=args.wait, tab_id=args.tab,
        ), indent=2))
    elif args.cmd in ("type", "input"):
        print(json.dumps(d.type(
            args.value, selector=args.selector, placeholder=args.placeholder, label=args.label,
            nth=args.nth, clear=not args.no_clear, submit=args.submit,
            tab_id=args.tab, wait=args.wait,
        ), indent=2))
    elif args.cmd == "select":
        if not (args.selector or args.text or args.rect or (args.from_ and args.to)):
            raise RuntimeError("select needs --selector, --text, --rect, or --from + --to")
        rect = None
        if args.rect:
            x1, y1, x2, y2 = (float(v) for v in args.rect.split(","))
            rect = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
        sel = args.selector if not args.from_ else None
        txt = args.text if not args.from_ else None
        print(json.dumps(d.select(
            selector=sel, text=txt, from_=args.from_, to=args.to, rect=rect,
            dispatch_mouse=not args.no_mouse, scroll=not args.no_scroll,
            focus=not args.no_focus, tab_id=args.tab,
        ), indent=2))
    elif args.cmd == "select-clear":
        print(json.dumps(d.select_clear(tab_id=args.tab), indent=2))
    elif args.cmd == "scroll":
        print(json.dumps(d.scroll(
            args.direction, pages=args.pages, pixels=args.pixels, to=args.to,
            smooth=not args.no_smooth, tab_id=args.tab,
        ), indent=2))
    elif args.cmd == "highlight":
        rect = None
        if args.rect:
            x, y, w, h = (float(v) for v in args.rect.split(","))
            rect = {"x": x, "y": y, "width": w, "height": h}
        print(json.dumps(d.highlight(
            selector=args.selector, text=args.text, rect=rect,
            all=args.all, nth=args.nth, color=args.color, label=args.label,
            duration_ms=args.duration, scroll=not args.no_scroll, tab_id=args.tab,
        ), indent=2))
    elif args.cmd == "clear-highlights":
        print(json.dumps(d.clear_highlights(tab_id=args.tab), indent=2))
    elif args.cmd == "close":
        ids = args.tabs or ([args.tab] if args.tab is not None else None)
        print(json.dumps(d.close_tab(tab_ids=ids), indent=2))
    elif args.cmd == "screenshot":
        rect = None
        if args.rect:
            x, y, w, h = (float(v) for v in args.rect.split(","))
            rect = {"x": x, "y": y, "width": w, "height": h}
        resp = d.screenshot(
            format=args.format, quality=args.quality,
            rect=rect, selector=args.selector, text=args.text, tab_id=args.tab,
        )
        data_url = resp.get("dataUrl", "")
        b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
        raw = base64.b64decode(b64)
        if args.out:
            path = Path(args.out)
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            ext = "jpg" if resp.get("format") == "jpeg" else "png"
            path = out_dir / f"{resp['tabId']}_{_safe(resp.get('title') or 'page')}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        print(f"wrote {path}  ({len(raw)} bytes)  url={resp.get('url')}")
    elif args.cmd == "dump":
        resp = d.dump(tab_id=args.tab)
        path = _write_dump(resp, out_dir)
        print(f"wrote {path}  ({len(resp['html'])} bytes)  url={resp.get('url')}")
    elif args.cmd == "resize":
        print(json.dumps(d.resize(**_parse_resize(args)), indent=2))
    elif args.cmd == "get":
        opened = d.open(args.url, wait=True)
        resp = d.dump(tab_id=opened["tabId"])
        path = _write_dump(resp, out_dir)
        print(f"wrote {path}  ({len(resp['html'])} bytes)")


_COMMANDS: dict[str, list[str]] = {
    "health": [],
    "ping": [],
    "tabs": [],
    "sessions": [],
    "use": [],
    "spawn": ["--timeout"],
    "open": ["--no-wait"],
    "nav": ["--tab", "--no-wait"],
    "click": ["--selector", "--text", "--nth", "--tab", "--no-wait"],
    "dump": ["--tab"],
    "screenshot": ["--format", "--quality", "--rect", "--selector", "--text", "--out", "--tab"],
    "close": ["--tab"],
    "type": ["--selector", "--placeholder", "--label", "--nth", "--no-clear", "--submit", "--tab", "--wait"],
    "wait": [],
    "key": ["--shift", "--ctrl", "--alt", "--meta", "--selector", "--wait", "--tab"],
    "tab": ["--shift", "--tab"],
    "focus": ["--selector", "--text", "--nth", "--tab"],
    "enter": ["--selector", "--wait", "--tab"],
    "space": ["--selector", "--wait", "--tab"],
    "input": ["--selector", "--placeholder", "--label", "--nth", "--no-clear", "--submit", "--tab", "--wait"],
    "select": ["--selector", "--text", "--from", "--to", "--rect", "--no-mouse", "--no-scroll", "--no-focus", "--tab"],
    "select-clear": ["--tab"],
    "scroll": ["up", "down", "--pages", "--pixels", "--to", "--no-smooth", "--tab"],
    "highlight": ["--selector", "--text", "--rect", "--all", "--nth", "--color", "--label", "--duration", "--no-scroll", "--tab"],
    "clear-highlights": ["--tab"],
    "resize": ["--width", "--height", "--left", "--top", "--max", "--full",
               "--min", "--normal", "--tab"],
    "get": [],
    "save": [],
    "load": [],
    "clear": [],
    "help": [],
    "quit": [],
    "exit": [],
}


def _make_completer():
    def completer(text: str, state: int):
        import readline
        line = readline.get_line_buffer()
        begin = readline.get_begidx()
        prefix_line = line[:begin]
        tokens_before = prefix_line.split()
        if not tokens_before:
            options = list(_COMMANDS.keys())
        else:
            cmd = tokens_before[0]
            options = _COMMANDS.get(cmd, [])
        matches = [o for o in options if o.startswith(text)]
        return matches[state] if state < len(matches) else None
    return completer


def _try_enable_readline() -> None:
    try:
        import readline
    except ImportError:
        return
    histfile = Path(os.environ.get("DUMPER_HISTFILE", Path.home() / ".dumper_history"))
    try:
        readline.read_history_file(str(histfile))
    except FileNotFoundError:
        pass
    import atexit
    atexit.register(lambda: readline.write_history_file(str(histfile)))
    readline.set_completer(_make_completer())
    readline.set_completer_delims(" \t\n")
    # libedit (macOS default) uses a different binding syntax than GNU readline.
    if "libedit" in getattr(readline, "__doc__", "") or "":
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


def _repl(d: DumperClient, out_dir: Path) -> None:
    parser = _build_parser(for_repl=True)
    _try_enable_readline()
    print("chrome-dumper REPL  —  type 'help', 'quit', or Ctrl-D")
    try:
        h = d.health()
        sess = h.get("sessions") or []
        names = ", ".join(f"{s['name']} ({s['id'][:8]})" for s in sess) or "none"
        print(f"bridge ok, sessions: {names}")
        if len(sess) > 1 and not d.session:
            print("multiple sessions connected — use `sessions` to list, "
                  "`use <name>` to pick one")
    except Exception as e:
        print(f"warning: cannot reach bridge: {e}")
    last_line: Optional[str] = None
    repeat = 1
    session_log: list[str] = []
    NON_LOGGED = {"save", "load", "clear", "quit", "exit", "help", "sessions"}

    def _run_one(line: str, source: str = "input") -> bool:
        """Process a single REPL line. Returns False if the loop should exit."""
        nonlocal repeat, last_line
        line = line.strip()
        if not line or line.startswith("#"):
            return True
        if line.isdigit():
            n = int(line)
            if n < 1:
                print("repeat count must be >= 1"); return True
            repeat = n
            return True
        if line == "":
            return True
        try:
            tokens = shlex.split(line)
        except ValueError as e:
            print(f"parse error: {e}"); repeat = 1; return True
        try:
            args = parser.parse_args(tokens)
        except _ParserError as e:
            print(f"error: {e}"); repeat = 1; return True
        except SystemExit:
            repeat = 1; return True
        if not args.cmd:
            repeat = 1; return True

        # session meta-commands — handled here, not logged
        if args.cmd == "save":
            try:
                Path(args.path).expanduser().write_text(
                    "\n".join(session_log) + ("\n" if session_log else "")
                )
                print(f"saved {len(session_log)} command(s) to {args.path}")
            except OSError as e:
                print(f"error: {e}")
            repeat = 1; return True
        if args.cmd == "clear":
            session_log.clear()
            print("session command log cleared")
            repeat = 1; return True
        if args.cmd == "load":
            try:
                lines = Path(args.path).expanduser().read_text().splitlines()
            except OSError as e:
                print(f"error: {e}"); repeat = 1; return True
            for raw in lines:
                print(f"$ {raw}")
                if not _run_one(raw, source="file"):
                    return False
            return True

        n = repeat
        repeat = 1
        try:
            for i in range(n):
                if n > 1:
                    print(f"--- {i + 1}/{n} ---")
                _dispatch(args, d, out_dir)
        except _ReplExit:
            return False
        except KeyboardInterrupt:
            print("\n(interrupted — returned to prompt; the extension may still be busy)")
            return True
        except RuntimeError as e:
            print(f"error: {e}"); return True
        except Exception as e:
            print(f"error: {e}"); return True

        # log on success
        if args.cmd not in NON_LOGGED:
            if n > 1:
                session_log.append(str(n))
            session_log.append(line)
        if source == "input":
            last_line = line
        return True

    while True:
        tag = f"({d.session})" if d.session else ""
        if repeat > 1:
            prompt = f"dumper{tag} [{repeat}x]> "
        else:
            prompt = f"dumper{tag}> "
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not line:
            if last_line is None:
                continue
            line = last_line
            print(f"(repeating: {line})")
        if not _run_one(line, source="input"):
            return


def _bridge_alive(base_url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/health", timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _wait_for_bridge(base_url: str, deadline: float, label: str) -> bool:
    printed = False
    while time.monotonic() < deadline:
        if _bridge_alive(base_url):
            if printed:
                print(" ok", file=sys.stderr)
            return True
        if not printed:
            print(f"{label}", end="", file=sys.stderr, flush=True)
            printed = True
        else:
            print(".", end="", file=sys.stderr, flush=True)
        time.sleep(0.5)
    if printed:
        print(" timeout", file=sys.stderr)
    return False


def _find_server_dir() -> Optional[Path]:
    """Locate sibling `server/` (with chrome-dumper-server pyproject) for `uv run`."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent.parent / "server"
        if (cand / "pyproject.toml").is_file():
            return cand
    return None


def _find_repo_root() -> Optional[Path]:
    """Locate the repo root (has the Makefile + extension/) so we can `make chrome`."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "Makefile").is_file() and (parent / "extension").is_dir():
            return parent
    return None


def _wait_for_chrome(d: DumperClient, before: set, timeout: float) -> bool:
    """Wait until the targeted session connects (or, if untargeted, any new one)."""
    want = d.session
    deadline = time.monotonic() + timeout
    printed = False
    while time.monotonic() < deadline:
        try:
            rows = d.sessions()
        except Exception:
            rows = []
        for s in rows:
            hit = (want in (s["id"], s["name"])) if want else (s["id"] not in before)
            if hit:
                if printed:
                    print(" ok", file=sys.stderr)
                print(f"connected: {s['name']} ({s['id']})")
                if not d.session:  # adopt the new browser for convenience
                    d.session = s["name"] or s["id"]
                    print(f"targeting session: {d.session}")
                return True
        print("waiting for chrome" if not printed else ".",
              end="", file=sys.stderr, flush=True)
        printed = True
        time.sleep(0.5)
    if printed:
        print(" timeout", file=sys.stderr)
    return False


def _spawn_chrome(d: DumperClient, timeout: float) -> None:
    """Launch `make chrome [SESSION=<name>]` detached, then wait for it to connect."""
    root = _find_repo_root()
    if root is None:
        print("error: can't find the repo root (Makefile) to run `make chrome`.",
              file=sys.stderr)
        return
    # The browser dials into a bridge; without one it can never connect, so a
    # spawn would just time out. Fail fast with a clear hint instead.
    try:
        d.health()
    except Exception:
        print(f"error: no bridge reachable at {d.base_url} — start one with "
              "`make server` first, then `spawn` again.", file=sys.stderr)
        return
    sess_arg = f"SESSION={d.session}" if d.session else ""
    cmd = ["make", "chrome"] + ([sess_arg] if sess_arg else [])
    if not shutil.which("make"):
        print(f"error: `make` not found. Run `{' '.join(cmd)}` manually from {root}.",
              file=sys.stderr)
        return
    stage = root / (f".chrome-stage-{d.session}" if d.session else "extension")
    log_path = Path(tempfile.gettempdir()) / f"dumper-chrome-{d.session or 'default'}.log"
    try:
        before = {s["id"] for s in d.sessions()}
    except Exception:
        before = set()
    print(f"spawning: {' '.join(cmd)}  (cwd={root}, log={log_path})", file=sys.stderr)
    try:
        log = open(log_path, "wb")
        subprocess.Popen(
            cmd, cwd=str(root),
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        log.close()  # the child keeps its own dup of the fd
    except OSError as e:
        print(f"error: failed to spawn chrome: {e}", file=sys.stderr)
        return
    if timeout <= 0:
        print(f"launched (not waiting) — run `sessions` once the badge turns green.\n"
              f"  launch log: {log_path}")
        return
    if _wait_for_chrome(d, before, timeout):
        return
    print(f"launched, but it hasn't connected within {int(timeout)}s.")
    print(f"  - check the launch log: {log_path}")
    print("  - if the toolbar has no HTML Dumper icon, this Chrome blocks "
          "--load-extension (Chrome 137+).")
    print(f"    Load it once: chrome://extensions > Developer mode > Load unpacked > {stage}")
    print("    It then persists in that profile, so future spawns reconnect on their own.")


def _autostart_bridge(base_url: str, timeout: float) -> bool:
    """Spawn the bridge in the background and wait for it to come up."""
    cmd: Optional[list[str]] = None
    cwd: Optional[Path] = None
    on_path = shutil.which("chrome-dumper-server")
    if on_path:
        cmd = [on_path]
    else:
        server_dir = _find_server_dir()
        if server_dir and shutil.which("uv"):
            cmd = ["uv", "run", "chrome-dumper-server"]
            cwd = server_dir
    if not cmd:
        print("error: no bridge running and could not find `chrome-dumper-server` "
              "or a sibling server/ dir to `uv run`. Start it manually or pass --wait.",
              file=sys.stderr)
        return False
    print(f"bridge not running — spawning: {' '.join(cmd)}"
          + (f" (cwd={cwd})" if cwd else ""), file=sys.stderr)
    try:
        subprocess.Popen(
            cmd, cwd=str(cwd) if cwd else None,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as e:
        print(f"error: failed to spawn bridge: {e}", file=sys.stderr)
        return False
    return _wait_for_bridge(base_url, time.monotonic() + timeout, "waiting for bridge")


def _ensure_bridge(base_url: str, wait_only: bool, timeout: float) -> bool:
    if _bridge_alive(base_url):
        return True
    if wait_only:
        return _wait_for_bridge(base_url, time.monotonic() + timeout,
                                f"waiting for bridge at {base_url}")
    return _autostart_bridge(base_url, timeout)


def main() -> None:
    parser = _build_parser(for_repl=False)
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    if not _ensure_bridge(args.base_url, args.wait_server, args.wait_timeout):
        sys.exit(1)
    with DumperClient(args.base_url, session=args.session) as d:
        if not args.cmd:
            _repl(d, out_dir)
            return
        if args.cmd == "load":
            _run_script(Path(args.path).expanduser(), d, out_dir, args.base_url)
            return
        try:
            _dispatch(args, d, out_dir)
        except _ReplExit:
            pass
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr); sys.exit(1)


def _run_script(path: Path, d: DumperClient, out_dir: Path, base_url: str) -> None:
    """One-shot: run each line of <path> through the same parser/dispatch."""
    parser = _build_parser(for_repl=True)
    try:
        lines = path.read_text().splitlines()
    except OSError as e:
        print(f"error: {e}", file=sys.stderr); sys.exit(1)
    repeat = 1
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        print(f"$ {line}")
        if line.isdigit():
            repeat = max(1, int(line)); continue
        try:
            args = parser.parse_args(shlex.split(line))
        except (_ParserError, SystemExit) as e:
            print(f"  parse error: {e}"); repeat = 1; continue
        if args.cmd in ("save", "clear", "load", "quit", "exit", "help"):
            print("  (skipped — meta commands have no effect in script mode)")
            continue
        n, repeat = repeat, 1
        try:
            for i in range(n):
                if n > 1:
                    print(f"  --- {i + 1}/{n} ---")
                _dispatch(args, d, out_dir)
        except _ReplExit:
            return
        except Exception as e:
            print(f"  error: {e}")


if __name__ == "__main__":
    main()
