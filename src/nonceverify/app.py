"""
Anti-Klepto Check, the verifier SD's application: load a seed, then
- scan a signed PSBT and check every nonce (and, on green, confirm the txid against
  Sparrow before broadcasting), or
- check that an address belongs to the wallet (v7): single-sig from the seed alone,
  multisig from the wallet descriptor scanned off the paper backup.

This image never signs, never exports a key and never shows a seed. The seed lives in
this process's memory until "Done" or until the UPS HAT switch cuts the power; a
descriptor scanned for "Check address" stays in RAM for the seed session too. Nothing is
ever written to the microSD.
"""
import base64
import logging
import random
import time

from nonceverify import address, crypto, seedqr, storage, transport, ui, wordentry
from nonceverify.hw import HEIGHT, WIDTH, Event, QRDecoderError, decode_qr
from nonceverify.psbt import PSBTError, combine_final_tx, has_signatures, parse_psbt, parse_tx
from nonceverify.verify import (OWNER__EXTERNAL, OWNER__FAKE, OWNER__OURS, OWNER__UNVERIFIED,
    GRIND__EMBIT, GRIND__LIBSECP, GRIND__NONE, STATUS__FAIL, STATUS__FOREIGN,
    STATUS__OK, STATUS__UNSIGNED, STATUS__UNVERIFIABLE, VERDICT__FAIL, VERDICT__OK, VERDICT__PARTIAL, verify_psbt)

# The signer under test, chosen by name on the "Seed ready" screen. Each one maps to the
# low-R grinding rule its firmware follows (verify.py keeps the three rules); the rule is
# what the nonce check uses, the name is what the user recognizes.
#   Coldcard Q: libsecp256k1, retry until r < 2^255 (low R, as Bitcoin Core);
#   SeedSigner: embit, retry until the DER signature is <= 70 bytes;
#   Trezor: trezor-crypto / secp256k1-zkp, first RFC 6979 nonce, no retry (firmware
#     source: core/src/apps/bitcoin/common.py ecdsa_sign -> secp256k1.sign with no
#     canonical callback -> crypto/ecdsa.c or zkp_ecdsa.c, retry counter 0; confirmed on
#     a real Trezor on 8 Nov 2026).
SIGNER__COLDCARD_Q = "coldcard_q"
SIGNER__SEEDSIGNER = "seedsigner"
SIGNER__TREZOR = "trezor"
SIGNERS = {
    SIGNER__COLDCARD_Q: ("Coldcard Q", GRIND__LIBSECP),
    SIGNER__SEEDSIGNER: ("SeedSigner", GRIND__EMBIT),
    SIGNER__TREZOR: ("Trezor", GRIND__NONE),
}
GRINDING_SHORT = {GRIND__LIBSECP: "libsecp", GRIND__EMBIT: "embit", GRIND__NONE: "none"}


def signer_name(signer: str) -> str:
    return SIGNERS[signer][0]


def signer_grinding(signer: str) -> str:
    return SIGNERS[signer][1]


def show_spaces(text: str) -> str:
    """ Spaces made visible (the keyboard can't type a middle dot, so no ambiguity) """
    return text.replace(" ", "·")


logger = logging.getLogger(__name__)

VERSION = "7.8"

# v7.5: randomness for the checks that must not be predictable (the xpub groups to find
# on the paper). The OS's CSPRNG; tests swap in a fixed one.
RNG = random.SystemRandom()


def btc(sats) -> str:
    if sats is None:
        return "?"
    return f"{sats / 1e8:.8f} BTC"


def txid_rows(txid: str) -> list:
    """ The 64 hex digits in 4 rows of 4 groups of 4, as they are compared by eye """
    groups = [txid[i:i + 4] for i in range(0, len(txid), 4)]
    return [" ".join(groups[i:i + 4]) for i in range(0, len(groups), 4)]


# ---------------------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------------------
SPLASH_FEATURES = (
    ("RFC 6979 nonce", "signature nonces"),
    ("Sighash ALL", "no ANYONECANPAY / NONE"),
    ("Txid match", "broadcast what was checked"),
    ("Address check", "single-sig & multisig"),
    ("Change check", "your change, proven yours"),      # v7.6 (Ollen)
    ("Air-gapped", "microSD read-only"),
)


def _blend(c0, c1, t: float):
    return tuple(int(round(c0[i] + (c1[i] - c0[i]) * t)) for i in range(3))


