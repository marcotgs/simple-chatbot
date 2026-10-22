"""SQLite storage. Schema lives in MIGRATIONS; append new entries, never edit applied ones."""
import sqlite3
import threading

from chatbot import config

# Each entry runs once, tracked by PRAGMA user_version. Later tasks append
# (e.g. ALTER TABLE facts ADD COLUMN ...) instead of editing earlier entries.
MIGRATIONS = [
    """
    CREATE TABLE messages (
        id INTEGER PRIMARY KEY,
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        turn_no INTEGER NOT NULL
    );
    CREATE INDEX messages_thread ON messages (user_id, thread_id, id);

    CREATE VIRTUAL TABLE messages_fts USING fts5 (
        content, content='messages', content_rowid='id'
    );
    CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
    END;
    CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts (messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
    END;
    CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
        INSERT INTO messages_fts (messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
        INSERT INTO messages_fts (rowid, content) VALUES (new.id, new.content);
    END;

    CREATE TABLE facts (
        id INTEGER PRIMARY KEY,
        user_id TEXT NOT NULL,
        thread_id TEXT,                       -- NULL for global scope
        scope TEXT NOT NULL CHECK (scope IN ('global', 'session', 'ephemeral')),
        subject TEXT NOT NULL,
        value TEXT NOT NULL,
        source_message_id INTEGER,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        valid_to TEXT,
        superseded_by INTEGER REFERENCES facts (id)
    );
    CREATE INDEX facts_user ON facts (user_id, scope, thread_id);

    CREATE TABLE summaries (
        id INTEGER PRIMARY KEY,
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        upto_turn INTEGER NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX summaries_thread ON summaries (user_id, thread_id, upto_turn);

    CREATE TABLE doc_notes (
        id INTEGER PRIMARY KEY,
        doc_id TEXT NOT NULL,
        thread_id TEXT,
        page INTEGER,
        kind TEXT NOT NULL DEFAULT 'summary', -- summary | key_fact
        content TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX doc_notes_doc ON doc_notes (doc_id, page);

    CREATE TABLE llm_calls (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        purpose TEXT NOT NULL,
        model TEXT NOT NULL,
        thread_id TEXT,
        estimated_tokens INTEGER NOT NULL,
        prompt_tokens INTEGER NOT NULL,
        completion_tokens INTEGER,
        usage_missing INTEGER NOT NULL DEFAULT 0,
        over_limit INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE send_log (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        user_id TEXT NOT NULL,
        thread_id TEXT,
        recipient TEXT NOT NULL,
        text TEXT NOT NULL
    );
    """,
    # 2: memory subsystem (T3). facts.subject is the normalized fact key.
    """
    ALTER TABLE facts ADD COLUMN source_turn INTEGER;
    CREATE INDEX facts_key ON facts (user_id, subject);

    ALTER TABLE doc_notes ADD COLUMN user_id TEXT;
    CREATE INDEX doc_notes_user ON doc_notes (user_id, doc_id, page);

    CREATE TABLE memory_cursor (
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        extracted_upto INTEGER NOT NULL DEFAULT 0,  -- last turn_no fed to the fact extractor
        PRIMARY KEY (user_id, thread_id)
    );

    CREATE TABLE forgotten (
        id INTEGER PRIMARY KEY,
        user_id TEXT NOT NULL,
        digest TEXT NOT NULL UNIQUE,                -- sha256 hex of a normalized value; never plaintext
        ntokens INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    """,
]

_local = threading.local()


def conn() -> sqlite3.Connection:
    """Per-thread connection with schema applied. Autocommit off; use `with conn():`."""
    c = getattr(_local, "conn", None)
    if c is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=10000")
        migrate(c)
        _local.conn = c
    return c


_migrate_lock = threading.Lock()


def migrate(c: sqlite3.Connection) -> None:
    # ponytail: in-process lock; a second process migrating at the same moment would race.
    with _migrate_lock:
        version = c.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            c.executescript(f"BEGIN IMMEDIATE; {script}; PRAGMA user_version = {i}; COMMIT;")
