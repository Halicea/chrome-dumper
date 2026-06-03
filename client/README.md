# chrome-dumper-client

CLI + library that drives the HTML Dumper extension via the bridge server.

## Install / run

```bash
uv sync
uv run dumper tabs
uv run dumper open https://example.com
uv run dumper click --text "More information"
uv run dumper dump
uv run dumper get https://example.com   # open + dump in one
```

Run with no subcommand to drop into a REPL:

```
$ uv run dumper
dumper> tabs
dumper> open https://example.com
dumper> click --text "More information"
dumper> dump
dumper> quit
```

History is persisted to `~/.dumper_history` (override with `DUMPER_HISTFILE`).

`--base-url` (or `DUMPER_BASE_URL`) overrides the server URL. `--out-dir` (or `DUMPER_OUT_DIR`) sets where HTML files are written.

## Library

```python
from chrome_dumper_client import DumperClient

with DumperClient() as d:
    d.open("https://example.com")
    print(d.dump()["html"][:200])
```
