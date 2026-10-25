"""SQLite storage. Every state change that must be all-or-nothing happens in ONE transaction."""
import json
import os
import sqlite3
from contextlib import contextmanager

from schemas import TERMINAL, canonical_json


class Store:
    def __init__(self, path):
        self.path = path
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        with self.conn() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks(
                    task_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, principal TEXT NOT NULL,
                    batch_id TEXT NOT NULL, state TEXT NOT NULL, task_json TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS tasks_owner_idx ON tasks(principal);
                CREATE TABLE IF NOT EXISTS idempotency(
                    principal TEXT NOT NULL, message_id TEXT NOT NULL, message_hash TEXT NOT NULL,
                    task_id TEXT NOT NULL, response_json TEXT NOT NULL,
                    PRIMARY KEY(principal, message_id));
                CREATE TABLE IF NOT EXISTS decisions(
                    package_hash TEXT PRIMARY KEY, decision_json TEXT NOT NULL);
                """
            )

    @contextmanager
    def conn(self):
        db = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    # ---- idempotency -------------------------------------------------
    def get_idempotency(self, principal, message_id):
        with self.conn() as db:
            return db.execute(
                "SELECT * FROM idempotency WHERE principal=? AND message_id=?", (principal, message_id)
            ).fetchone()

    # ---- tasks -------------------------------------------------------
    def create_task(self, tid, cid, principal, batch_id, state, task, message_id, message_hash):
        """Create the task AND remember the message id in one transaction."""
        with self.conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO tasks(task_id,context_id,principal,batch_id,state,task_json) VALUES(?,?,?,?,?,?)",
                    (tid, cid, principal, batch_id, state, canonical_json(task)),
                )
                db.execute(
                    "INSERT INTO idempotency VALUES(?,?,?,?,?)",
                    (principal, message_id, message_hash, tid, canonical_json(task)),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def get_task(self, tid, principal):
        with self.conn() as db:
            return db.execute(
                "SELECT * FROM tasks WHERE task_id=? AND principal=?", (tid, principal)
            ).fetchone()

    def list_tasks(self, principal):
        with self.conn() as db:
            return db.execute(
                "SELECT * FROM tasks WHERE principal=? ORDER BY rowid DESC", (principal,)
            ).fetchall()

    def finish_task(self, tid, principal, state, task, idem=None):
        """Move a NON-terminal task to a terminal state. Returns False if it was already terminal.
        This is the single gate for the cancel-vs-result race: BEGIN IMMEDIATE makes writers take
        turns, and the state is re-read inside the lock, so exactly one caller wins.
        idem=(message_id, message_hash) also stores the replay record in the same transaction."""
        with self.conn() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT state FROM tasks WHERE task_id=? AND principal=?", (tid, principal)
                ).fetchone()
                if not row or row["state"] in TERMINAL:
                    db.execute("ROLLBACK")
                    return False
                db.execute(
                    "UPDATE tasks SET state=?, task_json=? WHERE task_id=? AND principal=?",
                    (state, canonical_json(task), tid, principal),
                )
                if idem:
                    db.execute(
                        "INSERT INTO idempotency VALUES(?,?,?,?,?)",
                        (principal, idem[0], idem[1], tid, canonical_json(task)),
                    )
                db.execute("COMMIT")
                return True
            except Exception:
                db.execute("ROLLBACK")
                raise

    # ---- decision cache (keyed by package CONTENT) -------------------
    def get_decision(self, h):
        with self.conn() as db:
            r = db.execute("SELECT decision_json FROM decisions WHERE package_hash=?", (h,)).fetchone()
            return json.loads(r["decision_json"]) if r else None

    def put_decision(self, h, d):
        with self.conn() as db:
            db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?)", (h, canonical_json(d)))
