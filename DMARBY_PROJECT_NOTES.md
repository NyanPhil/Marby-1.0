# D’Marby project reference

Updated: 31 October 2026 (Philippines)

D’Marby is an 800 × 480 Tkinter touchscreen coin exchanger for a Raspberry Pi with a DSI display. The attached project is the working reference. Its main files are `dmarby.py`, `assets.py`, `dmarby_integration.py`, `coin_changer_hardware.py`, `coin_changer_config.json`, `marby_service.py` and the accompanying launcher and tests.

## User requirements

- Keep the application portable: missing Plus Jakarta Sans must not produce an error or warning. Use an installed system font or Tk's default without requiring font files, installation, downloads or font-cache utilities.
- Use a professional D’Marby logo with solid, extra-bold lettering and balanced spacing.
- Add a subtle shadow behind the D’Marby lettering and coin emblem.
- Make “Welcome to” larger and place it close to the logo.
- Retain these project details for future work. This file records that reference; it is not a confirmation that ChatGPT long-term Memory has been updated.

## Current implementation

- The welcome screen uses a blue-and-gold coin/exchange emblem and a measured, centred D’MARBY wordmark, drawn natively on the Tk canvas. A small matching canvas stroke gives the lettering a solid extra-bold weight without requiring a special font.
- Soft layered shadows behind the lettering and emblem add depth using native canvas shapes and text, with no extra assets or dependencies.
- The 36-pixel greeting shares the logo's white panel and sits directly above it.
- The font selector quietly checks installed sans-serif families and falls back to the actual family of `TkDefaultFont`.
- Demo mode is the default. Hardware mode is selected with `--hardware`; `--windowed` gives an 800 × 480 window.
- Supported bills are PHP 50 and 100; coins are PHP 1, 5, 10 and 20. GCash remains a simulated demo flow and is unavailable in hardware mode.
- Backend wiring, calibration settings, cash handling, inventory and journal behavior are unchanged by this visual/font update. The uploaded configuration's verification flags remain false.
- Upload suffixes such as `(1)` are removed from filenames in the complete package so Python imports and documented commands resolve correctly.

## Updating the machine

For the requested UI fix, replace only `dmarby.py` in the existing project. Preserve the machine's existing configuration, counted inventory, transaction journal and customer logs. The package omits the uploaded `transactions.log`.

Run `python3 dmarby.py --demo --windowed` to inspect the welcome screen. Run `python3 -m unittest -v test_marby.py test_dmarby_integration.py` for the simulated checks.
