#!/usr/bin/env python3
"""Render a captured request into a static HTML file.

Supports both Anthropic and Gemini API formats.

Usage:
    python scripts/render_capture.py <record_id> [--db path/to/captures.db] [--out output.html]
"""

import argparse
import hashlib
import html
import json
import re
import sqlite3
import sys
import webbrowser
from pathlib import Path

from typing import Optional, Any

DB_DEFAULT = Path(__file__).resolve().parent.parent / "data" / "captures.db"


# ---------------------------------------------------------------------------
# Format detection & normalisation
# ---------------------------------------------------------------------------

def _parse_body(raw: Optional[str]) -> Optional[Any]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        lines = []
        for line in raw.split('\n'):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("data: "):
                stripped = stripped[6:].strip()
            try:
                lines.append(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        return lines if lines else None

from typing import Any

def _detect_format(body: Any, record: dict) -> str:
    """Return 'anthropic', 'gemini', or 'openai' based on request body shape and record metadata."""
    api_type = record.get("api_type", "")
    if api_type == "openai" or api_type == "azure":
        return "openai"
    if api_type == "google":
        return "gemini"
    if api_type == "anthropic":
        return "anthropic"

    # Fallback heuristics
    first_frame = body[0] if isinstance(body, list) and body else body
    if not isinstance(first_frame, dict):
        return "openai"

    if "systemInstruction" in first_frame or "contents" in first_frame:
        return "gemini"
    
    url = record.get("url", "") or record.get("path", "")
    if "/chat/completions" in url or "/responses" in url or "/codex" in url:
        return "openai"
    if "/messages" in url:
        return "anthropic"

    if "tools" in first_frame and isinstance(first_frame["tools"], list):
        if any(isinstance(t, dict) and t.get("type") == "function" for t in first_frame["tools"]):
            return "openai"

    return "anthropic"


def _extract_openai(body: Any, record: dict) -> dict:
    """Normalise an OpenAI-format request body."""
    system_text = ""
    messages: list[dict] = []
    tools_flat: list[dict] = []
    model = "unknown"
    max_tokens = "—"
    stream = "—"

    if isinstance(body, list):
        for frame in body:
            if not isinstance(frame, dict):
                continue
            if not system_text:
                instructions = frame.get("instructions")
                if isinstance(instructions, str):
                    system_text = instructions
            if not tools_flat and "tools" in frame:
                for t in frame.get("tools", []):
                    if isinstance(t, dict) and t.get("type") == "function" and "function" in t:
                        f = t["function"]
                        tools_flat.append({
                            "name": f.get("name", "unnamed"),
                            "description": f.get("description", ""),
                            "parameters": f.get("parameters", {})
                        })
                    else:
                        tools_flat.append(t)
            if model == "unknown" and "model" in frame:
                model = frame["model"]
            if max_tokens == "—" and "max_tokens" in frame:
                max_tokens = frame["max_tokens"]
            if stream == "—" and "stream" in frame:
                stream = frame["stream"]
            
            inp = frame.get("input")
            if inp:
                if isinstance(inp, list):
                    for item in inp:
                        if isinstance(item, dict):
                            role = item.get("role", "user")
                            content = item.get("content", "")
                            text_content = ""
                            if isinstance(content, list):
                                text_parts = []
                                for part in content:
                                    if isinstance(part, dict) and part.get("type") in ("text", "input_text"):
                                        text_parts.append(part.get("text", ""))
                                text_content = "\n".join(text_parts)
                            elif isinstance(content, str):
                                text_content = content
                            messages.append({"role": role, "content": text_content})
                        else:
                            messages.append({"role": "user", "content": str(item)})
                elif isinstance(inp, str):
                    messages.append({"role": "user", "content": inp})
    else:
        instructions = body.get("instructions")
        if isinstance(instructions, str):
            system_text = instructions
        else:
            for m in body.get("messages", []):
                if isinstance(m, dict) and m.get("role") == "system":
                    content = m.get("content", "")
                    if isinstance(content, str):
                        system_text = content
                    elif isinstance(content, list):
                        text_parts = [
                            p.get("text", "")
                            for p in content
                            if isinstance(p, dict) and p.get("type") in ("text", "input_text") and p.get("text")
                        ]
                        if text_parts:
                            system_text = "\n".join(text_parts)
                    break

        for t in body.get("tools", []):
            if isinstance(t, dict) and t.get("type") == "function" and "function" in t:
                f = t["function"]
                tools_flat.append({
                    "name": f.get("name", "unnamed"),
                    "description": f.get("description", ""),
                    "parameters": f.get("parameters", {})
                })
            else:
                tools_flat.append(t)

        if not tools_flat and "functions" in body:
            for f in body.get("functions", []):
                if isinstance(f, dict):
                    tools_flat.append({
                        "name": f.get("name", "unnamed"),
                        "description": f.get("description", ""),
                        "parameters": {
                            "type": "object",
                            "properties": f.get("parameters", {}).get("properties", {}),
                            "required": f.get("parameters", {}).get("required", [])
                        } if "parameters" in f else {}
                    })

        msg_list = body.get("messages", [])
        if msg_list:
            messages = msg_list
        elif "input" in body:
            inp = body["input"]
            if isinstance(inp, list):
                messages = [{"role": "user", "content": str(x)} for x in inp]
            else:
                messages = [{"role": "user", "content": str(inp)}]

        model = body.get("model", "unknown")
        max_tokens = body.get("max_tokens", "—")
        stream = body.get("stream", "—")

    return {
        "api_format": "OpenAI",
        "model": model,
        "system_text": system_text,
        "tools": tools_flat,
        "msg_count": len(messages),
        "max_tokens": max_tokens,
        "stream": stream,
    }


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

def fetch_record(db_path: str, record_id=None) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if record_id is None:
        row = conn.execute(
            "SELECT * FROM captures ORDER BY id DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM captures WHERE id = ?", (record_id,)
        ).fetchone()
    conn.close()
    if not row:
        if record_id is None:
            print("No records found in database.", file=sys.stderr)
        else:
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

def render_html_content(info: dict, record_id: Any, host: str) -> str:
    tools_html = _render_tools(info["tools"])
    system_html = _render_system_prompt(info["system_text"])
    tool_count = len(info["tools"])

    # Include personality variables in system prompt rendering if present
    p_vars = info.get("personality_variables", {})
    if p_vars:
        p_html = []
        p_html.append("<div style='margin-top: 2rem; border-top: 2px dashed var(--border); padding-top: 2rem;'>")
        p_html.append("<h2>Personality Variables</h2>")
        for var_name, var_val in p_vars.items():
            if var_val:
                p_html.append(f"<h3 style='color: var(--purple); margin-top: 1.5rem;'>{html.escape(var_name)}</h3>")
                p_html.append(_render_system_prompt(var_val))
        p_html.append("</div>")
        system_html += "\n".join(p_html)

    title = info.get("display_name", f"Capture #{record_id} — {info['model']}")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
{_css()}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <h1>{html.escape(title)}</h1>
    <div class="meta-chips">
      <span class="chip api-fmt">{info['api_format']}</span>
      <span class="chip model">{html.escape(info['model'])}</span>
      <span class="chip">Hash: {info.get('hash', '—')}</span>
      <span class="chip">Source: {html.escape(str(info.get('source', '')))}</span>
      {f'<span class="chip">{info["msg_count"]} messages</span>' if info.get("msg_count") else ''}
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
  <p>Generated by <strong>prompt-peek</strong> &middot; record {record_id} &middot; {host}</p>
</footer>
</body>
</html>"""


def render_html(record: dict) -> str:
    body = _parse_body(record["request_body"])
    fmt = _detect_format(body, record)

    if fmt == "gemini":
        info = _extract_gemini(body, record)
    elif fmt == "openai":
        info = _extract_openai(body, record)
    else:
        info = _extract_anthropic(body, record)

    info["hash"] = hashlib.sha256(info["system_text"].encode("utf-8")).hexdigest()[:16] if info.get("system_text") else ""
    info["source"] = f"Record {record['id']} (URL: {record['url']})"
    info["display_name"] = f"Capture #{record['id']} — {info['model']}"

    return render_html_content(info, record["id"], record.get("host", ""))


def render_markdown_content(info: dict) -> str:
    parts = []
    title = info.get("display_name", info.get("model", "System Prompt"))
    parts.append(f"# System Prompt: {title}")
    parts.append("")
    parts.append(f"- **Source**: {info.get('source', '—')}")
    parts.append(f"- **Hash**: `{info.get('hash', '—')}`")
    parts.append(f"- **Length**: {len(info.get('system_text', ''))} characters")
    parts.append(f"- **API Format**: {info.get('api_format', '—')}")
    parts.append(f"- **Model**: {info.get('model', '—')}")
    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append(info.get("system_text", ""))
    parts.append("")

    # Render personality variables if any
    p_vars = info.get("personality_variables", {})
    if p_vars:
        parts.append("---")
        parts.append("")
        parts.append("## Personality Variables")
        parts.append("")
        for var_name, var_val in p_vars.items():
            if var_val:
                parts.append(f"### {var_name}")
                parts.append("")
                parts.append(var_val.strip())
                parts.append("")

    if info.get("tools"):
        parts.append("---")
        parts.append("")
        parts.append(f"## Available Tools ({len(info['tools'])})")
        parts.append("")
        for tool in info["tools"]:
            name = tool.get("name", "unnamed")
            desc = tool.get("description", "")
            props, required = _tool_props(tool)
            parts.append(f"### `{name}`")
            if desc:
                parts.append(desc.strip())
                parts.append("")
            if props:
                parts.append("| Parameter | Type | Description | Required |")
                parts.append("| :--- | :--- | :--- | :--- |")
                for pname, pdef in props.items():
                    ptype = pdef.get("type", "any")
                    pdesc = pdef.get("description", "").replace("\n", " ").strip()
                    req = "Yes" if pname in required else "No"
                    parts.append(f"| `{pname}` | `{ptype}` | {pdesc} | {req} |")
                parts.append("")

    return "\n".join(parts)


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


def scan_and_extract_prompts(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Check database tables
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='captures'")
    if not cursor.fetchone():
        print("No captures table found in the database.", file=sys.stderr)
        conn.close()
        return {}

    cursor.execute("SELECT id, url, api_type, request_body, host FROM captures")

    extracted = {}

    for row in cursor.fetchall():
        cid = row["id"]
        url = row["url"]
        api_type = row["api_type"]
        req_body = row["request_body"]
        host = row["host"]

        if not req_body:
            continue

        body = None
        try:
            body = _parse_body(req_body)
        except Exception:
            pass

        if not body:
            continue

        # We need to detect format and extract prompt and tools
        fmt = _detect_format(body, dict(row))

        if isinstance(body, list):
            # WebSocket messages are frames
            info = _extract_openai(body, dict(row))
        else:
            if fmt == "gemini":
                info = _extract_gemini(body, dict(row))
            elif fmt == "openai":
                info = _extract_openai(body, dict(row))
            else:
                info = _extract_anthropic(body, dict(row))

        sys_text = info.get("system_text")
        if sys_text and sys_text.strip():
            sys_text = sys_text.strip()
            h = hashlib.sha256(sys_text.encode("utf-8")).hexdigest()[:16]

            # Categorize agent
            agent_name = None
            display_name = None
            if "anthropic/v1/messages" in url and "Claude Code" in sys_text:
                agent_name = "claude_code"
                display_name = "Claude Code"
            elif "api.deepseek.com" in url and "DeepSeek TUI" in sys_text:
                agent_name = "deepseek_tui"
                display_name = "DeepSeek TUI"
            elif "backend-api/codex/responses" in url and "Codex" in sys_text:
                agent_name = "codex_gpt5.5_active"
                display_name = "Codex GPT-5.5 Active"
            elif "Hermes" in sys_text or "Nous Research" in sys_text:
                agent_name = "nous_hermes_agent"
                display_name = "Nous Hermes Agent"
            elif "inside pi" in sys_text or "pi, a coding agent" in sys_text or "operating inside pi" in sys_text:
                agent_name = "gemini_pi_harness"
                display_name = "Gemini Pi Harness"
            elif len(sys_text) < 100:
                # Skip small test/developer prompts
                continue
            else:
                agent_name = f"misc_{h}"
                display_name = f"Misc ({h})"

            if agent_name and (agent_name not in extracted or len(sys_text) > len(extracted[agent_name]["system_text"])):
                info["hash"] = h
                info["source"] = f"{Path(db_path).name} (capture ID {cid})"
                info["agent_name"] = agent_name
                info["display_name"] = display_name
                info["record_id"] = cid
                info["host"] = host
                extracted[agent_name] = info

    # Check for Codex models response in ID 5
    cursor.execute("SELECT response_body, host, url FROM captures WHERE id = 5")
    row = cursor.fetchone()
    if row and row[0]:
        try:
            data = json.loads(row[0])
            for m in data.get("models", []):
                slug = m.get("slug")
                display_name = m.get("display_name")
                base_inst = m.get("base_instructions")
                inst_vars = m.get("instructions_variables", {})

                if slug and base_inst:
                    base_inst = base_inst.strip()
                    h = hashlib.sha256(base_inst.encode("utf-8")).hexdigest()[:16]

                    extracted[f"codex_model_{slug}"] = {
                        "api_format": "OpenAI",
                        "model": slug,
                        "system_text": base_inst,
                        "tools": [],
                        "msg_count": 0,
                        "max_tokens": "—",
                        "stream": "—",
                        "hash": h,
                        "source": f"{Path(db_path).name} (capture ID 5)",
                        "agent_name": f"codex_model_{slug}",
                        "display_name": f"Codex Model: {display_name or slug}",
                        "personality_variables": inst_vars,
                        "record_id": 5,
                        "host": row[1]
                    }
        except Exception as e:
            print(f"Error parsing models response in db scan: {e}", file=sys.stderr)

    conn.close()
    return extracted


def parse_markdown_metadata(file_path: Path) -> Optional[dict]:
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return None

    lines = content.split("\n")
    if not lines or not lines[0].startswith("# System Prompt: "):
        return None

    title = lines[0].replace("# System Prompt: ", "").strip()
    source = "—"
    h = "—"
    api_format = "—"
    model = "—"
    tools_cnt = 0

    for line in lines[1:20]:
        line_s = line.strip()
        if line_s.startswith("- **Source**:"):
            source = line_s.split(":", 1)[1].strip()
        elif line_s.startswith("- **Hash**:"):
            h = line_s.split(":", 1)[1].replace("`", "").strip()
        elif line_s.startswith("- **API Format**:"):
            api_format = line_s.split(":", 1)[1].strip()
        elif line_s.startswith("- **Model**:"):
            model = line_s.split(":", 1)[1].strip()

    content_parts = content.split("\n---\n")
    if len(content_parts) >= 2:
        prompt_len = len(content_parts[1].strip())
    else:
        prompt_len = len(content)

    in_tools = False
    for line in lines:
        if line.strip().startswith("## Available Tools"):
            in_tools = True
            continue
        if in_tools:
            if line.strip().startswith("## "):
                in_tools = False
            elif line.strip().startswith("### `"):
                tools_cnt += 1

    return {
        "display_name": title,
        "source": source,
        "hash": h,
        "system_text": " " * prompt_len,
        "api_format": api_format,
        "model": model,
        "tools": [{}] * tools_cnt,
        "agent_name": file_path.stem
    }


def parse_html_metadata(file_path: Path) -> Optional[dict]:
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception:
        return None

    if file_path.name == "index.html":
        return None

    title_match = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE)
    title = title_match.group(1).strip() if title_match else file_path.stem
    if title.startswith("System Prompt — "):
        title = title.replace("System Prompt — ", "")

    source = "—"
    h = "—"
    api_format = "—"
    model = "—"
    tools_cnt = 0

    chips = re.findall(r'<span class="chip.*?">(.*?)</span>', content)
    for chip in chips:
        chip_s = chip.strip()
        if chip_s.startswith("Hash:"):
            h = chip_s.replace("Hash:", "").strip()
        elif chip_s.startswith("Source:"):
            source = chip_s.replace("Source:", "").strip()
        elif "messages" in chip_s:
            pass
        elif chip_s in ("OpenAI", "Anthropic", "Gemini"):
            api_format = chip_s
        else:
            model = chip_s

    tools_cnt = len(re.findall(r'<div class="tool-card">', content))

    prompt_match = re.search(r'<div class="prompt-box">(.*?)</div>', content, re.DOTALL)
    if prompt_match:
        raw_text = re.sub(r'<.*?>', '', prompt_match.group(1))
        prompt_len = len(raw_text.strip())
    else:
        prompt_len = 0

    return {
        "display_name": title,
        "source": source,
        "hash": h,
        "system_text": " " * prompt_len,
        "api_format": api_format,
        "model": model,
        "tools": [{}] * tools_cnt,
        "agent_name": file_path.stem
    }


def render_index_html(extracted: dict) -> str:
    rows = []
    for agent_name in sorted(extracted.keys()):
        info = extracted[agent_name]
        filename = f"{agent_name}.html"
        rows.append(f"""
        <tr>
          <td><a href="{filename}"><strong>{html.escape(info.get('display_name', agent_name))}</strong></a></td>
          <td>{html.escape(info.get('source', '—'))}</td>
          <td><code>{html.escape(info.get('hash', '—'))}</code></td>
          <td>{len(info.get('system_text', ''))}</td>
          <td>{len(info.get('tools', []))}</td>
          <td><a href="{filename}">View HTML</a></td>
        </tr>""")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>System Prompts Index</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
{_css()}
table.index-table {{
  width: 100%;
  border-collapse: collapse;
  margin-top: 1.5rem;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}}
table.index-table th {{
  background: var(--surface-2);
  text-align: left;
  padding: 1rem;
  font-weight: 600;
  border-bottom: 1px solid var(--border);
  font-size: 0.85rem;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
table.index-table td {{
  padding: 1rem;
  border-bottom: 1px solid var(--border);
  font-size: 0.9rem;
}}
table.index-table tr:last-child td {{
  border-bottom: none;
}}
table.index-table a {{
  color: var(--accent);
  text-decoration: none;
}}
table.index-table a:hover {{
  text-decoration: underline;
}}
</style>
</head>
<body>
<header>
  <div class="header-inner">
    <h1>System Prompts Index</h1>
    <p style="color: var(--text-muted); margin-top: 0.5rem;">Extracted unique system prompts and tools from agent capture databases.</p>
  </div>
</header>
<main>
  <table class="index-table">
    <thead>
      <tr>
        <th>Agent / Model</th>
        <th>Source</th>
        <th>Hash</th>
        <th>Prompt Chars</th>
        <th>Tools Count</th>
        <th>Link</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
</main>
<footer>
  <p>Generated by <strong>prompt-peek</strong> &middot; {len(extracted)} prompts found</p>
</footer>
</body>
</html>"""


def render_index_markdown(extracted: dict) -> str:
    lines = [
        "# System Prompts Index",
        "",
        "This directory contains unique system prompts and tools extracted from agent capture databases.",
        "",
        "## List of Extracted Agents & Models",
        "",
        "| Agent/Model | Source | Hash | Chars | Tools | Filename |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |"
    ]
    for agent_name in sorted(extracted.keys()):
        info = extracted[agent_name]
        filename = f"{agent_name}.md"
        title = info.get("display_name", agent_name.replace("_", " ").title())
        source = info.get("source", "—").split(" (")[0]
        h = info.get("hash", "—")
        chars = len(info.get("system_text", ""))
        tools_cnt = len(info.get("tools", []))
        lines.append(f"| {title} | {source} | `{h}` | {chars} | {tools_cnt} | [{filename}]({filename}) |")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Render capture records or extract system prompts.")
    parser.add_argument(
        "record_id",
        type=int,
        nargs="?",
        default=None,
        help="ID of the capture record. If omitted, scans the database to extract all unique system prompts."
    )
    parser.add_argument(
        "--db",
        type=str,
        default=str(DB_DEFAULT),
        help="Path to captures.db"
    )
    parser.add_argument(
        "--format", "-f",
        type=str,
        choices=["html", "md", "both"],
        default="html",
        help="Output format: html, md, or both (default is html)"
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Output directory or file path. Defaults to captured/html and captured/md folders."
    )
    parser.add_argument(
        "--browser", "-b",
        action="store_true",
        help="Open the generated HTML (or the index HTML in scanning mode) in the default web browser"
    )
    args = parser.parse_args()

    workspace = Path(__file__).resolve().parent.parent
    default_html_dir = workspace / "captured" / "html"
    default_md_dir = workspace / "captured" / "md"

    if args.record_id is not None:
        # Render a single record
        record = fetch_record(args.db, args.record_id)
        body = _parse_body(record["request_body"])
        fmt = _detect_format(body, record)

        if fmt == "gemini":
            info = _extract_gemini(body, record)
        elif fmt == "openai":
            info = _extract_openai(body, record)
        else:
            info = _extract_anthropic(body, record)

        sys_text = info.get("system_text", "")
        info["hash"] = hashlib.sha256(sys_text.encode("utf-8")).hexdigest()[:16] if sys_text else ""
        info["source"] = f"Record {record['id']} (URL: {record['url']})"
        info["display_name"] = f"Capture #{record['id']} — {info['model']}"

        html_path = None
        md_path = None

        if args.out:
            out_p = Path(args.out)
            if out_p.suffix in (".html", ".htm"):
                html_path = out_p
            elif out_p.suffix == ".md":
                md_path = out_p
            else:
                out_p.mkdir(parents=True, exist_ok=True)
                html_path = out_p / f"capture_{record['id']}.html"
                md_path = out_p / f"capture_{record['id']}.md"
        else:
            default_html_dir.mkdir(parents=True, exist_ok=True)
            default_md_dir.mkdir(parents=True, exist_ok=True)
            html_path = default_html_dir / f"capture_{record['id']}.html"
            md_path = default_md_dir / f"capture_{record['id']}.md"

        last_html_generated = None
        if args.format in ("html", "both") and html_path:
            html_content = render_html_content(info, record["id"], record.get("host", ""))
            html_path.parent.mkdir(parents=True, exist_ok=True)
            html_path.write_text(html_content, encoding="utf-8")
            print(f"Rendered HTML to {html_path.resolve()}")
            last_html_generated = html_path.resolve()

        if args.format in ("md", "both") and md_path:
            md_content = render_markdown_content(info)
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(md_content, encoding="utf-8")
            print(f"Rendered Markdown to {md_path.resolve()}")

        if args.browser and last_html_generated:
            webbrowser.open(last_html_generated.as_uri())

    else:
        # Scan and extract all unique prompts from database
        print(f"Scanning database {args.db} for unique system prompts...")
        extracted = scan_and_extract_prompts(args.db)
        if not extracted:
            print("No system prompts extracted.")
            sys.exit(0)

        if args.out:
            out_base = Path(args.out)
            html_dir = out_base / "html" if out_base.name != "html" else out_base
            md_dir = out_base / "md" if out_base.name != "md" else out_base
        else:
            html_dir = default_html_dir
            md_dir = default_md_dir

        last_html_generated = None

        # Write out files for each prompt
        for agent_name, info in extracted.items():
            if args.format in ("html", "both"):
                html_dir.mkdir(parents=True, exist_ok=True)
                html_path = html_dir / f"{agent_name}.html"
                html_content = render_html_content(info, info["record_id"], info["host"])
                html_path.write_text(html_content, encoding="utf-8")
                last_html_generated = html_path.resolve()

            if args.format in ("md", "both"):
                md_dir.mkdir(parents=True, exist_ok=True)
                md_path = md_dir / f"{agent_name}.md"
                md_content = render_markdown_content(info)
                md_path.write_text(md_content, encoding="utf-8")

        # Write out indexes (combining newly generated and pre-existing files in the output directory)
        combined_extracted = {}
        for agent_name, info in extracted.items():
            combined_extracted[agent_name] = info

        if args.format in ("md", "both") and md_dir.exists():
            for p in md_dir.glob("*.md"):
                if p.name == "README.md" or p.name.startswith("capture_"):
                    continue
                if p.stem not in combined_extracted:
                    meta = parse_markdown_metadata(p)
                    if meta:
                        combined_extracted[p.stem] = meta

        if args.format in ("html", "both") and html_dir.exists():
            for p in html_dir.glob("*.html"):
                if p.name == "index.html" or p.name.startswith("capture_"):
                    continue
                if p.stem not in combined_extracted:
                    meta = parse_html_metadata(p)
                    if meta:
                        combined_extracted[p.stem] = meta

        if args.format in ("html", "both"):
            index_path = html_dir / "index.html"
            index_content = render_index_html(combined_extracted)
            index_path.write_text(index_content, encoding="utf-8")
            print(f"Generated HTML index and prompts under {html_dir.resolve()}")
            last_html_generated = index_path.resolve()

        if args.format in ("md", "both"):
            readme_path = md_dir / "README.md"
            readme_content = render_index_markdown(combined_extracted)
            readme_path.write_text(readme_content, encoding="utf-8")
            print(f"Generated Markdown index and prompts under {md_dir.resolve()}")

        if args.browser and last_html_generated:
            webbrowser.open(last_html_generated.as_uri())


if __name__ == "__main__":
    main()
