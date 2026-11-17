# D'MARBY — Integrated Touchscreen and Hardware

D'MARBY combines the supplied 800 × 480 Tkinter frontend with the calibrated Raspberry Pi cash backend. It supports bills to coins, breaking larger coins into smaller coins, and combining smaller coins. GCash remains a demo flow; hardware mode shows it as unavailable before accepting cash.

## Start with the demo

Keep acceptors unpowered during a demo: demo mode does not control GPIO or inhibit hardware.

```bash
python3 dmarby.py --demo --windowed
```

The existing screens, embedded currency images and demo controls are retained. Supported bills are now only PHP 50 and PHP 100. Demo coins are PHP 1, 5, 10 and 20. The GCash demo retains the frontend's 5% fee; it does not transfer money.

## Install on the Raspberry Pi

Use Raspberry Pi OS Desktop and run the UI from its graphical desktop session. Stop the previous kiosk and any standalone GPIO or servo tests before running this application.

Extract this package into the project directory alongside any existing inventory and journal. The package does not include real inventory, transaction state, customer logs, or reconciliation history.

```bash
cd ~/Marby
sudo apt update
sudo apt install -y python3-tk python3-venv python3-pip python3-gpiozero python3-lgpio i2c-tools
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
```

Enable I2C in `sudo raspi-config`, restart if prompted, and check the driver:

```bash
sudo i2cdetect -y 1
```

The configuration uses PCA9685 address `0x40`.

### Portable fonts and welcome screen

No custom font files, font installation or network access are needed. The UI uses Plus Jakarta Sans if it is already installed, then checks installed system sans-serif families (including Segoe UI on Windows and DejaVu Sans on Raspberry Pi OS). If none match, it uses Tk's default font. Missing optional fonts are handled silently. The app does not copy fonts into your user account or run a font-cache command.

The welcome screen centres “Welcome to”, the solid D’MARBY wordmark and “COIN EXCHANGE” within one white panel. The large logo uses native Tk canvas text with a subtle shadow and no icon. Its width is checked using the selected font to keep it within the panel.

Circular controls, status dots, badges, loading/countdown rings, rounded cards and button ends use transparent antialiased images generated with Python's standard library. This smooths their curved edges without requiring Pillow, image downloads or another package. The app caches the images and spinner frames, and bounds its cache to keep memory use stable.

For this transaction-number update, install `dmarby.py`, `dmarby_integration.py`, `coin_changer_hardware.py` and the new `transaction_numbers.py` together in the existing project folder. The transaction-update ZIP contains those four Python files and this README. Keep your existing `assets.py`, machine configuration, inventory, transaction journal, logs and any `transaction_numbers.sqlite3` file. The complete package uses canonical module filenames without upload suffixes such as `(1)`.

## Sequential transaction references

Cash exchanges (bill to coins, breaking coins and combining coins) and GCash use one increasing sequence: `000001`, `000002`, `000003`, and so on. A number is reserved when the first money is accepted. Additional deposits, screen redraws, duplicate completion callbacks and GCash retries keep that same reference. An incident keeps the transaction's number too; cancelling before depositing money does not consume a number.

The cash collect-coins screen and GCash success screen show `Ref. No.`. The same number appears as `ref` in `transactions.log`, and hardware transactions retain it as `reference` in `transaction_state.json`.

Numbers and transaction details are saved automatically in `transaction_numbers.sqlite3`, beside the application's `transactions.log`. This uses Python's standard SQLite module and requires no additional package. The sequence survives application restarts and does not reset daily or depend on the system clock. Keep this database when updating or moving the installation. It is deliberately omitted from update packages.

On first use, existing completed cash/GCash entries in `transactions.log` are counted in their recorded order. Existing numeric references, including incidents, are retained if the history is used to initialise the database. Original log lines are never rewritten. Demo and hardware transactions share the sequence and remain labelled by mode in their records. The GCash number is a local machine reference; hardware GCash remains unavailable until a real transfer integration is supplied.

## Hardware settings to confirm

GPIO numbers are **BCM** numbers unless a physical header pin is explicitly stated. Never connect acceptor 12 V signals directly to Raspberry Pi GPIO. Keep the tested isolated acceptor pulse wiring and externally powered servo wiring unchanged.

