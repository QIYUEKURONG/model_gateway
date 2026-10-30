"""SQLite 连接与表结构。提示词版本和调用记录共用一个库文件。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_versions (
  prompt_key TEXT NOT NULL,
  version INTEGER NOT NULL,
  role TEXT NOT NULL,
  body TEXT NOT NULL,
  slots_json TEXT NOT NULL,
  status TEXT NOT NULL,
  changelog TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (prompt_key, version)
);

CREATE TABLE IF NOT EXISTS external_materials (
  material_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  content_type TEXT NOT NULL DEFAULT 'text/plain',
  source TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (material_id, version)
);

CREATE TABLE IF NOT EXISTS call_records (
  id TEXT PRIMARY KEY,
  logical_request_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_key TEXT,
  prompt_version INTEGER,
  materials_json TEXT,
  started_at TEXT NOT NULL,
  latency_ms REAL NOT NULL,
  status TEXT NOT NULL,
  error_code TEXT,
  error_message TEXT,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cache_read_tokens INTEGER NOT NULL,
  cache_write_tokens INTEGER NOT NULL,
  uncached_input_tokens INTEGER NOT NULL,
  total_tokens INTEGER NOT NULL,
  cost_usd TEXT NOT NULL,
  price_tier TEXT NOT NULL,
  provider_request_id TEXT,
  stream INTEGER NOT NULL,
  finish_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_lookup
  ON call_records(provider, model, started_at);
CREATE INDEX IF NOT EXISTS idx_calls_logical
  ON call_records(logical_request_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """打开账本和提示词共用的库。WAL，允许别的线程读；写操作由各 Store 自己加锁。"""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', '1')"
    )
    conn.commit()
    return conn
