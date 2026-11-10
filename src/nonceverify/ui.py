"""
The Anti-Klepto Check UI: a small touch interface for the 480x320 panel, drawn with PIL.
Its own look (dark slate, teal accent, monospace labels, shield-and-k emblem), so the
verifier SD can never be mistaken for the signing SD. No SeedSigner GUI code is used;
only the font files shipped in the image are reused, as plain data.

Every screen is a loop: render -> wait for a tap -> return what was chosen.
"""
import os
import time

from PIL import Image, ImageDraw, ImageFont

from nonceverify.hw import HEIGHT, WIDTH, Event

APP_NAME = "ANTI-KLEPTO CHECK"

# Palette
BACKGROUND = (10, 14, 20)
PANEL = (20, 28, 38)
PANEL_HI = (30, 42, 56)
LINE = (38, 52, 68)
TEXT = (232, 238, 245)
DIM = (126, 138, 153)
ACCENT = (25, 227, 201)               # teal
ACCENT_DARK = (8, 60, 56)
GREEN = (43, 217, 107)
RED = (255, 59, 71)
YELLOW = (255, 176, 32)
BUTTON = PANEL                        # kept for older call sites
BUTTON_PRESSED = PANEL_HI

PAD = 10
BUTTON_H = 44
HEADER_H = 30
TOP = HEADER_H + 6                    # first content row under the header

# Font Awesome 6 Free (Solid), shipped in the image
ICONS = {
    "shield": "", "search": "", "check": "", "x": "",
    "warning": "", "qr": "", "key": "", "camera": "",
    "power": "", "back": "", "erase": "", "lock": "",
    "fingerprint": "", "ok_circle": "", "x_circle": "",
    "keyboard": "",
}

STEPS = ["SEED", "PSBT", "RESULT"]

# Battery (v7): a callable returning (percent shown or None, fill 0-100, on the cable) or
# None when there is no reading; set by the app from the UPS HAT (C) gauge. Tests leave
# it None, and so does a device without the gauge: then no icon at all.
battery_provider = None


def battery_state():
    if battery_provider is None:
        return None
    try:
        return battery_provider()
    except Exception:
        return None


def draw_battery(d, right: int, cy: int):
    """ Battery icon (and percentage) right-aligned at `right`; returns the left edge used """
    state = battery_state()
    if state is None:
        return right
    shown, fill_percent, on_cable = state
    w, h = 24, 12
    x2 = right - 3
    x1 = x2 - w
    y1 = int(cy - h / 2)
    color = GREEN if fill_percent > 30 else (YELLOW if fill_percent > 15 else RED)
    d.rectangle((x1, y1, x1 + w - 1, y1 + h - 1), outline=TEXT, fill=PANEL, width=1)
    d.rectangle((x1 + w, y1 + 3, x1 + w + 2, y1 + h - 4), fill=TEXT)
    level = max(1, int(round((w - 4) * max(0, min(100, fill_percent)) / 100)))
    d.rectangle((x1 + 2, y1 + 2, x1 + 1 + level, y1 + h - 3), fill=color)
    if on_cable:
        bolt = [(x1 + 13, y1 + 1), (x1 + 8, y1 + 7), (x1 + 12, y1 + 7), (x1 + 10, y1 + 11), (x1 + 16, y1 + 5),
                (x1 + 12, y1 + 5)]
        d.polygon(bolt, fill=BACKGROUND, outline=TEXT)
    left = x1
    if shown is not None:
        f = font(13, bold=True, mono=True)
        text = f"{shown}%"
        left = x1 - 4 - text_width(text, f)
        d.text((x1 - 4, cy), text, fill=TEXT, font=f, anchor="rm")
    return left - 6


def _font_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "seedsigner", "resources", "fonts")


_FONTS = {}


def font(size: int, bold: bool = False, mono: bool = False, icons: bool = False):
    key = (size, bold, mono, icons)
    if key not in _FONTS:
        if icons:
            name = "Font_Awesome_6_Free-Solid-900.otf"
        elif mono:
            name = "Inconsolata-SemiBold.ttf" if bold else "Inconsolata-Regular.ttf"
        else:
            name = "OpenSans-SemiBold.ttf" if bold else "OpenSans-Regular.ttf"
        try:
            _FONTS[key] = ImageFont.truetype(os.path.join(_font_dir(), name), size)
        except OSError:
            try:
                _FONTS[key] = ImageFont.load_default(size)
            except TypeError:
                _FONTS[key] = ImageFont.load_default()
    return _FONTS[key]