| Function | BCM GPIO | Raspberry Pi physical pin |
|---|---:|---:|
| Allan coin pulses (existing PC817 input path) | 17 | 11 |
| TB74 bill pulses (existing PC817 input path) | 27 | 13 |
| Coin-exit sensor (configured but **disabled**) | 22 | 15 |
| Allan coin-acceptor INHIBIT relay control | **16** | **36** |
| TB74 bill-acceptor 12 V **power** relay control (active LOW) | **26** | **37** |
| PCA9685 SDA | 2 | 3 |
| PCA9685 SCL | 3 | 5 |

### Relay controls: current physical plan

- **Allan (physical pin 36 / GPIO16):** GPIO16 operates the relay used for Allan's INHIBIT function. The Allan relay's **trigger polarity and resulting accept/reject state have not yet been confirmed**; verify both GPIO levels physically before using cash.
- **TB74 (physical pin 37 / GPIO26):** GPIO26 operates an **active-LOW relay** that switches the acceptor's 12 V positive supply, **not** the TB74 INHIBIT terminals. GPIO26 **LOW** energizes the relay (TB74 powered if the contacts are wired correctly); GPIO26 **HIGH** releases it (TB74 unpowered with normally-open contacts). Leave the normally-closed contact unused for this power-off-default arrangement.
- The relay module must be electrically suitable for a Raspberry Pi's **3.3 V GPIO trigger** and its actual supply/terminal layout. The GPIO only drives the module control input; it must **never carry the TB74's 12 V power or relay-coil current**. Verify supply, fuse, contact rating, wiring and unpowered state with an electronics supervisor.
- **Do not turn off the TB74 while it is transporting a bill or transmitting a pulse group.** Switching the 12 V power is not the same as asserting TB74 INHIBIT, which leaves the mechanism energized. Only turn off power after the bill path is known to be idle.
- Test relay responses **without inserting cash**, and verify the TB74 is unpowered when the relay is released and during Raspberry Pi shutdown or reboot. A control signal being HIGH in a Python script does not by itself prove a fail-safe boot state.

**Code/config alignment:** `coin_changer_config.json` now sets `inhibit_pin.bill = 26` (physical pin 37), `inhibit_pin.coin = 16` (physical pin 36), and `inhibit_active_high.bill = false` for the active-LOW TB74 relay. The Allan relay retains the **unverified** `inhibit_active_high.coin = true` setting until its actual trigger polarity and accept/reject behavior are confirmed. `inhibit_verified` is **false for both**, so the backend intentionally blocks hardware mode until testing. **Critically, the existing backend still commands intake closed on the first pulse**; this must be redesigned before using GPIO26 to switch bill acceptor power, or the machine could cut power during bill transport. The config pin correction alone does not implement safe TB74 power sequencing.

#### Manual GPIO relay test (no money inserted)

Stop D'MARBY and other GPIO tests first. The following test sends levels to the relay inputs; it **does not** integrate them into transaction handling. Use the Pi's `.venv-pi` environment if that is where `gpiozero`/`lgpio` is installed:

```bash
cd ~/Marby-1.0
GPIOZERO_PIN_FACTORY=lgpio .venv-pi/bin/python - <<'PY'
from gpiozero import OutputDevice

allan = OutputDevice(16, active_high=True, initial_value=True)
bill = OutputDevice(26, active_high=True, initial_value=True)

print("1=Allan LOW  2=Allan HIGH  3=TB74 power ON (LOW)")
print("4=TB74 power OFF (HIGH)  5=both GPIO HIGH  q=quit")
try:
    while True:
        cmd = input("Relay test> ").strip().lower()
        if cmd == "1":
            allan.off()
        elif cmd == "2":
            allan.on()
        elif cmd == "3":
            bill.off()
        elif cmd == "4":
            bill.on()
        elif cmd == "5":
            allan.on()
            bill.on()
        elif cmd == "q":
            break
        else:
            print("Choose 1, 2, 3, 4, 5 or q")
            continue
        print("Allan GPIO16:", "HIGH" if allan.value else "LOW",
              "| TB74 GPIO26:", "HIGH" if bill.value else "LOW")
except KeyboardInterrupt:
    pass
finally:
    allan.on()
    bill.on()
    allan.close()
    bill.close()
    print("GPIO outputs reset HIGH; verify both relay states.")
PY
```

Keep hardware-control verification disabled for **real cash intake** until the TB74 power relay has safe bill-completion sequencing, the Allan inhibit relay is tested, complete pulse groups are validated, and recovery behavior is confirmed.

### Four payout dispensers

