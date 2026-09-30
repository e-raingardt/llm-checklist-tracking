"""Datenbankzugriff: Verbindung, Schema, Leseabfragen."""

import sqlite3
from pathlib import Path

DB_PATH = Path("app.db")

# Kern der deterministischen Logik: ein Vorschlag wird nur uebernommen,
# wenn er den Status nach vorne bewegt.
STATUS_RANK = {"open": 0, "requested": 1, "expected": 2, "received": 3}

SCHEMA ="""
CREATE TABLE IF NOT EXISTS items (
    id         TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open'
               CHECK (status IN ('open','requested','expected','received')),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS emails (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   TEXT UNIQUE,
    filename     TEXT,
    sender       TEXT,
    subject      TEXT,
    sent_at      TEXT,
    body         TEXT,
    attachments  TEXT,            -- JSON-Array als Text
    model        TEXT,
    raw_response TEXT,            -- unveraenderte LLM-Antwort
    processed_at TEXT NOT NULL
);

-- item_id bewusst OHNE Fremdschluessel: erfundene Items des Modells
-- sollen aufgezeichnet werden, nicht das Einfuegen blockieren.
CREATE TABLE IF NOT EXISTS proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id        INTEGER NOT NULL REFERENCES emails(id),
    item_id         TEXT,
    proposed_status TEXT,
    evidence        TEXT,
    decision        TEXT NOT NULL CHECK (decision IN ('accepted','rejected')),
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT NOT NULL REFERENCES items(id),
    old_status  TEXT NOT NULL,
    new_status  TEXT NOT NULL,
    source      TEXT NOT NULL,     -- 'email' | 'seed' | 'manual'
    email_id    INTEGER REFERENCES emails(id),
    proposal_id INTEGER REFERENCES proposals(id),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO app_state (key, value)
VALUES ('needs_full_recheck', '1');

"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row            # Zugriff per Spaltenname
    conn.execute("PRAGMA foreign_keys = ON")  # in SQLite standardmaessig AUS
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Legt nur das Schema an. Befuellt nichts - das macht seed.py."""
    conn.executescript(SCHEMA)
    item_schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'items'"
    ).fetchone()["sql"]
    if "'announced'" in item_schema:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(
            """
            BEGIN;
            ALTER TABLE events RENAME TO events_before_expected;
            ALTER TABLE items RENAME TO items_before_expected;

            CREATE TABLE items (
                id         TEXT PRIMARY KEY,
                label      TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open','requested','expected','received')),
                updated_at TEXT
            );
            INSERT INTO items (id, label, status, updated_at)
            SELECT id, label,
                   CASE status WHEN 'announced' THEN 'expected' ELSE status END,
                   updated_at
              FROM items_before_expected;

            CREATE TABLE events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id     TEXT NOT NULL REFERENCES items(id),
                old_status  TEXT NOT NULL,
                new_status  TEXT NOT NULL,
                source      TEXT NOT NULL,
                email_id    INTEGER REFERENCES emails(id),
                proposal_id INTEGER REFERENCES proposals(id),
                created_at  TEXT NOT NULL
            );
            INSERT INTO events
                (id, item_id, old_status, new_status, source, email_id, proposal_id, created_at)
            SELECT id, item_id,
                   CASE old_status WHEN 'announced' THEN 'expected' ELSE old_status END,
                   CASE new_status WHEN 'announced' THEN 'expected' ELSE new_status END,
                   source, email_id, proposal_id, created_at
              FROM events_before_expected;

            UPDATE proposals
               SET proposed_status = 'expected'
             WHERE proposed_status = 'announced';
            DROP TABLE events_before_expected;
            DROP TABLE items_before_expected;
            COMMIT;
            """
        )
        conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def get_item_labels(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT id, label FROM items ORDER BY id").fetchall()
    return {r["id"]: r["label"] for r in rows}


def get_status(conn: sqlite3.Connection, item_id: str) -> str | None:
    row = conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
    return row["status"] if row else None


def email_exists(conn: sqlite3.Connection, message_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM emails WHERE message_id = ?", (message_id,)
    ).fetchone() is not None


if __name__ == "__main__":
    conn = get_conn()
    init_db(conn)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    print("Tabellen:", ", ".join(r["name"] for r in tables))
