"""
SeedSigner Max 0025: on the zoomed-in SeedQR transcription screen a tap on the
"click to exit" label leaves the screen.

The label sits at the bottom centre, inside the bottom quarter that the touch d-pad maps
to DOWN, so before 0025 tapping it panned down one zone instead of exiting.

Run in its own interpreter by tests/test_seedsigner_max_seedqr_zoom_exit.py (hardware
modules are replaced by mocks here). It drives the real SeedTranscribeSeedQRZoomedInScreen:
touches go through its own handle_touch(), the key codes go into its own _run() loop,
on a 480x320 canvas. Where the label is comes from the pixels of the drawn overlay, not
from the screen's own exit_label_rect.
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

    def show_image_pan(self, *args, **kwargs):
        pass


TestRenderer.configure_instance()
Renderer.get_instance = classmethod(lambda cls: TestRenderer._instance)

from seedsigner.gui.components import GUIConstants
from seedsigner.gui.screens.seed_screens import SeedTranscribeSeedQRZoomedInScreen
from seedsigner.hardware.buttons import HardwareButtonsConstants as K
from seedsigner.hardware.touch import TouchEvent

failures = []


def check(condition, message):
    print(("ok    " if condition else "FAIL  ") + message)
    if not condition:
        failures.append(message)


def tap(x, y):
    return TouchEvent(TouchEvent.TYPE__UP, int(x), int(y), tap=True)


class Exhausted(Exception):
    pass


def new_screen(num_modules=25):
    # CompactSeedQR: 21x21 (12 words), 25x25 (24 words); standard SeedQR 24 words: 29x29
    qr_data = {21: "0" * 24, 25: "0" * 48, 29: "0" * 96}[num_modules]
    screen = SeedTranscribeSeedQRZoomedInScreen(qr_data=qr_data, num_modules=num_modules)
    with TestRenderer._instance.lock:
        screen._render()
    return screen


def run(screen, events):
    """ Feed touch events to the screen's _run(); returns 'exit' if _run() returned """
    pending = iter(events)

    def wait_for(keys=[]):
        for event in pending:
            code = screen.handle_touch(event)
            if code is not None and code in keys:
                return code
        raise Exhausted()

    screen.hw_inputs = MagicMock()
    screen.hw_inputs.wait_for = wait_for
    try:
        screen._run()
        return "exit"
    except Exhausted:
        return None


