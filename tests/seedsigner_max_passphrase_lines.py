"""
SeedSigner Max 0023: the passphrase field on three lines.

Run in its own interpreter by tests/test_seedsigner_max_passphrase_lines.py (hardware
modules are replaced by mocks here). It drives the real SeedAddPassphraseScreen: touches
go through its own handle_touch(), the key codes it returns go into its own _run() loop,
on a 480x320 canvas.
"""
import sys
import threading
from unittest.mock import MagicMock

sys.modules["RPi"] = MagicMock()
sys.modules["RPi.GPIO"] = MagicMock()
sys.modules["spidev"] = MagicMock()
sys.modules["seedsigner.hardware.microsd"] = MagicMock()

from PIL import Image, ImageDraw

from seedsigner.gui.renderer import Renderer


class TestRenderer(Renderer):
    @classmethod
    def configure_instance(cls):
        renderer = cls.__new__(cls)
        cls._instance = renderer
        renderer.canvas_width, renderer.canvas_height = 480, 320
        renderer.canvas = Image.new("RGB", (480, 320))
        renderer.draw = ImageDraw.Draw(renderer.canvas)
        renderer.lock = threading.Lock()
        renderer.disp = MagicMock()

    def show_image(self, *args, **kwargs):
        pass


TestRenderer.configure_instance()
Renderer.get_instance = classmethod(lambda cls: TestRenderer._instance)

from seedsigner.gui.keyboard import Keyboard, MultiLineTextEntryDisplay, TextEntryDisplay
from seedsigner.gui.screens.screen import component_rect
from seedsigner.gui.screens.seed_screens import SeedAddPassphraseScreen
from seedsigner.hardware import hw_profile
from seedsigner.hardware.touch import TouchEvent

failures = []
LEFT, RIGHT, DEL, SPACE = (Keyboard.KEY_CURSOR_LEFT["code"], Keyboard.KEY_CURSOR_RIGHT["code"],
                           Keyboard.KEY_BACKSPACE["code"], Keyboard.KEY_SPACE["code"])


def check(condition, message):
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        failures.append(message)


def tap(x, y):
    return TouchEvent(TouchEvent.TYPE__UP, int(x), int(y), tap=True)


