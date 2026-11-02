"""
SeedSigner Max (v4): integration checks for the touch screen, driving the real
HardwareButtons and the real Screens with scripted touches replayed in real time on a
480x320 canvas. Also checks the ST7796 and XPT2046 drivers against mocked SPI, the power
pins and the QR display layout, and saves screenshots.

Runs in its own interpreter (see tests/test_seedsigner_max.py) because the regular
test suite replaces modules such as the Renderer, numpy and HardwareButtons with
MagicMocks at import time, and these checks need the real ones.

Usage: python tests/seedsigner_max_harness.py [screenshot_output_dir]
"""
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

# Raspberry Pi-only modules
sys.modules["RPi"] = MagicMock()
sys.modules["RPi.GPIO"] = MagicMock()
sys.modules["spidev"] = MagicMock()
sys.modules["seedsigner.hardware.displays.st7789_mpy"] = MagicMock()
sys.modules["seedsigner.hardware.displays.ili9341"] = MagicMock()
sys.modules["seedsigner.hardware.camera"] = MagicMock()

from PIL import Image, ImageDraw

from seedsigner.gui.renderer import Renderer
from seedsigner.hardware import hw_profile
from seedsigner.hardware.buttons import HardwareButtons, HardwareButtonsConstants
from seedsigner.hardware.touch import TouchCalibration, TouchEvent
from seedsigner.models.seed import Seed
from seedsigner.models.settings_definition import SettingsConstants


SCREENSHOT_DIR = sys.argv[1] if len(sys.argv) > 1 else None
failures = []


def check(condition, message):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {message}")
    if not condition:
        failures.append(message)


def screenshot(name):
    if SCREENSHOT_DIR:
        Renderer.get_instance().canvas.save(os.path.join(SCREENSHOT_DIR, name))



class TestRenderer(Renderer):
    @classmethod
    def configure_instance(cls):
        renderer = cls.__new__(cls)
        renderer.canvas_width = 480
        renderer.canvas_height = 320
        renderer.canvas = Image.new("RGB", (480, 320))
        renderer.draw = ImageDraw.Draw(renderer.canvas)
        renderer.disp = MagicMock()
        Renderer._instance = renderer
        cls._instance = renderer

    def show_image(self, image=None, alpha_overlay=None, show_direct=False, **kwargs):
        if image:
            self.canvas.paste(image)



# ---------------------------------------------------------------------------------------
# Scripted touch panel
# ---------------------------------------------------------------------------------------
class FakeTouch:
    """
    Stands in for the XPT2046: a script of (seconds, point or None) segments replayed
    in real time from the moment it is set. A point is a screen position, or a callable
    of the time elapsed in that segment (for drags). Raises once the script has long
    ended, so a test that waits for a touch that never comes fails instead of hanging.
    """
    def __init__(self):
        self.calibration = TouchCalibration.from_tuple(hw_profile.TOUCH_CALIBRATION)
        self.set_script([])

    def set_script(self, segments, timeout_s=3.0):
        self.segments = segments
        self.timeout_s = sum(d for d, _ in segments) + timeout_s
        self.t0 = time.monotonic()

    def read(self):
        t = time.monotonic() - self.t0
        if t > self.timeout_s:
            raise TimeoutError("scripted touch input exhausted")
        for duration, point in self.segments:
            if t < duration:
                return point(t) if callable(point) else point
            t -= duration
        return None

    def is_touched(self):
        return self.read() is not None



def tap(point, down=0.08, up=0.12):
    return [(down, point), (up, None)]


def drag(start, end, duration=0.5):
    (x0, y0), (x1, y1) = start, end
    return [(duration, lambda t: (int(x0 + (x1 - x0) * t / duration), int(y0 + (y1 - y0) * t / duration))), (0.15, None)]


GUARD = [(0.5, None)]       # let the 400 ms input guard of a new screen pass


def center(rect):
    x1, y1, x2, y2 = rect
    return (int((x1 + x2) / 2), int((y1 + y2) / 2))


def setup_inputs():
    """ Real HardwareButtons, idle joystick, scripted touch panel """
    import seedsigner.controller
    import seedsigner.hardware.buttons as buttons_module

    seedsigner.controller.Controller.get_instance = staticmethod(
        lambda: SimpleNamespace(screensaver_activation_ms=10**9, is_screensaver_start_allowed=False))
    buttons_module.GPIO.LOW = 0
    buttons_module.GPIO.HIGH = 1
    buttons_module.GPIO.input = lambda pin: 1          # joystick and KEY1-3 not connected
    hw = HardwareButtons.get_instance()
    touch = FakeTouch()
    hw.touch_device = touch
    hw.touch_device_failed = False
    hw.touch_tracker.reset()
    return hw, touch



# ---------------------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------------------
def test_st7796_driver():
    import spidev
    from seedsigner.hardware.displays.display_driver import DisplayDriverFactory, DISPLAY_TYPE__ST7796
    from seedsigner.hardware.displays.st7796 import image_to_rgb565_be

    img = Image.new("RGB", (3, 1))
    img.putpixel((0, 0), (255, 0, 0))
    img.putpixel((1, 0), (0, 255, 0))
    img.putpixel((2, 0), (0, 0, 255))
    check(image_to_rgb565_be(img) == bytes([0xF8, 0x00, 0x07, 0xE0, 0x00, 0x1F]), "RGB565 conversion: red/green/blue")

    spi = MagicMock()
    spidev.SpiDev = MagicMock(return_value=spi)
    disp = DisplayDriverFactory.instantiate_display_driver(DISPLAY_TYPE__ST7796, width=480, height=320)
    check(spidev.SpiDev.call_args.args == (0, 0), "display on SPI0 CE0")
    check((disp.width, disp.height) == (480, 320), "ST7796 exposes a 480x320 landscape surface")

    commands = [c.args[0][0] for c in spi.writebytes.call_args_list]
    for cmd, name in [(0x01, "SWRESET"), (0x11, "SLPOUT"), (0x36, "MADCTL"), (0x3A, "COLMOD"), (0x20, "INVOFF (TN panel default)"), (0x29, "DISPON")]:
        check(cmd in commands, f"init sends {name}")
    data = [c.args[0] for c in spi.writebytes2.call_args_list]
    check([0x28] in data, "MADCTL = 0x28 (landscape, BGR)")

    spi.reset_mock()
    disp.show_image(Image.new("RGB", (480, 320), (255, 0, 0)))
    data = [c.args[0] for c in spi.writebytes2.call_args_list]
    check([0x00, 0x00, 0x01, 0xDF] in data and [0x00, 0x00, 0x01, 0x3F] in data, "window 0..479 x 0..319")
    frame = data[-1]
    check(len(frame) == 480 * 320 * 2 and frame[:2] == bytes([0xF8, 0x00]), "full frame of 307200 bytes, correct pixel format")

    spi.reset_mock()
    disp.invert(enabled=True)
    check([c.args[0][0] for c in spi.writebytes.call_args_list] == [0x21], "'Invert colors' flips the TN default (INVOFF -> INVON)")


