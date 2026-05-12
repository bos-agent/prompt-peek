# prompt-peek UI Redesign

**Date:** 2026-05-12
**Status:** approved

## Summary

Replace the multi-page navigation UI (index → capture → raw) with a single-page split-pane layout. Improve content rendering: markdown rendering for responses, structured tool cards, inline system prompt diffs. Add `system_prompt_hash` column for change detection.

## Architecture

### Layout

Single page (`index.html`). `capture.html` and `raw.html` are removed.

```
┌──────────────────────────────────────────────────────┐
│  Header (◉ prompt-peek)                              │
├────────────────────┬─────────────────────────────────┤
│  Toolbar (filters) │                                 │
│  Capture list      │  Detail pane (flex: ~45%)       │
│  (scrolls)         │  ├─ Metadata bar                │
│                    │  ├─ System Prompt (diff badge)  │
│                    │  ├─ Tools (tag cloud + cards)   │
│                    │  ├─ Messages (request)          │
│                    │  ├─ Response (markdown rendered)│
│                    │  └─ Headers (collapsed default) │
├────────────────────┴─────────────────────────────────┤
│  Status bar: N captures · WS connected               │
└──────────────────────────────────────────────────────┘
```

- Resizable divider between list and detail pane
- Narrow screens (<768px): detail pane goes full-width (slide-over)
- Raw JSON always available via toggle in each section

### Navigation

- Click capture → detail pane loads via `GET /api/captures/:id`
- `↑` `↓` navigate list, `Esc` closes detail pane, `r` toggles raw JSON
- Filters/search re-fetch list; selected capture highlight persists if still in results

## Frontend

### JS structure (vanilla, no framework)

Single `app.js` with clear objects:
- `state` — captures, selectedId, totalCount, ws status
- `api` — fetchCaptures, fetchCapture, deleteCapture
- `render` — renderList, renderDetail, renderDiff
- `ws` — connectWS, handleEvent
- `keyboard` — handleKeydown
- `markdown` — minimal sanitized renderer via marked.js (~20KB)
- `diff` — simple line-diff for system prompt comparison

### Data flow (unchanged backbone)

1. Page load → `GET /api/captures` → render list
2. WebSocket → live inserts/updates in list
3. Click capture → `GET /api/captures/:id` → render detail pane
4. WS response event for open capture → re-fetch detail to get full body

## Backend change

### New column: `system_prompt_hash`

```sql
ALTER TABLE captures ADD COLUMN system_prompt_hash TEXT;
```

Computed on insert from `request_body_parsed.messages` (first `role === "system"` content, SHA-256 truncated). Used for inline diff lookup: query most recent capture with same `host` + `api_type` + different hash. Store updated in `proxy_addon.py` and `store.py`.

### Markdown library

Serve `marked.js` as a static file. Used only in the response section. HTML output sanitized: strip `<script>`, `onerror`, `javascript:` URLs.

## Detail pane sections

### 1. Metadata bar (always visible)
- URL, timestamp, duration, sizes, HTTP status, API type badge
- "Raw JSON" toggle link

### 2. System Prompt
- Full text in code block
- Badge: green "unchanged vs #N" or yellow "changed vs #N"
- "Show diff" toggle: highlights additions/deletions vs previous
- Link: "view all versions (N)" — filters list to same host+api_type

### 3. Tools
- Tag cloud: pills color-coded by category (read→blue, write→green, shell→orange, search→purple, other→gray)
- Collapsible cards: one `<details>` per tool with description + key params + required params
- Count badge

### 4. Messages
- Role-colored left border (existing pattern)
- Content collapsed to ~200px with "show more"
- Excludes system messages (shown in section 2)
- Tool call/result messages rendered distinctively

### 5. Response
- Primary view: markdown-rendered content
- Content extraction by API type:
  - OpenAI: `choices[0].message.content`
  - Anthropic: join `content[*].text` blocks
  - Google: `candidates[0].content.parts[*].text`
  - Unknown: pretty-printed JSON fallback
- Metadata: finish_reason, model, token usage
- Raw toggle: switches to pretty-printed JSON with basic syntax coloring
- Truncate markdown rendering at 500KB; show "view raw" button for full body

### 6. Headers (collapsed by default)
- Request headers and response headers, pretty-printed JSON

## Edge cases

| Scenario | Behavior |
|---|---|
| No captures | Existing empty state ("Waiting for traffic") |
| Capture deleted while viewing | "This capture was deleted" message with dismiss |
| Body not JSON | Show raw text in code block, label "unparseable" |
| Response >1MB | Truncate markdown at 500KB, "view raw" button |
| WS disconnect | Amber indicator in status bar, reconnect 2s |
| No system prompt | Section hidden |
| No tools | Section hidden |
| Only system message | Messages section shows "No conversation messages" |
| Consecutive same-role messages | Each as own block (don't merge) |

## Out of scope

- Persisted read/unread state
- Export/save functionality
- Dark/light theme toggle
- Arbitrary pair-wise diffing (only "vs previous")
- Backend changes beyond `system_prompt_hash`
