"""Send kiosk transactions and machine status to Supabase (standard library only).

The kiosk keeps working with or without internet. A background thread:
  * every 15 s sends transaction rows that are new or changed since the last send
  * every 15 s picks up refills / stock counts queued on the dashboard and has the
    kiosk apply them (only between transactions), then reports the result
  * every 60 s sends a heartbeat (coin counts, journal status, fault)

Settings come from environment variables, or files in ~/.config/dmarby/:
  DMARBY_SUPABASE_URL  or supabase_url   e.g. https://abcd.supabase.co
  DMARBY_SUPABASE_KEY  or supabase_key   the *publishable* key (sb_publishable_...)
  DMARBY_KIOSK_KEY     or kiosk_key      the machine's kiosk key (secret)
If any is missing, sync stays off and the kiosk runs normally.

Demo mode only syncs when DMARBY_SYNC_DEMO=1, so test runs don't fill the dashboard.

Check from a terminal:
  python dmarby_sync.py --check     # settings + one heartbeat
  python dmarby_sync.py --once      # send everything that's waiting
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "dmarby"
REPORT_EVERY_S = 15
HEARTBEAT_EVERY_S = 60
OFFLINE_AFTER_S = 120            # no answer from Supabase this long = offline
BATCH = 200
KIOSK_TIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def setting(env, filename):
    value = os.environ.get(env, "").strip()
    if not value:
        try:
            value = (CONFIG_DIR / filename).read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
    return value


class SyncError(RuntimeError):
    pass


class KioskSync:
    def __init__(self, db_path, status=None, apply=None):
        self.db_path = str(db_path)
        self.status = status or (lambda: {})
        # apply(adjustment) -> ("applied" | "rejected", before, after, note) or None if busy.
        self.apply = apply
        self.url = setting("DMARBY_SUPABASE_URL", "supabase_url").rstrip("/")
        self.key = setting("DMARBY_SUPABASE_KEY", "supabase_key")
        self.kiosk_key = setting("DMARBY_KIOSK_KEY", "kiosk_key")
        self.stop = threading.Event()
        self.thread = None
        self.last_error = None
        self.last_contact = None   # monotonic time Supabase last answered
        self.started_at = None

    @property
    def configured(self):
        return bool(self.url and self.key and self.kiosk_key)

    def online(self):
        """False once Supabase hasn't answered for OFFLINE_AFTER_S."""
        if not self.configured or not self.thread:
            return True   # sync is off: nothing to judge by, so don't block anything
        since = self.last_contact or self.started_at
        return time.monotonic() - since < OFFLINE_AFTER_S

    # -- HTTP ---------------------------------------------------------------

    def _rpc(self, name, payload):
        request = urllib.request.Request(
            f"{self.url}/rest/v1/rpc/{name}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"apikey": self.key, "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = response.read().decode("utf-8") or "null"
                self.last_contact = time.monotonic()
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            self.last_contact = time.monotonic()   # the server answered, so we're online
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message", "")
            except Exception:
                detail = ""
            raise SyncError(f"Supabase said {exc.code}: {detail or exc.reason}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SyncError(f"No connection to Supabase ({getattr(exc, 'reason', exc)})") from None

    # -- Local bookkeeping ----------------------------------------------------

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS sync_state (number INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL)")
        # Remembers applied dashboard adjustments, so a lost acknowledgement never applies one twice.
        db.execute("CREATE TABLE IF NOT EXISTS applied_adjustments (id INTEGER PRIMARY KEY, status TEXT NOT NULL, "
                   "before_count INTEGER, after_count INTEGER, note TEXT)")
        return db

    @staticmethod
    def _fingerprint(row):
        return hashlib.sha256(json.dumps(row, sort_keys=True).encode("utf-8")).hexdigest()

    def _waiting(self):
        """Rows whose content differs from what was last sent."""
        if not Path(self.db_path).exists():
            return []
        with closing(self._connect()) as db:
            try:
                rows = db.execute(
                    "SELECT t.number, t.created_at, t.mode, t.kind, t.status, t.details, s.fingerprint "
                    "FROM transactions t LEFT JOIN sync_state s ON s.number = t.number "
                    "ORDER BY t.number").fetchall()
            except sqlite3.OperationalError:
                return []   # the kiosk hasn't created its table yet
        waiting = []
        for number, created_at, mode, kind, status, details, sent in rows:
            if mode not in ("demo", "hardware") or not KIOSK_TIME.match(created_at or ""):
                continue    # old imported log lines without a usable time
            if mode == "demo" and os.environ.get("DMARBY_SYNC_DEMO") != "1":
                continue
            try:
                details = json.loads(details)
            except (TypeError, ValueError):
                details = {"raw": details}
            row = {"number": f"{number:06d}", "created_at": created_at, "mode": mode,
                   "kind": kind, "status": status, "details": details}
            fingerprint = self._fingerprint(row)
            if fingerprint != sent:
                waiting.append((number, fingerprint, row))
        return waiting

    # -- Public steps ---------------------------------------------------------

    def report(self):
        """Send waiting rows. Returns how many were sent."""
        sent = 0
        waiting = self._waiting()
        for start in range(0, len(waiting), BATCH):
            chunk = waiting[start:start + BATCH]
            self._rpc("kiosk_report", {"p_key": self.kiosk_key, "p_rows": [r for _, _, r in chunk]})
            with closing(self._connect()) as db, db:
                db.executemany("INSERT OR REPLACE INTO sync_state(number, fingerprint) VALUES(?, ?)",
                               [(n, f) for n, f, _ in chunk])
            sent += len(chunk)
        return sent

    def adjustments(self):
        """Apply refills / counts queued on the dashboard. Returns how many were handled."""
        if not self.apply:
            return 0
        pending = self._rpc("kiosk_pending_adjustments", {"p_key": self.kiosk_key}) or []
        handled = 0
        for adj in pending:
            with closing(self._connect()) as db:
                done = db.execute("SELECT status, before_count, after_count, note FROM applied_adjustments "
                                  "WHERE id = ?", (adj["id"],)).fetchone()
            if done is None:
                result = self.apply(adj)
                if result is None:
                    break           # kiosk is busy; try again next round
                with closing(self._connect()) as db, db:
                    db.execute("INSERT OR REPLACE INTO applied_adjustments VALUES (?, ?, ?, ?, ?)",
                               (adj["id"], *result))
                done = result
            status, before, after, note = done
            self._rpc("kiosk_ack_adjustment", {"p_key": self.kiosk_key, "p_id": adj["id"], "p_status": status,
                                               "p_before": before, "p_after": after, "p_note": note})
            handled += 1
        return handled

    def heartbeat(self):
        status = dict(self.status() or {})
        if status.get("mode") == "demo" and os.environ.get("DMARBY_SYNC_DEMO") != "1":
            return False
        self._rpc("kiosk_heartbeat", {"p_key": self.kiosk_key, "p_status": status})
        return True

    def start(self):
        if not self.configured:
            print("Dashboard sync is off (Supabase settings not found).", file=sys.stderr)
            return False
        if self.thread:
            return True

        def run():
            next_beat = 0
            while not self.stop.is_set():
                try:
                    self.report()
                    if self.adjustments():
                        next_beat = 0      # send the new counts right away
                    if time.monotonic() >= next_beat:
                        self.heartbeat()
                        next_beat = time.monotonic() + HEARTBEAT_EVERY_S
                    if self.last_error:
                        print("Dashboard sync reconnected.", file=sys.stderr)
                    self.last_error = None
                except Exception as exc:   # never let sync take the kiosk down
                    message = str(exc)
                    if message != self.last_error:
                        print(f"Dashboard sync will retry: {message}", file=sys.stderr)
                    self.last_error = message
                self.stop.wait(REPORT_EVERY_S)

        self.started_at = time.monotonic()
        self.thread = threading.Thread(target=run, daemon=True, name="marby-sync")
        self.thread.start()
        return True


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Check or push the kiosk's dashboard sync.")
    parser.add_argument("--db", default=str(Path(__file__).with_name("transaction_numbers.sqlite3")))
    parser.add_argument("--check", action="store_true", help="check settings and send one heartbeat")
    parser.add_argument("--once", action="store_true", help="send every waiting transaction now")
    args = parser.parse_args()
    sync = KioskSync(args.db, status=lambda: {"mode": "hardware" if args.check else None})
    for label, value in (("Supabase URL", sync.url), ("Publishable key", sync.key), ("Kiosk key", sync.kiosk_key)):
        print(f"{label:16} {'found' if value else 'MISSING'}")
    if not sync.configured:
        raise SystemExit("Add the missing settings to ~/.config/dmarby/ first.")
    try:
        if args.check:
            sync._rpc("kiosk_heartbeat", {"p_key": sync.kiosk_key, "p_status": {"mode": "check"}})
            print("Heartbeat accepted. The kiosk can reach the dashboard.")
        if args.once or not args.check:
            print(f"Waiting to send: {len(sync._waiting())}")
            print(f"Sent: {sync.report()}")
    except SyncError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
