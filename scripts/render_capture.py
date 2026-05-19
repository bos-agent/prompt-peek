#!/usr/bin/env python3
"""Render a captured request into a static HTML file.

Supports both Anthropic and Gemini API formats.

Usage:
    python scripts/render_capture.py <record_id> [--db path/to/captures.db] [--out output.html]
"""

import argparse
import html
import json
import re
import sqlite3
import sys
from pathlib import Path

DB_DEFAULT = Path(__file__).resolve().parent.parent / "data" / "captures.db"


# ---------------------------------------------------------------------------
# Format detection & normalisation
# ---------------------------------------------------------------------------

def _detect_format(body: dict) -> str:
    """Return 'anthropic' or 'gemini' based on request body shape."""
    if "systemInstruction" in body or "contents" in body:
        return "gemini"
    return "anthropic"


def _extract_anthropic(body: dict, record: dict) -> dict:
    """Normalise an Anthropic-format request body."""
    system_blocks = body.get("system", [])
    system_text = ""
    if len(system_blocks) > 2 and isinstance(system_blocks[2], dict):
        system_text = system_blocks[2].get("text", "")
    elif len(system_blocks) > 0:
        last = system_blocks[-1]
        system_text = last.get("text", str(last)) if isinstance(last, dict) else str(last)

    return {
        "api_format": "Anthropic",
        "model": body.get("model", "unknown"),
        "system_text": system_text,
        "tools": body.get("tools", []),          # [{name, description, input_schema}]
        "msg_count": len(body.get("messages", [])),
        "max_tokens": body.get("max_tokens", "—"),
        "stream": body.get("stream", "—"),
    }


def _extract_gemini(body: dict, record: dict) -> dict:
    """Normalise a Gemini-format request body."""
    # System prompt lives in systemInstruction.parts[].text
    si = body.get("systemInstruction", {})
    parts = si.get("parts", []) if isinstance(si, dict) else []
    system_text = "\n\n".join(
        p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")
    )

    # Tools: tools[].functionDeclarations[] → flatten to [{name, description, parameters}]
    tools_flat: list[dict] = []
    for group in body.get("tools", []):
        for fd in group.get("functionDeclarations", []):
            tools_flat.append(fd)

    # Model: not in body, try to extract from the request URL path
    model = "unknown"
    url = record.get("url", "") or record.get("path", "")
    m = re.search(r"models/([^/:?]+)", url)
    if m:
        model = m.group(1)

    return {
        "api_format": "Gemini",
        "model": model,
        "system_text": system_text,
        "tools": tools_flat,                     # [{name, description, parameters}]
        "msg_count": len(body.get("contents", [])),
        "max_tokens": body.get("generationConfig", {}).get("maxOutputTokens", "—"),
        "stream": "—",
    }


# ---------------------------------------------------------------------------
# DB fetch
# ---------------------------------------------------------------------------

def fetch_record(db_path: str, record_id: int) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM captures WHERE id = ?", (record_id,)
    ).fetchone()
    conn.close()
    if not row:
        print(f"Record {record_id} not found.", file=sys.stderr)
        sys.exit(1)
    return dict(row)


# ---------------------------------------------------------------------------
# Tool normalisation (Anthropic vs Gemini schema shapes)
# ---------------------------------------------------------------------------

