# chrome-dumper-server

Bridge between the HTML Dumper Chrome extension and HTTP clients.

Multiple Chrome profiles can connect at once; each is a named session (see the
top-level README's "Sessions" section).

- `ws://127.0.0.1:8765` — extensions dial in here (one connection per profile).
- `http://127.0.0.1:8766` — clients POST JSON commands here:
  - `GET  /health`   → `{ ok, extension_connected, sessions: [...] }`
  - `GET  /sessions` → `{ sessions: [ { id, name, connected } ] }`
  - `POST /cmd`      → body is a protocol message (see top-level README); `?session=<id|name>` picks the browser. Returns the extension's reply.
  - `GET  /events`   → SSE stream of CDP events; `?session=<id|name>` and `?tab=<int>` filter it.

## Run

```bash
uv sync
uv run chrome-dumper-server
```

Env vars (or flags): `DUMPER_WS_HOST/PORT`, `DUMPER_HTTP_HOST/PORT`.