def drawn_label_box(screen):
    """
    Independent of exit_label_rect: the opaque box drawn into the overlay at the bottom
    centre (alpha 255 and background colour), as (x1, y1, x2, y2), plus the bounding box
    of the label text pixels inside it.
    """
    mask = screen.zone_mask.convert("RGBA")
    w, h = mask.size
    px = mask.load()
    bg = tuple(int(GUIConstants.BACKGROUND_COLOR[i:i + 2], 16) for i in (1, 3, 5)) \
        if isinstance(GUIConstants.BACKGROUND_COLOR, str) else tuple(GUIConstants.BACKGROUND_COLOR[:3])
    box = [(x, y) for y in range(h * 3 // 4, h) for x in range(w // 4, w * 3 // 4)
           if px[x, y][3] == 255 and px[x, y][:3] == bg]
    text = [(x, y) for y in range(h * 3 // 4, h) for x in range(w // 4, w * 3 // 4)
            if px[x, y][3] == 255 and sum(px[x, y][:3]) > 3 * 150]
    def bbox(points):
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (min(xs), min(ys), max(xs), max(ys))
    return bbox(box) if box else None, bbox(text) if text else None, text


def test_label_found():
    screen = new_screen()
    w, h = screen.canvas_width, screen.canvas_height
    box, text_box, text = drawn_label_box(screen)
    check(box is not None and text_box is not None, f"label drawn at the bottom centre: box {box}, text {text_box}")
    if box is None or text_box is None:
        return
    x1, y1, x2, y2 = box
    check(x2 - x1 >= 60 and y2 - y1 >= 12, f"label box has a plausible size ({x2 - x1}x{y2 - y1})")
    check(x1 < text_box[0] and text_box[2] < x2 and y1 < text_box[1], "the text lies inside the box")
    check(x1 > w // 4 and x2 < w * 3 // 4 - 1, "the box is not clipped by the search area")
    check(text_box[1] > h * 3 / 4, "the label lies inside the bottom quarter (the DOWN band of the d-pad)")


def test_every_label_pixel_exits():
    """ A tap on any pixel of the box or the text returns from _run() at once """
    for modules in (21, 25, 29):
        screen = new_screen(modules)
        box, text_box, text = drawn_label_box(screen)
        x1, y1, x2, y2 = box
        missed = [(x, y) for y in range(y1, min(y2, screen.canvas_height - 1) + 1) for x in range(x1, x2 + 1)
                  if screen.handle_touch(tap(x, y)) != K.KEY_PRESS]
        check(not missed, f"{modules}x{modules}: every pixel of the label box {box} is an exit tap ({len(missed)} missed)")
        cx, cy = (text_box[0] + text_box[2]) // 2, (text_box[1] + text_box[3]) // 2
        start = (screen.cur_zone_x, screen.cur_zone_y)
        check(run(screen, [tap(cx, cy)]) == "exit", f"{modules}x{modules}: tap on the text ({cx},{cy}) leaves the screen")
        check((screen.cur_zone_x, screen.cur_zone_y) == start, f"{modules}x{modules}: ... without panning first")


def test_near_misses_exit():
    """ Resistive touch lands a few pixels off: 10 px outside the box, on every side and corner, still exits """
    screen = new_screen()
    box, _, _ = drawn_label_box(screen)
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    for x, y in ((x1 - 10, cy), (x2 + 10, cy), (cx, y1 - 10), (x1 - 10, y1 - 10), (x2 + 10, y1 - 10),
                 (x1 - 10, y2), (x2 + 10, y2)):
        check(screen.handle_touch(tap(x, y)) == K.KEY_PRESS, f"tap 10 px off the box at ({x},{y}) exits")


def test_exit_zone_not_too_big():
    """ Measured from the drawn box, not from EXIT_TOUCH_MARGIN: 16 px outside it is DOWN again """
    screen = new_screen()
    box, _, _ = drawn_label_box(screen)
    x1, y1, x2, y2 = box
    cx = (x1 + x2) // 2
    for x, y in ((x1 - 16, y2), (x2 + 16, y2), (cx, y1 - 16), (x1 - 30, screen.canvas_height - 3),
                 (x2 + 30, screen.canvas_height - 3)):
        check(screen.handle_touch(tap(x, y)) == K.KEY_DOWN, f"tap at ({x},{y}), beyond the label, pans down")
    h = screen.canvas_height
    band = [y for y in range(h * 3 // 4 + 1, h * 3 // 4 + 21) if screen.handle_touch(tap(cx, y)) != K.KEY_DOWN]
    check(not band, f"20 px DOWN band above the label, under its centre ({len(band)} rows not DOWN)")


def test_dpad_still_works():
    """ The rest of the bottom band still pans down; the other edges still pan """
    screen = new_screen()
    box, _, _ = drawn_label_box(screen)
    x1, y1, x2, y2 = box
    w, h = screen.canvas_width, screen.canvas_height
    for x, y, want, name in ((w // 2, h * 3 // 4 + 8, K.KEY_DOWN, "bottom band above the label"),
                             (w // 4 + 10, h - 5, K.KEY_DOWN, "bottom band left of the label"),
                             (w * 3 // 4 - 10, h - 5, K.KEY_DOWN, "bottom band right of the label"),
                             (10, h // 2, K.KEY_LEFT, "left edge"),
                             (w - 10, h // 2, K.KEY_RIGHT, "right edge"),
                             (w // 2, 10, K.KEY_UP, "top edge"),
                             (w // 2, h // 2, K.KEY_PRESS, "centre")):
        check(screen.handle_touch(tap(x, y)) == want, f"{name} ({x},{y}) -> {want}")

    screen = new_screen()
    result = run(screen, [tap(w - 10, h // 2), tap(w // 2, h * 3 // 4 + 8), tap(10, h // 2)])
    check(result is None and (screen.cur_zone_x, screen.cur_zone_y) == (0, 1),
          f"right, down, left pans to zone (0, 1) without exiting: got {(screen.cur_zone_x, screen.cur_zone_y)}")
    cx = (x1 + x2) // 2
    check(run(screen, [tap(cx, (y1 + y2) // 2)]) == "exit", "then a tap on the label exits")


def test_non_taps_ignored():
    screen = new_screen()
    box, _, _ = drawn_label_box(screen)
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    down = TouchEvent(TouchEvent.TYPE__DOWN, cx, cy)
    check(screen.handle_touch(down) is None, "a press (not yet a tap) on the label does nothing")


for name, fn in list(globals().items()):
    if name.startswith("test_") and callable(fn):
        print(f"--- {name}")
        fn()

print(f"\n{len(failures)} failures")
sys.exit(1 if failures else 0)