def splash_background():
    """
    v7.4 (Ollen, 9 Nov 2026): the splash without its START button. A flat grid above a
    horizon, a teal floor in perspective below it, the emblem with a halo, the name, and a
    panel with the four checks. Plain PIL drawing, no blur: a few hundred lines and four
    alpha composites, drawn once per boot and kept.
    """
    from PIL import Image, ImageDraw
    hz = SplashScreen.HORIZON
    image = Image.new("RGBA", (WIDTH, HEIGHT))
    d = ImageDraw.Draw(image)
    sky_top, sky_bottom, floor_bottom = ui.BACKGROUND, (16, 26, 40), (8, 34, 38)
    for y in range(HEIGHT):
        color = _blend(sky_top, sky_bottom, y / hz) if y < hz else \
            _blend(sky_bottom, floor_bottom, (y - hz) / (HEIGHT - hz))
        d.line((0, y, WIDTH, y), fill=color + (255,))

    lines = Image.new("RGBA", image.size, (0, 0, 0, 0))
    g = ImageDraw.Draw(lines)
    for x in range(0, WIDTH, 24):                                   # flat grid in the sky
        g.line((x, 0, x, hz), fill=(60, 84, 112, 70))
    for y in range(0, hz, 24):
        g.line((0, y, WIDTH, y), fill=(60, 84, 112, 70))
    vx = WIDTH / 2                                                  # floor: rays to the vanishing point
    for i in range(-16, 17):
        g.line((vx + i * 6, hz, vx + i * 40, HEIGHT), fill=ui.ACCENT + (110,))
    y, step = float(hz), 3.0                                        # cross lines, closer as they recede
    while y < HEIGHT:
        g.line((0, int(y), WIDTH, int(y)), fill=ui.ACCENT + (int(60 + 140 * (y - hz) / (HEIGHT - hz)),))
        step *= 1.3
        y += step
    for width, alpha in ((13, 22), (9, 40), (5, 90), (2, 255)):    # horizon glow
        g.line((0, hz, WIDTH, hz), fill=ui.ACCENT + (alpha,), width=width)
    image.alpha_composite(lines)

    halo = Image.new("RGBA", image.size, (0, 0, 0, 0))
    h = ImageDraw.Draw(halo)
    cx, cy = WIDTH // 2, SplashScreen.EMBLEM_CY
    for r, alpha in ((60, 10), (50, 16), (42, 24), (34, 34)):
        h.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ui.ACCENT + (alpha,))
    image.alpha_composite(halo)
    d = ImageDraw.Draw(image)
    ui.emblem(d, cx, cy, SplashScreen.EMBLEM_SIZE)
    ui.spaced(d, (WIDTH / 2, 101), "ANTI-KLEPTO", ui.font(32, bold=True), ui.TEXT, 2)
    ui.spaced(d, (WIDTH / 2, 127), "CHECK", ui.font(15, bold=True, mono=True), ui.ACCENT, 9)

    x1, y1, x2, y2 = SplashScreen.PANEL
    glass = Image.new("RGBA", image.size, (0, 0, 0, 0))
    p = ImageDraw.Draw(glass)
    p.rounded_rectangle((x1 + 3, y1 + 4, x2 + 3, y2 + 4), radius=10, fill=(0, 0, 0, 120))
    p.rounded_rectangle((x1, y1, x2, y2), radius=10, fill=ui.PANEL + (232,), outline=(70, 96, 124, 200), width=1)
    p.line((x1 + 10, y1 + 1, x2 - 10, y1 + 1), fill=(120, 150, 180, 110))
    p.line((WIDTH / 2, y1 + 10, WIDTH / 2, y2 - 10), fill=ui.LINE + (255,))
    rows = (len(SPLASH_FEATURES) + 1) // 2
    for r in range(1, rows):
        y = y1 + (y2 - y1) * r / rows
        p.line((x1 + 12, y, x2 - 12, y), fill=ui.LINE + (255,))
    image.alpha_composite(glass)
    d = ImageDraw.Draw(image)
    f_name, f_sub = ui.font(13, bold=True), ui.font(10)
    rows = (len(SPLASH_FEATURES) + 1) // 2
    row_h = (y2 - y1) / rows
    for i, (name, sub) in enumerate(SPLASH_FEATURES):
        fx = x1 + 12 + (i % 2) * (WIDTH / 2 - x1)
        fy = y1 + row_h / 2 + (i // 2) * row_h
        check_mark(d, fx + 7, fy, 6)
        d.text((fx + 19, fy - 5), name, fill=ui.TEXT, font=f_name, anchor="lm")
        d.text((fx + 19, fy + 8), sub, fill=ui.DIM, font=f_sub, anchor="lm")
    d.text((WIDTH / 2, 303), f"SeedSigner Max · verifier SD v{VERSION}", fill=ui.DIM,
           font=ui.font(13, mono=True), anchor="mm")
    return image.convert("RGB")


def check_mark(d, x: float, y: float, r: float, color=ui.ACCENT):
    """ A tick in a circle, drawn (the fonts have no check glyph) """
    d.ellipse((x - r, y - r, x + r, y + r), outline=color, width=2)
    d.line(((x - r * .45, y), (x - r * .1, y + r * .4), (x + r * .5, y - r * .4)), fill=color, width=2)


class SplashScreen(ui.Screen):
    """
    Boot screen: the name of the tool, unmistakably not SeedSigner, and what it checks.
    v7.4: it stays until START is tapped (before: any tap, or 4 seconds).
    """
    HORIZON = 150
    EMBLEM_CY = 44
    EMBLEM_SIZE = 76
    PANEL = (40, 138, 440, 232)       # v7.6: three rows (six checks)
    START = (140, 240, 340, 276)
    _background = None
    sd_status = None                  # v7.5: storage.protect_microsd() result, set by main()

    def __init__(self):
        super().__init__()
        self.buttons = [ui.Button(self.START, "START", value="go", primary=True)]

    @classmethod
    def background(cls):
        if cls._background is None:
            cls._background = splash_background()
        return cls._background

    def render(self):
        from PIL import ImageDraw
        image = self.background().copy()
        d = ImageDraw.Draw(image)
        x1, y1, x2, y2 = self.START
        cy, radius = (y1 + y2) / 2, (y2 - y1) // 2
        d.rounded_rectangle((x1 - 3, y1 - 3, x2 + 3, y2 + 3), radius=radius + 3, outline=ui.ACCENT_DARK, width=3)
        d.rounded_rectangle(self.START, radius=radius, fill=ui.ACCENT)
        d.rounded_rectangle((x1, y1, x2, cy), radius=radius, fill=(110, 240, 224))
        d.rectangle((x1 + radius, cy - 5, x2 - radius, cy), fill=(70, 234, 212))
        f = ui.font(19, bold=True)
        text_w = ui.text_width("START", f)
        tx = (x1 + x2) / 2 - (text_w + 18) / 2
        d.text((tx, cy + 1), "START", fill=ui.BACKGROUND, font=f, anchor="lm")
        ax = tx + text_w + 8
        d.polygon(((ax, cy - 7), (ax + 11, cy), (ax, cy + 7)), fill=ui.BACKGROUND)
        if self.sd_status:
            writable = self.sd_status == storage.STATUS__WRITABLE
            text = "microSD WRITABLE: remount failed" if writable else f"microSD {self.sd_status}"
            d.text((WIDTH / 2, 289), text, fill=ui.YELLOW if writable else ui.DIM,
                   font=ui.font(13, bold=writable, mono=True), anchor="mm")
        return image


class HomeScreen(ui.Screen):
    step = 0
    STEPS = [
        ("qr", "Load the seed", "SeedQR or words, and passphrase: RAM only"),
        ("search", "Scan the signed PSBT", "from Sparrow · or check an address"),
        ("shield", "Read the result", "green: broadcast · red: do not"),
    ]

    def __init__(self):
        super().__init__()
        self.buttons = ui.bottom_row(["SeedQR", "Words"], primary_index=None, values=["scan_seed", "type_seed"],
                                     icons=["camera", "keyboard"])

    def draw(self, d, image):
        y = ui.TOP + 6
        for n, (icon_name, head, sub) in enumerate(self.STEPS, start=1):
            ui.panel(d, (ui.PAD, y, WIDTH - ui.PAD, y + 62))
            d.ellipse((ui.PAD + 12, y + 15, ui.PAD + 44, y + 47), outline=ui.ACCENT, width=2)
            d.text((ui.PAD + 28, y + 31), str(n), fill=ui.ACCENT, font=ui.font(18, bold=True, mono=True), anchor="mm")
            d.text((ui.PAD + 58, y + 20), head, fill=ui.TEXT, font=ui.font(19, bold=True), anchor="lm")
            d.text((ui.PAD + 58, y + 44), sub, fill=ui.DIM, font=ui.font(15), anchor="lm")
            ui.icon(d, icon_name, (WIDTH - ui.PAD - 26, y + 31), 22, ui.LINE)
            y += 70


class PassphraseQuestionScreen(ui.Screen):
    step = 0

    def __init__(self, fingerprint: str, words: int):
        super().__init__()
        self.fingerprint, self.words = fingerprint, words
        self.buttons = ui.bottom_row(["None", "Passphrase"], primary_index=1, values=["none", "enter"],
                                     icons=[None, "key"])

    def draw(self, d, image):
        ui.icon(d, "ok_circle", (ui.PAD + 13, ui.TOP + 16), 24, ui.GREEN)
        d.text((ui.PAD + 34, ui.TOP + 16), f"Seed read: {self.words} words, checksum OK",
               fill=ui.TEXT, font=ui.font(19, bold=True), anchor="lm")
        ui.panel(d, (ui.PAD, ui.TOP + 40, WIDTH - ui.PAD, ui.TOP + 112))
        ui.label_value(d, ui.PAD + 14, ui.TOP + 52, "XFP WITHOUT PASSPHRASE", self.fingerprint, ui.TEXT, 32)
        d.text((WIDTH / 2, ui.TOP + 150), "Does the wallet use a BIP39 passphrase?", fill=ui.TEXT,
               font=ui.font(20), anchor="mm")


class ConfirmPassphraseScreen(ui.Screen):
    step = 0

    def __init__(self, passphrase: str, fingerprint: str):
        super().__init__()
        self.passphrase, self.fingerprint = passphrase, fingerprint
        self.revealed = False             # v7.5: hidden until "Show"
        self.make_buttons()

    def make_buttons(self):
        self.buttons = ui.bottom_row(["Edit", "Hide" if self.revealed else "Show", "Confirm"], primary_index=2,
                                     values=["edit", "reveal", "ok"])

    def on_event(self, event):
        value = super().on_event(event)
        if value == "reveal":
            self.revealed = not self.revealed
            self.make_buttons()
            return "__redraw__"
        return value

    def draw(self, d, image):
        ui.title(d, "Check the passphrase")
        ui.panel(d, (ui.PAD, ui.TOP + 34, WIDTH - ui.PAD, ui.TOP + 120))
        ui.spaced(d, (ui.PAD + 14, ui.TOP + 44), "PASSPHRASE", ui.font(12, bold=True, mono=True), ui.DIM, 1, anchor="lt")
        d.text((WIDTH - ui.PAD - 12, ui.TOP + 44), f"{len(self.passphrase)} characters · spaces = ·",
               fill=ui.DIM, font=ui.font(12, mono=True), anchor="rt")
        f = ui.font(19, mono=True)
        shown = show_spaces(self.passphrase) if self.revealed else "•" * min(len(self.passphrase), 40)
        lines = ui.wrap(shown if self.passphrase else "(empty)", f, WIDTH - 5 * ui.PAD)
        if len(lines) > 3:
            lines = lines[:2] + ["… " + lines[-1][-30:]]
        for i, line in enumerate(lines):
            d.text((ui.PAD + 14, ui.TOP + 62 + i * 19), line, fill=ui.TEXT, font=f)
        ui.label_value(d, ui.PAD + 14, ui.TOP + 128, "RESULTING XFP", self.fingerprint, ui.ACCENT, 32)
        if not self.revealed:
            d.text((WIDTH - ui.PAD, ui.TOP + 160), "hidden: Show to check it", fill=ui.DIM, font=ui.font(14),
                   anchor="rm")
        elif self.passphrase != self.passphrase.strip():
            d.text((WIDTH - ui.PAD, ui.TOP + 160), "careful: spaces at the start or the end",
                   fill=ui.YELLOW, font=ui.font(14), anchor="rm")


class SeedReadyScreen(ui.Screen):
    step = 1
    GRINDING_Y = 196
    ORDER = (SIGNER__COLDCARD_Q, SIGNER__SEEDSIGNER, SIGNER__TREZOR)

    def __init__(self, fingerprint: str, words: int, has_passphrase: bool, signer: str = None,
                 locked: bool = False):
        super().__init__()
        self.fingerprint, self.words, self.has_passphrase = fingerprint, words, has_passphrase
        # Once "Scan PSBT" has been pressed in a seed session the signer can't change any
        # more (see Session.signer_locked): only "Change seed" / "Done" start a new choice
        self.locked = locked and signer in SIGNERS
        self.locked_hint = False
        # No default: after every seed load the user names the device that signed. A choice
        # left over from an earlier session (say Trezor, no grinding) would let a Coldcard
        # that skipped its grinding pass unnoticed.
        self.signer = signer if signer in SIGNERS else None
        self.must_choose = False
        self.make_buttons()

    def make_buttons(self):
        # v7.5: one button per device (a cycling toggle let a missed tap pick the wrong one)
        self.buttons = []
        width = (WIDTH - 4 * ui.PAD) / 3
        for i, signer in enumerate(self.ORDER):
            x1 = ui.PAD + i * (width + ui.PAD)
            b = ui.Button((x1, self.GRINDING_Y, x1 + width, self.GRINDING_Y + 42), signer_name(signer),
                          value=("signer", signer), size=18, primary=(signer == self.signer))
            if self.locked and signer != self.signer:
                b.color = ui.LINE
            self.buttons.append(b)
        self.buttons += ui.bottom_row(["Change seed", "Address", "Scan PSBT"], primary_index=2,
                                      values=["change_seed", "address", "scan_psbt"], icons=[None, "search", "camera"])

    def on_event(self, event):
        value = super().on_event(event)
        if isinstance(value, tuple) and value[0] == "signer" and self.locked:
            self.locked_hint = value[1] != self.signer
            return "__redraw__"
        if isinstance(value, tuple) and value[0] == "signer":
            self.signer = value[1]
            self.must_choose = False
            self.make_buttons()
            return "__redraw__"
        if value == "scan_psbt" and self.signer is None:
            self.must_choose = True
            return "__redraw__"
        return value

    def draw(self, d, image):
        ui.panel(d, (ui.PAD, ui.TOP + 4, WIDTH - ui.PAD, ui.TOP + 92), outline=ui.ACCENT)
        ui.icon(d, "fingerprint", (ui.PAD + 34, ui.TOP + 48), 40, ui.ACCENT)
        ui.spaced(d, (ui.PAD + 70, ui.TOP + 16), "SEED READY · XFP", ui.font(12, bold=True, mono=True), ui.DIM, 1, anchor="lt")
        d.text((ui.PAD + 68, ui.TOP + 30), self.fingerprint, fill=ui.ACCENT, font=ui.font(42, bold=True, mono=True))
        d.text((WIDTH - ui.PAD - 12, ui.TOP + 80), f"{self.words} words · " +
               ("with passphrase" if self.has_passphrase else "no passphrase"),
               fill=ui.DIM, font=ui.font(14), anchor="rm")
        ui.paragraph(d, "It must match the XFP on your wallet backup.", ui.TOP + 104, size=17, color=ui.TEXT)
        if self.locked_hint:
            d.text((ui.PAD, self.GRINDING_Y - 8), "Locked for this session: to change it, Change seed",
                   fill=ui.YELLOW, font=ui.font(13), anchor="ls")
        elif self.locked:
            d.text((ui.PAD, self.GRINDING_Y - 8), "Who signed? (fixed for this session)",
                   fill=ui.DIM, font=ui.font(13), anchor="ls")
        elif self.must_choose:
            d.text((ui.PAD, self.GRINDING_Y - 8), "First choose the device you signed with:",
                   fill=ui.YELLOW, font=ui.font(13), anchor="ls")
        else:
            d.text((ui.PAD, self.GRINDING_Y - 8), "Which device signed? (fixed once you press Scan PSBT)",
                   fill=ui.DIM, font=ui.font(14), anchor="ls")


class ScanScreen:
    """
    Live camera preview (320 x 320 in the middle) with "Back" on the left and the zoom
    and progress on the right; every frame goes to the QR decoder.
    """
    PREVIEW = 320
    LEFT = (WIDTH - PREVIEW) // 2

    def __init__(self, heading: str, handle_payload, errors=()):
        self.heading = heading
        # v7.7 (Ollen): no mosaic anywhere. A camera filming the screen would film the
        # SeedQR on the paper too: the preview is always the plain camera image.
        self.handle_payload = handle_payload       # payload -> (done: bool, progress: float, note)
        self.errors = (transport.TransportError, seedqr.SeedQRError) + tuple(errors)
        self.back = ui.Button((4, 200, self.LEFT - 4, 280), "", value="back", size=22, icon_name="back")
        self.zoom = ui.Button((self.LEFT + self.PREVIEW + 4, 200, WIDTH - 4, 280), "1x", value="zoom", size=18,
                              icon_name="search")
        self.progress = 0.0
        self.status = ""

    def compose(self, frame):
        image, d = ui.new_canvas()
        d.rectangle((0, 0, self.LEFT - 1, HEIGHT), fill=ui.PANEL)
        d.rectangle((self.LEFT + self.PREVIEW, 0, WIDTH, HEIGHT), fill=ui.PANEL)
        if frame is not None:
            if frame.size != (self.PREVIEW, self.PREVIEW):
                frame = frame.resize((self.PREVIEW, self.PREVIEW))
            image.paste(frame, (self.LEFT, 0))
        # corner brackets of the viewfinder
        L, R, T, B, k = self.LEFT + 16, self.LEFT + self.PREVIEW - 16, 16, HEIGHT - 16, 26
        for (x, y, dx, dy) in ((L, T, 1, 1), (R, T, -1, 1), (L, B, 1, -1), (R, B, -1, -1)):
            d.line((x, y, x + dx * k, y), fill=ui.ACCENT, width=3)
            d.line((x, y, x, y + dy * k), fill=ui.ACCENT, width=3)
        ui.emblem(d, self.LEFT // 2, 34, 36)
        for i, line in enumerate(ui.wrap(self.heading, ui.font(15, bold=True), self.LEFT - 8)[:3]):
            d.text((self.LEFT / 2, 76 + i * 19), line, fill=ui.ACCENT, font=ui.font(15, bold=True), anchor="mm")
        self.back.draw(d)
        self.zoom.draw(d)
        # progress ring
        cx, cy, r = (self.LEFT + self.PREVIEW + WIDTH) / 2, 100, 30
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ui.LINE, width=6)
        if self.progress > 0:
            d.arc((cx - r, cy - r, cx + r, cy + r), -90, -90 + 360 * self.progress, fill=ui.ACCENT, width=6)
        d.text((cx, cy), f"{int(self.progress * 100)}%", fill=ui.TEXT, font=ui.font(16, bold=True, mono=True), anchor="mm")
        if self.status:
            # message band over the bottom of the preview
            lines = ui.wrap(self.status, ui.font(16), self.PREVIEW - 16)[:4]
            top = HEIGHT - 10 - 20 * len(lines)
            d.rectangle((self.LEFT, top - 8, self.LEFT + self.PREVIEW, HEIGHT), fill=ui.BACKGROUND)
            d.line((self.LEFT, top - 8, self.LEFT + self.PREVIEW, top - 8), fill=ui.YELLOW, width=2)
            for i, line in enumerate(lines):
                d.text((self.LEFT + 8, top + i * 20), line, fill=ui.YELLOW, font=ui.font(16))
        return image

    NO_FRAME_TIMEOUT_S = 8

    def _taps(self, hw):
        """ "back", or None; handles zoom taps """
        for event in hw.poll():
            if event.kind != Event.TAP:
                continue
            if self.back.contains(event.x, event.y):
                return "back"
            if self.zoom.contains(event.x, event.y):
                steps = (1, 2, 3)
                z = steps[(steps.index(hw.zoom) + 1) % len(steps)] if hw.zoom in steps else 1
                hw.camera_set_zoom(z)
                self._zoom_label(z)
        return None

    def _zoom_label(self, z):
        self.zoom.label = f"{z}x"
        self.zoom.primary = z > 1

    def run(self, hw):
        self._zoom_label(hw.zoom)
        self.status = "starting the camera…"
        hw.show(self.compose(None))
        try:
            try:
                hw.camera_start()
            except Exception as e:
                logger.info(f"camera: {type(e).__name__}: {e}")
                ui.MessageScreen("Camera not available", "Check the camera cable (with the device "
                                 "switched off) and try again.", heading_color=ui.RED, icon_name="camera").run(hw)
                return None
            started = time.monotonic()
            got_frame = False
            while True:
                ui.housekeeping(hw)
                if self._taps(hw) == "back":
                    return None
                frame = hw.camera_frame()
                if frame is None:
                    if not got_frame and time.monotonic() - started > self.NO_FRAME_TIMEOUT_S:
                        self.status = "no picture from the camera: go back and try again"
                        hw.show(self.compose(None))
                        started = time.monotonic()
                    hw.wait()
                    continue
                if not got_frame:
                    got_frame = True
                    self.status = ""
                payload = decode_qr(frame)
                if payload is None and self._taps(hw) == "back":
                    # extra touch sample between the slow steps (decode, display)
                    return None
                if payload is not None:
                    try:
                        done, self.progress, note = self.handle_payload(payload)
                        self.status = note or ""
                    except self.errors as e:
                        self.status = str(e)
                        done = False
                    except QRDecoderError:
                        raise
                    except Exception as e:
                        # a hostile or damaged QR must never cost the loaded seed
                        logger.info(f"QR rejected: {type(e).__name__}")
                        self.status = "invalid QR, ignored"
                        done = False
                    if done:
                        return True
                hw.show(self.compose(frame))
        finally:
            hw.camera_stop()


class TxidCheckScreen(ui.Screen):
    """
    v7: every signature checks out, but green is shown only once the user has compared
    the txid with Sparrow's (the transaction tab). The nonce check says THIS PSBT is
    clean; the txid ties it to the transaction Sparrow will broadcast. In the first two
    field tests the comparison was forgotten: now it is a step that can't be skipped.
    v7.8 (Ollen): the v7.5 quiz on the txid ending is gone: read it, then confirm.
    """
    step = 2

    def __init__(self, report):
        super().__init__()
        self.report = report
        self.buttons = ui.bottom_row(["Different", "Same txid"], primary_index=1, values=["different", "same"])
        self.buttons[0].color = ui.RED

    def draw(self, d, image):
        ui.icon(d, "ok_circle", (ui.PAD + 13, ui.TOP + 14), 24, ui.GREEN)
        d.text((ui.PAD + 34, ui.TOP + 14), "Nonces OK. Last step: compare the txid", fill=ui.TEXT,
               font=ui.font(18, bold=True), anchor="lm")
        d.text((ui.PAD, ui.TOP + 40), "Sparrow, transaction tab: is it the same, all of it?", fill=ui.DIM,
               font=ui.font(15), anchor="lm")
        ui.panel(d, (ui.PAD, ui.TOP + 54, WIDTH - ui.PAD, ui.TOP + 214), outline=ui.ACCENT)
        f = ui.font(28, bold=True, mono=True)
        for i, row in enumerate(txid_rows(self.report.txid)):
            d.text((WIDTH / 2, ui.TOP + 76 + i * 38), row, fill=ui.ACCENT if i % 2 == 0 else ui.TEXT, font=f, anchor="mm")


def can_export(report, txid_confirmed: bool) -> bool:
    """
    v7.5 review: the Tx QR only on green. On "this seed clean" the transaction would carry
    the unchecked cosigner signature, or is not complete; v7.6: not with an unproven change.
    """
    return bool(txid_confirmed and report.broadcast_tx and report.verdict == VERDICT__OK
                and not report.change_unverified)


class ResultScreen(ui.Screen):
    step = 2
    BANNER_H = 62

    def __init__(self, report, signer: str = None, txid_confirmed: bool = False):
        super().__init__()
        self.report = report
        self.signer = signer
        self.txid_confirmed = txid_confirmed
        lines = []
        add = lambda text, color=ui.TEXT, size=17, mono=False: lines.append((text, color, size, mono))
        colors = {STATUS__OK: ui.GREEN, STATUS__FAIL: ui.RED, STATUS__UNSIGNED: ui.YELLOW,
                  STATUS__UNVERIFIABLE: ui.YELLOW, STATUS__FOREIGN: ui.DIM}
        rule = GRINDING_SHORT.get(report.grinding, report.grinding)
        who = f"{signer_name(signer)} ({rule})" if signer in SIGNERS else f"grinding {rule}"
        width = WIDTH - 3 * ui.PAD
        if report.verdict == VERDICT__FAIL:
            # v7.5: the reason first, not below the txid
            for line in ui.wrap(report.tx_problem, ui.font(17), width) if report.tx_problem else []:
                add(line, ui.RED, 17)
            for r in report.inputs:
                if r.status == STATUS__FAIL:
                    for line in ui.wrap(f"input #{r.index}: {r.message}", ui.font(17), width):
                        add(line, ui.RED, 17)
        if report.verdict == VERDICT__PARTIAL and report.unchecked_cosigners:
            for line in ui.wrap(f"Do not broadcast yet: {report.unchecked_cosigners} cosigner signature(s) not "
                                "checked. Their nonce needs their seed: Change seed, load that seed, pick its "
                                "device, scan this same PSBT.", ui.font(16), width):
                add(line, ui.YELLOW, 16)
        elif report.verdict == VERDICT__PARTIAL:
            for line in ui.wrap("Not complete: some input is not signed yet. When the transaction is fully "
                                "signed, scan it again here: only then broadcast, with the Tx QR.", ui.font(16),
                                width):
                add(line, ui.YELLOW, 16)
        if report.extra_fields:
            for line in ui.wrap(f"The PSBT carries {report.extra_fields} field(s) the check doesn't need: data "
                                "that could leave with the PSBT file. Broadcast with the Tx QR, don't pass the PSBT "
                                "on.", ui.font(15), width):
                add(line, ui.YELLOW, 15)
        # v7.6: where the money goes, change proven or not, at the top
        external = [o for o in report.outputs if o.owner == OWNER__EXTERNAL]
        ours = [o for o in report.outputs if o.owner == OWNER__OURS]
        unverified = [o for o in report.outputs if o.owner == OWNER__UNVERIFIED]
        add(f"Sends {btc(sum(o.amount for o in external))} to {len(external)} other address(es)", ui.TEXT, 17)
        for o in ours:
            add(f"Change {btc(o.amount)}: YOURS, {o.label}", ui.GREEN, 17)
            for line in ui.wrap(o.note, ui.font(15), width) if o.note else []:
                add(line, ui.YELLOW, 15)
        for o in unverified:
            add(f"Maybe NOT yours: {btc(o.amount)} (change not verified)", ui.YELLOW, 17)
            for line in ui.wrap(o.note, ui.font(15), width):
                add(line, ui.YELLOW, 15)
        if not ours and not unverified and report.verdict != VERDICT__FAIL:
            add("No change back to this wallet", ui.DIM, 15)
        if can_export(report, txid_confirmed) and not report.change_warning:
            # v7.8 (Ollen): the result first, then what to do with it
            for line in ui.wrap("Broadcast THIS: tap Broadcast, then in Sparrow File > Load Transaction > "
                                "From QR. Only these bytes are the ones checked.", ui.font(16, bold=True), width):
                add(line, ui.ACCENT, 16)
        add(f"XFP {report.fingerprint} · {who}", ui.DIM, 15, True)
        add("txid" + (" (same as Sparrow's)" if txid_confirmed else ""), ui.DIM, 15, True)
        for row in txid_rows(report.txid):
            add(f"  {row}", ui.TEXT, 20, True)
        # Every signature checked is SIGHASH_ALL (anything else is red): say so on green
        all_tag = " · sighash ALL" if report.verdict == VERDICT__OK else ""
        add(f"locktime {report.locktime} · version {report.version}{all_tag}", ui.DIM, 15, True)
        if report.tx_problem and report.verdict != VERDICT__FAIL:
            for line in ui.wrap(report.tx_problem, ui.font(17), WIDTH - 3 * ui.PAD):
                add(line, ui.RED, 17)
        if self.seed_mismatch():
            for line in ui.wrap("No input of this PSBT belongs to this XFP: "
                                "check seed and passphrase.", ui.font(17), WIDTH - 3 * ui.PAD):
                add(line, ui.YELLOW, 17)
        for r in report.inputs:
            marker = {STATUS__OK: "OK", STATUS__FAIL: "!!", STATUS__FOREIGN: "--"}.get(r.status, "??")
            add(f"[{marker}] input #{r.index}  {btc(r.amount)}", colors[r.status], 18)
            if r.path:
                add(f"     {r.path}", ui.DIM, 15, True)
            for line in ui.wrap(r.message, ui.font(16), WIDTH - 6 * ui.PAD):
                add(f"     {line}", colors[r.status], 16)
        add("OUTPUTS", ui.DIM, 14, True)
        tags = {OWNER__OURS: ("YOURS", ui.GREEN), OWNER__UNVERIFIED: ("not verified", ui.YELLOW),
                OWNER__FAKE: ("NOT YOURS", ui.RED), OWNER__EXTERNAL: ("", ui.TEXT)}
        for o in report.outputs:
            tag, color = tags[o.owner]
            add(f"#{o.index}  {btc(o.amount)}" + (f"  {tag}" if tag else "") + (f" · {o.label}" if o.label else ""),
                color, 17)
            if o.path:
                add(f"     {o.path}", ui.DIM, 15, True)
            for chunk in ui.wrap(address.grouped(o.address), ui.font(15, mono=True), WIDTH - 5 * ui.PAD):
                add(f"     {chunk}", ui.DIM if o.owner == OWNER__EXTERNAL else color, 15, True)
        add(f"Fee: {btc(report.fee)}", ui.TEXT, 17)
        self.list = ui.ScrollList(lines, ui.HEADER_H + self.BANNER_H + 8, HEIGHT - ui.BUTTON_H - 2 * ui.PAD)
        self.can_export = can_export(report, txid_confirmed)
        if self.can_export:
            self.buttons = ui.bottom_row(["Finish", "Broadcast", "Next PSBT"],
                                         primary_index=2 if report.change_warning else 1,
                                         values=["done", "txqr", "again"], icons=["power", "qr", "camera"])
        else:
            self.buttons = ui.bottom_row(["Finish", "Next PSBT"], primary_index=1, values=["done", "again"],
                                         icons=["power", "camera"])

    def seed_mismatch(self) -> bool:
        return not self.report.seed_matches and self.report.verdict == VERDICT__FAIL and \
            not any(r.status == STATUS__FAIL for r in self.report.inputs)

    def banner(self):
        """ (color, icon, headline, subline) """
        if self.seed_mismatch():
            return ui.RED, "x_circle", "SEED DOES NOT MATCH", "check seed and passphrase"
        if self.report.verdict == VERDICT__OK and self.txid_confirmed and self.report.change_unverified:
            return ui.YELLOW, "warning", "CHANGE NOT VERIFIED", "do not broadcast yet: see below"
        if self.report.verdict == VERDICT__OK and self.txid_confirmed and self.report.change_warning:
            return ui.YELLOW, "warning", "NONCE OK · CHANGE?", "your change is somewhere unusual: see below"
        if self.report.verdict == VERDICT__OK and self.txid_confirmed:
            who = signer_name(self.signer) if self.signer in SIGNERS else None
            what = "tap Broadcast" if self.report.broadcast_tx else "broadcast"
            return ui.GREEN, "ok_circle", "NONCE OK", (f"{who} signatures clean, txid OK: {what}"
                                                       if who else f"no covert channel, txid OK: {what}")
        if self.report.verdict == VERDICT__PARTIAL and self.txid_confirmed:
            if self.report.unchecked_cosigners:
                return ui.YELLOW, "warning", "THIS SEED: CLEAN", \
                    f"do not broadcast yet: {self.report.unchecked_cosigners} cosigner signature(s) to check"
            return ui.YELLOW, "warning", "THIS SEED: CLEAN", "not complete: scan again when fully signed"
        if self.report.verdict in (VERDICT__OK, VERDICT__PARTIAL):
            # never shown by the app (the txid step comes first), kept for safety
            return ui.YELLOW, "warning", "COMPARE THE TXID", "nonces OK: compare the txid before broadcasting"
        if self.report.verdict == VERDICT__FAIL:
            return ui.RED, "x_circle", "DO NOT BROADCAST", "a signature or the transaction is not what it must be"
        return ui.YELLOW, "warning", "INCOMPLETE CHECK", "not everything can be verified: not green"

    def draw(self, d, image):
        color, icon_name, headline, subline = self.banner()
        top = ui.HEADER_H
        d.rectangle((0, top, WIDTH, top + self.BANNER_H), fill=color)
        d.ellipse((ui.PAD + 2, top + 9, ui.PAD + 46, top + 53), fill=ui.BACKGROUND)
        ui.icon(d, icon_name, (ui.PAD + 24, top + 31), 44, color)
        d.text((ui.PAD + 60, top + 22), headline, fill=ui.BACKGROUND, font=ui.font(26, bold=True), anchor="lm")
        d.text((ui.PAD + 60, top + 48), subline, fill=ui.BACKGROUND, font=ui.fit_font(subline, 15, WIDTH - ui.PAD - 70),
               anchor="lm")
        self.list.draw(d)

    def on_event(self, event):
        if event.kind == Event.SWIPE_UP:
            self.list.scroll(46)
            return "__redraw__"
        if event.kind == Event.SWIPE_DOWN:
            self.list.scroll(-46)
            return "__redraw__"
        if event.kind == Event.TAP and self.list.top <= event.y < self.list.bottom:
            # tap on the upper/lower half of the list: page up/down
            middle = (self.list.top + self.list.bottom) / 2
            self.list.scroll(-120 if event.y < middle else 120)
            return "__redraw__"
        return super().on_event(event)


class WorkingScreen:
    def __init__(self, hw):
        self.hw = hw

    def _show(self, headline, counter, note, fraction, step=2, label=None):
        image, d = ui.new_canvas()
        ui.header(d, step, label)
        ui.emblem(d, WIDTH // 2, 110, 80)
        d.text((WIDTH / 2, 180), headline, fill=ui.TEXT, font=ui.font(22, bold=True), anchor="mm")
        d.text((WIDTH / 2, 212), counter, fill=ui.ACCENT, font=ui.font(20, bold=True, mono=True), anchor="mm")
        x1, x2, y = 60, WIDTH - 60, 246
        d.rounded_rectangle((x1, y, x2, y + 8), radius=4, fill=ui.PANEL)
        d.rounded_rectangle((x1, y, x1 + max(8, (x2 - x1) * min(1.0, fraction)), y + 8), radius=4, fill=ui.ACCENT)
        d.text((WIDTH / 2, 282), note, fill=ui.DIM, font=ui.font(15), anchor="mm")
        self.hw.show(image)

    def scan(self, index: int, total: int):
        """ Looking for a key of this seed that the PSBT's paths don't give """
        self._show("Looking for the seed's keys", f"branch {index + 1} / {total}",
                   "a key is not at the paths given in the PSBT", (index + 1) / total)

    def __call__(self, index: int, total: int):
        self._show("Recomputing the RFC 6979 nonces", f"input {index + 1} / {total}",
                   "signature by signature, with code independent of the signer", (index + 1) / total)

    def address(self, done: int, total: int):
        self._show("Searching the address", f"{done} / {total} tried",
                   "derived from the seed, nothing is taken on trust", done / max(1, total), None, "ADDRESS")


# ---------------------------------------------------------------------------------------
# v7: check an address
# ---------------------------------------------------------------------------------------
class AddressModeScreen(ui.Screen):
    header_label = "ADDRESS"

    def __init__(self, descriptor=None):
        super().__init__()
        self.descriptor = descriptor
        self.buttons = ui.button_row(["Single-sig", "Multisig", "Explorer"], 166, height=56,
                                     values=["single", "descriptor", "explorer"], icons=["fingerprint", "key", "qr"])
        self.buttons += ui.bottom_row(["Back"], values=["back"], icons=["back"])

    def draw(self, d, image):
        ui.title(d, "Check an address")
        ui.paragraph(d, "Check: is the address Sparrow shows really this wallet's? Explorer: list the "
                        "wallet's addresses, derived here from the seed, with their QR.", ui.TOP + 34, size=16,
                     color=ui.DIM)
        note = ("Multisig: the descriptor scanned this session is used: "
                + self.descriptor.describe()) if self.descriptor else \
            "Multisig: you scan the wallet descriptor from your paper backup first."
        ui.paragraph(d, note, ui.TOP + 236 - 4 - 40 - 2, size=14, color=ui.DIM, max_lines=2)


class DescriptorScreen(ui.Screen):
    """
    The descriptor just scanned: what it is, which key is this seed's (verified by
    derivation), and the cosigners' keys to compare by eye with the paper backup.
    """
    header_label = "DESCRIPTOR"

    def __init__(self, desc, check):
        super().__init__()
        self.desc, self.check = desc, check
        lines = []
        add = lambda text, color=ui.TEXT, size=16, mono=False: lines.append((text, color, size, mono))
        if check.problems:
            for p in check.problems:
                for line in ui.wrap(p, ui.font(16), WIDTH - 3 * ui.PAD):
                    add(line, ui.RED, 16)
        for i, key in enumerate(desc.keys):
            ours = i in check.ours
            fp = key.origin_fingerprint.hex() if key.origin_fingerprint else "--------"
            steps = check.ours[i] if ours else key.origin_path
            path = crypto.format_path(steps) if steps else "(no origin)"
            add(f"#{i + 1} {fp}  {path}" + ("   ← THIS SEED" if ours else ""), ui.GREEN if ours else ui.TEXT, 16, True)
            # the whole xpub and what follows it: everything that decides the addresses
            for chunk in ui.wrap(address.grouped(key.xpub_text), ui.font(15, mono=True), WIDTH - 5 * ui.PAD):
                add(f"   {chunk}", ui.TEXT, 15, True)
            add(f"   then {key.suffix()}", ui.TEXT, 15, True)
        # v7.5 (independent review): start and end of an xpub can be forged with look-alike
        # keys, and the BIP380 checksum too: it detects damage, not tampering
        if desc.source == "text" and desc.checksum:
            add(f"checksum #{desc.checksum}: detects damage, not tampering", ui.DIM, 14, True)
        others = sum(1 for i in range(len(desc.keys)) if i not in check.ours)
        if others and not check.problems:
            for line in ui.wrap(f"Next: for each of the {others} other key(s) you find on the paper two groups "
                                "picked at random here.", ui.font(15), WIDTH - 3 * ui.PAD):
                add(line, ui.YELLOW, 15)
        self.list = ui.ScrollList(lines, ui.TOP + 34, HEIGHT - ui.BUTTON_H - 2 * ui.PAD)
        if check.problems:
            self.buttons = ui.bottom_row(["Back"], values=["back"], icons=["back"])
        else:
            self.buttons = ui.bottom_row(["Back", "Check keys"], primary_index=1, values=["back", "ok"])

    def draw(self, d, image):
        color = ui.RED if self.check.problems else ui.TEXT
        title = "NOT USABLE" if self.check.problems else self.desc.describe()
        d.text((ui.PAD, ui.TOP + 14), title, fill=color, font=ui.font(20, bold=True), anchor="lm")
        self.list.draw(d)

    def on_event(self, event):
        if event.kind == Event.SWIPE_UP:
            self.list.scroll(44)
            return "__redraw__"
        if event.kind == Event.SWIPE_DOWN:
            self.list.scroll(-44)
            return "__redraw__"
        if event.kind == Event.TAP and self.list.top <= event.y < self.list.bottom:
            # v7.5: tap paging as on the result (swipes are unreliable on the resistive panel)
            middle = (self.list.top + self.list.bottom) / 2
            self.list.scroll(-110 if event.y < middle else 110)
            return "__redraw__"
        return super().on_event(event)


KEY_CHECK_GROUPS = 3


def key_dependent_start(xpub_text: str) -> int:
    """
    v7.5 review: the first character of the xpub that depends on its PUBLIC KEY. Base58
    writes the 78 bytes as one number: the leading ~60 characters are fixed by version,
    depth, parent fingerprint, child number and chain code, all public, so a look-alike
    key with the real chain code matches them for free. Found by changing the key and
    seeing where the text changes (a few variants: the earliest difference wins).
    """
    from nonceverify.psbt import base58check
    raw = address.b58decode_check(xpub_text)
    first = len(xpub_text)
    for n in range(1, 9):
        other = crypto.pubkey_compressed(crypto.point_mul_g(n * 0x1000003 + 7))
        text = base58check(raw[:45] + other)
        first = min(first, next((i for i, (a, b) in enumerate(zip(text, xpub_text)) if a != b), first))
    return first


def pick_groups(xpub_text: str, rng=None, count: int = KEY_CHECK_GROUPS) -> list:
    """
    Group indexes (groups of four) to find on the paper, chosen by the device at random
    among the groups that depend on the public key, the last (short) group excluded.
    Matching one unpredictable group costs a forger ~58^4 = 2^23 tries per key and
    group; matching enough of them to pass three random picks is out of reach.
    """
    rng = rng or RNG
    groups = address.grouped(xpub_text).split(" ")
    lo = (key_dependent_start(xpub_text) + 3) // 4            # first whole group after the change
    hi = len(groups) - 1 if len(groups[-1]) < 4 else len(groups)
    pool = list(range(lo, hi))
    picked = []
    for _ in range(min(count, len(pool))):
        picked.append(pool.pop(rng.randrange(len(pool))))
    return sorted(picked)


class KeyCheckScreen(ui.Screen):
    """
    v7.5 (independent review): a cosigner key of the descriptor scanned, with two groups of
    its xpub picked at random and highlighted. The user finds them on the paper backup.
    A forged look-alike key can match start, end and even the checksum, but not groups
    the attacker could not predict. The groups come from the part of the xpub that
    depends on the public key (pick_groups): the leading part is chain code and public
    metadata and can be copied into a look-alike key.
    """
    header_label = "DESCRIPTOR"
    COLS = 7

    def __init__(self, desc, index: int, rng=None):
        super().__init__()
        self.desc, self.index = desc, index
        self.key = desc.keys[index]
        self.groups = address.grouped(self.key.xpub_text).split(" ")
        self.picked = pick_groups(self.key.xpub_text, rng)
        self.buttons = ui.bottom_row(["Different", "Same on paper"], primary_index=1, values=["different", "same"])
        self.buttons[0].color = ui.RED

    def draw(self, d, image):
        fp = self.key.origin_fingerprint.hex() if self.key.origin_fingerprint else "--------"
        path = crypto.format_path(self.key.origin_path) if self.key.origin_path else "(no origin)"
        d.text((ui.PAD, ui.TOP + 12), f"Key #{self.index + 1} of {len(self.desc.keys)}  {fp}  {path}",
               fill=ui.TEXT, font=ui.fit_font(f"Key #{self.index + 1} of {len(self.desc.keys)}  {fp}  {path}", 17,
                                              WIDTH - 2 * ui.PAD, bold=True), anchor="lm")
        f = ui.font(16, mono=True)
        cell = ui.text_width("WWWW ", f) + 2
        y = ui.TOP + 30
        for n, group in enumerate(self.groups):
            col, row = n % self.COLS, n // self.COLS
            x = ui.PAD + 4 + col * cell
            gy = y + row * 22
            if n in self.picked:
                d.rounded_rectangle((x - 3, gy - 2, x + ui.text_width(group, f) + 3, gy + 19), radius=3,
                                    fill=ui.ACCENT)
            d.text((x, gy), group, fill=ui.BACKGROUND if n in self.picked else ui.DIM, font=f)
        y += ((len(self.groups) + self.COLS - 1) // self.COLS) * 22 + 6
        for n in self.picked:
            start = n * 4 + 1
            d.text((ui.PAD, y), f"characters {start}-{start + len(self.groups[n]) - 1}:", fill=ui.TEXT,
                   font=ui.font(16), anchor="lt")
            d.text((ui.PAD + 190, y - 3), self.groups[n], fill=ui.ACCENT, font=ui.font(22, bold=True, mono=True),
                   anchor="lt")
            y += 24
        d.text((ui.PAD, y + 2), "Find them in this key's xpub on your paper backup.", fill=ui.YELLOW,
               font=ui.font(15), anchor="lt")


class ThresholdScreen(ui.Screen):
    """ v7.5: M-of-N confirmed against the paper (an extra key + threshold 1 changed only the title) """
    header_label = "DESCRIPTOR"

    def __init__(self, desc):
        super().__init__()
        self.desc = desc
        m, n = desc.threshold, len(desc.keys)
        self.buttons = ui.bottom_row(["No", f"Yes, {m} of {n}"], primary_index=1, values=["no", "yes"])
        self.buttons[0].color = ui.RED

    def draw(self, d, image):
        ui.title(d, "Does your paper backup say")
        d.text((WIDTH / 2, ui.TOP + 96), f"{self.desc.threshold} of {len(self.desc.keys)}", fill=ui.ACCENT,
               font=ui.font(54, bold=True), anchor="mm")
        d.text((WIDTH / 2, ui.TOP + 150), self.desc.describe(), fill=ui.TEXT, font=ui.font(18), anchor="mm")
        ui.paragraph(d, "signatures needed, out of the keys written on it?", ui.TOP + 172, size=16, color=ui.DIM)


class AddressResultScreen(ui.Screen):
    header_label = "ADDRESS"
    BANNER_H = 62

    def __init__(self, addr, match, wallet: str, searched: str, error: str = None):
        super().__init__()
        self.addr, self.match, self.error = addr, match, error
        lines = []
        add = lambda text, color=ui.TEXT, size=17, mono=False: lines.append((text, color, size, mono))
        self.unusual = False
        if addr is not None:
            for chunk in ui.wrap(address.grouped(addr.text), ui.font(22, bold=True, mono=True), WIDTH - 3 * ui.PAD):
                add(chunk, ui.TEXT, 22, True)
            if addr.extras:
                # v7.5: lightning=, pj=, req-...: another payment instruction on the same QR
                for line in ui.wrap("The QR also carries: " + ", ".join(addr.extras) + ". A payer's wallet could "
                                    "follow that instead of paying this address.", ui.font(16), WIDTH - 3 * ui.PAD):
                    add(line, ui.RED, 16)
            if addr.amount is not None:
                add(f"amount in the QR: {addr.amount[:20]} BTC", ui.TEXT, 17)
        if error:
            for line in ui.wrap(error, ui.font(17), WIDTH - 3 * ui.PAD):
                add(line, ui.RED, 17)
        elif match is not None:
            where = "change" if match.change else "receive"
            add(f"{where} #{match.index}" if match.index is not None else where, ui.GREEN, 19)
            add(crypto.format_path(match.path) if match.account is not None else "…/" + address.format_steps(match.path),
                ui.DIM, 15, True)
            if match.account is not None and (match.account or match.path[0] != (84 | address.H) or addr.network != "main"):
                for line in ui.wrap(f"{match.label}, account {match.account}" + (" (testnet)" if addr.network != "main" else "")
                                    + ": make sure your wallet in Sparrow uses this type and account",
                                    ui.font(15), WIDTH - 3 * ui.PAD):
                    add(line, ui.YELLOW, 15)
                self.unusual = bool(match.account) or addr.network != "main"
            for line in ui.wrap("Compare it, all of it, with the address you will give out.", ui.font(16),
                                WIDTH - 3 * ui.PAD):
                add(line, ui.TEXT, 16)
        for text, size in ((wallet, 15), (searched, 14)):
            for line in ui.wrap(text, ui.font(size), WIDTH - 3 * ui.PAD) if text else []:
                add(line, ui.DIM, size)
        self.list = ui.ScrollList(lines, ui.HEADER_H + self.BANNER_H + 8, HEIGHT - ui.BUTTON_H - 2 * ui.PAD)
        self.buttons = ui.bottom_row(["Done", "Next address"], primary_index=1, values=["done", "again"],
                                     icons=["back", "camera"])

    def banner(self):
        if self.error:
            return ui.RED, "x_circle", "NOT CHECKED", "this address can't be checked here"
        if self.addr is not None and self.addr.extras:
            return ui.RED, "x_circle", "DO NOT SHARE THIS QR", "it carries other payment instructions"
        if self.match is not None and self.unusual:
            return ui.YELLOW, "warning", "YOUR ADDRESS · CHECK", "unusual account or network: see below"
        if self.match is not None:
            return ui.GREEN, "ok_circle", "YOUR ADDRESS", ("derived from the seed loaded" if self.match.account is not None
                                                           else "in the descriptor you confirmed")
        return ui.RED, "x_circle", "NOT YOUR ADDRESS", "not found: do not use it to receive"

    def draw(self, d, image):
        color, icon_name, headline, subline = self.banner()
        top = ui.HEADER_H
        d.rectangle((0, top, WIDTH, top + self.BANNER_H), fill=color)
        d.ellipse((ui.PAD + 2, top + 9, ui.PAD + 46, top + 53), fill=ui.BACKGROUND)
        ui.icon(d, icon_name, (ui.PAD + 24, top + 31), 44, color)
        d.text((ui.PAD + 60, top + 22), headline, fill=ui.BACKGROUND, font=ui.font(26, bold=True), anchor="lm")
        d.text((ui.PAD + 60, top + 48), subline, fill=ui.BACKGROUND, font=ui.fit_font(subline, 15, WIDTH - ui.PAD - 70),
               anchor="lm")
        self.list.draw(d)

    def on_event(self, event):
        if event.kind == Event.SWIPE_UP:
            self.list.scroll(46)
            return "__redraw__"
        if event.kind == Event.SWIPE_DOWN:
            self.list.scroll(-46)
            return "__redraw__"
        return super().on_event(event)


# ---------------------------------------------------------------------------------------
# v7.3: address explorer
# ---------------------------------------------------------------------------------------
EXPLORER_PAGE = 4                     # v7.5: 40-pixel rows (were 6 of 26)
EXPLORER_MAX_INDEX = 0x7FFFFFFF - EXPLORER_PAGE
EXPLORER_MAX_ACCOUNT = 99


def indexed(explorer) -> bool:
    """ Addresses numbered by an index (a descriptor without /* has a single address) """
    return getattr(explorer, "wildcard", lambda: True)()


def short_address(text: str, f, width: int) -> str:
    """ The address in groups of four, the middle elided if it doesn't fit """
    full = address.grouped(text)
    if ui.text_width(full, f) <= width:
        return full
    groups = full.split(" ")
    return " ".join(groups[:4]) + " … " + " ".join(groups[-2:])


def checked_qr(data: str, size: int):
    """
    A QR code of `data` at most `size` pixels wide, or None. Made with the `qrcode`
    package that SeedSigner OS ships (public data only), then read back with the same
    zbar decoder as the camera: if what it reads isn't EXACTLY `data` (v7.5: case
    included; base58 is case-sensitive), no QR is shown at all.
    """
    try:
        import qrcode
        from PIL import Image
        q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=1)
        q.add_data(data)
        q.make(fit=True)
        modules = q.make_image(fill_color="black", back_color="white").convert("RGB")
        scale = max(1, size // modules.size[0])
        image = modules.resize((modules.size[0] * scale, modules.size[1] * scale), Image.NEAREST)
        payload = decode_qr(image)
    except QRDecoderError:
        raise
    except Exception as e:
        logger.info(f"QR not available: {type(e).__name__}")
        return None
    if payload is None or payload.decode("ascii", "replace") != data:
        logger.info("QR self-check failed")
        return None
    return image


def address_qr(text: str, size: int):
    """ The address as a QR (bech32 uppercase: a smaller QR, and BIP173 allows it) """
    bech32 = text.lower().startswith(("bc1", "tb1", "bcrt1"))
    return checked_qr(text.upper() if bech32 else text, size)


# v7.5: the verified transaction as BBQr (Coinkite), file type "T", base32: Sparrow reads
# it with File > Load Transaction > From QR (and Coldcard Q too). Parts sized for QR
# version 8 (M): 4-pixel modules in the 230-pixel box, easy for a webcam.
BBQR_PART_CHARS = 192


def bbqr_parts(raw: bytes, file_type: str = "T") -> list:
    data = base64.b32encode(raw).decode("ascii").rstrip("=")
    chunks = [data[i:i + BBQR_PART_CHARS] for i in range(0, len(data), BBQR_PART_CHARS)] or [""]
    if len(chunks) > 36 * 36 - 1:
        raise ValueError("transaction too large for BBQr")

    def b36(n):
        digits = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        return digits[n // 36] + digits[n % 36]
    return [f"B$2{file_type}{b36(len(chunks))}{b36(i)}{c}" for i, c in enumerate(chunks)]


class BroadcastScreen(ui.Screen):
    """
    v7.5: the transaction verified here, as an animated QR, to be loaded into Sparrow and
    broadcast. A segwit txid does not cover the signatures: only these bytes are the
    ones checked (independent review, 10 Nov 2026).
    """
    step = 2
    QR_BOX = 228
    FRAME_S = 0.6

    def __init__(self, report):
        super().__init__()
        self.report = report
        self.parts = bbqr_parts(report.broadcast_tx)
        self.frames = [checked_qr(p, self.QR_BOX) for p in self.parts]
        self.ok = all(f is not None for f in self.frames)
        self.index = 0
        self.buttons = ui.bottom_row(["Back"], values=["back"], icons=["back"])

    def draw(self, d, image):
        x = ui.PAD
        if self.ok:
            frame = self.frames[self.index]
            d.rectangle((x, ui.TOP, x + self.QR_BOX, ui.TOP + self.QR_BOX), fill=(255, 255, 255))
            image.paste(frame, (x + (self.QR_BOX - frame.size[0]) // 2, ui.TOP + (self.QR_BOX - frame.size[1]) // 2))
        else:
            d.text((x, ui.TOP + 20), "QR not available", fill=ui.RED, font=ui.font(18, bold=True))
        x += self.QR_BOX + 12
        width = WIDTH - ui.PAD - x
        d.text((x, ui.TOP + 12), "Broadcast THIS", fill=ui.GREEN, font=ui.font(19, bold=True), anchor="lm")
        y = ui.TOP + 30
        for line in ui.wrap("Sparrow: File > Load Transaction > From QR, then Broadcast. The txid must "
                            "be the same as before.", ui.font(14), width):
            d.text((x, y), line, fill=ui.TEXT, font=ui.font(14))
            y += 18
        y += 6
        d.text((x, y), "txid ends", fill=ui.DIM, font=ui.font(13))
        d.text((x, y + 16), "…" + self.report.txid[-8:-4] + " " + self.report.txid[-4:], fill=ui.ACCENT,
               font=ui.font(18, bold=True, mono=True))
        d.text((x, ui.TOP + self.QR_BOX - 6), f"QR {self.index + 1} / {len(self.parts)} · BBQr",
               fill=ui.DIM, font=ui.font(13, mono=True), anchor="ls")

    def run(self, hw, timeout: float = None):
        hw.show(self.render())
        shown = time.monotonic()
        tick = getattr(hw, "battery_tick", None)
        while True:
            ui.housekeeping(hw)
            if tick is not None and tick():
                hw.show(self.render())
            for event in hw.poll():
                result = self.on_event(event)
                if result is not None and result != "__redraw__":
                    return result
            if len(self.frames) > 1 and time.monotonic() - shown >= self.FRAME_S:
                self.index = (self.index + 1) % len(self.frames)
                hw.show(self.render())
                shown = time.monotonic()
            hw.wait()


class ExplorerWalletScreen(ui.Screen):
    header_label = "EXPLORER"

    def __init__(self, descriptor=None):
        super().__init__()
        self.descriptor = descriptor
        self.buttons = ui.button_row(["Native bc1q · 84", "Nested 3… · 49"], ui.TOP + 40, height=56,
                                     values=["84", "49"])
        self.buttons += ui.button_row(["Legacy 1… · 44", "Multisig"], ui.TOP + 106, height=56,
                                      values=["44", "multisig"], icons=[None, "key"])
        self.buttons += ui.bottom_row(["Back"], values=["back"], icons=["back"])

    def draw(self, d, image):
        ui.title(d, "Which addresses?")
        note = ("Multisig: " + self.descriptor.describe()) if self.descriptor else \
            "Multisig: you scan the descriptor from your paper backup first."
        ui.paragraph(d, note, ui.TOP + 176, size=14, color=ui.DIM, max_lines=2)


class ExplorerListScreen(ui.Screen):
    header_label = "EXPLORER"
    ROW_TOP = ui.TOP + 66
    ROW_H = 40

    def __init__(self, explorer, change: bool, start: int, singlesig: bool):
        super().__init__()
        self.explorer, self.change, self.start = explorer, change, start
        self.rows = [explorer.address(change, start + i) for i in range(EXPLORER_PAGE)] \
            if indexed(explorer) else [explorer.address(change, 0)]
        top = ui.TOP + 2
        # v7.5: two segments instead of a toggle that turned teal (like an action button)
        self.buttons = [ui.Button((ui.PAD, top, 78, top + 38), "Recv", value="receive", size=17, primary=not change),
                        ui.Button((82, top, 150, top + 38), "Chg", value="change", size=17, primary=change)]
        if singlesig:
            self.buttons += [ui.Button((160, top, 204, top + 38), "−", value="acct-", size=22),
                             ui.Button((290, top, 334, top + 38), "+", value="acct+", size=22)]
        self.buttons.append(ui.Button((WIDTH - ui.PAD - 120, top, WIDTH - ui.PAD, top + 38), "Go to #",
                                      value="goto", size=17))
        self.buttons += ui.bottom_row(["Back", "Prev", "Next"], values=["back", "prev", "next"],
                                      icons=["back", None, None])
        self.singlesig = singlesig

    def draw(self, d, image):
        if self.singlesig:
            d.text((247, ui.TOP + 21), f"acct {self.explorer.account}", fill=ui.TEXT,
                   font=ui.font(16, bold=True, mono=True), anchor="mm")
        d.text((ui.PAD, ui.TOP + 54), self.explorer.label, fill=ui.DIM, font=ui.font(14), anchor="lm")
        f = ui.font(16, mono=True)
        f_index = ui.font(15, bold=True, mono=True)
        col = max(50, max((ui.text_width(f"#{steps[-1]}", f_index) + 6 for _t, steps in self.rows), default=50)) \
            if indexed(self.explorer) else 50
        for i, (text, steps) in enumerate(self.rows):
            y = self.ROW_TOP + i * self.ROW_H
            d.rounded_rectangle((ui.PAD, y + 1, WIDTH - ui.PAD, y + self.ROW_H - 3), radius=4,
                                fill=ui.PANEL, outline=ui.LINE)
            index = steps[-1] if indexed(self.explorer) else None
            d.text((ui.PAD + col, y + self.ROW_H / 2 - 1), f"#{index}" if index is not None else "", fill=ui.ACCENT,
                   font=f_index, anchor="rm")
            d.text((ui.PAD + col + 8, y + self.ROW_H / 2 - 1), short_address(text, f, WIDTH - 2 * ui.PAD - col - 12),
                   fill=ui.TEXT, font=f, anchor="lm")

    def on_event(self, event):
        value = super().on_event(event)
        if value is not None:
            return value
        if event.kind == Event.SWIPE_UP:
            return "next"
        if event.kind == Event.SWIPE_DOWN:
            return "prev"
        if event.kind == Event.TAP and self.ROW_TOP <= event.y < self.ROW_TOP + len(self.rows) * self.ROW_H:
            return ("row", int((event.y - self.ROW_TOP) // self.ROW_H))
        return None


class AddressDetailScreen(ui.Screen):
    header_label = "EXPLORER"
    QR_SIZE = 222

    def __init__(self, text: str, where: str, path: str, label: str):
        super().__init__()
        self.text, self.where, self.path, self.label = text, where, path, label
        self.qr = address_qr(text, self.QR_SIZE)
        self.buttons = ui.bottom_row(["Back"], values=["back"], icons=["back"])

    def draw(self, d, image):
        x = ui.PAD
        if self.qr is not None:
            box = self.QR_SIZE
            d.rectangle((x, ui.TOP + 2, x + box, ui.TOP + 2 + box), fill=(255, 255, 255))
            image.paste(self.qr, (x + (box - self.qr.size[0]) // 2, ui.TOP + 2 + (box - self.qr.size[1]) // 2))
            x += box + 12
        width = WIDTH - ui.PAD - x
        d.text((x, ui.TOP + 14), self.where, fill=ui.GREEN, font=ui.font(20, bold=True), anchor="lm")
        y = ui.TOP + 32
        for line in ui.wrap(self.path, ui.font(14, mono=True), width)[:2]:
            d.text((x, y), line, fill=ui.DIM, font=ui.font(14, mono=True))
            y += 17
        y += 6
        size = 18 if self.qr is not None else 22
        f = ui.font(size, bold=True, mono=True)
        for line in ui.wrap(address.grouped(self.text), f, width):
            d.text((x, y), line, fill=ui.TEXT, font=f)
            y += size + 4
        if self.qr is None:
            d.text((x, y + 8), "QR not available: compare the text", fill=ui.YELLOW, font=ui.font(14))
        d.text((x, ui.TOP + 214), self.label, fill=ui.DIM, font=ui.fit_font(self.label, 13, width), anchor="ls")


def explorer_flow(hw, session):
    while True:
        choice = ExplorerWalletScreen(session.descriptor).run(hw)
        if choice not in ("84", "49", "44", "multisig"):
            return
        if choice == "multisig":
            if session.descriptor is None and not load_descriptor(hw, session):
                continue
            explorer, singlesig = address.DescriptorExplorer(session.descriptor), False
        else:
            explorer, singlesig = address.SinglesigExplorer(session.master, int(choice), 0), True
        change, start = False, 0
        while True:
            value = ExplorerListScreen(explorer, change, start, singlesig).run(hw)
            if value == "back":
                break
            if value in ("receive", "change") and (value == "receive" or explorer.has_change()):
                change = value == "change"
            elif value in ("acct-", "acct+") and singlesig:
                account = min(EXPLORER_MAX_ACCOUNT, max(0, explorer.account + (1 if value == "acct+" else -1)))
                if account != explorer.account:
                    explorer, start = address.SinglesigExplorer(session.master, explorer.purpose, account), 0
            elif value == "prev":
                start = max(0, start - EXPLORER_PAGE)
            elif value == "next":
                start = min(EXPLORER_MAX_INDEX, start + EXPLORER_PAGE)
            elif value == "goto":
                result = ui.NumberPadScreen("Go to address #").run(hw)       # v7.5: digits only
                if result[0] == "done" and result[1].isdigit() and len(result[1]) <= 10:
                    start = min(EXPLORER_MAX_INDEX, int(result[1]))
            elif isinstance(value, tuple) and value[0] == "row":
                text, steps = explorer.address(change, start + value[1] if indexed(explorer) else 0)
                where = ("change" if change else "receive") + (f" #{steps[-1]}" if indexed(explorer) else "")
                path = crypto.format_path(steps) if singlesig else "…/" + address.format_steps(steps)
                AddressDetailScreen(text, where, path, explorer.label).run(hw)


def scan_address(hw):
    found = {}

    def on_payload(payload):
        found["address"] = address.parse_address(payload)
        return True, 1.0, ""

    if ScanScreen("Address", on_payload, errors=(address.AddressError,)).run(hw) is None:
        return None
    return found["address"]


def load_descriptor(hw, session) -> bool:
    collector = transport.DescriptorCollector()

    def on_payload(payload):
        collector.restarted = False
        collector.add(payload)
        return collector.complete, collector.progress, "QR of another descriptor: starting over" if collector.restarted else ""

    if ScanScreen("Descriptor from backup", on_payload, errors=(address.DescriptorError,)).run(hw) is None:
        return False
    desc = collector.result
    check = address.check_seed_in_descriptor(desc, session.master)
    if DescriptorScreen(desc, check).run(hw) != "ok" or check.problems:
        return False
    # v7.5 (independent review): random groups of every other key found on the paper,
    # then the threshold. Start, end and checksum alone can be forged.
    for i in range(len(desc.keys)):
        if i in check.ours:
            continue
        if KeyCheckScreen(desc, i).run(hw) != "same":
            ui.MessageScreen("Not your backup", f"Key #{i + 1} of the QR is not the one on your paper: the "
                             "descriptor QR was altered or belongs to another wallet. Don't use it.",
                             heading_color=ui.RED, icon_name="x_circle").run(hw)
            return False
    if desc.is_multisig and ThresholdScreen(desc).run(hw) != "yes":
        ui.MessageScreen("Not your backup", "The number of signatures needed is not the one on your paper: "
                         "the descriptor QR was altered. Don't use it.", heading_color=ui.RED,
                         icon_name="x_circle").run(hw)
        return False
    session.descriptor = desc
    return True


def address_flow(hw, session):
    """ "Address" on the seed screen. Returns when the user goes back to the seed screen """
    while True:
        mode = AddressModeScreen(session.descriptor).run(hw)
        if mode == "explorer":
            explorer_flow(hw, session)
            continue
        if mode not in ("single", "descriptor"):
            return
        if mode == "descriptor" and session.descriptor is None and not load_descriptor(hw, session):
            continue
        while True:
            addr = scan_address(hw)
            if addr is None:
                break
            working = WorkingScreen(hw)
            working.address(0, 1)
            match, error = None, None
            try:
                if mode == "single":
                    match = address.search_singlesig(session.master, addr, progress=working.address)
                    wallet = f"single-sig, XFP {session.master.fingerprint.hex()}"
                    searched = (f"searched: account 0 up to #{address.GAP_ACCOUNT0 - 1}, accounts 1-"
                                f"{address.OTHER_ACCOUNTS} up to #{address.GAP_OTHER_ACCOUNTS - 1}, receive and change")
                    if match is None and addr.kind == address.KIND__P2SH:
                        searched += " (BIP49). A 3… address can also be a P2SH multisig: check it with Multisig"
                else:
                    desc = session.descriptor
                    match = address.search_descriptor(desc, addr, progress=working.address)
                    wallet = desc.describe()
                    searched = f"searched: receive and change up to #{address.GAP_ACCOUNT0 - 1}"
            except address.AddressError as e:
                error, wallet, searched = str(e), "", ""
            choice = AddressResultScreen(addr, match, wallet, searched, error).run(hw)
            if choice != "again":
                return


# ---------------------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------------------
class Session:
    """ What the app keeps between screens: the BIP32 master node, nothing else secret """
    def __init__(self):
        self.master = None
        self.words = 0
        self.has_passphrase = False
        self.signer = None               # chosen by name after every seed load
        self.signer_locked = False       # True once "Scan PSBT" was pressed in this seed session
        self.failed = {}                 # txid -> signer it was red under; kept until power-off (v7)
        self.descriptor = None           # v7: wallet descriptor for "Check address" (public data)
        # v7.5: (pubkey, signature) pairs whose nonce a seed of this power-on verified,
        # green and txid confirmed. Public data, kept across "Change seed": a multisig's
        # cosigner signatures become checked when their own seed is loaded and the same
        # PSBT scanned again.
        self.clean_sigs = set()
        self.coldcard_intro_shown = False

    def wipe(self):
        if self.master is not None:
            self.master.wipe()
        self.master = None
        self.signer = None
        self.signer_locked = False
        # self.failed is kept: a transaction that came out red stays red until power-off,
        # also after "Change seed" (txids are public data)
        self.words = 0
        self.has_passphrase = False
        # v7.6 review: the descriptor is kept (public data, every key of it verified by
        # derivation or on the paper): the cosigner's seed checks the same multisig change

    def set_seed(self, mnemonic: str, passphrase: str):
        if self.master is not None:
            self.master.wipe()
        self.master = crypto.HDKey.from_seed(crypto.bip39_seed(mnemonic, passphrase))
        self.words = len(mnemonic.split())
        self.has_passphrase = bool(passphrase)

    @staticmethod
    def fingerprint_of(mnemonic: str, passphrase: str) -> str:
        node = crypto.HDKey.from_seed(crypto.bip39_seed(mnemonic, passphrase))
        try:
            return node.fingerprint.hex()
        finally:
            node.wipe()


def load_seed(hw, session: Session, method: str = "scan_seed") -> bool:
    if method == "type_seed":
        mnemonic = wordentry.enter_mnemonic(hw)
        if mnemonic is None:
            return False
    else:
        found = {}

        def on_payload(payload):
            found["mnemonic"] = seedqr.decode(payload)
            return True, 1.0, ""

        if ScanScreen("SeedQR", on_payload).run(hw) is None:
            return False
        mnemonic = found.pop("mnemonic")
    words = len(mnemonic.split())
    choice = PassphraseQuestionScreen(Session.fingerprint_of(mnemonic, ""), words).run(hw)
    passphrase = ""
    if choice == "enter":
        while True:
            result = ui.KeyboardScreen(passphrase).run(hw)
            passphrase = result[1]
            if ConfirmPassphraseScreen(passphrase, Session.fingerprint_of(mnemonic, passphrase)).run(hw) == "ok":
                break
    session.set_seed(mnemonic, passphrase)
    del mnemonic, passphrase
    return True


def check_psbt(hw, session: Session) -> str:
    collector = transport.PSBTCollector()

    def on_payload(payload):
        collector.restarted = False
        collector.add(payload)
        note = "QR of another PSBT: starting over" if collector.restarted else ""
        return collector.complete, collector.progress, note

    coldcard = session.signer == SIGNER__COLDCARD_Q
    if coldcard and not session.coldcard_intro_shown:
        session.coldcard_intro_shown = True
        ui.MessageScreen("Coldcard Q: two QR codes", "Both from Sparrow, in any order:\n"
                         "1. the signed transaction: load the Q's .txn in Sparrow, then show it as QR;\n"
                         "2. the UNSIGNED PSBT of the same transaction.\n"
                         "At the end, broadcast the Tx QR shown here.",
                         buttons=("Scan",), heading_color=ui.ACCENT, icon_name="camera").run(hw)
    heading = f"PSBT · {signer_name(session.signer)}"
    if ScanScreen(heading, on_payload).run(hw) is None:
        return "back"
    try:
        first = collector.result
        if transport.looks_like_final_tx(first):
            # v7.2: a final transaction (Coldcard Q): its unsigned PSBT gives the amounts
            parse_tx(first)
            ui.MessageScreen("1 of 2: final transaction read",
                             "Now the UNSIGNED PSBT of the same transaction, from Sparrow: it carries the "
                             "amounts and paths needed to check the signatures.",
                             buttons=("Scan unsigned PSBT",), heading_color=ui.ACCENT, icon_name="camera").run(hw)
            unsigned = scan_second(hw, "2 of 2: unsigned PSBT", final=False)
            if unsigned is None:
                return "back"
            psbt = combine_final_tx(parse_psbt(unsigned), first)
        else:
            psbt = parse_psbt(first)
            if not has_signatures(psbt):
                if not coldcard:
                    ui.MessageScreen("PSBT without signatures", "This is the unsigned PSBT. Scan the SIGNED one "
                                     "from Sparrow (after loading what the signer returned).",
                                     heading_color=ui.YELLOW, icon_name="warning").run(hw)
                    return "again"
                ui.MessageScreen("1 of 2: unsigned PSBT read",
                                 "Now the signed transaction, from Sparrow (the Q's .txn loaded there).",
                                 buttons=("Scan final transaction",), heading_color=ui.ACCENT,
                                 icon_name="camera").run(hw)
                final = scan_second(hw, "2 of 2: final transaction", final=True)
                if final is None:
                    return "back"
                psbt = combine_final_tx(psbt, final)
        working = WorkingScreen(hw)
        report = verify_psbt(psbt, session.master, progress=working, scan_progress=working.scan,
                             grinding=signer_grinding(session.signer), clean_elsewhere=session.clean_sigs,
                             descriptor=session.descriptor)
    except (PSBTError, ValueError) as e:
        ui.MessageScreen("PSBT not readable", str(e)[:160] + ". Scan it again from Sparrow; if it keeps "
                         "failing, export the PSBT again.", heading_color=ui.RED, icon_name="x_circle").run(hw)
        return "again"
    except Exception as e:
        logger.exception("verification error")
        ui.MessageScreen("Verification error", f"{type(e).__name__}: not checked, so not green. The seed "
                         "stays loaded: try the scan again.", heading_color=ui.RED, icon_name="warning").run(hw)
        return "again"
    lock_after_red(session, report)
    confirmed = False
    if report.verdict in (VERDICT__OK, VERDICT__PARTIAL):
        # v7: no green before the txid has been compared with Sparrow's (v7.8: read and
        # confirm, no quiz on the ending)
        if TxidCheckScreen(report).run(hw) == "same":
            confirmed = True
            for r in report.inputs:
                session.clean_sigs.update(r.checked_sigs)
        else:
            report.verdict = VERDICT__FAIL
            report.tx_problem = ("The txid is NOT the one in Sparrow: the transaction checked here is not "
                                 "the one Sparrow would broadcast. Do not broadcast.")
            # v7.5 review: a txid mismatch says nothing about the
            # signer: it is not recorded as red for the other seeds and devices of a multisig
    while True:
        choice = ResultScreen(report, session.signer, txid_confirmed=confirmed).run(hw)
        if choice != "txqr":
            return choice
        BroadcastScreen(report).run(hw)


def scan_second(hw, heading: str, final: bool):
    """ The second QR of a final transaction + unsigned PSBT pair (v7.2), raw bytes or None """
    state = {"collector": transport.PSBTCollector(), "result": None}

    def on_payload(payload):
        collector = state["collector"]
        collector.restarted = False
        collector.add(payload)
        if collector.complete:
            state["collector"] = transport.PSBTCollector()         # wrong kind: start clean
            if transport.looks_like_final_tx(collector.result) != final:
                raise transport.TransportError(transport.NOT_FINAL_TX_MESSAGE if final else
                                               "this is the final transaction again: scan the unsigned PSBT")
            state["result"] = collector.result
            return True, 1.0, ""
        return False, collector.progress, "QR of another PSBT: starting over" if collector.restarted else ""

    if ScanScreen(heading, on_payload).run(hw) is None:
        return None
    return state["result"]


def lock_after_red(session: Session, report):
    """
    Defence in depth behind Session.signer_locked: a transaction that came out red stays
    red for the rest of this seed session, under whatever signer.
    """
    earlier = session.failed.get(report.txid) if report.txid else None
    if earlier is not None and earlier != session.signer and report.verdict != VERDICT__FAIL:
        report.verdict = VERDICT__FAIL
        message = (f"This transaction already came out NOT valid with «{signer_name(earlier)}». "
                   "Changing the signer doesn't make it clean: do not broadcast.")
        report.tx_problem = message + (" " + report.tx_problem if report.tx_problem else "")
    # "SEED DOES NOT MATCH" (wrong seed or passphrase) says nothing about the signer: not kept
    seed_mismatch = not report.seed_matches and not report.tx_problem and \
        not any(r.status == STATUS__FAIL for r in report.inputs)
    if report.verdict == VERDICT__FAIL and report.txid and report.txid not in session.failed and not seed_mismatch:
        session.failed[report.txid] = session.signer


def run(hw, session: Session = None, splash: bool = True):
    session = session or Session()
    if splash:
        SplashScreen().run(hw)              # v7.4: until START
    while True:
        if session.master is None:
            method = HomeScreen().run(hw)
            try:
                if not load_seed(hw, session, method):
                    continue
            except seedqr.SeedQRError as e:
                ui.MessageScreen("Seed not valid", str(e), heading_color=ui.RED, icon_name="x_circle").run(hw)
                continue
        ready = SeedReadyScreen(session.master.fingerprint.hex(), session.words,
                                session.has_passphrase, session.signer, session.signer_locked)
        choice = ready.run(hw)
        if not session.signer_locked:
            session.signer = ready.signer
        if choice == "change_seed":
            if confirm_wipe(hw):
                session.wipe()
            continue
        if choice == "address":
            address_flow(hw, session)
            continue
        if choice != "scan_psbt" or session.signer not in SIGNERS:
            continue
        # One signer per seed session, fixed as soon as a scan starts (v7; before, after
        # the first verdict). Each rule accepts exactly one signature, but the rules differ:
        # a Coldcard that skipped its low-R grinding is red as "Coldcard Q" and, about half
        # the time, green as "Trezor". If the name could change after a scan, the signer's
        # choice of rule would come back through the user.
        session.signer_locked = True
        while True:
            outcome = check_psbt(hw, session)
            if outcome == "again":
                continue
            if outcome == "done" and not confirm_wipe(hw):
                outcome = "keep"             # v7.5: "Finish" asks first; "Keep" goes back to the seed
            break
        if outcome == "done":
            session.wipe()
            ui.MessageScreen("Seed wiped",
                             "Seed and passphrase have been removed from the app's memory.\n\n"
                             "Switch off with the UPS HAT switch: that clears the RAM completely.",
                             buttons=("Start again",), heading_color=ui.ACCENT, icon_name="lock").run(hw)


def confirm_wipe(hw) -> bool:
    """ v7.5: wiping the seed means typing it all again: ask, the destructive choice on the left """
    choice = ui.MessageScreen("Wipe the seed?", "Seed and passphrase are removed from memory: to check "
                              "anything else you will load them again.", buttons=("Wipe", "Keep"),
                              primary_index=1, values=["wipe", "keep"], heading_color=ui.YELLOW,
                              icon_name="lock").run(hw)
    return choice == "wipe"


def boot_error(hw, text: str):
    """ A fatal problem with the image itself: say so and stop (never run unguarded) """
    logger.error(text)
    if hw is not None:
        try:
            ui.MessageScreen("Image not valid", text, buttons=("Switch the device off",),
                             heading_color=ui.RED, icon_name="warning").run(hw)
        except Exception:
            pass
    while True:
        time.sleep(60)


def main():
    logging.basicConfig(level=logging.INFO)
    # v7.5: before anything else, the microSD read-only (storage.py)
    SplashScreen.sd_status = storage.protect_microsd()
    logger.info(f"microSD: {SplashScreen.sd_status}")
    # v7.5 review: SeedSigner OS may mount the card after this point (mdev coldplug runs
    # after start.sh), or again after a re-insertion: checked again every second
    guard = storage.MicroSDGuard(on_change=lambda status: setattr(SplashScreen, "sd_status", status))
    ui.housekeeping_hooks.append(guard.tick)
    from nonceverify.hw import Hardware, ImportGuard, loaded_blocked_modules

    # Drivers first, then the guard: if a driver pulled in the signer's crypto, the screen
    # says so instead of staying black, and the verifier does not run.
    hw = None
    try:
        hw = Hardware()
    except Exception as e:
        logger.exception("hardware")
        boot_error(None, f"driver: {type(e).__name__}: {e}")
    leaked = loaded_blocked_modules()
    if leaked:
        boot_error(hw, "the drivers load signer code: " + ", ".join(leaked[:4]))
    ImportGuard.install()
    # v7: the battery gauge only after the guard: if its driver ever needed signer code,
    # the import fails and the icon simply stays off
    if hw.battery.start():
        ui.battery_provider = hw.battery.display

    session = Session()
    splash = True
    while True:
        try:
            first, splash = splash, False
            run(hw, session, first)
        except QRDecoderError as e:
            boot_error(hw, str(e))
        except Exception as e:
            # keep the seed: the user decides with "Change seed" or "Done"
            logger.exception("verifier error")
            try:
                ui.MessageScreen("Error", f"{type(e).__name__}: {str(e)[:160]}", heading_color=ui.RED,
                                 icon_name="warning").run(hw)
            except Exception:
                time.sleep(1)
