"""
ST7796S driver for 480x320 SPI panels (e.g. LCDwiki MSP4021 4.0" resistive touch
module, Waveshare 3.5" SKU 29318; lcdwiki MSP3520/MSP4030 family).

Part of the "SeedSigner Max" modification. The init sequence is the widely used one
from Bodmer's TFT_eSPI (`TFT_Drivers/ST7796_Init.h`, FreeBSD license). The panel is
natively portrait (320x480); MADCTL puts it in landscape so the driver exposes a
480x320 surface and the Renderer needs no extra rotation.

Pixel path: the same RGB565 big-endian conversion used by SeedSigner's original
ST7789 driver, with a numpy fallback for Pillow versions that dropped "BGR;16".

The touch controller of the MSP4021 (XPT2046) shares this SPI bus on CE1; see
`hardware/xpt2046.py`. The panel's own SDO pin is left unconnected on purpose, so
MISO belongs to the touch controller alone.
"""
import array
import time
from dataclasses import dataclass

import RPi.GPIO as GPIO
import spidev

from seedsigner.hardware import hw_profile
from seedsigner.hardware.displays.display_driver import BaseDisplayDriver


# Command set (ST7796S datasheet)
SWRESET = 0x01
SLPOUT = 0x11
INVOFF = 0x20
INVON = 0x21
DISPON = 0x29
CASET = 0x2A
RASET = 0x2B
RAMWR = 0x2C
MADCTL = 0x36
COLMOD = 0x3A
CSCON = 0xF0


def image_to_rgb565_be(image) -> bytes:
    """
    Convert a PIL RGB image to big-endian RGB565 (R in the high bits), the byte order
    the ST77xx family expects over SPI.
    """
    try:
        # Identical to the original SeedSigner ST7789 driver (Pillow <= 10.x)
        arr = array.array("H", image.convert("BGR;16").tobytes())
        arr.byteswap()
        return arr.tobytes()
    except Exception:
        # Newer Pillow releases removed the "BGR;16" mode
        import numpy as np
        rgb = np.asarray(image.convert("RGB"), dtype=np.uint16)
        r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        return rgb565.astype(">u2").tobytes()



@dataclass
class ST7796(BaseDisplayDriver):
    def __post_init__(self):
        if (self._width, self._height) != (480, 320):
            raise ValueError("ST7796 driver only supports 480x320 (landscape)")

        self._dc = hw_profile.ST7796_PIN_DC
        self._rst = hw_profile.ST7796_PIN_RST
        self._bl = hw_profile.ST7796_PIN_BL
        self._inversion_on_by_default = hw_profile.ST7796_INVERSION_ON

        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
        GPIO.setup(self._dc, GPIO.OUT)
        GPIO.setup(self._rst, GPIO.OUT)
        GPIO.setup(self._bl, GPIO.OUT)

        self._spi = spidev.SpiDev(0, 0)
        self._spi.max_speed_hz = hw_profile.ST7796_SPI_SPEED_HZ
        self._spi.mode = 0

        self.init()
        GPIO.output(self._bl, GPIO.HIGH)


    def command(self, cmd: int, data: bytes | list[int] = None):
        GPIO.output(self._dc, GPIO.LOW)
        self._spi.writebytes([cmd])
        if data:
            GPIO.output(self._dc, GPIO.HIGH)
            self._spi.writebytes2(list(data))


    def reset(self):
        GPIO.output(self._rst, GPIO.HIGH)
        time.sleep(0.01)
        GPIO.output(self._rst, GPIO.LOW)
        time.sleep(0.01)
        GPIO.output(self._rst, GPIO.HIGH)
        time.sleep(0.12)


    def init(self):
        self.reset()

        self.command(SWRESET)
        time.sleep(0.12)
        self.command(SLPOUT)
        time.sleep(0.12)

        # Unlock extended command set
        self.command(CSCON, [0xC3])
        self.command(CSCON, [0x96])

        self.command(MADCTL, [hw_profile.ST7796_MADCTL])
        self.command(COLMOD, [0x55])                  # 16 bits per pixel
        self.command(0xB4, [0x01])                    # 1-dot inversion
        self.command(0xB6, [0x80, 0x02, 0x3B])        # display function control
        self.command(0xE8, [0x40, 0x8A, 0x00, 0x00, 0x29, 0x19, 0xA5, 0x33])
        self.command(0xC1, [0x06])                    # power control 2
        self.command(0xC2, [0xA7])                    # power control 3
        self.command(0xC5, [0x18])                    # VCOM
        time.sleep(0.12)

        # Gamma
        self.command(0xE0, [0xF0, 0x09, 0x0B, 0x06, 0x04, 0x15, 0x2F, 0x54, 0x42, 0x3C, 0x17, 0x14, 0x18, 0x1B])
        self.command(0xE1, [0xE0, 0x09, 0x0B, 0x06, 0x04, 0x03, 0x2B, 0x43, 0x42, 0x3B, 0x16, 0x14, 0x17, 0x1B])
        time.sleep(0.12)

        # Lock extended command set
        self.command(CSCON, [0x3C])
        self.command(CSCON, [0x69])

        self.command(INVON if self._inversion_on_by_default else INVOFF)
        time.sleep(0.12)
        self.command(DISPON)


    def invert(self, enabled: bool = True):
        """
        The "Invert colors" setting flips the panel away from its profile default
        (see `hw_profile.ST7796_INVERSION_ON`).
        """
        inversion_on = self._inversion_on_by_default != enabled
        self.command(INVON if inversion_on else INVOFF)


    def _set_window(self, x0: int, y0: int, x1: int, y1: int):
        # x1/y1 are inclusive
        self.command(CASET, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self.command(RASET, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
        self.command(RAMWR)


    def show_image(self, image, x_start: int = 0, y_start: int = 0):
        imwidth, imheight = image.size
        if imwidth != self.width or imheight != self.height:
            raise ValueError(f"Image must be same dimensions as display ({self.width}x{self.height}).")

        pixels = image_to_rgb565_be(image)
        self._set_window(0, 0, self.width - 1, self.height - 1)
        GPIO.output(self._dc, GPIO.HIGH)
        self._spi.writebytes2(pixels)


    def backlight(self, on: bool):
        """ SeedSigner Max: backlight on/off (the MSP4021 drives it through a transistor) """
        GPIO.output(self._bl, GPIO.HIGH if on else GPIO.LOW)


    def cleanup(self):
        try:
            self._spi.close()
        except Exception:
            pass