def test_ili9486_driver():
    """ v5 panel: Waveshare 3.5inch RPi LCD (A), 16-bit words through its SPI converter """
    import spidev
    import RPi.GPIO as GPIO
    from seedsigner.hardware.displays.display_driver import DisplayDriverFactory, DISPLAY_TYPE__ILI9486
    from seedsigner.models.settings_definition import SettingsDefinition

    check(SettingsDefinition.get_settings_entry(SettingsConstants.SETTING__DISPLAY_CONFIGURATION).default_value == "ili9486_480x320",
          "v5 profile boots on the ILI9486 480x320 panel")

    # Record the exact bus traffic: D/C level changes and every SPI transfer, in order
    log = []
    spi = MagicMock()
    spi.xfer2 = MagicMock(side_effect=lambda data, speed_hz=0, *a: log.append(("xfer", list(data), speed_hz)) or [0] * len(data))
    spi.writebytes2 = MagicMock(side_effect=lambda data: log.append(("pixels", bytes(data), None)))
    spi.writebytes = MagicMock(side_effect=lambda data: log.append(("writebytes", list(data), None)))
    spidev.SpiDev = MagicMock(return_value=spi)
    GPIO.setup = MagicMock()
    GPIO.LOW, GPIO.HIGH = 0, 1
    GPIO.output = lambda pin, value: log.append(("pin", pin, value))
    disp = DisplayDriverFactory.instantiate_display_driver(DISPLAY_TYPE__ILI9486, width=480, height=320)
    check(spidev.SpiDev.call_args.args == (0, 0), "ILI9486 on SPI0 CE0")
    check(spi.max_speed_hz == 24_000_000, "ILI9486 pixel clock 24 MHz (measured on the board)")
    check((disp.width, disp.height) == (480, 320), "ILI9486 exposes a 480x320 landscape surface")
    pins = sorted(c.args[0] for c in GPIO.setup.call_args_list)
    check(pins == [18, 22], f"only RS (pin 18) and RST (pin 22) are driven, no backlight pin: {pins}")

    def bus(entries):
        """ (D/C level, transfer) pairs, in order """
        dc, out = None, []
        for kind, data, extra in entries:
            if kind == "pin" and data == hw_profile.ILI9486_PIN_DC:
                dc = extra
            elif kind != "pin":
                out.append((dc, kind, data, extra))
        return out

    traffic = bus(log)
    xfers = [t for t in traffic if t[1] == "xfer"]
    check(not [t for t in traffic if t[1] == "writebytes"], "no unframed writebytes() at all")
    check(all(len(t[2]) % 2 == 0 and t[2][0::2] == [0] * (len(t[2]) // 2) for t in xfers),
          "every command and parameter transfer is whole (0x00, byte) words")
    check(all(t[3] == 10_000_000 for t in xfers), "commands and parameters at 10 MHz (Linux mipi_dbi cap)")
    commands = [t[2][1] for t in xfers if t[0] == 0]
    check(all(len(t[2]) == 2 for t in xfers if t[0] == 0), "D/C low: exactly one 16-bit command word per transfer")
    expected = [0xB0, 0x11, 0x3A, 0xC2, 0xC5, 0xE0, 0xE1, 0xE2, 0x36, 0x20, 0x29]
    check(commands == expected, f"init sequence in the Linux driver's order: {[hex(c) for c in commands]}")
    params = [t[2] for t in xfers if t[0] == 1]
    check([0, 0x55] in params, "16 bits per pixel")
    check([0, 0x28] in params, "MADCTL 0x28: landscape turned 180 degrees (header at the bottom)")
    check([0, 0x00] * 4 in params, "VCOM control: four zero parameters")

    log.clear()
    # A frame whose rows differ, so each band can be matched to the right rows
    from seedsigner.hardware.displays.st7796 import image_to_rgb565_be
    frame_image = Image.new("RGB", (480, 320), (255, 0, 0))
    ImageDraw.Draw(frame_image).rectangle((0, 128, 479, 255), fill=(0, 255, 0))
    ImageDraw.Draw(frame_image).rectangle((0, 256, 479, 319), fill=(255, 159, 10))
    expected_pixels = image_to_rgb565_be(frame_image)
    disp.show_image(frame_image)
    traffic = bus(log)

    def word16(value):
        return [0, value >> 8, 0, value & 0xFF]

    # Each band: CASET 0..479, PASET first..last row, RAMWR (all D/C low commands with
    # D/C high parameters, at 10 MHz), then the band's pixels with D/C high, one transfer
    expected = []
    for first in range(0, 320, 64):
        last = first + 63
        expected += [
            (0, "xfer", [0, 0x2A]), (1, "xfer", word16(0) + word16(479)),
            (0, "xfer", [0, 0x2B]), (1, "xfer", word16(first) + word16(last)),
            (0, "xfer", [0, 0x2C]),
            (1, "pixels", expected_pixels[first * 960:(last + 1) * 960]),
        ]
    check([(t[0], t[1], t[2]) for t in traffic] == expected,
          "frame in 5 row bands of 64 rows, each with its own CASET/PASET/RAMWR, then its pixels with D/C high")
    pixel_transfers = [t[2] for t in traffic if t[1] == "pixels"]
    check([len(p) for p in pixel_transfers] == [61440] * 5 and sum(len(p) for p in pixel_transfers) == 307200,
          "pixel transfers of 5 x 61440 = 307200 bytes")
    check(all(len(t[2]) % 2 == 0 and len(t[2]) < 65536 for t in traffic),
          "every transfer is even and under 64 KiB (the colors broke ~65536 bytes into a 122880-byte band)")
    check(all(t[3] == 10_000_000 for t in traffic if t[1] == "xfer"), "window and RAMWR at the 10 MHz command speed")
    check(pixel_transfers[0][:2] == bytes([0xF8, 0x00]) and pixel_transfers[2][:2] == bytes([0x07, 0xE0])
          and pixel_transfers[4] == expected_pixels[256 * 960:],
          "big-endian RGB565: red bands, then green, then orange")

    log.clear()
    disp.invert(enabled=True)
    check([(t[0], t[2]) for t in bus(log)] == [(0, [0, 0x21])], "'Invert colors' = INVON")


def test_display_choice_locked():
    """ v5: the display type can't be switched to a driver that drives pins 18/22 differently """
    from seedsigner.models.settings_definition import SettingsDefinition
    from seedsigner.models.settings import Settings
    entry = SettingsDefinition.get_settings_entry(SettingsConstants.SETTING__DISPLAY_CONFIGURATION)
    check([v for v, _ in entry.selection_options] == ["ili9486_480x320"], f"only the v5 panel is offered: {entry.selection_options}")

    import seedsigner.gui.renderer as renderer_module
    created = []
    factory = renderer_module.DisplayDriverFactory
    renderer_module.DisplayDriverFactory = SimpleNamespace(
        instantiate_display_driver=lambda display_type, width, height: created.append((display_type, width, height)) or SimpleNamespace(width=width, height=height, invert=lambda **k: None, cleanup=lambda: None))
    settings = Settings.get_instance()
    previous = settings.get_value(SettingsConstants.SETTING__DISPLAY_CONFIGURATION)
    try:
        # As if Persistent Settings or a SettingsQR carried another panel
        settings._data[SettingsConstants.SETTING__DISPLAY_CONFIGURATION] = "st7789_240x240"
        renderer = Renderer.__new__(Renderer)
        renderer.disp = None
        Renderer.initialize_display(renderer)
        check(created == [("ili9486", 480, 320)] and (renderer.canvas_width, renderer.canvas_height) == (480, 320),
              f"a stored st7789 setting is ignored: the v5 panel is used ({created})")
    finally:
        settings._data[SettingsConstants.SETTING__DISPLAY_CONFIGURATION] = previous
        renderer_module.DisplayDriverFactory = factory


def test_boot_calibration_view():
    from seedsigner.views.settings_views import TouchCalibrationView, SettingsMenuView
    from seedsigner.views.view import MainMenuView
    check(hw_profile.TOUCH_CALIBRATE_AT_BOOT is False and hw_profile.TOUCH_RAW_TEST_AT_BOOT is False,
          "calibrated image: no touch test and no calibration at boot")
    hw, _ = setup_inputs()
    saved = hw.touch_device
    hw.touch_device = None
    hw.touch_device_failed = True            # no panel: the view must not get stuck
    try:
        check(TouchCalibrationView(at_boot=True).run().View_cls == MainMenuView, "boot calibration continues to the main menu")
        check(TouchCalibrationView().run().View_cls == SettingsMenuView, "from Settings it goes back to Settings")
    finally:
        hw.touch_device = saved
        hw.touch_device_failed = False


def test_xpt2046_driver():
    import spidev
    import RPi.GPIO as GPIO
    from seedsigner.hardware.xpt2046 import XPT2046, READ_SEQUENCE

    def response_for(values):
        response = [0] * len(READ_SEQUENCE)
        for k, value in enumerate(values):
            word = (value & 0x0FFF) << 3
            response[2 * k + 1] = word >> 8
            response[2 * k + 2] = word & 0xFF
        return response

    spi = MagicMock()
    # Z1, Z2, X0 (settling), X1-X3, Y0 (settling), Y1-Y3
    spi.xfer2 = MagicMock(return_value=response_for([800, 3500, 3999, 1000, 1010, 1005, 5, 2000, 2010, 2005]))
    spidev.SpiDev = MagicMock(return_value=spi)
    GPIO.LOW = 0
    irq = {"level": 1}
    GPIO.input = lambda pin: irq["level"] if pin == hw_profile.TOUCH_PIN_IRQ else 1

    touch = XPT2046(calibration=TouchCalibration(0.1, 0.0, 0.0, 0.0, 0.1, 0.0))
    check(spidev.SpiDev.call_args.args == (0, 1), "touch controller on SPI0 CE1")
    check(spi.max_speed_hz == 500_000, "touch read at 500 kHz (longer acquisition)")
    check(spi.xfer2.call_args.args[0] == [0xD0, 0, 0], "init powers down with PENIRQ armed")

    spi.xfer2.reset_mock()
    check(touch.read() is None and not spi.xfer2.called, "PENIRQ high: no SPI traffic at all")

    irq["level"] = 0
    position = touch.read()
    sent = spi.xfer2.call_args.args[0]
    check(sent == READ_SEQUENCE and len(sent) == 21, "PENIRQ low: one 21-byte read sequence")
    check(sent[0:20:2] == [0xB1, 0xC1, 0x91, 0x91, 0x91, 0x91, 0xD1, 0xD1, 0xD1, 0xD0] and sent[1:21:2] == [0] * 10,
          "Z1, Z2, X x4, Y x4 in a row, each command in an even byte, the last Y powering down with PENIRQ armed")
    # medians of (1000, 1010, 1005) and (2000, 2010, 2005); settling reads ignored; x0.1 each
    check(position == (100, 200), f"raw (1005, 2005) mapped through the calibration: {position}")
    check(touch.read_raw() == (1005, 2005, 1395), "read_raw() still returns (raw_x, raw_y, pressure)")
    check(touch.last_response == response_for([800, 3500, 3999, 1000, 1010, 1005, 5, 2000, 2010, 2005]),
          "the bytes of the last read are kept for the touch test's raw dump")



# ---------------------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------------------
def _menu(n=3, **kwargs):
    from seedsigner.gui.screens.screen import ButtonListScreen, ButtonOption
    return ButtonListScreen(title="Menu", button_data=[ButtonOption(f"Item {i}") for i in range(n)], **kwargs)


def _button_center(screen, index):
    from seedsigner.gui.screens.screen import component_rect
    return center(component_rect(screen.buttons[index]))


def test_menu_tap():
    hw, touch = setup_inputs()
    screen = _menu(show_back_button=False)
    touch.set_script(GUARD + tap(_button_center(screen, 2)))
    check(screen.display() == 2, "tapping the third item selects it")

    screen = _menu(show_back_button=False)
    touch.set_script(GUARD + tap((240, 20)) + tap(_button_center(screen, 1)))
    check(screen.display() == 1, "a tap on the title does nothing; then the second item")


def test_back_and_power():
    from seedsigner.gui.screens.screen import RET_CODE__BACK_BUTTON, RET_CODE__POWER_BUTTON, MainMenuScreen, ButtonOption
    hw, touch = setup_inputs()
    screen = _menu(show_back_button=True)
    touch.set_script(GUARD + tap((30, 30)))
    check(screen.display() == RET_CODE__BACK_BUTTON, "top-left tap = back")

    screen = _menu(show_back_button=True)
    touch.set_script(GUARD + tap((70, 40)))
    check(screen.display() == RET_CODE__BACK_BUTTON, "the back zone is wider than the icon (70, 40)")

    screen = MainMenuScreen(button_data=[ButtonOption(t) for t in ("Scan", "Seeds", "Tools", "Settings")])
    touch.set_script(GUARD + tap((460, 20)))
    check(screen.display() == RET_CODE__POWER_BUTTON, "top-right tap on the home screen = power")

    screen = MainMenuScreen(button_data=[ButtonOption(t) for t in ("Scan", "Seeds", "Tools", "Settings")])
    touch.set_script(GUARD + tap((360, 250)))
    check(screen.display() == 3, "home screen: bottom-right tap opens Settings")
    screenshot("max_main_menu_tapped.png")


def test_input_guard():
    """
    The finger that confirmed the previous screen is still down when this one appears,
    or a fast double tap lands its second press on it: neither may select anything.
    """
    hw, touch = setup_inputs()
    screen = _menu(show_back_button=False)
    touch.set_script([(0.7, _button_center(screen, 2)), (0.1, None)] + tap(_button_center(screen, 1)))
    started = time.monotonic()
    result = screen.display()
    check(result == 1, f"finger held across screens ignored, deliberate tap counts: {result!r}")
    check(time.monotonic() - started > 0.8, "nothing accepted before the held finger was lifted")

    screen = _menu(show_back_button=False)
    touch.set_script([(0.05, None), (0.1, _button_center(screen, 2)), (0.4, None)] + tap(_button_center(screen, 1)))
    result = screen.display()
    check(result == 1, f"second press of a fast double tap ignored: {result!r}")


def test_slide_off_cancels():
    hw, touch = setup_inputs()
    screen = _menu(show_back_button=False)
    x, y = _button_center(screen, 0)
    touch.set_script(GUARD + drag((x, y), (x + 150, y), duration=0.4) + tap(_button_center(screen, 2)))
    check(screen.display() == 2, "press on item 1, slide away, release: nothing; then item 3")


def test_swipe_scrolls_long_list():
    hw, touch = setup_inputs()
    screen = _menu(n=12, show_back_button=False)
    check(screen.has_scroll_arrows, "12 items need scrolling")
    first_slot = _button_center(screen, 0)
    # Finger up by just over 3 rows: the list scrolls 3 rows; the first slot is now item 3
    touch.set_script(GUARD + drag((240, 290), (240, 290 - 3 * 44 - 6), duration=0.6) + tap(first_slot))
    result = screen.display()
    check(result == 3, f"swipe up 3 rows, tap the top slot: item 3 ({result!r})")
    screenshot("max_menu_scrolled.png")

    screen = _menu(n=12, show_back_button=False)
    touch.set_script(GUARD + drag((240, 100), (240, 300), duration=0.4) + tap(first_slot))
    check(screen.display() == 0, "swiping down at the top of the list doesn't move it")



# ---------------------------------------------------------------------------------------
# Hold to confirm
# ---------------------------------------------------------------------------------------
def test_hold_to_confirm():
    from seedsigner.gui.screens.screen import ButtonListScreen, ButtonOption
    from seedsigner.gui.screens.psbt_screens import PSBTFinalizeScreen
    from seedsigner.gui.screens.seed_screens import SeedExportXpubDetailsScreen, SeedSignMessageConfirmAddressScreen
    check(all(cls.requires_hold_to_confirm for cls in (PSBTFinalizeScreen, SeedExportXpubDetailsScreen, SeedSignMessageConfirmAddressScreen)),
          "sign PSBT, export xpub and sign message require a hold")
    check(ButtonListScreen.requires_hold_to_confirm is False, "ordinary menus don't")

    hw, touch = setup_inputs()
    screen = PSBTFinalizeScreen(button_data=[ButtonOption("Approve transaction")])
    progress = []
    original = screen._render_hold_progress
    def spy(value):
        progress.append(value)
        if value is not None and 0.45 < value < 0.55:
            original(value)
            screenshot("max_menu_hold_progress.png")
        else:
            original(value)
    screen._render_hold_progress = spy

    button = _button_center(screen, 0)
    x, y = button
    touch.set_script(GUARD
        + tap(button, down=0.3, up=0.3)                       # a firm tap: not enough
        + drag(button, (x, y - 120), duration=0.6)            # press, then slide off: cancelled
        + [(0.2, None), (1.3, button), (0.2, None)])          # held for the full second
    started = time.monotonic()
    result = screen.display()
    elapsed = time.monotonic() - started
    check(result == 0, f"held finger approves: {result!r}")
    check(progress.count(None) >= 2, "the tap and the slide-off are both reported as cancelled")
    check(progress[-1] == 1.0, "the progress bar fills to 100% before approving")
    check(elapsed > 0.5 + 0.6 + 0.75 + 0.2 + 1.0, f"approval only after the full 1 s hold ({elapsed:.2f} s)")

    screen = PSBTFinalizeScreen(button_data=[ButtonOption("Approve transaction")])
    touch.set_script(GUARD + tap((30, 30)))
    from seedsigner.gui.screens.screen import RET_CODE__BACK_BUTTON
    check(screen.display() == RET_CODE__BACK_BUTTON, "back still works with a plain tap on a hold screen")



# ---------------------------------------------------------------------------------------
# Text entry
# ---------------------------------------------------------------------------------------
def _key_center(keyboard, code):
    for row in keyboard.keys:
        for key in row:
            if key.code == code:
                return center(keyboard.key_rect(key))
    raise KeyError(code)


def test_keyboard_screens():
    from seedsigner.gui.screens.tools_screens import ToolsDiceEntropyEntryScreen
    from seedsigner.gui.screens.seed_screens import SeedBIP85SelectChildIndexScreen

    hw, touch = setup_inputs()
    screen = ToolsDiceEntropyEntryScreen(return_after_n_chars=3)
    kb = screen.keyboard
    codes = [k.code for row in kb.keys for k in row if not k.is_additional_key]
    script = list(GUARD)
    for code in (codes[0], codes[1], codes[5]):
        script += tap(_key_center(kb, code))
    touch.set_script(script)
    result = screen.display()
    check(result == "126", f"dice: tap 1, 2, 6 -> {result!r}")

    screen = SeedBIP85SelectChildIndexScreen()
    kb = screen.keyboard
    from seedsigner.gui.screens.screen import component_rect
    touch.set_script(GUARD + tap(_key_center(kb, "1")) + tap(_key_center(kb, "2")) + tap(_key_center(kb, "7"))
                     + tap(_key_center(kb, kb.KEY_BACKSPACE["code"])) + tap(_key_center(kb, "0"))
                     + tap(center(component_rect(screen.save_button))))
    result = screen.display()
    check(result == "120", f"BIP-85 index: 1, 2, 7, DEL, 0, save -> {result!r}")


def test_passphrase_screen():
    from seedsigner.gui.screens.seed_screens import SeedAddPassphraseScreen
    from seedsigner.gui.screens.screen import component_rect
    hw, touch = setup_inputs()
    screen = SeedAddPassphraseScreen(title="BIP-39 Passphrase")
    abc = screen.keyboard_abc
    digits = screen.keyboard_digits
    script = list(GUARD)
    script += tap(_key_center(abc, "a")) + tap(_key_center(abc, "b"))
    script += tap(center(component_rect(screen.hw_button2)))           # 123
    script += tap(_key_center(digits, "7"))
    script += tap(center(component_rect(screen.hw_button1)))           # back to the letters (lowercase)
    script += tap(center(component_rect(screen.hw_button1)))           # ABC
    script += tap(_key_center(screen.keyboard_ABC, "Z"))
    script += tap(_key_center(screen.keyboard_ABC, Keyboard_KEY_SPACE()))
    script += tap(_key_center(screen.keyboard_ABC, "Q"))
    touch.set_script(script + [(0.3, None)])
    # Stop before saving to take a screenshot of the typed passphrase
    try:
        screen.display()
    except TimeoutError:
        pass
    check(screen.passphrase == "ab7Z Q", f"typed a, b, 123 > 7, abc > ABC > Z, space, Q: {screen.passphrase!r}")
    screenshot("max_passphrase_typed.png")

    screen = SeedAddPassphraseScreen(title="BIP-39 Passphrase")
    touch.set_script(GUARD + tap(_key_center(screen.keyboard_abc, "x")) + tap(center(component_rect(screen.hw_button3))))
    result = screen.display()
    check(result == dict(passphrase="x"), f"check mark saves: {result!r}")


def test_shuffled_passphrase_keys():
    from seedsigner.gui.screens.seed_screens import SeedAddPassphraseScreen
    from seedsigner.gui.screens.screen import component_rect
    from seedsigner.models.settings import Settings
    settings = Settings.get_instance()
    check(settings.get_value(SettingsConstants.SETTING__SCRAMBLED_KEYBOARD) == SettingsConstants.OPTION__DISABLED, "shuffled keys are off by default")
    settings.set_value(SettingsConstants.SETTING__SCRAMBLED_KEYBOARD, SettingsConstants.OPTION__ENABLED)
    try:
        layouts = []
        for _ in range(3):
            screen = SeedAddPassphraseScreen(title="BIP-39 Passphrase")
            lower = screen.keyboard_abc.charset
            upper = screen.keyboard_ABC.charset
            layouts.append(lower)
            check(sorted(lower) == list("abcdefghijklmnopqrstuvwxyz") and upper == lower.upper(),
                  f"every letter once, uppercase in the same places: {lower}")
            check(sorted(screen.keyboard_digits.charset) == list("0123456789"), "every digit once")
        check(len(set(layouts)) == 3 and "abcdefghijklmnopqrstuvwxyz" not in layouts, "a new layout every time the screen opens")

        hw, touch = setup_inputs()
        screen = SeedAddPassphraseScreen(title="BIP-39 Passphrase")
        abc = screen.keyboard_abc
        touch.set_script(GUARD + tap(_key_center(abc, "o")) + tap(_key_center(abc, "k")) + tap(center(component_rect(screen.hw_button3))))
        with Renderer.get_instance().lock:
            screen._render()
        screenshot("max_passphrase_shuffled.png")
        result = screen.display()
        check(result == dict(passphrase="ok"), f"typing on the shuffled layout: {result!r}")
    finally:
        settings.set_value(SettingsConstants.SETTING__SCRAMBLED_KEYBOARD, SettingsConstants.OPTION__DISABLED)


def Keyboard_KEY_SPACE():
    from seedsigner.gui.keyboard import Keyboard
    return Keyboard.KEY_SPACE["code"]


def test_mnemonic_entry_screen():
    from seedsigner.gui.screens.seed_screens import SeedMnemonicEntryScreen
    from seedsigner.gui.screens.screen import component_rect, RET_CODE__BACK_BUTTON
    wordlist = Seed.get_wordlist(SettingsConstants.WORDLIST_LANGUAGE__ENGLISH)
    hw, touch = setup_inputs()

    screen = SeedMnemonicEntryScreen(title="Seed Word #1", initial_letters=["a"], wordlist=wordlist)
    kb = screen.keyboard
    highlight = center(component_rect(screen.matches_list_highlight_button))
    touch.set_script(GUARD + tap(_key_center(kb, "f")) + tap(_key_center(kb, "o")) + tap(_key_center(kb, "z"))
                     + tap(_key_center(kb, "r")) + tap(highlight))
    result = screen.display()
    check(result == "force", f"f, o, (z inactive), r, tap the highlighted word: {result!r}")

    screen = SeedMnemonicEntryScreen(title="Seed Word #2", initial_letters=["a"], wordlist=wordlist)
    kb = screen.keyboard
    below = (highlight[0], highlight[1] + 50)
    touch.set_script(GUARD + tap(_key_center(kb, "f")) + tap(_key_center(kb, "o")) + tap(_key_center(kb, "r"))
                     + tap(below) + tap(below) + tap(highlight))
    result = screen.display()
    check(result == "forget", f"for + two taps below the highlight (force > forest > forget): {result!r}")

    screen = SeedMnemonicEntryScreen(title="Seed Word #3", initial_letters=["a"], wordlist=wordlist)
    kb = screen.keyboard
    touch.set_script(GUARD + tap(_key_center(kb, "m")) + tap(_key_center(kb, "u")) + tap(_key_center(kb, "x"))
                     + tap(_key_center(kb, kb.KEY_BACKSPACE["code"])) + tap(_key_center(kb, "a")) + [(0.3, None)])
    try:
        screen.display()
    except TimeoutError:
        pass
    check("".join(screen.letters).strip() == "ma", f"m, u, (x inactive), DEL, a: {''.join(screen.letters)!r}")
    screenshot("max_mnemonic_typed.png")

    screen = SeedMnemonicEntryScreen(title="Seed Word #4", initial_letters=["a"], wordlist=wordlist)
    touch.set_script(GUARD + tap((30, 30)))
    check(screen.display() == RET_CODE__BACK_BUTTON, "back from the seed word screen")



# ---------------------------------------------------------------------------------------
# Camera loops, QR display
# ---------------------------------------------------------------------------------------
def test_camera_loops_get_touch_input():
    from seedsigner.gui.screens.tools_screens import ToolsImageEntropyLivePreviewScreen, ToolsImageEntropyFinalImageScreen
    from seedsigner.gui.screens.scan_screens import ScanScreen
    canvas = SimpleNamespace(canvas_width=480, canvas_height=320)
    def mapped(cls, x, y):
        return cls.handle_touch(canvas, TouchEvent(TouchEvent.TYPE__UP, x, y, tap=True))
    K = HardwareButtonsConstants
    check(mapped(ScanScreen, 100, 300) == K.KEY_LEFT and mapped(ScanScreen, 240, 100) is None,
          "scan: tapping the '< back' strip leaves, the rest ignores touches")
    check(mapped(ToolsImageEntropyLivePreviewScreen, 60, 300) == K.KEY_LEFT and mapped(ToolsImageEntropyLivePreviewScreen, 300, 150) == K.KEY_PRESS,
          "entropy preview: bottom-left = back, elsewhere = take the picture")
    check(mapped(ToolsImageEntropyFinalImageScreen, 50, 160) == K.KEY_LEFT and mapped(ToolsImageEntropyFinalImageScreen, 430, 160) == K.KEY_RIGHT,
          "entropy final image: left half reshoot, right half accept")

    hw, touch = setup_inputs()
    hw.begin_screen(touch_handler=lambda event: ScanScreen.handle_touch(canvas, event))
    touch.set_script(GUARD + tap((100, 300)))
    deadline = time.monotonic() + 1.5
    right = left = False
    while time.monotonic() < deadline and not left:
        # Same calls as the scan loop: KEY_RIGHT first, then KEY_LEFT
        right = right or hw.check_for_low(HardwareButtonsConstants.KEY_RIGHT)
        left = hw.check_for_low(HardwareButtonsConstants.KEY_LEFT)
        time.sleep(0.01)
    check(left and not right, "the tap reaches the second check_for_low() of a camera loop")


def test_qr_display_centered():
    from seedsigner.gui.screens.screen import QRDisplayScreen
    from seedsigner.models.settings import Settings
    hw, touch = setup_inputs()
    renderer = Renderer.get_instance()
    renderer.draw.rectangle((0, 0, 480, 320), fill="red")          # leftovers of the previous screen
    Settings.get_instance().set_value(SettingsConstants.SETTING__QR_BRIGHTNESS_TIPS, SettingsConstants.OPTION__DISABLED)
    encoder = MagicMock()
    sizes = []
    def part(width, height, border=2, background_color=None):
        sizes.append((width, height))
        return Image.new("RGB", (width, height), "white")
    encoder.next_part_image = part
    screen = QRDisplayScreen(qr_encoder=encoder)
    thread = screen.threads[-1]
    thread.keep_running = True
    import threading
    t = threading.Thread(target=thread.run, daemon=True)
    t.start()
    time.sleep(0.4)
    thread.keep_running = False
    t.join(1)
    canvas = renderer.canvas
    check(sizes and sizes[0] == (320, 320), f"QR rendered at the largest square that fits: {sizes[:1]}")
    check(canvas.getpixel((240, 160)) == (255, 255, 255) and canvas.getpixel((80, 5)) == (255, 255, 255),
          "QR centered: x 80..399")
    check(canvas.getpixel((40, 40)) == (0, 0, 0) and canvas.getpixel((40, 300)) == (0, 0, 0) and canvas.getpixel((404, 160)) == (0, 0, 0),
          "the side bars are black around the controls: nothing of the previous screen shows through")
    screenshot("max_qr_display.png")

    K = HardwareButtonsConstants
    check(screen.handle_touch(TouchEvent(TouchEvent.TYPE__SWIPE, 240, 100, direction=TouchEvent.SWIPE__UP)) == K.KEY_UP,
          "QR: drag up = brighter")
    check(screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, 240, 100, tap=True)) is None,
          "QR: a tap on the QR code itself no longer closes the screen")


