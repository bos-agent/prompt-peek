"""Configuration for prompt-peek."""

from dataclasses import dataclass, field
from pathlib import Path


def _default_db_path() -> Path:
    # Prefer local data/ (visible, colocated). Fall back to ~/.prompt-peek/.
    local = Path.cwd() / "data" / "captures.db"
    home = Path.home() / ".prompt-peek" / "captures.db"
    for candidate in (local, home):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            test = candidate.parent / ".write_test"
            test.touch()
            test.unlink()
            return candidate
        except (OSError, PermissionError):
            continue
    return local


@dataclass
class Config:
    # Proxy
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8083

    # Web UI
    web_host: str = "127.0.0.1"
    web_port: int = 9000

    # Storage — uses ~/.prompt-peek by default, falls back to ./data/
    db_path: str = field(default_factory=lambda: str(
        _default_db_path()
    ))

    # mitmproxy CA certificate (for HTTPS interception)
    cert_path: str = field(default_factory=lambda: str(
        Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    ))

    # LLM API endpoint patterns to capture (path fragments)
    api_patterns: list[str] = field(default_factory=lambda: [
        "/chat/completions",          # OpenAI, DeepSeek, many providers
        "/v1/messages",               # Anthropic
        "/v1beta/models/",            # Google AI (v1beta)
        "/v1alpha/models/",           # Google AI (v1alpha)
        "/v1/models/",                # Google AI (v1)
        "/projects/",                 # Google Vertex AI (e.g., /v1/projects/.../locations/.../publishers/google/models/...)
        "/openai/deployments/",       # Azure OpenAI
        "/responses",                 # OpenAI Responses API
        "/codex",                     # Codex WebSocket & REST API
    ])


DEFAULT_CONFIG = Config()
