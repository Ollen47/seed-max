"""
v7.4 (Ollen, 9 Nov 2026): the splash stays until START and says what is checked; the
word entry shows "WORD N of M" large, with a progress bar.
"""
import inspect
import time

from nonceverify import app, ui
from nonceverify.hw import HEIGHT, WIDTH, Event
from nonceverify.wordentry import WordEntryScreen
from test_nonceverify_ui import SPLASH_START, FakeHardware, Stop   # noqa: F401

import pytest


def center(rect):
    return ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)


def test_start_button_is_where_the_tests_tap():
    assert app.SplashScreen().buttons[0].contains(*SPLASH_START[1:])


def test_splash_waits_for_start_only(monkeypatch):
    clock = [0.0]

    def monotonic():
        clock[0] += 5.0                     # every look at the clock: 5 s later
        return clock[0]
    monkeypatch.setattr(ui.time, "monotonic", monotonic)
    emblem = ("tap", WIDTH // 2, app.SplashScreen.EMBLEM_CY)
    panel = ("tap", *center(app.SplashScreen.PANEL))
    corner = ("tap", 10, 310)
    hw = FakeHardware([("shot", "s0"), emblem, panel, corner, ("shot", "s1"), SPLASH_START, ("shot", "home"),
                       ("stop",)])
    with pytest.raises(Stop):
        app.run(hw)
    assert hw.shots["s1"].tobytes() == hw.shots["s0"].tobytes(), "taps off START and time don't leave the splash"
    assert hw.shots["home"].tobytes() != hw.shots["s0"].tobytes()
    assert "timeout" not in inspect.getsource(app.run).split("SplashScreen().run(")[1].split(")")[0]


def test_splash_layout():
    s = app.SplashScreen()
    image = s.render()
    assert image.size == (WIDTH, HEIGHT)
    assert image.getpixel((center(s.START)[0] - 60, s.START[3] - 6)) == ui.ACCENT, "START is filled teal"
    px1, py1, px2, py2 = s.PANEL
    assert py2 < s.START[1] and s.START[3] < 290, "panel, START, footer don't overlap"
    assert s.background() is s.background(), "drawn once per boot"
    src = inspect.getsource(app)
    assert "signs nothing" not in src and "tap to start" not in src
    for name, _sub in app.SPLASH_FEATURES:
        assert name in ("RFC 6979 nonce", "Sighash ALL", "Txid match", "Address check")


def test_splash_draws_quickly():
    app.SplashScreen._background = None
    started = time.perf_counter()
    app.splash_background()
    assert time.perf_counter() - started < 1.0, "no blur: cheap enough for the Pi Zero"


@pytest.mark.parametrize("count", [12, 24])
def test_word_row_and_bar(count):
    for index in (0, count // 2, count - 1):
        s = WordEntryScreen(index, count)
        segs = s.bar_segments()
        assert len(segs) == count
        assert segs[0][0] >= s.BAR_LEFT and segs[-1][2] <= WIDTH - ui.PAD
        assert all(a[2] < b[0] for a, b in zip(segs, segs[1:])), "segments apart"
        assert all(r[2] - r[0] >= 5 for r in segs), "wide enough to see"
        image = s.render()
        for i, rect in enumerate(segs):
            want = ui.ACCENT if i < index else (ui.TEXT if i == index else ui.LINE)
            assert image.getpixel(center(rect)) == want, (count, index, i)


def test_word_label_fits_before_the_bar():
    f_label, f_num, f_of = ui.font(14, bold=True, mono=True), ui.font(28, bold=True), ui.font(18, bold=True)
    width = ui.PAD + sum(ui.text_width(c, f_label) for c in "WORD") + 3 + 8 + ui.text_width("24", f_num) + 6 \
        + ui.text_width("of 24", f_of)
    assert width < WordEntryScreen.BAR_LEFT - 8


def test_word_screen_regions_do_not_overlap():
    W = WordEntryScreen
    assert ui.HEADER_H < W.LABEL_CY - 14 and W.LABEL_CY + 14 <= W.FIELD[1]
    assert W.FIELD[3] < W.CAND_TOP and W.CAND_TOP + W.CAND_H < W.KEYS_TOP
    last_key = W.key_rect("m")
    assert last_key[3] < W.CONTROLS_TOP
    assert max(r[3] for r in W.control_rects().values()) <= HEIGHT
    assert W.KEY_H >= 42 and W.CAND_H >= 36, "still finger-sized"


def test_words_note_inside_the_field():
    s = WordEntryScreen(0, 12, "a")          # "a": many words, the count shows in the field
    s.render()                                 # no exception, note drawn inside FIELD
    assert s.on_event(Event(Event.TAP, *center(WordEntryScreen.key_rect("b")))) == "__redraw__"