def test_scan_zoom_button():
    """
    0020: zoom button at the top right of the scan preview. Starts at the setting's value,
    cycles 1x -> 2x -> 3x -> 1x on the camera at once, remembered for the next scan;
    "< back" and the rest of the preview behave as before.
    """
    import threading
    import seedsigner.hardware.camera as camera_module          # MagicMock in this harness
    from seedsigner.gui.components import GUIConstants
    from seedsigner.gui.screens.scan_screens import ScanScreen
    from seedsigner.models.settings import Settings
    K = HardwareButtonsConstants

    class FakeCamera:
        def __init__(self, frame):
            self.zoom, self.applied, self.frame = 1, [], frame
            self._video_stream = object()
        def start_video_stream_mode(self, **kwargs):
            self.started = kwargs
        def set_zoom(self, factor):
            self.zoom = factor
            self.applied.append(factor)
        def read_video_stream(self, as_image=False, rgb=False):
            time.sleep(0.05)
            return self.frame.copy()

    import qrcode
    qr = qrcode.make("bitcoin:bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq", border=2).convert("RGB")
    camera_frame = Image.new("RGB", (320, 320), (70, 80, 90))
    camera_frame.paste(qr.resize((120, 120)), (100, 100))
    fake = FakeCamera(camera_frame)
    saved_get_instance = camera_module.Camera.get_instance
    camera_module.Camera.get_instance = lambda: fake
    settings = Settings.get_instance()
    try:
        settings.set_value(SettingsConstants.SETTING__MAX_CAMERA_ZOOM, 2)
        decoder = MagicMock()
        decoder.get_percent_complete = MagicMock(return_value=0)
        screen = ScanScreen(decoder=decoder, instructions_text="Scan a QR code")
        check(fake.applied == [2], f"the scan starts at the Camera zoom setting: {fake.applied}")
        rect = screen.zoom_button_rect
        check(rect[2] <= 480 and rect[1] >= 0 and rect[3] < 320 * 0.6 and rect[0] > 300,
              f"zoom button at the top right, clear of the '< back' strip: {rect}")

        tap_zoom = TouchEvent(TouchEvent.TYPE__UP, *center(rect), tap=True)
        results = [screen.handle_touch(tap_zoom) for _ in range(3)]
        check(results == [None] * 3 and fake.applied == [2, 3, 1, 2], f"tap: 2x -> 3x -> 1x -> 2x, scan goes on: {fake.applied}")
        check(settings.get_value(SettingsConstants.SETTING__MAX_CAMERA_ZOOM) == 2, "the zoom is kept for the next scan")
        check(screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, 100, 300, tap=True)) == K.KEY_LEFT, "'< back' still leaves")
        check(screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, 240, 150, tap=True)) is None and fake.applied[-1] == 2,
              "the rest of the preview still ignores touches")
        check(screen.handle_touch(TouchEvent(TouchEvent.TYPE__SWIPE, *center(rect), direction=TouchEvent.SWIPE__UP)) is None
              and len(fake.applied) == 4, "only a tap on the button changes the zoom")

        # The button is drawn on the live preview
        thread = screen.threads[0]
        thread.keep_running = True
        t = threading.Thread(target=thread.run, daemon=True)
        t.start()
        time.sleep(0.4)
        thread.keep_running = False
        t.join(1)
        canvas = Renderer.get_instance().canvas
        accent = tuple(int(GUIConstants.ACCENT_COLOR[i:i + 2], 16) for i in (1, 3, 5))
        check(canvas.getpixel((rect[0] + 6, rect[3] - 6)) == accent, "zoomed in: the button is orange")
        screenshot("max_scan_zoom_2x.png")
        fake.zoom = 1
        ScanScreen.LivePreviewThread.render_zoom_button(canvas, rect, 1)
        check(canvas.getpixel((rect[0] + 6, rect[3] - 6)) == (0, 0, 0), "at 1x: black button")
    finally:
        camera_module.Camera.get_instance = saved_get_instance
        settings.set_value(SettingsConstants.SETTING__MAX_CAMERA_ZOOM, 1)


