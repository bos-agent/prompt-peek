# prompt-peek UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace multi-page navigation with a single-page split-pane layout featuring markdown rendering for responses, structured tool cards, inline system prompt diffs, and keyboard navigation.

**Architecture:** Single `index.html` page with a CSS grid split: capture list (left) + detail pane (right). All rendering done in vanilla JS (`app.js`). Backend gets one new DB column (`system_prompt_hash`), one new API endpoint for diff lookup, and removal of two old page routes (`/capture/:id`, `/raw/:id`).

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, SQLite (aiosqlite), mitmproxy, vanilla JS, marked.js (CDN-free, vendored as static file)

---

### Task 1: DB schema — add system_prompt_hash column

**Files:**
- Modify: `src/prompt_peek/store.py:13-36`

- [ ] **Step 1: Add column to CREATE TABLE and add migration**

In `store.py`, update the `SCHEMA` constant — add `system_prompt_hash TEXT` to the CREATE TABLE, and add a migration block that alters existing tables:

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL NOT NULL,
    method      TEXT NOT NULL,
    url         TEXT NOT NULL,
    host        TEXT NOT NULL,
    path        TEXT NOT NULL,
    request_headers  TEXT,
    request_body     TEXT,
    response_status  INTEGER,
    response_headers TEXT,
    response_body    TEXT,
    api_type    TEXT DEFAULT 'unknown',
    duration_ms REAL,
    request_size    INTEGER DEFAULT 0,
    response_size   INTEGER DEFAULT 0,
    system_prompt_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_captures_timestamp ON captures (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_captures_host      ON captures (host);
CREATE INDEX IF NOT EXISTS idx_captures_api_type  ON captures (api_type);
CREATE INDEX IF NOT EXISTS idx_captures_sys_hash  ON captures (system_prompt_hash);
"""

MIGRATIONS = [
    """ALTER TABLE captures ADD COLUMN system_prompt_hash TEXT""",
]
```

- [ ] **Step 2: Add migration runner to Store._get_conn()**

After `conn.executescript(SCHEMA)`, add migration logic that catches the duplicate-column error:

```python
def _get_conn(self) -> sqlite3.Connection:
    conn = getattr(self._local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists
        conn.commit()
        self._local.conn = conn
        with self._conn_lock:
            self._all_connections.add(conn)
    return conn
```

- [ ] **Step 3: Run the app to verify migration**

Run: `uv run prompt-peek` (then Ctrl+C after it starts)

Expected: No errors, app starts normally. The `system_prompt_hash` column exists.

- [ ] **Step 4: Commit**

```bash
git add src/prompt_peek/store.py
git commit -m "feat: add system_prompt_hash column to captures table"
```

---

### Task 2: Store — update insert() and add system prompt query

**Files:**
- Modify: `src/prompt_peek/store.py:81-104` (insert method)
- Modify: `src/prompt_peek/store.py:152-157` (add new method after get_capture)

- [ ] **Step 1: Update insert() signature and SQL to accept system_prompt_hash**

```python
def insert(self, *, timestamp: float, method: str, url: str,
           host: str, path: str, request_headers: dict,
           request_body: Optional[str], response_status: Optional[int],
           response_headers: Optional[dict], response_body: Optional[str],
           api_type: str = "unknown", duration_ms: float = 0.0,
           request_size: int = 0, response_size: int = 0,
           system_prompt_hash: Optional[str] = None) -> int:
    conn = self._get_conn()
    cur = conn.execute(
        """INSERT INTO captures
           (timestamp, method, url, host, path,
            request_headers, request_body,
            response_status, response_headers, response_body,
            api_type, duration_ms, request_size, response_size,
            system_prompt_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (timestamp, method, url, host, path,
         json.dumps(request_headers or {}, ensure_ascii=False),
         request_body,
         response_status,
         json.dumps(response_headers or {}, ensure_ascii=False),
         response_body,
         api_type, duration_ms, request_size, response_size,
         system_prompt_hash),
    )
    conn.commit()
    return cur.lastrowid
```

- [ ] **Step 2: Add get_previous_system_prompt() method**

Insert after `get_capture()`:

```python
def get_previous_system_prompt(self, capture_id: int, host: str,
                               api_type: str) -> Optional[dict]:
    """Return the most recent capture (before *capture_id*) with the
    same host+api_type that has a system_prompt_hash, or None."""
    conn = self._get_conn()
    row = conn.execute(
        """SELECT id, system_prompt_hash,
                  json_extract(request_body, '$.messages') AS messages_json
           FROM captures
           WHERE host = ? AND api_type = ?
             AND id < ? AND system_prompt_hash IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        (host, api_type, capture_id),
    ).fetchone()
    if not row:
        return None
    # Extract system prompt content from messages JSON
    content = None
    try:
        messages = json.loads(row["messages_json"])
        for m in messages:
            if m.get("role") == "system":
                content = m.get("content", "")
                break
    except (json.JSONDecodeError, TypeError):
        pass
    return {
        "id": row["id"],
        "hash": row["system_prompt_hash"],
        "content": content,
    }


def count_system_prompt_versions(self, host: str, api_type: str) -> int:
    """Count how many distinct system_prompt_hash values exist for host+api_type."""
    conn = self._get_conn()
    row = conn.execute(
        """SELECT COUNT(DISTINCT system_prompt_hash)
           FROM captures
           WHERE host = ? AND api_type = ?
             AND system_prompt_hash IS NOT NULL""",
        (host, api_type),
    ).fetchone()
    return row[0] if row else 0
```

- [ ] **Step 3: Add system_prompt_hash to _row_to_dict**

Update the dict to include the new column:

```python
def _row_to_dict(self, row: sqlite3.Row) -> dict:
    return {
        ...
        "system_prompt_hash": row["system_prompt_hash"],
    }
```

- [ ] **Step 4: Verify by importing the module**

Run: `python3 -c "from prompt_peek.store import Store; print('OK')"` (from repo root with venv active)

Expected: `OK`, no import errors.

- [ ] **Step 5: Commit**

```bash
git add src/prompt_peek/store.py
git commit -m "feat: add system_prompt_hash to insert and query methods"
```

---

### Task 3: Proxy — compute system_prompt_hash on capture

**Files:**
- Modify: `src/prompt_peek/proxy_addon.py:88-131` (request method)

- [ ] **Step 1: Add hash computation and pass to store.insert()**

In `proxy_addon.py`, add `import hashlib` at the top. Then in the `request()` method, after parsing the body JSON and before `store.insert()`, compute the hash:

```python
import hashlib

# … inside request() method, after body_json is parsed:

# Compute system_prompt_hash
system_prompt_hash = None
if body_json and isinstance(body_json, dict):
    messages = body_json.get("messages", [])
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            content = m.get("content", "")
            if isinstance(content, str):
                system_prompt_hash = hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest()[:16]
            break

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
```

- [ ] **Step 2: Verify by importing**

Run: `python3 -c "from prompt_peek.proxy_addon import PromptPeekAddon; print('OK')"`

Expected: `OK`, no import errors.

- [ ] **Step 3: Commit**

```bash
git add src/prompt_peek/proxy_addon.py
git commit -m "feat: compute system_prompt_hash on request capture"
```

---

### Task 4: Backend API — add diff endpoint, remove old page routes

**Files:**
- Modify: `src/prompt_peek/web_server.py:97-114` (page routes and API routes)

- [ ] **Step 1: Remove old page routes**

Delete the `/capture/{capture_id}` and `/raw/{capture_id}` routes (lines 105-114 in `web_server.py`).

- [ ] **Step 2: Add system prompt previous endpoint**

Add after the `/api/captures/{capture_id}` endpoint:

```python
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

    host = capture.get("host", "")
    api_type = capture.get("api_type", "")

    previous = await loop.run_in_executor(
        None,
        lambda: store.get_previous_system_prompt(capture_id, host, api_type),
    )
    total = await loop.run_in_executor(
        None,
        lambda: store.count_system_prompt_versions(host, api_type),
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
```

- [ ] **Step 3: Start the app and test the new endpoint with curl**

Run: `uv run prompt-peek` in background, then:

```bash
# Test that old routes return 404
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9000/capture/1
# Expected: 404

# Test that new endpoint returns valid JSON (even with no data)
curl -s http://127.0.0.1:9000/api/captures/1/system-prompt-previous | python3 -m json.tool
# Expected: {"previous":null,"changed":false,"total_versions":0}
```

- [ ] **Step 4: Commit**

```bash
git add src/prompt_peek/web_server.py
git commit -m "feat: add system-prompt-previous API, remove old page routes"
```

---

### Task 5: Frontend — download and serve marked.js

**Files:**
- Create: `src/prompt_peek/static/marked.min.js`
- Modify: `src/prompt_peek/templates/index.html` (add script tag)

- [ ] **Step 1: Download marked.js v15**

Run:
```bash
curl -sL -o src/prompt_peek/static/marked.min.js \
  https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js
```

- [ ] **Step 2: Verify the file was downloaded**

Run: `wc -c src/prompt_peek/static/marked.min.js`
Expected: ~35-50KB file.

- [ ] **Step 3: Commit**

```bash
git add src/prompt_peek/static/marked.min.js
git commit -m "feat: add marked.js v15 for response markdown rendering"
```

---

### Task 6: CSS — split-pane layout and responsive

**Files:**
- Modify: `src/prompt_peek/static/style.css`

- [ ] **Step 1: Replace the layout CSS**

Replace the `.container` and layout-related styles. Add split-pane, detail pane, and responsive rules. Replace the existing `.container` rule and add these new rules after the header styles:

```css
/* ── split-pane layout ──────────────────────────────────── */

.split-pane {
    display: flex;
    height: calc(100vh - 53px - 28px); /* header + status bar */
    overflow: hidden;
}

.list-panel {
    flex: 1;
    min-width: 300px;
    overflow-y: auto;
    padding: 16px;
    border-right: 1px solid var(--border);
}

.detail-pane {
    flex: 0 0 45%;
    min-width: 400px;
    max-width: 55%;
    overflow-y: auto;
    padding: 16px;
    display: none;
    background: var(--bg);
}
.detail-pane.open { display: block; }

/* resizable divider */
.divider {
    width: 4px;
    cursor: col-resize;
    background: transparent;
    transition: background 0.15s;
    flex-shrink: 0;
}
.divider:hover, .divider.dragging { background: var(--accent); }

/* status bar */
.status-bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 4px 16px;
    font-size: 11px;
    color: var(--text-muted);
    background: var(--bg-secondary);
    border-top: 1px solid var(--border);
    height: 28px;
}
.status-bar .ws-indicator { display: flex; align-items: center; gap: 4px; }
.ws-dot { width: 6px; height: 6px; border-radius: 50%; }
.ws-dot.connected { background: var(--accent-green); }
.ws-dot.disconnected { background: var(--accent-yellow); }

/* ── responsive ─────────────────────────────────────────── */

@media (max-width: 768px) {
    .split-pane { flex-direction: column; }
    .list-panel { border-right: none; border-bottom: 1px solid var(--border); max-height: 50%; }
    .detail-pane {
        position: fixed; inset: 0; z-index: 10;
        max-width: none; flex: none;
        padding-top: 40px;
    }
    .detail-pane .close-btn {
        display: block;
        position: absolute; top: 12px; right: 16px;
        background: none; border: none; color: var(--text-muted);
        font-size: 18px; cursor: pointer;
    }
    .divider { display: none; }
}
```

- [ ] **Step 2: Remove old styles that reference removed templates**

Delete the `raw view` and `raw tabs` CSS blocks (lines 277-293 in current `style.css`). Keep the detail view and message styles — they'll be reused.

- [ ] **Step 3: Start the app and verify the page renders without CSS errors**

Run: `uv run prompt-peek`, open http://127.0.0.1:9000

Expected: Page loads, no CSS 404s, layout is split-pane ready (list panel visible).

- [ ] **Step 4: Commit**

```bash
git add src/prompt_peek/static/style.css
git commit -m "feat: add split-pane layout CSS with responsive rules"
```

---

### Task 7: HTML — restructure index.html for split pane

**Files:**
- Modify: `src/prompt_peek/templates/index.html`

- [ ] **Step 1: Rewrite the template with split-pane structure**

Replace all of `index.html`:

```html
{% extends "base.html" %}
{% block title %}prompt-peek · captures{% endblock %}

{% block content %}
<div class="split-pane">
    <!-- Left: list -->
    <div class="list-panel" id="list-panel">
        <div class="toolbar">
            <select id="filter-api-type">
                <option value="">All APIs</option>
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
                <option value="google">Google</option>
                <option value="azure">Azure</option>
                <option value="chat">Other Chat</option>
                <option value="unknown">Unknown</option>
            </select>
            <input type="text" id="search" placeholder="Search URL or body…">
            <span class="count" id="count">0 captures</span>
        </div>
        <ul class="capture-list" id="capture-list"></ul>
        <div class="empty-state" id="empty-state">
            <h2>Waiting for traffic</h2>
            <p>No LLM API calls intercepted yet. Follow the steps below.</p>
            <div style="text-align:left;max-width:520px;margin:24px auto 0;font-size:13px;line-height:2;">
                <div style="margin-bottom:16px;padding:12px;background:var(--bg-tertiary);border-radius:6px;">
                    <strong>1. Install the CA certificate</strong><br>
                    <span style="color:var(--text-muted)">HTTPS traffic must be decrypted. Download and trust the cert:</span><br>
                    <a href="/cert" style="font-family:monospace;font-size:12px;">⬇ Download mitmproxy-ca-cert.pem</a><br>
                    <span style="color:var(--text-muted);font-size:11px;">macOS: double-click → add to Keychain → mark as "Always Trust"</span>
                </div>
                <div style="margin-bottom:16px;padding:12px;background:var(--bg-tertiary);border-radius:6px;">
                    <strong>2. Set proxy environment variables</strong><br>
                    <code style="display:block;background:var(--bg);padding:6px 10px;border-radius:4px;margin:4px 0;">
                        export HTTP_PROXY=http://127.0.0.1:8083<br>
                        export HTTPS_PROXY=http://127.0.0.1:8083
                    </code>
                </div>
                <div style="padding:12px;background:var(--bg-tertiary);border-radius:6px;">
                    <strong>3. Run your coding agent</strong><br>
                    <span style="color:var(--text-muted);font-size:11px;">Captures will appear here in real time. The proxy listens on port <strong>8083</strong>.</span>
                </div>
            </div>
        </div>
    </div>

    <!-- Divider -->
    <div class="divider" id="divider"></div>

    <!-- Right: detail pane -->
    <div class="detail-pane" id="detail-pane">
        <button class="close-btn" onclick="closeDetail()" title="Close (Esc)">&times;</button>
        <div id="detail-loading" style="text-align:center;padding:40px;display:none;">
            <span class="spinner"></span> Loading…
        </div>
        <div id="detail-content"></div>
        <div id="detail-empty" style="text-align:center;padding:80px 20px;color:var(--text-muted);">
            <p>Select a capture to inspect</p>
        </div>
        <div id="detail-deleted" style="text-align:center;padding:40px;display:none;">
            <p>This capture was deleted.</p>
        </div>
    </div>
</div>

<div class="status-bar">
    <span id="status-count">0 captures</span>
    <span class="ws-indicator">
        <span class="ws-dot" id="ws-dot"></span>
        <span id="ws-label">connecting</span>
    </span>
</div>

<script src="/static/marked.min.js"></script>
<script>
// ── State ──────────────────────────────────────────────────
const state = {
    captures: [],
    totalCount: 0,
    selectedId: null,
    selectedData: null,
    wsConnected: false,
};

// ── DOM refs ───────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);

// ── API ────────────────────────────────────────────────────
async function fetchCaptures() {
    const apiType = $('#filter-api-type').value;
    const search  = $('#search').value;
    const params  = new URLSearchParams({ limit: 200 });
    if (apiType) params.set('api_type', apiType);
    if (search)  params.set('search', search);
    const res = await fetch('/api/captures?' + params);
    const data = await res.json();
    state.captures = data.captures;
    state.totalCount = data.total;
    renderList();
}

async function fetchCapture(id) {
    const res = await fetch('/api/captures/' + id);
    const data = await res.json();
    return data.capture;
}

async function fetchSysPromptPrevious(id) {
    const c = state.selectedData;
    if (!c) return null;
    const res = await fetch('/api/captures/' + id + '/system-prompt-previous');
    return await res.json();
}

// ── WebSocket ──────────────────────────────────────────────
function connectWS() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(proto + '://' + location.host + '/ws');
    ws.onopen = () => {
        state.wsConnected = true;
        $('#ws-dot').className = 'ws-dot connected';
        $('#ws-label').textContent = 'live';
    };
    ws.onclose = () => {
        state.wsConnected = false;
        $('#ws-dot').className = 'ws-dot disconnected';
        $('#ws-label').textContent = 'reconnecting…';
        setTimeout(connectWS, 2000);
    };
    ws.onmessage = (ev) => {
        const events = JSON.parse(ev.data);
        for (const e of events) {
            if (e.method) {
                state.captures.unshift({
                    id: e.id, method: e.method, host: e.host,
                    path: e.path, url: 'https://' + e.host + e.path,
                    api_type: e.api_type, response_status: null,
                    duration_ms: null, request_size: e.request_size,
                    response_size: null,
                });
                state.totalCount++;
            }
            if (e.response_status !== undefined || e.error !== undefined) {
                const found = state.captures.find(c => c.id === e.id);
                if (found) {
                    found.response_status = e.response_status || 0;
                    found.duration_ms = e.duration_ms || 0;
                    found.response_size = e.response_size || 0;
                }
                if (state.selectedId === e.id) {
                    fetchCapture(e.id).then(c => {
                        state.selectedData = c;
                        if (c) renderDetail();
                    });
                }
            }
        }
        renderList();
        if (events.some(e => e.method)) {
            const first = $('.capture-item');
            if (first) { first.classList.add('new-item');
                setTimeout(() => first.classList.remove('new-item'), 600); }
        }
    };
}

// ── Filters ────────────────────────────────────────────────
$('#filter-api-type').onchange = () => { fetchCaptures(); };
let searchTimer;
$('#search').oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(fetchCaptures, 300);
};

// ── Init ───────────────────────────────────────────────────
fetchCaptures();
connectWS();
</script>
{% endblock %}
```

- [ ] **Step 2: Start the app and verify the split-pane renders**

Run: `uv run prompt-peek`, open http://127.0.0.1:9000

Expected: Left panel with toolbar and "Waiting for traffic" empty state. Right panel shows "Select a capture to inspect". Status bar at bottom.

- [ ] **Step 3: Commit**

```bash
git add src/prompt_peek/templates/index.html
git commit -m "feat: restructure index.html with split-pane layout"
```

---

### Task 8: JS — list rendering and capture selection

**Files:**
- Modify: `src/prompt_peek/templates/index.html` (add JS functions below the init block)

- [ ] **Step 1: Add list rendering and selection handler JS**

Append to the `<script>` block in `index.html` (after `fetchCaptures(); connectWS();`):

```javascript
// ── List rendering ─────────────────────────────────────────
function renderList() {
    const list  = $('#capture-list');
    const empty = $('#empty-state');
    const count = $('#count');
    const statusCount = $('#status-count');

    count.textContent = state.totalCount + ' capture' + (state.totalCount !== 1 ? 's' : '');
    statusCount.textContent = count.textContent;
    empty.style.display = state.captures.length === 0 ? 'block' : 'none';
    list.innerHTML = '';

    for (const c of state.captures) {
        const li = document.createElement('li');
        li.className = 'capture-item';
        if (c.id === state.selectedId) li.classList.add('selected');
        li.setAttribute('data-id', c.id);
        li.onclick = () => selectCapture(c.id);

        const stClass = statusClass(c.response_status);
        li.innerHTML = `
            <span class="method POST">${escapeHTML(c.method)}</span>
            <span class="host" title="${escapeHTML(c.url)}">${escapeHTML(c.host)}${escapeHTML(c.path)}</span>
            <span class="api-badge ${c.api_type}">${escapeHTML(c.api_type)}</span>
            <span class="status ${stClass}">${c.response_status || '…'}</span>
            <span class="duration">${formatDuration(c.duration_ms)}</span>
            <span class="size">${formatSize(c.request_size)}</span>
        `;
        list.appendChild(li);
    }
}

// ── Capture selection ──────────────────────────────────────
async function selectCapture(id) {
    state.selectedId = id;
    renderList();  // highlight selected row

    $('#detail-empty').style.display = 'none';
    $('#detail-deleted').style.display = 'none';
    $('#detail-content').innerHTML = '';
    $('#detail-loading').style.display = 'block';
    $('#detail-pane').classList.add('open');

    const c = await fetchCapture(id);
    state.selectedData = c;
    $('#detail-loading').style.display = 'none';

    if (!c) {
        $('#detail-deleted').style.display = 'block';
        return;
    }
    await renderDetail();

    // Scroll selected row into view
    const row = document.querySelector(`.capture-item[data-id="${id}"]`);
    if (row) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function closeDetail() {
    state.selectedId = null;
    state.selectedData = null;
    $('#detail-pane').classList.remove('open');
    renderList();
}
```

- [ ] **Step 2: Add `.selected` state style for capture-item**

In `style.css`, add after the `.capture-item` rules:

```css
.capture-item.selected {
    border-color: var(--accent);
    background: #1a3a5c;
}
```

- [ ] **Step 3: Verify clicking a capture opens the detail pane**

Run the app, click a capture in the list (or add a test capture via curl if none exist). Verify the right pane opens with the loading spinner, then shows "Loading…" gone (detail content still empty — next task fills it in).

- [ ] **Step 4: Commit**

```bash
git add src/prompt_peek/templates/index.html src/prompt_peek/static/style.css
git commit -m "feat: add list rendering with capture selection and detail pane toggle"
```

---

### Task 9: JS — detail pane: metadata bar + system prompt with diff

**Files:**
- Modify: `src/prompt_peek/templates/index.html` (add JS functions)

- [ ] **Step 1: Add content extraction and diff utility functions**

Append to the `<script>` block:

```javascript
// ── Content extraction ─────────────────────────────────────
function extractSysPrompt(messages) {
    if (!messages) return null;
    const sys = messages.find(m => m.role === 'system');
    if (!sys) return null;
    return typeof sys.content === 'string' ? sys.content : formatJSON(sys.content);
}

function extractResponseContent(c) {
    const body = c.response_body_parsed;
    if (!body) return { type: 'empty' };

    if (c.api_type === 'openai' && body.choices) {
        const msg = body.choices[0]?.message;
        let text = msg?.content || '';
        const toolCalls = msg?.tool_calls;
        return {
            type: 'text',
            content: text,
            finishReason: body.choices[0]?.finish_reason,
            model: body.model,
            usage: body.usage,
            toolCalls: toolCalls,
        };
    }
    if (c.api_type === 'anthropic') {
        const textParts = (body.content || []).filter(p => p.type === 'text');
        const toolParts = (body.content || []).filter(p => p.type === 'tool_use');
        return {
            type: 'text',
            content: textParts.map(p => p.text).join('\n'),
            finishReason: body.stop_reason,
            model: body.model,
            usage: body.usage,
            toolCalls: toolParts,
        };
    }
    if (c.api_type === 'google' && body.candidates) {
        const parts = (body.candidates[0]?.content?.parts || []).map(p => p.text || '').join('\n');
        return {
            type: 'text',
            content: parts,
            finishReason: body.candidates[0]?.finishReason,
            model: body.modelVersion,
            usage: body.usageMetadata,
        };
    }
    return { type: 'json', json: body };
}

// ── Simple line diff ───────────────────────────────────────
function lineDiff(oldText, newText) {
    const oldLines = (oldText || '').split('\n');
    const newLines = (newText || '').split('\n');
    const result = [];
    let oi = 0, ni = 0;
    while (oi < oldLines.length || ni < newLines.length) {
        if (oi < oldLines.length && ni < newLines.length && oldLines[oi] === newLines[ni]) {
            result.push({ type: 'same', text: oldLines[oi] });
            oi++; ni++;
        } else if (ni < newLines.length) {
            result.push({ type: 'add', text: newLines[ni] });
            ni++;
        } else {
            result.push({ type: 'remove', text: oldLines[oi] });
            oi++;
        }
    }
    return result;
}

function renderDiff(oldText, newText) {
    const diff = lineDiff(oldText, newText);
    return diff.map(d => {
        const cls = d.type === 'add' ? 'diff-add' : d.type === 'remove' ? 'diff-remove' : '';
        return `<span class="${cls}">${d.type === 'add' ? '+' : d.type === 'remove' ? '-' : ' '} ${escapeHTML(d.text)}</span>`;
    }).join('\n');
}

// ── HTML sanitizer ─────────────────────────────────────────
function sanitizeHtml(html) {
    const el = document.createElement('div');
    el.innerHTML = html;
    // Strip dangerous elements
    el.querySelectorAll('script, iframe, object, embed').forEach(n => n.remove());
    // Strip dangerous attributes
    el.querySelectorAll('*').forEach(n => {
        for (const attr of [...n.attributes]) {
            if (attr.name.startsWith('on') || (
                attr.name === 'href' && /^javascript:/i.test(attr.value)
            )) {
                n.removeAttribute(attr.name);
            }
        }
    });
    return el.innerHTML;
}
```

- [ ] **Step 2: Add metadata bar and system prompt rendering functions**

```javascript
// ── Detail rendering ───────────────────────────────────────
async function renderDetail() {
    const c = state.selectedData;
    if (!c) return;

    const stClass = statusClass(c.response_status);

    let html = '';

    // Metadata bar
    html += `<div class="detail-header">
        <div class="url">${escapeHTML(c.method)} ${escapeHTML(c.url)}</div>
        <div class="detail-meta">
            <span>${formatTime(c.timestamp)}</span>
            <span>${escapeHTML(c.host)}</span>
            <span class="api-badge ${c.api_type}">${escapeHTML(c.api_type)}</span>
            <span class="status ${stClass}">${c.response_status || '…'}</span>
            <span>${c.duration_ms ? Math.round(c.duration_ms) + 'ms' : ''}</span>
            <span>req ${formatSize(c.request_size)} · res ${formatSize(c.response_size)}</span>
            <a href="#" onclick="toggleRaw();return false;" style="font-size:12px;">raw json</a>
        </div>
    </div>`;

    // System Prompt
    const sysContent = c.request_body_parsed
        ? extractSysPrompt(c.request_body_parsed.messages) : null;
    if (sysContent) {
        const prev = await fetchSysPromptPrevious(c.id);
        const changed = prev && prev.changed;
        const badgeClass = changed ? 'badge-changed' : 'badge-unchanged';
        const badgeText = changed
            ? `changed vs #${prev.previous.id}`
            : (prev.previous ? `unchanged vs #${prev.previous.id}` : 'first seen');

        html += `<div class="section">
            <div class="section-title">
                ⚙ System Prompt
                <span class="badge ${badgeClass}">${badgeText}</span>
                <span class="toggle" onclick="toggleSection(this)">toggle</span>
            </div>
            <div class="sys-prompt-body code-block">
                <pre class="sys-prompt-text">${escapeHTML(sysContent)}</pre>
                <pre class="sys-prompt-diff" style="display:none;">${prev && prev.previous ? renderDiff(prev.previous.content, sysContent) : ''}</pre>
            </div>`;
        if (changed !== undefined) {
            html += `<div style="margin-top:4px;font-size:12px;">
                <label><input type="checkbox" onchange="toggleSysDiff(this)"> Show diff</label>`;
            if (prev.total_versions > 1) {
                html += ` · <a href="#" onclick="filterByHostApiType('${escapeHTML(c.host)}','${escapeHTML(c.api_type)}');return false;">view all versions (${prev.total_versions})</a>`;
            }
            html += `</div>`;
        }
        html += `</div>`;
    }

    $('#detail-content').innerHTML = html;

    // Continue rendering remaining sections (next tasks)
    renderToolsSection(c);
    renderMessagesSection(c);
    renderResponseSection(c);
    renderHeadersSection(c);
}

function toggleSysDiff(checkbox) {
    const section = checkbox.closest('.section');
    const textEl = section.querySelector('.sys-prompt-text');
    const diffEl = section.querySelector('.sys-prompt-diff');
    textEl.style.display = checkbox.checked ? 'none' : 'block';
    diffEl.style.display = checkbox.checked ? 'block' : 'none';
}

function toggleSection(btn) {
    const block = btn.closest('.section').querySelector('.code-block');
    if (block) block.classList.toggle('hide');
}
```

- [ ] **Step 3: Add diff and badge CSS to style.css**

```css
/* ── system prompt diff ─────────────────────────────────── */
.sys-prompt-diff {
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
}
.diff-add { background: #0d2f1a; color: #3fb950; display: block; }
.diff-remove { background: #3d1f1f; color: #f85149; display: block; }

.badge {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
}
.badge-unchanged { background: #1a3a2a; color: #3fb950; }
.badge-changed { background: #3a2e1a; color: #d2991d; }

.code-block.hide { display: none; }
```

- [ ] **Step 4: Start app and verify metadata + system prompt render**

Run: `uv run prompt-peek`, open a capture. Verify the metadata bar and system prompt section render correctly. Check the change badge appears.

- [ ] **Step 5: Commit**

```bash
git add src/prompt_peek/templates/index.html src/prompt_peek/static/style.css
git commit -m "feat: add detail pane metadata bar and system prompt with diff"
```

---

### Task 10: JS — detail pane: tools section

**Files:**
- Modify: `src/prompt_peek/templates/index.html` (add renderToolsSection function)

- [ ] **Step 1: Add tool categorization and rendering function**

```javascript
function categorizeTool(name) {
    const n = name.toLowerCase();
    if (/read|get|list|find|search|grep|view|show|cat|ls|glob/i.test(n)) return 'tool-read';
    if (/write|create|edit|delete|remove|save|insert|update|patch|put|mv|cp|rm/i.test(n)) return 'tool-write';
    if (/bash|shell|exec|run|spawn|terminal|command|sh\b/i.test(n)) return 'tool-shell';
    if (/search|find|grep|query|lookup/i.test(n)) return 'tool-search';
    return 'tool-other';
}

function renderToolsSection(c) {
    const tools = c.request_body_parsed && c.request_body_parsed.tools;
    if (!tools || !tools.length) return;

    let html = `<div class="section">
        <div class="section-title">🔧 Tools (${tools.length})
            <span class="toggle" onclick="toggleSection(this)">toggle</span>
        </div>`;

    // Tag cloud
    html += '<div class="tool-tags">';
    for (const t of tools) {
        const cat = categorizeTool(t.name || t.function?.name || '');
        html += `<span class="tool-tag ${cat}" onclick="document.getElementById('tool-${CSS.escape(String(t.name || t.function?.name || ''))}').scrollIntoView({behavior:'smooth'});this.closest('.section').querySelectorAll('details').forEach(d=>d.open=true)">${escapeHTML(t.name || t.function?.name || '(unnamed)')}</span>`;
    }
    html += '</div>';

    // Tool cards
    for (const t of tools) {
        const name = t.name || t.function?.name || '(unnamed)';
        const desc = t.description || (t.function && t.function.description) || '';
        const schema = t.input_schema || (t.function && t.function.parameters) || null;
        const required = (schema && schema.required) || [];
        const props = (schema && schema.properties) || {};

        html += `<details class="tool-card" id="tool-${escapeHTML(name)}">
            <summary>${escapeHTML(name)}</summary>
            <div class="tool-desc">${escapeHTML(desc)}</div>`;
        if (Object.keys(props).length) {
            html += '<div class="tool-params">';
            for (const [pname, pinfo] of Object.entries(props)) {
                const req = required.includes(pname) ? ' <span class="param-required">required</span>' : '';
                html += `<span class="tool-param">${escapeHTML(pname)}${req} <span class="param-type">${escapeHTML(pinfo.type || 'any')}</span></span>`;
            }
            html += '</div>';
        }
        html += '</details>';
    }

    html += '</div>';
    $('#detail-content').insertAdjacentHTML('beforeend', html);
}
```

- [ ] **Step 2: Add tool card and tag CSS to style.css**

```css
/* ── tools section ───────────────────────────────────────── */
.tool-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-bottom: 10px;
}
.tool-tag {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    cursor: pointer;
    font-family: var(--font-mono);
    transition: opacity 0.15s;
}
.tool-tag:hover { opacity: 0.8; }
.tool-tag.tool-read   { background: #1a3a5c; color: #58a6ff; }
.tool-tag.tool-write  { background: #1a3a2a; color: #3fb950; }
.tool-tag.tool-shell  { background: #3a2e1a; color: #d2991d; }
.tool-tag.tool-search { background: #2a1a3a; color: #bc8cff; }
.tool-tag.tool-other  { background: var(--bg-tertiary); color: var(--text-muted); }

.tool-card {
    background: var(--bg-tertiary);
    border-radius: var(--radius);
    padding: 8px 12px;
    margin-bottom: 6px;
}
.tool-card summary {
    font-family: var(--font-mono);
    font-size: 13px;
    cursor: pointer;
    color: var(--accent);
}
.tool-desc {
    font-size: 12px;
    color: var(--text-muted);
    margin: 4px 0 8px;
}
.tool-params {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
}
.tool-param {
    font-size: 11px;
    font-family: var(--font-mono);
    background: var(--bg);
    padding: 2px 8px;
    border-radius: 4px;
}
.param-type { color: var(--text-muted); }
.param-required { color: var(--accent-red); font-size: 10px; }
```

- [ ] **Step 3: Verify tools section renders**

Run the app, open a capture that has tools. Verify tag cloud and collapsible cards render.

- [ ] **Step 4: Commit**

```bash
git add src/prompt_peek/templates/index.html src/prompt_peek/static/style.css
git commit -m "feat: add tools section with tag cloud and collapsible cards"
```

---

### Task 11: JS — detail pane: messages, response, and headers sections

**Files:**
- Modify: `src/prompt_peek/templates/index.html` (add renderMessagesSection, renderResponseSection, renderHeadersSection)

- [ ] **Step 1: Add messages section renderer**

```javascript
function renderMessagesSection(c) {
    const messages = c.request_body_parsed && c.request_body_parsed.messages;
    if (!messages) return;

    const nonSys = messages.filter(m => m.role !== 'system');
    if (!nonSys.length) {
        let html = `<div class="section">
            <div class="section-title">📨 Messages (0)</div>
            <div style="color:var(--text-muted);font-size:13px;padding:12px;">No conversation messages</div>
        </div>`;
        $('#detail-content').insertAdjacentHTML('beforeend', html);
        return;
    }

    let html = `<div class="section">
        <div class="section-title">📨 Messages (${nonSys.length})
            <span class="toggle" onclick="toggleSection(this)">toggle</span>
        </div>
        <div class="message-list">`;

    for (const msg of nonSys) {
        const role = msg.role || 'unknown';
        const rawContent = typeof msg.content === 'string'
            ? msg.content
            : (msg.content ? formatJSON(msg.content) : '(empty)');
        const content = escapeHTML(rawContent);
        const isLong = rawContent.length > 200;
        const visible = isLong ? content.slice(0, 200) : content;
        const isToolMsg = role === 'tool' || role === 'tool_call' || role === 'function';
        const extraClass = isToolMsg ? ' message-tool' : '';

        html += `<div class="message-item role-${role}${extraClass}">
            <div class="message-header">
                <span class="message-role">${role}</span>
                ${msg.name ? '<span class="message-name">' + escapeHTML(msg.name) + '</span>' : ''}
                ${msg.tool_call_id ? '<span class="message-name">' + escapeHTML(msg.tool_call_id) + '</span>' : ''}
            </div>
            <div class="message-content${isLong ? ' collapsed' : ''}">
                ${visible}
                ${isLong ? '<span class="expand-btn" onclick="this.parentElement.classList.toggle(\'collapsed\');this.textContent=this.parentElement.classList.contains(\'collapsed\')?\'… show more (\'+Math.round(rawContent.length/1000)+\'KB)\':\'\'">… show more (' + Math.round(rawContent.length / 1000) + 'KB)</span>' : ''}
            </div>
        </div>`;
    }

    html += '</div></div>';
    $('#detail-content').insertAdjacentHTML('beforeend', html);
}
```

- [ ] **Step 2: Add response section renderer (with markdown)**

```javascript
function renderResponseSection(c) {
    const extracted = extractResponseContent(c);
    let bodyHTML = '';

    if (extracted.type === 'empty') {
        bodyHTML = '<div style="color:var(--text-muted);">(empty)</div>';
    } else if (extracted.type === 'text') {
        const mdContent = extracted.content || '(empty)';
        const truncated = mdContent.length > 500000;
        const toRender = truncated ? mdContent.slice(0, 500000) : mdContent;
        let rendered;
        try {
            rendered = sanitizeHtml(marked.parse(toRender));
        } catch (e) {
            rendered = `<pre>${escapeHTML(toRender)}</pre>`;
        }
        bodyHTML = `<div class="response-markdown">${rendered}</div>`;
        if (truncated) {
            bodyHTML += `<div style="margin-top:8px;color:var(--accent-yellow);font-size:12px;">
                Content truncated at 500KB.
                <a href="#" onclick="this.closest('.section').querySelector('.toggle').click();return false;">View raw</a>
            </div>`;
        }
        // Metadata
        const meta = [];
        if (extracted.finishReason) meta.push(`finish: ${extracted.finishReason}`);
        if (extracted.model) meta.push(`model: ${extracted.model}`);
        if (extracted.usage) {
            const u = extracted.usage;
            const inp = u.input_tokens || u.prompt_tokens || u.inputTokenCount;
            const out = u.output_tokens || u.completion_tokens || u.candidatesTokenCount;
            if (inp || out) meta.push(`tokens: ${inp || '?'} in / ${out || '?'} out`);
        }
        if (meta.length) {
            bodyHTML += `<div class="response-meta">${meta.join(' · ')}</div>`;
        }
    } else {
        bodyHTML = `<pre>${escapeHTML(formatJSON(extracted.json))}</pre>`;
    }

    const respBodyRaw = `<pre style="display:none;" class="resp-raw">${escapeHTML(c.response_body_parsed ? formatJSON(c.response_body_parsed) : (c.response_body || '(empty)'))}</pre>`;

    let html = `<div class="section">
        <div class="section-title">📥 Response (${c.response_status || '…'})
            <span class="toggle" onclick="toggleResponseView(this)">raw</span>
        </div>
        <div class="response-rendered code-block">${bodyHTML}</div>
        <div class="code-block response-raw" style="display:none;">${respBodyRaw}</div>
    </div>`;

    $('#detail-content').insertAdjacentHTML('beforeend', html);
}

function toggleResponseView(btn) {
    const section = btn.closest('.section');
    const rendered = section.querySelector('.response-rendered');
    const raw = section.querySelector('.response-raw');
    if (raw.style.display === 'none') {
        rendered.style.display = 'none';
        raw.style.display = 'block';
        btn.textContent = 'rendered';
    } else {
        rendered.style.display = 'block';
        raw.style.display = 'none';
        btn.textContent = 'raw';
    }
}
```

- [ ] **Step 3: Add headers section renderer**

```javascript
function renderHeadersSection(c) {
    let html = `<div class="section">
        <div class="section-title">📤 Request Headers
            <span class="toggle" onclick="toggleSection(this)">toggle</span>
        </div>
        <div class="code-block"><pre>${escapeHTML(formatJSON(c.request_headers))}</pre></div>
    </div>

    <div class="section">
        <div class="section-title">📥 Response Headers
            <span class="toggle" onclick="toggleSection(this)">toggle</span>
        </div>
        <div class="code-block"><pre>${escapeHTML(formatJSON(c.response_headers))}</pre></div>
    </div>`;

    $('#detail-content').insertAdjacentHTML('beforeend', html);
}
```

- [ ] **Step 4: Add response markdown and tool message CSS**

```css
/* ── response markdown ───────────────────────────────────── */
.response-markdown { font-size: 13px; line-height: 1.6; }
.response-markdown pre {
    background: var(--bg);
    padding: 10px;
    border-radius: var(--radius);
    overflow-x: auto;
    font-family: var(--font-mono);
    font-size: 12px;
}
.response-markdown code {
    font-family: var(--font-mono);
    font-size: 12px;
    background: var(--bg);
    padding: 1px 4px;
    border-radius: 3px;
}
.response-markdown p { margin-bottom: 8px; }
.response-markdown ul, .response-markdown ol { margin: 4px 0 8px 20px; }

.response-meta {
    margin-top: 8px;
    font-size: 11px;
    color: var(--text-muted);
    border-top: 1px solid var(--border);
    padding-top: 6px;
}

/* tool messages */
.message-tool {
    opacity: 0.75;
    font-size: 11px;
}
```

- [ ] **Step 5: Verify all sections render**

Run: `uv run prompt-peek`, open a capture with all data. Verify messages, response (markdown), and headers render.

- [ ] **Step 6: Commit**

```bash
git add src/prompt_peek/templates/index.html src/prompt_peek/static/style.css
git commit -m "feat: add messages, markdown response, and headers to detail pane"
```

---

### Task 12: JS — keyboard navigation, divider, and raw toggle

**Files:**
- Modify: `src/prompt_peek/templates/index.html` (add keyboard handler and misc functions)

- [ ] **Step 1: Add keyboard navigation**

```javascript
// ── Keyboard navigation ────────────────────────────────────
document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;

    if (e.key === 'Escape') {
        closeDetail();
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const currentIdx = state.captures.findIndex(c => c.id === state.selectedId);
        const nextIdx = e.key === 'ArrowDown'
            ? Math.min(currentIdx + 1, state.captures.length - 1)
            : Math.max(currentIdx - 1, 0);
        if (state.captures[nextIdx]) {
            selectCapture(state.captures[nextIdx].id);
        }
    }
    if (e.key === 'r' && state.selectedId) {
        const firstRawLink = $('#detail-content').querySelector('a[href="#"][onclick*="toggleRaw"]');
        if (firstRawLink) {
            e.preventDefault();
            showRaw();
        }
    }
});

// ── Divider drag ───────────────────────────────────────────
const divider = $('#divider');
const detailPane = $('#detail-pane');

divider.addEventListener('mousedown', (e) => {
    e.preventDefault();
    divider.classList.add('dragging');
    const startX = e.clientX;
    const startWidth = detailPane.offsetWidth;

    const onMove = (ev) => {
        const dx = startX - ev.clientX;
        const newWidth = Math.min(Math.max(startWidth + dx, 300), window.innerWidth * 0.6);
        detailPane.style.flex = '0 0 ' + newWidth + 'px';
        detailPane.style.maxWidth = 'none';
    };
    const onUp = () => {
        divider.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
});

// ── Raw view toggle (entire capture as raw JSON) ────────────
function toggleRaw() {
    if ($('#detail-content').classList.contains('raw-mode')) {
        renderDetail();
        $('#detail-content').classList.remove('raw-mode');
    } else {
        showRaw();
    }
}

function showRaw() {
    const c = state.selectedData;
    if (!c) return;
    const clone = {...c};
    delete clone.request_body_parsed;
    delete clone.response_body_parsed;
    $('#detail-content').innerHTML = `<div class="code-block"><pre>${escapeHTML(formatJSON(clone))}</pre></div>`;
    $('#detail-content').classList.add('raw-mode');
}

function filterByHostApiType(host, apiType) {
    $('#filter-api-type').value = apiType;
    fetchCaptures().then(() => {
        $('#search').value = host;
        fetchCaptures();
    });
}
```

- [ ] **Step 2: Verify keyboard navigation and divider drag**

Run the app. Test `↑` `↓` to navigate, `Esc` to close, `r` for raw toggle. Drag the divider to resize panes.

- [ ] **Step 3: Commit**

```bash
git add src/prompt_peek/templates/index.html
git commit -m "feat: add keyboard navigation, divider drag resize, and raw toggle"
```

---

### Task 13: Cleanup — remove old templates, update base.html nav

**Files:**
- Delete: `src/prompt_peek/templates/capture.html`
- Delete: `src/prompt_peek/templates/raw.html`
- Modify: `src/prompt_peek/templates/base.html`

- [ ] **Step 1: Delete old templates and simplify base.html nav**

```bash
rm src/prompt_peek/templates/capture.html src/prompt_peek/templates/raw.html
```

Update `base.html` nav — remove the "Captures" link (single page now, no other pages to link to):

```html
<header class="header">
    <h1><span class="logo">◉</span> prompt-peek</h1>
    <nav>
        <a href="/cert">CA Cert</a>
    </nav>
</header>
```

- [ ] **Step 2: Verify app starts and no broken references**

Run: `uv run prompt-peek`, open the app. Check browser console for 404 errors. Page should load without errors.

- [ ] **Step 3: Commit**

```bash
git rm src/prompt_peek/templates/capture.html src/prompt_peek/templates/raw.html
git add src/prompt_peek/templates/base.html
git commit -m "chore: remove old capture/raw templates, simplify nav"
```

---

### Task 14: Integration — end-to-end verification

- [ ] **Step 1: Start the app fresh**

Run: `uv run prompt-peek`

- [ ] **Step 2: Verify list loads**

Open http://127.0.0.1:9000 — "Waiting for traffic" or existing captures render in the left panel.

- [ ] **Step 3: Test with a real capture**

With proxy env vars set (`HTTP_PROXY=http://127.0.0.1:8083 HTTPS_PROXY=http://127.0.0.1:8083`), run a curl to an LLM API or use a coding agent. Verify the capture appears in the list via WebSocket.

- [ ] **Step 4: Click the capture — verify all sections**

Click the capture in the list. Verify:
- Metadata bar shows URL, status, timing, sizes
- System Prompt section shows content with change badge (and diff toggle works)
- Tools section shows tag cloud + collapsible cards
- Messages section shows role-colored messages
- Response section renders markdown (toggle to raw JSON)
- Headers sections expand/collapse

- [ ] **Step 5: Test keyboard nav**

`↑` `↓` to switch captures. `Esc` to close detail pane. `r` to toggle raw JSON.

- [ ] **Step 6: Test splitter drag**

Drag the divider between list and detail pane. Verify it resizes.

- [ ] **Step 7: Test responsive (mobile viewport)**

Resize browser to <768px wide. Verify detail pane goes full-width overlay.

- [ ] **Step 8: Test edge cases**
  - Capture with no system prompt → section hidden
  - Capture with no tools → section hidden
  - Capture with non-JSON body → "unparseable" label
  - WebSocket disconnect → amber indicator, auto-reconnect

- [ ] **Step 9: Commit any final fixes**

```bash
git add -A
git commit -m "chore: end-to-end integration fixes"
```

