# prompt-peek

Intercept and inspect LLM API prompts and responses from coding agents via mitmproxy.

**prompt-peek** is a tool that allows you to inspect traffic between coding agents and LLM APIs. It runs an interception proxy (via `mitmproxy`) and provides a Web UI to view the captured prompts and responses in real-time.

## Features

- **Intercept API Traffic**: Uses a mitmproxy addon to intercept HTTP/HTTPS traffic matching known LLM API paths.
- **Web UI**: A FastAPI and Jinja2-based web interface to view live intercepts.
- **Real-Time Updates**: WebSocket integration broadcasts new requests and responses directly to the UI.
- **Persistent Storage**: Captures are stored in an SQLite database.

## Prerequisites

- Python 3.11+
- `uv` package manager

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/prompt-peek.git
cd prompt-peek

# Install dependencies using uv
uv sync
```

## Usage

1. Start `prompt-peek`:
   ```bash
   uv run prompt-peek
   ```
   This will start:
   - The intercept proxy on `127.0.0.1:8083`
   - The Web UI on `127.0.0.1:9000`

2. Configure your agent to use the proxy. For example, by setting environment variables:
   ```bash
   export HTTP_PROXY=http://127.0.0.1:8083
   export HTTPS_PROXY=http://127.0.0.1:8083
   export NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem
   export SSL_CERT_FILE=~/.mitmproxy/mitmproxy-ca-cert.pem
   export REQUESTS_CA_BUNDLE=~/.mitmproxy/mitmproxy-ca-cert.pem

   claude
   ```

   ```bash
   alias peek="env HTTP_PROXY=http://127.0.0.1:8083 HTTPS_PROXY=http://127.0.0.1:8083 NODE_EXTRA_CA_CERTS=$HOME/.mitmproxy/mitmproxy-ca-cert.pem SSL_CERT_FILE=$HOME/.mitmproxy/mitmproxy-ca-cert.pem REQUESTS_CA_BUNDLE=$HOME/.mitmproxy/mitmproxy-ca-cert.pem "

   peek claude
   ```


3. Open the Web UI at [http://127.0.0.1:9000](http://127.0.0.1:9000) to inspect incoming prompts and responses.

### Exporting System Prompts and Tools

You can render captured prompt/response records into standalone, beautifully styled HTML pages or clean Markdown documents. The script supports rendering individual capture records or scanning the database for all unique system prompts and tools.

```bash
# Render a specific capture record by its ID to HTML (default)
uv run python scripts/render_capture.py 42

# Render a specific capture to both HTML and Markdown and open the HTML in the browser
uv run python scripts/render_capture.py 42 --format both --browser

# Scan the database and extract all unique system prompts and tools (default output to captured/html/ and captured/md/)
uv run python scripts/render_capture.py --format both

# Scan and export from a custom database to a custom output directory
uv run python scripts/render_capture.py --db data/captures-old.db --out my_exports --format both --browser
```

## Architecture

- **`proxy_addon.py`**: mitmproxy addon that intercepts traffic and pushes events via a thread-safe EventBus.
- **`store.py`**: SQLite database (using `aiosqlite`) with WAL mode and thread-local connections.
- **`web_server.py`**: FastAPI application that serves the web interface, REST API, and WebSocket updates.
- **UI**: Server-rendered Jinja2 templates combined with vanilla JavaScript for real-time interactivity.
