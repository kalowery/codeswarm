from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .models import InboundEmail, ReferenceLine


class Storage:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                  run_id TEXT PRIMARY KEY,
                  subject TEXT NOT NULL,
                  sender TEXT NOT NULL,
                  recipients_json TEXT NOT NULL,
                  summary_text TEXT NOT NULL,
                  references_json TEXT NOT NULL,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbound_events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL,
                  recipient TEXT NOT NULL,
                  channel TEXT NOT NULL,
                  status TEXT NOT NULL,
                  details TEXT,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbound_jobs (
                  job_id TEXT PRIMARY KEY,
                  source TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  error TEXT,
                  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def create_run(
        self,
        email: InboundEmail,
        recipients: list[str],
        summary_text: str,
        references: list[ReferenceLine],
    ) -> str:
        run_id = str(uuid.uuid4())
        refs = [r.model_dump() for r in references]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, subject, sender, recipients_json, summary_text, references_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    email.subject,
                    email.sender,
                    json.dumps(recipients),
                    summary_text,
                    json.dumps(refs),
                ),
            )
            conn.commit()
        return run_id

    def enqueue_job(self, source: str, payload: dict[str, Any]) -> str:
        job_id = str(uuid.uuid4())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO inbound_jobs (job_id, source, payload_json, status)
                VALUES (?, ?, ?, 'queued')
                """,
                (job_id, source, json.dumps(payload)),
            )
            conn.commit()
        return job_id

    def claim_next_job(self) -> tuple[str, str, dict[str, Any]] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT job_id, source, payload_json
                FROM inbound_jobs
                WHERE status = 'queued'
                ORDER BY created_at ASC
                LIMIT 1
                """
            ).fetchone()

            if not row:
                return None

            job_id, source, payload_json = row
            conn.execute(
                """
                UPDATE inbound_jobs
                SET status = 'processing', updated_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
                """,
                (job_id,),
            )
            conn.commit()

        return job_id, source, json.loads(payload_json)

    def mark_job_done(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inbound_jobs
                SET status = 'done', updated_at = CURRENT_TIMESTAMP, error = NULL
                WHERE job_id = ?
                """,
                (job_id,),
            )
            conn.commit()

    def mark_job_failed(self, job_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inbound_jobs
                SET status = 'failed', updated_at = CURRENT_TIMESTAMP, error = ?
                WHERE job_id = ?
                """,
                (error[:2000], job_id),
            )
            conn.commit()

    def log_outbound(
        self,
        run_id: str,
        recipient: str,
        channel: str,
        status: str,
        details: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO outbound_events (run_id, recipient, channel, status, details)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, recipient, channel, status, details),
            )
            conn.commit()
