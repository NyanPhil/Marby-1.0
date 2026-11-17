"""Durable, shared cash/GCash transaction numbering (standard library only)."""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path


class TransactionNumbers:
    """Reserve one increasing number per transaction, independent of clock time."""

    def __init__(self, filename, legacy_log=None):
        self.path = Path(filename)
        self.legacy_log = Path(legacy_log) if legacy_log is not None else None
        self._ready = False

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(str(self.path), timeout=5)
        if not self._ready:
            try:
                database.execute("BEGIN IMMEDIATE")
                database.execute("""CREATE TABLE IF NOT EXISTS transactions (
                    number INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL, mode TEXT NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL, details TEXT NOT NULL)""")
                database.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)")
                imported = database.execute(
                    "SELECT value FROM metadata WHERE name='legacy_imported'").fetchone()
                if imported is None:
                    if self.legacy_log is not None and self.legacy_log.exists():
                        with self.legacy_log.open(encoding="utf-8") as history:
                            for line in history:
                                try:
                                    record = json.loads(line)
                                except json.JSONDecodeError:
                                    continue  # A truncated old log line is not a completed transaction.
                                if not isinstance(record, dict):
                                    continue
                                event = record.get("kind")
                                ref = str(record.get("ref", ""))
                                numbered = ref.isascii() and ref.isdigit() and int(ref) > 0
                                if not numbered and event not in ("exchange", "gcash"):
                                    continue
                                values = (record.get("time", ""), record.get("mode", "legacy"),
                                          "gcash" if event == "gcash" or record.get("flow") == "gcash" else "cash",
                                          "completed" if event in ("exchange", "gcash") else "incident",
                                          json.dumps(record))
                                if numbered:
                                    database.execute(
                                        "INSERT OR REPLACE INTO transactions(number,created_at,mode,kind,status,details) "
                                        "VALUES(?,?,?,?,?,?)", (int(ref), *values))
                                else:
                                    database.execute(
                                        "INSERT INTO transactions(created_at,mode,kind,status,details) VALUES(?,?,?,?,?)",
                                        values)
                    database.execute("INSERT INTO metadata(name,value) VALUES('legacy_imported','1')")
                database.commit()
                self._ready = True
            except Exception:
                database.rollback()
                database.close()
                raise
        return database

    def ensure_ready(self):
        with closing(self._connect()):
            pass

    def allocate(self, kind, mode, details):
        with closing(self._connect()) as database, database:
            cursor = database.execute(
                "INSERT INTO transactions(created_at,mode,kind,status,details) VALUES(?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M:%S"), mode, kind, "open", json.dumps(details)))
            number = cursor.lastrowid
        return f"{number:06d}"

    def record(self, reference, status, details):
        with closing(self._connect()) as database, database:
            result = database.execute("UPDATE transactions SET status=?,details=? WHERE number=?",
                                      (status, json.dumps(details), int(reference)))
            if result.rowcount != 1:
                raise RuntimeError("Transaction reference is missing from its saved history.")
