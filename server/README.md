# chrome-dumper-server

Bridge between the HTML Dumper Chrome extension and HTTP clients.

- `ws://127.0.0.1:8765` — the extension dials in here.
- `http://127.0.0.1:8766` — clients POST JSON commands here:
  - `GET  /health` → `{ ok, extension_connected }`
  - `POST /cmd`    → body is a protocol message (see top-level README); returns the extension's reply.

## Run

```bash
uv sync
uv run chrome-dumper-server
```

Env vars (or flags): `DUMPER_WS_HOST/PORT`, `DUMPER_HTTP_HOST/PORT`.
