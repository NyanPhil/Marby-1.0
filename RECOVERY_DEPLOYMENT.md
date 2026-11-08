# D’MARBY — Restartable UI with locked transaction recovery

## What changed

D’MARBY's **hardware UI can launch after an interrupted transaction** without deleting or clearing `transaction_state.json`. If the saved status is `paid`, `dispensing`, or `fault`, the UI immediately displays the existing **Service required** incident with the saved reference and journal-based outstanding balance. Both acceptor control outputs stay disabled, all dispensing remains blocked, and the original journal is not rewritten merely because the UI opens or closes.

The current journal remains authoritative. **Reopening is NOT permission to take new cash**. An operator must inspect the physically accepted and dispensed money, settle any outstanding balance, adjust inventory if necessary, and reconcile the incident before new hardware transactions.

This update keeps the existing verification requirements for acceptor inhibit circuits, pulse mapping, dispenser calibration, and coin-exit sensing. It does not make disconnected inhibit wires safe or change the supported denominations.

## Files in this update

- `coin_changer_hardware.py` — no longer crashes solely because an old journal is unresolved. It instead starts with its `stopping` event set so intake and servo actuation are disabled.
- `dmarby_integration.py` — reopens to the existing incident using the saved credit/reference without writing a new transaction; keeps startup recovery read-only and prevents additional payout. Re-closing a recovered incident leaves the journal as it was.
- `test_dmarby_integration.py` — updated simulated regression tests for fault, paid and dispensing recoveries, as well as a corrected partial-deposit timeout test.

**Do NOT replace** your existing `coin_changer_config.json`, `inventory.json`, `transaction_state.json`, `transactions.log`, `transaction_numbers.sqlite3`, or `reconciliations.jsonl`. Do not erase any customer transaction data.

## Install on the Raspberry Pi

1. Stop the running D’MARBY UI safely while no coin mechanism is moving; disable cash access at the machine before stopping the software. If a service is running, stop the appropriate service first (check its actual name with `systemctl list-units --type=service --all | grep -i marby`).
2. Transfer the 2 modified runtime `.py` files from this update using WinSCP/SFTP or a USB drive into `/home/marby/Marby`. Optionally copy the updated `test_dmarby_integration.py` test file.
3. Before overwriting files, make a backup from the Raspberry Pi terminal:

   ```bash
   cd /home/marby/Marby
   mkdir -p backup-before-recovery-update
   cp -p coin_changer_hardware.py dmarby_integration.py backup-before-recovery-update/
   ```

4. Copy `coin_changer_hardware.py` and `dmarby_integration.py` from the update into this directory (overwriting just those two originals). Do not change `dmarby.py` or your JSON data files for this fix.
5. Run syntax checks and simulated unit tests:

   ```bash
   cd /home/marby/Marby
   .venv/bin/python -m py_compile coin_changer_hardware.py dmarby_integration.py
   .venv/bin/python -m unittest -v test_marby.py test_dmarby_integration.py
   ```

6. From your Raspberry Pi desktop, launch with `.venv/bin/python dmarby.py --hardware --windowed`. From SSH, if the Pi's X11 desktop is logged in and accessible, use:

   ```bash
   cd /home/marby/Marby
   DISPLAY=:0 XAUTHORITY=/home/marby/.Xauthority .venv/bin/python dmarby.py --hardware
   ```

   The X11 command requires an active, authorized graphical session. Fullscreen is the default; add `--windowed` for a window.

## What you should see

- No unfinished journal: normal Welcome screen.
- Journal status `paid`, `dispensing`, or `fault`: **Service required** screen with original reference and outstanding balance (based on journal records). The UI stays in locked recovery. It does not automatically restart dispensing or clear the journal.
- During recovery, the system commands the acceptor-control outputs OFF and blocks servo commands. **This software lock is not a substitute for verified physical inhibit circuits**; if an acceptor can take money while disabled, isolate its power/access and complete electrical safety checks first.
- An actual GPIO/I²C failure or missing verification still prevents hardware mode startup. This patch specifically fixes the journal-related startup block.

## After physical settlement

Inspect accepted bills/coins and coins actually delivered; check the journal's `received`, `credit`, `plan`, `dispensed`, `uncredited_inputs`, and `reference`. Journal-based remaining balance may differ from coins physically dispensed after a power interruption. Settle the discrepancy and update the inventory based on a real count.

Then run the existing supervised reconciliation utility:

```bash
cd /home/marby/Marby
.venv/bin/python marby_service.py --operator "Technician ID" --reason "Cash physically counted, customer balance settled, inventory verified"
```

Enter `RECONCILED` only after the checks actually happened. Then restart D’MARBY; it will be allowed to open a new transaction if all hardware safety checks are satisfied.

If you use `--config /path/to/machine.json` to launch the kiosk, pass that same `--config` argument to `marby_service.py`.

## Testing

The included project tests passed **55 of 55** on a simulated Tkinter/GPIO/servo backend. This is not a test on your actual Raspberry Pi, acceptors, servo gates, or physical cash inventory.
