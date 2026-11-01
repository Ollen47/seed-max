"""
SeedSigner Max v5: battery gauge (UPS HAT (C) INA219 over bit-banged I2C), battery
monitor logic, camera RGB frames and the scan screen's frame-rate overlay.

The I2C master is exercised against a simulated open-drain bus with an INA219 slave
that reacts to every SCL/SDA transition, as the real chip would.
"""
from types import SimpleNamespace

import pytest
from PIL import Image

from seedsigner.hardware import hw_profile
from seedsigner.hardware.ina219 import (REG_BUS_VOLTAGE, REG_SHUNT_VOLTAGE, BatteryReading,
    INA219Gauge, SoftI2C, decode_bus_voltage, decode_shunt_mv, estimate_percent)


# ---------------------------------------------------------------------------------------
# Simulated open-drain bus with an INA219 slave
# ---------------------------------------------------------------------------------------
class FakeINA219:
    """ I2C slave state machine, clocked by the bus' line changes """
    def __init__(self, address=0x43, registers=None):
        self.address = address
        self.registers = dict(registers or {})
        self.pointer = 0
        self.sda_out = 1             # what the slave drives (1 = released)
        self.reset()
        self.writes = []             # (register pointer, data bytes) of every write transaction
        self.read_hook = None        # optional: register -> value override per read

    def reset(self):
        self.state = "idle"
        self.bits = 0
        self.nbits = 0
        self.byte_index = 0
        self.is_read = False
        self.selected = False
        self.tx = []
        self.data_bytes = []
        self.sda_out = 1

    def on_start(self):
        self.reset()
        self.state = "addr"

    def on_stop(self):
        if self.selected and not self.is_read and self.data_bytes:
            self.writes.append((self.pointer, list(self.data_bytes)))
        self.reset()

    def on_scl_rise(self, sda):
        if self.state in ("addr", "write"):
            self.bits = (self.bits << 1) | sda
            self.nbits += 1
        elif self.state == "read_ack":
            # Master's ACK (0) or NACK (1) after a byte we sent
            self.state = "read" if sda == 0 else "idle_wait"

    def on_scl_fall(self):
        if self.state in ("addr", "write") and self.nbits == 8:
            byte = self.bits
            self.bits = self.nbits = 0
            if self.state == "addr":
                if (byte >> 1) == self.address:
                    self.selected = True
                    self.is_read = bool(byte & 1)
                    self.sda_out = 0                        # ACK
                    self.state = "ack_then_read" if self.is_read else "ack_then_write"
                else:
                    self.state = "idle_wait"
                return
            # data byte from the master
            if self.byte_index == 0:
                self.pointer = byte
            else:
                self.data_bytes.append(byte)
            self.byte_index += 1
            self.sda_out = 0
            self.state = "ack_then_write"
            return

        if self.state == "ack_then_write":
            self.sda_out = 1
            self.state = "write"
            return

        if self.state in ("ack_then_read", "read"):
            if self.state == "ack_then_read":
                value = self.registers.get(self.pointer, 0)
                if self.read_hook:
                    value = self.read_hook(self.pointer, value)
                self.tx = [(value >> 8) & 0xFF, value & 0xFF]
                self.tx_bits = [(b >> (7 - i)) & 1 for b in self.tx for i in range(8)]
                self.tx_pos = 0
                self.state = "read"
            if self.tx_pos < len(self.tx_bits):
                self.sda_out = self.tx_bits[self.tx_pos]
                self.tx_pos += 1
                if self.tx_pos % 8 == 0:
                    self.state = "read_last_bit"
            return

        if self.state == "read_last_bit":
            # Release SDA for the master's ACK/NACK bit
            self.sda_out = 1
            self.state = "read_ack"
            return

        if self.state == "idle_wait":
            self.sda_out = 1


