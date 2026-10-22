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

    spi = MagicMock()
    spidev.SpiDev = MagicMock(return_value=spi)
    GPIO.setup = MagicMock()
    disp = DisplayDriverFactory.instantiate_display_driver(DISPLAY_TYPE__ILI9486, width=480, height=320)
    check(spidev.SpiDev.call_args.args == (0, 0), "ILI9486 on SPI0 CE0")
    check(spi.max_speed_hz == 16_000_000, "ILI9486 SPI clock 16 MHz (converter-limited default)")
    check((disp.width, disp.height) == (480, 320), "ILI9486 exposes a 480x320 landscape surface")
    pins = sorted(c.args[0] for c in GPIO.setup.call_args_list)
    check(pins == [18, 22], f"only RS (pin 18) and RST (pin 22) are driven, no backlight pin: {pins}")

    transfers = [list(c.args[0]) for c in spi.writebytes.call_args_list + spi.writebytes2.call_args_list]
    check(all(len(t) % 2 == 0 for t in transfers), "every transfer is whole 16-bit words")
    check(all(t[0::2] == [0] * (len(t) // 2) for t in transfers), "commands and parameters are sent as (0x00, byte) words")
    commands = [c.args[0][1] for c in spi.writebytes.call_args_list]
    check(commands[:2] == [0xB0, 0x11] and commands[-1] == 0x29, f"init: interface mode, sleep out ... display on: {[hex(c) for c in commands]}")
    params = [list(c.args[0]) for c in spi.writebytes2.call_args_list]
    check([0, 0x55] in params, "16 bits per pixel")
    check([0, 0xE8] in params, "MADCTL 0xE8: landscape as the Linux driver's rotation 0")

    spi.reset_mock()
    disp.show_image(Image.new("RGB", (480, 320), (255, 0, 0)))
    params = [list(c.args[0]) for c in spi.writebytes2.call_args_list]
    check([0, 0, 0, 0, 0, 0x01, 0, 0xDF] in params and [0, 0, 0, 0, 0, 0x01, 0, 0x3F] in params,
          "window 0..479 x 0..319, as 16-bit words")
    frame = spi.writebytes2.call_args_list[-1].args[0]
    check(len(frame) == 480 * 320 * 2 and frame[:2] == bytes([0xF8, 0x00]), "full frame of 307200 bytes, big-endian RGB565")

    spi.reset_mock()
    disp.invert(enabled=True)
    check([list(c.args[0]) for c in spi.writebytes.call_args_list] == [[0, 0x21]], "'Invert colors' = INVON")


def test_boot_calibration_view():
    from seedsigner.views.settings_views import TouchCalibrationView, SettingsMenuView
    from seedsigner.views.view import MainMenuView
    check(hw_profile.TOUCH_CALIBRATE_AT_BOOT is True, "the first v5 image calibrates the touch screen at boot")
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
    spi.xfer2 = MagicMock(return_value=response_for([800, 3500, 0, 1000, 2000, 1010, 2010, 1005, 2005]))
    spidev.SpiDev = MagicMock(return_value=spi)
    GPIO.LOW = 0
    irq = {"level": 1}
    GPIO.input = lambda pin: irq["level"] if pin == hw_profile.TOUCH_PIN_IRQ else 1

    touch = XPT2046(calibration=TouchCalibration(0.1, 0.0, 0.0, 0.0, 0.1, 0.0))
    check(spidev.SpiDev.call_args.args == (0, 1), "touch controller on SPI0 CE1")
    check(spi.max_speed_hz == 1_000_000, "touch read at 1 MHz")
    check(spi.xfer2.call_args.args[0] == [0xD0, 0, 0], "init powers down with PENIRQ armed")

    spi.xfer2.reset_mock()
    check(touch.read() is None and not spi.xfer2.called, "PENIRQ high: no SPI traffic at all")

    irq["level"] = 0
    position = touch.read()
    check(spi.xfer2.call_args.args[0] == READ_SEQUENCE, "PENIRQ low: one 19-byte read sequence")
    # best two of (1000, 1010, 1005) -> 1002, of (2000, 2010, 2005) -> 2002; x0.1 each
    check(position == (100, 200), f"raw (1002, 2002) mapped through the calibration: {position}")



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
    check(canvas.getpixel((40, 160)) == (0, 0, 0) and canvas.getpixel((450, 300)) == (0, 0, 0),
          "the side bars are black: nothing of the previous screen shows through")
    screenshot("max_qr_display.png")

    K = HardwareButtonsConstants
    check(screen.handle_touch(TouchEvent(TouchEvent.TYPE__SWIPE, 240, 100, direction=TouchEvent.SWIPE__UP)) == K.KEY_UP,
          "QR: drag up = brighter")
    check(screen.handle_touch(TouchEvent(TouchEvent.TYPE__UP, 240, 100, tap=True)) == K.KEY_PRESS, "QR: tap = done")



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

    # v5 (no carrier, PiSugar 3 Air): the power pins are never touched
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



if __name__ == "__main__":
    TestRenderer.configure_instance()
    test_st7796_driver()
    test_ili9486_driver()
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
    test_touch_calibration()
    test_power_pins()
    test_io_test_screen_touch()

    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
