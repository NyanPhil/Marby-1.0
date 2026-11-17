"""End-to-end GUI/model tests using simulated Tk, GPIO, time and coin exits.

No Raspberry Pi, acceptor, network payment service or motor is operated.
Run: python -m unittest -v test_dmarby_integration.py
"""
import time
import copy
import io
import json
import tempfile
import types
import unittest
from collections import Counter
from concurrent.futures import Future
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import dmarby
from coin_changer_hardware import HardwareFault, Storage, atomic_json
from dmarby_integration import KioskHardwareMixin, load_config


class Root:
    def __init__(self):
        self.callbacks = {}
        self.sequence = 0

    def after(self, delay, callback):
        self.sequence += 1
        key = str(self.sequence)
        self.callbacks[key] = callback
        return key

    def after_cancel(self, key):
        self.callbacks.pop(key, None)

    def destroy(self):
        self.callbacks.clear()

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class Canvas:
    def __init__(self, *args, **kwargs):
        self.sequence = 0

    def __getattr__(self, name):
        def operation(*args, **kwargs):
            self.sequence += 1
            return self.sequence
        return operation


class Font:
    def __init__(self, *args, **kwargs):
        pass

    def measure(self, text):
        return len(text) * 8


class Input:
    def __init__(self, pin, **kwargs):
        self.is_active = False
        self.when_activated = self.when_deactivated = None

    def close(self):
        pass


class Output:
    def __init__(self, pin, **kwargs):
        self.enabled = False

    def on(self):
        self.enabled = True

    def off(self):
        self.enabled = False

    close = off


class Servo:
    def __init__(self, kit, channel):
        self.kit, self.channel = kit, channel
        self.history = []

    @property
    def angle(self):
        return self.history[-1] if self.history else None

    @angle.setter
    def angle(self, value):
        self.history.append(value)
        if value in (65, 115) and self.kit.on_release:
            self.kit.on_release(self.channel)

    def set_pulse_width_range(self, minimum, maximum):
        self.pulse_limits = (minimum, maximum)


class Kit:
    def __init__(self, **kwargs):
        self.on_release = None
        self.servo = [Servo(self, channel) for channel in range(16)]


