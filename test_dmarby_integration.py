"""End-to-end GUI/model tests using simulated Tk, GPIO, time and coin exits.

No Raspberry Pi, acceptor, network payment service or motor is operated.
Run: python -m unittest -v test_dmarby_integration.py
"""
import copy
import io
import json
import struct
import tempfile
import types
import unittest
import zlib
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
        if value == 65 and self.kit.on_release:
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


class SmoothCurveTests(unittest.TestCase):
    BLUE = (29, 63, 191)

    def pixels(self, fill=None, outline=None, stroke=0, start=None, extent=None, corner=None):
        data = dmarby.smooth_shape_png(24, 24, 12, 12, 9, 9,
                                     fill, outline, stroke, start, extent, corner)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        compressed = bytearray()
        offset = 8
        while offset < len(data):
            size = struct.unpack(">I", data[offset:offset + 4])[0]
            kind = data[offset + 4:offset + 8]
            payload = data[offset + 8:offset + 8 + size]
            checksum = struct.unpack(">I", data[offset + 8 + size:offset + 12 + size])[0]
            self.assertEqual(checksum, zlib.crc32(kind + payload) & 0xFFFFFFFF)
            if kind == b"IDAT":
                compressed.extend(payload)
            offset += size + 12
        rows = zlib.decompress(compressed)
        pixels = b"".join(rows[y * 97 + 1:(y + 1) * 97] for y in range(24))
        return pixels, lambda x, y: tuple(pixels[(y * 24 + x) * 4:(y * 24 + x + 1) * 4])

    def test_circle_has_transparent_corners_and_smooth_coloured_edges(self):
        pixels, at = self.pixels(fill=self.BLUE)
        self.assertEqual(at(12, 12), (*self.BLUE, 255))
        self.assertEqual(at(0, 0), (0, 0, 0, 0))
        self.assertTrue(any(0 < alpha < 255 for alpha in pixels[3::4]))
        for offset in range(0, len(pixels), 4):
            if pixels[offset + 3]:
                self.assertEqual(tuple(pixels[offset:offset + 3]), self.BLUE)

    def test_outline_keeps_ring_hollow_and_supports_a_filled_centre(self):
        pixels, at = self.pixels(outline=self.BLUE, stroke=3)
        self.assertEqual(at(12, 12)[3], 0)
        self.assertGreater(at(12, 3)[3], 240)
        self.assertTrue(any(0 < alpha < 255 for alpha in pixels[3::4]))
        _, filled = self.pixels(fill=self.BLUE, outline=(255, 255, 255), stroke=4)
        self.assertEqual(filled(12, 12), (*self.BLUE, 255))
        self.assertEqual(filled(12, 3), (255, 255, 255, 255))

    def test_arc_preserves_clockwise_and_counterclockwise_progress(self):
        _, clockwise = self.pixels(outline=self.BLUE, stroke=3, start=90, extent=-90)
        _, counterclockwise = self.pixels(outline=self.BLUE, stroke=3, start=90, extent=90)
        self.assertGreater(clockwise(18, 6)[3], 240)
        self.assertEqual(clockwise(5, 5)[3], 0)
        self.assertGreater(counterclockwise(5, 5)[3], 240)
        self.assertEqual(counterclockwise(18, 6)[3], 0)
        self.assertEqual(clockwise(12, 12)[3], 0)
        self.assertEqual(clockwise(12, 21)[3], 0)

    def test_empty_arc_is_transparent(self):
        pixels, _ = self.pixels(outline=self.BLUE, stroke=3, start=90, extent=0)
        self.assertFalse(any(pixels[3::4]))

    def test_rounded_box_keeps_its_middle_solid_and_corners_transparent(self):
        pixels, at = self.pixels(fill=self.BLUE, corner=5)
        self.assertEqual(at(12, 12), (*self.BLUE, 255))
        self.assertEqual(at(12, 3), (*self.BLUE, 255))
        self.assertEqual(at(3, 3)[3], 0)
        self.assertTrue(any(0 < alpha < 255 for alpha in pixels[3::4]))

    def app(self):
        self.addCleanup(patch.stopall)
        patch.object(dmarby, "FULLSCREEN", False).start()
        patch.object(dmarby.tk, "PhotoImage", lambda **kwargs: object()).start()
        patch.object(dmarby.tk, "Canvas", Canvas).start()
        patch.object(dmarby.tkfont, "families", return_value=("DejaVu Sans",)).start()
        patch.object(dmarby.tkfont, "Font", Font).start()
        app = dmarby.Kiosk(Root())
        self.addCleanup(app.close)
        return app

    def test_same_circle_reuses_image_at_different_locations(self):
        app = self.app()
        previous_count = len(app.shape_cache)
        first = app.oval(0, 0, 36, 36, fill=dmarby.BLUE)
        second = app.oval(100, 50, 136, 86, fill=dmarby.BLUE)
        self.assertIs(app.shape_refs[first], app.shape_refs[second])
        self.assertEqual(len(app.shape_cache), previous_count + 1)

    def test_cache_eviction_keeps_visible_shapes_alive_until_redraw(self):
        app = self.app()
        with patch.object(dmarby, "SHAPE_CACHE_LIMIT", 4):
            first = app.oval(0, 0, 8, 8, fill=dmarby.BLUE)
            first_photo = app.shape_refs[first]
            for size in range(10, 20):
                app.oval(0, 0, size, size, fill=dmarby.BLUE)
            self.assertLessEqual(len(app.shape_cache), 4)
            self.assertNotIn(first_photo, app.shape_cache.values())
            self.assertIs(app.shape_refs[first], first_photo)
            app.redraw()
            self.assertNotIn(first, app.shape_refs)
            self.assertEqual(app.arc_specs, {})

    def test_spinner_updates_image_without_replacing_canvas_item(self):
        app = self.app()
        app.show("gc_cashing")
        spinner = app.spinner
        with patch.object(app.c, "itemconfigure") as update:
            app.spin()
        self.assertEqual(app.spinner, spinner)
        self.assertEqual(update.call_args.args, (spinner,))
        self.assertEqual(set(update.call_args.kwargs), {"image"})
        self.assertIs(app.shape_refs[spinner], update.call_args.kwargs["image"])


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
        self.app.overlay = {"kind": "timeout_money", "deadline": self.clock.now - 1}
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

    def test_gcash_is_blocked_before_real_cash_entry(self):
        self.app.start_gcash()
        self.assertEqual(self.app.screen, "gc_unavailable")
        self.assertIsNone(self.app.hardware.accepting)
        self.assertFalse(self.app.cashing_result(True))
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
        with self.assertRaisesRegex(HardwareFault, "Unresolved transaction"):
            Connected(Root(), self.cfg)

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
        self.assertIn(65, kit.servo[14].history)  # Proposed PHP 20 tube.
        self.assertIn(65, kit.servo[12].history)  # Proposed PHP 10 tube.
        self.assertEqual(kit.servo[14].pulse_limits, (1000, 2000))
        self.assertEqual(kit.servo[8].history, [])

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
