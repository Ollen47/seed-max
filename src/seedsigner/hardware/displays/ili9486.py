"""
ILI9486 driver for the Waveshare 3.5inch RPi LCD (A) and its clones (480x320, resistive
touch, plugs straight onto the Raspberry Pi header).

Part of the "SeedSigner Max" modification (v5). These boards do not wire the ILI9486's
own SPI interface: a converter turns the SPI stream into its 16-bit parallel bus. Two
consequences, both handled here as in the Linux driver for the same board
(drivers/gpu/drm/tiny/ili9486.c, "waveshare,rpi-lcd-35"):
- every command and every parameter is a 16-bit word: (0x00, byte), D/C low for the
  command and high for its parameters;
- pixel data is RGB565, which is 16 bits per pixel anyway: big-endian words.
Every transfer is an even number of bytes, so the converter never gets out of step.
A full frame (307200 bytes) is split by `writebytes2()` into chunks of spidev's bufsiz
(131072 bytes on SeedSigner OS: `spidev.bufsiz` in cmdline.txt), even as well; CS goes
high between chunks, which only ends a 16-bit word boundary, and RAMWR keeps going until
the next command. luma.lcd drives the Waveshare 3.5" (B) the same way, in 4096-byte
chunks.

The init sequence (power, VCOM, gamma) and the landscape MADCTL are the Linux driver's.
There is no backlight pin on this board: the backlight is always on.

The touch controller (XPT2046) sits on the same SPI bus on CE1; see `hardware/xpt2046.py`.
"""
import time
from dataclasses import dataclass

import RPi.GPIO as GPIO
import spidev

from seedsigner.hardware import hw_profile
from seedsigner.hardware.displays.display_driver import BaseDisplayDriver
from seedsigner.hardware.displays.st7796 import image_to_rgb565_be


# Command set (ILI9486 datasheet)
SLPOUT = 0x11
INVOFF = 0x20
INVON = 0x21
DISPON = 0x29
CASET = 0x2A
PASET = 0x2B
RAMWR = 0x2C
MADCTL = 0x36
COLMOD = 0x3A
ITFCTR1 = 0xB0        # interface mode control
PWCTRL3 = 0xC2        # power control 3
VMCTRL1 = 0xC5        # VCOM control
PGAMCTRL = 0xE0       # positive gamma
NGAMCTRL = 0xE1       # negative gamma
DGAMCTRL = 0xE2       # digital gamma

CMD_MAX_SPEED_HZ = 10_000_000


def words(values) -> list[int]:
    """ Each byte as a big-endian 16-bit word: (0x00, byte) """
    out = []
    for value in values:
        out += [0x00, value & 0xFF]
    return out



@dataclass
class ILI9486(BaseDisplayDriver):
    def __post_init__(self):
        if (self._width, self._height) != (480, 320):
            raise ValueError("ILI9486 driver only supports 480x320 (landscape)")

        self._dc = hw_profile.ILI9486_PIN_DC
        self._rst = hw_profile.ILI9486_PIN_RST

        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
        GPIO.setup(self._dc, GPIO.OUT)
        GPIO.setup(self._rst, GPIO.OUT)

        self._spi = spidev.SpiDev(0, 0)
        self._spi.max_speed_hz = hw_profile.ILI9486_SPI_SPEED_HZ
        self._spi.mode = 0

        # Commands and parameters at most at 10 MHz, pixel data at the full clock, as the
        # Linux driver does (mipi_dbi_spi_cmd_max_speed): raising ILI9486_SPI_SPEED_HZ
        # can then only affect the pixels, never the init sequence or the window.
        self._cmd_speed_hz = min(CMD_MAX_SPEED_HZ, hw_profile.ILI9486_SPI_SPEED_HZ)

        self.init()


    def command(self, cmd: int, data: list[int] = None):
        GPIO.output(self._dc, GPIO.LOW)
        self._spi.xfer2(words([cmd]), self._cmd_speed_hz)
        if data:
            GPIO.output(self._dc, GPIO.HIGH)
            self._spi.xfer2(words(data), self._cmd_speed_hz)


    def reset(self):
        GPIO.output(self._rst, GPIO.HIGH)
        time.sleep(0.01)
        GPIO.output(self._rst, GPIO.LOW)
        time.sleep(0.01)
        GPIO.output(self._rst, GPIO.HIGH)
        time.sleep(0.12)


    def init(self):
        self.reset()

        self.command(ITFCTR1)
        self.command(SLPOUT)
        time.sleep(0.25)
        self.command(COLMOD, [0x55])                  # 16 bits per pixel
        self.command(PWCTRL3, [0x44])
        self.command(VMCTRL1, [0x00, 0x00, 0x00, 0x00])
        self.command(PGAMCTRL, [0x0F, 0x1F, 0x1C, 0x0C, 0x0F, 0x08, 0x48, 0x98, 0x37, 0x0A, 0x13, 0x04, 0x11, 0x0D, 0x00])
        self.command(NGAMCTRL, [0x0F, 0x32, 0x2E, 0x0B, 0x0D, 0x05, 0x47, 0x75, 0x37, 0x06, 0x10, 0x03, 0x24, 0x20, 0x00])
        self.command(DGAMCTRL, [0x0F, 0x32, 0x2E, 0x0B, 0x0D, 0x05, 0x47, 0x75, 0x37, 0x06, 0x10, 0x03, 0x24, 0x20, 0x00])
        self.command(MADCTL, [hw_profile.ILI9486_MADCTL])
        self.command(INVOFF)
        self.command(DISPON)
        time.sleep(0.1)


    def invert(self, enabled: bool = True):
        """ The "Invert colors" setting (the panel's default is not inverted) """
        self.command(INVON if enabled else INVOFF)


    def _set_window(self, x0: int, y0: int, x1: int, y1: int):
        # x1/y1 are inclusive
        self.command(CASET, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self.command(PASET, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
        self.command(RAMWR)


    def show_image(self, image, x_start: int = 0, y_start: int = 0):
        imwidth, imheight = image.size
        if imwidth != self.width or imheight != self.height:
            raise ValueError(f"Image must be same dimensions as display ({self.width}x{self.height}).")

        pixels = image_to_rgb565_be(image)
        self._set_window(0, 0, self.width - 1, self.height - 1)
        GPIO.output(self._dc, GPIO.HIGH)
        self._spi.writebytes2(pixels)


    def cleanup(self):
        try:
            self._spi.close()
        except Exception:
            pass
