"""D'MARBY 800 x 480 Tkinter kiosk.

Demo:     python dmarby.py --demo --windowed
Hardware: python dmarby.py --hardware --windowed

Hardware mode uses dmarby_integration.py and coin_changer_hardware.py.
It requires verified acceptance control, calibrated dispensers and a coin-exit
sensor. Bill pulses: 5=PHP 50, 10=PHP 100; coin pulses: 1/5/10/20=PHP 1/5/10/20.
GCash is simulated in demo mode and unavailable for real cash until a real
transfer integration is supplied. Hardware transactions never auto-forfeit
accepted cash on timeout. Existing transaction logs are append-only.
"""

import json
import math
import os
import shutil
import subprocess
import sys
import time
import tkinter as tk
from collections import Counter
from tkinter import font as tkfont
from tkinter import messagebox

from assets import CURRENCY_PICTURES, IMAGES, SHADOWS, FADES

# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

APP_NAME = "D' MARBY"
W, H = 800, 480
FULLSCREEN = "--windowed" not in sys.argv
ALLOW_ESCAPE = True
DEMO_MODE = True                 # Default; --hardware selects the real backend.
HARDWARE_MODE = False
CONTACT_NUMBER = "09XX XXX XXXX"  # shown on incident screens

BILLS = [50, 100]
COINS = [20, 10, 5, 1]
START_STOCK = {20: 60, 10: 80, 5: 100, 1: 200}
GCASH_START_BALANCE = 2000
GCASH_FEE_RATE = 0.05
GCASH_MAX = 5000

CHECK_BILL_MS = 1500             # "Checking your bill" overlay
CHECK_COIN_MS = 450              # quick "Counting your coin" flash
COIN_DROP_MS = 140               # simulated hopper speed
CASHING_MS = 2500                # simulated GCash transfer
IDLE_EMPTY_S = 45                # no taps, no money -> prompt
IDLE_MONEY_S = 75                # no taps, money inside -> prompt
PROMPT_S = 15                    # countdown on both prompts
DONE_S = 8
AUTO_HOME_S = 60                 # incident and out-of-coins screens
ADMIN_TAPS = 5

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "transactions.log")

# Colours
WHITE = "#FFFFFF"
INK = "#0F1B4D"
GREY = "#5B6B8C"
LIGHT = "#E3E8F2"
BLUE = "#1D3FBF"
SOFT = "#B9C6EE"
CHIP = "#22388F"
TAB_OFF = "#2B47B5"
TAB_OFF_TEXT = "#8FA3E6"
TINT = "#EEF2FB"
GREEN = "#16A34A"
RED = "#DC2626"
AMBER = "#F59E0B"
AMBER_TEXT = "#92400E"
AMBER_SOFT = "#FEF3C7"
OFF_GREY = "#8A94AD"
PRESS_TINT = "#D6E0FA"     # white buttons while pressed
PRESS_BLUE = "#142C8E"     # blue buttons while pressed
PRESS_KEY = "#CBD6F2"      # keypad keys and light pills while pressed

# Cebuano strings: add "English text": "Cebuano text" pairs here.
# Anything missing falls back to English, so the app always works.
CEBUANO = {
}

FONT_NAME = "Plus Jakarta Sans"
FONT_FALLBACK = "DejaVu Sans"
FONT_FAMILY = FONT_FALLBACK
FONT_FILES = ("PlusJakartaSans-Regular.ttf", "PlusJakartaSans-Bold.ttf")


