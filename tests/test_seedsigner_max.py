"""
SeedSigner Max (v4): unit tests for the touch logic (debounce, taps, drags, swipes),
the calibration math and the XPT2046 response parsing, plus an isolated-interpreter run
of tests/seedsigner_max_harness.py that drives the real HardwareButtons and the real
screens with scripted, real-time touches on a 480x320 canvas.
"""
import os
import subprocess
import sys

import pytest

from seedsigner.hardware.touch import TouchCalibration, TouchEvent, TouchTracker, rect_contains
from seedsigner.hardware.xpt2046 import (READ_SEQUENCE, best_two_average, decode_results,
    parse_response)


class Finger:
    """ Drives the tracker with samples 10 ms apart """
    def __init__(self):
        self.tracker = TouchTracker()
        self.now = 0

    def samples(self, *points):
        events = []
        for point in points:
            self.now += 10
            events += self.tracker.feed(point, self.now)
        return events

    def hold(self, point, n=5):
        return self.samples(*([point] * n))

    def lift(self, n=TouchTracker.RELEASE_SAMPLES):
        return self.samples(*([None] * n))


def types(events):
    return [e.type for e in events]



# ---------------------------------------------------------------------------------------
# TouchTracker
# ---------------------------------------------------------------------------------------
def test_tap():
    f = Finger()
    events = f.hold((100, 200))
    assert types(events) == [TouchEvent.TYPE__DOWN]
    assert (events[0].x, events[0].y) == (100, 200)
    events = f.lift()
    assert types(events) == [TouchEvent.TYPE__UP]
    assert events[0].is_tap and events[0].duration_ms > 0
    assert not f.tracker.is_pressed


def test_contact_needs_consecutive_samples():
    f = Finger()
    assert f.samples((10, 10)) == []               # one sample: not yet a press
    assert f.samples(None) == []                   # a blip is forgotten entirely
    assert f.samples((300, 300)) == []
    events = f.samples((300, 300))
    assert types(events) == [TouchEvent.TYPE__DOWN] and (events[0].x, events[0].y) == (300, 300)


def test_release_is_debounced():
    """ Resistive panels drop out for a sample or two at low pressure """
    f = Finger()
    f.hold((50, 50))
    assert f.samples(None, None, (50, 50), None, None, (51, 50)) == []   # never 3 empty in a row
    assert f.tracker.is_pressed
    assert types(f.lift()) == [TouchEvent.TYPE__UP]


def test_median_filter_rejects_a_wild_sample():
    f = Finger()
    f.hold((200, 150))
    f.samples((460, 10))                            # one garbage reading
    assert f.tracker.position == (200, 150)
    events = f.lift()
    assert events[0].is_tap                         # the outlier didn't turn it into a drag


def test_tap_position_is_where_the_finger_landed():
    f = Finger()
    f.hold((120, 80))
    f.samples((125, 84), (127, 86))                 # small drift within the slop
    events = f.lift()
    assert events[0].is_tap and (events[0].x, events[0].y) == (120, 80)


def test_sliding_away_cancels_the_tap():
    f = Finger()
    f.hold((100, 100))
    f.hold((100 + TouchTracker.TAP_SLOP_PX + 5, 100))
    events = f.lift()
    assert types(events) == [TouchEvent.TYPE__UP] and not events[0].is_tap
    # and sliding back doesn't make it a tap again
    f.hold((100, 100))
    f.hold((160, 100))
    f.hold((100, 100))
    assert not f.lift()[0].is_tap


def test_vertical_drag_emits_swipe_steps():
    f = Finger()
    f.hold((240, 280))
    events = []
    for y in range(280, 130, -10):                  # finger moves up to y=140: 140 px
        events += f.hold((240, y), n=3)
    swipes = [e for e in events if e.is_swipe]
    assert len(swipes) == 140 // TouchTracker.SWIPE_STEP_PX
    assert all(e.direction == TouchEvent.SWIPE__UP for e in swipes)
    events = []
    for y in range(140, 240, 10):                   # and back down 100 px
        events += f.hold((240, y), n=3)
    swipes = [e for e in events if e.is_swipe]
    assert swipes and all(e.direction == TouchEvent.SWIPE__DOWN for e in swipes)
    assert not f.lift()[0].is_tap


def test_rect_contains():
    assert rect_contains((10, 10, 20, 20), 10, 20)
    assert not rect_contains((10, 10, 20, 20), 21, 15)
    assert rect_contains((10, 10, 20, 20), 23, 15, margin=3)



# ---------------------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------------------
def test_calibration_solves_swapped_and_mirrored_axes():
    # A panel whose raw X runs along the screen's vertical axis, mirrored
    truth = TouchCalibration(0.0, 0.13, -26.0, -0.087, 0.0, 345.0)
    raw = [(300, 400), (3800, 350), (3700, 3900), (250, 3850), (2000, 2100)]
    screen = [(truth.a * rx + truth.b * ry + truth.c, truth.d * rx + truth.e * ry + truth.f) for rx, ry in raw]
    fitted = TouchCalibration.solve(raw, screen)
    assert fitted.max_error(raw, screen) < 0.01
    for got, want in zip(fitted.as_tuple(), truth.as_tuple()):
        assert abs(got - want) < 1e-6