def text_width(text: str, f) -> int:
    return int(f.getlength(text))


def fit_font(text: str, size: int, width: int, bold: bool = False, mono: bool = False):
    """ The font at `size`, or smaller (down to 11) until `text` fits in `width` """
    while size > 11 and text_width(text, font(size, bold=bold, mono=mono)) > width:
        size -= 1
    return font(size, bold=bold, mono=mono)


def wrap(text: str, f, width: int) -> list:
    lines = []
    for paragraph_text in text.split("\n"):
        words = paragraph_text.split(" ")
        line = ""
        for word in words:
            candidate = word if not line else line + " " + word
            if text_width(candidate, f) <= width:
                line = candidate
            else:
                if line:
                    lines.append(line)
                while text_width(word, f) > width:
                    # break very long tokens (addresses, hex)
                    cut = len(word)
                    while cut > 1 and text_width(word[:cut], f) > width:
                        cut -= 1
                    lines.append(word[:cut])
                    word = word[cut:]
                line = word
        lines.append(line)
    return lines


def new_canvas():
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    return image, ImageDraw.Draw(image)


def icon(d, name: str, xy, size: int = 20, color=TEXT, anchor: str = "mm"):
    if not name:
        return
    d.text(xy, ICONS.get(name, name), fill=color, font=font(size, icons=True), anchor=anchor)


def spaced(d, xy, text: str, f, fill, spacing: int = 2, anchor: str = "mm"):
    """ Letter-spaced text (labels, the app name) """
    total = sum(text_width(c, f) for c in text) + spacing * (len(text) - 1)
    x, y = xy
    if anchor[0] == "m":
        x -= total / 2
    elif anchor[0] == "r":
        x -= total
    for c in text:
        d.text((x, y), c, fill=fill, font=f, anchor="l" + anchor[1])
        x += text_width(c, f) + spacing


def emblem(d, cx: int, cy: int, size: int = 96):
    """ The app's mark: a shield guarding the nonce k """
    icon(d, "shield", (cx, cy), size, ACCENT)
    d.text((cx, cy + size * 0.02), "k", fill=BACKGROUND, font=font(int(size * 0.62), bold=True, mono=True), anchor="mm")


def header(d, step: int = None, label: str = None):
    """ Top bar: emblem and app name on the left, the step (or a label) on the right """
    d.rectangle((0, 0, WIDTH, HEADER_H), fill=PANEL)
    d.line((0, HEADER_H, WIDTH, HEADER_H), fill=LINE, width=1)
    icon(d, "shield", (16, HEADER_H / 2), 17, ACCENT)
    d.text((16, HEADER_H / 2 + 1), "k", fill=PANEL, font=font(12, bold=True, mono=True), anchor="mm")
    spaced(d, (32, HEADER_H / 2), APP_NAME, font(14, bold=True, mono=True), ACCENT, 1, anchor="lm")
    try:
        right = draw_battery(d, WIDTH - PAD, HEADER_H / 2)
    except Exception:
        right = WIDTH - PAD                # a bad reading never breaks a screen
    if step is not None:
        x = right
        f = font(13, bold=True, mono=True)
        for i in range(len(STEPS) - 1, -1, -1):
            name = STEPS[i]
            w = text_width(name, f) + 12
            active = (i == step)
            done = (i < step)
            if active:
                d.rounded_rectangle((x - w, 6, x, HEADER_H - 6), radius=3, fill=ACCENT)
            d.text((x - w / 2, HEADER_H / 2), name, fill=BACKGROUND if active else (ACCENT if done else DIM),
                   font=f, anchor="mm")
            x -= w + 4
    elif label:
        d.text((right, HEADER_H / 2), label, fill=DIM, font=font(13, bold=True, mono=True), anchor="rm")


