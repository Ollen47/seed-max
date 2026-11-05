"""
SeedSigner Max 0022: tap a candidate word on the seed word entry screen to choose it.

Run in its own interpreter by tests/test_seedsigner_max_word_taps.py (hardware modules
are replaced by mocks here, which must not leak into the other tests). It drives the
real SeedMnemonicEntryScreen: touches go through its own handle_touch(), the key codes
it returns go into its own _run() loop, on a 480x320 canvas.
"""
import os
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

from seedsigner.gui.components import GUIConstants
from seedsigner.gui.screens.screen import component_rect
from seedsigner.gui.screens.seed_screens import SeedMnemonicEntryScreen
from seedsigner.hardware.buttons import HardwareButtonsConstants as K
from seedsigner.hardware.touch import TouchEvent
from seedsigner.models.seed import Seed
from seedsigner.models.settings_definition import SettingsConstants

WORDLIST = Seed.get_wordlist(SettingsConstants.WORDLIST_LANGUAGE__ENGLISH)
failures = []


def check(condition, message):
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        failures.append(message)


def tap(x, y):
    return TouchEvent(TouchEvent.TYPE__UP, int(x), int(y), tap=True)


def swipe_up(x, y):
    return TouchEvent(TouchEvent.TYPE__SWIPE, int(x), int(y), direction=TouchEvent.SWIPE__UP)


class Exhausted(Exception):
    pass


def new_screen():
    screen = SeedMnemonicEntryScreen(title="Seed Word #1", initial_letters=["a"], wordlist=WORDLIST)
    with TestRenderer._instance.lock:
        screen._render()
    return screen


def run(screen, events):
    """ Feed touch events (or callables returning a key code) to the screen's _run() """
    pending = iter(events)

    def wait_for(keys=[]):
        for event in pending:
            code = event() if callable(event) else screen.handle_touch(event)
            if code is not None and code in keys:
                return code
        raise Exhausted()

    screen.hw_inputs = MagicMock()
    screen.hw_inputs.wait_for = wait_for
    try:
        return screen._run()
    except Exhausted:
        return None


