"""Minimal FastAPI wrapper for the checklist demo."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from anthropic import Anthropic

from db import STATUS_RANK, get_conn, get_item_labels, init_db
from pipeline import MODEL, apply_updates, ask_llm, insert_email, now, read_eml
from seed import DEFAULT_ITEMS, FIXTURE_DIR, reset as reset_demo


app = FastAPI(title="AI Checklist Demo API")
db_lock = threading.Lock()
DUPLICATE_MODEL = os.environ.get(
    "ANTHROPIC_DUPLICATE_MODEL", "claude-haiku-4-5-20251001"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ProcessAllRequest(BaseModel):
    custom_email_text: str = ""
    custom_email_texts: list[str] = Field(default_factory=list)


class ItemUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1)
    status: str | None = None
    allow_similar: bool = False


def _row_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _normalize_item_name(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").split())


def _find_similar_item(
    conn: sqlite3.Connection, item_id: str, label: str
) -> tuple[sqlite3.Row | None, float]:
    candidate_id = _normalize_item_name(item_id)
    candidate_label = _normalize_item_name(label)
    best_row = None
    best_score = 0.0
    for row in conn.execute("SELECT id, label FROM items"):
        score = max(
            SequenceMatcher(None, candidate_id, _normalize_item_name(row["id"])).ratio(),
            SequenceMatcher(None, candidate_label, _normalize_item_name(row["label"])).ratio(),
        )
        if score > best_score:
            best_row = row
            best_score = score
    return best_row, best_score


def _find_semantic_duplicate(
    conn: sqlite3.Connection, label: str
) -> sqlite3.Row | None:
    existing = list(conn.execute("SELECT id, label FROM items ORDER BY id"))
    if not existing:
        return None

    item_ids = [row["id"] for row in existing]
    schema = {
        "type": "object",
        "properties": {
            "duplicate_item_id": {
                "type": "string",
                "enum": ["", *item_ids],
            }
        },
        "required": ["duplicate_item_id"],
        "additionalProperties": False,
    }
    prompt = f"""Decide whether the proposed checklist item describes the same
document as one existing item. Treat singular/plural forms, abbreviations and
true synonyms as duplicates. Do not match documents that are merely related.

Proposed item: {json.dumps(label, ensure_ascii=False)}
Existing items: {json.dumps({row['id']: row['label'] for row in existing}, ensure_ascii=False)}

Return an empty duplicate_item_id when no item describes the same document."""
    response = Anthropic().messages.create(
        model=DUPLICATE_MODEL,
        max_tokens=100,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(block.text for block in response.content if block.type == "text")
    duplicate_id = json.loads(text)["duplicate_item_id"]
    return next((row for row in existing if row["id"] == duplicate_id), None)


def _fixture_emails() -> list[dict]:
    emails: list[dict] = []
    for path in sorted(FIXTURE_DIR.glob("*.eml")):
        body, attachments, meta = read_eml(path)
        emails.append(
            {
                **meta,
                "body": body,
                "attachments": attachments,
                "path": str(path),
            }
        )
    return emails


def _state(conn: sqlite3.Connection) -> dict:
    items = [
        {**_row_dict(row), "deletable": row["id"] not in DEFAULT_ITEMS}
        for row in conn.execute(
            """SELECT i.id, i.label, i.status, i.updated_at,
                      (SELECT e.message_id
                         FROM events ev
                         JOIN emails e ON e.id = ev.email_id
                        WHERE ev.item_id = i.id
                          AND ev.new_status = i.status
                          AND ev.source = 'email'
                        ORDER BY ev.id DESC
                        LIMIT 1) AS evidence_message_id
                 FROM items i
                 ORDER BY i.id"""
        )
    ]
    processed_emails = [
        {
            **_row_dict(row),
            "attachments": json.loads(row["attachments"] or "[]"),
        }
        for row in conn.execute(
            """SELECT id, message_id, filename, sender, subject, sent_at, body,
                      attachments, processed_at
               FROM emails
               ORDER BY id"""
        )
    ]
    return {
        "items": items,
        "processed_emails": processed_emails,
        "fixture_emails": _fixture_emails(),
        "statuses": list(STATUS_RANK.keys()),
    }


def _emails_for_full_check(
    conn: sqlite3.Connection, custom_texts: list[str]
) -> list[dict]:
    emails = [
        {
            "id": row["id"],
            "body": row["body"] or "",
            "attachments": json.loads(row["attachments"] or "[]"),
            "meta": {
                "message_id": row["message_id"],
                "filename": row["filename"],
                "sender": row["sender"],
                "subject": row["subject"],
                "sent_at": row["sent_at"],
            },
        }
        for row in conn.execute(
            """SELECT id, message_id, filename, sender, subject, sent_at,
                      body, attachments
               FROM emails
               ORDER BY id"""
        )
    ]
    known_message_ids = {email["meta"]["message_id"] for email in emails}

    for fixture in _fixture_emails():
        if fixture["message_id"] not in known_message_ids:
            emails.append(
                {
                    "id": None,
                    "body": fixture["body"],
                    "attachments": fixture["attachments"],
                    "meta": fixture,
                }
            )
            known_message_ids.add(fixture["message_id"])

    for custom_text in custom_texts:
        emails.append(
            {
                "id": None,
                "body": custom_text,
                "attachments": [],
                "meta": {
                    "message_id": f"<custom-{uuid4()}@demo.local>",
                    "filename": "custom-input.txt",
                    "sender": "Demo User <demo@example.local>",
                    "subject": "Custom test email",
                    "sent_at": "",
                },
            }
        )
    return emails


def _needs_full_recheck(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM app_state WHERE key = 'needs_full_recheck'"
    ).fetchone()
    return row is None or row["value"] == "1"


def _set_full_recheck(conn: sqlite3.Connection, required: bool) -> None:
    conn.execute(
        """INSERT INTO app_state (key, value)
           VALUES ('needs_full_recheck', ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        ("1" if required else "0",),
    )


