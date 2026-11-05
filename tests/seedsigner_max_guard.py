"""
SeedSigner Max 0024: input guard, toasts and hold-to-confirm against the real
HardwareButtons, the real toast thread and real screens, with a scripted touch panel
replayed on the wall clock on a 480x320 canvas.

Run in its own interpreter by tests/test_seedsigner_max_guard.py (it mocks the hardware
modules at import time). Each timing scenario runs in a forked child so that the
singletons start fresh.
"""
import os
import sys
import threading
import time
import types
from threading import Lock
from unittest.mock import MagicMock

# --- hardware mocks ---------------------------------------------------------------------
gpio = types.ModuleType("RPi.GPIO")
gpio.RPI_INFO = {"P1_REVISION": 3}
gpio.BOARD, gpio.IN, gpio.OUT, gpio.PUD_UP, gpio.PUD_OFF, gpio.LOW, gpio.HIGH = 10, 1, 0, 22, 20, 0, 1
gpio.setmode = gpio.setwarnings = gpio.setup = gpio.output = lambda *a, **k: None
gpio.input = lambda pin: 1
rpi = types.ModuleType("RPi")
rpi.GPIO = gpio
sys.modules["RPi"], sys.modules["RPi.GPIO"] = rpi, gpio
spidev = types.ModuleType("spidev")
spidev.SpiDev = MagicMock
sys.modules["spidev"] = spidev
sys.modules["numpy"] = MagicMock()
sys.modules["seedsigner.hardware.pivideostream"] = MagicMock()
sys.modules["seedsigner.hardware.camera"] = MagicMock()
sys.modules["seedsigner.hardware.microsd"] = MagicMock()

from PIL import Image, ImageDraw

from seedsigner.models.settings import Settings
Settings.SETTINGS_FILENAME = "/tmp/seedsigner-max-guard-settings-test.json"

from seedsigner.gui import renderer as renderer_mod


class DummyDisp:
    width, height = 480, 320

    def __init__(self, frame_ms=0):
        self.frame_ms = frame_ms
        self.frames = 0

    def show_image(self, img, x=0, y=0):
        self.frames += 1
        if self.frame_ms:
            time.sleep(self.frame_ms / 1000)

    def invert(self, *a):
        pass


def init_renderer(frame_ms=0):
    r = renderer_mod.Renderer.__new__(renderer_mod.Renderer)
    renderer_mod.Renderer._instance = r
    r.lock = Lock()
    r.disp = DummyDisp(frame_ms)
    r.display_type = "ili9486"
    r.canvas_width, r.canvas_height = 480, 320
    r.canvas = Image.new("RGB", (480, 320))
    r.draw = ImageDraw.Draw(r.canvas)
    return r


renderer = init_renderer()

from seedsigner import controller as ctrl_mod
from seedsigner.hardware.buttons import HardwareButtons, HardwareButtonsConstants as K
from seedsigner.hardware.xpt2046 import parse_response, READ_SEQUENCE

hw = HardwareButtons.get_instance()
failures = []


def check(condition, message):
    print(("ok    " if condition else "FAIL  ") + message, flush=True)
    if not condition:
        failures.append(message)


class FakeTouch:
    """ Scripted panel: list of (seconds, point or None), replayed on the wall clock """
    def __init__(self, script):
        self.script, self.t0 = list(script), None

    def _point(self):
        if self.t0 is None:
            self.t0 = time.time()
        elapsed = time.time() - self.t0
        for duration, point in self.script:
            if elapsed < duration:
                return point
            elapsed -= duration
        return None

    def is_touched(self):
        return self._point() is not None

    def read(self):
        return self._point()


class LeadingIrqTouch(FakeTouch):
    """ PENIRQ (read by the toast thread) goes low `lead` s before the first valid sample """
    def __init__(self, script, lead=0.03):
        super().__init__(script)
        self.lead = lead

    def read(self):
        if self.t0 is None:
            self.t0 = time.time()
        saved = self.t0
        self.t0 = saved + self.lead
        try:
            return self._point()
        finally:
            self.t0 = saved


def install_touch(script, lead=0.0):
    hw.touch_device = LeadingIrqTouch(script, lead) if lead else FakeTouch(script)
    hw.touch_device_failed = False
    hw.touch_tracker.reset()
    hw.touch_blocked_rect = None
    hw.touch_last_contact_ms = 0
    return hw.touch_device