class Button:
    """ primary: filled teal; otherwise an outlined panel button """
    def __init__(self, rect, label: str, value=None, primary: bool = False, color=None, size: int = 19,
                 icon_name: str = None):
        self.rect = tuple(int(v) for v in rect)
        self.label = label
        self.value = label if value is None else value
        self.primary = primary
        self.color = color
        self.size = size
        self.icon_name = icon_name

    def contains(self, x: int, y: int, margin: int = 4) -> bool:
        x1, y1, x2, y2 = self.rect
        return x1 - margin <= x <= x2 + margin and y1 - margin <= y <= y2 + margin

    def draw(self, d: ImageDraw.ImageDraw, pressed: bool = False):
        filled = self.primary or self.color is not None
        fill = (self.color or ACCENT) if filled else PANEL
        if pressed:
            fill = PANEL_HI
        outline = None if filled else LINE
        d.rounded_rectangle(self.rect, radius=5, fill=fill, outline=outline, width=1)
        fg = BACKGROUND if filled and not pressed else TEXT
        f = font(self.size, bold=True)
        cx = (self.rect[0] + self.rect[2]) / 2
        cy = (self.rect[1] + self.rect[3]) / 2
        if self.icon_name:
            isz = self.size
            w = isz + 8 + text_width(self.label, f)
            icon(d, self.icon_name, (cx - w / 2 + isz / 2, cy), isz, fg)
            d.text((cx - w / 2 + isz + 8, cy), self.label, fill=fg, font=f, anchor="lm")
        else:
            d.text((cx, cy), self.label, fill=fg, font=f, anchor="mm")


def button_row(labels, y: int, height: int = BUTTON_H, primary_index=None, values=None, icons=None):
    """ Equal-width buttons across the screen """
    n = len(labels)
    width = (WIDTH - PAD * (n + 1)) / n
    out = []
    for i, label in enumerate(labels):
        x1 = PAD + i * (width + PAD)
        out.append(Button((x1, y, x1 + width, y + height), label,
                          value=None if values is None else values[i],
                          primary=(i == primary_index),
                          icon_name=None if icons is None else icons[i]))
    return out


def bottom_row(labels, primary_index=None, values=None, icons=None):
    return button_row(labels, HEIGHT - BUTTON_H - PAD, primary_index=primary_index, values=values, icons=icons)


def title(d, text: str, color=TEXT, y: int = TOP + 14):
    d.text((PAD, y), text, fill=color, font=font(22, bold=True), anchor="lm")


def paragraph(d, text: str, y: int, size: int = 18, color=TEXT, width: int = WIDTH - 2 * PAD,
              x: int = PAD, mono: bool = False, line_gap: int = 4, max_lines: int = 12) -> int:
    f = font(size, mono=mono)
    lines = wrap(text, f, width)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [lines[max_lines - 1][:-1] + "…"]
    for line in lines:
        d.text((x, y), line, fill=color, font=f)
        y += size + line_gap
    return y


def panel(d, rect, color=PANEL, outline=LINE):
    d.rounded_rectangle(rect, radius=6, fill=color, outline=outline, width=1)


def label_value(d, x: int, y: int, label: str, value: str, value_color=TEXT, value_size: int = 30):
    """ A small spaced label above a big monospace value (XFP and the like) """
    spaced(d, (x, y), label, font(12, bold=True, mono=True), DIM, 1, anchor="lt")
    d.text((x, y + 16), value, fill=value_color, font=font(value_size, bold=True, mono=True))


# v7.5: periodic checks every screen loop runs (the microSD guard, see storage.py)
housekeeping_hooks = []


def housekeeping(hw=None):
    for hook in list(housekeeping_hooks):
        try:
            hook()
        except Exception:
            pass                       # never breaks a screen


class Screen:
    """ Base: draw once, then wait for a tap on one of `self.buttons` """
    step = None                       # header step (0 SEED, 1 PSBT, 2 ESITO) or None
    header_label = None

    def __init__(self):
        self.buttons = []

    def draw(self, d, image):
        raise NotImplementedError

    def render(self):
        image, d = new_canvas()
        header(d, self.step, self.header_label)
        self.draw(d, image)
        for b in self.buttons:
            b.draw(d)
        return image

    def on_event(self, event):
        """ Return a value to leave the screen, or None to stay """
        if event.kind == Event.TAP:
            for b in self.buttons:
                if b.contains(event.x, event.y):
                    return b.value
        return None

    def run(self, hw, timeout: float = None):
        hw.show(self.render())
        started = time.monotonic()
        tick = getattr(hw, "battery_tick", None)
        while True:
            housekeeping(hw)
            if tick is not None and tick():
                hw.show(self.render())
            for event in hw.poll():
                result = self.on_event(event)
                if result == "__redraw__":
                    hw.show(self.render())
                elif result is not None:
                    return result
            if timeout is not None and time.monotonic() - started > timeout:
                return None
            hw.wait()