def test_qr_display_touch_controls():
    """
    0019: brightness on the signed-PSBT (and every other) QR screen. "+"/"-" in the right
    bar, "Done" in the left bar; taps on the QR code are ignored and light up "Done".
    """
    import threading
    from seedsigner.gui.components import GUIConstants
    from seedsigner.gui.screens.screen import QRDisplayScreen
    from seedsigner.models.settings import Settings
    K = HardwareButtonsConstants
    settings = Settings.get_instance()
    settings.set_value(SettingsConstants.SETTING__QR_BRIGHTNESS_TIPS, SettingsConstants.OPTION__ENABLED)
    settings.set_value(SettingsConstants.SETTING__QR_BRIGHTNESS, 124)
    renderer = Renderer.get_instance()

    encoder = MagicMock()
    parts = []
    def part(width, height, border=2, background_color=None):
        parts.append(background_color)
        return Image.new("RGB", (width, height), "#" + background_color)
    encoder.next_part_image = part
    encoder.part_to_image = lambda p, w, h, border=2, background_color=None: Image.new("RGB", (w, h), "red")

    screen = QRDisplayScreen(qr_encoder=encoder)
    check(screen.touch_controls, "480x320: controls in the 80 px side bars")
    rects = screen.control_rects()
    check(rects["done"][0] >= 0 and rects["done"][2] <= 80 and rects["brighter"][0] >= 400 and rects["darker"][3] <= 320,
          f"controls stay inside the side bars, off the QR code (x 80..399): {rects}")
    for name, rect in rects.items():
        if name != "meter":
            check(rect[2] - rect[0] >= 60 and rect[3] - rect[1] >= 80, f"{name}: at least 60 x 80 px (~9 x 12 mm): {rect}")

    taps = {
        "qr": screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, 240, 160, tap=True)),
        "qr corner": screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, 90, 310, tap=True)),
        "left bar, off Done": screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, 40, 20, tap=True)),
        "brighter": screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, *center(rects["brighter"]), tap=True)),
        "darker": screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, *center(rects["darker"]), tap=True)),
        "right bar top edge": screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, 470, 2, tap=True)),
        "done": screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, *center(rects["done"]), tap=True)),
        "press, not yet a tap": screen.handle_touch(TouchEvent(TouchEvent.TYPE__DOWN, *center(rects["done"]))),
    }
    check(taps == {"qr": None, "qr corner": None, "left bar, off Done": None, "brighter": K.KEY_UP, "darker": K.KEY_DOWN,
                   "right bar top edge": K.KEY_UP, "done": K.KEY_PRESS, "press, not yet a tap": None},
          f"tap zones: {taps}")
    check(screen.done_hint_until.cur_count > time.time_ns(), "a tap on the QR code lights up Done")

    # Rendering: the joystick tip is off (it would cover the QR code), the controls are drawn
    thread = screen.threads[-1]
    thread.keep_running = True
    t = threading.Thread(target=thread.run, daemon=True)
    t.start()
    time.sleep(0.4)
    thread.keep_running = False
    t.join(1)
    canvas = renderer.canvas
    accent = tuple(int(GUIConstants.ACCENT_COLOR[i:i + 2], 16) for i in (1, 3, 5))
    check(canvas.getpixel((240, 160)) != (255, 0, 0), "no brightness tip over the QR code on the Max")
    check(canvas.getpixel((rects["done"][0] + 3, center(rects["done"])[1])) == accent, "Done highlighted after the tap on the QR")
    meter = rects["meter"]
    lit = [canvas.getpixel((center(meter)[0], int(meter[3] - (i + 0.5) * (meter[3] - meter[1]) / 8))) for i in range(8)]
    white = tuple(int(GUIConstants.BUTTON_FONT_COLOR[i:i + 2], 16) for i in (1, 3, 5))
    check([c == white for c in lit] == [True] * 4 + [False] * 4, "meter: 4 of 8 segments at brightness 124")
    check(canvas.getpixel(center(rects["brighter"])) == white and canvas.getpixel(center(rects["darker"])) == white,
          "+ and - drawn")
    screenshot("max_qr_display_controls.png")

    # The real screen loop, driven by touches: nothing closes it but Done
    hw, touch = setup_inputs()
    settings.set_value(SettingsConstants.SETTING__QR_BRIGHTNESS, 124)
    parts.clear()
    screen = QRDisplayScreen(qr_encoder=encoder)
    rects = screen.control_rects()
    touch.set_script(GUARD
        + tap((240, 160)) + tap((150, 60))                                      # fingers on the QR code
        + tap(center(rects["brighter"])) + tap(center(rects["brighter"]))       # 124 -> 155 -> 186
        + tap(center(rects["darker"]))                                          # -> 155
        + drag((240, 250), (240, 120), duration=0.5)                            # finger up 130 px: 2 steps -> 217
        + [(0.4, None)]
        + tap(center(rects["done"])))
    started = time.monotonic()
    screen.display()
    elapsed = time.monotonic() - started
    check(elapsed > 2.0, f"taps on the QR code and on +/- don't close it; Done does ({elapsed:.1f} s)")
    check(screen.qr_brightness.cur_count == 217, f"+, +, -, drag up 2 rows: 124 -> 217 ({screen.qr_brightness.cur_count})")
    check(settings.get_value(SettingsConstants.SETTING__QR_BRIGHTNESS) == 217, "the chosen brightness is kept for the next QR")
    check("d9d9d9" in parts, f"the QR background follows the brightness (0xd9 = 217): {sorted(set(parts))}")
    settings.set_value(SettingsConstants.SETTING__QR_BRIGHTNESS_TIPS, SettingsConstants.OPTION__DISABLED)