This frontend uses PHP 1, 5, 10 and 20 coins. The previous backend's fifth, 25-centavo dispenser remains supported for other configurations, but is not enabled by this frontend's configuration.

**Current `coin_changer_config.json` servo channel assignments** (PCA9685 channel numbers, 0–15) are listed below. Earlier README values 8/10/12/14 were described as physical sockets; they do **not** match the current software channel settings. Confirm actual dispenser wiring before powering the servos.

| Payout coin | Configured PCA9685 channel |
|---:|---:|
| PHP 1 | 5 |
| PHP 5 | 4 |
| PHP 10 | 1 |
| PHP 20 | 0 |

Each dispenser has independent `min_pulse_us`, `max_pulse_us`, `actuation_range`, `home_angle`, `release_angle`, `hold_seconds` and `settle_seconds` settings. The current configured home/release angles are PHP 1: 0°/70°, PHP 5: 0°/65°, PHP 10: 12°/108°, and PHP 20: 0°/90°; direction settings can mirror these positions. These are software settings, not proof of mechanical calibration.

**Current configuration has `coin_exit_sensor = false`.** Although `drop_sensor_pin = 22` (physical pin 15) is reserved in the config, the backend does **not** open/read that sensor while disabled. In this mode it counts each completed servo actuation as one dispensed coin, without sensor confirmation. If a physical coin-exit sensor is installed and validated, it must be enabled and tested explicitly before the README can claim confirmed dispensing.

### Pulse mappings

| Acceptor | Pulse count | Amount |
|---|---:|---:|
| TB74 | 5 | PHP 50 |
| TB74 | 10 | PHP 100 |
| Allan | 1 / 5 / 10 / 20 | PHP 1 / 5 / 10 / 20 |

Bills use a 3-second quiet period and coins use 2 seconds after the last transition, with the signal returned HIGH. Only one bill is allowed per exchange. Exchange totals are limited to PHP 100. A 21-pulse bill group is not rounded or split into guessed bills.

## Load inventory and enable hardware mode

1. If no inventory file exists, create `inventory.json` using the structure in `inventory.example.json`. Enter physically counted coin quantities. Preserve any existing inventory file and reconcile it against the machine.
2. Confirm the PCA9685 channel-to-dispenser mapping, verify pulse maps, both acceptance-control circuits, the TB74 power relay's safe timing, and every dispenser's one-coin cycle. Set `inhibit_verified` to true only after the corrected backend and physical controls pass fail-safe testing. `servo_verified` flags in the supplied config are already true; these flags do not prove the currently wired mechanisms were physically calibrated.
3. Set `contact_number` to the staff contact shown on incident screens.
4. Run the tests before enabling real-money hardware mode:

```bash
.venv/bin/python -m unittest -v test_marby.py test_dmarby_integration.py test_transaction_numbers.py
```

### Start D'MARBY on the configured Raspberry Pi

Open a terminal and type:

```bash
marby
```

**`marby` is the existing startup command configured on this Raspberry Pi.** You do not need to type `python dmarby.py`, specify `--hardware`, or run `run_marby.sh` manually to launch the kiosk there. This command depends on the Pi's installed shell command/launcher; a fresh clone of the repository may not provide it automatically.

For development or troubleshooting, `dmarby.py` still supports direct launch options such as `--windowed` and `--config /absolute/path/machine.json`, but these are not required for the normal `marby` startup command. Inventory and journal paths are resolved relative to the configuration file. The repository's `run_marby.sh` remains an alternative launcher, not the normal startup instruction.

Hardware startup is intentionally blocked by `inhibit_verified.bill = false` and `inhibit_verified.coin = false`, pending re-verification of the changed relay wiring and safe power-control behavior. Changing those flags does not perform the physical checks.

## Connected transaction flow

1. The touchscreen selects an exchange and an exact coin mix.
2. Only the relevant acceptor is enabled on the insert screen, subject to stock checks.
3. **Current backend behavior (not suitable for TB74 power switching):** the first pulse commands both acceptance outputs OFF. For an inhibit input this can close intake, but with a TB74 12 V power relay it may interrupt an in-progress bill. The bill power-control path must be revised and tested before real-money use. Complete calibrated pulse groups are recorded before credit appears on the UI.
4. The customer confirms the payout. A worker performs one servo cycle at a time while the touchscreen remains responsive.
5. **With the coin-exit sensor currently disabled**, each completed servo cycle is assumed to have dispensed one coin and updates journal/inventory; the software does not independently confirm physical exit. The collect screen appears after the planned payout is committed.

