"""FastAPI web server for prompt-peek. Serves the UI and a REST/WS API."""

import asyncio
import json
from pathlib import Path
from typing import Optional

from contextlib import asynccontextmanager

from jinja2 import Environment, FileSystemLoader

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from prompt_peek.config import DEFAULT_CONFIG
from prompt_peek.proxy_addon import EventBus
from prompt_peek.store import Store

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"


# ── helpers ───────────────────────────────────────────────────────

def _get_store(request: Request) -> Optional[Store]:
    return getattr(request.app.state, 'store', None)


def _deserialize_capture_fields(c: dict):
    """Parse JSON string fields on a capture dict in-place."""
    for field in ("request_headers", "response_headers"):
        if isinstance(c.get(field), str):
            try:
                c[field] = json.loads(c[field])
            except (json.JSONDecodeError, TypeError):
                pass
    for field in ("request_body", "response_body"):
        raw = c.get(field)
        if isinstance(raw, str):
            try:
                c[f"{field}_parsed"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                c[f"{field}_parsed"] = None
        else:
            c[f"{field}_parsed"] = None


# ── background: EventBus → WebSocket broadcast ─────────────────────

async def _broadcast_events(event_bus: EventBus, ws_set: set[WebSocket]):
    """Block until the EventBus signals, then push to every WebSocket."""
    loop = asyncio.get_running_loop()
    while True:
        # Event-driven: blocks the executor thread until push() sets the Event.
        await loop.run_in_executor(None, event_bus.wait_for_events)
        events = event_bus.drain()
        if events and ws_set:
            payload = json.dumps(events, ensure_ascii=False)
            dead: set[WebSocket] = set()
            for ws in ws_set:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            ws_set.difference_update(dead)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ws_clients = set()
    event_bus: EventBus = app.state.event_bus
    task = asyncio.create_task(_broadcast_events(event_bus, app.state.ws_clients))
    yield
    # Signal the proxy thread to shut down.
    shutdown_event = getattr(app.state, 'shutdown_event', None)
    if shutdown_event:
        shutdown_event.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    store: Optional[Store] = getattr(app.state, 'store', None)
    if store is not None:
        store.close()


# ── FastAPI app ────────────────────────────────────────────────────

app = FastAPI(title="prompt-peek", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))


# ── pages ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    template = jinja_env.get_template("index.html")
    return HTMLResponse(template.render())


# ── REST API ──────────────────────────────────────────────────────

@app.get("/api/captures")
async def api_list_captures(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    host: Optional[str] = None,
    api_type: Optional[str] = None,
    search: Optional[str] = None,
):
    store = _get_store(request)
    if store is None:
        return {"captures": [], "total": 0}

    loop = asyncio.get_running_loop()
    captures = await loop.run_in_executor(
        None,
        lambda: store.list_captures(
            limit=limit, offset=offset, host=host,
            api_type=api_type, search=search,
        ),
    )
    total = await loop.run_in_executor(
        None,
        lambda: store.count(host=host, api_type=api_type, search=search),
    )
    for c in captures:
        _deserialize_capture_fields(c)
    return {"captures": captures, "total": total}


@app.get("/api/captures/{capture_id}")
async def api_get_capture(request: Request, capture_id: int):
    store = _get_store(request)
    if store is None:
        return {"capture": None}
    loop = asyncio.get_running_loop()
    capture = await loop.run_in_executor(
        None, lambda: store.get_capture(capture_id),
    )
    if capture is None:
        return {"capture": None}
    _deserialize_capture_fields(capture)
    return {"capture": capture}


@app.delete("/api/captures/{capture_id}")
async def api_delete_capture(request: Request, capture_id: int):
    store = _get_store(request)
    if store is None:
        return {"ok": False}
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, lambda: store.delete(capture_id),
    )
    return {"ok": True}


@app.get("/api/captures/{capture_id}/system-prompt-previous")
async def api_sys_prompt_previous(request: Request, capture_id: int):
    store = _get_store(request)
    if store is None:
        return {"previous": None, "changed": False, "total_versions": 0}

    loop = asyncio.get_running_loop()
    capture = await loop.run_in_executor(
        None, lambda: store.get_capture(capture_id),
    )
    if capture is None:
        return {"previous": None, "changed": False, "total_versions": 0}

    previous = await loop.run_in_executor(
        None,
        lambda: store.get_previous_system_prompt(capture_id),
    )
    total = await loop.run_in_executor(
        None,
        lambda: store.count_system_prompt_versions(),
    )

    current_hash = capture.get("system_prompt_hash")
    changed = (
        previous is not None
        and current_hash is not None
        and previous["hash"] != current_hash
    )

    return {
        "previous": previous,
        "changed": changed,
        "current_hash": current_hash,
        "total_versions": total,
    }


# ── utilities ──────────────────────────────────────────────────────

@app.get("/health")
async def health(request: Request):
    """Health check — shows proxy and web status."""
    import socket
    proxy_up = False
    try:
        s = socket.create_connection(("127.0.0.1", DEFAULT_CONFIG.proxy_port), timeout=1)
        s.close()
        proxy_up = True
    except Exception:
        pass
    store = _get_store(request)
    return {
        "web": "ok",
        "proxy": "up" if proxy_up else "down",
        "proxy_port": DEFAULT_CONFIG.proxy_port,
        "proxy_error": getattr(request.app.state, 'proxy_error', None),
        "db_path": store.db_path if store else None,
    }


@app.get("/cert")
async def download_cert():
    """Serve the mitmproxy CA certificate for installation."""
    cert_path = Path(DEFAULT_CONFIG.cert_path)
    if not cert_path.exists():
        # Also check a few common alternate locations.
        alt_paths = [
            Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem",
            Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.cer",
        ]
        for alt in alt_paths:
            if alt.exists():
                return FileResponse(alt, media_type="application/x-pem-file",
                                    filename="mitmproxy-ca-cert.pem")

        return HTMLResponse("""\
<h3>CA certificate not found</h3>
<p>The certificate is generated by mitmproxy on the first HTTPS request
that passes through it. To trigger generation:</p>
<ol>
  <li>Make sure the proxy is running (<code>uv run python3 launcher.py</code>)</li>
  <li>Run any HTTPS request through it, e.g.:
    <pre style="background:var(--bg-tertiary);padding:8px;border-radius:4px;">
HTTPS_PROXY=http://127.0.0.1:8083 curl -s https://example.com</pre>
  </li>
  <li>Refresh this page — the cert should now be available.</li>
</ol>
<p>Expected location: <code>{}</code></p>
<p><a href='/'>Back</a></p>""".format(cert_path), status_code=404)
    return FileResponse(cert_path, media_type="application/x-pem-file",
                        filename="mitmproxy-ca-cert.pem")


# ── WebSocket ─────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws.app.state.ws_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws.app.state.ws_clients.discard(ws)