def center(rect):
    x1, y1, x2, y2 = rect
    return ((x1 + x2) // 2, (y1 + y2) // 2)


class Exhausted(Exception):
    pass


def new_screen(passphrase=""):
    screen = SeedAddPassphraseScreen(passphrase=passphrase)
    with TestRenderer._instance.lock:
        screen._render()
    return screen


def run(screen, actions):
    """ `actions`: callables returning a key code (they tap through handle_touch) """
    pending = iter(actions)

    def wait_for(keys=[]):
        for action in pending:
            code = action()
            if code is not None and code in keys:
                return code
        raise Exhausted()

    screen.hw_inputs = MagicMock()
    screen.hw_inputs.wait_for = wait_for
    try:
        return screen._run()
    except Exhausted:
        return None


def key(screen, code):
    """ A tap on the key `code` of the keyboard currently shown """
    def action():
        keyboard = getattr(screen, "touch_cur_keyboard", screen.keyboard_abc)
        for row in keyboard.keys:
            for k in row:
                if k.code == code:
                    return screen.handle_touch(tap(*center(keyboard.key_rect(k))))
        raise KeyError(code)
    return action


def type_text(screen, text):
    return [key(screen, SPACE if c == " " else c) for c in text]


def button(screen, name):
    return lambda: screen.handle_touch(tap(*center(component_rect(getattr(screen, name)))))


def save(screen):
    return button(screen, "hw_button3")


def lit_pixels(rect, color_test):
    canvas = TestRenderer._instance.canvas
    x1, y1, x2, y2 = rect
    return sum(1 for y in range(y1, y2) for x in range(x1, x2) if color_test(canvas.getpixel((x, y))))


def is_orange(p):
    return p[0] > 200 and 100 < p[1] < 200 and p[2] < 80


def is_grey_dot(p):
    return 100 <= p[0] <= 140 and abs(p[0] - p[1]) < 8 and abs(p[1] - p[2]) < 8


def line_rect(display, row):
    """ Screen rectangle of visible line `row` of the field """
    top = display.rect[1] + display.LINE_GAP + 2 + row * display.line_height
    return (display.rect[0] + 4, top, display.rect[2] - 16, top + display.ascent + 2)


def test_layout():
    screen = new_screen()
    display = screen.text_entry_display
    check(isinstance(display, MultiLineTextEntryDisplay), "Max profile: multi-line passphrase field")
    check((display.num_lines, display.chars_per_line) == (3, 40), f"3 lines of 40 characters ({display.num_lines} x {display.chars_per_line})")
    for keyboard in (screen.keyboard_abc, screen.keyboard_ABC, screen.keyboard_digits, screen.keyboard_symbols_1, screen.keyboard_symbols_2):
        check(keyboard.rect[1] >= display.rect[3] + 4, f"keyboard starts below the field ({keyboard.rect[1]} >= {display.rect[3] + 4})")
    check(screen.keyboard_abc.key_height >= 40, f"keys stay at least 40 px tall ({screen.keyboard_abc.key_height} px)")
    for name in ("hw_button1", "hw_button2", "hw_button3"):
        check(component_rect(getattr(screen, name))[0] > display.rect[2], f"{name} right of the field")


def test_typing_wraps_onto_the_next_lines():
    screen = new_screen()
    text = "abcdefghij" * 5          # 50 characters: a full line and 10 on the second
    result = run(screen, type_text(screen, text) + [save(screen)])
    check(result == dict(passphrase=text), f"50 letters typed by touch and saved ({len(result['passphrase']) if result else None} chars)")
    display = screen.text_entry_display
    check(lit_pixels(line_rect(display, 0), is_orange) > 200, "line 1 is full of text")
    second = lit_pixels(line_rect(display, 1), is_orange)
    check(second > 40, f"line 2 has the last 10 characters ({second} text pixels)")
    check(lit_pixels(line_rect(display, 2), is_orange) == 0, "line 3 is still empty")


def test_spaces_are_visible():
    screen = new_screen()
    display = screen.text_entry_display
    display.render("ab  cd ", 7)
    dots = lit_pixels(line_rect(display, 0), is_grey_dot)
    check(dots > 6, f"the three spaces (two in a row and one at the end) are drawn as dots ({dots} px)")
    display.render("abcdefghij" * 4 + " x", 42)
    check(lit_pixels(line_rect(display, 1), is_grey_dot) > 2, "a space at the start of a line is visible")


def test_cursor_editing_in_the_middle():
    screen = new_screen()
    actions = type_text(screen, "abcdef") + [key(screen, LEFT)] * 3 + type_text(screen, "z")
    actions += [key(screen, RIGHT), key(screen, DEL)] + [save(screen)]
    result = run(screen, actions)
    check(result == dict(passphrase="abczef"), f"cursor left 3, type z, right 1, DEL: {result!r} ('abczef')")


def test_long_passphrase_scrolls_vertically():
    screen = new_screen()
    text = ("abcdefghijklmnopqrst" * 7)[:130]   # 130 characters: 4 lines
    run(screen, type_text(screen, text))
    display = screen.text_entry_display
    check(display.first_line == 1, f"130 characters, cursor at the end: lines 2-4 shown (first line {display.first_line + 1})")
    canvas = TestRenderer._instance.canvas
    check(canvas.getpixel((display.rect[2] - 8, display.rect[1] + 5)) == (204, 204, 204), "mark: more text above")
    run(screen, [key(screen, LEFT)] * 95)       # cursor at 35, on line 1
    check(display.first_line == 0 and display.cursor_position == 35, f"cursor back on line 1: first line shown {display.first_line + 1}, cursor {display.cursor_position}")
    check(canvas.getpixel((display.rect[2] - 8, display.rect[3] - 5)) == (204, 204, 204), "mark: more text below")


def test_keyboard_switch_keeps_the_cursor():
    screen = new_screen()
    actions = type_text(screen, "abcdef") + [key(screen, LEFT)] * 2 + [button(screen, "hw_button2")]
    actions += [key(screen, "7"), save(screen)]
    result = run(screen, actions)
    check(result == dict(passphrase="abcd7ef"), f"cursor left 2, switch to 123, type 7: {result!r} ('abcd7ef')")


def test_prefilled_passphrase():
    screen = new_screen("Correct horse  battery staple, il mio cane si chiama Fido 2026!")
    display = screen.text_entry_display
    check(display.cursor_position == len(screen.passphrase), "an existing passphrase opens with the cursor at the end")
    check(lit_pixels(line_rect(display, 1), is_orange) > 100, "and fills the second line")


def test_standard_profile_unchanged():
    saved = hw_profile.ACTIVE_PROFILE
    hw_profile.ACTIVE_PROFILE = hw_profile.PROFILE__STANDARD
    try:
        screen = SeedAddPassphraseScreen(passphrase="")
        check(type(screen.text_entry_display) is TextEntryDisplay, "standard profile: the original one-line field")
        check(screen.text_entry_display.height == 30, "with its original height")
    finally:
        hw_profile.ACTIVE_PROFILE = saved


if __name__ == "__main__":
    test_layout()
    test_typing_wraps_onto_the_next_lines()
    test_spaces_are_visible()
    test_cursor_editing_in_the_middle()
    test_long_passphrase_scrolls_vertically()
    test_keyboard_switch_keeps_the_cursor()
    test_prefilled_passphrase()
    test_standard_profile_unchanged()
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