def _new_emails_for_check(
    conn: sqlite3.Connection, custom_texts: list[str]
) -> list[dict]:
    return [
        email
        for email in _emails_for_full_check(conn, custom_texts)
        if email["id"] is None
    ]


def _store_email_result(conn: sqlite3.Connection, email: dict, result: dict) -> None:
    if email["id"] is None:
        email["id"] = insert_email(
            conn,
            email["body"],
            email["attachments"],
            email["meta"],
            result,
        )
    else:
        conn.execute(
            """UPDATE emails
               SET model = ?, raw_response = ?, processed_at = ?
               WHERE id = ?""",
            (MODEL, json.dumps(result, ensure_ascii=False), now(), email["id"]),
        )
    apply_updates(conn, email["id"], result)


@app.on_event("startup")
def startup() -> None:
    with db_lock:
        conn = get_conn()
        try:
            init_db(conn)
        finally:
            conn.close()


@app.get("/api/state")
def get_state() -> dict:
    with db_lock:
        conn = get_conn()
        try:
            return _state(conn)
        finally:
            conn.close()


@app.post("/api/process-all")
def process_all(payload: ProcessAllRequest | None = None) -> dict:
    custom_texts = []
    if payload:
        custom_texts = [text.strip() for text in payload.custom_email_texts if text.strip()]
        if payload.custom_email_text.strip():
            custom_texts.append(payload.custom_email_text.strip())

    with db_lock:
        conn = get_conn()
        try:
            full_recheck = _needs_full_recheck(conn)
            emails = (
                _emails_for_full_check(conn, custom_texts)
                if full_recheck
                else _new_emails_for_check(conn, custom_texts)
            )
            item_labels = get_item_labels(conn)

            try:
                results = [
                    ask_llm(
                        email["body"],
                        email["attachments"],
                        item_labels,
                        message_id=None,
                    )
                    for email in emails
                ]
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

            with conn:
                for email, result in zip(emails, results):
                    if email["id"] is None:
                        email["id"] = insert_email(
                            conn,
                            email["body"],
                            email["attachments"],
                            email["meta"],
                            result,
                        )
                    else:
                        conn.execute(
                            """UPDATE emails
                               SET model = ?, raw_response = ?, processed_at = ?
                               WHERE id = ?""",
                            (MODEL, json.dumps(result, ensure_ascii=False), now(), email["id"]),
                        )

                if full_recheck:
                    conn.execute("DELETE FROM events")
                    conn.execute("DELETE FROM proposals")
                    conn.execute("UPDATE items SET status = 'open', updated_at = NULL")

                for email, result in zip(emails, results):
                    apply_updates(conn, email["id"], result)
                if full_recheck:
                    _set_full_recheck(conn, False)

            return {"processed": len(emails), **_state(conn)}
        finally:
            conn.close()