# ---------------------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------------------
class FakeRawTouch:
    """ Raw ADC stand-in for the calibration screen: presses are given in screen pixels """
    def __init__(self, truth: TouchCalibration):
        self.truth = truth
        self.calibration = truth
        self.touch = FakeTouch()

    def set_script(self, segments):
        self.touch.set_script(segments)

    def read_raw(self):
        point = self.touch.read()
        if point is None:
            return None
        # invert the true transform: screen (x, y) -> raw (rx, ry)
        a, b, c, d, e, f = self.truth.as_tuple()
        det = a * e - b * d
        x, y = point[0] - c, point[1] - f
        return ((e * x - b * y) / det, (-d * x + a * y) / det, 1000)

    def read(self):
        return self.touch.read()

    def is_touched(self):
        return self.touch.read() is not None


def test_touch_calibration():
    from seedsigner.gui.screens.settings_screens import TouchCalibrationScreen, TouchCalibrationResultScreen
    hw, _ = setup_inputs()
    truth = TouchCalibration(0.0, 0.1297, -25.9, -0.0865, 0.0, 345.9)       # swapped + mirrored
    device = FakeRawTouch(truth)
    hw.touch_device = device

    screen = TouchCalibrationScreen()
    script = [(0.3, None)]
    for i, target in enumerate(screen.targets):
        # Pressing a few pixels off the cross, as a real finger would
        script += [(0.25, (target[0] + (2 if i % 2 else -2), target[1] + 1)), (0.25, None)]
    device.set_script(script)
    with Renderer.get_instance().lock:
        screen._render()
    screenshot("max_touch_calibration.png")
    result = screen._run()
    check(result is not None, "four presses give a calibration")
    calibration, max_error = result
    check(max_error < 4, f"fit error within a few pixels: {max_error:.2f}")
    worst = 0
    for raw in [(500, 500), (2000, 2000), (3500, 800), (900, 3400)]:
        got = calibration.to_screen(*raw, 480, 320)
        want = truth.to_screen(*raw, 480, 320)
        worst = max(worst, abs(got[0] - want[0]), abs(got[1] - want[1]))
    check(worst <= 3, f"the fitted transform matches the panel's within {worst} px")

    # Result screen: "Keep" must be tapped through the new calibration; otherwise timeout
    class QuickResult(TouchCalibrationResultScreen):
        CONFIRM_TIMEOUT_S = 1.0
    hw.touch_device = FakeTouch()
    result_screen = QuickResult(calibration_values=calibration.as_tuple(), max_error_px=max_error)
    hw.touch_device.set_script([(3.0, None)])
    started = time.monotonic()
    check(result_screen.display() == "timeout", "nothing tapped: the result screen times out (calibration discarded)")
    check(time.monotonic() - started < 2.0, "timeout after ~1 s")

    result_screen = TouchCalibrationResultScreen(calibration_values=calibration.as_tuple(), max_error_px=max_error)
    hw.touch_device.set_script(GUARD + tap(_button_center(result_screen, 0)))
    check(result_screen.display() == 0, "'Keep' tapped with the new calibration")
    screenshot("max_touch_calibration_result.png")



