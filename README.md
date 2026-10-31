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

For an existing installation, replace `dmarby.py` alongside your current `assets.py` and backend modules. Keep your machine's current configuration, inventory, transaction journal and logs. The complete package uses the canonical module filenames without upload suffixes such as `(1)`.

## Hardware settings to confirm

GPIO numbers are BCM numbers. Never connect acceptor 12 V signals directly to Raspberry Pi GPIO. Keep the tested isolated acceptor pulse wiring and externally powered servo wiring.

| Function | BCM GPIO | Physical pin |
|---|---:|---:|
| Allan coin pulses | 17 | 11 |
| TB74 bill pulses | 27 | 13 |
| Coin-exit sensor | 22 | 15 |
| TB74 acceptance control | 23 | 16 |
| Allan acceptance control — reserved until established | 24 | 18 |
| PCA9685 SDA | 2 | 3 |
| PCA9685 SCL | 3 | 5 |

The software cannot provide physical acceptance control through an unidentified or unwired inhibit input. Keep the verification flags false until both acceptors reject cash when disabled and preserve the complete current pulse group.

### Four payout dispensers

This frontend uses PHP 1, 5, 10 and 20 coins. The previous backend's fifth, 25-centavo dispenser remains supported for other configurations, but is not enabled by this frontend's configuration.

**The denomination-to-channel assignments below are proposed defaults.** The known socket numbers are 8, 10, 12 and 14; the physical coin tube attached to each has not been confirmed. Correct the `servos` entries to match your machine.

| Payout coin | Proposed PCA9685 socket |
|---:|---:|
| PHP 1 | 8 |
| PHP 5 | 10 |
| PHP 10 | 12 |
| PHP 20 | 14 |

Each dispenser has independent `min_pulse_us`, `max_pulse_us`, `actuation_range`, `home_angle`, `release_angle`, `hold_seconds` and `settle_seconds` settings. The supplied 1000–2000 microsecond range and 0/65-degree commands are starting values, not measured dispensing positions. Calibrate each gate to release exactly one coin. The reported differences in servos 2 and 3 have not been diagnosed or corrected by guessing new limits.

A common coin-exit sensor must detect coins from all four tubes. The UI advances only after a servo cycle reports exactly one detected exit. A missing or extra exit stops the payout.

### Pulse mappings

| Acceptor | Pulse count | Amount |
|---|---:|---:|
| TB74 | 5 | PHP 50 |
| TB74 | 10 | PHP 100 |
| Allan | 1 / 5 / 10 / 20 | PHP 1 / 5 / 10 / 20 |

Bills use a 3-second quiet period and coins use 2 seconds after the last transition, with the signal returned HIGH. Only one bill is allowed per exchange. Exchange totals are limited to PHP 100. A 21-pulse bill group is not rounded or split into guessed bills.

## Load inventory and enable hardware mode

1. If no inventory file exists, create `inventory.json` using the structure in `inventory.example.json`. Enter physically counted coin quantities. Preserve any existing inventory file and reconcile it against the machine.
2. Check the proposed channel mapping, pulse maps, both acceptance-control circuits and every dispenser. Set only the corresponding `inhibit_verified` and `servo_verified` flags to true after those checks pass.
3. Set `contact_number` to the staff contact shown on incident screens.
4. Run the tests, then launch hardware mode:

```bash
.venv/bin/python -m unittest -v test_marby.py test_dmarby_integration.py
.venv/bin/python dmarby.py --hardware --windowed
```

For fullscreen, omit `--windowed`. You can choose another configuration with `--config /absolute/path/machine.json`. Inventory and journal paths are resolved relative to that configuration file.

The updated `run_marby.sh` also launches this frontend in hardware mode and writes startup output to `marby.log`. An existing autostart entry using that script will use the integrated application after the files are replaced.

Hardware startup is intentionally blocked by the supplied false verification flags. Changing those flags does not perform the physical checks.

## Connected transaction flow

1. The touchscreen selects an exchange and an exact coin mix.
2. Only the relevant acceptor is enabled on the insert screen, subject to stock checks.
3. The first pulse commands intake closed. Complete calibrated groups are recorded before the credit appears on the UI.
4. The customer confirms the payout. A worker performs one servo cycle at a time while the touchscreen remains responsive.
5. Each confirmed coin updates the journal and inventory once. The collect screen appears after the entire payout is committed.

A different supported bill can be rerouted to a rebuilt mix. An accepted coin that exceeds the selected amount is recorded as a service incident; the screen never falsely claims it was physically returned.

## Recovery

Money left inside after a timeout or application exit stays recorded as owed. A customer-facing screen or demo control cannot clear a real incident.

After counting the actual accepted and dispensed cash, settling the customer's balance, and correcting inventory, record the resolution:

```bash
.venv/bin/python marby_service.py \
  --operator "Technician ID" \
  --reason "Cash counted; customer balance settled; inventory corrected"
```

Enter `RECONCILED` when prompted. This records the resolution; it does not dispense or refund money. Review `uncredited_inputs` as well as `credit`, `received`, `plan` and `dispensed` when present. Preserve `transaction_state.json`, `inventory.json`, `transactions.log` and `reconciliations.jsonl` during updates.

If the kiosk used a custom configuration, pass the same `--config /absolute/path/machine.json` to `marby_service.py`. Its reconciliation log is saved beside that journal.

## GCash status

The supplied frontend uses a timer to simulate a successful cash-in. No real transfer API, authentication, wallet verification or provider transaction callback was supplied. Hardware mode therefore blocks this service before collecting money. A real transfer integration must provide confirmed, recoverable provider results before this can accept cash.

## Validation

The included tests exercise the actual frontend and backend with simulated Tk, GPIO, time, servo outputs and coin-exit events. They cover complete bill and coin exchanges, wrong bills, ambiguous groups, excess deposits, blocked demo hooks, duplicate confirmation, sensor failures, timeouts, restart protection, asynchronous payout, startup without optional fonts, curve transparency and antialiasing, bounded image caching and spinner image updates. They do not establish electrical safety, mechanical calibration, real GCash transfers or the display's physical appearance.

## Files

- `dmarby.py`: touchscreen frontend and launch modes.
- `assets.py`: original embedded visual assets.
- `dmarby_integration.py`: Tk/backend adapter and asynchronous payout flow.
- `coin_changer_hardware.py`: calibrated inputs, inventory, journal and dispensers.
- `coin_changer_config.json`: current four-dispenser configuration.
- `inventory.example.json`: zero-count inventory structure.
- `marby_service.py`: audited operator reconciliation.
- `run_marby.sh`: hardware launcher for the integrated frontend.
- `test_marby.py`, `test_dmarby_integration.py`: simulated checks.
- `DMARBY_PROJECT_NOTES.md`: project reference and the requested design/portability preferences.

The original README merge-conflict markers and conflicting PHP 20-bill/PHP 2-coin descriptions have been removed.