class FakeController:
    screensaver_activation_ms = 10 ** 9
    is_screensaver_start_allowed = False
    is_screensaver_running = False
    toast_notification_thread = None

    def __init__(self):
        self.toasts = []

    def activate_toast(self, toast):
        self.toasts.append(toast)
        toast.start()


ctrl_mod.Controller._instance = FakeController()
ctrl_mod.Controller.get_instance = classmethod(lambda cls: cls._instance)

from seedsigner.gui.screens.screen import ButtonListScreen, ButtonOption, component_rect
from seedsigner.gui.screens.psbt_screens import PSBTFinalizeScreen
from seedsigner.gui.screens.seed_screens import SeedMnemonicEntryScreen
from seedsigner.gui.toast import WarningToast
from seedsigner.models.seed import Seed
from seedsigner.models.settings_definition import SettingsConstants


def run_with_timeout(screen, timeout):
    box = {}

    def target():
        try:
            box["r"] = screen.display()
        except Exception as e:
            box["r"] = f"EXC {type(e).__name__}: {e}"

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    return box.get("r", "TIMEOUT")


def center(rect):
    x1, y1, x2, y2 = rect
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def scenario(name, make_screen, script, expect, timeout=4.0, frame_ms=0, before=None, lead=0.0):
    """ Fork: fresh singletons per scenario; the child prints the result line """
    pid = os.fork()
    if pid == 0:
        try:
            renderer.disp.frame_ms = frame_ms
            install_touch(script, lead)
            screen = make_screen()
            if before:
                before(screen)
            started = time.time()
            result = run_with_timeout(screen, timeout)
            elapsed = time.time() - started
            ok = result in expect if isinstance(expect, tuple) else result == expect
            print(("ok    " if ok else "FAIL  ") + f"{name}: {result!r} after {elapsed:.1f}s (expected {expect!r})", flush=True)
            os._exit(0 if ok else 1)
        except Exception as e:
            print(f"FAIL  {name}: exception {e!r}", flush=True)
            os._exit(1)
    _, status = os.waitpid(pid, 0)
    if os.WEXITSTATUS(status) != 0:
        failures.append(name)


def finalize_screen():
    return PSBTFinalizeScreen(button_data=[ButtonOption("Approve PSBT")])


def bottom_list():
    return ButtonListScreen(title="Warning", button_data=[ButtonOption("Cancel"), ButtonOption("I understand")],
                            is_bottom_list=True, show_back_button=False)


APPROVE = center(component_rect(finalize_screen().buttons[0]))
UNDERSTAND = center(component_rect(bottom_list().buttons[1]))


def test_resting_finger_dropout_does_not_start_a_hold():
    """ 0024: a finger resting on the panel across screens flickers; one dropout must not end the guard """
    scenario("resting finger, 70 ms dropout, then held 1.2 s: ignored", finalize_screen,
             [(0.5, APPROVE), (0.07, None), (1.2, APPROVE), (5, None)], "TIMEOUT", timeout=3.0)
    scenario("resting finger, 150 ms dropout, then held 1.2 s: ignored", finalize_screen,
             [(0.5, APPROVE), (0.15, None), (1.2, APPROVE), (5, None)], "TIMEOUT", timeout=3.0)
    scenario("finger lifted 300 ms, then held 1.2 s: approved", finalize_screen,
             [(0.5, APPROVE), (0.3, None), (1.3, APPROVE), (5, None)], 0, timeout=4.0)
    scenario("deliberate tap 150 ms after the guard: ignored (hold needed)", finalize_screen,
             [(0.6, None), (0.15, APPROVE), (5, None)], "TIMEOUT", timeout=2.5)