class FakeBusGPIO:
    """ RPi.GPIO stand-in: two open-drain lines with pull-ups, shared with the slave """
    IN, OUT, LOW, HIGH = "in", "out", 0, 1

    def __init__(self, slave, sda=3, scl=5):
        self.slave = slave
        self.sda_pin, self.scl_pin = sda, scl
        self.master = {sda: 1, scl: 1}           # 1 = released
        self.prev = (1, 1)

    def line(self, pin):
        level = self.master[pin]
        if pin == self.sda_pin and self.slave is not None:
            level &= self.slave.sda_out
        return level

    def _changed(self):
        scl, sda = self.line(self.scl_pin), self.line(self.sda_pin)
        pscl, psda = self.prev
        self.prev = (scl, sda)
        if self.slave is None:
            return
        if scl and pscl and psda and not sda:
            self.slave.on_start()
        elif scl and pscl and not psda and sda:
            self.slave.on_stop()
        elif scl and not pscl:
            self.slave.on_scl_rise(sda)
        elif not scl and pscl:
            self.slave.on_scl_fall()
        self.prev = (self.line(self.scl_pin), self.line(self.sda_pin))

    def setup(self, pin, mode, initial=None, pull_up_down=None):
        self.master[pin] = 0 if mode == self.OUT and initial == self.LOW else 1
        self._changed()

    def input(self, pin):
        return self.line(pin)


def bus_with(slave):
    gpio = FakeBusGPIO(slave)
    return SoftI2C(gpio, 3, 5, half_period_s=0)


def raw_bus(volts):
    return (int(round(volts / 0.004)) << 3) | 0b10       # CNVR set, as the chip does


def raw_shunt(current_ma, shunt_ohm=0.1):
    raw = int(round(current_ma / 1000 * shunt_ohm / 10e-6))
    return raw & 0xFFFF



# ---------------------------------------------------------------------------------------
# Decoding and estimate
# ---------------------------------------------------------------------------------------
def test_decode_registers():
    assert decode_bus_voltage(raw_bus(3.848)) == pytest.approx(3.848, abs=0.002)
    assert decode_bus_voltage(0xFFFF) == pytest.approx(8191 * 0.004)
    assert decode_shunt_mv(raw_shunt(-400)) == pytest.approx(-40.0)
    assert decode_shunt_mv(raw_shunt(250)) == pytest.approx(25.0)
    assert decode_shunt_mv(0x8000) == pytest.approx(-327.68)


def test_estimate_percent():
    assert estimate_percent(4.20, 0) == 100
    assert estimate_percent(3.25, 0) == 0
    assert estimate_percent(3.77, 0) == 45
    mid = estimate_percent(3.795, 0)
    assert 45 < mid < 55
    # Under load the cell sags: 3.70 V at -600 mA is ~3.79 V resting
    assert estimate_percent(3.70, -600, internal_ohm=0.15) == estimate_percent(3.79, 0, internal_ohm=0.15)
    # Monotonic
    values = [estimate_percent(3.2 + i * 0.01, 0) for i in range(110)]
    assert values == sorted(values)


def test_external_power_flag():
    assert not BatteryReading(3.8, -450.0, 50).on_external_power
    assert BatteryReading(4.1, 380.0, 95).on_external_power          # charging
    assert BatteryReading(4.18, -5.0, 100).on_external_power         # full, on the cable



# ---------------------------------------------------------------------------------------
# Bit-banged I2C against the simulated chip
# ---------------------------------------------------------------------------------------
def test_soft_i2c_probe_and_read():
    chip = FakeINA219(address=0x43, registers={REG_BUS_VOLTAGE: 0x1E12, REG_SHUNT_VOLTAGE: 0xF060})
    bus = bus_with(chip)
    assert bus.probe(0x43)
    assert not bus.probe(0x42)
    assert bus.read_register16(0x43, REG_BUS_VOLTAGE) == 0x1E12
    assert bus.read_register16(0x43, REG_SHUNT_VOLTAGE) == 0xF060
    assert bus.read_register16(0x43, REG_BUS_VOLTAGE) == 0x1E12      # pointer handling repeatable
    # Only register pointers were ever sent: no data written to the chip
    assert chip.writes == []


def test_gauge_reads_the_cell_without_writing_to_the_chip():
    chip = FakeINA219(address=0x43, registers={
        REG_BUS_VOLTAGE: raw_bus(3.848),
        REG_SHUNT_VOLTAGE: raw_shunt(-400),
        0x00: 0x399F, 0x05: 0x0000,          # power-on config, empty calibration
    })
    gauge = INA219Gauge(bus=bus_with(chip))
    reading = gauge.read()
    assert gauge.address == 0x43
    assert reading.voltage == pytest.approx(3.848, abs=0.002)
    assert reading.current_ma == pytest.approx(-400.0, abs=0.2)
    assert reading.percent == estimate_percent(3.848, -400)
    assert not reading.on_external_power
    assert chip.writes == [], "read-only: config and calibration registers never written"
    assert chip.registers[0x00] == 0x399F and chip.registers[0x05] == 0x0000


