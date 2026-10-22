"""
XPT2046 resistive touch controller driver for the "SeedSigner Max" (v4).

The XPT2046 is a fixed-function 12-bit ADC with a touch-panel front end: it has no
firmware and no memory of its own, it only measures the resistive panel when the Pi
asks. That is why the Max uses a resistive panel instead of a capacitive one, whose
controller is a programmable microcontroller sitting between the finger and the Pi.

Wiring (see hw_profile.py): SPI0 CE1 at 1 MHz, sharing MOSI/SCLK with the display;
PENIRQ on a GPIO, low while the panel is pressed.

Read sequence and filtering follow Paul Stoffregen's widely used XPT2046_Touchscreen
library (MIT license): pressure first, then three X/Y pairs, keep the average of the
two closest readings of each axis, finish with a power-down command so PENIRQ is armed
again. One SPI transaction of 19 bytes; at 1 MHz that is ~150 us of bus time.
"""
import logging

from seedsigner.hardware import hw_profile
from seedsigner.hardware.touch import TouchCalibration

logger = logging.getLogger(__name__)


# Control byte: S | A2 A1 A0 | MODE (0 = 12 bit) | SER/DFR (0 = differential) | PD1 PD0
CMD_Z1 = 0xB1           # A = 011, ADC on between conversions, PENIRQ disabled
CMD_Z2 = 0xC1           # A = 100
CMD_X = 0x91            # A = 001
CMD_Y = 0xD1            # A = 101
CMD_Y_POWER_DOWN = 0xD0 # same conversion, then power down with PENIRQ enabled

READ_SEQUENCE = [
    CMD_Z1,
    CMD_Z2, 0x00,
    CMD_X, 0x00,        # first X after the pressure readings settles the input: discarded
    CMD_X, 0x00,
    CMD_Y, 0x00,
    CMD_X, 0x00,
    CMD_Y, 0x00,
    CMD_X, 0x00,
    CMD_Y_POWER_DOWN, 0x00,
    0x00, 0x00,
]
# The result of the command sent at byte 2k-1 (or byte 0) arrives in bytes 2k+1, 2k+2
# (16-clock overlapped mode): 9 results, in the order Z1, Z2, dummy, X1, Y1, X2, Y2, X3, Y3.


def decode_results(response: list[int]) -> list[int]:
    """ The nine 12-bit conversion results packed in an 19-byte response """
    return [((response[2 * k + 1] << 8) | response[2 * k + 2]) >> 3 & 0x0FFF for k in range(9)]


def best_two_average(a: int, b: int, c: int) -> int:
    """ Average of the two closest of three readings: one outlier is simply ignored """
    da, db, dc = abs(a - b), abs(a - c), abs(b - c)
    if da <= db and da <= dc:
        return (a + b) // 2
    if db <= da and db <= dc:
        return (a + c) // 2
    return (b + c) // 2


def parse_response(response: list[int], pressure_threshold: int) -> tuple[int, int, int] | None:
    """
    (raw_x, raw_y, pressure) from a READ_SEQUENCE response, or None if the pressure is
    below `pressure_threshold` (no touch, or too light to give a trustworthy position).
    """
    z1, z2, _, x1, y1, x2, y2, x3, y3 = decode_results(response)
    pressure = z1 + 4095 - z2
    if pressure < pressure_threshold:
        return None
    return (best_two_average(x1, x2, x3), best_two_average(y1, y2, y3), pressure)



class XPT2046:
    def __init__(self, calibration: TouchCalibration = None, width: int = 480, height: int = 320):
        import RPi.GPIO as GPIO
        import spidev
        self.GPIO = GPIO
        self.irq_pin = hw_profile.TOUCH_PIN_IRQ
        self.pressure_threshold = hw_profile.TOUCH_PRESSURE_THRESHOLD
        self.width = width
        self.height = height
        self.calibration = calibration or TouchCalibration.from_tuple(hw_profile.TOUCH_CALIBRATION)

        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
        GPIO.setup(self.irq_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        self.spi = spidev.SpiDev(hw_profile.TOUCH_SPI_BUS, hw_profile.TOUCH_SPI_DEVICE)
        self.spi.max_speed_hz = hw_profile.TOUCH_SPI_SPEED_HZ
        self.spi.mode = 0

        # Power down with PENIRQ enabled, so `is_touched()` works from the start
        self.spi.xfer2([CMD_Y_POWER_DOWN, 0x00, 0x00])


    def is_touched(self) -> bool:
        """ Cheap check on the PENIRQ line: no SPI traffic while nobody touches """
        return self.GPIO.input(self.irq_pin) == self.GPIO.LOW


    def read_raw(self) -> tuple[int, int, int] | None:
        """ (raw_x, raw_y, pressure) or None """
        if not self.is_touched():
            return None
        response = self.spi.xfer2(list(READ_SEQUENCE))
        return parse_response(response, self.pressure_threshold)


    def read(self) -> tuple[int, int] | None:
        """ Calibrated screen position of the finger, or None """
        raw = self.read_raw()
        if raw is None:
            return None
        return self.calibration.to_screen(raw[0], raw[1], self.width, self.height)


    def close(self):
        try:
            self.spi.close()
        except Exception:
            pass