def test_hold_needs_contact_at_the_end():
    """ 0024: with slow frames the release debounce must not carry a short hold over the line """
    # (the first frame itself takes 200 ms here, so the guard ends at ~0.6 s)
    scenario("200 ms frames, held 0.85 s then lifted: ignored", finalize_screen,
             [(0.9, None), (0.85, APPROVE), (5, None)], "TIMEOUT", timeout=3.5, frame_ms=200)
    scenario("200 ms frames, held 1.35 s: approved", finalize_screen,
             [(0.9, None), (1.35, APPROVE), (5, None)], 0, timeout=4.5, frame_ms=200)
    # Tight sweep with 100 ms frames (the Pi Zero's): finger down 1.00-1.07 s, i.e. a hold
    # of ~0.88-0.95 s once the DOWN debounce and the selection frame are taken off
    for k in range(4):
        scenario(f"100 ms frames, finger down {1.0 + k * 0.023:.3f} s: ignored", finalize_screen,
                 [(0.7, None), (1.0 + k * 0.023, APPROVE), (5, None)], "TIMEOUT", timeout=3.0, frame_ms=100)


def toast_after(delay, duration=5):
    """ The realistic order: the screen is up and polling when the toast appears """
    def before(screen):
        def later():
            time.sleep(delay)
            WarningToast("Battery low (14%)", activation_delay=0, duration=duration).start()
        threading.Thread(target=later, daemon=True).start()
    return before


def test_toast_tap_is_swallowed():
    """
    0024: a tap on a toast dismisses it and nothing else. The toast thread polls PENIRQ
    every 100 ms, so the contact phase is swept (10 offsets of 11 ms) with PENIRQ
    leading the first valid sample by 30 ms, as on a real landing.
    """
    for k in range(10):
        scenario(f"tap on a battery toast over 'I understand', phase {k}: ignored", bottom_list,
                 [(1.0 + k * 0.011, None), (0.12, UNDERSTAND), (5, None)], "TIMEOUT", timeout=2.5,
                 before=toast_after(0.5), lead=0.03)
    for k in range(5):
        scenario(f"hold on 'Approve' under a battery toast, phase {k}: ignored", finalize_screen,
                 [(1.0 + k * 0.011, None), (1.4, APPROVE), (5, None)], "TIMEOUT", timeout=3.5,
                 before=toast_after(0.5), lead=0.03)
    scenario("tap on the toast, lift, tap again after it is gone: accepted", bottom_list,
             [(1.0, None), (0.12, UNDERSTAND), (0.6, None), (0.12, UNDERSTAND), (5, None)], 1, timeout=4.0,
             before=toast_after(0.5), lead=0.03)
    # The toast thread only sees PENIRQ, not where the finger is: a touch that dismisses a
    # blocking toast never acts, wherever it lands. Depending on the phase the tap is
    # swallowed or (if the input loop got to it first) acted on once the toast is gone;
    # it is never misdirected to the other button.
    for k in range(3):
        scenario(f"tap outside the toast band while it is up, phase {k}: swallowed or 'Cancel', never 'I understand'", bottom_list,
                 [(1.0 + k * 0.033, None), (0.12, center(component_rect(bottom_list().buttons[0]))), (5, None)], ("TIMEOUT", 0),
                 timeout=4.0, before=toast_after(0.5, duration=1.5), lead=0.03)
    scenario("tap (hint toast appears), then hold through the hint: approved", finalize_screen,
             [(0.6, None), (0.15, APPROVE), (0.5, None), (1.4, APPROVE), (5, None)], 0, timeout=4.5)

    def replaced_while_down(screen):
        def later():
            time.sleep(0.5)
            first = WarningToast("Battery low (14%)", activation_delay=0, duration=5)
            first.start()
            time.sleep(0.52)          # the finger lands on the band at 1.0 s; replace the toast right then
            first.stop()
            WarningToast("Battery almost empty", activation_delay=0, duration=5).start()
        threading.Thread(target=later, daemon=True).start()
    scenario("toast replaced while the finger is on its band: ignored", bottom_list,
             [(1.0, None), (0.3, UNDERSTAND), (5, None)], "TIMEOUT", timeout=3.0, before=replaced_while_down, lead=0.03)


def test_toast_tap_on_seed_word_list_is_swallowed():
    """ The case found by the review: a word hidden under a toast must not be chosen """
    wordlist = Seed.get_wordlist(SettingsConstants.WORDLIST_LANGUAGE__ENGLISH)

    def make():
        return SeedMnemonicEntryScreen(title="Seed Word #1", initial_letters=["a"], wordlist=wordlist)

    def typed_fo_with_toast(screen):
        screen.letters = ["f", "o", " "]
        screen.calc_possible_alphabet(new_letter=True)
        toast_after(0.5)(screen)

    word_x = make().matches_list_x + 10
    for k in range(5):
        scenario(f"tap on the toast over the word column, phase {k}: no word chosen", make,
                 [(1.0 + k * 0.011, None), (0.12, (word_x, 297)), (5, None)], "TIMEOUT", timeout=2.5,
                 before=typed_fo_with_toast, lead=0.03)