def key_center(screen, letter):
    for row in screen.keyboard.keys:
        for key in row:
            if key.code == letter:
                x1, y1, x2, y2 = screen.keyboard.key_rect(key)
                return ((x1 + x2) // 2, (y1 + y2) // 2)
    raise KeyError(letter)


def type_letters(screen, letters):
    return [lambda letter=letter: screen.handle_touch(tap(*key_center(screen, letter))) for letter in letters]


def word_x(screen):
    # Middle of a short word: well left of the arrows on the right edge
    return screen.matches_list_x + GUIConstants.COMPONENT_PADDING + 20


def zone_of(screen, word):
    for index, y1, y2 in screen.match_row_zones():
        if screen.possible_words[index] == word:
            return (y1, y2)
    raise KeyError(word)


def middle(zone):
    return (zone[0] + zone[1]) // 2


def test_zones_match_the_drawing():
    """ Every touch zone sits on the word drawn there (light grey text pixels) """
    screen = new_screen()
    run(screen, type_letters(screen, "fo"))
    zones = screen.match_row_zones()
    check(len(zones) >= 5, f"'fo': {len(zones)} words shown besides the highlighted one")
    canvas = TestRenderer._instance.canvas
    x1 = screen.matches_list_x + GUIConstants.COMPONENT_PADDING
    for index, y1, y2 in zones:
        lit = sum(1 for y in range(y1, y2 + 1) for x in range(x1, x1 + 30) if canvas.getpixel((x, y))[0] > 150)
        check(lit > 20, f"zone {y1}-{y2} covers the drawn word {screen.possible_words[index]!r} ({lit} text pixels)")
    previous_bottom = None
    for index, y1, y2 in zones:
        if previous_bottom is not None:
            check(y1 > previous_bottom + 1, f"dead band between zones before {screen.possible_words[index]!r}")
        previous_bottom = y2


def test_tap_a_word_below():
    for position, word in ((1, "focus"), (3, "foil"), (6, "food")):
        screen = new_screen()
        events = type_letters(screen, "fo")
        events.append(lambda: screen.handle_touch(tap(word_x(screen), middle(zone_of(screen, word)))))
        result = run(screen, events)
        check(result == word, f"'fo', tap the word {position} rows below the highlight: {result!r} (expected {word!r})")


def test_tap_a_word_above():
    screen = new_screen()
    down = center(component_rect(screen.matches_list_down_button))
    events = type_letters(screen, "fo") + [tap(*down)] * 4
    events.append(lambda: screen.handle_touch(tap(word_x(screen), middle(zone_of(screen, "focus")))))
    result = run(screen, events)
    check(result == "focus", f"'fo', down arrow 4 times (highlight on 'fold'), tap 'focus' above: {result!r}")


def test_tap_the_highlighted_word():
    screen = new_screen()
    events = type_letters(screen, "for")
    events.append(lambda: screen.handle_touch(tap(*center(component_rect(screen.matches_list_highlight_button)))))
    check(run(screen, events) == "force", "'for', tap the highlighted word: 'force'")


def test_arrows_scroll_by_one():
    screen = new_screen()
    down = center(component_rect(screen.matches_list_down_button))
    up = center(component_rect(screen.matches_list_up_button))
    run(screen, type_letters(screen, "fo") + [tap(*down), tap(*down), tap(*up)])
    check(screen.selected_possible_words_index == 1, f"down, down, up: highlight on {screen.possible_words[screen.selected_possible_words_index]!r} ('focus')")


def test_swipe_still_scrolls():
    screen = new_screen()
    events = type_letters(screen, "fo") + [lambda: screen.handle_touch(swipe_up(word_x(screen), 200))] * 2
    run(screen, events)
    check(screen.selected_possible_words_index == 2, f"two swipes up: highlight on {screen.possible_words[screen.selected_possible_words_index]!r} ('fog')")


def test_boundary_and_empty_space_are_ignored():
    screen = new_screen()
    run(screen, type_letters(screen, "fo"))
    zones = screen.match_row_zones()
    rejected = []
    for (_, _, bottom), (_, top, _) in zip(zones, zones[1:]):
        for y in range(bottom + 1, top):
            if screen.handle_touch(tap(word_x(screen), y)) is not None:
                rejected.append(y)
    check(not rejected, f"taps on the boundaries between words do nothing (failed at y={rejected})")
    check(screen.handle_touch(tap(word_x(screen), screen.top_nav.height + 5)) is None, "empty space above the first word does nothing")
    check(screen.selected_possible_words_index == 0, "and the highlight did not move")

    # Near the end of a short list: nothing below the last word
    screen = new_screen()
    run(screen, type_letters(screen, "zo"))
    check(screen.possible_words == ["zone", "zoo"], f"'zo': {screen.possible_words}")
    check(screen.handle_touch(tap(word_x(screen), 300)) is None, "empty space below the last word does nothing")


def test_letters_and_back_unchanged():
    screen = new_screen()
    run(screen, type_letters(screen, "mux"))
    check("".join(screen.letters).strip() == "mu", f"m, u, (x inactive): {''.join(screen.letters)!r}")
    screen = new_screen()
    from seedsigner.gui.screens.screen import RET_CODE__BACK_BUTTON
    check(run(screen, [tap(30, 30)]) == RET_CODE__BACK_BUTTON, "back from the seed word screen")


def center(rect):
    x1, y1, x2, y2 = rect
    return ((x1 + x2) // 2, (y1 + y2) // 2)


if __name__ == "__main__":
    test_zones_match_the_drawing()
    test_tap_a_word_below()
    test_tap_a_word_above()
    test_tap_the_highlighted_word()
    test_arrows_scroll_by_one()
    test_swipe_still_scrolls()
    test_boundary_and_empty_space_are_ignored()
    test_letters_and_back_unchanged()
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