def install_bundled_fonts():
    """Make ./fonts available to Tk. Must run before tk.Tk()."""
    paths = [os.path.join(BASE_DIR, "fonts", n) for n in FONT_FILES]
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return
    try:
        if sys.platform.startswith("win"):
            import ctypes
            for p in paths:
                ctypes.windll.gdi32.AddFontResourceExW(p, 0x10, 0)
            return
        target = os.path.expanduser("~/Library/Fonts" if sys.platform == "darwin"
                                    else "~/.local/share/fonts")
        os.makedirs(target, exist_ok=True)
        copied = False
        for p in paths:
            dst = os.path.join(target, os.path.basename(p))
            if not os.path.exists(dst):
                shutil.copy2(p, dst)
                copied = True
        if copied and sys.platform.startswith("linux"):
            subprocess.run(["fc-cache", "-f", target], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        print(f"Could not install fonts: {error}", file=sys.stderr)


def money(v):
    return f"₱{v:,}"


def plural(n, word="coin"):
    return f"{n} {word}{'' if n == 1 else 's'}"


def describe(plan):
    parts = []
    for coin in sorted(plan, reverse=True):
        n = plan[coin]
        parts.append(money(coin) if n == 1 and len(plan) > 1 else f"{n} × {money(coin)}")
    return " + ".join(parts)


def fewest_plan(amount, stock):
    """Exact payout with the fewest coins the hoppers can cover, or None."""
    best = None
    for n20 in range(min(stock.get(20, 0), amount // 20) + 1):
        r20 = amount - 20 * n20
        for n10 in range(min(stock.get(10, 0), r20 // 10) + 1):
            r10 = r20 - 10 * n10
            for n5 in range(min(stock.get(5, 0), r10 // 5) + 1):
                n1 = r10 - 5 * n5
                if n1 > stock.get(1, 0):
                    continue
                total = n20 + n10 + n5 + n1
                if best is None or total < best[0]:
                    best = (total, {c: n for c, n in ((20, n20), (10, n10), (5, n5), (1, n1)) if n})
    return best[1] if best else None


def payable(amount, stock):
    return amount > 0 and fewest_plan(amount, stock) is not None


def fits(plan, stock):
    return all(stock.get(c, 0) >= n for c, n in plan.items())


def ref_number():
    return "PX-" + time.strftime("%y%m%d-%H%M%S")


# ----------------------------------------------------------------------
# App
# ----------------------------------------------------------------------

class Kiosk:
    MONEY_IDLE_EXEMPT = ("welcome", "dispensing", "done", "gc_cashing", "gc_success",
                         "incident", "out_of_coins", "admin")

    def __init__(self, root):
        self.root = root
        self.closed = False
        self.lang = "en"
        self.stock = dict(START_STOCK)
        self.gcash_balance = GCASH_START_BALANCE
        self.demo = {"reject": False, "jam": False, "bad_number": False}
        self.fault = None
        self.taps = []
        self.actions = []
        self.overlay = None
        self.screen = "welcome"
        self.screen_since = time.monotonic()
        self.last_input = time.monotonic()
        self.spin_angle = 90
        self.spinner = None
        self.jobs = {}
        self.reset_session()

        global FONT_FAMILY
        FONT_FAMILY = FONT_NAME if FONT_NAME in tkfont.families(root) else FONT_FALLBACK
        if FONT_FAMILY != FONT_NAME:
            print(f"{FONT_NAME} not found, using {FONT_FALLBACK}.", file=sys.stderr)

        root.title("D' MARBY Coin Exchanger")
        root.configure(bg="#0A1A5C")
        if FULLSCREEN:
            root.attributes("-fullscreen", True)
            root.config(cursor="none")
        else:
            root.geometry(f"{W}x{H}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        if ALLOW_ESCAPE:
            root.bind("<Escape>", lambda e: self.close())
        root.bind("<Key>", self.on_key)

        self.img = {k: tk.PhotoImage(master=root, data=v, format="png")
                    for k, v in {**CURRENCY_PICTURES, **IMAGES}.items()}
        self.shadow_img = {k: tk.PhotoImage(master=root, data=v, format="png")
                           for k, v in SHADOWS.items()}
        self.fade_img = {k: tk.PhotoImage(master=root, data=v, format="png")
                         for k, v in FADES.items()}
        self.fonts = {}

        self.c = tk.Canvas(root, width=W, height=H, highlightthickness=0, bd=0, bg="#0A1A5C")
        self.c.place(relx=0.5, rely=0.5, anchor="center")
        self.c.bind("<ButtonPress-1>", self.on_press)
        self.c.bind("<B1-Motion>", self.on_drag)
        self.c.bind("<ButtonRelease-1>", self.on_release)
        self.pressed = None
        self.show("welcome")
        self.tick()

    # ------------------------------------------------------------------
    # Session state
    # ------------------------------------------------------------------

    def reset_session(self):
        self.flow = None          # "bill" | "break" | "combine" | "gcash"
        self.target = 0           # amount the customer said they'd insert
        self.mix = Counter()      # chosen payout (bill / break)
        self.have = Counter()     # coins the customer holds (combine)
        self.combine_choice = 0
        self.plan = {}            # payout to dispense
        self.credit = 0           # money actually inside the machine
        self.inserted = []        # list of ("bill"|"coin", value)
        self.notice = None
        self.rerouted = False     # wrong bill -> rebuilt coin mix
        self.gc_amount_buf = ""
        self.gc_number_buf = ""
        self.gc_amount = 0
        self.gc_fee = 0
        self.gc_reentry = False
        self.gc_ref = ""
        self.dropped = 0
        self.queue = []
        self.done_text = ""

    def tr(self, s):
        return CEBUANO.get(s, s) if self.lang == "ceb" else s

    def log(self, kind, **data):
        record = dict(time=time.strftime("%Y-%m-%d %H:%M:%S"),
                      mode="demo" if DEMO_MODE else "hardware", kind=kind, **data)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as error:
            print(f"Could not write log: {error}", file=sys.stderr)

    # ------------------------------------------------------------------
    # Drawing primitives
    # ------------------------------------------------------------------

    def font(self, px, bold):
        key = (px, bold)
        if key not in self.fonts:
            self.fonts[key] = tkfont.Font(family=FONT_FAMILY, size=-px,
                                          weight="bold" if bold else "normal")
        return self.fonts[key]

    def t(self, x, y, s, px=18, bold=False, fill=INK, anchor="center", width=None):
        kw = dict(text=s, font=self.font(px, bold), fill=fill, anchor=anchor,
                  justify="center" if anchor == "center" else ("right" if anchor.endswith("e") else "left"))
        if width:
            kw["width"] = width
        return self.c.create_text(x, y, **kw)

    def measure(self, s, px, bold):
        return self.font(px, bold).measure(s)

    def rounded(self, x1, y1, x2, y2, r, fill, outline="", width=1):
        r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.c.create_polygon(pts, smooth=True, splinesteps=24, fill=fill,
                                     outline=outline, width=width)

    def pill(self, x1, y1, x2, y2, fill, outline="", width=1):
        """Shape with true semicircular ends."""
        r = (y2 - y1) / 2
        r = min(r, (x2 - x1) / 2)
        pts = []
        for cx, cy, a0 in ((x2 - r, y1 + r, -90), (x2 - r, y2 - r, 0),
                           (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
            for k in range(13):
                a = math.radians(a0 + k * 7.5)
                pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return self.c.create_polygon(pts, fill=fill, outline=outline, width=width)

    def card(self, x1, y1, x2, y2, r=24, enabled=True, kind="card"):
        """White card with a soft blurred shadow toward the bottom right."""
        w, h = int(x2 - x1), int(y2 - y1)
        key = f"{kind}:{w}x{h}:{r}"
        _collect(key)
        shadow = self.shadow_img.get(key)
        if shadow is not None:
            dx, dy = (9, 12) if kind == "card" else (8, 12)
            self.c.create_image(x1 - 30 + dx, y1 - 30 + dy, image=shadow, anchor="nw")
        shape = self.rounded(x1, y1, x2, y2, r, WHITE)
        if not enabled:
            fade = self.fade_img.get(f"{w}x{h}:{r}")
            return shape, (lambda: fade is not None and
                           self.c.create_image(x1, y1, image=fade, anchor="nw"))
        return shape, (lambda: None)

    def picture(self, name, x, y):
        if name in self.img:
            self.c.create_image(x, y, image=self.img[name])

    def coin(self, v, x, y, s=64):
        self.picture(f"coin_{v}_{s}", x, y)

    def note(self, v, x, y, s=96):
        self.picture(f"note_{v}_{s}", x, y)

    def title(self, s, y=72, px=36):
        self.t(W / 2, y, self.tr(s), px, True, WHITE)

    def chip(self, label, y=82):
        w = self.measure(label, 15, True) + 34
        self.pill(W / 2 - w / 2, y, W / 2 + w / 2, y + 28, CHIP)
        self.t(W / 2, y + 14, label, 15, True, WHITE)

    def arrow_badge(self, x, y, r=18):
        self.c.create_oval(x - r, y - r, x + r, y + r, fill=BLUE, outline="")
        self.c.create_line(x - 7, y, x + 7, y, fill=WHITE, width=2.5, capstyle="round")
        self.c.create_line(x + 2, y - 5, x + 7, y, x + 2, y + 5, fill=WHITE,
                           width=2.5, capstyle="round", joinstyle="round")

    def status(self, x, y, ok, label=None):
        label = self.tr(label or ("Available" if ok else "Not available now"))
        colour = GREEN if ok else OFF_GREY
        w = self.measure(label, 15, True) + 16
        left = x - w / 2
        self.c.create_oval(left, y - 4, left + 8, y + 4, fill=colour, outline="")
        self.t(left + 16, y, label, 15, True, colour, "w")

    def alert(self, x, y, r, colour, ring):
        self.c.create_oval(x - r - 10, y - r - 10, x + r + 10, y + r + 10, fill=ring, outline="")
        self.c.create_oval(x - r, y - r, x + r, y + r, fill=colour, outline="")
        self.c.create_line(x, y - r * 0.5, x, y + r * 0.12, fill=WHITE,
                           width=max(3, r * 0.17), capstyle="round")
        d = max(2.5, r * 0.11)
        self.c.create_oval(x - d, y + r * 0.38 - d, x + d, y + r * 0.38 + d, fill=WHITE, outline="")

    def check_mark(self, x, y, r, colour, ring):
        self.c.create_oval(x - r - 10, y - r - 10, x + r + 10, y + r + 10, fill=ring, outline="")
        self.c.create_oval(x - r, y - r, x + r, y + r, fill=colour, outline="")
        s = r / 30
        self.c.create_line(x - 13 * s, y + 1 * s, x - 3 * s, y + 11 * s, x + 14 * s, y - 10 * s,
                           fill=WHITE, width=5 * s, capstyle="round", joinstyle="round")

    def bar(self, x1, x2, y, frac, thick=12):
        self.pill(x1, y - thick / 2, x2, y + thick / 2, LIGHT)
        if frac > 0:
            self.pill(x1, y - thick / 2, x1 + max(thick, (x2 - x1) * min(1, frac)),
                      y + thick / 2, BLUE)

    def spinner_ring(self, x, y, r, width=10):
        self.c.create_oval(x - r, y - r, x + r, y + r, outline=LIGHT, width=width)
        self.spinner = self.c.create_arc(x - r, y - r, x + r, y + r, start=self.spin_angle,
                                         extent=-110, style="arc", outline=BLUE, width=width)
        if "spin" not in self.jobs:
            self.jobs["spin"] = self.root.after(50, self.spin)

    def countdown_ring(self, x, y, r, left, total):
        self.c.create_oval(x - r, y - r, x + r, y + r, outline=LIGHT, width=8)
        if left > 0:
            self.c.create_arc(x - r, y - r, x + r, y + r, start=90,
                              extent=-359.9 * min(1, left / total), style="arc",
                              outline=BLUE, width=8)
        self.t(x, y, str(max(0, math.ceil(left))), 24, True, INK)

    # Buttons ----------------------------------------------------------

    def act(self, rect, command, press=None):
        """Register a tap target. `press` draws its pressed look (default: card sink)."""
        self.actions.append((rect, command, press))

    def press_card(self, x1, y1, x2, y2, r=24):
        """Cards sink a little toward their shadow and get a blue edge."""
        for item in self.c.find_enclosed(x1 - 2, y1 - 2, x2 + 2, y2 + 2):
            self.c.move(item, 2, 3)
        self.rounded(x1 + 2, y1 + 3, x2 + 2, y2 + 3, r, "", BLUE, 3)

    def edge(self, side, label, command=None, enabled=True, w=None):
        """ATM-style white pill flush with a screen edge."""
        label = self.tr(label)
        w = w or max(168, self.measure(label, 20, True) + 80)
        y, h = 392, 52
        if side == "left":
            x1, x2, cx = -40, w, w / 2 + 2
        else:
            x1, x2, cx = W - w, W + 40, W - w / 2 - 2
        self.pill(x1, y, x2, y + h, WHITE if enabled else TAB_OFF)
        self.t(cx, y + h / 2, label, 20, True, BLUE if enabled else TAB_OFF_TEXT)

        def press():
            self.pill(x1, y, x2, y + h, BLUE)
            self.t(cx, y + h / 2, label, 20, True, WHITE)
        if enabled and command:
            self.act((max(0, x1), y, min(W, x2), y + h), command, press)

    def back(self, command, enabled=True):
        self.edge("left", "‹  Back", command, enabled, w=168)

    def blue_button(self, cx, y, label, command, w=220):
        def draw(fill):
            self.pill(cx - w / 2, y, cx + w / 2, y + 48, fill)
            self.t(cx, y + 24, self.tr(label), 19, True, WHITE)
        draw(BLUE)
        self.act((cx - w / 2, y, cx + w / 2, y + 48), command, lambda: draw(PRESS_BLUE))

    def small_pill(self, x1, y1, x2, y2, label, command, enabled=True):
        def draw(fill, ink):
            self.pill(x1, y1, x2, y2, fill)
            self.t((x1 + x2) / 2, (y1 + y2) / 2, self.tr(label), 15, True, ink)
        draw(TINT if enabled else "#F4F6FA", BLUE if enabled else "#B9C1D4")
        if enabled:
            self.act((x1, y1, x2, y2), command, lambda: draw(BLUE, WHITE))

    def stepper(self, x, y, sign, enabled, command):
        def draw(fill, ink, outline, r=18):
            self.c.create_oval(x - r, y - r, x + r, y + r, fill=fill, outline=outline, width=2)
            self.c.create_line(x - 7, y, x + 7, y, fill=ink, width=3, capstyle="round")
            if sign == "+":
                self.c.create_line(x, y - 7, x, y + 7, fill=ink, width=3, capstyle="round")
        if sign == "+":
            style = (BLUE, WHITE, "") if enabled else (LIGHT, "#A7B1C8", "")
            pressed = (PRESS_BLUE, WHITE, PRESS_BLUE)
        else:
            style = (WHITE, BLUE, BLUE) if enabled else ("#EEF1F7", "#B9C1D4", "")
            pressed = (BLUE, WHITE, BLUE)
        draw(*style)
        if enabled:
            # Pressed: filled and a touch bigger so it shows around the fingertip.
            self.act((x - 24, y - 24, x + 24, y + 24), command, lambda: draw(*pressed, r=21))

    def tap_card(self, x1, y1, x2, y2, command, footer=True):
        """Divider + 'Tap to start' + arrow, and make the card tappable."""
        if footer:
            self.c.create_line(x1 + 26, y2 - 58, x2 - 26, y2 - 58, fill=LIGHT, width=2)
            self.t(x1 + 26, y2 - 30, self.tr("Tap to start"), 18, True, BLUE, "w")
            self.arrow_badge(x2 - 44, y2 - 30)
        self.act((x1, y1, x2, y2), command, lambda: self.press_card(x1, y1, x2, y2))

    # ------------------------------------------------------------------
    # Screen management
    # ------------------------------------------------------------------

    def show(self, screen, overlay=None):
        if self.closed:
            return
        if screen != self.screen:
            self.screen_since = time.monotonic()
        self.screen = screen
        self.overlay = overlay
        self.redraw()

    def redraw(self):
        self.c.delete("all")
        self.actions = []
        self.spinner = None
        self.c.create_image(0, 0, image=self.img["bg"], anchor="nw")
        getattr(self, "s_" + self.screen)()
        if self.overlay:
            self.actions = []
            self.c.create_image(0, 0, image=self.img["dim"], anchor="nw")
            getattr(self, "o_" + self.overlay["kind"])()

    def go(self, screen):
        return lambda: self.show(screen)

    # ------------------------------------------------------------------
    # Welcome and menus
    # ------------------------------------------------------------------

    def s_welcome(self):
        self.reset_session()
        self.coin(20, 38, 32, 32)
        self.t(64, 32, "D' MARBY Coin Exchanger", 19, False, WHITE, "w")
        self.t(W / 2, 130, self.tr("Welcome to"), 28, False, SOFT)

        # A native-canvas wordmark keeps the logo sharp on the touchscreen.
        self.rounded(126, 188, 686, 332, 32, "#112875")
        self.rounded(120, 176, 680, 320, 32, WHITE, "#DCE5FA", 1)
        self.c.create_oval(148, 203, 236, 291, fill=TINT, outline="")
        self.c.create_oval(161, 224, 213, 276, fill="#F6DE98", outline="#D8B35A", width=2)
        self.c.create_oval(174, 212, 226, 264, fill="#FFD976", outline="#D2A343", width=2)
        self.t(200, 239, "₱", 32, True, "#805D23")
        self.t(446, 232, "D’MARBY", 61, True, BLUE)
        self.t(446, 279, "COIN EXCHANGE", 14, True, GREY)
        self.edge("left", "Contact us", self.go("contact"), w=212)
        self.edge("right", "Start", self.go("exchange_type"), w=212)

    def pick_lang(self, lang):
        """Kept for the Cebuano option later; the welcome screen now just says Start."""
        self.lang = lang
        self.show("exchange_type")

    def s_contact(self):
        x1, y1, x2, y2 = 185, 70, 615, 340
        self.card(x1, y1, x2, y2)
        cx = W / 2
        # phone icon: handset drawn in a blue circle
        self.c.create_oval(cx - 32, y1 + 24, cx + 32, y1 + 88, fill=TINT, outline="")
        self.c.create_oval(cx - 22, y1 + 34, cx + 22, y1 + 78, fill=BLUE, outline="")
        self.c.create_line(cx - 9, y1 + 46, cx - 9, y1 + 52, cx - 2, y1 + 62, cx + 6, y1 + 68,
                           cx + 11, y1 + 68, fill=WHITE, width=5, capstyle="round", joinstyle="round",
                           smooth=True)
        self.t(cx, y1 + 120, self.tr("Need help?"), 30, True, INK)
        self.t(cx, y1 + 156, self.tr("Call or text the D' MARBY team at"), 16, True, GREY)
        self.t(cx, y1 + 194, CONTACT_NUMBER, 30, True, BLUE)
        self.c.create_line(x1 + 34, y1 + 222, x2 - 34, y1 + 222, fill=LIGHT, width=2)
        self.t(cx, y1 + 246, self.tr("Have your reference number ready if you have one."), 14, False, GREY)
        self.back(self.go("welcome"))

    def s_exchange_type(self):
        self.title("What are you exchanging?")
        x1, y1, x2, y2 = 30, 126, 386, 366
        self.card(x1, y1, x2, y2)
        self.t(x1 + 26, y1 + 36, self.tr("Cash"), 28, True, INK, "w")
        self.t(x1 + 26, y1 + 68, self.tr("Exchange bills or coins."), 16, False, GREY, "w")
        self.note(50, x1 + 92, y1 + 120)
        self.note(100, x1 + 130, y1 + 134)
        self.coin(10, x1 + 240, y1 + 128)
        self.coin(20, x1 + 290, y1 + 128)
        self.tap_card(x1, y1, x2, y2, self.start_cash)
        x1, x2 = 414, 770
        self.card(x1, y1, x2, y2)
        self.t(x1 + 26, y1 + 36, "GCash", 28, True, INK, "w")
        self.t(x1 + 26, y1 + 68, self.tr("Cash in to your GCash wallet."), 16, False, GREY, "w")
        self.picture("gcash_big", (x1 + x2) / 2, y1 + 128)
        self.tap_card(x1, y1, x2, y2, self.start_gcash)
        self.back(self.go("welcome"))

    def start_cash(self):
        if not any(payable(v, self.stock) for v in (5, 10, 20, 50, 100)):
            self.show("out_of_coins")
        else:
            self.show("choose_tx")

    def s_choose_tx(self):
        self.title("Choose a transaction")
        x1, y1, x2, y2 = 30, 126, 386, 366
        self.card(x1, y1, x2, y2)
        self.t(x1 + 26, y1 + 36, self.tr("Bill to coins"), 26, True, INK, "w")
        self.t(x1 + 26, y1 + 66, self.tr("Insert a bill, pick the coins you need."), 15, False, GREY, "w")
        for i, v in enumerate(BILLS):
            x = (x1 + x2) / 2 + (i - (len(BILLS) - 1) / 2) * 114
            self.note(v, x, y1 + 118)
            self.t(x, y1 + 160, money(v), 19, True, INK)
        self.tap_card(x1, y1, x2, y2, self.go("pick_bill"))
        x1, x2 = 414, 770
        self.card(x1, y1, x2, y2)
        self.t(x1 + 26, y1 + 36, self.tr("Coin to coins"), 26, True, INK, "w")
        self.t(x1 + 26, y1 + 66, self.tr("Break ₱5 or ₱10, or combine coins."), 15, False, GREY, "w")
        for i, v in enumerate(sorted(COINS)):
            x = x1 + 52 + i * 84
            self.coin(v, x, y1 + 114)
            self.t(x, y1 + 164, money(v), 19, True, INK)
        self.tap_card(x1, y1, x2, y2, self.go("coin_mode"))
        self.back(self.go("exchange_type"))

    def s_coin_mode(self):
        self.title("Coin to coins")
        cards = [(30, "Break coins", "Trade a coin for smaller ones.", ([10], [5, 5]), "pick_coin"),
                 (414, "Combine coins", "Trade small coins for a bigger one.", ([5, 5], [10]), "have_coins")]
        for x1, name, desc, (src, dst), nxt in cards:
            x2, y1, y2 = x1 + 356, 126, 366
            self.card(x1, y1, x2, y2)
            self.t(x1 + 26, y1 + 36, self.tr(name), 28, True, INK, "w")
            self.t(x1 + 26, y1 + 68, self.tr(desc), 16, False, GREY, "w")
            cx, cy = (x1 + x2) / 2, y1 + 124
            for group, centre in ((src, cx - 104), (dst, cx + 104)):
                for k, v in enumerate(group):
                    self.coin(v, centre + (k - (len(group) - 1) / 2) * 50, cy)
            self.c.create_line(cx - 22, cy, cx + 20, cy, fill=BLUE, width=4, capstyle="round")
            self.c.create_line(cx + 10, cy - 10, cx + 21, cy, cx + 10, cy + 10, fill=BLUE,
                               width=4, capstyle="round", joinstyle="round")
            self.tap_card(x1, y1, x2, y2, self.go(nxt))
        self.back(self.go("choose_tx"))

    def s_out_of_coins(self):
        x1, y1, x2, y2 = 130, 90, 670, 390
        self.card(x1, y1, x2, y2)
        cx = W / 2
        self.alert(cx, y1 + 66, 24, AMBER, AMBER_SOFT)
        self.t(cx, y1 + 140, self.tr("Out of coins for now"), 34, True, INK)
        self.t(cx, y1 + 190, self.tr("This machine can't give change right now.\nPlease try again later."),
               19, False, GREY)
        self.c.create_line(x1 + 40, y1 + 236, x2 - 40, y1 + 236, fill=LIGHT, width=2)
        self.t(cx, y1 + 266, self.tr("No money has been taken."), 19, True, BLUE)
        self.act((0, 0, W, H), self.go("welcome"), lambda: None)

    # ------------------------------------------------------------------
    # Picking an amount
    # ------------------------------------------------------------------

    def three_cards(self, title, kind, values, available, on_pick, captions=None):
        self.title(title, 74)
        start = (W - (236 * len(values) + 16 * (len(values) - 1))) / 2
        for i, v in enumerate(values):
            ok = available(v)
            x1 = start + i * 252
            x2, y1, y2 = x1 + 236, 128, 356
            _, fade = self.card(x1, y1, x2, y2, enabled=ok)
            cx = (x1 + x2) / 2
            if kind == "note":
                self.note(v, cx, y1 + 66, 146)
            else:
                self.coin(v, cx, y1 + 60, 84)
            self.t(cx, y1 + (124 if captions else 136), money(v), 34, True, INK)
            if captions:
                self.t(cx, y1 + 156, self.tr(captions[v]), 15, True, GREY)
            self.c.create_line(x1 + 26, y1 + 180, x2 - 26, y1 + 180, fill=LIGHT, width=2)
            if not ok:
                fade()
            self.status(cx, y1 + 204, ok)
            if ok:
                self.act((x1, y1, x2, y2), lambda v=v: on_pick(v),
                         lambda a=(x1, y1, x2, y2): self.press_card(*a))

    def s_pick_bill(self):
        self.three_cards("Which bill will you insert?", "note", BILLS,
                         lambda v: payable(v, self.stock), self.pick_bill)
        self.back(self.go("choose_tx"))

    def pick_bill(self, v):
        self.flow, self.target, self.mix = "bill", v, Counter()
        self.show("build_mix")

    def smaller_stock(self, v):
        return {c: n for c, n in self.stock.items() if c < v}

    def s_pick_coin(self):
        self.three_cards("Which coin will you insert?", "coin", [5, 10, 20],
                         lambda v: payable(v, self.smaller_stock(v)), self.pick_coin)
        self.back(self.go("coin_mode"))

    def pick_coin(self, v):
        self.flow, self.target, self.mix = "break", v, Counter()
        self.show("build_mix")

    # ------------------------------------------------------------------
    # Build your coin mix (bill and break)
    # ------------------------------------------------------------------

    def mix_state(self, coin):
        if self.flow == "break" and coin >= self.target:
            return "Too big"
        if self.flow == "bill" and coin > self.target:
            return "Too big"
        if self.stock.get(coin, 0) == 0:
            return "Out of stock"
        return None

    def can_add(self, coin):
        remaining = self.target - sum(c * n for c, n in self.mix.items())
        return (self.mix_state(coin) is None and remaining >= coin
                and self.mix[coin] < self.stock.get(coin, 0))

    def adjust(self, coin, delta):
        if delta > 0 and not self.can_add(coin):
            return
        if delta < 0 and self.mix[coin] == 0:
            return
        self.mix[coin] += delta
        self.redraw()

    def s_build_mix(self):
        self.t(W / 2, 56, self.tr("Build your coin mix"), 34, True, WHITE)
        label = (f"Changing a {money(self.target)} bill" if self.flow == "bill"
                 else f"Breaking {money(self.target)}")
        self.chip(self.tr(label))
        total = sum(c * n for c, n in self.mix.items())
        remaining = self.target - total
        for i, coin in enumerate(COINS):
            x1 = 30 + (i % 2) * 252
            y1 = 128 + (i // 2) * 122
            x2, y2 = x1 + 238, y1 + 110
            state = self.mix_state(coin)
            _, fade = self.card(x1, y1, x2, y2, r=20, enabled=state is None)
            n = self.mix[coin]
            self.coin(coin, x1 + 46, y1 + 55)
            self.t(x1 + 92, y1 + 24, money(coin), 22, True, INK, "w")
            self.t(x1 + 92, y1 + 48, self.tr(state) if state else f"{self.tr('Subtotal')} {money(coin * n)}",
                   14, True, GREY, "w")
            self.stepper(x1 + 112, y1 + 82, "-", state is None and n > 0, lambda c=coin: self.adjust(c, -1))
            self.t(x1 + 158, y1 + 82, str(n), 22, True, INK)
            self.stepper(x1 + 204, y1 + 82, "+", self.can_add(coin), lambda c=coin: self.adjust(c, 1))
            if state:
                fade()
        x1, y1, x2, y2 = 540, 128, 770, 360
        self.card(x1, y1, x2, y2, r=20)
        cx = (x1 + x2) / 2
        self.t(cx, y1 + 26, self.tr("Selected"), 16, True, GREY)
        self.t(cx, y1 + 68, f"{money(total)} / {money(self.target)}", 30, True, INK)
        self.bar(x1 + 24, x2 - 24, y1 + 108, total / self.target, 10)
        self.t(cx, y1 + 134, f"{plural(sum(self.mix.values()))} {self.tr('selected')}", 15, False, GREY)
        hint = self.tr("Exact match") if remaining == 0 else f"{self.tr('Add')} {money(remaining)} {self.tr('more')}"
        self.t(cx, y1 + 164, hint, 17, True, GREEN if remaining == 0 else BLUE)
        can_fill = 0 < remaining <= self.stock.get(1, 0) - self.mix[1] and self.mix_state(1) is None
        self.small_pill(x1 + 16, y1 + 188, cx - 5, y1 + 220, "Clear", self.clear_mix, total > 0)
        self.small_pill(cx + 5, y1 + 188, x2 - 16, y1 + 220, "Fill with ₱1", self.fill_mix, can_fill)
        if self.credit:
            self.edge("right", "Review  →", self.to_review, remaining == 0)
        else:
            back = "pick_bill" if self.flow == "bill" else "pick_coin"
            self.back(self.go(back))
            label = "Insert bill  →" if self.flow == "bill" else "Insert coins  →"
            self.edge("right", label, self.to_insert, remaining == 0)

    def clear_mix(self):
        self.mix = Counter()
        self.redraw()

    def fill_mix(self):
        remaining = self.target - sum(c * n for c, n in self.mix.items())
        self.mix[1] += remaining
        self.redraw()

    def to_insert(self):
        if self.flow in ("bill", "break"):
            self.plan = dict(+self.mix)
        self.notice = None
        self.show("insert")

    def to_review(self):
        if self.flow in ("bill", "break"):
            self.plan = dict(+self.mix)
        self.show("review")

    # ------------------------------------------------------------------
    # Combine coins
    # ------------------------------------------------------------------

    def combine_options(self):
        total = sum(c * n for c, n in self.have.items())
        count = sum(self.have.values())
        if total == 0:
            return []
        candidates = []
        for c in (20, 10, 5):
            if total % c == 0:
                candidates.append({c: total // c})
        best = fewest_plan(total, self.stock)
        if best:
            candidates.append(best)
        seen, options = set(), []
        for plan in candidates:
            key = tuple(sorted(plan.items()))
            if key in seen or sum(plan.values()) >= count or plan == dict(+self.have):
                continue
            seen.add(key)
            options.append((plan, fits(plan, self.stock)))
        options.sort(key=lambda o: (not o[1], sum(o[0].values())))
        return options[:3]

    def adjust_have(self, coin, delta):
        total = sum(c * n for c, n in self.have.items())
        if delta > 0 and total + coin > 100:
            return
        if delta < 0 and self.have[coin] == 0:
            return
        self.have[coin] += delta
        self.redraw()

    def s_have_coins(self):
        self.flow = "combine"
        self.t(W / 2, 56, self.tr("What coins do you have?"), 34, True, WHITE)
        self.chip(self.tr("Combine coins"))
        total = sum(c * n for c, n in self.have.items())
        for i, coin in enumerate((1, 5, 10, 20)):
            x1 = 30 + (i % 2) * 252
            y1 = 128 + (i // 2) * 122
            x2, y2 = x1 + 238, y1 + 110
            ok = coin != 20
            _, fade = self.card(x1, y1, x2, y2, r=20, enabled=ok)
            n = self.have[coin]
            self.coin(coin, x1 + 46, y1 + 55)
            self.t(x1 + 92, y1 + 24, money(coin), 22, True, INK, "w")
            self.t(x1 + 92, y1 + 48, f"{self.tr('Subtotal')} {money(coin * n)}" if ok
                   else self.tr("Already the biggest"), 14, True, GREY, "w")
            self.stepper(x1 + 112, y1 + 82, "-", ok and n > 0, lambda c=coin: self.adjust_have(c, -1))
            self.t(x1 + 158, y1 + 82, str(n), 22, True, INK)
            self.stepper(x1 + 204, y1 + 82, "+", ok and total + coin <= 100,
                         lambda c=coin: self.adjust_have(c, 1))
            if not ok:
                fade()
        x1, y1, x2, y2 = 540, 128, 770, 360
        self.card(x1, y1, x2, y2, r=20)
        cx = (x1 + x2) / 2
        self.t(cx, y1 + 26, self.tr("You have"), 16, True, GREY)
        self.t(cx, y1 + 74, money(total), 44, True, INK)
        self.t(cx, y1 + 118, plural(sum(self.have.values())), 16, False, GREY)
        self.c.create_line(x1 + 22, y1 + 146, x2 - 22, y1 + 146, fill=LIGHT, width=2)
        options = [o for o, ok in self.combine_options() if ok]
        self.t(cx, y1 + 172, self.tr("Can become"), 15, True, GREY)
        text = f" {self.tr('or')} ".join(describe(o) for o in options[:2]) if options else "-"
        self.t(cx, y1 + 200, text, 16, True, BLUE, width=200)
        self.back(self.go("coin_mode"))
        self.edge("right", "Next  →", self.to_combine_choice, bool(options))

    def to_combine_choice(self):
        self.combine_choice = 0
        self.show("combine_choice")

    def s_combine_choice(self):
        total = sum(c * n for c, n in self.have.items())
        self.t(W / 2, 56, self.tr("How do you want them combined?"), 34, True, WHITE)
        self.chip(f"{self.tr('You have')} {money(total)} {self.tr('in')} {plural(sum(self.have.values()))}")
        options = self.combine_options()
        n = len(options)
        gap = 20
        cw = 280 if n <= 2 else 236
        start = (W - (cw * n + gap * (n - 1))) / 2
        for i, (plan, ok) in enumerate(options):
            x1 = start + i * (cw + gap)
            x2, y1, y2 = x1 + cw, 132, 360
            _, fade = self.card(x1, y1, x2, y2, enabled=ok)
            cx = (x1 + x2) / 2
            coins = [c for c in sorted(plan, reverse=True) for _ in range(min(plan[c], 2))][:2]
            for k, c in enumerate(coins):
                self.coin(c, cx + (k - (len(coins) - 1) / 2) * 60, y1 + 62, 84)
            self.t(cx, y1 + 132, describe(plan), 28 if len(plan) == 1 else 22, True, INK)
            self.t(cx, y1 + 164, plural(sum(plan.values())), 16, True, GREY)
            self.c.create_line(x1 + 26, y1 + 186, x2 - 26, y1 + 186, fill=LIGHT, width=2)
            if not ok:
                fade()
            self.status(cx, y1 + 208, ok)
            if ok:
                self.act((x1, y1, x2, y2), lambda i=i: self.choose_combine(i),
                         lambda a=(x1, y1, x2, y2): self.press_card(*a))
                if i == self.combine_choice:
                    self.rounded(x1 - 3, y1 - 3, x2 + 3, y2 + 3, 26, "", BLUE, 4)
                    self.c.create_oval(x2 - 26, y1 - 14, x2 + 6, y1 + 18, fill=BLUE, outline=WHITE, width=3)
                    self.c.create_line(x2 - 17, y1 + 2, x2 - 12, y1 + 7, x2 - 3, y1 - 3, fill=WHITE,
                                       width=3, capstyle="round", joinstyle="round")
        chosen = options[self.combine_choice] if self.combine_choice < n else None
        if self.credit:
            self.edge("right", "Review  →", self.to_review_combine, bool(chosen and chosen[1]))
        else:
            self.back(self.go("have_coins"))
            self.edge("right", "Insert coins  →", self.start_combine_insert, bool(chosen and chosen[1]))

    def choose_combine(self, i):
        self.combine_choice = i
        self.redraw()

    def start_combine_insert(self):
        options = self.combine_options()
        self.plan = dict(options[self.combine_choice][0])
        self.target = sum(c * n for c, n in self.have.items())
        self.notice = None
        self.show("insert")

    def to_review_combine(self):
        options = self.combine_options()
        self.plan = dict(options[self.combine_choice][0])
        self.show("review")

    # ------------------------------------------------------------------
    # Insert money (all flows)
    # ------------------------------------------------------------------

    def accepts(self):
        """Which kinds of money the insert screen accepts right now."""
        if self.screen != "insert" or self.overlay:
            return set()
        if self.flow == "bill":
            return {"bill"} if self.credit == 0 else set()
        if self.flow in ("break", "combine"):
            return {"coin"} if self.credit < self.target else set()
        if self.flow == "gcash":
            return {"bill", "coin"} if self.credit < self.target else set()
        return set()

    def s_insert(self):
        gc = self.flow == "gcash"
        label = {"bill": "Insert a bill", "gcash": "Insert money"}.get(self.flow, "Insert coins")
        self.title(label, 70 if gc else 74, 38 if gc else 40)
        if gc:
            x1, y1, x2, y2 = 170, 112, 630, 362
        else:
            x1, y1, x2, y2 = 140, 126, 660, 358
        self.card(x1, y1, x2, y2)
        cx = W / 2
        if gc:
            self.t(x1 + 30, y1 + 34, self.tr("Cash in"), 17, True, GREY, "w")
            self.picture("gcash_logo", x2 - 80, y1 + 34)
            top = y1 + 90
            big_px, small_px = 54, 28
        else:
            self.t(cx, y1 + 28, self.tr("Inserted"), 17, True, GREY)
            top = y1 + 82
            big_px, small_px = 66, 34
        big, small = money(self.credit), f" / {money(self.target)}"
        wb, ws = self.measure(big, big_px, True), self.measure(small, small_px, True)
        left = cx - (wb + ws) / 2
        self.t(left, top, big, big_px, True, INK, "w")
        self.t(left + wb, top + (10 if gc else 12), small, small_px, True, "#9AA5BF", "w")
        counting = getattr(self, "counting_cash", False)
        if counting:
            sub, colour = self.tr("Counting your money. Please wait."), BLUE
        elif self.notice:
            sub, colour = self.notice, RED
        elif self.flow == "bill":
            sub = (f"{self.tr('Waiting for your')} {money(self.target)} {self.tr('bill')}"
                   if not self.credit else self.tr("Bill accepted"))
            colour = GREY
        elif gc:
            sub, colour = self.tr("Bills or coins"), GREY
        else:
            n = sum(1 for k, _ in self.inserted if k == "coin")
            sub, colour = (plural(n) if n else self.tr("No coins yet")), GREY
        self.t(cx, top + (40 if gc else 48), sub, 16 if gc else 17, bool(self.notice), colour)
        by = top + (72 if gc else 92)
        self.bar(x1 + (34 if gc else 50), x2 - (34 if gc else 50), by, self.credit / self.target,
                 12 if gc else 14)
        self.t(x1 + (34 if gc else 50), by + 24, "₱0", 14 if gc else 15, True, GREY, "w")
        self.t(x2 - (34 if gc else 50), by + 24, money(self.target), 14 if gc else 15, True, INK, "e")
        if gc:
            ny = y2 - 32
            self.pill(x1 + 22, ny - 17, x2 - 22, ny + 17, TINT)
            self.c.create_oval(x1 + 36, ny - 9, x1 + 54, ny + 9, fill=BLUE, outline="")
            self.t(x1 + 45, ny, "i", 13, True, WHITE)
            self.t(x1 + 64, ny, f"{self.tr('Includes 5% fee:')} {money(self.gc_amount)} + {money(self.gc_fee)} "
                   f"{self.tr('fee')}", 15, True, INK, "w")
        if self.credit == 0 and not counting:
            prev = {"bill": "build_mix", "break": "build_mix", "combine": "combine_choice",
                    "gcash": "gc_number"}[self.flow]
            self.back(self.go(prev))
        done = self.credit >= self.target and not counting
        if gc:
            self.edge("right", "Cash in  →", self.start_cashing, done)
        else:
            self.edge("right", "Review  →", self.to_review_from_insert, done)
        if DEMO_MODE:
            self.demo_insert_row()

    def demo_insert_row(self):
        kinds = self.accepts()
        if not kinds:
            return
        items = []
        if "coin" in kinds:
            items += [("coin", v) for v in (1, 5, 10, 20)]
        if "bill" in kinds:
            items += [("bill", v) for v in BILLS]
        labels = []
        for kind, v in items:
            labels.append((kind, v, money(v) if kind == "coin" else f"{money(v)} bill"))
        widths = [self.measure(lbl, 12, True) + 16 for _, _, lbl in labels]
        total = sum(widths) + 6 * (len(widths) - 1) + 42
        x = W / 2 - total / 2
        self.t(x, 462, "Demo", 12, True, SOFT, "w")
        x += 42
        for (kind, v, label), w in zip(labels, widths):
            self.pill(x, 451, x + w, 473, CHIP)
            self.t(x + w / 2, 462, label, 12, True, WHITE)
            cmd = (lambda v=v: self.coin_inserted(v, None)) if kind == "coin" else (lambda v=v: self.bill_inserted(v))
            press = (lambda x=x, w=w, label=label: (self.pill(x, 451, x + w, 473, WHITE),
                                                     self.t(x + w / 2, 462, label, 12, True, BLUE)))
            self.act((x, 448, x + w, 476), cmd, press)
            x += w + 6

    def to_review_from_insert(self):
        self.show("review")

    # Hardware entry points -------------------------------------------

    def bill_inserted(self, value):
        if "bill" not in self.accepts():
            return False
        self.notice = None
        self.show("insert", {"kind": "check", "what": "bill", "value": value})
        if DEMO_MODE:
            ok = not self.demo["reject"]
            self.demo["reject"] = False
            self.jobs["check"] = self.root.after(CHECK_BILL_MS, lambda: self.bill_checked(value, ok))
        return True

    def bill_checked(self, value, ok):
        self.jobs.pop("check", None)
        if not (self.overlay and self.overlay.get("kind") == "check"):
            return False
        self.overlay = None
        if not ok:
            self.notice = self.tr("That bill wasn't accepted. Try another one.")
            self.show("insert")
            return False
        if self.flow == "gcash":
            if self.credit + value > self.target:
                self.notice = self.tr("That bill is more than what's left. It was returned.")
                self.show("insert")
                return False
            self.add_credit("bill", value)
            self.show("insert")
            return True
        # Bill to coins
        self.add_credit("bill", value)
        if value == self.target:
            self.show("insert")
        elif payable(value, self.stock):
            self.wrong = (self.target, value)
            self.show("wrong_bill")
        else:
            self.incident("cant_change", value)
        return True

    def coin_inserted(self, value, ok=True):
        if "coin" not in self.accepts():
            return False
        if ok is None:  # demo button
            ok = not self.demo["reject"]
            self.demo["reject"] = False
        self.notice = None
        if not ok:
            self.notice = self.tr("That coin was returned. Try another one.")
            self.show("insert")
            return False
        if self.credit + value > self.target:
            self.notice = self.tr("That coin would go over. It was returned.")
            self.show("insert")
            return False
        self.add_credit("coin", value)
        self.show("insert", {"kind": "check", "what": "coin", "value": value})
        self.jobs["check"] = self.root.after(CHECK_COIN_MS, self.close_check)
        return True

    def close_check(self):
        self.jobs.pop("check", None)
        if self.overlay and self.overlay.get("kind") == "check":
            self.show(self.screen)

    def add_credit(self, kind, value):
        self.credit += value
        self.inserted.append((kind, value))
        self.last_input = time.monotonic()

    def s_wrong_bill(self):
        picked, value = self.wrong
        x1, y1, x2, y2 = 150, 40, 650, 364
        self.card(x1, y1, x2, y2)
        cx = W / 2
        self.note(value, cx, y1 + 74, 146)
        bx, by = cx + 70, y1 + 40
        self.c.create_oval(bx - 18, by - 18, bx + 18, by + 18, fill=AMBER, outline=WHITE, width=3)
        self.c.create_line(bx, by - 8, bx, by + 2, fill=WHITE, width=4, capstyle="round")
        self.c.create_oval(bx - 2.5, by + 6, bx + 2.5, by + 11, fill=WHITE, outline="")
        self.t(cx, y1 + 154, f"{self.tr('You inserted a')} {money(value)} {self.tr('bill')}", 30, True, INK)
        self.t(cx, y1 + 190, f"{self.tr('Not the')} {money(picked)} {self.tr('you picked.')}", 17, True, "#B45309")
        self.c.create_line(x1 + 40, y1 + 222, x2 - 40, y1 + 222, fill=LIGHT, width=2)
        self.t(cx, y1 + 266, self.tr("We can't give bills back, so let's turn") +
               f"\n{self.tr('your')} {money(value)} " + self.tr("into coins instead."), 16, True, BLUE)
        self.edge("right", "Choose coins  →", self.reroute_bill, w=280)

    def reroute_bill(self):
        self.target = self.credit
        self.mix = Counter()
        self.rerouted = True
        self.show("build_mix")

    # ------------------------------------------------------------------
    # Review, dispense, done
    # ------------------------------------------------------------------

    def s_review(self):
        self.t(W / 2, 56, self.tr("Review your exchange"), 34, True, WHITE)
        self.chip(self.tr({"bill": "Bill to coins", "break": "Break coins",
                           "combine": "Combine coins"}[self.flow]))
        x1, y1, x2, y2 = 90, 128, 710, 362
        self.card(x1, y1, x2, y2)
        mid = (x1 + x2) / 2
        lx, rx = (x1 + mid) / 2 - 10, (mid + x2) / 2 + 10
        self.t(lx, y1 + 30, self.tr("You put in"), 16, True, GREY)
        self.t(rx, y1 + 30, self.tr("You get"), 16, True, GREY)
        bills = [v for k, v in self.inserted if k == "bill"]
        coins_in = Counter(v for k, v in self.inserted if k == "coin")
        if bills:
            self.note(bills[0], lx, y1 + 92, 96)
            put_label, put_sub = money(self.credit), self.tr("1 bill") if len(bills) == 1 else plural(len(bills), "bill")
        else:
            self.coin_group(coins_in, lx, y1 + 92)
            put_label, put_sub = money(self.credit), plural(sum(coins_in.values()))
        self.t(lx, y1 + 160, put_label, 30, True, INK)
        self.t(lx, y1 + 190, put_sub, 15, True, GREY)
        self.c.create_oval(mid - 24, y1 + 68, mid + 24, y1 + 116, fill=TINT, outline="")
        self.c.create_line(mid - 10, y1 + 92, mid + 10, y1 + 92, fill=BLUE, width=3.5, capstyle="round")
        self.c.create_line(mid + 2, y1 + 84, mid + 10, y1 + 92, mid + 2, y1 + 100, fill=BLUE,
                           width=3.5, capstyle="round", joinstyle="round")
        self.coin_group(Counter(self.plan), rx, y1 + 92)
        self.t(rx, y1 + 160, describe(self.plan), 26 if len(self.plan) < 3 else 20, True, INK)
        self.t(rx, y1 + 190, plural(sum(self.plan.values())), 15, True, GREY)
        change = "build_mix" if self.flow in ("bill", "break") else "combine_choice"
        self.edge("left", "‹  Change", self.go(change), w=168)
        self.edge("right", "Confirm  →", self.start_dispense)

    def coin_group(self, counts, x, y):
        coins = sorted(counts, reverse=True)[:2]
        if not coins:
            return
        if len(coins) == 1:
            self.coin(coins[0], x, y, 84)
            n = counts[coins[0]]
        else:
            self.coin(coins[0], x - 34, y, 84)
            self.coin(coins[1], x + 40, y + 6, 64)
            n = counts[coins[1]]
        if n > 1:
            bx = x + (40 if len(coins) > 1 else 30)
            label = f"× {n}"
            w = self.measure(label, 14, True) + 18
            self.pill(bx + 12, y + 22, bx + 12 + w, y + 48, BLUE)
            self.t(bx + 12 + w / 2, y + 35, label, 14, True, WHITE)

    def start_dispense(self):
        if not fits(self.plan, self.stock) or sum(c * n for c, n in self.plan.items()) != self.credit:
            self.incident("cant_change", self.credit)
            return
        self.queue = [c for c in sorted(self.plan, reverse=True) for _ in range(self.plan[c])]
        self.dropped = 0
        self.show("dispensing")
        if DEMO_MODE:
            self.jobs["drop"] = self.root.after(COIN_DROP_MS, self.demo_drop)

    def demo_drop(self):
        self.jobs.pop("drop", None)
        if self.screen != "dispensing":
            return
        if self.demo["jam"] and self.dropped >= max(1, len(self.queue) // 2):
            self.demo["jam"] = False
            self.payout_fault()
            return
        self.coin_dropped(self.queue[self.dropped])

    def coin_dropped(self, value):
        """Hardware hook: one hopper pulse."""
        if self.screen != "dispensing" or self.dropped >= len(self.queue):
            return False
        if value != self.queue[self.dropped]:
            self.payout_fault()
            return False
        self.stock[value] = max(0, self.stock.get(value, 0) - 1)
        self.dropped += 1
        self.redraw()
        if self.dropped == len(self.queue):
            self.jobs["drop"] = self.root.after(500, self.finish_dispense)
        elif DEMO_MODE:
            self.jobs["drop"] = self.root.after(COIN_DROP_MS, self.demo_drop)
        return True

    def payout_fault(self):
        """Hardware hook: a hopper jammed or timed out."""
        if self.screen != "dispensing":
            return False
        paid = sum(self.queue[:self.dropped])
        self.incident("jam", self.credit - paid)
        return True

    def finish_dispense(self):
        self.jobs.pop("drop", None)
        self.log("exchange", flow=self.flow, amount=self.credit, plan=self.plan)
        self.done_text = (f"{money(self.credit)} {self.tr('exchanged into')} "
                          f"{plural(sum(self.plan.values()))}")
        self.show("done")

    def s_dispensing(self):
        self.t(W / 2, 64, self.tr("Dispensing your coins"), 36, True, WHITE)
        x1, y1, x2, y2 = 150, 112, 650, 400
        self.card(x1, y1, x2, y2)
        cx = W / 2
        self.picture("tray", cx, y1 + 92)
        total = max(1, len(self.queue))
        self.t(cx, y1 + 190, f"{self.dropped} {self.tr('of')} {plural(len(self.queue))}", 26, True, INK)
        self.bar(x1 + 50, x2 - 50, y1 + 226, self.dropped / total)
        self.t(cx, y1 + 258, self.tr("Please wait, coins are dropping into the tray."), 16, True, GREY)

    def s_done(self):
        x1, y1, x2, y2 = 150, 40, 650, 362
        self.card(x1, y1, x2, y2)
        cx = W / 2
        self.picture("done", cx, y1 + 90)
        self.t(cx, y1 + 190, self.tr("Please take your coins"), 32, True, INK)
        self.t(cx, y1 + 230, self.done_text, 18, True, BLUE)
        self.c.create_line(x1 + 40, y1 + 262, x2 - 40, y1 + 262, fill=LIGHT, width=2)
        self.t(cx, y1 + 290, self.tr("Thank you for using D' MARBY!"), 16, True, GREY)
        left = max(0, math.ceil(DONE_S - (time.monotonic() - self.screen_since)))
        self.t(140, 418, f"{self.tr('Back to start in')} {left}s", 16, True, SOFT)
        self.edge("right", "New exchange  →", self.go("welcome"), w=250)

    # ------------------------------------------------------------------
    # Incidents (machine keeps money, customer contacts staff)
    # ------------------------------------------------------------------

    def incident(self, kind, amount):
        for job in ("drop", "check"):
            if job in self.jobs:
                self.root.after_cancel(self.jobs.pop(job))
        ref = ref_number()
        self.log("incident", type=kind, owed=amount, flow=self.flow, inserted=self.inserted, ref=ref)
        self.incident_info = dict(kind=kind, amount=amount, ref=ref,
                                  bill=next((v for k, v in self.inserted if k == "bill"), None))
        if kind == "jam":
            self.fault = self.incident_info
        self.show("incident", None)

    def s_incident(self):
        info = self.incident_info
        x1, y1, x2, y2 = 185, 50, 615, 346
        self.card(x1, y1, x2, y2)
        cx = W / 2
        if info["kind"] == "cant_change" and info["bill"]:
            self.note(info["bill"], cx, y1 + 48, 96)
            bx, by = cx + 46, y1 + 26
            self.c.create_oval(bx - 14, by - 14, bx + 14, by + 14, fill=RED, outline=WHITE, width=3)
            self.c.create_line(bx, by - 6, bx, by + 2, fill=WHITE, width=3.5, capstyle="round")
            self.c.create_oval(bx - 2, by + 5, bx + 2, by + 9, fill=WHITE, outline="")
            head = self.tr("We can't change") + f" {money(info['amount'])} " + self.tr("right now")
            sub = self.tr("Not enough coins in the machine.")
            recorded = f"{self.tr('Your')} {money(info['amount'])} {self.tr('is recorded. Contact us at')}"
        elif info["kind"] == "hardware":
            self.alert(cx, y1 + 48, 22, RED, "#FEE2E2")
            head = self.tr("Service required")
            sub = (f"Recorded balance: {money(info['amount'])}" if info["amount"]
                   else self.tr("Please ask staff to check your cash."))
            recorded = self.tr("This is recorded. Contact us at")
        else:
            self.alert(cx, y1 + 48, 22, RED, "#FEE2E2")
            head = self.tr("Coins got stuck")
            sub = f"{self.tr('We still owe you')} {money(info['amount'])}."
            recorded = self.tr("This is recorded. Contact us at")
        self.t(cx, y1 + 108, head, 24, True, INK)
        self.t(cx, y1 + 138, sub, 15, True, RED)
        self.c.create_line(x1 + 34, y1 + 162, x2 - 34, y1 + 162, fill=LIGHT, width=2)
        self.t(cx, y1 + 186, recorded, 15, True, GREY)
        self.t(cx, y1 + 216, CONTACT_NUMBER, 24, True, BLUE)
        self.pill(cx - 128, y1 + 242, cx + 128, y1 + 272, TINT)
        self.t(cx, y1 + 257, f"Ref  {info['ref']}", 15, True, INK)
        self.t(140 if info["kind"] not in ("jam", "hardware") else W / 2, 418, self.tr("Take a photo of this screen."),
               16, True, SOFT, "center")
        if info["kind"] not in ("jam", "hardware"):
            self.edge("right", "Done  →", self.go("welcome"), w=200)

    # ------------------------------------------------------------------
    # GCash
    # ------------------------------------------------------------------

    def s_gc_unavailable(self):
        self.title("GCash is unavailable", 100, 34)
        self.t(W / 2, 230, "Please choose a cash exchange or contact kiosk staff.",
               20, True, WHITE, width=650)
        self.back(self.go("exchange_type"))

    def start_gcash(self):
        self.reset_session()
        self.flow = "gcash"
        self.show("gc_amount")

    def keypad(self, on_digit, on_clear, on_back):
        x1, y1, x2, y2 = 414, 112, 770, 364
        self.card(x1, y1, x2, y2)
        kw, kh, gap = 100, 50, 10
        left = x1 + (x2 - x1 - (3 * kw + 2 * gap)) / 2
        top = y1 + (y2 - y1 - (4 * kh + 3 * gap)) / 2
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "Clear", "0", "⌫"]
        for i, k in enumerate(keys):
            kx = left + (i % 3) * (kw + gap)
            ky = top + (i // 3) * (kh + gap)
            self.draw_key(k, kx, ky, kw, kh, TINT)
            if k == "Clear":
                cmd = on_clear
            elif k == "⌫":
                cmd = on_back
            else:
                cmd = lambda d=k: on_digit(d)
            self.act((kx, ky, kx + kw, ky + kh), cmd,
                     lambda k=k, kx=kx, ky=ky: self.draw_key(k, kx, ky, kw, kh, PRESS_KEY, sink=2))

    def draw_key(self, k, kx, ky, kw, kh, fill, sink=0):
        kx, ky = kx, ky + sink
        self.rounded(kx, ky, kx + kw, ky + kh, 14, fill)
        if k == "Clear":
            self.t(kx + kw / 2, ky + kh / 2, self.tr("Clear"), 17, True, BLUE)
        elif k == "⌫":
            bx, by = kx + kw / 2, ky + kh / 2
            self.c.create_polygon(bx - 16, by, bx - 8, by - 10, bx + 14, by - 10, bx + 14, by + 10,
                                  bx - 8, by + 10, fill="", outline=INK, width=2.5, joinstyle="round")
            self.c.create_line(bx - 2, by - 5, bx + 8, by + 5, fill=INK, width=2.5, capstyle="round")
            self.c.create_line(bx - 2, by + 5, bx + 8, by - 5, fill=INK, width=2.5, capstyle="round")
        else:
            self.t(kx + kw / 2, ky + kh / 2, k, 24, True, INK)

    def s_gc_amount(self):
        self.title("How much do you want to cash in?", 70, 34)
        x1, y1, x2, y2 = 30, 112, 386, 364
        self.card(x1, y1, x2, y2)
        self.t(x1 + 26, y1 + 34, self.tr("Amount to cash in"), 16, False, GREY, "w")
        self.picture("gcash_logo", x2 - 80, y1 + 34)
        amount = int(self.gc_amount_buf or 0)
        self.t(x1 + 24, y1 + 106, money(amount), 60, True, INK if amount else "#B8C2D9", "w")
        self.c.create_line(x1 + 26, y1 + 152, x2 - 26, y1 + 152, fill=LIGHT, width=2)
        if amount:
            fee = math.ceil(amount * GCASH_FEE_RATE)
            self.t(x1 + 26, y1 + 176, f"+ {money(fee)} {self.tr('fee (5%)')}", 15, True, GREY, "w")
        self.rounded(x1 + 22, y1 + 194, x2 - 22, y1 + 238, 14, AMBER_SOFT)
        ax, ay = x1 + 44, y1 + 216
        self.c.create_polygon(ax, ay - 11, ax + 12, ay + 10, ax - 12, ay + 10, fill=AMBER,
                              outline=AMBER, width=2, joinstyle="round")
        self.t(ax, ay + 2, "!", 12, True, WHITE)
        self.t(ax + 22, ay, self.tr("Proof of concept, for research only."), 14, True, AMBER_TEXT, "w")
        self.keypad(self.gc_amount_digit, self.gc_amount_clear, self.gc_amount_back)
        self.back(self.go("exchange_type"))
        self.edge("right", "Continue  →", self.gc_amount_next, 0 < amount <= GCASH_MAX)

    def gc_amount_digit(self, d):
        nxt = (self.gc_amount_buf + d).lstrip("0")
        if nxt and int(nxt) <= GCASH_MAX:
            self.gc_amount_buf = nxt
        self.redraw()

    def gc_amount_clear(self):
        self.gc_amount_buf = ""
        self.redraw()

    def gc_amount_back(self):
        self.gc_amount_buf = self.gc_amount_buf[:-1]
        self.redraw()

    def gc_amount_next(self):
        amount = int(self.gc_amount_buf or 0)
        if amount > self.gcash_balance:
            self.show("gc_amount", {"kind": "gc_balance", "amount": amount})
            return
        self.gc_amount = amount
        self.gc_fee = math.ceil(amount * GCASH_FEE_RATE)
        self.show("gc_number")

    def s_gc_number(self):
        self.title("What's your GCash number?", 70, 34)
        x1, y1, x2, y2 = 30, 112, 386, 364
        self.card(x1, y1, x2, y2)
        self.t(x1 + 26, y1 + 34, self.tr("GCash number"), 16, False, GREY, "w")
        self.picture("gcash_logo", x2 - 80, y1 + 34)
        digits = self.gc_number_buf
        x = x1 + 30
        for i in range(11):
            if i in (4, 7):
                x += 14
            if i < len(digits):
                self.t(x + 8, y1 + 100, digits[i], 26, True, INK)
            else:
                self.c.create_oval(x + 2, y1 + 94, x + 14, y1 + 106, fill="#C5CEE0", outline="")
            x += 24
        self.t(x1 + 26, y1 + 142, self.tr("Mobile number linked to your GCash"), 15, False, GREY, "w")
        self.rounded(x1 + 22, y1 + 180, x2 - 22, y1 + 232, 14, TINT)
        self.t(x1 + 42, y1 + 206, self.tr("Cash in amount"), 16, False, GREY, "w")
        self.t(x2 - 42, y1 + 206, money(self.gc_amount), 22, True, INK, "e")
        self.keypad(self.gc_number_digit, self.gc_number_clear, self.gc_number_back)
        if not self.credit:
            self.back(self.go("gc_amount"))
        valid = len(digits) == 11 and digits.startswith("09")
        self.edge("right", "Continue  →", self.gc_number_next, valid)

    def gc_number_digit(self, d):
        if len(self.gc_number_buf) < 11:
            self.gc_number_buf += d
        self.redraw()

    def gc_number_clear(self):
        self.gc_number_buf = ""
        self.redraw()

    def gc_number_back(self):
        self.gc_number_buf = self.gc_number_buf[:-1]
        self.redraw()

    def gc_number_next(self):
        if self.gc_reentry and self.credit >= self.target:
            self.start_cashing()
            return
        self.target = self.gc_amount + self.gc_fee
        self.notice = None
        self.show("insert")

    def masked(self):
        n = self.gc_number_buf
        return f"{n[:4]} *** {n[7:]}"

    def spaced(self):
        n = self.gc_number_buf
        return f"{n[:4]} {n[4:7]} {n[7:]}"

    def start_cashing(self):
        self.show("gc_cashing")
        bad = self.demo["bad_number"]
        self.demo["bad_number"] = False
        self.jobs["cash"] = self.root.after(CASHING_MS, lambda: self.cashing_result(not bad))

    def cashing_result(self, ok):
        self.jobs.pop("cash", None)
        if self.screen != "gc_cashing":
            return
        if ok:
            self.gcash_balance -= self.gc_amount
            self.gc_ref = time.strftime("%H%M %Y %m%d")
            self.log("gcash", amount=self.gc_amount, fee=self.gc_fee, number=self.masked(), ref=self.gc_ref)
            self.show("gc_success")
        else:
            self.show("gc_error")

    def gc_card(self, y1=40, y2=370):
        x1, x2 = 130, 670
        self.card(x1, y1, x2, y2)
        self.t(x1 + 30, y1 + 34, self.tr("Cash in"), 17, True, GREY, "w")
        self.picture("gcash_logo", x2 - 80, y1 + 34)
        return x1, y1, x2, y2

    def s_gc_cashing(self):
        x1, y1, x2, y2 = self.gc_card(60, 400)
        cx = W / 2
        self.spinner_ring(cx, y1 + 132, 46)
        self.t(cx, y1 + 222, f"{self.tr('Cashing in')} {money(self.gc_amount)}", 30, True, INK)
        self.t(cx, y1 + 258, f"{self.tr('Sending to GCash')} {self.masked()}", 17, True, BLUE)
        self.c.create_line(x1 + 40, y1 + 286, x2 - 40, y1 + 286, fill=LIGHT, width=2)
        self.t(cx, y1 + 312, self.tr("This might take a while. Please don't leave yet."), 16, False, GREY)

    def s_gc_success(self):
        x1, y1, x2, y2 = self.gc_card()
        cx = W / 2
        self.check_mark(cx, y1 + 118, 30, GREEN, "#DCFCE7")
        self.t(cx, y1 + 198, self.tr("Cash in successful"), 30, True, INK)
        self.t(cx, y1 + 234, f"{money(self.gc_amount)} {self.tr('sent to GCash')} {self.masked()}", 17, True, BLUE)
        self.c.create_line(x1 + 40, y1 + 256, x2 - 40, y1 + 256, fill=LIGHT, width=2)
        self.t(cx, y1 + 280, f"Ref. No. {self.gc_ref}", 18, True, INK)
        self.t(cx, y1 + 304, f"{self.tr('Fee paid:')} {money(self.gc_fee)}. {self.tr('Take a photo of this screen.')}",
               15, False, GREY)
        left = max(0, math.ceil(10 - (time.monotonic() - self.screen_since)))
        self.t(140, 418, f"{self.tr('Back to start in')} {left}s", 16, True, SOFT)
        self.edge("right", "Done  →", self.go("welcome"), w=200)

    def s_gc_error(self):
        x1, y1, x2, y2 = self.gc_card()
        cx = W / 2
        self.alert(cx, y1 + 112, 28, RED, "#FEE2E2")
        self.t(cx, y1 + 186, self.tr("Number not found"), 30, True, INK)
        self.t(cx, y1 + 222, f"{self.spaced()} {self.tr("isn't on GCash.")}", 17, True, RED)
        self.c.create_line(x1 + 40, y1 + 254, x2 - 40, y1 + 254, fill=LIGHT, width=2)
        self.t(cx, y1 + 282, f"{self.tr('Your')} {money(self.credit)} {self.tr('is safe. Check the number and try again.')}",
               16, True, BLUE)
        self.edge("right", "Re-enter GCash number  →", self.reenter_number, w=330)

    def reenter_number(self):
        self.gc_reentry = True
        self.gc_number_buf = ""
        self.show("gc_number")

    # ------------------------------------------------------------------
    # Overlays
    # ------------------------------------------------------------------

    def overlay_card(self, x1, y1, x2, y2):
        self.card(x1, y1, x2, y2, r=28, kind="overlay")

    def o_check(self):
        o = self.overlay
        self.overlay_card(230, 130, 570, 350)
        cx = W / 2
        self.spinner_ring(cx, 202, 36, 8)
        title = "Checking your bill" if o["what"] == "bill" else "Counting your coin"
        self.t(cx, 278, self.tr(title), 24, True, INK)
        self.t(cx, 314, f"{money(o['value'])} {self.tr(o['what'])}", 18, True, BLUE)

    def o_timeout_empty(self):
        self.overlay_card(190, 90, 610, 390)
        cx = W / 2
        left = self.overlay["deadline"] - time.monotonic()
        self.countdown_ring(cx, 156, 36, left, PROMPT_S)
        self.t(cx, 228, self.tr("Are you still there?"), 26, True, INK)
        self.t(cx, 266, self.tr("No money was inserted.\nGoing back to the start soon."), 16, False, GREY)
        self.blue_button(cx, 312, "I'm still here", self.still_here)

    def o_timeout_money(self):
        self.overlay_card(180, 60, 620, 400)
        cx = W / 2
        left = self.overlay["deadline"] - time.monotonic()
        self.countdown_ring(cx, 120, 34, left, PROMPT_S)
        self.t(cx, 186, self.tr("Are you still there?"), 26, True, INK)
        self.t(cx, 220, f"{self.tr('Your')} {money(self.credit)} {self.tr('is still in the machine.')}", 17, True, BLUE)
        msg = self.tr("If time runs out, it can't be returned.")
        fw = self.measure(msg, 15, True)
        self.pill(206, 250, 594, 292, AMBER_SOFT)
        left_x = cx - (fw + 30) / 2
        ix, iy = left_x + 10, 271
        self.c.create_polygon(ix, iy - 10, ix + 11, iy + 9, ix - 11, iy + 9, fill=AMBER,
                              outline=AMBER, width=2, joinstyle="round")
        self.t(ix, iy + 2, "!", 11, True, WHITE)
        self.t(left_x + 30, iy, msg, 15, True, AMBER_TEXT, "w")
        self.blue_button(cx, 316, "I'm still here", self.still_here)

    def o_gc_balance(self):
        self.overlay_card(230, 110, 570, 380)
        cx = W / 2
        self.alert(cx, 170, 24, AMBER, AMBER_SOFT)
        self.t(cx, 232, self.tr("Not enough GCash balance"), 23, True, INK)
        self.t(cx, 264, self.tr("Can't cash in") + " " + money(self.overlay["amount"]) +
               " " + self.tr("right now"), 17, True, BLUE)
        self.t(cx, 290, self.tr("Please try a smaller amount."), 15, False, GREY)
        self.blue_button(cx, 312, "Change amount", lambda: self.show("gc_amount"))

    def still_here(self):
        self.last_input = time.monotonic()
        self.show(self.screen)

    # ------------------------------------------------------------------
    # Kiosk controls (tap the top-left corner of the welcome screen 5x)
    # ------------------------------------------------------------------

    def s_admin(self):
        self.title("Kiosk controls", 52, 30)
        stock = "   ".join(f"{money(c)} × {self.stock.get(c, 0)}" for c in COINS)
        self.t(W / 2, 90, f"Coins: {stock}     GCash float: {money(self.gcash_balance)}", 15, True, SOFT)
        rows = [
            ("Refill all coins", lambda: self.admin_stock("refill"), False),
            ("Empty all coins", lambda: self.admin_stock("empty"), False),
            ("Low on ₱5 coins", lambda: self.admin_stock("low5"), False),
            ("GCash float: low" if self.gcash_balance < 100 else "GCash float: normal",
             self.admin_float, self.gcash_balance < 100),
        ]
        if DEMO_MODE:
            rows += [
                (f"Reject next money: {'On' if self.demo['reject'] else 'Off'}",
                 lambda: self.admin_toggle("reject"), self.demo["reject"]),
                (f"Jam next payout: {'On' if self.demo['jam'] else 'Off'}",
                 lambda: self.admin_toggle("jam"), self.demo["jam"]),
                (f"Next GCash number fails: {'On' if self.demo['bad_number'] else 'Off'}",
                 lambda: self.admin_toggle("bad_number"), self.demo["bad_number"]),
            ]
        if self.fault:
            rows.append((f"Mark jam {self.fault['ref']} fixed", self.admin_fix, True))
        for i, (label, cmd, active) in enumerate(rows):
            x1 = 40 + (i % 2) * 370
            y1 = 112 + (i // 2) * 56
            self.pill(x1, y1, x1 + 350, y1 + 44, BLUE if active else WHITE)
            self.t(x1 + 175, y1 + 22, label, 16, True, WHITE if active else BLUE)
            press = (lambda x1=x1, y1=y1, label=label: (self.pill(x1, y1, x1 + 350, y1 + 44, PRESS_BLUE),
                                                         self.t(x1 + 175, y1 + 22, label, 16, True, WHITE)))
            self.act((x1, y1, x1 + 350, y1 + 44), cmd, press)
        self.edge("left", "Exit app", self.close, w=168)
        self.edge("right", "Back to kiosk  →", self.go("welcome"), w=250)

    def admin_stock(self, change):
        if change == "refill":
            self.stock = dict(START_STOCK)
        elif change == "empty":
            self.stock = {c: 0 for c in COINS}
        else:
            self.stock[5] = min(self.stock.get(5, 0), 2)
        self.redraw()

    def admin_float(self):
        self.gcash_balance = GCASH_START_BALANCE if self.gcash_balance < 100 else 50
        self.redraw()

    def admin_toggle(self, key):
        self.demo[key] = not self.demo[key]
        self.redraw()

    def admin_fix(self):
        self.fault = None
        self.redraw()

    # ------------------------------------------------------------------
    # Input and clock
    # ------------------------------------------------------------------

    # Touch handling: show the pressed look on touch, act on lift-off, and
    # cancel if the finger slides off the button (like a phone).
    PRESS_MIN_MS = 90     # keep the pressed look visible even on very quick taps
    SLOP = 14             # how far a finger may wander before the tap cancels

    def hit(self, x, y):
        for rect, cmd, press in reversed(self.actions):
            x1, y1, x2, y2 = rect
            if x1 <= x <= x2 and y1 <= y <= y2:
                return rect, cmd, press
        return None

    def on_press(self, event):
        self.last_input = time.monotonic()
        if self.screen in ("welcome", "incident") and not self.overlay and event.x < 130 and event.y < 70:
            now = time.monotonic()
            self.taps = [t for t in self.taps if now - t < 3] + [now]
            if len(self.taps) >= ADMIN_TAPS:
                self.taps = []
                self.pressed = None
                self.show("admin")
                return
        else:
            self.taps = []
        if self.pressed:
            return
        target = self.hit(event.x, event.y)
        if not target:
            return
        rect, cmd, press = target
        self.pressed = dict(rect=rect, cmd=cmd, since=time.monotonic(), active=True)
        if press is None:
            x1, y1, x2, y2 = rect
            self.press_card(x1, y1, x2, y2)
        else:
            press()

    def on_drag(self, event):
        p = self.pressed
        if not p or not p["active"]:
            return
        x1, y1, x2, y2 = p["rect"]
        s = self.SLOP
        if not (x1 - s <= event.x <= x2 + s and y1 - s <= event.y <= y2 + s):
            p["active"] = False
            self.redraw()   # finger slid off: drop the pressed look, do nothing

    def on_release(self, event):
        p, self.pressed = self.pressed, None
        if not p:
            return
        if not p["active"]:
            return
        held = (time.monotonic() - p["since"]) * 1000
        wait = max(0, int(self.PRESS_MIN_MS - held))
        self.pressed = dict(p, active=False)   # block new taps until this one lands
        self.root.after(wait, lambda: self.fire(p["cmd"]))

    def fire(self, cmd):
        self.pressed = None
        if self.closed:
            return
        before = self.c.find_all()[-1:] if self.c.find_all() else ()
        cmd()
        after = self.c.find_all()[-1:] if self.c.find_all() else ()
        if before == after:
            self.redraw()   # the action didn't repaint; clear the pressed look

    def on_key(self, event):
        if not DEMO_MODE:
            return
        coins = {"1": 1, "5": 5, "0": 10, "2": 20}
        bills = {"q": 20, "w": 50, "e": 100}
        if event.char in coins:
            self.coin_inserted(coins[event.char], None)
        elif event.char in bills:
            self.bill_inserted(bills[event.char])

    def spin(self):
        self.jobs.pop("spin", None)
        if self.closed or self.spinner is None:
            return
        self.spin_angle = (self.spin_angle - 12) % 360
        try:
            self.c.itemconfigure(self.spinner, start=self.spin_angle)
        except tk.TclError:
            return
        self.jobs["spin"] = self.root.after(50, self.spin)

    def tick(self):
        if self.closed:
            return
        now = time.monotonic()
        idle = now - self.last_input
        on_screen = now - self.screen_since
        o = self.overlay
        if self.fault and self.screen not in ("incident", "admin"):
            self.incident_info = self.fault
            self.show("incident")
        elif o and o["kind"] in ("timeout_empty", "timeout_money"):
            if now >= o["deadline"]:
                if o["kind"] == "timeout_money":
                    self.log("forfeit", amount=self.credit, flow=self.flow, inserted=self.inserted,
                             ref=ref_number())
                self.show("welcome")
            else:
                self.redraw()
        elif not o:
            if self.screen == "done" and on_screen >= DONE_S:
                self.show("welcome")
            elif self.screen == "gc_success" and on_screen >= 10:
                self.show("welcome")
            elif self.screen in ("out_of_coins",) and on_screen >= 8:
                self.show("welcome")
            elif self.screen == "incident" and not self.fault and on_screen >= AUTO_HOME_S:
                self.show("welcome")
            elif self.screen in ("done", "gc_success"):
                self.redraw()  # countdown text
            elif self.credit and self.screen not in self.MONEY_IDLE_EXEMPT and idle >= IDLE_MONEY_S:
                self.show(self.screen, {"kind": "timeout_money", "deadline": now + PROMPT_S})
            elif (not self.credit and self.screen not in ("welcome", "admin", "out_of_coins")
                  and self.screen not in self.MONEY_IDLE_EXEMPT and idle >= IDLE_EMPTY_S):
                self.show(self.screen, {"kind": "timeout_empty", "deadline": now + PROMPT_S})
        self.jobs["tick"] = self.root.after(1000 if not o else 250, self.tick)

    def close(self):
        if self.closed:
            return
        self.closed = True
        for job in self.jobs.values():
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        self.root.destroy()


# Used by tools/make_assets.py to discover every card size the screens use.
_COLLECTED = set()


def _collect(key):
    if os.environ.get("DMARBY_COLLECT"):
        _COLLECTED.add(key)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="D'MARBY touchscreen kiosk")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--hardware", action="store_true", help="use verified GPIO and servo hardware")
    modes.add_argument("--demo", action="store_true", help="simulate money and payouts (default)")
    parser.add_argument("--windowed", action="store_true")
    parser.add_argument("--config", default=os.path.join(BASE_DIR, "coin_changer_config.json"))
    args = parser.parse_args(argv)
    global DEMO_MODE, HARDWARE_MODE, FULLSCREEN, CONTACT_NUMBER
    HARDWARE_MODE = args.hardware
    DEMO_MODE = not args.hardware
    FULLSCREEN = not args.windowed
    install_bundled_fonts()
    root = None
    try:
        root = tk.Tk()
        if args.hardware:
            from dmarby_integration import KioskHardwareMixin, load_config
            cfg = load_config(args.config)
            CONTACT_NUMBER = cfg.get("contact_number", CONTACT_NUMBER)
            class ConnectedKiosk(KioskHardwareMixin, Kiosk):
                pass
            app = ConnectedKiosk(root, cfg)
        else:
            app = Kiosk(root)
        root.mainloop()
        if not app.closed:
            app.close()
        return 0
    except (OSError, ValueError, RuntimeError, ImportError, tk.TclError) as error:
        print(f"Kiosk startup blocked: {error}", file=sys.stderr)
        if root is not None:
            messagebox.showerror("Kiosk startup blocked", str(error), parent=root)
            root.destroy()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