def test_gauge_finds_the_other_addresses():
    chip = FakeINA219(address=0x40, registers={REG_BUS_VOLTAGE: raw_bus(4.0), REG_SHUNT_VOLTAGE: raw_shunt(-300)})
    gauge = INA219Gauge(bus=bus_with(chip))
    assert gauge.find() == 0x40
    assert gauge.read().voltage == pytest.approx(4.0, abs=0.002)


def test_gauge_drops_bad_readings():
    # No chip on the bus
    assert INA219Gauge(bus=bus_with(None)).read() is None

    # Two reads that disagree (a glitched transfer): dropped
    chip = FakeINA219(registers={REG_BUS_VOLTAGE: raw_bus(3.9), REG_SHUNT_VOLTAGE: raw_shunt(-400)})
    flip = {"n": 0}
    def unstable(register, value):
        if register == REG_BUS_VOLTAGE:
            flip["n"] += 1
            return value if flip["n"] % 2 else raw_bus(3.2)
        return value
    chip.read_hook = unstable
    assert INA219Gauge(bus=bus_with(chip)).read() is None

    # Not a LiPo cell voltage: dropped
    chip = FakeINA219(registers={REG_BUS_VOLTAGE: raw_bus(5.1), REG_SHUNT_VOLTAGE: raw_shunt(0)})
    assert INA219Gauge(bus=bus_with(chip)).read() is None



# ---------------------------------------------------------------------------------------
# Battery monitor logic
# ---------------------------------------------------------------------------------------
def monitor():
    from seedsigner.hardware.power import BatteryMonitor
    m = BatteryMonitor.__new__(BatteryMonitor)
    m.recent, m.reading, m.reading_time, m.is_low, m.gauge = [], None, 0, False, None
    return m


def test_monitor_median_and_staleness():
    import time
    m = monitor()
    assert m.current is None
    m.add_reading(BatteryReading(3.90, -400, 70))
    m.add_reading(BatteryReading(3.20, -400, 0))          # one bad sag
    m.add_reading(BatteryReading(3.88, -400, 68))
    assert m.current.voltage == 3.88, "the outlier doesn't move the median"
    m.add_reading(None)
    assert m.current.voltage == 3.88
    m.reading_time = time.time() - m.STALE_AFTER_S - 1
    assert m.current is None, "no fresh reading: no icon"


def test_monitor_warnings():
    import time
    m = monitor()
    now = time.time()

    def set_reading(percent, current=-400):
        m.recent = []
        m.add_reading(BatteryReading(3.7, current, percent), now=now)

    set_reading(60)
    assert m.warning_due(now, None, None) == (None, None)

    set_reading(hw_profile.BATTERY_LOW_PERCENT)
    text, level = m.warning_due(now, None, None)
    assert level == "low" and "Battery low" in text and f"{hw_profile.BATTERY_LOW_PERCENT}%" in text
    assert m.warning_due(now + 10, now, "low") == (None, "low"), "not again within 5 minutes"
    assert m.warning_due(now + m.REMIND_EVERY_S + 1, now, "low")[0] is not None

    set_reading(hw_profile.BATTERY_CRITICAL_PERCENT)
    text, level = m.warning_due(now + 10, now, "low")
    assert level == "critical" and text is not None, "dropping to critical warns at once"
    assert m.warning_due(now + 20, now + 10, "critical")[0] is None
    assert m.warning_due(now + 10 + m.CRITICAL_REMIND_S + 1, now + 10, "critical")[0] is not None

    set_reading(3, current=200)
    assert m.warning_due(now, None, None) == (None, None), "no warnings on the USB cable"


def test_monitor_starts_only_with_a_gauge_or_carrier(monkeypatch):
    from seedsigner.hardware import power
    started = []
    m = monitor()
    m.start = lambda: started.append(True)
    monkeypatch.setattr(power, "_gpio", lambda: object())
    monkeypatch.setattr(hw_profile, "BATTERY_GAUGE_INA219", False)
    power.BatteryMonitor.start_monitoring(m)
    assert started == []
    monkeypatch.setattr(hw_profile, "BATTERY_GAUGE_INA219", True)
    assert hw_profile.has_battery_gauge() and not hw_profile.has_carrier()
    power.BatteryMonitor.start_monitoring(m)
    power.BatteryMonitor.start_monitoring(m)
    assert started == [True], "started once, however many times the Controller asks"


