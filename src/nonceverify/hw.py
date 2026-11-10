"""
Hardware of the SeedSigner Max v5, reused from the image's own drivers (display, touch,
camera stream): code that moves pixels and reads the panel, nothing that touches keys.

`ImportGuard` keeps the signer's cryptography out of this process: embit, urtypes and
the SeedSigner modules that handle seeds, PSBTs or QR payloads cannot be imported while
the verifier runs. If one of them ever got pulled in by a driver, the import fails
instead of silently making the two SD cards share code.
"""
import importlib.abc
import logging
import sys
import time

logger = logging.getLogger(__name__)

WIDTH = 480
HEIGHT = 320

BLOCKED_MODULES = (
    "embit",
    "urtypes",
    "seedsigner.controller",
    "seedsigner.views",
    "seedsigner.models.seed",
    "seedsigner.models.seed_storage",
    "seedsigner.models.psbt_parser",
    "seedsigner.models.decode_qr",
    "seedsigner.models.encode_qr",
    "seedsigner.helpers.embit_utils",
    "seedsigner.helpers.ur2",
)

# v7.5 (independent review): modules that can write to the microSD (Settings.save writes
# settings.json; MicroSD watches the card; camera.py and the GUI pull Settings in). Blocked
# from the moment the guard is installed. Not in the fatal list checked right after the
# drivers: the Max's ILI9486 driver reads the "Display speed" setting through Settings
# inside a try/except, so it may already be loaded; it only reads, and the card is
# remounted read-only before anything runs anyway (storage.py).
PERSISTENCE_MODULES = (
    "seedsigner.models.settings",
    "seedsigner.hardware.microsd",
    "seedsigner.hardware.camera",
    "seedsigner.gui",
)


class ImportGuard(importlib.abc.MetaPathFinder):
    def __init__(self, blocked=BLOCKED_MODULES + PERSISTENCE_MODULES):
        self.blocked = tuple(blocked)
        self.attempts = []

    def find_spec(self, fullname, path=None, target=None):
        for name in self.blocked:
            if fullname == name or fullname.startswith(name + "."):
                if fullname not in self.attempts:        # a driver may retry on every frame
                    self.attempts.append(fullname)
                raise ModuleNotFoundError(f"nonceverify: '{fullname}' is not allowed on the verifier SD",
                                          name=fullname)
        return None

    @classmethod
    def install(cls) -> "ImportGuard":
        for finder in sys.meta_path:
            if isinstance(finder, cls):
                return finder
        guard = cls()
        sys.meta_path.insert(0, guard)
        return guard


def loaded_blocked_modules() -> list:
    return sorted(m for m in sys.modules
                  if any(m == b or m.startswith(b + ".") for b in BLOCKED_MODULES))


class Event:
    """ A tap or a vertical swipe, in screen coordinates """
    TAP = "tap"
    SWIPE_UP = "swipe_up"         # finger moved up: show what is below
    SWIPE_DOWN = "swipe_down"

    def __init__(self, kind: str, x: int = 0, y: int = 0):
        self.kind, self.x, self.y = kind, x, y

    def __repr__(self):
        return f"Event({self.kind}, {self.x}, {self.y})"


class Hardware:
    """ The real device: ILI9486 panel, XPT2046 touch, Pi camera """
    def __init__(self):
        from seedsigner.hardware import hw_profile
        from seedsigner.hardware.displays.ili9486 import ILI9486
        from seedsigner.hardware.touch import TouchTracker
        from seedsigner.hardware.xpt2046 import XPT2046

        self.hw_profile = hw_profile
        self.display = ILI9486(_width=WIDTH, _height=HEIGHT)
        self.touch = XPT2046(width=WIDTH, height=HEIGHT)
        self.tracker = TouchTracker()
        self.stream = None
        self.zoom = 1
        self.battery = BatteryGauge()

    # Display ------------------------------------------------------------------------
    def show(self, image):
        self.display.show_image(image.convert("RGB"))

    # Touch --------------------------------------------------------------------------
    def poll(self) -> list:
        """ Taps and swipes since the last call (non-blocking) """
        # Monotonic, as the signing SD's input loop (0028): a step of the system clock can't
        # stretch or hide a release
        now = int(time.monotonic() * 1000)
        point = self.touch.read()
        events = []
        for e in self.tracker.feed(point, now):
            if getattr(e, "is_tap", False):
                events.append(Event(Event.TAP, e.x, e.y))
            elif getattr(e, "is_swipe", False):
                events.append(Event(Event.SWIPE_UP if e.direction == "up" else Event.SWIPE_DOWN, e.x, e.y))
        return events

    def wait(self, seconds: float = 0.005):
        time.sleep(seconds)

    # Battery (v7) -------------------------------------------------------------------
    def battery_tick(self) -> bool:
        """ Called by the screens' loops: True when the battery icon should be redrawn """
        return self.battery.tick()

    # Camera -------------------------------------------------------------------------
    def camera_start(self):
        from seedsigner.hardware.pivideostream import PiVideoStream
        size = getattr(self.hw_profile, "CAMERA_RESOLUTION", 320)
        if isinstance(size, (tuple, list)):
            size = size[0]
        size = int(size)
        self.stream = PiVideoStream(resolution=(size, size), framerate=12, format="bgr")
        self.stream.start()
        self.camera_set_zoom(self.zoom)

    def camera_set_zoom(self, factor: int):
        """ Centered crop by the camera's own ISP (picamera's `zoom` region): free for the CPU """
        self.zoom = factor
        if self.stream is None:
            return
        side = 1.0 / factor
        offset = (1.0 - side) / 2
        try:
            self.stream.camera.zoom = (offset, offset, side, side)
        except Exception as e:
            logger.info(f"camera zoom not applied: {e}")

    def camera_frame(self):
        """ Latest frame as an RGB PIL image, rotated like SeedSigner does, or None """
        from PIL import Image
        if self.stream is None or self.stream.frame is None:
            return None
        rotation = int(getattr(self.hw_profile, "CAMERA_ROTATION", 0))
        image = Image.fromarray(self.stream.frame.astype("uint8"), "RGB")
        return image.rotate(90 + rotation)

    def camera_stop(self):
        """
        Stop without trusting PiVideoStream.stop(), which busy-waits forever if its
        capture thread has died: ask, wait up to 2 s, then close the camera directly.
        """
        stream, self.stream = self.stream, None
        if stream is None:
            return
        stream.should_stop = True
        deadline = time.monotonic() + 2
        while not getattr(stream, "is_stopped", True) and time.monotonic() < deadline:
            time.sleep(0.02)
        try:
            stream.camera.close()
        except Exception:
            pass


