"""prompt-peek entry point — starts the MITM proxy and web UI in one process."""

import asyncio
import threading
from pathlib import Path

import uvicorn

from .config import DEFAULT_CONFIG, Config
from .proxy_addon import EventBus, PromptPeekAddon
from .store import Store
from . import web_server


def _ensure_certs():
    """Pre-generate the mitmproxy CA certificate if missing."""
    cert_path = Path(DEFAULT_CONFIG.cert_path)
    if cert_path.exists():
        return

    try:
        cert_path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        print(f"[certs] cannot create {cert_path.parent} — cert will be generated on first HTTPS request")
        return

    try:
        from mitmproxy.certs import CertStore
        CertStore.create_store(
            path=cert_path.parent,
            basename="mitmproxy",
            key_size=2048,
        )
    except Exception as e:
        print(f"[certs] cert generation failed: {e} — will be created on first HTTPS request")
        return

    if cert_path.exists():
        print(f"[certs] CA certificate generated at {cert_path}")
    else:
        print(f"[certs] cert generation deferred (will be created on first HTTPS request)")


def _run_proxy(config: Config, store: Store, event_bus: EventBus):
    """Run mitmproxy in a dedicated OS thread with its own event loop."""
    from mitmproxy.options import Options
    from mitmproxy.tools.dump import DumpMaster

    async def _async_run():
        opts = Options(
            listen_host=config.proxy_host,
            listen_port=config.proxy_port,
        )
        # DumpMaster loads default addons (including proxyserver) automatically.
        master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        addon = PromptPeekAddon(store, config, event_bus)
        master.addons.add(addon)

        print(f"[proxy] listening on {config.proxy_host}:{config.proxy_port}", flush=True)
        await master.run()

    try:
        asyncio.run(_async_run())
    except Exception:
        import sys, traceback
        print("[proxy] FATAL — proxy thread crashed:", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        # Store the error on app.state so /health can report it.
        web_server.app.state.proxy_error = traceback.format_exc()


def main():
    config = DEFAULT_CONFIG

    # Pre-generate CA certificate so /cert endpoint works immediately.
    _ensure_certs()

    store = Store(config.db_path)
    event_bus = EventBus()

    # Wire shared state into the FastAPI app (replaces module-level globals).
    web_server.app.state.store = store
    web_server.app.state.event_bus = event_bus
    web_server.app.state.proxy_error = None

    # Start proxy in a background thread.
    proxy_thread = threading.Thread(
        target=_run_proxy,
        args=(config, store, event_bus),
        daemon=True,
    )
    proxy_thread.start()

    # Start web UI in the main thread.
    print(f"[web]   serving on http://{config.web_host}:{config.web_port}")
    uvicorn.run(
        web_server.app,
        host=config.web_host,
        port=config.web_port,
        log_level="info",
    )
