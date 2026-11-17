"""Connect the D'MARBY screens to the calibrated Raspberry Pi backend.

All Tk work stays on the main thread. One worker handles sensor-confirmed
servo cycles; the Tk loop consumes its result before updating the journal.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from coin_changer_hardware import HardwareFault, RealHardware, payout_coins_for, atomic_json


def load_config(filename):
    path = Path(filename).expanduser().resolve()
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for field, default in (("inventory_file", "inventory.json"),
                           ("journal_file", "transaction_state.json")):
        value = Path(cfg.get(field, default)).expanduser()
        cfg[field] = str(value if value.is_absolute() else path.parent / value)
    if set(payout_coins_for(cfg)) != {1, 5, 10, 20}:
        raise HardwareFault("This frontend requires the four-coin configuration: 1, 5, 10, 20.")
    return cfg


class KioskHardwareMixin:
    """Place before the frontend Kiosk class in the inheritance list."""

    def __init__(self, root, cfg):
        self._hw_ready = False
        self._recovery_locked = False
        self._syncing = False
        self._deposit_open = None
        self._payout_future = None
        self._payout_coin = None
        self._verified_coin = None
        self.counting_cash = False
        self.references = self.make_reference_store()
        self.hardware = RealHardware(cfg, references=self.references)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marby-payout")
        try:
            super().__init__(root)
            self._hw_ready = True
            self.stock = self._stock_from_backend()
            # Team GCash wallet balance available for manual sends (config, default PHP 2,000).
            self.gcash_balance = int(cfg.get("gcash_float", 2000))
            state = self.hardware.storage.state
            if state.get("status") == "paid":
                request = self.notification_store().get_request(state.get("reference", ""))
                if (request and not self.hardware.input_pending()
                        and not self.hardware._input_fault
                        and not any(inp.is_active for inp in self.hardware.inputs.values())):
                    self.hardware.storage.complete_transfer(request["amount"], request["fee"])
                    self.hardware.stopping.clear()
            if self.hardware.storage.unresolved:
                self._show_saved_incident()
            else:
                self.redraw()
            self._hardware_tick()
        except Exception:
            self.hardware.close()
            self._executor.shutdown(wait=False, cancel_futures=True)
            raise

    def _stock_from_backend(self):
        return {c: self.hardware.storage.inventory.get(str(c), 0)
                for c in self.hardware.coins}

    def _owed(self):
        state = self.hardware.storage.state
        received = state.get("credit", 0) + sum(
            item.get("value", 0) for item in state.get("uncredited_inputs", []))
        delivered = sum(float(c) * n for c, n in state.get("dispensed", {}).items())
        return max(0, int(received - delivered))

    def _show_saved_incident(self):
        """After a restart mid-transaction, show the saved receipt once.

        Reopening never pays anything out. Leaving the receipt (Main menu, or
        automatically after a minute) records it as an incident, moves the journal
        into parked_incidents.jsonl and lets the next customer start.
        """
        state = self.hardware.storage.state
        self.hardware.stopping.set()
        self.hardware.set_acceptance(None)
        self._deposit_open = None
        self.transaction_ref = str(state.get("reference") or "")
        self.credit = state.get("credit", 0)
        self.inserted = [(entry.get("source"), entry.get("value"))
                         for entry in state.get("received", [])]
        self.incident_dismissed = False
        self.fault = self.incident_info = {
            "kind": "hardware",
            "amount": self._owed(),
            "ref": self.transaction_ref or "STAFF REVIEW",
            "bill": next((value for source, value in reversed(self.inserted)
                          if source == "bill"), None),
            "detail": state.get("fault") or "Unfinished transaction found after restart.",
        }
        self.show("incident")

    def show(self, screen, overlay=None):
        if self._hw_ready:
            pending = self.hardware.input_pending()
            if pending and screen not in ("insert", "incident", "admin"):
                self.last_input = time.monotonic()
                return
            # Leaving a deposit with money in it records the balance as an incident
            # first, so the customer's money is never silently forgotten.
            if (screen == "welcome" and self.hardware.storage.unresolved
                    and not self.incident_dismissed and not self.fault):
                self._hardware_fault("Customer left before finishing; balance recorded.")
                return
        super().show(screen, overlay)
        if self._hw_ready:
            self._sync_acceptance()

    def _sync_acceptance(self):
        if self._syncing or self.closed:
            return
        self._syncing = True
        try:
            source = None
            if self.screen == "insert" and not self.overlay and not self.fault:
                if self.flow == "bill" and not self.credit:
                    source = "bill"
                elif self.flow in ("break", "combine", "gcash") and self.credit < self.target:
                    source = "coin"
            if source is not None:
                if not self.hardware.storage.ready_for_source(source, self.hardware.maps[source].values()):
                    self.hardware.set_acceptance(None)
                    if self.credit:
                        self._hardware_fault("Inventory cannot cover the accepted deposit.")
                    else:
                        self.show("out_of_coins")
                    return
                if self._deposit_open != source:
                    self.hardware.begin_deposit(source)
                    self._deposit_open = source
            self.hardware.set_acceptance(source)
        except Exception as exc:
            self._hardware_fault(str(exc))
        finally:
            self._syncing = False

    def _cash_event(self, source, value):
        # These values have already been accepted physically. Persist them
        # before applying frontend target/flow restrictions.
        try:
            self.hardware.storage.receive(source, value, flow=self.flow)
            if self.flow == "gcash":
                self.hardware.storage.state["gcash_customer"] = {
                    "name": self.gc_name, "number": self.gc_number_buf}
                atomic_json(self.hardware.storage.path, self.hardware.storage.state)
        except HardwareFault as exc:
            self.hardware.storage.state.setdefault("uncredited_inputs", []).append(
                {"source": source, "value": value, "time": time.time()})
            self._hardware_fault(f"Accepted additional cash: {source} PHP {value}. {exc}")
            return
        self.credit = self.hardware.storage.state["credit"]
        self.transaction_ref = self.hardware.storage.state["reference"]
        self.stock = self._stock_from_backend()
        self.inserted = [(item["source"], item["value"])
                         for item in self.hardware.storage.state["received"]]
        self.last_input = time.monotonic()
        expected = "bill" if self.flow == "bill" else "coin"
        if (self.screen != "insert" or self.flow not in ("bill", "break", "combine", "gcash")
                or source != expected):
            self._hardware_fault("Cash was accepted outside its deposit screen.")
            return
        self.notice = None
        if source == "bill" and not self.hardware.storage.can_make_exact(value):
            self.incident("wrong_bill_unpayable" if value != self.target else "cant_change", value)
            return
        if source == "bill" and value != self.target:
            self.wrong = (self.target, value)
            self.show("wrong_bill")
        elif self.credit > self.target:
            self._hardware_fault("Accepted coins exceed the selected total; staff must settle the balance.")
        else:
            self.show("insert")

    def exit_incident(self):
        """Leave the receipt; the incident stays recorded and the kiosk keeps going."""
        try:
            self.hardware.resume()
        except Exception as exc:
            print(f"Could not reset hardware after incident: {exc}", file=sys.stderr)
        self._deposit_open = None
        super().exit_incident()

    def _hardware_resume_tick(self):
        # If resume had to wait for a coin being counted, finish it once quiet.
        if (not self.fault and self.screen in ("welcome", "contact", "paused")
                and (self.hardware.storage.unresolved or self.hardware.stopping.is_set())):
            self.hardware.resume()

    def reroute_bill(self):
        if self.fault:
            return False
        self.stock = self._stock_from_backend()
        return super().reroute_bill()

    def _hardware_tick(self):
        if self.closed:
            return
        try:
            for kind, value in self.hardware.poll():
                if kind == "fault":
                    self._hardware_fault(value)
                    break
                self._cash_event(kind, value)
                if self.fault:
                    break
            pending = self.hardware.input_pending()
            changed = pending != self.counting_cash
            self.counting_cash = pending
            if pending:
                self.last_input = time.monotonic()
            if changed and self.screen == "insert" and not self.overlay:
                self.redraw()
            if self._payout_future is not None and self._payout_future.done():
                future, coin = self._payout_future, self._payout_coin
                self._payout_future = self._payout_coin = None
                future.result()
                if not self.fault:
                    self.hardware.storage.confirmed(coin)
                    self._verified_coin = coin
                    self.coin_dropped(coin)
                    if self.screen == "dispensing" and self.dropped < len(self.queue):
                        self._next_coin()
            if not self.fault:
                self._hardware_resume_tick()
                self._sync_acceptance()
        except Exception as exc:
            self._hardware_fault(str(exc))
        if not self.closed:
            self.jobs["hardware"] = self.root.after(50, self._hardware_tick)

    def start_dispense(self):
        if self.fault or self.hardware.storage.state.get("status") != "paid":
            return
        refund = self.flow == "gcash" and self.screen == "insert"  # cancelled cash-in
        if self._payout_future is not None or (self.screen != "review" and not refund):
            return
        try:
            self.hardware.set_acceptance(None)
            if self.hardware.input_pending():
                raise HardwareFault("Cash counting must finish before payout.")
            self.hardware.storage.start(self.plan)
            self.queue = [c for c in sorted(self.plan, reverse=True) for _ in range(self.plan[c])]
            self.dropped = 0
            self.show("dispensing")
            self._next_coin()
        except Exception as exc:
            self._hardware_fault(str(exc))

    def _next_coin(self):
        if (self.fault or self.closed or self._payout_future is not None
                or self.screen != "dispensing" or self.dropped >= len(self.queue)):
            return
        self._payout_coin = self.queue[self.dropped]
        self._payout_future = self._executor.submit(self.hardware.dispense_one, self._payout_coin)

    def coin_dropped(self, value):
        # Only the worker's successful sensor-confirmed cycle can advance UI.
        if (self._verified_coin != value or self.screen != "dispensing"
                or self.dropped >= len(self.queue) or self.queue[self.dropped] != value):
            return False
        self._verified_coin = None
        self.stock = self._stock_from_backend()  # Storage already deducted exactly once.
        self.dropped += 1
        self.redraw()
        if self.dropped == len(self.queue):
            self.jobs["drop"] = self.root.after(500, self.finish_dispense)
        return True

    def finish_dispense(self):
        if self.fault or self.screen != "dispensing":
            return
        try:
            self.hardware.complete_transaction()
            self._deposit_open = None
            super().finish_dispense()
        except Exception as exc:
            self._hardware_fault(str(exc))

    def _hardware_fault(self, detail, kind="hardware", bill=None, picked_bill=None):
        self.hardware.stopping.set()
        try:
            self.hardware.set_acceptance(None)
        except Exception:
            pass
        if getattr(self, "fault", None):
            return
        context = {}
        if bill is not None:
            context["bill"] = bill
        if picked_bill is not None:
            context["picked_bill"] = picked_bill
        try:
            if context:
                self.hardware.storage.state["incident"] = dict(type=kind, **context)
            self.hardware.storage.fault(detail)
        except Exception as exc:
            detail = f"{detail}; journal write failed: {exc}"
        for name in ("drop", "check", "cash"):
            if name in self.jobs:
                self.root.after_cancel(self.jobs.pop(name))
        from uuid import uuid4
        reference = self.hardware.storage.state.get("reference") or self.transaction_ref
        info = {"kind": kind, "amount": self._owed(), "ref": reference or "HW-" + uuid4().hex[:10].upper(),
                "bill": bill, "detail": str(detail), **context}
        self.fault = self.incident_info = info
        self.log("hardware_fault", fault_kind=info["kind"], amount=info["amount"],
                 ref=info["ref"], detail=info["detail"], **context)
        self.show("incident", None)

    def incident(self, kind, amount):
        if kind in ("wrong_bill_unpayable", "cant_change"):
            bill = next((value for source, value in reversed(self.inserted) if source == "bill"), None)
            self._hardware_fault(f"Cannot give exact coin change for accepted PHP {amount} bill.",
                                 kind=kind, bill=bill, picked_bill=self.target)
            return
        self._hardware_fault(f"{kind}: recorded balance PHP {amount}")

    def payout_fault(self):
        self._hardware_fault("Payout was interrupted; inspect the delivered coins and journal.")

    def tick(self):
        if self._hw_ready:
            if self.hardware.input_pending():
                self.last_input = time.monotonic()
            if (self.waiting_for_money() and self.overlay
                    and self.overlay.get("kind") == "timeout_money"
                    and time.monotonic() >= self.overlay["deadline"]):
                self._hardware_fault("The customer timed out with cash in the machine. Balance remains owed.")
        super().tick()

    def start_cashing(self):
        """Coins in, nothing out: close the deposit and save a pending GCash request."""
        if (self.fault or self.flow != "gcash" or self.overlay or self.screen != "insert"
                or self.credit <= 0 or self.hardware.storage.state.get("status") != "paid"):
            return False
        self.gc_amount, self.gc_fee = self.gc_split()
        if self.gc_amount <= 0:
            return False
        try:
            self.hardware.set_acceptance(None)
            if self.hardware.input_pending():
                return False
            self.queue_gcash_request()
            self.hardware.complete_transfer(self.gc_amount, self.gc_fee)
            self._deposit_open = None
        except Exception as exc:
            self._hardware_fault(str(exc))
            return False
        return super().cashing_result(True)

    def cashing_result(self, ok):
        return False  # Only start_cashing, after the journal closes, may record a cash-in.

    def bill_inserted(self, value):
        return False

    def bill_checked(self, value, ok):
        return False

    def coin_inserted(self, value, ok=True):
        return False

    def add_credit(self, kind, value):
        raise HardwareFault("Hardware credit must come from calibrated acceptor pulses.")

    def s_admin(self):
        self.title("Kiosk status", 52, 30)
        counts = "   ".join(f"₱{c}: {self.stock.get(c, 0)}" for c in sorted(self.stock))
        self.t(400, 150, counts, 22, True, "#FFFFFF")
        self.t(400, 225, "Inventory and incident records require staff reconciliation.",
               18, fill="#FFFFFF", width=660)
        self.edge("left", "Exit app", self.close, w=168)
        self.edge("right", "Back", self.go("incident" if self.fault else "welcome"), w=200)

    def admin_stock(self, change):
        return False

    def admin_float(self):
        return False

    def admin_fix(self):
        return False

    def close(self):
        if self.closed:
            return
        try:
            self.hardware.set_acceptance(None)
            self.hardware.stopping.set()
            if (not self._recovery_locked
                    and (self.hardware.storage.state.get("status") in ("paid", "dispensing")
                         or self.hardware.input_pending())):
                # A recovered journal stays untouched across repeated UI closes.
                # A newly interrupted live transaction is marked for staff review.
                self.hardware.storage.fault(
                    "Application closed with an unfinished exchange; staff review required.")
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self.hardware.close()
            super().close()


# Email queue and staff tools live here to keep deployment in existing files.
"""Durable manual GCash requests and retrying Gmail outbox (standard library)."""
import argparse
import json
import os
import smtplib
import sqlite3
import ssl
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

SENDER = 'frijmagpulong@gmail.com'
RECIPIENT = 'bagotsay.christianphillip2187@gmail.com'

_password_file = Path.home() / ".config" / "dmarby" / "smtp_password"

if not os.environ.get("DMARBY_SMTP_APP_PASSWORD"):
    try:
        os.environ["DMARBY_SMTP_APP_PASSWORD"] = (
            _password_file.read_text(encoding="utf-8").strip()
        )
    except FileNotFoundError:
        pass

def configured():
    return bool(os.environ.get('DMARBY_SMTP_APP_PASSWORD', '').strip())

def format_admin_email(subject, body):
    """Render stored payloads at send time, including previously queued JSON."""
    from html import escape
    from datetime import timezone, timedelta
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        return subject, body, None

    def clean(value):
        return str(value if value is not None else "Not recorded")

    def peso(value):
        try:
            return f"₱{float(value):,.2f}"
        except (ValueError, TypeError):
            return clean(value)

    def date_label(value):
        try:
            date = datetime.fromisoformat(str(value))
            if date.tzinfo is not None:
                date = date.astimezone(timezone(timedelta(hours=8)))
                return date.strftime("%d %B %Y, %I:%M:%S %p") + " (Philippine time)"
            return clean(value)
        except (ValueError, TypeError):
            return clean(value)

    is_demo = data.get("mode") == "demo"
    is_request = all(field in data for field in ("account_name", "number", "amount", "cash_received"))
    reference = clean(data.get("reference") or data.get("incident", {}).get("ref") or "Not recorded")
    if is_request:
        title = "Manual GCash cash-in request"
        status = "DEMO — DO NOT TRANSFER MONEY" if is_demo else "PENDING — MANUAL TRANSFER REQUIRED"
        lead = "This is a test request. Do not send money." if is_demo else "Please manually send the cash-in amount to the account below."
        rows = [
            ("Transaction reference", reference),
            ("GCash account name", clean(data.get("account_name"))),
            ("GCash mobile number", clean(data.get("number"))),
            ("Amount to send", peso(data.get("amount"))),
            ("Cash received by kiosk", peso(data.get("cash_received"))),
            ("Service fee retained", peso(data.get("fee"))),
            ("Requested at", date_label(data.get("time"))),
            ("Mode", "Demo / test" if is_demo else clean(data.get("mode", "hardware")).title()),
        ]
        action = ("Do not fulfill this demo request." if is_demo else
                  "Check that this reference has not already been processed. Transfer only the amount to send, excluding the service fee. After verifying the transfer, record completion and the actual GCash transaction reference.")
        note = "This email is a cash-in request, not proof of a completed GCash transfer."
        subject = f"[D’ MARBY]{' [DEMO]' if is_demo else ''} GCash request {reference} — {peso(data.get('amount'))}"
    else:
        title = "Kiosk alert"
        status = "DEMO ALERT — TEST ONLY" if is_demo else "STAFF ATTENTION REQUIRED"
        labels = {"timeout_empty": "Customer inactive — no cash inserted",
                  "timeout_money": "Customer inactive — cash remains in the kiosk",
                  "out_of_coins": "Insufficient coins", "wrong_bill": "Different bill inserted",
                  "gc_unavailable": "GCash service unavailable", "gc_balance": "Insufficient GCash float",
                  "gc_error": "GCash request error", "incident": "Transaction needs staff review"}
        alert = clean(data.get("alert", "Kiosk alert"))
        description = labels.get(alert, alert.replace("_", " "))
        lead = "Please review the kiosk and any recorded customer balance."
        rows = [("Alert", description), ("Transaction reference", reference),
                ("Cash received / recorded credit", peso(data.get("balance_received", 0))),
                ("Reported at", date_label(data.get("time"))),
                ("Mode", "Demo / test" if is_demo else clean(data.get("mode", "hardware")).title())]
        incident = data.get("incident")
        if isinstance(incident, dict):
            names = {"kind": "Incident type", "amount": "Balance owed to customer", "detail": "Details",
                     "bill": "Bill received", "picked_bill": "Bill selected", "excess": "Excess payment"}
            for field, label in names.items():
                if incident.get(field) is not None:
                    value = peso(incident[field]) if field in ("amount", "bill", "picked_bill", "excess") else clean(incident[field])
                    rows.append((label, value))
        action = "Review the transaction journal and physically verify cash before resolving the incident. Returning home does not settle an outstanding balance."
        note = "Use the transaction reference to avoid processing the same incident twice."
        subject = f"[D’ MARBY]{' [DEMO]' if is_demo else ''} Alert — {description} — Ref {reference}"

    subject = subject.replace("\r", " ").replace("\n", " ")
    plain = title + "\n" + status + "\n\n" + lead + "\n\n"
    plain += "\n".join(label + ": " + value for label, value in rows)
    plain += "\n\nAdmin action\n" + action + "\n\n" + note + "\n"
    colour = "#92400e" if is_demo else "#1d3fbf"
    rendered_rows = "".join(
        '<tr><th style="padding:12px 16px;text-align:left;border-bottom:1px solid #e3e8f2;color:#5b6b8c;font-weight:normal;width:43%">'
        + escape(label) + '</th><td style="padding:12px 16px;border-bottom:1px solid #e3e8f2;color:#0f1b4d;'
        + ('font-size:22px;font-weight:bold;' if label == 'Amount to send' else 'font-size:16px;')
        + '">' + escape(value) + '</td></tr>' for label, value in rows)
    html = ('<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#eef2fb;font-family:Arial,sans-serif;line-height:1.5">'
            '<div style="max-width:620px;margin:24px auto;background:#ffffff;border-radius:12px;overflow:hidden">'
            '<div style="padding:24px;background:#0f1b4d;color:white"><div style="font-size:14px;letter-spacing:2px">D’ MARBY</div>'
            '<h1 style="font-size:24px;margin:8px 0 0">' + escape(title) + '</h1></div>'
            '<div style="padding:24px"><p style="font-size:14px;font-weight:bold;color:' + colour + '">' + escape(status) + '</p>'
            '<p>' + escape(lead) + '</p><table style="width:100%;border-collapse:collapse;background:#f8fafc">'
            + rendered_rows + '</table><h2 style="font-size:18px;margin-top:24px">Admin action</h2><p>' + escape(action) + '</p>'
            '<p style="font-size:13px;color:#5b6b8c;border-top:1px solid #e3e8f2;padding-top:16px">' + escape(note) + '</p>'
            '</div></div></body></html>')
    return subject, plain, html


class Notifications:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, subject TEXT NOT NULL, body TEXT NOT NULL,
                sent INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS requests (
                ref TEXT PRIMARY KEY, details TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                operator TEXT, provider_ref TEXT, completed_at TEXT);''')
        # Import records if the earlier optional email module was already installed.
        # Existing transaction-number tables are kept intact.
        legacy = Path(self.path).with_name('notifications.sqlite3')
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS notification_migrations (name TEXT PRIMARY KEY)')
            migrated = db.execute("SELECT 1 FROM notification_migrations WHERE name='legacy_outbox'").fetchone()
            if legacy.exists() and legacy.resolve() != Path(self.path).resolve() and not migrated:
                with sqlite3.connect(legacy.resolve().as_uri()+'?mode=ro', uri=True) as old:
                    for table, columns in (
                        ('outbox', 'id,subject,body,sent,attempts,next_try'),
                        ('requests', 'ref,details,status,operator,provider_ref,completed_at')):
                        rows = old.execute('SELECT '+columns+' FROM '+table).fetchall()
                        db.executemany('INSERT OR IGNORE INTO '+table+'('+columns+') VALUES(?,?,?,?,?,?)', rows)
                db.execute("INSERT INTO notification_migrations(name) VALUES('legacy_outbox')")
        os.chmod(self.path, 0o600)
        self.stop = threading.Event()
        self.thread = None

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def enqueue(self, key, subject, details):
        body = json.dumps(details, ensure_ascii=False, indent=2)
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO outbox(id,subject,body) VALUES(?,?,?)',
                       (key, '[D’ MARBY] '+subject, body))

    def request(self, details):
        ref = details['reference']
        if (not details.get('account_name', '').strip() or
                len(details.get('number', '')) != 11 or not details['number'].startswith('09') or
                not 0 < details['amount'] < 100 or
                details['cash_received'] != details['amount'] + details['fee']):
            raise ValueError('Invalid or unpaid GCash request')
        with self.connect() as db:
            previous = db.execute('SELECT details FROM requests WHERE ref=?', (ref,)).fetchone()
            if previous:
                existing = json.loads(previous[0])
                details = dict(details, time=existing.get('time')) if 'time' in existing else details
                if existing != details:
                    raise ValueError('Reference already belongs to a different request')
            body = json.dumps(details, ensure_ascii=False, indent=2)
            db.execute('INSERT OR IGNORE INTO requests(ref,details) VALUES(?,?)', (ref,body))
            db.execute('INSERT OR IGNORE INTO outbox(id,subject,body) VALUES(?,?,?)',
                       ('gcash:'+ref, '[D’ MARBY] Manual GCash cash-in request '+ref, body))

    def get_request(self, ref):
        with self.connect() as db:
            row = db.execute('SELECT details FROM requests WHERE ref=?', (ref,)).fetchone()
        return json.loads(row[0]) if row else None

    def has_request(self, ref):
        with self.connect() as db:
            return bool(db.execute('SELECT 1 FROM requests WHERE ref=?', (ref,)).fetchone())

    def deliver_one(self):
        if not configured():
            return False
        with self.connect() as db:
            row = db.execute('SELECT id,subject,body FROM outbox WHERE sent=0 AND next_try<=? ORDER BY rowid LIMIT 1', (time.time(),)).fetchone()
        if not row:
            return False
        key, subject, body = row
        subject, plain_body, html_body = format_admin_email(subject, body)
        message = EmailMessage()
        message['From'], message['To'], message['Subject'] = SENDER, RECIPIENT, subject
        import hashlib
        message['Message-ID'] = '<'+hashlib.sha256(key.encode()).hexdigest()+'@dmarby.local>'
        message.set_content(plain_body)
        if html_body:
            message.add_alternative(html_body, subtype="html")
        try:
            with smtplib.SMTP_SSL('smtp.gmail.com',465,timeout=15,context=ssl.create_default_context()) as smtp:
                smtp.login(SENDER, os.environ['DMARBY_SMTP_APP_PASSWORD'].replace(' ',''))
                smtp.send_message(message)
        except Exception:
            # Never log credentials, customer details or SMTP exception payloads.
            with self.connect() as db:
                db.execute('UPDATE outbox SET attempts=attempts+1,next_try=? WHERE id=?', (time.time()+60,key))
            return False
        with self.connect() as db:
            db.execute('UPDATE outbox SET sent=1,attempts=attempts+1 WHERE id=?', (key,))
        return True

    def start(self):
        if self.thread:
            return
        def run():
            while not self.stop.is_set():
                try:
                    sent = self.deliver_one()
                except Exception:
                    sent = False
                self.stop.wait(1 if sent else 10)
        self.thread = threading.Thread(target=run,daemon=True,name='marby-email')
        self.thread.start()


def now():
    return datetime.now(ZoneInfo('Asia/Manila')).isoformat(timespec='seconds')


def notification_main():
    parser=argparse.ArgumentParser(description='Local staff tool; never transfers GCash automatically.')
    parser.add_argument('--db',default=str(Path(__file__).with_name('transaction_numbers.sqlite3')))
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('pending')
    done=sub.add_parser('complete')
    done.add_argument('--ref',required=True); done.add_argument('--operator',required=True)
    done.add_argument('--gcash-reference',required=True)
    sub.add_parser('send-pending')
    a=parser.parse_args(); store=Notifications(a.db)
    if a.command=='pending':
        with store.connect() as db:
            for row in db.execute("SELECT ref,details FROM requests WHERE status='pending'"):
                print(row[0],row[1])
    elif a.command=='complete':
        if input('After verifying the real transfer, type COMPLETED: ').strip()!='COMPLETED':
            raise SystemExit('No change made.')
        with store.connect() as db:
            result=db.execute("UPDATE requests SET status='completed',operator=?,provider_ref=?,completed_at=? WHERE ref=? AND status='pending'",(a.operator,a.gcash_reference,now(),a.ref))
            if result.rowcount!=1:
                raise SystemExit('Pending reference not found; no change made.')
        print('Recorded manual completion; no funds transferred by this command.')
    else:
        if not configured():
            raise SystemExit('Set DMARBY_SMTP_APP_PASSWORD first.')
        while store.deliver_one():
            pass
        with store.connect() as db:
            count=db.execute('SELECT count(*) FROM outbox WHERE sent=0').fetchone()[0]
        print('Remaining queued emails:',count)

if __name__=='__main__':
    notification_main()
