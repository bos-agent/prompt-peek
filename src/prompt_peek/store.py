"""SQLite store for captured LLM API requests and responses."""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, Any

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   REAL NOT NULL,
    method      TEXT NOT NULL,
    url         TEXT NOT NULL,
    host        TEXT NOT NULL,
    path        TEXT NOT NULL,
    request_headers  TEXT,   -- JSON
    request_body     TEXT,   -- JSON string as-is (may be large)
    response_status  INTEGER,
    response_headers TEXT,   -- JSON
    response_body    TEXT,   -- JSON string as-is (may be large)
    api_type    TEXT DEFAULT 'unknown',
    duration_ms REAL,
    request_size    INTEGER DEFAULT 0,
    response_size   INTEGER DEFAULT 0,
    system_prompt_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_captures_timestamp ON captures (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_captures_host      ON captures (host);
CREATE INDEX IF NOT EXISTS idx_captures_api_type  ON captures (api_type);
"""

MIGRATIONS = [
    """ALTER TABLE captures ADD COLUMN system_prompt_hash TEXT""",
    """CREATE INDEX IF NOT EXISTS idx_captures_sys_hash ON captures (system_prompt_hash)""",
]


class Store:
    """Thread-safe SQLite store for prompt captures.

    Each calling thread gets its own SQLite connection (thread-local).
    All connections use WAL mode so concurrent reads/writes across
    threads are safe.  ``close()`` closes every connection.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all_connections: set[sqlite3.Connection] = set()
        self._conn_lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        """Return this thread's dedicated connection, creating one if needed."""
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
                    pass
            conn.commit()
            self._local.conn = conn
            with self._conn_lock:
                self._all_connections.add(conn)
        return conn

    def close(self):
        """Close every connection opened by any thread."""
        with self._conn_lock:
            for conn in self._all_connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_connections.clear()

    # ── write ──────────────────────────────────────────────────────

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

    def update_response(self, capture_id: int, *,
                        response_status: int,
                        response_headers: dict,
                        response_body: Optional[str],
                        duration_ms: float = 0.0,
                        response_size: int = 0):
        conn = self._get_conn()
        conn.execute(
            """UPDATE captures
               SET response_status = ?, response_headers = ?, response_body = ?,
                   duration_ms = ?, response_size = ?
               WHERE id = ?""",
            (response_status,
             json.dumps(response_headers or {}, ensure_ascii=False),
             response_body, duration_ms, response_size,
             capture_id),
        )
        conn.commit()

    def append_websocket_message(self, capture_id: int, text: str, from_client: bool):
        conn = self._get_conn()
        row = conn.execute(
            "SELECT request_body, response_body, system_prompt_hash FROM captures WHERE id = ?",
            (capture_id,)
        ).fetchone()
        if not row:
            return

        if from_client:
            req = row["request_body"] or ""
            if req:
                req += "\n" + text
            else:
                req = text

            system_prompt_hash = row["system_prompt_hash"]
            try:
                body_json = json.loads(text)
                from prompt_peek.proxy_addon import _extract_system_prompt_text
                sys_text = _extract_system_prompt_text(body_json)
                if sys_text:
                    import hashlib
                    system_prompt_hash = hashlib.sha256(
                        sys_text.encode("utf-8")
                    ).hexdigest()[:16]
            except Exception:
                pass

            conn.execute(
                "UPDATE captures SET request_body = ?, system_prompt_hash = ?, request_size = ? WHERE id = ?",
                (req, system_prompt_hash, len(req), capture_id)
            )
        else:
            resp = row["response_body"] or ""
            resp += f"data: {text}\n"

            conn.execute(
                "UPDATE captures SET response_body = ?, response_size = ? WHERE id = ?",
                (resp, len(resp), capture_id)
            )
        conn.commit()

    # ── read ───────────────────────────────────────────────────────

    def list_captures(self, *, limit: int = 50, offset: int = 0,
                      host: Optional[str] = None,
                      api_type: Optional[str] = None,
                      search: Optional[str] = None) -> list[dict]:
        conn = self._get_conn()

        # Build the base WHERE clause
        where = "1=1"
        params: list[Any] = []

        if host:
            where += " AND host = ?"
            params.append(host)
        if api_type:
            where += " AND api_type = ?"
            params.append(api_type)
        if search:
            where += " AND (url LIKE ? OR request_body LIKE ? OR response_body LIKE ?)"
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern])

        # Use a CTE to annotate each row with system-prompt change info.
        # Window functions operate over the filtered set (before LIMIT/OFFSET)
        # so prev/next comparisons are correct regardless of pagination.
        query = f"""
            WITH base AS (
                SELECT * FROM captures WHERE {where}
            ),
            annotated AS (
                SELECT
                    base.*,
                    LAG(base.system_prompt_hash) OVER w AS prev_sys_hash,
                    ROW_NUMBER() OVER (PARTITION BY base.system_prompt_hash ORDER BY base.timestamp ASC) AS sys_hash_occurrence
                FROM base
                WINDOW w AS (ORDER BY base.timestamp ASC)
            )
            SELECT * FROM annotated
            ORDER BY timestamp DESC
            LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_capture(self, capture_id: int) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM captures WHERE id = ?", (capture_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_previous_system_prompt(self, capture_id: int) -> Optional[dict]:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT id, system_prompt_hash, request_body
               FROM captures
               WHERE id < ? AND system_prompt_hash IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (capture_id,),
        ).fetchone()
        if not row:
            return None
        content = None
        try:
            from prompt_peek.proxy_addon import _extract_system_prompt_text
            body = json.loads(row["request_body"])
            content = _extract_system_prompt_text(body)
        except (json.JSONDecodeError, TypeError):
            pass
        return {
            "id": row["id"],
            "hash": row["system_prompt_hash"],
            "content": content,
        }

    def count_system_prompt_versions(self) -> int:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COUNT(DISTINCT system_prompt_hash)
               FROM captures
               WHERE system_prompt_hash IS NOT NULL""",
        ).fetchone()
        return row[0] if row else 0

    def count(self, *, host: Optional[str] = None,
              api_type: Optional[str] = None,
              search: Optional[str] = None) -> int:
        conn = self._get_conn()
        query = "SELECT COUNT(*) FROM captures WHERE 1=1"
        params: list[Any] = []
        if host:
            query += " AND host = ?"
            params.append(host)
        if api_type:
            query += " AND api_type = ?"
            params.append(api_type)
        if search:
            query += " AND (url LIKE ? OR request_body LIKE ? OR response_body LIKE ?)"
            pattern = f"%{search}%"
            params.extend([pattern, pattern, pattern])
        return conn.execute(query, params).fetchone()[0]

    def delete(self, capture_id: int):
        conn = self._get_conn()
        conn.execute("DELETE FROM captures WHERE id = ?", (capture_id,))
        conn.commit()

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        row_keys = row.keys()
        has_window = "sys_hash_occurrence" in row_keys
        return {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "method": row["method"],
            "url": row["url"],
            "host": row["host"],
            "path": row["path"],
            "request_headers": row["request_headers"],
            "request_body": row["request_body"],
            "response_status": row["response_status"],
            "response_headers": row["response_headers"],
            "response_body": row["response_body"],
            "api_type": row["api_type"],
            "duration_ms": row["duration_ms"],
            "request_size": row["request_size"],
            "response_size": row["response_size"],
            "system_prompt_hash": row["system_prompt_hash"],
            "sys_prompt_first": bool(row["sys_hash_occurrence"] == 1 and row["system_prompt_hash"]) if has_window else None,
            "sys_prompt_changed": bool(
                row["system_prompt_hash"] is not None
                and row["prev_sys_hash"] is not None
                and row["system_prompt_hash"] != row["prev_sys_hash"]
            ) if has_window else None,
        }
