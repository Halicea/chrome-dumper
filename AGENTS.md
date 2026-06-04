# Agents

This project is organized into three main components that work together to provide browser automation capabilities:

## 1. Chrome Extension (`extension/`)
- A Manifest v3 Chrome extension that runs in a background service worker
- Communicates with the bridge via WebSocket at `ws://127.0.0.1:8765`
- Provides full automation capabilities for browser tabs
- Isolated profile support for clean testing environments
- Announces a per-profile session id + name so multiple browsers can share one bridge

## 2. Server/Bridge (`server/`)
- WebSocket server acting as a bridge between clients and extensions
- HTTP API endpoint at `http://127.0.0.1:8766/cmd` for client commands
- Handles command routing between clients and extensions
- Built with Python using websockets, aiohttp, and pillow

## 3. Client/Library (`client/`)
- Command-line interface (CLI) tool named 'dumper'
- Python library for programmatic use
- Supports REPL functionality with command history and tab completion
- Built with Python using httpx for HTTP requests

## Architecture
```
   Chrome (extension SW) ── ws://127.0.0.1:8765 ──► bridge (server)
                                                       │
                                                       ▼
                          http://127.0.0.1:8766/cmd  ◄── any client
```

The extension dials into the bridge. Clients POST JSON commands to the bridge's HTTP API; the bridge forwards them over the WS and returns the extension's reply.

## Usage Flow
1. Run `make sync` to setup dependencies using uv
2. Run `make server` to start the bridge server
3. Run `make chrome` (or `make chrome SESSION=<name>`) to launch the browser with the extension
4. Run `make client` (or `make client SESSION=<name>`) to start REPL or use CLI commands

## Key Features
- List/open/close browser tabs
- Click, type, and press keys
- Scroll, highlight regions
- Dump live DOM to HTML files  
- Take screenshots
- Navigate pages and wait for load
- Text selection
- Multiple named browser sessions over one bridge