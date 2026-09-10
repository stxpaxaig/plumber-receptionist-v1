import json
import os
import sqlite3
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class LeadStore:
    def __init__(self, database_path):
        self.database_path = database_path
        parent = os.path.dirname(os.path.abspath(database_path))
        os.makedirs(parent, exist_ok=True)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self):
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS webhook_events (
                    event_id TEXT PRIMARY KEY,
                    call_id TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS calls (
                    call_id TEXT PRIMARY KEY,
                    caller_number TEXT,
                    called_number TEXT,
                    started_at TEXT NOT NULL,
                    answered_at TEXT,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    transcript_json TEXT NOT NULL DEFAULT '[]',
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL UNIQUE REFERENCES calls(call_id),
                    caller_name TEXT NOT NULL,
                    callback_number TEXT NOT NULL,
                    service_address TEXT NOT NULL,
                    plumbing_problem TEXT NOT NULL,
                    urgency TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    caller_number TEXT,
                    called_number TEXT,
                    call_time TEXT NOT NULL,
                    saved_at TEXT NOT NULL,
                    transcript_json TEXT NOT NULL
                );
            """)

    def claim_webhook(self, event_id, call_id):
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO webhook_events(event_id, call_id, received_at) VALUES (?, ?, ?)",
                    (event_id, call_id, utc_now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def start_call(self, call_id, caller_number, called_number):
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO calls
                   (call_id, caller_number, called_number, started_at, status)
                   VALUES (?, ?, ?, ?, 'received')""",
                (call_id, caller_number, called_number, utc_now()),
            )

    def mark_answered(self, call_id):
        with self._connect() as db:
            db.execute(
                "UPDATE calls SET answered_at = ?, status = 'answered' WHERE call_id = ?",
                (utc_now(), call_id),
            )

    def update_transcript(self, call_id, transcript):
        with self._connect() as db:
            db.execute(
                "UPDATE calls SET transcript_json = ? WHERE call_id = ?",
                (json.dumps(transcript, ensure_ascii=False), call_id),
            )

    def finish_call(self, call_id, status, transcript, error):
        with self._connect() as db:
            db.execute(
                """UPDATE calls
                   SET ended_at = ?, status = ?, transcript_json = ?, error = ?
                   WHERE call_id = ?""",
                (utc_now(), status, json.dumps(transcript, ensure_ascii=False), error, call_id),
            )
            db.execute(
                "UPDATE leads SET transcript_json = ? WHERE call_id = ?",
                (json.dumps(transcript, ensure_ascii=False), call_id),
            )

    def save_lead(self, call_id, caller_number, called_number, lead, transcript):
        required = (
            "caller_name",
            "callback_number",
            "service_address",
            "plumbing_problem",
            "urgency",
            "summary",
        )
        missing = [field for field in required if not str(lead.get(field, "")).strip()]
        if missing:
            raise ValueError(f"Missing lead fields: {', '.join(missing)}")
        if lead["urgency"] not in {"emergency", "urgent", "routine", "unsure"}:
            raise ValueError("Invalid urgency")

        now = utc_now()
        with self._connect() as db:
            call = db.execute(
                "SELECT started_at FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if call is None:
                raise ValueError("Unknown call")
            cursor = db.execute(
                """INSERT INTO leads (
                    call_id, caller_name, callback_number, service_address,
                    plumbing_problem, urgency, summary, caller_number,
                    called_number, call_time, saved_at, transcript_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(call_id) DO UPDATE SET
                    caller_name=excluded.caller_name,
                    callback_number=excluded.callback_number,
                    service_address=excluded.service_address,
                    plumbing_problem=excluded.plumbing_problem,
                    urgency=excluded.urgency,
                    summary=excluded.summary,
                    saved_at=excluded.saved_at,
                    transcript_json=excluded.transcript_json
                RETURNING id""",
                (
                    call_id,
                    lead["caller_name"].strip(),
                    lead["callback_number"].strip(),
                    lead["service_address"].strip(),
                    lead["plumbing_problem"].strip(),
                    lead["urgency"],
                    lead["summary"].strip(),
                    caller_number,
                    called_number,
                    call["started_at"],
                    now,
                    json.dumps(transcript, ensure_ascii=False),
                ),
            )
            return cursor.fetchone()[0]

    def list_leads(self):
        with self._connect() as db:
            rows = db.execute(
                """SELECT id, call_id, caller_name, callback_number, service_address,
                          plumbing_problem, urgency, summary, caller_number,
                          called_number, call_time, saved_at
                   FROM leads ORDER BY id DESC LIMIT 100"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_lead(self, lead_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["transcript"] = json.loads(result.pop("transcript_json"))
        return result
