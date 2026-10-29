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

from seedsigner.hardware.touch import (PRESS_DROP_FIRST, PressStats, TouchCalibration, TouchEvent,
    TouchTracker, press_stats, rect_contains)
from seedsigner.hardware.xpt2046 import (CMD_X, CMD_Y, CMD_Y_POWER_DOWN, CMD_Z1, CMD_Z2, READ_RESULTS,
    READ_SEQUENCE, decode_results, median3, parse_response)


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

    def lift(self, n=TouchTracker.RELEASE_MS // 10 + 1):
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


def test_release_needs_time_not_just_samples():
    """ The real loop polls every ~1-2 ms: a few empty samples are not a release """
    tracker = TouchTracker()
    now = 0
    for _ in range(5):
        now += 2
        tracker.feed((100, 100), now)
    for _ in range(10):                             # 20 ms of pressure dip
        now += 2
        assert tracker.feed(None, now) == []
    assert tracker.is_pressed
    events = []
    for _ in range(40):                             # then really lifted
        now += 2
        events += tracker.feed(None, now)
    assert types(events) == [TouchEvent.TYPE__UP] and events[0].is_tap


def test_drag_with_pressure_dips_still_scrolls():
    """
    A stylus sliding up 240 px on a resistive panel, sampled every 2 ms, losing
    contact for 10-20 ms now and then: one drag, five swipe steps (44 px each), no tap.
    """
    tracker = TouchTracker()
    now, events, y = 0, [], 290.0
    for step in range(300):
        now += 2
        y -= 240 / 300
        dip = step % 40 >= 33                       # 14 ms out of every 80 ms
        events += tracker.feed(None if dip else (240, int(y)), now)
    for _ in range(40):
        now += 2
        events += tracker.feed(None, now)
    assert [e.type for e in events].count(TouchEvent.TYPE__DOWN) == 1
    swipes = [e for e in events if e.is_swipe]
    assert len(swipes) == 5 and all(e.direction == TouchEvent.SWIPE__UP for e in swipes)
    assert not any(e.is_tap for e in events)


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


def test_built_in_calibration_maps_the_touch_test_presses_onto_their_targets():
    """ Raw medians of the 29 Oct 2026 touch test, through the calibration in the image """
    from seedsigner.hardware import hw_profile
    cal = TouchCalibration.from_tuple(hw_profile.TOUCH_CALIBRATION)
    presses = {(40, 40): (489, 3462), (440, 40): (3661, 3519), (440, 280): (3634, 737),
               (40, 280): (533, 670), (240, 160): (2121, 2195)}
    for (tx, ty), raw in presses.items():
        x, y = cal.to_screen(*raw, 480, 320)
        assert abs(x - tx) <= 10 and abs(y - ty) <= 10, (raw, (x, y), (tx, ty))


def test_boot_screens_off_in_the_calibrated_image():
    from seedsigner.hardware import hw_profile
    assert hw_profile.TOUCH_CALIBRATE_AT_BOOT is False
    assert hw_profile.TOUCH_RAW_TEST_AT_BOOT is False



# ---------------------------------------------------------------------------------------
# XPT2046 response parsing
# ---------------------------------------------------------------------------------------
def encode_response(values):
    """ Build a 21-byte SPI response carrying these ten 12-bit results """
    response = [0] * len(READ_SEQUENCE)
    for k, value in enumerate(values):
        word = (value & 0x0FFF) << 3
        response[2 * k + 1] = word >> 8
        response[2 * k + 2] = word & 0xFF
    return response


def test_read_sequence_shape():
    """
    Z1, Z2, then X four times and Y four times in a row, ending with the power down. Each
    command in an even byte (0, 2, ... 18), as Stoffregen's transfer16() sends it.
    """
    assert len(READ_SEQUENCE) == 21
    commands = READ_SEQUENCE[0:20:2]
    assert commands == [CMD_Z1, CMD_Z2, CMD_X, CMD_X, CMD_X, CMD_X, CMD_Y, CMD_Y, CMD_Y, CMD_Y_POWER_DOWN]
    assert len(commands) == READ_RESULTS
    assert READ_SEQUENCE[1:21:2] == [0x00] * 10      # the odd bytes (and byte 20) only clock data out
    assert READ_SEQUENCE[20] == 0x00
    assert READ_SEQUENCE[-3] == 0xD0                  # last command powers down, arms PENIRQ


def xpt2046_answer(sequence, values):
    """
    What the chip sends back (16-clock overlapped mode, as seen on the board): the
    result of a command in byte b comes out in bytes b+1 (high) and b+2 (low). A command
    sent before the previous result is fully out takes over the following bytes.
    `values`: the 12-bit result of each command, in order.
    """
    response = [0] * len(sequence)
    commands = [i for i, byte in enumerate(sequence) if byte & 0x80]
    for b, value in zip(commands, values):
        word = (value & 0x0FFF) << 3
        if b + 1 < len(response):
            response[b + 1] = word >> 8
        if b + 2 < len(response):
            response[b + 2] = word & 0xFF
    return response


def test_layout_reads_the_chip_back_correctly():
    """ With every command in an even byte, each result lands where it's decoded """
    values = [800, 3500, 2222, 1000, 1004, 1002, 3333, 2000, 2003, 2001]
    response = xpt2046_answer(READ_SEQUENCE, values)
    assert decode_results(response) == values
    assert parse_response(response, 400) == (1002, 2001, 1395)


def test_old_layout_reproduces_the_board_readings():
    """
    The 0010 layout (Z1 in byte 0, then commands in bytes 1, 3, 5, ...): every decoded
    value straddles two results. Its low 8 bits are the 3 trailing zeros of one result
    and the top 4 bits of the next (so 0x00-0x0F), bits 8-11 the 4 noise bits of the
    first. A press in the middle of the panel (2048 on both axes, noise 0..15) gave
    readings like 0x708, 0x808, 0xC08: low byte 0x08, whatever the noise.
    """
    old = [CMD_Z1, CMD_Z2, 0x00, CMD_X, 0x00, CMD_X, 0x00, CMD_X, 0x00, CMD_X, 0x00,
           CMD_Y, 0x00, CMD_Y, 0x00, CMD_Y, 0x00, CMD_Y_POWER_DOWN, 0x00, 0x00, 0x00]
    for noise in (0, 7, 12, 15):
        middle = 2048 + noise
        response = xpt2046_answer(old, [800, 3500] + [middle] * 8)
        decoded = decode_results(response)
        assert all(v & 0xF0 == 0 for v in decoded[2:9]), [hex(v) for v in decoded]
        assert all(v & 0xFF == 0x08 for v in decoded[2:9])          # top nibble of 2048 = 0b1000
        assert all(v >> 8 == noise for v in decoded[2:9])           # the noise moved to the top
    # Same press decoded with the corrected layout: the real value
    assert parse_response(xpt2046_answer(READ_SEQUENCE, [800, 3500] + [2050] * 8), 400)[:2] == (2050, 2050)


def test_decode_known_bytes():
    """
    Hand-built response: the k-th result sits in bytes 2k+1 (high) and 2k+2 (low) as
    busy bit + 12 data bits + 3 zero bits, i.e. value << 3.
    """
    # 0xABC << 3 = 0x55E0; 0x123 << 3 = 0x0918; 0xFFF << 3 = 0x7FF8; 0x001 << 3 = 0x0008
    response = [0x00,            # byte 0: nothing comes back while Z1 is sent
                0x55, 0xE0,      # Z1 = 0xABC
                0x09, 0x18,      # Z2 = 0x123
                0x7F, 0xF8,      # X0 = 0xFFF (settling read)
                0x00, 0x08,      # X1 = 1
                0x00, 0x10,      # X2 = 2
                0x00, 0x18,      # X3 = 3
                0x7F, 0xF8,      # Y0 = 0xFFF (settling read)
                0x00, 0x20,      # Y1 = 4
                0x00, 0x28,      # Y2 = 5
                0x00, 0x30]      # Y3 = 6
    assert decode_results(response) == [0xABC, 0x123, 0xFFF, 1, 2, 3, 0xFFF, 4, 5, 6]
    # The busy bit (MSB of the high byte) and the three trailing bits are masked off
    noisy = list(response)
    noisy[1] |= 0x80
    noisy[2] |= 0x07
    assert decode_results(noisy)[0] == 0xABC
    # pressure = 0xABC + 4095 - 0x123 = 2748 + 4095 - 291 = 6552
    assert parse_response(response, 400) == (2, 5, 6552)


def test_decode_results_roundtrip():
    values = [10, 4000, 1, 2048, 1024, 4095, 0, 7, 3333, 12]
    assert decode_results(encode_response(values)) == values


def test_median3_drops_the_outlier():
    assert median3(1000, 1004, 3000) == 1004
    assert median3(3000, 1000, 1004) == 1004
    assert median3(1000, 3000, 1004) == 1004
    assert median3(5, 5, 4000) == 5


def test_parse_response_discards_settling_reads():
    # Settling reads far off (an axis still switching): ignored. One outlier per axis too.
    response = encode_response([800, 3500, 100, 1000, 1004, 3900, 4000, 2000, 2002, 10])
    assert parse_response(response, 400) == (1004, 2000, 1395)


def test_parse_response_pressure_threshold():
    # Firm press: z = z1 + 4095 - z2 = 800 + 4095 - 3500 = 1395
    firm = encode_response([800, 3500, 0, 1000, 1002, 1500, 0, 2000, 2004, 2002])
    assert parse_response(firm, 400) == (1002, 2002, 1395)
    # Light contact: z = 100 + 4095 - 3900 = 295
    light = encode_response([100, 3900, 0, 1000, 1000, 1000, 0, 2000, 2000, 2000])
    assert parse_response(light, 400) is None



# ---------------------------------------------------------------------------------------
# Press statistics (touch test log, calibration points)
# ---------------------------------------------------------------------------------------
def test_press_stats_drops_landing_noise():
    landing = [(4000, 10, 300), (3000, 900, 500), (2500, 1500, 700)]
    steady = [(1000, 2000, 1200), (1010, 1990, 1250), (990, 2004, 1180), (1004, 1996, 1300)]
    stats = press_stats(landing + steady)
    assert PRESS_DROP_FIRST == 3
    assert stats == PressStats(x=1004, y=2000, x_half_range=10, y_half_range=7, pressure=1250, samples=4)


def test_press_stats_edge_cases():
    assert press_stats([(1, 2, 3)] * 3) is None                     # nothing left after the drop
    one = press_stats([(0, 0, 0)] * 3 + [(100, 200, 900)])
    assert one == PressStats(100, 200, 0, 0, 900, 1)
    # Odd spread rounds up: any movement shows as at least 1
    assert press_stats([(10, 10, 500), (11, 10, 500)], drop_first=0).x_half_range == 1
    # Floats (e.g. the calibration harness) are rounded to whole ADC units
    assert press_stats([(10.4, 20.6, 500.0)], drop_first=0) == PressStats(10, 21, 0, 0, 500, 1)


def test_press_stats_log_line():
    stats = PressStats(x=3712, y=402, x_half_range=12, y_half_range=8, pressure=1234, samples=47)
    assert stats.log_line(1) == "1  x=3712(\u00b112)  y=402(\u00b18)  z=1234  k=47"
    worst = PressStats(x=4095, y=4095, x_half_range=999, y_half_range=999, pressure=8190, samples=47)
    assert len(worst.log_line(5)) <= 44              # 8 px per character: <= 352 px



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
