"""Dependency-free tests. Run: python -m unittest -v test_marby.py"""
import json
import tempfile
import time
import unittest
import copy
import types
from pathlib import Path
from unittest.mock import patch
from coin_changer_hardware import Storage, PulseCounter, HardwareFault, RealHardware, pulse_counter_for


class TestHardware(unittest.TestCase):
    def storage(self, folder):
        return Storage({'journal_file': str(Path(folder) / 'transactions.json'),
                        'inventory_file': str(Path(folder) / 'inventory.json')})

    def test_pulses(self):
        p = PulseCounter(gap_s=.1, min_edge_s=.02)
        p.edge(); p.edge()    # bounce is ignored
        self.assertIsNone(p.drain())
        p.last = time.monotonic() - .15
        self.assertEqual(p.drain(), (1, False))

    def test_complete_transaction(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.storage(d)
            s.inventory = {'1': 10, '5': 10, '10': 5, '20': 5}
            from coin_changer_hardware import atomic_json
            atomic_json(s.inventory_path, s.inventory)
            s.receive('bill', 50)
            self.assertFalse(self.storage(d).can_pay({20: 6}))
            s.start({20: 2, 10: 1})
            for c in (20, 20, 10):
                s.confirmed(c)
            s.complete()
            self.assertEqual(s.state['status'], 'completed')
            self.assertEqual(self.storage(d).inventory['20'], 3)
            self.assertEqual(self.storage(d).inventory['10'], 4)

    def test_recovery_blocks_replay(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.storage(d)
            s.receive('coin', 5)
            self.assertTrue(self.storage(d).unresolved)
            s.start({1: 5}) if s.can_pay({1: 5}) else None
            self.assertTrue(self.storage(d).unresolved)

    def test_quarter_coin_fifth_servo_inventory(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.storage(d)
            s.inventory = {'0.25': 400, '1': 0, '5': 0, '10': 0, '20': 0}
            self.assertTrue(s.can_make_exact(100))
            self.assertTrue(s.can_pay({0.25: 4}))
            s.receive('coin', 1)
            s.start({0.25: 4})
            for _ in range(4):
                s.confirmed(0.25)
            s.complete()
            self.assertEqual(s.inventory['0.25'], 396)

    def test_operator_reconciliation_reopens_journal(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.storage(d)
            s.receive('coin', 5)
            s.fault('jam')
            s.state['status'] = 'reconciled'
            from coin_changer_hardware import atomic_json
            atomic_json(s.path, s.state)
            reopened = self.storage(d)
            self.assertFalse(reopened.unresolved)
            reopened.receive('coin', 1)
            self.assertEqual(reopened.state['credit'], 1)

    def test_not_enough_stock(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.storage(d)
            self.assertFalse(s.ready_for_source('coin', [1, 5, 10]))
            s.inventory = {'1': 100, '5': 0, '10': 0, '20': 0}
            self.assertTrue(s.ready_for_source('coin', [1, 5, 10]))
            self.assertTrue(s.can_make_exact(99))
            self.assertFalse(s.can_make_exact(101))

    def test_reject_bad_value(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.storage(d)
            s.receive('bill', 100)
            with self.assertRaises(HardwareFault):
                s.start({20: 4})

    def test_second_bill_is_not_added_to_journal_credit(self):
        with tempfile.TemporaryDirectory() as d:
            s = self.storage(d)
            s.receive('bill', 100)
            with self.assertRaises(HardwareFault):
                s.receive('bill', 100)
            self.assertEqual(s.state['credit'], 100)
            self.assertEqual(len(s.state['received']), 1)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeInput:
    def __init__(self, pin, **kwargs):
        self.is_active = False
        self.when_activated = None
        self.when_deactivated = None

    def close(self):
        pass


class FakeOutput:
    def __init__(self, pin, **kwargs):
        self.enabled = False

    def on(self):
        self.enabled = True

    def off(self):
        self.enabled = False

    def close(self):
        self.off()


class TestSlowBillInput(unittest.TestCase):
    """Exercise the actual backend with simulated GPIO and deterministic time."""
    def setUp(self):
        self.clock = Clock()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = json.loads(Path(__file__).with_name('coin_changer_config.json').read_text())
        self.cfg['journal_file'] = str(Path(self.temp.name) / 'journal.json')
        self.cfg['inventory_file'] = str(Path(self.temp.name) / 'inventory.json')
        # Simulation fixtures only; never write these verification flags
        # to the supplied production configuration.
        self.cfg['inhibit_verified'] = {'bill': True, 'coin': True}
        self.cfg['servo_verified'] = {str(c): True for c in (0.25, 1, 5, 10, 20)}
        modules = {
            'gpiozero': types.SimpleNamespace(DigitalInputDevice=FakeInput, OutputDevice=FakeOutput),
            'adafruit_servokit': types.SimpleNamespace(ServoKit=lambda **kw: types.SimpleNamespace(servo=[None] * 16))
        }
        self.addCleanup(patch.stopall)
        patch.dict('sys.modules', modules).start()
        patch('coin_changer_hardware.time.monotonic', self.clock).start()
        self.hw = RealHardware(self.cfg)
        self.addCleanup(self.hw.close)

    def advance(self, seconds, poll=True):
        events = []
        target = self.clock.now + seconds
        while self.clock.now + .000001 < target:
            self.clock.now = min(target, self.clock.now + .05)
            if poll:
                events.extend(self.hw.poll())
        return events

    def start_bill(self):
        self.hw.begin_deposit('bill')
        self.hw.set_acceptance('bill')
        self.assertEqual(self.hw.accepting, 'bill')

    def train(self, count, source='bill'):
        events = []
        inp = self.hw.inputs[source]
        for _ in range(count):
            inp.is_active = True
            inp.when_activated()
            events.extend(self.advance(.05))
            inp.is_active = False
            inp.when_deactivated()
            events.extend(self.advance(.95))
        return events

    def test_five_slow_pulses_are_one_fifty_peso_bill(self):
        self.start_bill()
        self.assertEqual(self.train(5), [])
        self.assertEqual(self.advance(3.1), [('bill', 50)])

    def test_ten_slow_pulses_are_one_hundred_peso_bill(self):
        self.start_bill()
        self.assertEqual(self.train(10), [])
        self.assertEqual(self.advance(3.1), [('bill', 100)])

    def test_both_edges_restart_quiet_period_and_held_low_is_not_credited(self):
        self.start_bill()
        inp = self.hw.inputs['bill']
        inp.is_active = True
        inp.when_activated()
        self.assertEqual(self.advance(4.0), [])
        inp.is_active = False
        inp.when_deactivated()
        self.assertEqual(self.advance(2.9), [])
        events = self.advance(.2)
        self.assertEqual(events[0][0], 'fault')  # One pulse is not a supported bill.

    def test_first_pulse_inhibits_and_redraw_cannot_reenable_bill(self):
        self.start_bill()
        self.hw.inputs['bill'].is_active = True
        self.hw.inputs['bill'].when_activated()
        self.assertIsNone(self.hw.accepting)
        self.assertTrue(all(not output.enabled for output in self.hw.inhibits.values()))
        self.hw.set_acceptance('bill')
        self.hw.poll()
        self.assertIsNone(self.hw.accepting)
        with self.assertRaises(HardwareFault):
            self.hw.begin_deposit('bill')

    def test_twenty_pulses_are_not_credited_as_two_bills(self):
        self.start_bill()
        self.assertEqual(self.train(20), [])
        events = self.advance(3.1)
        self.assertEqual(events[0][0], 'fault')
        self.assertIn('20 pulses', events[0][1])
        self.assertEqual(self.hw.storage.state['status'], 'fault')
        self.assertEqual(self.hw.storage.state.get('credit', 0), 0)

    def test_reported_twenty_one_pulse_failure_is_latched_and_persisted(self):
        self.start_bill()
        self.assertEqual(self.train(21), [])
        events = self.advance(3.1)
        self.assertEqual(events[0][0], 'fault')
        self.assertIn('21 pulses', events[0][1])
        disk = json.loads(Path(self.cfg['journal_file']).read_text())
        self.assertEqual(disk['status'], 'fault')
        self.assertIn('21 pulses', disk['fault'])
        self.hw.set_acceptance('bill')
        self.assertIsNone(self.hw.accepting)

    def test_second_note_after_first_credit_preserves_first_credit_and_faults(self):
        self.start_bill()
        self.train(10)
        self.assertEqual(self.advance(3.1), [('bill', 100)])
        self.hw.storage.receive('bill', 100)
        self.hw.set_acceptance('bill')
        self.assertIsNone(self.hw.accepting)
        self.train(10)  # Simulate physical intake despite the inhibit command.
        events = self.advance(3.1)
        self.assertEqual(events[0][0], 'fault')
        self.assertEqual(self.hw.storage.state['credit'], 100)
        self.assertEqual(len(self.hw.storage.state['received']), 1)

    def test_bill_lock_released_only_by_committed_payout(self):
        self.start_bill()
        self.train(5)
        self.assertEqual(self.advance(3.1), [('bill', 50)])
        self.hw.storage.receive('bill', 50)
        with self.assertRaises(HardwareFault):
            self.hw.begin_deposit('bill')
        self.hw.storage.inventory = {'0.25': 0, '1': 100, '5': 20, '10': 20, '20': 20}
        self.hw.storage.start({20: 2, 10: 1})
        for coin in (20, 20, 10):
            self.hw.storage.confirmed(coin)
        self.hw.complete_transaction()
        self.start_bill()
        self.train(10)
        self.assertEqual(self.advance(3.1), [('bill', 100)])

    def test_missing_release_event_times_out_without_credit(self):
        self.start_bill()
        self.hw.inputs['bill'].is_active = True
        self.hw.inputs['bill'].when_activated()
        events = self.advance(45.1)
        self.assertEqual(events[0][0], 'fault')
        self.assertEqual(self.hw.storage.state.get('credit', 0), 0)

    def test_disabled_acceptor_event_is_fault_not_credit(self):
        self.train(5)
        events = self.advance(3.1)
        self.assertEqual(events[0][0], 'fault')
        self.assertEqual(self.hw.storage.state.get('credit', 0), 0)

    def test_coin_gap_is_independent_and_two_seconds(self):
        self.assertEqual(self.hw.pulses['bill'].gap_s, 3.0)
        self.assertEqual(self.hw.pulses['coin'].gap_s, 2.0)
        self.hw.begin_deposit('coin')
        self.hw.set_acceptance('coin')
        self.assertEqual(self.train(1, source='coin'), [])
        self.assertEqual(self.advance(1.0), [])
        self.assertEqual(self.advance(.2), [('coin', 1)])
        self.hw.storage.receive('coin', 1)
        self.hw.set_acceptance('coin')
        self.assertEqual(self.hw.accepting, 'coin')

    def fast_coin_train(self, count):
        events = []
        inp = self.hw.inputs['coin']
        for _ in range(count):
            inp.is_active = True
            inp.when_activated()
            events.extend(self.advance(.005))
            inp.is_active = False
            inp.when_deactivated()
            events.extend(self.advance(.005))
        return events

    def test_all_reported_coin_denominations_at_raw_counter_spacing(self):
        self.hw.begin_deposit('coin')
        self.hw.set_acceptance('coin')
        for count, value in ((1, 1), (5, 5), (10, 10), (20, 20)):
            with self.subTest(pulses=count, pesos=value):
                self.assertEqual(self.fast_coin_train(count), [])
                self.assertEqual(self.advance(1.9), [])
                self.assertEqual(self.advance(.2), [('coin', value)])
                self.hw.storage.receive('coin', value)
                self.hw.set_acceptance('coin')
                self.assertEqual(self.hw.accepting, 'coin')
        self.assertEqual(self.hw.storage.state['credit'], 36)

    def test_unmapped_coin_group_is_a_persistent_fault(self):
        self.hw.begin_deposit('coin')
        self.hw.set_acceptance('coin')
        self.fast_coin_train(2)
        events = self.advance(2.1)
        self.assertEqual(events[0][0], 'fault')
        self.assertEqual(self.hw.storage.state.get('credit', 0), 0)
        disk = json.loads(Path(self.cfg['journal_file']).read_text())
        self.assertEqual(disk['status'], 'fault')
        self.assertIn('2 pulses', disk['fault'])

    def test_merged_twenty_one_coin_pulses_are_not_credited(self):
        self.hw.begin_deposit('coin')
        self.hw.set_acceptance('coin')
        self.fast_coin_train(21)
        events = self.advance(2.1)
        self.assertEqual(events[0][0], 'fault')
        self.assertEqual(self.hw.storage.state.get('credit', 0), 0)

    def test_calibrated_coins_still_require_verified_inhibit(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['inhibit_verified']['coin'] = False
        with self.assertRaisesRegex(HardwareFault, 'coin inhibit circuit not verified'):
            RealHardware(cfg)

    def test_unverified_inhibit_remains_blocked(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['inhibit_verified']['bill'] = False
        with self.assertRaisesRegex(HardwareFault, 'inhibit circuit not verified'):
            RealHardware(cfg)

    def test_disable_command_forces_outputs_off_even_when_cached_state_is_disabled(self):
        self.assertIsNone(self.hw.accepting)
        for output in self.hw.inhibits.values():
            output.enabled = True  # Simulate uncertain external driver state.
        self.hw.set_acceptance(None)
        self.assertTrue(all(not output.enabled for output in self.hw.inhibits.values()))

    def test_late_frame_boundary_is_not_split_into_guessed_bills(self):
        self.start_bill()
        self.train(5)
        self.advance(4.0, poll=False)
        self.train(5)
        events = self.advance(3.1)
        self.assertEqual(events[0][0], 'fault')

    def test_invalid_timing_is_rejected(self):
        for gap in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(HardwareFault):
                PulseCounter(gap_s=gap)

if __name__ == '__main__':
    unittest.main()
