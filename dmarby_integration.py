"""Connect the D'MARBY screens to the calibrated Raspberry Pi backend.

All Tk work stays on the main thread. One worker handles sensor-confirmed
servo cycles; the Tk loop consumes its result before updating the journal.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from coin_changer_hardware import HardwareFault, RealHardware, payout_coins_for


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
        self._syncing = False
        self._deposit_open = None
        self._payout_future = None
        self._payout_coin = None
        self._verified_coin = None
        self.counting_cash = False
        self.hardware = RealHardware(cfg)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marby-payout")
        try:
            super().__init__(root)
            self._hw_ready = True
            self.stock = self._stock_from_backend()
            self.gcash_balance = 0  # A demo balance is never usable in hardware mode.
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

    def show(self, screen, overlay=None):
        if self._hw_ready:
            pending = self.hardware.input_pending()
            if pending and screen not in ("insert", "incident", "admin"):
                self.last_input = time.monotonic()
                return
            if screen == "welcome" and self.hardware.storage.unresolved:
                self._hardware_fault("An unfinished deposit cannot be cleared by returning home.")
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
                elif self.flow in ("break", "combine") and self.credit < self.target:
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
            self.hardware.storage.receive(source, value)
        except HardwareFault as exc:
            self.hardware.storage.state.setdefault("uncredited_inputs", []).append(
                {"source": source, "value": value, "time": time.time()})
            self._hardware_fault(f"Accepted additional cash: {source} PHP {value}. {exc}")
            return
        self.credit = self.hardware.storage.state["credit"]
        self.inserted = [(item["source"], item["value"])
                         for item in self.hardware.storage.state["received"]]
        self.last_input = time.monotonic()
        expected = "bill" if self.flow == "bill" else "coin"
        if (self.screen != "insert" or self.flow not in ("bill", "break", "combine")
                or source != expected):
            self._hardware_fault("Cash was accepted outside its deposit screen.")
            return
        self.notice = None
        if source == "bill" and value != self.target:
            self.wrong = (self.target, value)
            self.show("wrong_bill")
        elif self.credit > self.target:
            self._hardware_fault("Accepted coins exceed the selected total; staff must settle the balance.")
        else:
            self.show("insert")

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
                self._sync_acceptance()
        except Exception as exc:
            self._hardware_fault(str(exc))
        if not self.closed:
            self.jobs["hardware"] = self.root.after(50, self._hardware_tick)

    def start_dispense(self):
        if self.fault or self.hardware.storage.state.get("status") != "paid":
            return
        if self._payout_future is not None or self.screen != "review":
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

    def _hardware_fault(self, detail):
        self.hardware.stopping.set()
        try:
            self.hardware.set_acceptance(None)
        except Exception:
            pass
        if getattr(self, "fault", None):
            return
        try:
            self.hardware.storage.fault(detail)
        except Exception as exc:
            detail = f"{detail}; journal write failed: {exc}"
        for name in ("drop", "check", "cash"):
            if name in self.jobs:
                self.root.after_cancel(self.jobs.pop(name))
        from uuid import uuid4
        info = {"kind": "hardware", "amount": self._owed(), "ref": "HW-" + uuid4().hex[:10].upper(),
                "bill": None, "detail": str(detail)}
        self.fault = self.incident_info = info
        self.log("hardware_fault", fault_kind=info["kind"], amount=info["amount"],
                 ref=info["ref"], detail=info["detail"])
        self.show("incident", None)

    def incident(self, kind, amount):
        self._hardware_fault(f"{kind}: recorded balance PHP {amount}")

    def payout_fault(self):
        self._hardware_fault("Payout was interrupted; inspect the delivered coins and journal.")

    def tick(self):
        if self._hw_ready:
            if self.hardware.input_pending():
                self.last_input = time.monotonic()
            if (self.overlay and self.overlay.get("kind") == "timeout_money"
                    and time.monotonic() >= self.overlay["deadline"]):
                self._hardware_fault("The customer timed out with cash in the machine. Balance remains owed.")
        super().tick()

    def start_gcash(self):
        self.hardware.set_acceptance(None)
        self.show("gc_unavailable")

    def start_cashing(self):
        self.hardware.set_acceptance(None)
        if self.hardware.storage.unresolved:
            self._hardware_fault("GCash transfer is unavailable; accepted cash requires staff settlement.")
        else:
            self.show("gc_unavailable")

    def cashing_result(self, ok):
        return False  # Demo completion callbacks can never confirm a real transfer.

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
            if self.hardware.storage.unresolved or self.hardware.input_pending():
                self.hardware.storage.fault("Application closed with an unfinished exchange; staff review required.")
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self.hardware.close()
            super().close()