class SequenceRawTouch:
    """ Raw readings from a fixed list (None = no contact), one per read_raw() call """
    def __init__(self, readings):
        self.readings = list(readings)

    def read_raw(self):
        return self.readings.pop(0) if self.readings else None


def test_press_reading_drops_landing_noise():
    from seedsigner.gui.screens.settings_screens import TouchCalibrationScreen, read_raw_press
    hw, _ = setup_inputs()
    wild = [(3900, 100, 450), (200, 3800, 500), (3000, 3000, 600)]
    steady = [(1000 + i % 3, 2000 - i % 2, 1200) for i in range(8)]
    screen = TouchCalibrationScreen()
    point = screen._read_press(SequenceRawTouch(wild + steady + [None]), time.time() + 2)
    check(point == (1001, 2000), f"calibration: the first 3 readings of a press are dropped: {point}")
    # MIN_SAMPLES counts the readings kept after the drop: 3 + 7 is still a bounce
    check(read_raw_press(SequenceRawTouch(wild + steady[:7] + [None]), time.time() + 0.3, 8, 25) is None,
          "3 dropped + 7 readings: too short, not a press")


def test_touch_raw_test_screen():
    """ The diagnostic touch test: five presses, raw statistics, fit, color bars """
    from seedsigner.gui.components import GUIConstants
    from seedsigner.gui.screens.settings_screens import TouchRawTestScreen
    hw, _ = setup_inputs()
    truth = TouchCalibration(0.0, 0.1297, -25.9, -0.0865, 0.0, 345.9)       # swapped + mirrored
    device = FakeRawTouch(truth)
    hw.touch_device = device
    renderer = Renderer.get_instance()

    screen = TouchRawTestScreen()
    check(screen.TARGETS == [(40, 40), (440, 40), (440, 280), (40, 280), (240, 160)], "targets 1-5: corners, then center")

    def landing(target):
        # ~30 ms of a wild reading as the stylus lands, then steady on the target
        return lambda t: (479 - target[0], 319 - target[1]) if t < 0.03 else target

    script = [(0.3, None)]
    for target in screen.TARGETS:
        script += [(0.35, landing(target)), (0.25, None)]
    script += [(0.3, (100, 100)), (1.0, None)]            # "Touch anywhere to continue"
    device.set_script(script)
    device.last_response = [0x00, 0x7F, 0xF8] + [0x40, 0x00] * 9     # the raw dump's source
    hw.touch_require_release = False

    shown = []
    original_show = renderer.show_image
    def spy_show(*args, **kwargs):
        shown.append(len(screen.results))
        return original_show(*args, **kwargs)
    renderer.show_image = spy_show
    try:
        started = time.monotonic()
        results = screen.display()
        elapsed = time.monotonic() - started
    finally:
        renderer.show_image = original_show
    screenshot("max_touch_test.png")

    check(len(results) == 5, f"five presses logged: {len(results)}")
    check(shown[0] == 0 and [n for n in shown[1:]] == [1, 2, 3, 4, 5], f"whole screen redrawn after each press: {shown}")
    worst = 0
    for stats, target in zip(results, screen.TARGETS):
        x, y = truth.to_screen(stats.x, stats.y, 480, 320)
        worst = max(worst, abs(x - target[0]), abs(y - target[1]))
    check(worst <= 1, f"each logged median maps back onto its target (within {worst} px)")
    check(all(r.samples >= 8 and r.pressure == 1000 for r in results), "sample counts and pressure logged")
    check(all(r.samples <= TouchRawTestScreen.MAX_SAMPLES - 3 for r in results), "up to 50 readings per press, the first 3 dropped")
    calibration, max_error = screen.fit
    check(calibration is not None and max_error < 2, f"5-point fit: error {max_error:.2f} px")
    check(screen.fit_lines()[0] == f"fit err {max_error:.0f} px" and len(screen.fit_lines()) == 3, f"fit shown: {screen.fit_lines()}")
    check(hw.touch_require_release, "the press that ends the test can't leak into the next screen")
    check(elapsed < 6.0, f"continues right after the press that follows the 5th ({elapsed:.1f} s)")

    # The final screen: log lines, fit, color bars
    canvas = renderer.canvas
    lines = [r.log_line(i + 1) for i, r in enumerate(results)] + screen.fit_lines()
    widest = max(screen.fixed_font.getlength(line) for line in lines)
    check(all(len(line) <= 44 for line in lines) and screen.TEXT_LEFT + widest < 420,
          f"text fits between the corner targets (widest {widest:.0f} px)")
    check(lines[0].startswith("1  x=") and "  y=" in lines[0] and "  z=1000  k=" in lines[0], f"log line format: {lines[0]!r}")
    raw = screen.raw_line()
    check(raw == "r 7FF8" + "4000" * 9, f"raw dump: bytes 1-20 of the last read, as ten 16-bit words: {raw!r}")
    raw_width = screen.fixed_font.getlength(raw)
    raw_box = canvas.crop((0, screen.RAW_Y, 480, 320))
    check(screen.TEXT_LEFT + raw_width < 420 and screen.RAW_Y >= 280 + 13 and raw_box.getbbox() is not None,
          f"raw dump drawn under the bottom targets, between them ({screen.TEXT_LEFT}..{screen.TEXT_LEFT + raw_width:.0f} px)")
    rects = screen.color_bar_rects()
    colors = [canvas.getpixel((rect[0] + 4, rect[3] - 3)) for rect in rects]
    accent = tuple(int(GUIConstants.ACCENT_COLOR[i:i + 2], 16) for i in (1, 3, 5))
    check(colors == [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), accent, (0, 0, 0)],
          f"color strip: red, green, blue, white, UI orange, black: {colors}")
    check(canvas.getpixel((rects[5][0], rects[5][1] + 10)) == (255, 255, 255), "the black box has a white border")
    check(rects[0][1] > 40 + 12 and rects[0][3] < screen.FIT_Y and rects[0][0] >= 0 and rects[-1][2] < 480,
          "color strip below the title and the top targets, above the log")

    # Nobody touches: the test gives up and the boot goes on
    class QuickTest(TouchRawTestScreen):
        TIMEOUT_S = 0.5
    device.set_script([(5.0, None)])
    started = time.monotonic()
    results = QuickTest().display()
    check(results == [] and time.monotonic() - started < 2.0, "no touch for TIMEOUT_S: continues without results")



