"""
XPT2046 resistive touch controller driver for the "SeedSigner Max" (v4, v5).

The XPT2046 is a fixed-function 12-bit ADC with a touch-panel front end: it has no
firmware and no memory of its own, it only measures the resistive panel when the Pi
asks. That is why the Max uses a resistive panel instead of a capacitive one, whose
controller is a programmable microcontroller sitting between the finger and the Pi.

Wiring (see hw_profile.py): SPI0 CE1 at 500 kHz, sharing MOSI/SCLK with the display;
PENIRQ on a GPIO, low while the panel is pressed.

Read sequence: pressure first (Z1, Z2), then each axis read several times in a row
instead of alternating X/Y/X/Y. Every switch of the panel drivers (Z -> X, X -> Y) needs
time to settle, all the more with the filter capacitors some boards put on the panel
lines; alternating the axes switched them before every conversion, so one axis could
bleed into the other. Now the drivers switch only twice, and the first conversion
after each switch is discarded:

    Z1, Z2, X (settling, discarded), X, X, X, Y (settling, discarded), Y, Y, Y + power down

Each axis is the median of its three kept readings. The last command powers the ADC
down with PENIRQ enabled again, as before. 16-clock overlapped mode and the decode are
those of Paul Stoffregen's XPT2046_Touchscreen library (MIT license). One SPI
transaction of 21 bytes; at 500 kHz that is ~340 us of bus time, and the slower clock
gives each conversion a longer acquisition (settling) window.

Byte layout (fixed after the first boot on the real board): every command goes out in
an EVEN byte (0, 2, 4, ... 18), i.e. in the low byte of the previous result, exactly as
Stoffregen's `SPI.transfer(0xB1); z1 = SPI.transfer16(0xC1) >> 3; ...` sends it:
`transfer16(0xC1)` clocks out 0x00 first, then 0xC1. The result of the command in byte
2k comes back in bytes 2k+1 (high) and 2k+2 (low). The earlier layout sent each command
one byte too early (bytes 1, 3, 5, ...), in the high byte of the previous result: the
chip still answered in the two bytes right after each command, so every value was read
straddling two results, the low byte of one and the high byte of the next. On the real
panel that gave readings whose low 8 bits were always 0x00-0x0E (the 3 trailing zero
bits of one result followed by the top 4 bits of the next) and whose bits 8-11 were the
4 noise bits of the previous conversion: +-1900 of spread on every press, no geometry.
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

READ_SEQUENCE = [            # command sent in byte:
    CMD_Z1, 0x00,               #  0  (k = 0)
    CMD_Z2, 0x00,               #  2  (k = 1)
    CMD_X, 0x00,                #  4  (k = 2)  X after Z: settling read, discarded
    CMD_X, 0x00,                #  6  (k = 3)
    CMD_X, 0x00,                #  8  (k = 4)
    CMD_X, 0x00,                # 10  (k = 5)
    CMD_Y, 0x00,                # 12  (k = 6)  Y after X: settling read, discarded
    CMD_Y, 0x00,                # 14  (k = 7)
    CMD_Y, 0x00,                # 16  (k = 8)
    CMD_Y_POWER_DOWN, 0x00,     # 18  (k = 9)
    0x00,                       # byte 20 only clocks out the low byte of the last result
]
# 16-clock overlapped mode: the k-th command (k = 0..9) goes out in byte 2k, its result
# comes back in bytes 2k+1 (high) and 2k+2 (low), and the next command goes out in that
# low byte, 2k+2. Each 16-bit word is: 1 busy bit, 12 data bits (MSB first), 3 zero
# bits, hence `>> 3 & 0xFFF`. Byte -> result mapping:
#   bytes  1, 2: Z1        bytes  3, 4: Z2
#   bytes  5, 6: X0 (settling, discarded)   bytes  7, 8: X1   bytes  9, 10: X2   bytes 11, 12: X3
#   bytes 13,14: Y0 (settling, discarded)   bytes 15,16: Y1   bytes 17, 18: Y2   bytes 19, 20: Y3
READ_RESULTS = 10


def decode_results(response: list[int]) -> list[int]:
    """ The 12-bit conversion results packed in a READ_SEQUENCE response, in command order """
    count = (len(response) - 1) // 2
    return [((response[2 * k + 1] << 8) | response[2 * k + 2]) >> 3 & 0x0FFF for k in range(count)]


def median3(a: int, b: int, c: int) -> int:
    """ Median of three readings: one outlier, in either direction, is simply ignored """
    return sorted((a, b, c))[1]


def parse_response(response: list[int], pressure_threshold: int) -> tuple[int, int, int] | None:
    """
    (raw_x, raw_y, pressure) from a READ_SEQUENCE response, or None if the pressure is
    below `pressure_threshold` (no touch, or too light to give a trustworthy position).
    """
    z1, z2, _, x1, x2, x3, _, y1, y2, y3 = decode_results(response)
    if z2 <= z1:
        # On a real contact Z1 is small and Z2 large (XPT2046 datasheet, pressure measurement). Z1 >= Z2 means the converter was not measuring a panel: a floating
        # or clock-coupled MISO (0x55/0xAA patterns give Z1 == Z2) would otherwise read
        # as the strongest possible press.
        return None
    pressure = z1 + 4095 - z2
    if pressure < pressure_threshold:
        return None
    x, y = median3(x1, x2, x3), median3(y1, y2, y3)
    if any(v in (0, 0x0FFF) for v in (z1, z2, x, y)):
        # A stuck MISO line (every bit 0 or every bit 1) would read as the strongest
        # possible press at a corner of the screen. A real contact never saturates the
        # 12-bit converter on these channels (this panel: x ~260-3780, y ~200-3930).
        return None
    return (x, y, pressure)



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
        # The bytes of the last read, for the touch test's raw dump (no seed data here:
        # only ADC readings of the panel)
        self.last_response = None

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
        self.last_response = list(response)
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
