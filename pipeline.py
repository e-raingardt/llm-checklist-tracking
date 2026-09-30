"""Verarbeitungspipeline: .eml lesen -> LLM fragen -> Vorschlaege pruefen.

Kernidee: Das Modell entscheidet nichts. Es schlaegt vor, deterministische
Regeln entscheiden, und jeder Vorschlag wird aufgezeichnet - auch die
abgelehnten.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path

import anthropic
from anthropic import Anthropic
from dotenv import load_dotenv

from db import STATUS_RANK, email_exists, get_item_labels, get_status

load_dotenv()

MODEL = "claude-opus-5-5"
CACHE_DIR = Path("fixtures/cache")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- E-Mail lesen --------------------------------------------------------

def read_eml(path: Path) -> tuple[str, list[str], dict]:
    with open(path, "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    attachments = [
        a.get_filename() for a in msg.iter_attachments() if a.get_filename()
    ]
    meta = {
        "message_id": msg.get("Message-ID", path.name),
        "filename": path.name,
        "sender": msg.get("From", ""),
        "subject": msg.get("Subject", ""),
        "sent_at": msg.get("Date", ""),
    }
    return text, attachments, meta


# --- LLM -----------------------------------------------------------------

def _cache_path(message_id: str) -> Path:
    """<msg-001@demo.local> -> fixtures/cache/_msg-001_demo.local_.json

    Werte aus externen Quellen werden bereinigt, bevor sie zu Dateinamen
    werden (ein '/' im Header koennte sonst aus dem Ordner ausbrechen).
    """
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", message_id)
    return CACHE_DIR / f"{safe}.json"


def ask_llm(email_text: str, attachments: list[str], item_labels: dict[str, str], message_id: str | None = None,) -> dict:
    """Fragt das Modell - oder liefert die aufgezeichnete Antwort.

    message_id=None ist der Live-Fall (Textfeld in der UI): weder lesen
    noch schreiben, damit Besucher-Eingaben den Fixture-Cache nicht fuellen.

    Neu aufzeichnen: fixtures/cache/ loeschen und seed.py erneut laufen
    lassen (noetig nach Aenderungen am Prompt oder an den Fixtures).
    """
    cache_file = _cache_path(message_id) if message_id else None

    # --- 1. Cache lesen ---
    if cache_file and cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    # --- 2. Sicherung: kein Cache und kein Key ---
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            f"Kein Cache-Eintrag unter {cache_file} und kein "
            f"ANTHROPIC_API_KEY gesetzt."
        )

    # --- 3. Prompt ---
    schema = {
        "type": "object",
        "properties": {
            "updates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string","enum": list(item_labels.keys())},
                        "status": {"type": "string", "enum": ["requested", "expected", "received"]},
                        "evidence": {"type": "string"},
                    },
                    "required": ["item_id", "status", "evidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["updates"],
        "additionalProperties": False,
    }

    prompt = f"""Analysiere die E-Mail aus Sicht des Absenders und melde Statusaenderungen für diese Items:
{json.dumps(item_labels, indent=2, ensure_ascii=False)}

Statusregeln:
- requested: Der Absender fordert den Empfaenger auf, das Dokument zu senden oder bereitzustellen.
- expected: Der Absender kuendigt an, dass er selbst das Dokument spaeter senden oder bereitstellen wird.
- received: Der Absender sendet das Dokument jetzt in der E-Mail oder es liegt als passender Anhang vor.

Entscheide nach der Richtung der Handlung und nicht allein nach einzelnen Verben.
Ordne einen Status nur zu, wenn sich die belegende Textstelle eindeutig auf das konkrete Checklist-Item bezieht.
Evidence muss die konkrete Textstelle enthalten, die den Status belegt.
Wenn nichts zutrifft, gib eine leere updates-Liste zurück.

<email>
{email_text}
</email>
<attachments>{json.dumps(attachments, ensure_ascii=False)}</attachments>"""

    resp = Anthropic().messages.create(
        model=MODEL,
        max_tokens=1000,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    result = json.loads(text)

    # --- 4. Cache schreiben ---
    if cache_file:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return result


# --- Schreiben -----------------------------------------------------------

def insert_email(conn: sqlite3.Connection, body: str, attachments: list[str], meta: dict, result: dict) -> int:
    """Legt die Mail an und gibt ihre id zurueck. Kein eigenes 'with conn'."""
    cur = conn.execute(
        """INSERT INTO emails
           (message_id, filename, sender, subject, sent_at, body,
            attachments, model, raw_response, processed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            meta.get("message_id"),
            meta.get("filename"),
            meta.get("sender"),
            meta.get("subject"),
            meta.get("sent_at"),
            body,
            json.dumps(attachments, ensure_ascii=False),
            MODEL,
            json.dumps(result, ensure_ascii=False),
            now(),
        ),
    )
    return cur.lastrowid