def test_v5_profile_reads_the_battery():
    """ The image ships with the gauge on (tests turn it off in tests/base.py) """
    import re, pathlib
    source = pathlib.Path(hw_profile.__file__).read_text()
    assert re.search(r"^BATTERY_GAUGE_INA219 = True$", source, re.M)
    assert hw_profile.BATTERY_I2C_SDA_PIN == 3 and hw_profile.BATTERY_I2C_SCL_PIN == 5
    assert hw_profile.BATTERY_INA219_ADDRESSES[0] == 0x43
    # The display and touch never use pins 3 and 5
    assert {3, 5}.isdisjoint({hw_profile.ILI9486_PIN_DC, hw_profile.ILI9486_PIN_RST, hw_profile.TOUCH_PIN_IRQ, 19, 21, 23, 24, 26})



# ---------------------------------------------------------------------------------------
# Camera frames and the frame-rate overlay
# ---------------------------------------------------------------------------------------
class FakeFrame(bytes):
    """ A tiny 8x8 RGB frame with the array interface PIL reads (numpy is mocked in the suite) """
    @property
    def __array_interface__(self):
        return {"shape": (8, 8, 3), "typestr": "|u1", "data": bytes(self), "version": 3}

    def astype(self, dtype):
        return self


def test_camera_rgb_frames_match_the_rgba_path():
    from seedsigner.hardware.camera import Camera
    pixels = bytearray(8 * 8 * 3)
    for x in range(8):
        pixels[x * 3] = 255                     # red top row
    for y in range(8):
        pixels[y * 8 * 3 + 2] = 200             # blue left column
    frame = FakeFrame(pixels)
    camera = Camera.__new__(Camera)
    camera._camera_rotation = 180
    camera._video_stream = SimpleNamespace(read=lambda: frame)
    rgba = camera.read_video_stream(as_image=True)
    rgb = camera.read_video_stream(as_image=True, rgb=True)
    assert rgba.mode == "RGBA" and rgb.mode == "RGB"
    assert rgba.convert("RGB").tobytes() == rgb.tobytes(), "same pixels, same rotation"
    assert camera.read_video_stream() is frame


def test_fps_overlay_text():
    import sys
    from unittest.mock import MagicMock
    sys.modules.setdefault("RPi", MagicMock())
    sys.modules.setdefault("RPi.GPIO", MagicMock())
    from seedsigner.gui.screens.scan_screens import ScanScreen
    text = ScanScreen.LivePreviewThread.fps_text(5.04, "3.20", 320)
    assert text == "preview  5.0 fps  decode 3.20 fps  320px"


def test_tuning_settings():
    from seedsigner.models.settings_definition import SettingsConstants, SettingsDefinition
    res = SettingsDefinition.get_settings_entry(SettingsConstants.SETTING__MAX_CAMERA_RESOLUTION)
    spi = SettingsDefinition.get_settings_entry(SettingsConstants.SETTING__MAX_DISPLAY_SPI_MHZ)
    assert [v for v, _ in res.selection_options] == [480, 400, 320] and res.default_value == hw_profile.CAMERA_RESOLUTION
    assert [v for v, _ in spi.selection_options] == [16, 24, 32] and spi.default_value == 24
    assert res.visibility == SettingsConstants.VISIBILITY__HARDWARE == spi.visibility


def test_final_camera_and_display_values():
    """ Values measured on the device (1 Nov 2026) """
    from seedsigner.models.settings_definition import SettingsConstants, SettingsDefinition
    assert hw_profile.CAMERA_RESOLUTION == 320
    assert hw_profile.ILI9486_SPI_SPEED_HZ == 24_000_000
    assert hw_profile.CAMERA_FPS_OVERLAY is False
    rotation = SettingsDefinition.get_settings_entry(SettingsConstants.SETTING__CAMERA_ROTATION)
    assert rotation.default_value == 90 == hw_profile.CAMERA_ROTATION
    assert rotation.default_value in [v for v, _ in rotation.selection_options]