class BatteryGauge:
    """
    v7: battery level from the UPS HAT (C)'s INA219, through the signing SD's own v5.6
    driver (seedsigner.hardware.ina219): bit-banged I2C on header pins 3 and 5, read-only
    (only register pointers are ever sent to the chip). It touches no key.

    Started after the import guard (see app.main): if the driver ever pulled in signer
    code, the import fails, the icon stays off and the verifier runs as before. Any other
    problem (no HAT, GPIO mode, a glitch) also just means no icon. Readings every
    EVERY_S, median of the last three, nothing shown after STALE_S without one; the chip
    not answering GIVE_UP_AFTER times in a row stops the readings.
    """
    EVERY_S = 5
    STALE_S = 60
    GIVE_UP_AFTER = 6
    KEPT = 3

    def __init__(self, gauge=None, clock=time.monotonic):
        self.gauge = gauge
        self.clock = clock
        self.enabled = gauge is not None
        self.recent = []
        self.reading_time = None
        self.last_on_battery = None
        self.failures = 0
        self.next_read = 0
        self.shown = None

    def start(self):
        """ Find the driver and the chip; False (and no icon) if anything is missing """
        try:
            from seedsigner.hardware import hw_profile
            if not getattr(hw_profile, "has_battery_gauge", lambda: False)():
                return False
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BOARD)
            GPIO.setwarnings(False)
            from seedsigner.hardware.ina219 import INA219Gauge
            self.gauge = INA219Gauge()
            self.enabled = True
            return True
        except Exception as e:
            logger.info(f"battery gauge not available: {type(e).__name__}: {e}")
            self.gauge, self.enabled = None, False
            return False

    @staticmethod
    def plausible(reading) -> bool:
        """ A reading the icon can use: numbers in range, the two flags readable """
        try:
            return (isinstance(reading.percent, int) and not isinstance(reading.percent, bool)
                    and 0 <= reading.percent <= 100 and 2.5 <= float(reading.voltage) <= 4.5
                    and isinstance(bool(reading.on_external_power), bool)
                    and isinstance(bool(getattr(reading, "charging", False)), bool))
        except Exception:
            return False

    def add(self, reading, now: float):
        if reading is None:
            return
        if self.recent and self.recent[-1].on_external_power != reading.on_external_power:
            self.recent = []             # cable plugged or pulled: the old readings describe another state
        self.recent = (self.recent + [reading])[-self.KEPT:]
        self.reading_time = now
        current = self.current()
        if current is not None and not current.on_external_power:
            self.last_on_battery = current.percent

    def current(self):
        if not self.recent or self.reading_time is None or self.clock() - self.reading_time > self.STALE_S:
            return None
        ordered = sorted(self.recent, key=lambda r: r.voltage)
        return ordered[len(ordered) // 2]

    def display(self):
        """ (percent shown or None, fill 0-100, on the cable) or None: what the icon shows """
        r = self.current()
        if r is None:
            return None
        if r.on_external_power:
            if getattr(r, "charging", False):
                # the charger lifts the voltage whatever the charge: no number, the last level
                return None, self.last_on_battery if self.last_on_battery is not None else 50, True
            return 100, 100, True
        return r.percent, r.percent, False

    def tick(self) -> bool:
        if not self.enabled:
            return False
        now = self.clock()
        if now >= self.next_read:
            self.next_read = now + self.EVERY_S
            try:
                reading = self.gauge.read()
                if reading is not None and not self.plausible(reading):
                    reading = None
            except Exception as e:
                logger.info(f"battery read failed: {type(e).__name__}")
                reading = None
            if reading is None:
                self.failures += 1
                if self.failures >= self.GIVE_UP_AFTER and self.current() is None:
                    self.enabled = False
            else:
                self.failures = 0
                self.add(reading, now)
        shown = self.display()
        if shown != self.shown:
            self.shown = shown
            return True
        return False


class QRDecoderError(RuntimeError):
    pass


def decode_qr(image):
    """
    Raw payload (bytes) of the first QR code in the image, or None. Binary mode is
    required: in text mode zbar re-encodes bytes >= 0x80, which could turn a Compact
    SeedQR into a different valid seed. SeedSigner OS ships the pyzbar fork that has it.
    """
    from pyzbar import pyzbar
    from pyzbar.pyzbar import ZBarSymbol
    gray = image.convert("L")
    try:
        found = pyzbar.decode(gray, symbols=[ZBarSymbol.QRCODE], binary=True)
    except TypeError:
        raise QRDecoderError("pyzbar without binary mode: unsuitable image")
    for symbol in found:
        return symbol.data
    return None
