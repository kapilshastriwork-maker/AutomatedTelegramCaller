import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "atc.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    plan_id TEXT,
    run_id TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    earliest_available TEXT,
    patient_name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_SCHEDULED_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    job_id TEXT UNIQUE NOT NULL,
    run_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    who TEXT NOT NULL,
    what TEXT NOT NULL,
    language TEXT,
    patient_name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


_USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_counters (
    chat_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, day)
)
"""

_PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id TEXT UNIQUE NOT NULL,
    chat_id INTEGER NOT NULL,
    clinic_name TEXT,
    phone TEXT NOT NULL,
    patient_name TEXT,
    reason TEXT,
    what TEXT,
    original_call_run_id TEXT,
    alternatives_json TEXT NOT NULL,
    chosen_index INTEGER,
    second_call_run_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


_CHAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id TEXT UNIQUE NOT NULL,
    chat_id INTEGER NOT NULL,
    language TEXT,
    patient_name TEXT,
    reason TEXT,
    preferred_date TEXT,
    preferred_time TEXT,
    steps_json TEXT NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    last_message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.execute(_SCHEDULED_SCHEMA)
    conn.execute(_USAGE_SCHEMA)
    conn.execute(_PENDING_SCHEMA)
    conn.execute(_CHAIN_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(calls)")}
    if "status_message_id" not in columns:
        try:
            conn.execute("ALTER TABLE calls ADD COLUMN status_message_id INTEGER")
        except sqlite3.OperationalError:
            pass
    if "batch_id" not in columns:
        try:
            conn.execute("ALTER TABLE calls ADD COLUMN batch_id TEXT")
        except sqlite3.OperationalError:
            pass
    if "clinic_name" not in columns:
        try:
            conn.execute("ALTER TABLE calls ADD COLUMN clinic_name TEXT")
        except sqlite3.OperationalError:
            pass
    if "earliest_available" not in columns:
        try:
            conn.execute("ALTER TABLE calls ADD COLUMN earliest_available TEXT")
        except sqlite3.OperationalError:
            pass
    if "phone" not in columns:
        try:
            conn.execute("ALTER TABLE calls ADD COLUMN phone TEXT")
        except sqlite3.OperationalError:
            pass
    if "earliest_batch_summarised" not in columns:
        try:
            conn.execute(
                "ALTER TABLE calls ADD COLUMN earliest_batch_summarised "
                "INTEGER NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass
    if "patient_name" not in columns:
        try:
            conn.execute("ALTER TABLE calls ADD COLUMN patient_name TEXT")
        except sqlite3.OperationalError:
            pass
    sched_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(scheduled_calls)")
    }
    if "language" not in sched_columns:
        try:
            conn.execute("ALTER TABLE scheduled_calls ADD COLUMN language TEXT")
        except sqlite3.OperationalError:
            pass
    if "patient_name" not in sched_columns:
        try:
            conn.execute("ALTER TABLE scheduled_calls ADD COLUMN patient_name TEXT")
        except sqlite3.OperationalError:
            pass


def insert_call(
    chat_id: int,
    plan_id: str | None,
    run_id: str,
    status_message_id: int | None = None,
    batch_id: str | None = None,
    clinic_name: str | None = None,
    phone: str | None = None,
    patient_name: str | None = None,
) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO calls (chat_id, plan_id, run_id, status_message_id, "
            "batch_id, clinic_name, phone, patient_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chat_id,
                plan_id,
                run_id,
                status_message_id,
                batch_id,
                clinic_name,
                phone,
                patient_name,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def get_running_calls() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, chat_id, plan_id, run_id, status_message_id, clinic_name, phone, patient_name, batch_id "
            "FROM calls WHERE status = 'running'"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_stuck_running_calls(older_than_seconds: int) -> list[dict]:
    """Return rows stuck in status='running' whose created_at is older than
    the given wall-clock seconds. Used by the poller safety net to detect
    calls that have been polling forever without reaching a terminal status
    (e.g. unknown status string from CALL-E, or a code path that never
    called finish_call).

    Note: `created_at` is written in *local* time (naive ISO with a 'T'
    separator, since the project assumes server and user share one
    timezone — see parse_when in app/core.py), so the comparison uses
    `strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime', ...)` to (a)
    compare in the same timezone and (b) match the same wall-clock
    format that `created_at` uses after `strftime` normalisation.
    Without this, a string comparison fails on the 'T' vs ' ' separator
    mismatch between Python isoformat() and SQLite's datetime('now').
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, chat_id, plan_id, run_id, status_message_id, "
            "clinic_name, phone, patient_name, batch_id, created_at "
            "FROM calls WHERE status = 'running' "
            "AND strftime('%Y-%m-%d %H:%M:%S', created_at) < "
            "strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime', ?)",
            (f"-{older_than_seconds} seconds",),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def set_call_status_by_rowid(row_id: int, status: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE calls SET status = ? WHERE id = ?", (status, row_id))
        conn.commit()
    finally:
        conn.close()


def finish_call(run_id: str, status: str) -> None:
    conn = _connect()
    try:
        conn.execute("UPDATE calls SET status = ? WHERE run_id = ?", (status, run_id))
        conn.commit()
    finally:
        conn.close()


def set_earliest_available(run_id: str, raw_text: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE calls SET earliest_available = ? WHERE run_id = ?",
            (raw_text, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_open_earliest_batches() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT batch_id, chat_id FROM calls "
            "WHERE batch_id LIKE 'earliest_%' "
            "AND earliest_batch_summarised = 0"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_batch_calls(batch_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, chat_id, plan_id, run_id, status, status_message_id, "
            "clinic_name, earliest_available, earliest_batch_summarised "
            "FROM calls WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def mark_earliest_batch_summarised(batch_id: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE calls SET earliest_batch_summarised = 1 WHERE batch_id = ?",
            (batch_id,),
        )
        conn.commit()
    finally:
        conn.close()


def insert_scheduled(
    chat_id: int,
    job_id: str,
    run_at: str,
    who: str,
    what: str,
    language: str | None = None,
    patient_name: str | None = None,
) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO scheduled_calls (chat_id, job_id, run_at, who, what, language, patient_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, job_id, run_at, who, what, language, patient_name),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def list_scheduled_for_chat(chat_id: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT chat_id, job_id, run_at, status, who, what, language, patient_name "
            "FROM scheduled_calls "
            "WHERE chat_id = ? AND status = 'scheduled' ORDER BY run_at",
            (chat_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def find_scheduled_by_prefix(chat_id: int, short_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT job_id, run_at, status, who, what FROM scheduled_calls "
            "WHERE chat_id = ? AND status = 'scheduled' AND job_id LIKE ?",
            (chat_id, short_id + "%"),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_scheduled_status(job_id: str, status: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE scheduled_calls SET status = ? WHERE job_id = ?", (status, job_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_usage(chat_id: int, day: str) -> int:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT count FROM usage_counters WHERE chat_id = ? AND day = ?",
            (chat_id, day),
        ).fetchone()
        return int(row["count"]) if row else 0
    finally:
        conn.close()


def bump_usage(chat_id: int, day: str) -> int:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO usage_counters (chat_id, day, count) VALUES (?, ?, 1) "
            "ON CONFLICT(chat_id, day) DO UPDATE SET count = count + 1",
            (chat_id, day),
        )
        conn.commit()
        row = conn.execute(
            "SELECT count FROM usage_counters WHERE chat_id = ? AND day = ?",
            (chat_id, day),
        ).fetchone()
        return int(row["count"])
    finally:
        conn.close()


def insert_pending_confirmation(
    short_id: str,
    chat_id: int,
    clinic_name: str,
    phone: str,
    patient_name: str,
    reason: str,
    what: str,
    original_call_run_id: str,
    alternatives_list: list[dict],
) -> None:
    conn = _connect()
    try:
        import json

        alternatives_json = json.dumps(alternatives_list)
        conn.execute(
            """
            INSERT INTO pending_confirmations (
                short_id, chat_id, clinic_name, phone, patient_name, reason, what,
                original_call_run_id, alternatives_json, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                short_id,
                chat_id,
                clinic_name,
                phone,
                patient_name,
                reason,
                what,
                original_call_run_id,
                alternatives_json,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_pending_confirmation(short_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT id, short_id, chat_id, clinic_name, phone, patient_name, reason, what,
                   original_call_run_id, alternatives_json, chosen_index, second_call_run_id, status, created_at
            FROM pending_confirmations
            WHERE short_id = ?
            """,
            (short_id,),
        ).fetchone()
        if row is None:
            return None
        import json

        return {
            "id": row["id"],
            "short_id": row["short_id"],
            "chat_id": row["chat_id"],
            "clinic_name": row["clinic_name"],
            "phone": row["phone"],
            "patient_name": row["patient_name"],
            "reason": row["reason"],
            "what": row["what"],
            "original_call_run_id": row["original_call_run_id"],
            "alternatives": json.loads(row["alternatives_json"]),
            "chosen_index": row["chosen_index"],
            "second_call_run_id": row["second_call_run_id"],
            "status": row["status"],
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def set_pending_status(short_id: str, status: str, **fields) -> None:
    conn = _connect()
    try:
        updates = []
        params = []
        if "chosen_index" in fields:
            updates.append("chosen_index = ?")
            params.append(fields["chosen_index"])
        if "second_call_run_id" in fields:
            updates.append("second_call_run_id = ?")
            params.append(fields["second_call_run_id"])
        if updates:
            params.append(short_id)
            conn.execute(
                f"UPDATE pending_confirmations SET {', '.join(updates)}, status = ? WHERE short_id = ?",
                (*params, status),
            )
            conn.commit()
    finally:
        conn.close()


def get_pending_for_chat(chat_id: int) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, short_id, chat_id, clinic_name, phone, patient_name, reason, what,
                   original_call_run_id, alternatives_json, chosen_index, second_call_run_id, status, created_at
            FROM pending_confirmations
            WHERE chat_id = ? AND status = 'pending'
            """,
            (chat_id,),
        ).fetchall()
        import json

        result = []
        for row in rows:
            result.append(
                {
                    "id": row["id"],
                    "short_id": row["short_id"],
                    "chat_id": row["chat_id"],
                    "clinic_name": row["clinic_name"],
                    "phone": row["phone"],
                    "patient_name": row["patient_name"],
                    "reason": row["reason"],
                    "what": row["what"],
                    "original_call_run_id": row["original_call_run_id"],
                    "alternatives": json.loads(row["alternatives_json"]),
                    "chosen_index": row["chosen_index"],
                    "second_call_run_id": row["second_call_run_id"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                }
            )
        return result
    finally:
        conn.close()


def get_pending_confirmation_by_original_run(original_call_run_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT id, short_id, chat_id, clinic_name, phone, patient_name, reason, what,
                   original_call_run_id, alternatives_json, chosen_index, second_call_run_id, status, created_at
            FROM pending_confirmations
            WHERE original_call_run_id = ? AND status = 'pending'
            """,
            (original_call_run_id,),
        ).fetchone()
        if row is None:
            return None
        import json

        return {
            "id": row["id"],
            "short_id": row["short_id"],
            "chat_id": row["chat_id"],
            "clinic_name": row["clinic_name"],
            "phone": row["phone"],
            "patient_name": row["patient_name"],
            "reason": row["reason"],
            "what": row["what"],
            "original_call_run_id": row["original_call_run_id"],
            "alternatives": json.loads(row["alternatives_json"]),
            "chosen_index": row["chosen_index"],
            "second_call_run_id": row["second_call_run_id"],
            "status": row["status"],
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def insert_chain_run(
    short_id: str,
    chat_id: int,
    language: str | None,
    patient_name: str | None,
    reason: str | None,
    preferred_date: str | None,
    preferred_time: str | None,
    steps_json: str,
) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO chain_runs (
                short_id, chat_id, language, patient_name, reason,
                preferred_date, preferred_time, steps_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                short_id,
                chat_id,
                language,
                patient_name,
                reason,
                preferred_date,
                preferred_time,
                steps_json,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_chain_run(short_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT id, short_id, chat_id, language, patient_name, reason,
                   preferred_date, preferred_time, steps_json, current_step,
                   status, last_message_id, created_at
            FROM chain_runs
            WHERE short_id = ?
            """,
            (short_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "short_id": row["short_id"],
            "chat_id": row["chat_id"],
            "language": row["language"],
            "patient_name": row["patient_name"],
            "reason": row["reason"],
            "preferred_date": row["preferred_date"],
            "preferred_time": row["preferred_time"],
            "steps_json": row["steps_json"],
            "current_step": row["current_step"],
            "status": row["status"],
            "last_message_id": row["last_message_id"],
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def advance_chain_step(short_id: str, new_step_index: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE chain_runs SET current_step = ? WHERE short_id = ?",
            (new_step_index, short_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_chain_status(
    short_id: str, status: str, last_message_id: int | None = None
) -> None:
    conn = _connect()
    try:
        if last_message_id is not None:
            conn.execute(
                "UPDATE chain_runs SET status = ?, last_message_id = ? "
                "WHERE short_id = ?",
                (status, last_message_id, short_id),
            )
        else:
            conn.execute(
                "UPDATE chain_runs SET status = ? WHERE short_id = ?",
                (status, short_id),
            )
        conn.commit()
    finally:
        conn.close()