# ---------------------------------------------------------------------------------------
# Power pins
# ---------------------------------------------------------------------------------------
def test_power_pins():
    import RPi.GPIO as GPIO
    from seedsigner.hardware import power
    GPIO.LOW = 0
    GPIO.HIGH = 1
    GPIO.OUT = "out"
    levels = {hw_profile.PIN_BATT_LOW: 1, hw_profile.PIN_CHG_PG: 1}
    GPIO.input = lambda pin: levels.get(pin, 1)

    # v5 (no carrier, Waveshare UPS HAT (C)): the power pins are never touched
    check(hw_profile.HAS_CARRIER is False and not hw_profile.has_carrier(), "v5 profile: no carrier PCB")
    GPIO.setup = MagicMock()
    GPIO.output = MagicMock()
    levels[hw_profile.PIN_BATT_LOW] = 0
    levels[hw_profile.PIN_CHG_PG] = 0
    sleep = power.time.sleep
    power.time.sleep = lambda s: None
    power.power_off()
    check(not power.is_battery_low() and not power.is_usb_power_connected()
          and not GPIO.setup.called and not GPIO.output.called,
          "without the carrier, battery and power-off pins are left alone (floating inputs can't raise alarms)")
    levels[hw_profile.PIN_BATT_LOW] = 1
    levels[hw_profile.PIN_CHG_PG] = 1
    power.time.sleep = sleep

    # v4 carrier
    hw_profile.HAS_CARRIER = True
    try:
        _test_carrier_power_pins(GPIO, power, levels)
    finally:
        hw_profile.HAS_CARRIER = False


def _test_carrier_power_pins(GPIO, power, levels):
    check(not power.is_battery_low() and not power.is_usb_power_connected(), "both inputs idle high")
    levels[hw_profile.PIN_BATT_LOW] = 0
    levels[hw_profile.PIN_CHG_PG] = 0
    check(power.is_battery_low() and power.is_usb_power_connected(), "LOW = battery low / cable connected")

    GPIO.output = MagicMock()
    GPIO.setup = MagicMock()
    power.time.sleep = lambda s: None
    power.power_off()
    check(GPIO.setup.call_args.args[:2] == (hw_profile.PIN_PWR_OFF, "out"), "PWR_OFF pin set as output")
    check(GPIO.output.call_args.args == (hw_profile.PIN_PWR_OFF, 1), "power off drives the Pololu OFF input high")



