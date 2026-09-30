"""Builds the demo state.

reset()  - wipe everything, insert the items
replay() - run the fixture emails through the pipeline

The UI reset button will call the same two functions later.

Destructive - acceptable only because all data comes from fixtures and
is reproducible at any time.
"""

import sqlite3
from pathlib import Path

from db import get_conn, init_db
from pipeline import CACHE_DIR, process_email, read_eml

FIXTURE_DIR = Path("fixtures/emails")

DEFAULT_ITEMS = {
    "id_card":      "Identification document",
    "payslips":     "Payslips for the last 3 months",
    "rental_lease": "Current rental agreement",
    "bank_statements": "Bank statements for the last 3 months",
}


def reset(conn: sqlite3.Connection) -> None:
    init_db(conn)
    with conn:
        # Order matters because of the foreign keys: children first.
        for table in ("events", "proposals", "emails", "items"):
            conn.execute(f"DELETE FROM {table}")
        for item_id, label in DEFAULT_ITEMS.items():
            conn.execute(
                "INSERT INTO items (id, label, status) VALUES (?, ?, 'open')",
                (item_id, label),
            )
        conn.execute(
            """INSERT INTO app_state (key, value)
               VALUES ('needs_full_recheck', '1')
               ON CONFLICT(key) DO UPDATE SET value = '1'"""
        )


def replay(conn: sqlite3.Connection) -> None:
    paths = sorted(FIXTURE_DIR.glob("*.eml"))
    if not paths:
        print(f"No .eml files found in {FIXTURE_DIR.resolve()}")
        return
    for path in paths:
        print(f"\n=== {path.name} ===")
        text, attachments, meta = read_eml(path)
        process_email(conn, text, attachments, meta)


if __name__ == "__main__":
    conn = get_conn()
    reset(conn)
    print(f"{len(DEFAULT_ITEMS)} items inserted.")

    replay(conn)

    print("\nDemo state ready.")
    print(f"Recorded LLM responses: {CACHE_DIR.resolve()}")
    print("To re-record: delete that directory and run again.")
