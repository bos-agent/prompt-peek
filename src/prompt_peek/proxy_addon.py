"""mitmproxy addon that intercepts LLM API calls and persists them to the store."""

import hashlib
import json
import logging
import threading
import time
from collections import deque
from typing import Optional

from mitmproxy import http

from prompt_peek.config import Config
from prompt_peek.store import Store

logger = logging.getLogger(__name__)

# Max queued events before older ones are dropped.
_EVENT_QUEUE_CAP = 5000


def _extract_system_prompt_text(body: dict) -> Optional[str]:
    """Extract canonical system prompt text from a parsed request body.

    Handles both Anthropic (top-level ``system``) and OpenAI
    (``role: "system"`` inside ``messages``) formats, returning a
    single string that is used for hashing, comparison, and display.
    """
    # Anthropic format: top-level "system" (string or content-block array)
    sys_field = body.get("system")
    if isinstance(sys_field, str):
        return sys_field
    if isinstance(sys_field, list):
        parts = []
        for block in sys_field:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if not text.startswith("x-anthropic-billing-header:"):
                    parts.append(text)
        return "\n--\n".join(parts) if parts else None

    # Google format: top-level "system_instruction" or "systemInstruction"
    sys_inst = body.get("system_instruction") or body.get("systemInstruction")
    if sys_inst and isinstance(sys_inst, dict):
        parts = sys_inst.get("parts", [])
        if isinstance(parts, list):
            text_parts = [p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p]
            if text_parts:
                return "\n--\n".join(text_parts)

    # OpenAI format: role="system" inside messages
    for m in body.get("messages", []):
        if isinstance(m, dict) and m.get("role") == "system":
            content = m.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                text_parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
                ]
                if text_parts:
                    return "\n".join(text_parts)
            break
    return None


class EventBus:
    """Thread-safe ring-buffer queue for notifying the web server of new captures.

    ``push()`` is called from the proxy thread; ``drain()`` is called
    from the async event loop.  A ``threading.Event`` replaces the
    old polling loop — the web server blocks until events arrive.
    """

    def __init__(self):
        self._queue: deque[dict] = deque(maxlen=_EVENT_QUEUE_CAP)
        self._lock = threading.Lock()
        self._has_events = threading.Event()

    def push(self, event: dict):
        with self._lock:
            self._queue.append(event)
        self._has_events.set()

    def drain(self) -> list[dict]:
        """Return all pending events and clear the queue atomically."""
        with self._lock:
            events = list(self._queue)
            self._queue.clear()
        self._has_events.clear()
        return events

    def wait_for_events(self, timeout: float = 30.0) -> None:
        """Block until events are pushed (or *timeout* seconds pass).

        Call this from a thread-pool executor inside the async loop
        to get event-driven wake-ups instead of polling.
        """
        self._has_events.wait(timeout)


class PromptPeekAddon:
    """mitmproxy addon that captures LLM API traffic."""

    def __init__(self, store: Store, config: Config, event_bus: EventBus):
        self.store = store
        self.config = config
        self.event_bus = event_bus

    def _matches_api(self, path: str) -> bool:
        for pattern in self.config.api_patterns:
            if pattern in path:
                return True
        return False

    def _detect_api_type(self, path: str, body: Optional[dict]) -> str:
        if "/chat/completions" in path:
            return "openai"
        if "/messages" in path:
            return "anthropic"
        if "/v1beta/models/" in path:
            return "google"
        if "/openai/deployments/" in path:
            return "azure"
        # Heuristic: body has "messages" key → likely chat API
        if body and isinstance(body, dict) and "messages" in body:
            return "chat"
        return "unknown"

    # ── mitmproxy hooks ─────────────────────────────────────────

    def request(self, flow: http.HTTPFlow) -> None:
        logger.info("DEBUG request: host=%s path=%s method=%s",
                    flow.request.host, flow.request.path, flow.request.method)
        if not self._matches_api(flow.request.path):
            return

        body = flow.request.text
        body_json: Optional[dict] = None
        if body:
            try:
                body_json = json.loads(body)
            except (json.JSONDecodeError, TypeError):
                logger.debug("Failed to parse request body as JSON: %s", flow.request.path)

        api_type = self._detect_api_type(flow.request.path, body_json)
        req_headers = dict(flow.request.headers)

        # Compute system_prompt_hash
        system_prompt_hash = None
        if body_json and isinstance(body_json, dict):
            sys_text = _extract_system_prompt_text(body_json)
            if sys_text:
                system_prompt_hash = hashlib.sha256(
                    sys_text.encode("utf-8")
                ).hexdigest()[:16]

        capture_id = self.store.insert(
            timestamp=time.time(),
            method=flow.request.method,
            url=flow.request.url,
            host=flow.request.host,
            path=flow.request.path,
            request_headers=req_headers,
            request_body=body,
            response_status=None,
            response_headers=None,
            response_body=None,
            api_type=api_type,
            request_size=len(body) if body else 0,
            system_prompt_hash=system_prompt_hash,
        )

        # Stash on flow for response-phase correlation.
        flow._prompt_peek_capture_id = capture_id           # type: ignore[attr-defined]
        flow._prompt_peek_request_time = time.time()         # type: ignore[attr-defined]

        # Push a lightweight event for the web UI.
        self.event_bus.push({
            "id": capture_id,
            "timestamp": time.time(),
            "method": flow.request.method,
            "host": flow.request.host,
            "path": flow.request.path,
            "api_type": api_type,
            "request_size": len(body) if body else 0,
        })

    def response(self, flow: http.HTTPFlow) -> None:
        capture_id: Optional[int] = getattr(flow, "_prompt_peek_capture_id", None)
        if capture_id is None:
            return

        req_time: float = getattr(flow, "_prompt_peek_request_time", 0.0)
        duration_ms = (time.time() - req_time) * 1000

        resp_body = flow.response.text if flow.response else None
        resp_headers = dict(flow.response.headers) if flow.response else {}
        resp_status = flow.response.status_code if flow.response else 0

        self.store.update_response(
            capture_id,
            response_status=resp_status,
            response_headers=resp_headers,
            response_body=resp_body,
            duration_ms=duration_ms,
            response_size=len(resp_body) if resp_body else 0,
        )

        # Push response event for live update.
        self.event_bus.push({
            "id": capture_id,
            "response_status": resp_status,
            "duration_ms": duration_ms,
            "response_size": len(resp_body) if resp_body else 0,
        })

    def error(self, flow: http.HTTPFlow) -> None:
        capture_id: Optional[int] = getattr(flow, "_prompt_peek_capture_id", None)
        if capture_id is None:
            return
        req_time: float = getattr(flow, "_prompt_peek_request_time", 0.0)
        duration_ms = (time.time() - req_time) * 1000

        self.store.update_response(
            capture_id,
            response_status=0,
            response_headers={},
            response_body=None,
            duration_ms=duration_ms,
            response_size=0,
        )
        error_msg = str(flow.error) if flow.error else "unknown"
        logger.warning("Proxy error for capture %d: %s", capture_id, error_msg)
        self.event_bus.push({
            "id": capture_id,
            "error": error_msg,
        })