A different supported bill can be rerouted to a rebuilt mix when the recorded inventory can pay its full value. The hardware handler rechecks the actual bill after acceptance, and inventory is checked again when the customer taps “Choose coins”. If the customer selected PHP 50 but the accepted PHP 100 cannot be paid, the screen shows “You inserted a PHP 100 bill”, “Not the PHP 50 you picked”, the staff contact and the existing transaction reference. No coin payout starts. The full accepted amount, actual bill and selected bill are saved in the fault journal and transaction history.

The initial hardware check still requires inventory to cover both accepted bill values before intake is enabled. The post-acceptance check handles stock changes or an unexpected accepted bill. On the unpayable-bill screen, **Exit** returns to the welcome screen while preserving the outstanding transaction and keeping both acceptors disabled. Starting again returns to the incident; staff must settle the customer, reconcile the journal and restart before hardware cash intake resumes. If the previous transaction-number update is installed, this fallback fix requires replacing `dmarby.py` and `dmarby_integration.py` together.

An accepted coin that exceeds the selected amount is recorded as a hardware service incident; the screen never falsely claims it was physically returned.

For a GCash bill overpayment, the UI records the accepted bill and all earlier deposits, stops cash-in and shows the contact incident screen. It displays the amount still needed before that bill, the actual bill denomination, the staff contact and the transaction's existing sequential reference. The machine cannot dispense bills, so the screen does not claim an accepted bill was returned. The full received amount remains recorded for staff settlement; the extra amount is also recorded separately. An exact payment can proceed to cash-in, and a bill rejected by the acceptor before acceptance does not create an overpayment incident.

## Recovery

Money left inside after a timeout or application exit stays recorded as owed. A customer-facing screen or demo control cannot clear a real incident.

After counting the actual accepted and dispensed cash, settling the customer's balance, and correcting inventory, record the resolution:

```bash
.venv/bin/python marby_service.py \
  --operator "Technician ID" \
  --reason "Cash counted; customer balance settled; inventory corrected"
```

Enter `RECONCILED` when prompted. This records the resolution; it does not dispense or refund money. Review `reference` and `uncredited_inputs` as well as `credit`, `received`, `plan` and `dispensed` when present. Preserve `transaction_state.json`, `inventory.json`, `transactions.log`, `transaction_numbers.sqlite3` and `reconciliations.jsonl` during updates.

If the kiosk used a custom configuration, pass the same `--config /absolute/path/machine.json` to `marby_service.py`. Its reconciliation log is saved beside that journal.

## GCash status

The supplied frontend uses a timer to simulate a successful cash-in. No real transfer API, authentication, wallet verification or provider transaction callback was supplied. Hardware mode therefore blocks this service before collecting money. A real transfer integration must provide confirmed, recoverable provider results before this can accept cash.

## Validation

The included tests exercise the actual frontend and backend with simulated Tk, GPIO, time, servo outputs and coin-exit events; the current production config has the coin-exit sensor disabled. They cover complete bill and coin exchanges, wrong bills, ambiguous groups, excess deposits, blocked demo hooks, duplicate confirmation, sensor failures, timeouts, restart protection, asynchronous payout, startup without optional fonts, curve transparency and antialiasing, bounded image caching, spinner image updates, shared transaction numbering, restart persistence, legacy-log import, concurrent number allocation and reference reuse through retries/incidents. They do not establish electrical safety, mechanical calibration, real GCash transfers or the display's physical appearance.

## Files

- `dmarby.py`: touchscreen frontend and launch modes.
- `assets.py`: original embedded visual assets.
- `dmarby_integration.py`: Tk/backend adapter and asynchronous payout flow.
- `coin_changer_hardware.py`: calibrated inputs, inventory, journal and dispensers.
- `transaction_numbers.py`: persistent shared cash/GCash numbering and transaction history.
- `coin_changer_config.json`: current four-dispenser configuration.
- `inventory.example.json`: zero-count inventory structure.
- `marby_service.py`: audited operator reconciliation.
- `run_marby.sh`: hardware launcher for the integrated frontend.
- `test_marby.py`, `test_dmarby_integration.py`, `test_transaction_numbers.py`: simulated checks.
- `DMARBY_PROJECT_NOTES.md`: project reference and the requested design/portability preferences.

The original README merge-conflict markers and conflicting PHP 20-bill/PHP 2-coin descriptions have been removed.
