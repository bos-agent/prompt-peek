# CLAUDE.md

## Project

**prompt-peek** — intercept and inspect LLM API prompts/responses from coding agents via mitmproxy.

- Python 3.11+, managed with `uv`
- Proxy: mitmproxy addon intercepts HTTP/HTTPS traffic matching known LLM API paths
- Web UI: FastAPI + Jinja2 templates + vanilla JS (no framework)
- Storage: SQLite via aiosqlite (WAL mode, thread-local connections)

## Commands

```bash
# Run (proxy + web UI)
uv run prompt-peek
# Proxy on 127.0.0.1:8083, Web UI on 127.0.0.1:9000

# Run tests (none yet — TODO)
# uv run pytest
```

## Architecture

```
src/prompt_peek/
├── __main__.py      # Entry point: starts proxy thread + uvicorn
├── config.py        # Config dataclass (ports, db path, API patterns)
├── proxy_addon.py   # mitmproxy addon + thread-safe EventBus
├── store.py         # SQLite store (thread-local connections, WAL)
├── web_server.py    # FastAPI app: pages + REST API + WebSocket
├── templates/       # Jinja2 templates (base, index, capture, raw)
└── static/          # style.css, app.js (vanilla)
```

**Data flow:** proxy thread captures request → Store.insert() → EventBus.push() → WebSocket broadcast to UI → response arrives → Store.update_response() → another event → UI updates live.

## Key patterns

- All HTML is server-rendered with Jinja2; JS handles interactivity/live updates
- Store is thread-local (one SQLite connection per thread); EventBus uses threading.Event for async wakeup
- WebSocket pushes capture events (new request, response update, error) to connected browsers
- Body fields are stored as JSON strings in SQLite, deserialized by `_deserialize_capture_fields()` on read

## .gitignore

Add `.superpowers/` to `.gitignore`.