def apply_updates(conn: sqlite3.Connection, email_id: int, llm_result: dict) -> list[str]:
    """Deterministische Zustandsaenderung.

    Jeder Vorschlag erzeugt genau eine Zeile in 'proposals'. Nur ein
    akzeptierter Vorschlag erzeugt zusaetzlich ein 'event' und ein UPDATE
    auf 'items'.

    Kein eigenes 'with conn' - der Aufrufer klammert die Transaktion.
    """
    log: list[str] = []

    for u in llm_result.get("updates", []):
        item_id = u.get("item_id")
        new_status = u.get("status")
        evidence = u.get("evidence", "")
        old_status = get_status(conn, item_id) if item_id else None

        # Entscheidung treffen. Gruende sind feste Codes, damit sie
        # spaeter zaehlbar und filterbar sind.
        if old_status is None:
            decision, reason = "rejected", "unknown_item"
        elif new_status not in STATUS_RANK or new_status == "open":
            decision, reason = "rejected", "invalid_status"
        elif STATUS_RANK[new_status] <= STATUS_RANK[old_status]:
            decision, reason = "rejected", "no_progress"
        else:
            decision, reason = "accepted", "applied"

        cur = conn.execute(
            """INSERT INTO proposals
               (email_id, item_id, proposed_status, evidence,decision, reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (email_id, item_id, new_status, evidence,
             decision, reason, now()),
        )
        proposal_id = cur.lastrowid

        if decision == "rejected":
            log.append(f"REJECTED [{reason}]: {item_id} -> {new_status} "
                       f"(aktuell: {old_status})")
            continue

        conn.execute(
            "UPDATE items SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now(), item_id),
        )
        conn.execute(
            """INSERT INTO events
               (item_id, old_status, new_status, source,
                email_id, proposal_id, created_at)
               VALUES (?, ?, ?, 'email', ?, ?, ?)""",
            (item_id, old_status, new_status, email_id, proposal_id, now()),
        )
        log.append(f"ACCEPTED: {item_id}: {old_status} -> {new_status} "
                   f"({evidence})")

    return log


def process_email(conn: sqlite3.Connection, email_text: str, attachments: list[str], meta: dict) -> int | None:
    """Verarbeitet eine Mail vollstaendig. Gibt die email_id zurueck."""
    message_id = meta.get("message_id")

    # Doppelverarbeitung ist in einem Intake-Prozess ein echter Fehler.
    if message_id and email_exists(conn, message_id):
        print(f"UEBERSPRUNGEN: {message_id} wurde bereits verarbeitet.")
        return None

    item_labels = get_item_labels(conn)

    # LLM-Aufruf BEWUSST ausserhalb der Transaktion: er dauert Sekunden,
    # und solange eine Transaktion offen ist, sperrt SQLite die Datei.
    try:
        result = ask_llm(email_text, attachments, item_labels, message_id=message_id)
    except RuntimeError as e:
        print(f"Fehler: {e}")
        return None
    except anthropic.AuthenticationError:
        print("Fehler: Ungueltiger API-Key.")
        return None
    except anthropic.RateLimitError:
        print("Fehler: Rate-Limit erreicht. Spaeter erneut versuchen.")
        return None
    except anthropic.APIConnectionError:
        print("Fehler: Keine Verbindung zur API.")
        return None
    except anthropic.APIStatusError as e:
        print(f"Fehler: API antwortete mit Status {e.status_code}.")
        return None
    except json.JSONDecodeError:
        print("Fehler: LLM-Antwort war kein gueltiges JSON.")
        return None

    # Eine Transaktion pro Mail: entweder alles oder nichts. Sonst kann
    # der Audit-Trail nicht mehr zum Zustand passen.
    with conn:
        email_id = insert_email(conn, email_text, attachments, meta, result)
        for line in apply_updates(conn, email_id, result):
            print("  " + line)

    return email_id