def test_hint_toast_lets_touches_through():
    ctrl = ctrl_mod.Controller.get_instance()
    ctrl.toasts.clear()
    hw.touch_device = FakeTouch([(5, None)])
    screen = finalize_screen()
    screen._show_hold_hint()
    toast = ctrl.toasts[-1]
    check(toast.toast.blocks_touches is False, "the 'Hold to confirm' hint does not block touches")
    check(WarningToast("x").toast.blocks_touches is True, "other toasts do")
    toast.stop()
    time.sleep(0.3)


def test_blocked_rect_unit():
    install_touch([(5, None)])
    hw.begin_screen(touch_handler=lambda e: 42)
    hw.touch_require_release = False
    hw.touch_screen_started = 0
    hw.set_touch_blocked_rect((0, 274, 480, 320))
    from seedsigner.hardware.touch import TouchEvent
    # Drive the tracker directly: DOWN inside the band
    hw.touch_device = FakeTouch([(0.3, (240, 300)), (5, None)])
    codes = []
    armed_at = None
    deadline = time.time() + 0.8
    t0 = time.time()
    while time.time() < deadline:
        codes += hw._touch_codes(hw.poll_touch())
        if hw.touch_require_release and armed_at is None:
            armed_at = time.time() - t0
        time.sleep(0.01)
    check(codes == [] and armed_at is not None and armed_at < 0.1, f"DOWN on the blocked band: no code, guard armed at {armed_at}")
    check(not hw.touch_require_release, "and released again once the panel has been quiet")
    hw.set_touch_blocked_rect(None)


def test_guard_counts_from_the_shown_frame():
    hw.begin_screen(touch_handler=lambda e: None)
    t0 = hw.touch_screen_started
    time.sleep(0.05)
    hw.mark_screen_shown()
    check(hw.touch_screen_started >= t0 + 40, "mark_screen_shown moves the guard's start")


def test_touch_read_errors_are_contained():
    class Broken:
        def read(self):
            raise OSError("spi")

        def is_touched(self):
            return False

    hw.touch_device = Broken()
    hw.touch_device_failed = False
    hw.touch_read_errors = 0
    hw.touch_require_release = False
    try:
        events = hw.poll_touch()
    except Exception as e:
        events = e
    check(events == [] and hw.touch_read_errors == 1, f"an SPI error reads as no touch ({events!r})")


def test_xpt2046_rejects_z1_ge_z2():
    def response_for(values):
        response = [0] * len(READ_SEQUENCE)
        for k, value in enumerate(values):
            word = (value & 0x0FFF) << 3
            response[2 * k + 1] = word >> 8
            response[2 * k + 2] = word & 0xFF
        return response

    check(parse_response([0x55] * len(READ_SEQUENCE), 400) is None, "0x55 pattern on MISO rejected")
    check(parse_response([0xAA] * len(READ_SEQUENCE), 400) is None, "0xAA pattern on MISO rejected")
    check(parse_response(response_for([2000, 1500, 0, 1000, 1002, 1001, 0, 2000, 2002, 2001]), 400) is None, "Z1 > Z2 rejected")
    real = parse_response(response_for([800, 3500, 0, 1000, 1002, 1001, 0, 2000, 2002, 2001]), 400)
    check(real is not None and real[:2] == (1001, 2001), f"a real press still parses: {real}")


if __name__ == "__main__":
    test_xpt2046_rejects_z1_ge_z2()
    test_guard_counts_from_the_shown_frame()
    test_touch_read_errors_are_contained()
    test_blocked_rect_unit()
    test_hint_toast_lets_touches_through()
    test_resting_finger_dropout_does_not_start_a_hold()
    test_hold_needs_contact_at_the_end()
    test_toast_tap_is_swallowed()
    test_toast_tap_on_seed_word_list_is_swallowed()
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
