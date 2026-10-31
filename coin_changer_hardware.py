"""Hardware backend for Marby Raspberry Pi kiosk.

Real money mode requires a calibrated pulse map, verified inhibit circuits,
a coin-exit sensor, and five PCA9685 servo channels. GPIO numbers are BCM.
Never connect an acceptor's 12V signal directly to Raspberry Pi GPIO.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

from transaction_numbers import TransactionNumbers

BASE = Path(__file__).resolve().parent
COINS = (0.25, 1, 5, 10, 20)


def payout_coins_for(cfg):
    values = cfg.get('payout_coins', COINS)
    if (not isinstance(values, (list, tuple)) or not values
            or any(type(v) not in (int, float) or v not in COINS for v in values)
            or len(values) != len(set(values))):
        raise HardwareFault('Invalid payout denominations.')
    return tuple(c for c in COINS if c in values)


class HardwareFault(RuntimeError):
    pass


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf8') as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def project_path(path):
    p = Path(path).expanduser()
    return p if p.is_absolute() else BASE / p


class Storage:
    """Crash-visible journal. Unfinished transactions require an operator."""
    def __init__(self, cfg, references=None):
        self.coins = payout_coins_for(cfg)
        self.path = project_path(cfg.get('journal_file', 'transaction_state.json'))
        self.inventory_path = project_path(cfg.get('inventory_file', 'inventory.json'))
        self.references = references if references is not None else TransactionNumbers(
            self.path.with_name('transaction_numbers.sqlite3'), self.path.with_name('transactions.log'))
        self._lock = threading.Lock()
        self.state = json.loads(self.path.read_text(encoding='utf8')) if self.path.exists() else {}
        self.inventory = (
            json.loads(self.inventory_path.read_text(encoding='utf8'))
            if self.inventory_path.exists() else {str(c): 0 for c in self.coins}
        )
        for c in self.coins:
            n = self.inventory.get(str(c), 0)
            if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                raise HardwareFault('Invalid inventory count for PHP ' + str(c))
        if not self.inventory_path.exists():
            atomic_json(self.inventory_path, self.inventory)

    @property
    def unresolved(self):
        return self.state.get('status') in ('paid', 'dispensing', 'fault')

    def receive(self, source, value, flow=None):
        with self._lock:
            allowed = {'bill': {50, 100}, 'coin': {1, 5, 10, 20}}
            if (source not in allowed or type(value) is not int
                    or value not in allowed[source]):
                raise HardwareFault('Unsupported cash input.')
            if self.state.get('status') not in ('paid', None, 'completed', 'reconciled'):
                raise HardwareFault('Another transaction requires operator review.')
            if self.state.get('status') == 'paid' and self.state.get('source') != source:
                raise HardwareFault('Mixed input sources in a single transaction.')
            if self.state.get('status') == 'paid' and source == 'bill':
                raise HardwareFault('Only one bill is allowed per exchange. Operator required for additional cash.')
            if (self.state.get('status') == 'paid'
                    and self.state.get('credit', 0) + value > 100):
                raise HardwareFault('The maximum exchange total is PHP 100.')
            if self.state.get('status') != 'paid':
                self.state = {'status': 'paid', 'source': source, 'credit': 0,
                              'received': [], 'dispensed': {}, 'started_at': time.time()}
            self.state['credit'] += value
            self.state['received'].append({'source': source, 'value': value, 'time': time.time()})
            atomic_json(self.path, self.state)
            # Record received cash first, then attach its durable reference.
            if not self.state.get('reference'):
                self.state['reference'] = self.references.allocate(
                    'cash', 'hardware', dict(self.state, flow=flow))
                atomic_json(self.path, self.state)
            else:
                self.references.record(self.state['reference'], 'open', self.state)

    def can_pay(self, plan):
        return (bool(plan) and all(c in self.coins and type(n) is int and n > 0
                and self.inventory.get(str(c), 0) >= n for c, n in plan.items()))

    def can_make_exact(self, amount):
        """Bounded coin-stock solver in integer centavos, cached per inventory."""
        if not isinstance(amount, int) or amount < 1 or amount > 100:
            return False
        key = tuple(self.inventory.get(str(v), 0) for v in self.coins)
        if getattr(self, '_reachability_key', None) != key:
            reachable = {0}
            for value, available in zip(self.coins, key):
                cents = int(value * 100)
                count = min(10000 // cents, available)
                block = 1
                while count:
                    take = min(block, count)
                    increment = take * cents
                    reachable |= {n + increment for n in tuple(reachable)
                                  if n + increment <= 10000}
                    count -= take
                    block *= 2
            self._reachable = reachable
            self._reachability_key = key
        return amount * 100 in self._reachable

    def ready_for_source(self, source, mapped_values):
        if source == 'bill':
            return all(self.can_make_exact(v) for v in mapped_values)
        if source == 'coin':
            # Any running total from 1 to 100 must remain returnable.
            return all(self.can_make_exact(a) for a in range(1, 101))
        return False

    def start(self, plan):
        with self._lock:
            if self.state.get('status') != 'paid':
                raise HardwareFault('No recorded funds available for payment.')
            if sum(c * n for c, n in plan.items()) != self.state.get('credit'):
                raise HardwareFault('Payout value is not equal to recorded credit.')
            if not self.can_pay(plan):
                raise HardwareFault('Insufficient coins. Select another combination.')
            self.state['status'] = 'dispensing'
            self.state['plan'] = {str(c): n for c, n in plan.items()}
            self.state['dispensed'] = {}
            atomic_json(self.path, self.state)

    def confirmed(self, coin):
        with self._lock:
            if self.state.get('status') != 'dispensing':
                raise HardwareFault('Dispensing journal is not active.')
            if self.inventory.get(str(coin), 0) <= 0:
                raise HardwareFault('Inventory exhausted during payout.')
            self.inventory[str(coin)] -= 1
            self.state['dispensed'][str(coin)] = self.state['dispensed'].get(str(coin), 0) + 1
            # If power is lost between writes, pending journal prevents another payout.
            atomic_json(self.path, self.state)
            atomic_json(self.inventory_path, self.inventory)

    def complete(self):
        with self._lock:
            if self.state.get('status') != 'dispensing':
                raise HardwareFault('Payout not active.')
            if self.state.get('dispensed') != self.state.get('plan'):
                raise HardwareFault('Payout count does not match requested plan.')
            self.state['status'] = 'completed'
            self.state['completed_at'] = time.time()
            atomic_json(self.path, self.state)
            if self.state.get('reference'):
                self.references.record(self.state['reference'], 'completed', self.state)

    def fault(self, detail):
        with self._lock:
            self.state['status'] = 'fault'
            self.state['fault'] = str(detail)
            self.state['fault_at'] = time.time()
            atomic_json(self.path, self.state)
            if self.state.get('reference'):
                self.references.record(self.state['reference'], 'incident', self.state)


class PulseCounter:
    """Count a frame, then require quiet after BOTH edges and an idle input.

    An invalid/overlong frame is returned as ambiguous, never split or rounded.
    require_release is enabled on real inputs; the default preserves callers
    that feed only falling edges (including older diagnostic code).
    """
    def __init__(self, min_edge_s=.018, gap_s=1.0, max_pulses=100,
                 require_release=False, max_frame_s=60.0):
        if (not math.isfinite(gap_s) or gap_s <= 0
                or not math.isfinite(min_edge_s) or min_edge_s < 0
                or not math.isfinite(max_frame_s) or max_frame_s <= gap_s
                or type(max_pulses) is not int or max_pulses < 1):
            raise HardwareFault('Invalid pulse timing or frame limit.')
        self.lock = threading.Lock()
        self.min_edge_s = min_edge_s
        self.gap_s = gap_s
        self.max_pulses = max_pulses
        self.count = 0
        self.last = 0.0
        self.overflow = False
        self.require_release = require_release
        self.max_frame_s = max_frame_s
        self.active = False
        self.started = 0.0
        self.last_pulse = 0.0

    @property
    def pending(self):
        with self.lock:
            return self.count > 0

    def is_quiet(self, seconds):
        with self.lock:
            return (not self.count and not self.active
                    and (self.last == 0 or time.monotonic() - self.last >= seconds))

    def edge(self):
        now = time.monotonic()
        with self.lock:
            if self.count and now - self.last_pulse < self.min_edge_s:
                self.last = now
                self.active = True
                return False
            if self.count and now - self.last > self.gap_s:
                # Missing UI poll means frame boundary was ambiguous.
                self.overflow = True
            first_pulse = self.count == 0
            if first_pulse:
                self.started = now
            self.count += 1
            self.last = now
            self.last_pulse = now
            self.active = True
            if self.count > self.max_pulses or now - self.started >= self.max_frame_s:
                self.overflow = True
            return first_pulse

    def release(self):
        with self.lock:
            self.active = False
            self.last = time.monotonic()

    def drain(self, idle=True):
        with self.lock:
            if not self.count:
                return None
            now = time.monotonic()
            timed_out = now - self.started >= self.max_frame_s
            if not timed_out and (now - self.last < self.gap_s
                    or not idle or (self.require_release and self.active)):
                return None
            count, overflow = self.count, self.overflow or timed_out
            self.count, self.overflow = 0, False
            return count, overflow


def pulse_counter_for(cfg, source):
    """Build the per-source reader from the calibrated pulse profile.

    The supplied profile uses three seconds for bills and two for coins.
    A zero min-edge setting counts every falling edge, like the bench test;
    only verified electrical signals should be used for cash recognition.
    """
    gap = cfg.get(source + '_pulse_gap_seconds',
                  3.0 if source == 'bill' else cfg.get('pulse_gap_seconds', 1.0))
    mapping = cfg.get('pulse_map', {}).get(source, {})
    limit = max((int(k) for k in mapping), default=10) if source == 'bill' else 100
    return PulseCounter(
        min_edge_s=float(cfg.get(source + '_min_edge_seconds', .018)),
        gap_s=float(gap), max_pulses=limit, require_release=True,
        max_frame_s=float(cfg.get(source + '_max_frame_seconds', 45.0 if source == 'bill' else 60.0)))


class RealHardware:
    def __init__(self, cfg, references=None):
        self.cfg = cfg
        self.coins = payout_coins_for(cfg)
        for field in ('bill', 'coin'):
            if not cfg.get('inhibit_verified', {}).get(field, False):
                raise HardwareFault(f'{field} inhibit circuit not verified. Block money entry until verified.')
        if cfg.get('drop_sensor_pin') is None:
            raise HardwareFault('Coin-exit sensor required in real-money mode.')
        for coin in self.coins:
            if not cfg.get('servo_verified', {}).get(str(coin), False):
                raise HardwareFault(f'Servo channel for PHP {coin} not marked calibrated.')
        channels = [cfg.get('servos', {}).get(str(c), {}).get('channel') for c in self.coins]
        if (any(type(ch) is not int or ch not in range(16) for ch in channels)
                or len(channels) != len(set(channels))):
            raise HardwareFault('Payout servo channels must be unique PCA9685 outputs 0..15.')
        pins = list(cfg['input_pin'].values()) + list(cfg['inhibit_pin'].values()) + [cfg['drop_sensor_pin']]
        if len(pins) != len(set(pins)) or any(pin in (0, 1, 2, 3) for pin in pins):
            raise HardwareFault('GPIO pin conflict or pin reserved for I2C detected.')
        self.maps = {}
        for source, allowed in [('bill', {50, 100}), ('coin', {1, 5, 10, 20})]:
            raw = cfg.get('pulse_map', {}).get(source, {})
            try:
                mapping = {int(k): int(v) for k, v in raw.items()}
            except (TypeError, ValueError) as e:
                raise HardwareFault('Bad pulse mapping: ' + source) from e
            if not mapping or any(k <= 0 or v not in allowed for k, v in mapping.items()):
                raise HardwareFault('Calibrate ' + source + ' pulses before enabling hardware.')
            if len(set(mapping.values())) != len(mapping):
                raise HardwareFault('Duplicate denominations in ' + source + ' pulse map.')
            self.maps[source] = mapping
        self.storage = Storage(cfg, references=references)
        if self.storage.unresolved:
            raise HardwareFault('Unresolved transaction in transaction_state.json; operator must reconcile.')
        try:
            self.storage.references.ensure_ready()
        except Exception as error:
            raise HardwareFault(f'Transaction numbering storage is unavailable: {error}') from error
        self.accepting = None
        self._accept_lock = threading.RLock()
        self._requested_source = None
        self._deposit_source = None
        self._bill_latched = False
        self._input_fault = None
        self.frame_permission = {'bill': None, 'coin': None}
        self.stopping = threading.Event()
        self.pulses = {s: pulse_counter_for(cfg, s) for s in ('bill', 'coin')}
        self.rearm_quiet = {s: float(cfg.get(s + '_rearm_quiet_seconds', self.pulses[s].gap_s))
                            for s in ('bill', 'coin')}
        if any(not math.isfinite(v) or v < 0 for v in self.rearm_quiet.values()):
            raise HardwareFault('Invalid acceptor rearm quiet period.')
        self.drop_count = 0
        self.drop_time = 0
        self.drop_lock = threading.Lock()
        try:
            from gpiozero import DigitalInputDevice, OutputDevice
            from adafruit_servokit import ServoKit
        except ImportError as e:
            raise HardwareFault('Install gpiozero and adafruit-circuitpython-servokit') from e
        self.inhibits = {}
        self.inputs = {}
        self.drop_sensor = None
        self.kit = None
        try:
            self.kit = ServoKit(channels=16, address=int(cfg.get('pca9685_address', '0x40'), 0))
            for source in ('bill', 'coin'):
                pin = cfg['inhibit_pin'][source]
                self.inhibits[source] = OutputDevice(
                    pin, active_high=bool(cfg.get('inhibit_active_high', {}).get(source, True)),
                    initial_value=False)
            for source in ('bill', 'coin'):
                inp = DigitalInputDevice(cfg['input_pin'][source], pull_up=True)
                self.inputs[source] = inp
                inp.when_activated = self._make_pulse_callback(source)
                inp.when_deactivated = self.pulses[source].release
            self.drop_sensor = DigitalInputDevice(cfg['drop_sensor_pin'], pull_up=True)
            self.drop_sensor.when_activated = self._on_drop
        except Exception:
            self.close()
            raise

    def _make_pulse_callback(self, source):
        def callback():
            # Acceptance checks and ownership of the first edge are atomic.
            with self._accept_lock:
                first = self.pulses[source].edge()
                if first:
                    self.frame_permission[source] = (self.accepting == source)
                    if source == 'bill':
                        self._bill_latched = True
                    try:
                        self.set_acceptance(None)
                    except Exception:
                        self.frame_permission[source] = False
                        self.stopping.set()
        return callback

    def input_pending(self, source=None):
        sources = (source,) if source else ('bill', 'coin')
        return any(self.pulses[s].pending for s in sources)

    def begin_deposit(self, source):
        """Explicitly open a new UI deposit session, never an in-flight bill."""
        with self._accept_lock:
            if source not in ('bill', 'coin'):
                raise HardwareFault('Unsupported deposit source.')
            if (self.stopping.is_set() or self.storage.unresolved
                    or self.input_pending() or self._bill_latched):
                raise HardwareFault('Finish or reconcile the current exchange before accepting another bill.')
            self.set_acceptance(None)
            self._deposit_source = source

    def complete_transaction(self):
        """Release the one-bill lock only after the payout is committed."""
        with self._accept_lock:
            self.set_acceptance(None)
            if self.stopping.is_set() or self.input_pending():
                raise HardwareFault('Cash signal arrived during payout. Operator must reconcile the cash.')
            self.storage.complete()
            self._bill_latched = False
            self._deposit_source = None

    def _on_drop(self):
        now = time.monotonic()
        with self.drop_lock:
            if now - self.drop_time > float(self.cfg.get('sensor_debounce_seconds', .08)):
                self.drop_time = now
                self.drop_count += 1

    def set_acceptance(self, source=None):
        with self._accept_lock:
            if source not in (None, 'bill', 'coin'):
                raise HardwareFault('Unsupported acceptor.')
            self._requested_source = source
            self._refresh_acceptance()

    def _refresh_acceptance(self):
        # Caller holds _accept_lock. UI redraws cannot reopen an active frame,
        # a bill already consumed by this session, or a paid bill transaction.
        permitted = self._requested_source
        if (permitted != self._deposit_source or self.stopping.is_set()
                or self._input_fault or self.input_pending()
                or any(inp.is_active for inp in self.inputs.values())
                or any(not self.pulses[s].is_quiet(self.rearm_quiet[s])
                       for s in ('bill', 'coin'))):
            permitted = None
        if permitted == 'bill' and (self._bill_latched or self.storage.unresolved):
            permitted = None
        if self.accepting == permitted and permitted is not None:
            return
        self.accepting = None
        for output in self.inhibits.values():
            output.off()
        if permitted is not None:
            self.inhibits[permitted].on()
            self.accepting = permitted

    def _latch_input_fault(self, detail):
        self._input_fault = str(detail)
        self.stopping.set()
        self.set_acceptance(None)
        # Persist ambiguity before returning a UI event. Existing credit remains
        # recorded, but no additional value is inferred from the pulse total.
        self.storage.fault(self._input_fault)
        return [('fault', self._input_fault)]

    def poll(self):
        events = []
        with self._accept_lock:
            if self._input_fault:
                return []
            for source in ('bill', 'coin'):
                frame = self.pulses[source].drain(idle=not self.inputs[source].is_active)
                if frame is None:
                    continue
                pulses, overflow = frame
                allowed_at_first_pulse = self.frame_permission[source]
                self.frame_permission[source] = None
                value = self.maps[source].get(pulses)
                if overflow or value is None:
                    if source == 'bill':
                        detail = (f'Ambiguous bill pulse group: {pulses} pulses. '
                                  'Expected exactly 5 for PHP 50 or 10 for PHP 100. '
                                  'Possible repeated bills or signal error; no credit inferred. '
                                  'Operator must count and reconcile accepted cash.')
                    else:
                        detail = f'Unknown or ambiguous coin pulse train: {pulses} pulses. Operator required.'
                    return self._latch_input_fault(detail)
                if not allowed_at_first_pulse:
                    return self._latch_input_fault(
                        f'Unexpected {source} pulse group while intake was disabled: {pulses} pulses. '
                        'Additional cash may have entered; operator must reconcile it.')
                events.append((source, value))
            self._refresh_acceptance()
        return events

    def dispense_one(self, coin):
        if coin not in self.coins:
            raise HardwareFault('Unsupported payout coin.')
        if self.stopping.is_set():
            raise HardwareFault('Emergency stop / shutdown requested.')
        setting = self.cfg.get('servos', {}).get(str(coin))
        if not setting or not setting.get('enabled', True):
            raise HardwareFault(f'No calibrated servo for PHP {coin}.')
        channel = int(setting['channel'])
        home = float(setting['home_angle'])
        release = float(setting['release_angle'])
        hold = float(setting.get('hold_seconds', .45))
        settle = float(setting.get('settle_seconds', .3))
        timeout = float(self.cfg.get('drop_timeout_seconds', 2.0))
        with self.drop_lock:
            before = self.drop_count
        servo = self.kit.servo[channel]
        servo.actuation_range = float(setting.get('actuation_range', 180))
        if 'min_pulse_us' in setting or 'max_pulse_us' in setting:
            minimum = int(setting.get('min_pulse_us', 1000))
            maximum = int(setting.get('max_pulse_us', 2000))
            if not 500 <= minimum < maximum <= 2500:
                raise HardwareFault('Invalid calibrated servo pulse limits.')
            servo.set_pulse_width_range(minimum, maximum)
        try:
            servo.angle = home
            time.sleep(.15)
            servo.angle = release
            time.sleep(hold)
            servo.angle = home
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.stopping.is_set():
                    raise HardwareFault('Dispensing interrupted.')
                with self.drop_lock:
                    passed = self.drop_count - before
                if passed == 1:
                    time.sleep(settle)
                    with self.drop_lock:
                        if self.drop_count - before != 1:
                            raise HardwareFault('Extra coin detected during sensor settling.')
                    return
                if passed > 1:
                    raise HardwareFault('Multiple sensor crossings for one commanded coin.')
                time.sleep(.025)
            raise HardwareFault(f'No coin detected after PHP {coin} servo cycle (possible empty tube/jam).')
        finally:
            try:
                servo.angle = None if self.stopping.is_set() else home
            except Exception:
                pass

    def close(self):
        self.stopping.set()
        if getattr(self, 'kit', None) is not None:
            for coin in self.coins:
                try:
                    channel = self.cfg['servos'][str(coin)]['channel']
                    self.kit.servo[channel].angle = None
                except Exception:
                    pass
        for output in getattr(self, 'inhibits', {}).values():
            try: output.off(); output.close()
            except Exception: pass
        for inp in getattr(self, 'inputs', {}).values():
            try: inp.close()
            except Exception: pass
        if getattr(self, 'drop_sensor', None) is not None:
            try: self.drop_sensor.close()
            except Exception: pass