class ImmediateExecutor:
    def __init__(self, **kwargs):
        pass

    def submit(self, function, *args):
        future = Future()
        try:
            future.set_result(function(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, **kwargs):
        pass


class Clock:
    now = 100.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Connected(KioskHardwareMixin, dmarby.Kiosk):
    pass


class FontPortabilityTests(unittest.TestCase):
    def test_system_fonts_work_without_plus_jakarta_sans(self):
        root = object()
        for installed, expected in (
            (("Segoe UI", "Arial"), "Segoe UI"),
            (("DejaVu Sans",), "DejaVu Sans"),
            (("Helvetica",), "Helvetica"),
            (("ARIAL",), "ARIAL"),
            (("Plus Jakarta Sans", "Segoe UI"), "Plus Jakarta Sans"),
        ):
            with self.subTest(installed=installed), patch.object(
                    dmarby.tkfont, "families", return_value=installed):
                self.assertEqual(dmarby.choose_font_family(root), expected)

    def test_unrecognized_or_empty_font_inventory_uses_tk_default(self):
        root = object()
        default = types.SimpleNamespace(actual=lambda key: "Platform Default")
        for installed in ((), ("An unfamiliar system family",)):
            with self.subTest(installed=installed), patch.object(
                    dmarby.tkfont, "families", return_value=installed), patch.object(
                    dmarby.tkfont, "nametofont", return_value=default) as named:
                self.assertEqual(dmarby.choose_font_family(root), "Platform Default")
                named.assert_called_once_with("TkDefaultFont", root=root)

    def test_kiosk_starts_silently_without_optional_fonts(self):
        errors = io.StringIO()
        with patch.object(dmarby, "FULLSCREEN", False), patch.object(
                dmarby.tk, "PhotoImage", lambda **kwargs: object()), patch.object(
                dmarby.tk, "Canvas", Canvas), patch.object(
                dmarby.tkfont, "families", return_value=("DejaVu Sans",)), patch.object(
                dmarby.tkfont, "Font", Font), redirect_stderr(errors):
            app = dmarby.Kiosk(Root())
            try:
                self.assertEqual(app.font_family, "DejaVu Sans")
                self.assertEqual(app.screen, "welcome")
            finally:
                app.close()
        self.assertEqual(errors.getvalue(), "")


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.cfg = json.loads(Path(__file__).with_name("coin_changer_config.json").read_text())
        self.cfg["inventory_file"] = str(self.folder / "inventory.json")
        self.cfg["journal_file"] = str(self.folder / "journal.json")
        # Simulation-only fixtures; never save these flags to deployment config.
        self.cfg["inhibit_verified"] = {"bill": True, "coin": True}
        self.cfg["servo_verified"] = {str(c): True for c in (1, 5, 10, 20)}
        self.cfg["coin_exit_sensor"] = True   # most tests simulate the sensor
        atomic_json(self.cfg["inventory_file"], {str(c): 100 for c in (1, 5, 10, 20)})
        self.clock = Clock()
        self.addCleanup(patch.stopall)
        patch.dict("sys.modules", {
            "gpiozero": types.SimpleNamespace(DigitalInputDevice=Input, OutputDevice=Output),
            "adafruit_servokit": types.SimpleNamespace(ServoKit=Kit),
        }).start()
        patch("coin_changer_hardware.time.monotonic", self.clock.monotonic).start()
        patch("coin_changer_hardware.time.sleep", self.clock.advance).start()
        patch("dmarby_integration.ThreadPoolExecutor", ImmediateExecutor).start()
        patch.object(dmarby, "DEMO_MODE", False).start()
        patch.object(dmarby, "FULLSCREEN", False).start()
        patch.object(dmarby, "LOG_FILE", str(self.folder / "events.log")).start()
        patch.object(dmarby.tk, "PhotoImage", lambda **kwargs: object()).start()
        patch.object(dmarby.tk, "Canvas", Canvas).start()
        patch.object(dmarby.tkfont, "families", lambda root: (dmarby.FONT_NAME,)).start()
        patch.object(dmarby.tkfont, "Font", Font).start()
        self.app = Connected(Root(), self.cfg)
        self.addCleanup(self.app.close)
        self.app.hardware.kit.on_release = lambda channel: self.app.hardware._on_drop()

    def bill(self, target=50):
        self.app.pick_bill(target)
        self.app.mix = Counter({20: target // 20, 10: (target % 20) // 10})
        self.app.to_insert()

    def train(self, source, count):
        inp = self.app.hardware.inputs[source]
        for _ in range(count):
            inp.is_active = True
            inp.when_activated()
            self.app._hardware_tick()
            self.clock.advance(.05)
            inp.is_active = False
            inp.when_deactivated()
            self.clock.advance(.95)
            self.app._hardware_tick()
        self.clock.advance(3.1 if source == "bill" else 2.1)
        self.app._hardware_tick()

    def pay(self):
        self.app.to_review_from_insert()
        self.app.start_dispense()
        for _ in range(110):
            self.app._hardware_tick()
            if self.app.fault or self.app.dropped == len(self.app.queue):
                break
        if not self.app.fault:
            self.app.finish_dispense()

    def test_fifty_peso_bill_to_selected_coins(self):
        self.bill()
        self.assertEqual(self.app.hardware.accepting, "bill")
        self.train("bill", 5)
        self.assertEqual(self.app.credit, 50)
        self.pay()
        self.assertIsNone(self.app.fault)
        self.assertEqual(self.app.screen, "done")
        self.assertEqual(self.app.hardware.storage.state["status"], "completed")
        self.assertEqual(self.app.stock[20], 98)
        self.assertEqual(self.app.stock[10], 99)
        self.assertEqual(self.app.hardware.storage.inventory["20"], 98)

    def test_hundred_peso_bill_completes(self):
        self.bill(100)
        self.train("bill", 10)
        self.pay()
        self.assertEqual(self.app.screen, "done")
        self.assertEqual(self.app.stock[20], 95)

    def test_coin_break_accumulates_and_pays(self):
        self.app.pick_coin(10)
        self.app.mix = Counter({5: 1, 1: 5})
        self.app.to_insert()
        self.train("coin", 5)
        self.assertEqual(self.app.credit, 5)
        self.assertEqual(self.app.hardware.accepting, "coin")
        self.train("coin", 5)
        self.assertIsNone(self.app.hardware.accepting)
        self.pay()
        self.assertEqual(self.app.screen, "done")
        self.assertEqual(self.app.stock[1], 95)

    def test_coin_combine_uses_actual_frontend_plan(self):
        self.app.have = Counter({1: 10})
        self.app.show("have_coins")
        self.app.to_combine_choice()
        self.app.start_combine_insert()
        for _ in range(10):
            self.train("coin", 1)
        self.pay()
        self.assertEqual(self.app.plan, {10: 1})
        self.assertEqual(self.app.screen, "done")

    def test_wrong_supported_bill_can_rebuild_mix(self):
        self.bill(100)
        self.train("bill", 5)
        self.assertEqual(self.app.screen, "wrong_bill")
        self.app.reroute_bill()
        self.assertEqual(self.app.target, 50)
        self.app.mix = Counter({20: 2, 10: 1})
        self.app.to_review()
        self.app.start_dispense()
        for _ in range(4):
            self.app._hardware_tick()
        self.app.finish_dispense()
        self.assertEqual(self.app.screen, "done")

    def test_excess_coin_is_recorded_not_claimed_returned(self):
        self.app.pick_coin(5)
        self.app.mix = Counter({1: 5})
        self.app.to_insert()
        self.train("coin", 10)
        self.assertEqual(self.app.screen, "incident")
        self.assertEqual(self.app.hardware.storage.state["credit"], 10)
        self.assertEqual(self.app.incident_info["amount"], 10)

    def test_ambiguous_twenty_one_bill_group_persists_fault(self):
        self.bill(100)
        self.train("bill", 21)
        self.assertEqual(self.app.screen, "incident")
        self.assertEqual(self.app.hardware.storage.state["status"], "fault")
        self.assertIn("21 pulses", self.app.hardware.storage.state["fault"])

    def test_home_is_blocked_while_counting(self):
        self.bill()
        inp = self.app.hardware.inputs["bill"]
        inp.is_active = True
        inp.when_activated()
        self.app.show("welcome")
        self.assertEqual(self.app.screen, "insert")
        self.assertEqual(self.app.target, 50)

    def test_timeout_preserves_customer_credit(self):
        self.bill()
        self.train("bill", 5)
        self.app.overlay = {"kind": "timeout_money", "deadline": time.monotonic() - 1}
        self.app.tick()
        self.assertEqual(self.app.screen, "incident")
        self.assertEqual(self.app.hardware.storage.state["credit"], 50)
        self.assertEqual(self.app.hardware.storage.state["status"], "fault")
        self.assertNotIn('"kind": "forfeit"', (self.folder / "events.log").read_text())

    def test_missing_coin_exit_stops_without_inventory_deduction(self):
        self.bill()
        self.train("bill", 5)
        self.app.hardware.kit.on_release = None
        self.pay()
        self.assertEqual(self.app.screen, "incident")
        self.assertEqual(self.app.stock[20], 100)
        self.assertEqual(self.app.hardware.storage.state["dispensed"], {})

    def test_fake_ui_credit_and_drop_hooks_cannot_pay(self):
        self.assertFalse(self.app.bill_inserted(100))
        self.assertFalse(self.app.coin_inserted(20))
        self.assertFalse(self.app.coin_dropped(20))
        self.assertEqual(self.app.credit, 0)

    def gcash(self):
        self.app.start_gcash()
        for digit in "09171234567":
            self.app.gc_number_digit(digit)
        self.app.gc_number_next()
        for ch in "JUAN DELA CRUZ":
            self.app.gc_name_type(ch)
        self.app.gc_name_next()

    def reference(self, ref):
        import sqlite3
        row = sqlite3.connect(self.folder / "transaction_numbers.sqlite3").execute(
            "SELECT kind, status, details FROM transactions WHERE number=?", (int(ref),)).fetchone()
        return row[0], row[1], json.loads(row[2])

    def adjust(self, adj):
        import threading
        box = {"done": threading.Event()}
        self.app.adjustment_queue.put((adj, box))
        self.app.drain_adjustments()
        return box["result"]

    def test_dashboard_refill_updates_hardware_inventory(self):
        result = self.adjust({"id": 1, "coin": 5, "kind": "refill", "amount": 40})
        self.assertEqual(result, ("applied", 100, 140, None))
        saved = json.loads(Path(self.cfg["inventory_file"]).read_text())
        self.assertEqual(saved["5"], 140)
        self.assertEqual(self.app.stock[5], 140)
        # The same request arriving again is not applied twice.
        self.assertEqual(self.adjust({"id": 1, "coin": 5, "kind": "refill", "amount": 40}), result)
        self.assertEqual(json.loads(Path(self.cfg["inventory_file"]).read_text())["5"], 140)

    def test_dashboard_count_sets_hardware_inventory(self):
        self.assertEqual(self.adjust({"id": 2, "coin": 20, "kind": "count", "amount": 37}),
                         ("applied", 100, 37, None))
        self.assertEqual(json.loads(Path(self.cfg["inventory_file"]).read_text())["20"], 37)

    def test_dashboard_refill_waits_while_cash_is_in_the_machine(self):
        self.bill()
        self.train("bill", 5)
        self.assertEqual(self.app.credit, 50)
        self.assertIsNone(self.adjust({"id": 3, "coin": 5, "kind": "refill", "amount": 40}))
        self.assertEqual(json.loads(Path(self.cfg["inventory_file"]).read_text())["5"], 100)

    def test_gcash_accepts_coins_only(self):
        self.gcash()
        self.assertEqual(self.app.screen, "insert")
        self.assertEqual(self.app.hardware.accepting, "coin")
        self.assertFalse(self.app.cashing_result(True))
        self.assertNotEqual(self.app.screen, "gc_success")

    def test_gcash_cash_in_closes_journal_and_saves_pending_request(self):
        self.gcash()
        self.train("coin", 20)
        self.train("coin", 10)
        self.assertEqual(self.app.credit, 30)
        self.app.start_cashing()
        self.assertEqual(self.app.screen, "gc_success")
        self.assertEqual((self.app.gc_amount, self.app.gc_fee), (29, 1))
        state = self.app.hardware.storage.state
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["outcome"], "gcash_pending")
        self.assertEqual(state["dispensed"], {})
        self.assertFalse(self.app.hardware.storage.unresolved)
        self.assertIsNone(self.app.hardware.accepting)
        kind, status, details = self.reference(self.app.gc_ref)
        self.assertEqual((kind, status), ("gcash", "pending"))
        self.assertEqual((details["number"], details["name"], details["amount"]),
                         ("09171234567", "JUAN DELA CRUZ", 29))
        self.app.show("welcome")
        self.bill()
        self.assertEqual(self.app.hardware.accepting, "bill")

    def test_gcash_cancel_returns_the_same_coins(self):
        self.gcash()
        self.train("coin", 10)
        self.train("coin", 10)
        before = self.app.hardware.storage.inventory["10"]
        self.app.ask_cancel()
        self.app.cancel_gcash()
        self.assertEqual(self.app.screen, "dispensing")
        for _ in range(110):
            self.app._hardware_tick()
            if self.app.fault or self.app.dropped == len(self.app.queue):
                break
        self.app.finish_dispense()
        self.assertEqual(self.app.screen, "done")
        self.assertEqual(self.app.hardware.storage.state["dispensed"], {"10": 2})
        self.assertEqual(self.app.hardware.storage.inventory["10"], before - 2)
        self.assertEqual(self.reference(self.app.transaction_ref)[1], "refunded")

    def test_gcash_cannot_cash_in_coins_already_in_another_state(self):
        self.gcash()
        self.train("coin", 5)
        self.app.hardware.storage.fault("simulated jam")
        self.assertFalse(self.app.start_cashing())
        self.assertNotEqual(self.app.screen, "gc_success")

    def test_demo_admin_controls_cannot_clear_hardware_journal(self):
        self.bill()
        self.train("bill", 5)
        stock = dict(self.app.stock)
        self.assertFalse(self.app.admin_stock("refill"))
        self.assertFalse(self.app.admin_fix())
        self.assertEqual(self.app.stock, stock)
        self.assertEqual(self.app.hardware.storage.state["credit"], 50)

    def test_close_with_paid_cash_keeps_recovery_record(self):
        self.bill()
        self.train("bill", 5)
        self.app.close()
        self.assertEqual(self.app.hardware.storage.state["status"], "fault")
        self.assertEqual(self.app.hardware.storage.state["credit"], 50)
        # Restarting shows the saved receipt once, then the kiosk keeps going.
        self.app = Connected(Root(), self.cfg)
        self.addCleanup(self.app.close)
        self.app.hardware.kit.on_release = lambda channel: self.app.hardware._on_drop()
        self.assertEqual(self.app.screen, "incident")
        self.assertEqual(self.app.incident_info["amount"], 50)
        self.app.exit_incident()
        self.assert_kiosk_keeps_going(parked_credit=50)

    # -- The kiosk keeps going after any failure --------------------------------

    def parked(self):
        path = self.folder / "parked_incidents.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_kiosk_keeps_going(self, parked_credit=None):
        """After leaving a failure: recorded, parked, and a full exchange works."""
        self.assertEqual(self.app.screen, "welcome")
        self.assertIsNone(self.app.fault)
        self.assertEqual(self.app.hardware.storage.state["status"], "parked")
        self.assertFalse(self.app.hardware.stopping.is_set())
        if parked_credit is not None:
            self.assertEqual(self.parked()[-1]["credit"], parked_credit)
        self.app.show("exchange_type")
        self.assertEqual(self.app.screen, "exchange_type")
        self.bill()
        self.assertEqual(self.app.hardware.accepting, "bill")
        self.train("bill", 5)
        self.assertEqual(self.app.credit, 50)
        self.pay()
        self.assertEqual(self.app.screen, "done")
        self.assertEqual(self.app.hardware.storage.state["status"], "completed")

    def test_clockwise_servos_are_mirrored(self):
        from coin_changer_hardware import servo_angles
        for coin, expected in (("1", (0, 65)), ("10", (0, 65)), ("5", (180, 115)), ("20", (180, 115))):
            self.assertEqual(servo_angles(self.cfg, self.cfg["servos"][coin]), expected, coin)
        flipped = dict(self.cfg, increasing_angle_turns_clockwise=True)
        self.assertEqual(servo_angles(flipped, self.cfg["servos"]["20"]), (0, 65))
        self.assertEqual(servo_angles(flipped, self.cfg["servos"]["1"]), (180, 115))
        with self.assertRaises(HardwareFault):
            servo_angles(self.cfg, dict(self.cfg["servos"]["1"], direction="sideways"))

    def test_hundred_peso_payout_moves_clockwise_twenty_servo_mirrored(self):
        self.bill(100)
        self.train("bill", 10)
        self.pay()
        self.assertEqual(self.app.screen, "done")
        history = self.app.hardware.kit.servo[0].history       # PHP 20, clockwise
        self.assertIn(115, history)
        self.assertNotIn(65, history)
        self.assertIsNone(history[-1])

    def without_sensor(self):
        self.app.close()
        self.cfg["coin_exit_sensor"] = False
        self.app = Connected(Root(), self.cfg)
        self.addCleanup(self.app.close)
        self.app.hardware.kit.on_release = None     # nothing to detect coins

    def test_without_coin_exit_sensor_each_servo_cycle_pays_one_coin(self):
        self.without_sensor()
        self.assertIsNone(self.app.hardware.drop_sensor)
        self.bill()                                  # PHP 50 -> 2 x 20 + 1 x 10
        self.train("bill", 5)
        self.pay()
        self.assertEqual(self.app.screen, "done")
        saved = json.loads(Path(self.cfg["inventory_file"]).read_text())
        self.assertEqual((saved["20"], saved["10"]), (98, 99))
        self.assertEqual(self.app.hardware.storage.state["status"], "completed")

    def test_without_coin_exit_sensor_the_pin_is_not_required(self):
        self.app.close()
        cfg = dict(self.cfg, coin_exit_sensor=False)
        cfg.pop("drop_sensor_pin")
        app = Connected(Root(), cfg)
        self.addCleanup(app.close)
        self.assertIsNone(app.hardware.drop_sensor)

    def test_servos_release_after_paying_out(self):
        self.bill()
        self.train("bill", 5)
        self.pay()
        self.assertEqual(self.app.screen, "done")
        for coin in (1, 5, 10, 20):
            channel = self.cfg["servos"][str(coin)]["channel"]
            self.assertIsNone(self.app.hardware.kit.servo[channel].angle)

    def test_stray_coin_pulse_is_recorded_then_kiosk_keeps_going(self):
        self.app.show("welcome")
        self.train("coin", 1)                       # nobody asked for a coin
        self.assertEqual(self.app.screen, "incident")
        self.assertIn("Unexpected coin pulse", self.app.hardware.storage.state["fault"])
        self.app.exit_incident()
        self.assertIn("Unexpected coin pulse", self.parked()[-1]["fault"])
        self.assert_kiosk_keeps_going()

    def test_jam_is_recorded_then_kiosk_keeps_going(self):
        self.bill()
        self.train("bill", 5)
        self.app.hardware.kit.on_release = None     # coin never reaches the exit sensor
        self.pay()
        self.assertEqual(self.app.screen, "incident")
        ref = self.app.incident_info["ref"]
        self.app.exit_incident()
        self.assertEqual(self.parked()[-1]["reference"], ref)
        import sqlite3
        row = sqlite3.connect(self.folder / "transaction_numbers.sqlite3").execute(
            "SELECT status FROM transactions WHERE number=?", (int(ref),)).fetchone()
        self.assertEqual(row[0], "incident")          # still on record for the dashboard
        self.app.hardware.kit.on_release = lambda channel: self.app.hardware._on_drop()
        self.assert_kiosk_keeps_going(parked_credit=50)

    def test_incident_screen_returns_home_by_itself(self):
        self.app.show("welcome")
        self.train("coin", 1)
        self.assertEqual(self.app.screen, "incident")
        self.app.screen_since -= dmarby.AUTO_HOME_S + 1
        self.app.tick()
        self.assert_kiosk_keeps_going()

    def test_leaving_with_money_inside_records_the_balance(self):
        self.bill()
        self.train("bill", 5)
        self.app.show("welcome")                    # customer walks off mid-exchange
        self.assertEqual(self.app.screen, "incident")
        self.assertEqual(self.app.incident_info["amount"], 50)
        self.app.exit_incident()
        self.assert_kiosk_keeps_going(parked_credit=50)

    def test_new_exchange_after_committed_payout(self):
        self.bill()
        self.train("bill", 5)
        self.pay()
        self.app.show("welcome")
        self.assertEqual(self.app.credit, 0)
        self.bill(100)
        self.assertEqual(self.app.hardware.accepting, "bill")

    def test_calibrated_channels_and_pulse_limits_are_applied(self):
        self.bill()
        self.train("bill", 5)
        self.pay()
        kit = self.app.hardware.kit
        servos = self.cfg["servos"]
        twenty, ten = servos["20"]["channel"], servos["10"]["channel"]
        self.assertIn(115, kit.servo[twenty].history)   # PHP 20, clockwise (mirrored)
        self.assertIn(65, kit.servo[ten].history)       # PHP 10, counterclockwise
        self.assertEqual(kit.servo[twenty].pulse_limits, (1000, 2000))
        unused = set(range(16)) - {int(v["channel"]) for v in servos.values()}
        self.assertTrue(all(kit.servo[ch].history == [] for ch in unused))

    def test_negative_payout_counts_are_rejected(self):
        self.bill()
        self.train("bill", 5)
        with self.assertRaises(HardwareFault):
            self.app.hardware.storage.start({20: 3, 10: -1})

    def test_load_config_resolves_storage_relative_to_config(self):
        config = self.folder / "machine.json"
        data = copy.deepcopy(self.cfg)
        data["inventory_file"] = "coins.json"
        config.write_text(json.dumps(data))
        self.assertEqual(load_config(config)["inventory_file"], str(self.folder / "coins.json"))

    def test_duplicate_confirmation_cannot_start_another_payout(self):
        self.bill()
        self.train("bill", 5)
        self.app.to_review_from_insert()
        self.app.start_dispense()
        history = list(self.app.hardware.kit.servo[14].history)
        self.app.start_dispense()
        self.assertEqual(self.app.hardware.kit.servo[14].history, history)

    def test_two_coin_exits_for_one_servo_cycle_stop_payout(self):
        self.bill()
        self.train("bill", 5)
        def double_drop(channel):
            self.app.hardware._on_drop()
            self.clock.advance(.1)
            self.app.hardware._on_drop()
        self.app.hardware.kit.on_release = double_drop
        self.pay()
        self.assertEqual(self.app.screen, "incident")
        self.assertEqual(self.app.hardware.storage.state["dispensed"], {})
        self.assertEqual(self.app.stock[20], 100)

    def test_actual_worker_does_not_block_the_frontend(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.bill()
        self.train("bill", 5)
        started, release = threading.Event(), threading.Event()
        worker_ids = []
        def slow_cycle(coin):
            worker_ids.append(threading.get_ident())
            started.set()
            if not release.wait(1):
                raise RuntimeError("Test worker was not released")
        pool = ThreadPoolExecutor(max_workers=1)
        self.app._executor = pool
        self.app.hardware.dispense_one = slow_cycle
        try:
            self.app.to_review_from_insert()
            self.app.start_dispense()
            self.assertTrue(started.wait(.5))
            self.assertFalse(self.app._payout_future.done())
            self.app.redraw()
            self.assertEqual(self.app.screen, "dispensing")
            self.assertNotEqual(worker_ids[0], threading.get_ident())
        finally:
            release.set()
            self.app.close()
            pool.shutdown(wait=True, cancel_futures=True)

    def test_service_reconciles_the_custom_config_journal(self):
        import subprocess
        import sys
        config = self.folder / "machine.json"
        config.write_text(json.dumps(self.cfg))
        self.bill()
        self.train("bill", 5)
        self.app.close()
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("marby_service.py")),
             "--config", str(config), "--operator", "Test operator",
             "--reason", "Simulated cash counted and settled"],
            input="RECONCILED\n", text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(Path(self.cfg["journal_file"]).read_text())
        self.assertEqual(state["status"], "reconciled")
        self.assertTrue((self.folder / "reconciliations.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