class MessageScreen(Screen):
    def __init__(self, heading: str, body: str, buttons=("OK",), heading_color=TEXT, primary_index=0, values=None,
                 icon_name: str = None):
        super().__init__()
        self.heading, self.body, self.heading_color, self.icon_name = heading, body, heading_color, icon_name
        self.buttons = bottom_row(list(buttons), primary_index=primary_index, values=values)

    def draw(self, d, image):
        x = PAD
        if self.icon_name:
            icon(d, self.icon_name, (PAD + 14, TOP + 16), 26, self.heading_color)
            x = PAD + 38
        d.text((x, TOP + 16), self.heading, fill=self.heading_color, font=font(22, bold=True), anchor="lm")
        paragraph(d, self.body, TOP + 44, max_lines=8)


class KeyboardScreen(Screen):
    """ Passphrase entry: printable ASCII, four pages of keys """
    PAGES = {
        "abc": ["qwertyuiop", "asdfghjkl", "zxcvbnm"],
        "ABC": ["QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"],
        "123": ["1234567890", "!@#$%^&*()", "-_=+[]{};:"],
        "#+=": ["'\",.<>/?\\|", "`~"],
    }
    KEY_W = 46
    KEY_H = 44
    TOP = 104
    FIELD_FONT = 19
    step = 0

    def __init__(self, text: str = ""):
        super().__init__()
        self.text = text
        self.page = "abc"

    def keys(self):
        keys = []
        for row, chars in enumerate(self.PAGES[self.page]):
            total = len(chars) * self.KEY_W + (len(chars) - 1) * 2
            x = (WIDTH - total) / 2
            y = self.TOP + row * (self.KEY_H + 4)
            for c in chars:
                keys.append(Button((x, y, x + self.KEY_W, y + self.KEY_H), c, value=("char", c), size=22))
                x += self.KEY_W + 2
        y = self.TOP + 3 * (self.KEY_H + 4) + 2
        shift = "abc" if self.page == "ABC" else ("ABC" if self.page == "abc" else "abc")
        numbers = "#+=" if self.page == "123" else "123"
        controls = [
            (70, "aA" if self.page in ("abc", "ABC") else "abc", ("page", shift), None),
            (70, numbers, ("page", numbers), None),
            (140, "space", ("char", " "), None),
            (70, "", ("back", None), "erase"),
            (90, "Done", ("done", None), None),
        ]
        x = PAD
        for w, label, value, icon_name in controls:
            keys.append(Button((x, y, x + w, y + self.KEY_H), label, value=value, size=18,
                               primary=(value[0] == "done"), icon_name=icon_name))
            x += w + 5
        return keys

    def render(self):
        self.buttons = self.keys()
        image, d = new_canvas()
        header(d, self.step)
        spaced(d, (PAD, TOP + 2), "PASSPHRASE", font(12, bold=True, mono=True), DIM, 1, anchor="lt")
        d.text((WIDTH - PAD, TOP + 2), f"{len(self.text)} characters · space = ·", fill=DIM,
               font=font(13, mono=True), anchor="rt")
        # Two lines (v7): a long passphrase stays readable; past two lines, the end is shown
        d.rounded_rectangle((PAD, TOP + 16, WIDTH - PAD, self.TOP - 4), radius=5, fill=PANEL, outline=ACCENT, width=1)
        for i, line in enumerate(self.field_lines()):
            d.text((2 * PAD, TOP + 30 + i * 24), line, fill=TEXT, font=font(self.FIELD_FONT, mono=True), anchor="lm")
        for b in self.buttons:
            b.draw(d)
        return image

    def per_line(self) -> int:
        f = font(self.FIELD_FONT, mono=True)
        widest = max(f.getlength(c) for c in "MW@·_…")
        return max(8, int((WIDTH - 5 * PAD) // widest))

    def field_lines(self) -> list:
        """ The passphrase (spaces as ·) and the cursor on at most two lines """
        n = self.per_line()
        shown = self.text.replace(" ", "·") + "_"
        if len(shown) > 2 * n:
            shown = "…" + shown[-(2 * n - 1):]
        return [shown[:n], shown[n:]] if len(shown) > n else [shown]

    def on_event(self, event):
        if event.kind != Event.TAP:
            return None
        for b in self.keys():
            if b.contains(event.x, event.y, margin=1):
                kind, arg = b.value
                if kind == "char":
                    self.text += arg
                    if self.page == "ABC":
                        self.page = "abc"
                elif kind == "page":
                    self.page = arg
                elif kind == "back":
                    self.text = self.text[:-1]
                elif kind == "done":
                    return ("done", self.text)
                return "__redraw__"
        return None


class NumberPadScreen(Screen):
    """ v7.5: digits only (the explorer's "Go to #" opened the passphrase keyboard) """
    KEYS_TOP = TOP + 64
    KEY_H = 60
    MAX_DIGITS = 10

    def __init__(self, title_text: str, text: str = ""):
        super().__init__()
        self.title_text, self.text = title_text, text

    def keys(self):
        out = []
        width = (WIDTH - 6 * PAD) / 5
        for n, digit in enumerate("1234567890"):
            col, row = n % 5, n // 5
            x = PAD + col * (width + PAD)
            y = self.KEYS_TOP + row * (self.KEY_H + 8)
            out.append(Button((x, y, x + width, y + self.KEY_H), digit, value=("digit", digit), size=26))
        out.append(Button((WIDTH - PAD - 90, TOP + 8, WIDTH - PAD, TOP + 50), "", value=("erase", None),
                          icon_name="erase"))
        out += bottom_row(["Cancel", "Go"], primary_index=1, values=[("cancel", None), ("done", None)])
        return out

    def render(self):
        self.buttons = self.keys()
        image, d = new_canvas()
        header(d, None, "EXPLORER")
        d.text((PAD, TOP + 2), self.title_text, fill=DIM, font=font(14, bold=True), anchor="lt")
        d.rounded_rectangle((PAD, TOP + 20, WIDTH - PAD - 100, TOP + 50), radius=5, fill=PANEL, outline=ACCENT)
        d.text((2 * PAD, TOP + 35), (self.text or "") + "_", fill=TEXT, font=font(22, bold=True, mono=True),
               anchor="lm")
        for b in self.buttons:
            b.draw(d)
        return image

    def on_event(self, event):
        if event.kind != Event.TAP:
            return None
        for b in self.keys():
            if b.contains(event.x, event.y, margin=1):
                kind, arg = b.value
                if kind == "digit":
                    if len(self.text) < self.MAX_DIGITS:
                        self.text += arg
                elif kind == "erase":
                    self.text = self.text[:-1]
                else:
                    return (kind, self.text)
                return "__redraw__"
        return None


class ScrollList:
    """ Lines of (text, color, size, mono) with swipe and tap scrolling """
    def __init__(self, lines, top: int, bottom: int):
        self.lines = lines
        self.top, self.bottom = top, bottom
        self.offset = 0

    def total_height(self):
        return sum(l[2] + 6 for l in self.lines)

    def max_offset(self):
        return max(0, self.total_height() - (self.bottom - self.top))

    def scroll(self, pixels: int):
        self.offset = min(self.max_offset(), max(0, self.offset + pixels))

    def draw(self, d):
        y = self.top - self.offset
        for text, color, size, mono in self.lines:
            if y >= self.top and y + size + 2 <= self.bottom:
                d.text((PAD, y), text, fill=color, font=font(size, mono=mono, bold=False))
            y += size + 6
        # scroll bar
        total = self.total_height()
        view = self.bottom - self.top
        if total > view:
            bar_h = max(20, int(view * view / total))
            bar_y = self.top + int((view - bar_h) * self.offset / max(1, self.max_offset()))
            d.rounded_rectangle((WIDTH - 6, self.top, WIDTH - 3, self.bottom), radius=1, fill=PANEL)
            d.rounded_rectangle((WIDTH - 6, bar_y, WIDTH - 3, bar_y + bar_h), radius=1, fill=ACCENT)
