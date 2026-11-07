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
    # 0026: no dead band. Every zone, the highlighted word's included, starts right
    # below the previous one
    all_zones = screen.match_zones()
    gaps = [(a[2], b[1]) for a, b in zip(all_zones, all_zones[1:]) if b[1] != a[2] + 1]
    check(not gaps, f"zones follow each other with no gap and no overlap (bad joins: {gaps})")


def drawn_word_rows(screen):
    """
    Independent of match_row_zones(): intercept the real draw.text() calls of
    render_possible_matches() and return {word: set of screen rows with its pixels}.
    """
    import PIL.ImageDraw
    original = PIL.ImageDraw.ImageDraw.text
    calls = []

    def record(self, xy, text, *args, **kwargs):
        calls.append((xy, text, kwargs.get("font"), kwargs.get("anchor")))
        return original(self, xy, text, *args, **kwargs)

    PIL.ImageDraw.ImageDraw.text = record
    try:
        screen.render_possible_matches()
    finally:
        PIL.ImageDraw.ImageDraw.text = original
    rows = {}
    panel_width = screen.canvas_width - screen.matches_list_x
    for xy, text, font, anchor in calls:
        if font is not screen.word_font:
            continue
        img = Image.new("L", (panel_width, screen.canvas_height), 0)
        ImageDraw.Draw(img).text(xy, text, fill=255, font=font, anchor=anchor)
        img = img.crop((0, screen.top_nav.height, panel_width, screen.canvas_height))
        px = img.load()
        lit = {y + screen.top_nav.height for y in range(img.height) if any(px[x, y] > 100 for x in range(img.width))}
        rows.setdefault(text, set()).update(lit)
    return rows


def drawn_word_lines(screen):
    """
    Independent of match_rows(): {word: (top, baseline)} of every word on screen, the
    line box each one is drawn in. The baseline is the y of the real draw.text() call
    (anchor "ls"); the top is the baseline minus the font's own capital-and-ascender
    height, measured here on "Hdl" with the same font. Lines with less than half of
    that height on screen are left out (cut off by the bottom edge).
    """
    import PIL.ImageDraw
    original = PIL.ImageDraw.ImageDraw.text
    calls = []

    def record(self, xy, text, *args, **kwargs):
        calls.append((xy, text, kwargs.get("font")))
        return original(self, xy, text, *args, **kwargs)

    PIL.ImageDraw.ImageDraw.text = record
    try:
        screen.render_possible_matches()
    finally:
        PIL.ImageDraw.ImageDraw.text = original
    left, top, right, bottom = screen.word_font.getbbox("Hdl", anchor="ls")
    height = -top
    lines = {}
    for xy, text, font in calls:
        if font is not screen.word_font:
            continue
        # The list is drawn on an image whose row `top_nav.height` lands on screen row
        # `matches_list_y` (render_possible_matches pastes it there)
        baseline = xy[1] - screen.top_nav.height + screen.matches_list_y
        if baseline - height / 2 < screen.canvas_height:
            lines[text] = (baseline - height, baseline)
    return lines


