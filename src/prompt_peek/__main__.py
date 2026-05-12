"""prompt-peek entry point — starts the MITM proxy and web UI in one process."""

import asyncio
import logging
import threading
import traceback
from pathlib import Path

import uvicorn

from .config import DEFAULT_CONFIG, Config
from .proxy_addon import EventBus, PromptPeekAddon
from .store import Store
from . import web_server

logger = logging.getLogger(__name__)


def _ensure_certs():
    """Pre-generate the mitmproxy CA certificate if missing."""
    cert_path = Path(DEFAULT_CONFIG.cert_path)
    if cert_path.exists():
        return

    try:
        cert_path.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        logger.warning("Cannot create %s — cert will be generated on first HTTPS request", cert_path.parent)
        return

    try:
        from mitmproxy.certs import CertStore
        CertStore.create_store(
            path=cert_path.parent,
            basename="mitmproxy",
            key_size=2048,
        )
    except Exception as e:
        logger.warning("Cert generation failed: %s — will be created on first HTTPS request", e)
        return

    if cert_path.exists():
        logger.info("CA certificate generated at %s", cert_path)
    else:
        logger.info("Cert generation deferred (will be created on first HTTPS request)")


def _run_proxy(config: Config, store: Store, event_bus: EventBus,
               shutdown_event: threading.Event):
    """Run mitmproxy in a dedicated OS thread with its own event loop."""
    from mitmproxy.options import Options
    from mitmproxy.tools.dump import DumpMaster

    async def _async_run():
        opts = Options(
            listen_host=config.proxy_host,
            listen_port=config.proxy_port,
        )
        master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        addon = PromptPeekAddon(store, config, event_bus)
        master.addons.add(addon)

        logger.info("Proxy listening on %s:%s", config.proxy_host, config.proxy_port)

        async def _watch_shutdown():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, shutdown_event.wait)
            logger.info("Proxy shutting down…")
            master.shutdown()

        done, _ = await asyncio.wait(
            [asyncio.ensure_future(master.run()),
             asyncio.ensure_future(_watch_shutdown())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Cancel whichever task didn't finish.
        for task in done:
            if not task.cancelled():
                try:
                    await task
                except Exception:
                    pass

    try:
        asyncio.run(_async_run())
    except Exception:
        tb = traceback.format_exc()
        logger.critical("Proxy thread crashed:\n%s", tb)
        web_server.app.state.proxy_error = tb


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = DEFAULT_CONFIG

    _ensure_certs()

    store = Store(config.db_path)
    event_bus = EventBus()
    shutdown_event = threading.Event()

    web_server.app.state.store = store
    web_server.app.state.event_bus = event_bus
    web_server.app.state.proxy_error = None
    web_server.app.state.shutdown_event = shutdown_event

    proxy_thread = threading.Thread(
        target=_run_proxy,
        args=(config, store, event_bus, shutdown_event),
        daemon=True,
    )
    proxy_thread.start()

    logger.info("Web UI serving on http://%s:%s", config.web_host, config.web_port)
    uvicorn.run(
        web_server.app,
        host=config.web_host,
        port=config.web_port,
        log_level="info",
    )