@app.post("/api/process-all/stream")
def process_all_stream(payload: ProcessAllRequest | None = None) -> StreamingResponse:
    custom_texts = []
    if payload:
        custom_texts = [text.strip() for text in payload.custom_email_texts if text.strip()]
        if payload.custom_email_text.strip():
            custom_texts.append(payload.custom_email_text.strip())

    def stream():
        with db_lock:
            conn = get_conn()
            try:
                full_recheck = _needs_full_recheck(conn)
                emails = (
                    _emails_for_full_check(conn, custom_texts)
                    if full_recheck
                    else _new_emails_for_check(conn, custom_texts)
                )
                item_labels = get_item_labels(conn)
                total = len(emails)
                failed = 0

                if full_recheck:
                    with conn:
                        conn.execute("DELETE FROM events")
                        conn.execute("DELETE FROM proposals")
                        conn.execute("UPDATE items SET status = 'open', updated_at = NULL")

                yield json.dumps(
                    {"type": "started", "completed": 0, "total": total, "state": _state(conn)},
                    ensure_ascii=False,
                ) + "\n"

                with ThreadPoolExecutor(max_workers=3) as executor:
                    futures = [
                        executor.submit(
                            ask_llm,
                            email["body"],
                            email["attachments"],
                            item_labels,
                            None,
                        )
                        for email in emails
                    ]

                    for index, (email, future) in enumerate(
                        zip(emails, futures), start=1
                    ):
                        try:
                            result = future.result()
                            with conn:
                                _store_email_result(conn, email, result)
                            event_type = "progress"
                            error = None
                        except Exception as exc:
                            failed += 1
                            event_type = "email_error"
                            error = str(exc)

                        yield json.dumps(
                            {
                                "type": event_type,
                                "completed": index,
                                "total": total,
                                "failed": failed,
                                "error": error,
                                "state": _state(conn),
                            },
                            ensure_ascii=False,
                        ) + "\n"

                if full_recheck and failed == 0:
                    with conn:
                        _set_full_recheck(conn, False)

                yield json.dumps(
                    {
                        "type": "complete",
                        "completed": total,
                        "total": total,
                        "failed": failed,
                        "state": _state(conn),
                    },
                    ensure_ascii=False,
                ) + "\n"
            finally:
                conn.close()

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/reset")
def reset_state() -> dict:
    with db_lock:
        conn = get_conn()
        try:
            reset_demo(conn)
            return _state(conn)
        finally:
            conn.close()


@app.patch("/api/items/{item_id}")
def upsert_item(item_id: str, payload: ItemUpdate) -> dict:
    item_id = item_id.strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="item_id must not be empty")
    if payload.status is not None and payload.status not in STATUS_RANK:
        raise HTTPException(status_code=400, detail="invalid status")

    with db_lock:
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT id, label, status FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            label = payload.label.strip() if payload.label is not None else None
            status = payload.status

            if row is None and label and not payload.allow_similar:
                similar, score = _find_similar_item(conn, item_id, label)
                match_type = "text"
                if similar is None or score < 0.78:
                    similar = _find_semantic_duplicate(conn, label)
                    match_type = "semantic"
                if similar is not None:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "similar_item",
                            "item_id": similar["id"],
                            "label": similar["label"],
                            "similarity": round(score, 2),
                            "match_type": match_type,
                        },
                    )

            with conn:
                if row is None:
                    conn.execute(
                        "INSERT INTO items (id, label, status) VALUES (?, ?, ?)",
                        (item_id, label or item_id, status or "open"),
                    )
                    _set_full_recheck(conn, True)
                else:
                    conn.execute(
                        """UPDATE items
                           SET label = COALESCE(?, label),
                               status = COALESCE(?, status),
                               updated_at = datetime('now')
                           WHERE id = ?""",
                        (label, status, item_id),
                    )
            return _state(conn)
        finally:
            conn.close()


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str) -> dict:
    item_id = item_id.strip()
    if item_id in DEFAULT_ITEMS:
        raise HTTPException(status_code=400, detail="Default items cannot be deleted")

    with db_lock:
        conn = get_conn()
        try:
            with conn:
                conn.execute("DELETE FROM events WHERE item_id = ?", (item_id,))
                result = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="Item not found")
            return _state(conn)
        finally:
            conn.close()