def test_zones_against_the_real_draw_calls():
    """ Every zone covers only its own drawn word; every y in a zone chooses that word; a tap picks the nearest word drawn """
    problems = 0
    checked = 0
    for prefix in ("fo", "ab", "zo", "mush", "w"):
        screen = new_screen()
        screen.letters = list(prefix) + [" "]
        screen.calc_possible_alphabet(new_letter=True)
        words = screen.possible_words
        for index in range(len(words)):
            screen.selected_possible_words_index = index
            rows = drawn_word_rows(screen)
            x1, y1, x2, y2 = component_rect(screen.matches_list_highlight_button)
            zones = screen.match_row_zones()
            covered = set(range(y1, y2 + 1))      # the highlighted button itself
            for i, zy1, zy2 in zones:
                checked += 1
                own = {y for y in rows.get(words[i], set())}
                if not own or not (zy1 <= min(own) and max(own) - 4 <= zy2):
                    problems += 1
                    print(f"  misaligned: {prefix!r} index {index} word {words[i]!r} zone {zy1}-{zy2} drawn {sorted(own)[:1]}-{sorted(own)[-1:]}")
                for other, orows in rows.items():
                    if other != words[i] and any(zy1 <= r <= zy2 for r in orows):
                        problems += 1
                        print(f"  overlap: {prefix!r} index {index} zone of {words[i]!r} touches {other!r}")
                for y in (zy1, (zy1 + zy2) // 2, zy2):
                    screen.selected_possible_words_index = index
                    code = screen.touch_matches_list(tap(word_x(screen), y))
                    if code != K.KEY2 or screen.selected_possible_words_index != i:
                        problems += 1
                        print(f"  tap: {prefix!r} index {index} y={y} expected word {i}, got {code} / {screen.selected_possible_words_index}")
                covered.update(range(zy1, zy2 + 1))
            # 0026, independent oracle: every tap between the first and the last word
            # drawn picks the word whose drawn line (from the real draw calls; the
            # highlighted word: its button) is nearest; a few px beyond them still
            # count; further out nothing
            screen.selected_possible_words_index = index      # the taps above moved it
            extents = dict(drawn_word_lines(screen))
            extents[words[index]] = (y1, y2)
            top = min(e[0] for e in extents.values())
            bottom = max(e[1] for e in extents.values())
            for y in range(screen.top_nav.height, screen.canvas_height):
                screen.selected_possible_words_index = index
                code = screen.touch_matches_list(tap(word_x(screen), y))
                chosen = words[screen.selected_possible_words_index] if code == K.KEY2 else None
                distances = sorted((max(e[0] - y, 0, y - e[1]), w) for w, e in extents.items())
                if top <= y <= bottom:
                    # Exactly on a midpoint either neighbor is a fair answer (rounding)
                    fair = {w for d, w in distances if d <= distances[0][0] + 1}
                    if chosen not in fair or code != K.KEY2:
                        problems += 1
                        print(f"  nearest: {prefix!r} index {index} y={y} chose {chosen!r}, nearest {distances[0][1]!r}")
                elif y < top - 10 or y > bottom + 10:
                    if code is not None:
                        problems += 1
                        print(f"  empty space acted: {prefix!r} index {index} y={y} chose {chosen!r}")
    check(checked > 100 and problems == 0, f"{checked} zones checked against the real draw calls, {problems} problems")


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


def test_no_dead_band_and_empty_space_is_ignored():
    """
    0026: on the device a tap reads a few pixels off. Every pixel row from the first word
    to the last chooses a word (no dead band); empty space beyond them still does nothing.
    """
    screen = new_screen()
    run(screen, type_letters(screen, "fo"))
    zones = screen.match_zones()
    dead = []
    for y in range(zones[0][1], zones[-1][2] + 1):
        screen.selected_possible_words_index = 0
        if screen.handle_touch(tap(word_x(screen), y)) is None:
            dead.append(y)
    screen.selected_possible_words_index = 0
    check(not dead, f"no dead band between the first and the last word (dead at y={dead[:10]})")
    check(screen.handle_touch(tap(word_x(screen), zones[0][1] - 1)) is None, "the row above the first zone does nothing")
    check(screen.selected_possible_words_index == 0, "and the highlight did not move")

    # Near the end of a short list: nothing well below the last word
    screen = new_screen()
    run(screen, type_letters(screen, "zo"))
    check(screen.possible_words == ["zone", "zoo"], f"'zo': {screen.possible_words}")
    check(screen.handle_touch(tap(word_x(screen), 300)) is None, "empty space below the last word does nothing")


def test_list_edges_reach_four_pixels():
    """
    Literal 4 px beyond the first and last word (the drawn lines, not match_zones()):
    from 4 px above the first line a tap chooses it, from 5 px it does nothing; same
    below the last one.
    """
    for prefix in ("zo", "fo"):
        screen = new_screen()
        run(screen, type_letters(screen, prefix))
        lines = drawn_word_lines(screen)
        x1, y1, x2, y2 = component_rect(screen.matches_list_highlight_button)
        lines[screen.possible_words[0]] = (y1, y2)       # the highlighted word: its button
        first = min(lines.items(), key=lambda kv: kv[1][0])
        last = max(lines.items(), key=lambda kv: kv[1][1])
        results = {}
        for name, (word, (top, bottom)), inside, outside in (
                ("above the first", first, first[1][0] - 4, first[1][0] - 5),
                ("below the last", last, last[1][1] + 4, last[1][1] + 5)):
            screen.selected_possible_words_index = 0
            code_in = screen.handle_touch(tap(word_x(screen), inside))
            chosen = screen.possible_words[screen.selected_possible_words_index]
            screen.selected_possible_words_index = 0
            code_out = screen.handle_touch(tap(word_x(screen), outside)) if outside < screen.canvas_height else None
            results[name] = (code_in == K.KEY2 and chosen == word, code_out is None)
        check(all(a and b for a, b in results.values()), f"{prefix!r}: 4 px beyond the outer words counts, 5 px doesn't: {results}")


def test_second_word_reachable_with_reading_offsets():
    """
    The report of 6 Nov 2026: right under the highlighted word, the second word could
    not be chosen, because taps read lower than the stylus fell in the dead band below
    it. Now any tap from 9 px above its drawn middle (halfway to the button) to 10 px below
    (halfway to the next word) chooses it.
    """
    for prefix, second in (("b", "bachelor"), ("fo", "focus"), ("ab", "ability")):
        screen = new_screen()
        run(screen, type_letters(screen, prefix))
        rows = drawn_word_rows(screen)[second]
        drawn_middle = (min(rows) + max(rows)) // 2
        missed = []
        for offset in range(-9, 11):
            screen.selected_possible_words_index = 0
            code = screen.handle_touch(tap(word_x(screen), drawn_middle + offset))
            if code != K.KEY2 or screen.possible_words[screen.selected_possible_words_index] != second:
                missed.append(offset)
        check(not missed, f"{prefix!r}: {second!r} (drawn middle y={drawn_middle}) chosen at every offset -9..+10 (missed: {missed})")


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
    test_zones_against_the_real_draw_calls()
    test_tap_a_word_below()
    test_tap_a_word_above()
    test_tap_the_highlighted_word()
    test_arrows_scroll_by_one()
    test_swipe_still_scrolls()
    test_no_dead_band_and_empty_space_is_ignored()
    test_list_edges_reach_four_pixels()
    test_second_word_reachable_with_reading_offsets()
    test_letters_and_back_unchanged()
    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
