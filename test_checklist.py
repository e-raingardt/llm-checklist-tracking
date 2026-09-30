import json
import sqlite3

from db import init_db
from pipeline import apply_updates, now


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    with conn:
        conn.execute(
            "INSERT INTO items (id, label, status) VALUES (?, ?, ?)",
            ("id_card", "Identification document", "open"),
        )
        conn.execute(
            "INSERT INTO items (id, label, status) VALUES (?, ?, ?)",
            ("rental_lease", "Current rental agreement", "received"),
        )
        cur = conn.execute(
            """INSERT INTO emails
               (message_id, filename, sender, subject, sent_at, body,
                attachments, model, raw_response, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "<test@demo.local>",
                "test.eml",
                "sender@example.local",
                "Subject",
                "",
                "body",
                json.dumps([]),
                "test",
                json.dumps({"updates": []}),
                now(),
            ),
        )
    return conn, cur.lastrowid


def get_status(conn, item_id):
    row = conn.execute("SELECT status FROM items WHERE id = ?", (item_id,)).fetchone()
    return row["status"] if row else None


def test_valid_transition():
    conn, email_id = make_conn()
    result = {"updates": [{"item_id": "id_card", "status": "received", "evidence": "Attachment"}]}
    apply_updates(conn, email_id, result)
    assert get_status(conn, "id_card") == "received"


def test_no_downgrade():
    conn, email_id = make_conn()
    result = {"updates": [{"item_id": "rental_lease", "status": "requested", "evidence": "requested again"}]}
    apply_updates(conn, email_id, result)
    assert get_status(conn, "rental_lease") == "received"


def test_unknown_item_is_ignored():
    conn, email_id = make_conn()
    result = {"updates": [{"item_id": "tax_assessment", "status": "received", "evidence": "?"}]}
    log = apply_updates(conn, email_id, result)
    assert get_status(conn, "tax_assessment") is None
    assert log[0].startswith("REJECTED")


def test_invalid_status_is_ignored():
    conn, email_id = make_conn()
    result = {"updates": [{"item_id": "id_card", "status": "lost", "evidence": "?"}]}
    apply_updates(conn, email_id, result)
    assert get_status(conn, "id_card") == "open"


def test_empty_updates_change_nothing():
    conn, email_id = make_conn()
    apply_updates(conn, email_id, {"updates": []})
    assert get_status(conn, "id_card") == "open"
    assert get_status(conn, "rental_lease") == "received"