def _tool_props(tool: dict) -> tuple[dict, set]:
    """Return (properties_dict, required_set) regardless of format."""
    # Anthropic: input_schema.properties / input_schema.required
    schema = tool.get("input_schema") or tool.get("parameters") or {}
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    return props, required


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def render_html(record: dict) -> str:
    body = json.loads(record["request_body"])
    fmt = _detect_format(body)

    if fmt == "gemini":
        info = _extract_gemini(body, record)
    else:
        info = _extract_anthropic(body, record)

    tools_html = _render_tools(info["tools"])
    system_html = _render_system_prompt(info["system_text"])
    tool_count = len(info["tools"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Capture #{record['id']} — {html.escape(info['model'])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
{_css()}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <h1>Capture <span class="mono">#{record['id']}</span></h1>
    <div class="meta-chips">
      <span class="chip api-fmt">{info['api_format']}</span>
      <span class="chip model">{html.escape(info['model'])}</span>
      <span class="chip">{info['msg_count']} messages</span>
      <span class="chip">max_tokens: {info['max_tokens']}</span>
      <span class="chip">stream: {info['stream']}</span>
    </div>
  </div>
</header>

<main>
  <section id="system-prompt">
    <h2>System Prompt</h2>
    <div class="prompt-box">
      {system_html}
    </div>
  </section>

  <section id="tools">
    <h2>Tools <span class="count">{tool_count}</span></h2>
    <div class="tools-grid">
      {tools_html}
    </div>
  </section>
</main>

<footer>
  <p>Generated by <strong>prompt-peek</strong> &middot; record {record['id']} &middot; {record.get('host', '')}</p>
</footer>
</body>
</html>"""


def _render_system_prompt(text: str) -> str:
    """Convert the system prompt text into formatted HTML paragraphs."""
    if not text:
        return "<p class='empty'>No system prompt found.</p>"

    lines = text.split("\n")
    out = []
    in_code = False
    code_buf = []

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append(
                    '<pre><code>' + html.escape("\n".join(code_buf)) + '</code></pre>'
                )
                code_buf = []
                in_code = False
            else:
                in_code = True
            continue

        if in_code:
            code_buf.append(line)
            continue

        stripped = line.strip()
        if stripped.startswith("# "):
            out.append(f"<h3>{html.escape(stripped[2:])}</h3>")
        elif stripped.startswith("## "):
            out.append(f"<h4>{html.escape(stripped[3:])}</h4>")
        elif stripped.startswith("- "):
            out.append(f"<li>{html.escape(stripped[2:])}</li>")
        elif stripped == "":
            out.append("<br>")
        else:
            out.append(f"<p>{html.escape(line)}</p>")

    if in_code and code_buf:
        out.append(
            '<pre><code>' + html.escape("\n".join(code_buf)) + '</code></pre>'
        )

    return "\n".join(out)


def _render_tools(tools: list) -> str:
    parts = []
    for tool in tools:
        name = tool.get("name", "unnamed")
        desc = tool.get("description", "")
        props, required = _tool_props(tool)

        # Truncate long descriptions for the card, keep full in expandable
        short_desc = desc[:300].rsplit(" ", 1)[0] + "…" if len(desc) > 300 else desc
        has_more = len(desc) > 300

        params_rows = []
        for pname, pdef in props.items():
            ptype = pdef.get("type", "any")
            pdesc = pdef.get("description", "")
            if len(pdesc) > 200:
                pdesc = pdesc[:200].rsplit(" ", 1)[0] + "…"
            req_badge = '<span class="req">required</span>' if pname in required else ""
            params_rows.append(f"""
              <tr>
                <td class="param-name"><code>{html.escape(pname)}</code> {req_badge}</td>
                <td class="param-type"><code>{html.escape(ptype)}</code></td>
                <td class="param-desc">{html.escape(pdesc)}</td>
              </tr>""")

        params_table = ""
        if params_rows:
            params_table = f"""
            <table class="params-table">
              <thead><tr><th>Parameter</th><th>Type</th><th>Description</th></tr></thead>
              <tbody>{"".join(params_rows)}</tbody>
            </table>"""
        else:
            params_table = '<p class="no-params">No parameters</p>'

        details_block = ""
        if has_more:
            details_block = f"""
            <details class="full-desc">
              <summary>Full description</summary>
              <div class="desc-content">{html.escape(desc)}</div>
            </details>"""

        parts.append(f"""
        <div class="tool-card">
          <div class="tool-header">
            <h3>{html.escape(name)}</h3>
            <span class="param-count">{len(props)} params</span>
          </div>
          <p class="tool-desc">{html.escape(short_desc)}</p>
          {details_block}
          {params_table}
        </div>""")

    return "\n".join(parts)


def _css() -> str:
    return """
:root {
  --bg: #0d1117;
  --surface: #161b22;
  --surface-2: #1c2333;
  --border: #30363d;
  --border-light: #3d444d;
  --text: #e6edf3;
  --text-muted: #8b949e;
  --accent: #58a6ff;
  --accent-dim: #1f6feb33;
  --green: #3fb950;
  --orange: #d29922;
  --purple: #bc8cff;
  --red: #f85149;
  --radius: 8px;
  --font: 'Inter', -apple-system, sans-serif;
  --mono: 'JetBrains Mono', 'Fira Code', monospace;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: var(--font);
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}

header {
  background: linear-gradient(135deg, #0d1117 0%, #161b22 50%, #1a1e2e 100%);
  border-bottom: 1px solid var(--border);
  padding: 2rem 0;
  position: sticky;
  top: 0;
  z-index: 100;
  backdrop-filter: blur(12px);
}
.header-inner {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 2rem;
}
header h1 {
  font-size: 1.5rem;
  font-weight: 600;
  margin-bottom: 0.75rem;
}
.mono { font-family: var(--mono); color: var(--accent); }

.meta-chips {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.chip {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 20px;
  padding: 0.25rem 0.75rem;
  font-size: 0.8rem;
  color: var(--text-muted);
  font-family: var(--mono);
  font-weight: 500;
}
.chip.model {
  background: var(--accent-dim);
  border-color: var(--accent);
  color: var(--accent);
}
.chip.api-fmt {
  background: #3fb95022;
  border-color: var(--green);
  color: var(--green);
}

main {
  max-width: 1200px;
  margin: 0 auto;
  padding: 2rem;
}

section { margin-bottom: 3rem; }

section h2 {
  font-size: 1.3rem;
  font-weight: 600;
  margin-bottom: 1rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.count {
  background: var(--accent-dim);
  color: var(--accent);
  font-size: 0.8rem;
  padding: 0.15rem 0.5rem;
  border-radius: 10px;
  font-family: var(--mono);
}

/* System Prompt */
.prompt-box {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.5rem 2rem;
  font-size: 0.9rem;
  line-height: 1.7;
  max-height: 70vh;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--border-light) transparent;
}
.prompt-box h3 {
  color: var(--accent);
  font-size: 1.1rem;
  margin: 1.5rem 0 0.5rem 0;
  padding-top: 0.5rem;
  border-top: 1px solid var(--border);
}
.prompt-box h3:first-child { border-top: none; margin-top: 0; }
.prompt-box h4 {
  color: var(--purple);
  font-size: 0.95rem;
  margin: 1rem 0 0.4rem 0;
}
.prompt-box p {
  margin: 0.3rem 0;
  color: var(--text);
}
.prompt-box li {
  margin-left: 1.5rem;
  margin-bottom: 0.2rem;
  color: var(--text-muted);
}
.prompt-box pre {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 1rem;
  margin: 0.75rem 0;
  overflow-x: auto;
}
.prompt-box code {
  font-family: var(--mono);
  font-size: 0.85rem;
  color: var(--green);
}
.prompt-box br { display: block; content: ""; margin: 0.3rem 0; }

/* Tools */
.tools-grid {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.tool-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1.25rem 1.5rem;
  transition: border-color 0.2s;
}
.tool-card:hover { border-color: var(--accent); }

.tool-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
}
.tool-header h3 {
  font-size: 1rem;
  font-weight: 600;
  color: var(--green);
  font-family: var(--mono);
}
.param-count {
  font-size: 0.75rem;
  color: var(--text-muted);
  background: var(--surface-2);
  padding: 0.2rem 0.5rem;
  border-radius: 10px;
  font-family: var(--mono);
}

.tool-desc {
  font-size: 0.85rem;
  color: var(--text-muted);
  margin-bottom: 0.75rem;
  line-height: 1.5;
  white-space: pre-wrap;
}

.full-desc {
  margin-bottom: 0.75rem;
}
.full-desc summary {
  font-size: 0.8rem;
  color: var(--accent);
  cursor: pointer;
  user-select: none;
  margin-bottom: 0.5rem;
}
.full-desc summary:hover { text-decoration: underline; }
.desc-content {
  font-size: 0.82rem;
  color: var(--text-muted);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 1rem;
  max-height: 40vh;
  overflow-y: auto;
  white-space: pre-wrap;
  line-height: 1.5;
}

/* Params table */
.params-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.82rem;
}
.params-table thead th {
  text-align: left;
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  font-weight: 500;
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.params-table td {
  padding: 0.5rem 0.75rem;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
.params-table tr:last-child td { border-bottom: none; }
.param-name code {
  font-family: var(--mono);
  font-size: 0.82rem;
  color: var(--orange);
}
.param-type code {
  font-family: var(--mono);
  font-size: 0.78rem;
  color: var(--purple);
}
.param-desc { color: var(--text-muted); }
.req {
  font-size: 0.65rem;
  background: var(--red);
  color: #fff;
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
  margin-left: 0.3rem;
  font-weight: 600;
  vertical-align: middle;
  text-transform: uppercase;
}
.no-params {
  font-size: 0.82rem;
  color: var(--text-muted);
  font-style: italic;
}
.empty { color: var(--text-muted); font-style: italic; }

footer {
  text-align: center;
  padding: 2rem;
  color: var(--text-muted);
  font-size: 0.8rem;
  border-top: 1px solid var(--border);
}
footer strong { color: var(--accent); }

@media (max-width: 768px) {
  main { padding: 1rem; }
  .prompt-box { padding: 1rem; }
  .params-table { font-size: 0.75rem; }
}
"""


def main():
    parser = argparse.ArgumentParser(description="Render a capture record to static HTML.")
    parser.add_argument("record_id", type=int, help="ID of the capture record")
    parser.add_argument("--db", type=str, default=str(DB_DEFAULT), help="Path to captures.db")
    parser.add_argument("--out", type=str, default=None, help="Output HTML file path")
    args = parser.parse_args()

    record = fetch_record(args.db, args.record_id)
    html_content = render_html(record)

    out_path = args.out or str(
        Path(__file__).resolve().parent.parent / "data" / f"capture_{args.record_id}.html"
    )
    Path(out_path).write_text(html_content, encoding="utf-8")
    print(f"Rendered to {out_path}")


if __name__ == "__main__":
    main()