def test_io_test_screen_touch():
    from seedsigner.gui.screens.settings_screens import IOTestScreen
    from seedsigner.gui.screens.screen import component_rect
    hw, touch = setup_inputs()
    screen = IOTestScreen()
    with Renderer.get_instance().lock:
        screen._render()
    K = HardwareButtonsConstants
    check(screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, *center(component_rect(screen.key3_button)), tap=True)) == K.KEY3, "I/O test: Exit button")
    check(screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, *center(component_rect(screen.joystick_up_button)), tap=True)) == K.KEY_UP, "I/O test: d-pad up")
    check(screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, 250, 200, tap=True)) is None, "I/O test: other taps only draw a cross")
    screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, 260, 120, tap=True))
    check(Renderer.get_instance().canvas.getpixel((250, 200)) != (0, 0, 0), "the cross is drawn where the touch was read")
    screenshot("max_io_test_crosses.png")



def test_battery_icon():
    """ v5: battery icon in the top nav from the INA219 reading (UPS HAT (C)) """
    import time as _time
    from seedsigner.gui.components import GUIConstants, TopNav
    from seedsigner.gui.screens.screen import ButtonOption, MainMenuScreen
    from seedsigner.hardware.ina219 import BatteryReading
    from seedsigner.hardware.power import BatteryMonitor
    renderer = Renderer.get_instance()
    monitor = BatteryMonitor.get_instance()
    saved = (monitor.reading, monitor.reading_time, list(monitor.recent))

    def icon_pixels(box):
        return renderer.canvas.crop(box).getcolors(10000) or []

    def colors_in(box):
        return {c for _, c in icon_pixels(box)}

    try:
        green = tuple(int(GUIConstants.SUCCESS_COLOR[i:i + 2], 16) for i in (1, 3, 5))
        red = tuple(int(GUIConstants.ERROR_COLOR[i:i + 2], 16) for i in (1, 3, 5))

        monitor.reading, monitor.reading_time = None, 0
        screen = _menu(show_back_button=True)
        with renderer.lock:
            screen._render()
        check(green not in colors_in((420, 0, 480, 48)) and red not in colors_in((420, 0, 480, 48)),
              "no reading: no battery icon (screens unchanged)")

        monitor.reading, monitor.reading_time = BatteryReading(3.95, -420, 80), _time.time()
        screen = _menu(show_back_button=True)
        with renderer.lock:
            screen._render()
        check(green in colors_in((440, 10, 475, 38)), "80%: green battery icon at the top right")
        screenshot("max_battery_icon_menu.png")

        home = MainMenuScreen(button_data=[ButtonOption(t) for t in ("Scan", "Seeds", "Tools", "Settings")])
        with renderer.lock:
            home._render()
        power_x = home.top_nav.right_button.screen_x
        check(green in colors_in((power_x - 40, 10, power_x - 2, 38)) and green not in colors_in((power_x, 0, 480, 48)),
              "home screen: the icon sits left of the power button")
        screenshot("max_battery_icon_home.png")

        monitor.reading = BatteryReading(3.45, -420, 4)
        screen = _menu(show_back_button=True)
        with renderer.lock:
            screen._render()
        check(red in colors_in((440, 10, 475, 38)), "4%: red battery icon")

        monitor.reading = BatteryReading(4.12, 350, 96)
        screen = _menu(show_back_button=True)
        with renderer.lock:
            screen._render()
        white = tuple(int(GUIConstants.BODY_FONT_COLOR[i:i + 2], 16) for i in (1, 3, 5))
        check(white in colors_in((448, 12, 468, 36)), "on the USB cable: lightning bolt on the icon")
        screenshot("max_battery_icon_charging.png")

        nav = TopNav(text="A very long screen title that fills the bar", show_back_button=True)
        check(nav.battery_layout("80%") == (None, 443), f"a long title: icon only, percentage dropped: {nav.battery_layout('80%')}")
        nav = TopNav(text="An even longer screen title that fills the bar", show_back_button=True)
        check(nav.battery_layout("80%") is None or nav.title.needs_scroll, "a title reaching the corner: no icon at all")
        nav = TopNav(text="Settings", show_back_button=True)
        layout = nav.battery_layout("80%")
        check(layout is not None and layout[0] is not None, f"a short title leaves room for icon and percentage: {layout}")
    finally:
        monitor.reading, monitor.reading_time, monitor.recent = saved


def test_battery_screen():
    """ Settings > Advanced > Hardware > Battery: live INA219 readings """
    import time as _time
    from types import SimpleNamespace as NS
    from seedsigner.gui.screens.settings_screens import BatteryInfoScreen
    from seedsigner.hardware.ina219 import BatteryReading
    from seedsigner.hardware.power import BatteryMonitor
    monitor = BatteryMonitor.get_instance()
    saved = (monitor.gauge, monitor.latest, list(monitor.recent), monitor.reading, monitor.reading_time)
    try:
        monitor.gauge = NS(address=0x43, last_raw=(0x1E10, 0xF060))
        monitor.recent = []
        monitor.add_reading(BatteryReading(3.848, -400.0, 52))
        screen = BatteryInfoScreen()
        with Renderer.get_instance().lock:
            screen._render()
        screenshot("max_battery_screen.png")
        check(Renderer.get_instance().canvas.crop((20, 60, 460, 300)).getbbox() is not None, "Battery screen shows the readings")
        hw, touch = setup_inputs()
        screen = BatteryInfoScreen()
        touch.set_script(GUARD + tap((30, 30)))
        from seedsigner.gui.screens.screen import RET_CODE__BACK_BUTTON
        check(screen.display() == RET_CODE__BACK_BUTTON, "back leaves the Battery screen")
        _time.sleep(0.3)
        check(monitor.fast is False, "fast readings stop when the screen closes")
    finally:
        monitor.gauge, monitor.latest, monitor.recent, monitor.reading, monitor.reading_time = saved


def test_display_speed_setting():
    """ Settings > Advanced > Hardware > Display speed sets the pixel clock, live """
    import spidev
    from seedsigner.hardware.displays.display_driver import DisplayDriverFactory, DISPLAY_TYPE__ILI9486
    from seedsigner.models.settings import Settings
    spi = MagicMock()
    spidev.SpiDev = MagicMock(return_value=spi)
    disp = DisplayDriverFactory.instantiate_display_driver(DISPLAY_TYPE__ILI9486, width=480, height=320)
    settings = Settings.get_instance()
    try:
        disp.show_image(Image.new("RGB", (480, 320)))
        check(spi.max_speed_hz == 24_000_000, "default pixel clock 24 MHz")
        settings.set_value(SettingsConstants.SETTING__MAX_DISPLAY_SPI_MHZ, 32)
        disp.show_image(Image.new("RGB", (480, 320)))
        check(spi.max_speed_hz == 32_000_000, "Display speed 32 MHz applies on the next frame")
        check(all(c.args[1] == 10_000_000 for c in spi.xfer2.call_args_list), "commands stay at 10 MHz")
    finally:
        settings.set_value(SettingsConstants.SETTING__MAX_DISPLAY_SPI_MHZ, 24)



if __name__ == "__main__":
    TestRenderer.configure_instance()
    test_st7796_driver()
    test_ili9486_driver()
    test_display_choice_locked()
    test_xpt2046_driver()
    test_boot_calibration_view()
    test_menu_tap()
    test_back_and_power()
    test_input_guard()
    test_slide_off_cancels()
    test_swipe_scrolls_long_list()
    test_hold_to_confirm()
    test_keyboard_screens()
    test_passphrase_screen()
    test_shuffled_passphrase_keys()
    test_mnemonic_entry_screen()
    test_camera_loops_get_touch_input()
    test_qr_display_centered()
    test_qr_display_touch_controls()
    test_scan_zoom_button()
    test_touch_calibration()
    test_press_reading_drops_landing_noise()
    test_touch_raw_test_screen()
    test_power_pins()
    test_io_test_screen_touch()
    test_battery_icon()
    test_display_speed_setting()
    test_battery_screen()

    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