def test_calibration_least_squares_with_noise():
    truth = TouchCalibration(0.128, 0.002, -25.0, -0.001, 0.087, -18.0)
    raw = [(300, 300), (3800, 320), (3790, 3850), (310, 3820)]
    noise = [(1.5, -1.0), (-1.0, 2.0), (0.5, -1.5), (-2.0, 1.0)]
    screen = [(truth.a * rx + truth.b * ry + truth.c + nx, truth.d * rx + truth.e * ry + truth.f + ny)
              for (rx, ry), (nx, ny) in zip(raw, noise)]
    fitted = TouchCalibration.solve(raw, screen)
    assert fitted.max_error(raw, screen) < 3.0


def test_calibration_rejects_degenerate_points():
    with pytest.raises(ValueError):
        TouchCalibration.solve([(100, 100), (200, 200), (300, 300)], [(0, 0), (10, 10), (20, 20)])
    with pytest.raises(ValueError):
        TouchCalibration.solve([(100, 100), (200, 200)], [(0, 0), (10, 10)])


def test_to_screen_clamps_to_the_display():
    cal = TouchCalibration(1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    assert cal.to_screen(-50, 999, 480, 320) == (0, 319)
    assert cal.to_screen(123.4, 56.6, 480, 320) == (123, 57)


def test_default_calibration_maps_the_raw_range_onto_the_screen():
    from seedsigner.hardware import hw_profile
    cal = TouchCalibration.from_tuple(hw_profile.TOUCH_CALIBRATION)
    assert cal.to_screen(200, 200, 480, 320) == (0, 0)
    assert cal.to_screen(3900, 3900, 480, 320) == (479, 319)



# ---------------------------------------------------------------------------------------
# XPT2046 response parsing
# ---------------------------------------------------------------------------------------
def encode_response(values):
    """ Build a 19-byte SPI response carrying these nine 12-bit results """
    response = [0] * len(READ_SEQUENCE)
    for k, value in enumerate(values):
        word = (value & 0x0FFF) << 3
        response[2 * k + 1] = word >> 8
        response[2 * k + 2] = word & 0xFF
    return response


def test_read_sequence_shape():
    assert len(READ_SEQUENCE) == 19
    assert READ_SEQUENCE[-4] == 0xD0                  # last command powers down, arms PENIRQ


def test_decode_results_roundtrip():
    values = [10, 4000, 1, 2048, 1024, 4095, 0, 7, 3333]
    assert decode_results(encode_response(values)) == values


def test_best_two_average_drops_the_outlier():
    assert best_two_average(1000, 1004, 3000) == 1002
    assert best_two_average(3000, 1000, 1004) == 1002
    assert best_two_average(1000, 3000, 1004) == 1002


def test_parse_response_pressure_threshold():
    # Firm press: z = z1 + 4095 - z2 = 800 + 4095 - 3500 = 1395
    firm = encode_response([800, 3500, 0, 1000, 2000, 1002, 2004, 1500, 2002])
    assert parse_response(firm, 400) == (1001, 2001, 1395)   # ties pick either close pair
    # Light contact: z = 100 + 4095 - 3900 = 295
    light = encode_response([100, 3900, 0, 1000, 2000, 1000, 2000, 1000, 2000])
    assert parse_response(light, 400) is None



# ---------------------------------------------------------------------------------------
# Real screens, real HardwareButtons
# ---------------------------------------------------------------------------------------
def test_touch_screens_in_isolation(tmp_path):
    """
    Runs tests/seedsigner_max_harness.py in a clean interpreter: the rest of the test
    suite replaces the Renderer, numpy and HardwareButtons with MagicMocks globally.
    """
    harness = os.path.join(os.path.dirname(__file__), "seedsigner_max_harness.py")
    result = subprocess.run(
        [sys.executable, harness, str(tmp_path)],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[FAIL]" not in result.stdout
    assert (tmp_path / "max_menu_hold_progress.png").exists()



# ---------------------------------------------------------------------------------------
# v5: Waveshare 3.5inch RPi LCD (A)
# ---------------------------------------------------------------------------------------
def test_ili9486_words():
    """ Every byte becomes a 16-bit word for the board's SPI-to-parallel converter """
    import sys
    from unittest.mock import MagicMock
    sys.modules.setdefault("RPi", MagicMock())
    sys.modules.setdefault("RPi.GPIO", MagicMock())
    sys.modules.setdefault("spidev", MagicMock())
    from seedsigner.hardware.displays.ili9486 import words
    assert words([0x2A]) == [0x00, 0x2A]
    assert words([0x01, 0xDF, 0x1FF]) == [0x00, 0x01, 0x00, 0xDF, 0x00, 0xFF]


def test_v5_profile():
    from seedsigner.hardware import hw_profile
    assert hw_profile.is_max_profile()
    assert hw_profile.DISPLAY_CONFIGURATION == "ili9486_480x320"
    assert not hw_profile.has_carrier()
    # Display and touch pins of the Waveshare 3.5" (A) don't collide
    assert {hw_profile.ILI9486_PIN_DC, hw_profile.ILI9486_PIN_RST}.isdisjoint({hw_profile.TOUCH_PIN_IRQ, 19, 21, 23, 24, 26})
