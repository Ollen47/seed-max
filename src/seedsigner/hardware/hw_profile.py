"""
Build-time hardware profile for the "SeedSigner Max" modification (v5).

SeedSigner is stateless: unless Persistent Settings are enabled on a microSD card,
every boot starts from the default settings. A board that does not use the original
Waveshare 1.3" hat would therefore boot into an unusable screen every time.

The fix is to bake the hardware defaults into the image itself. Everything that is
specific to the custom hardware lives in this one file so that it is easy to audit
and easy to change before building your own image.

    ACTIVE_PROFILE = PROFILE__STANDARD  -> stock SeedSigner behavior (Waveshare 1.3")
    ACTIVE_PROFILE = PROFILE__MAX       -> 480x320 panel + XPT2046 resistive touch

SeedSigner Max v5 hardware (no custom PCB, no soldering):
- Raspberry Pi Zero v1.3 with a solderless (hammer) header
- Waveshare 3.5inch RPi LCD (A): 480x320 ILI9486 behind an SPI-to-16-bit-parallel
  converter, XPT2046 resistive touch. It plugs straight onto header pins 1-26.
- PiSugar 3 Air under the Pi (pogo pins: no GPIO used) with a thin LiPo cell beside it.
  Its button switches the device on and off; SeedSigner does not talk to it.
No joystick, no KEY1-3: the touch screen is the only input.

The v4 hardware (LCDwiki MSP4021 4.0" ST7796S on the carrier PCB) is still supported:
set DISPLAY_CONFIGURATION to "st7796_480x320" and HAS_CARRIER to True.

All pin numbers below use the physical header numbering (GPIO.BOARD), the same
convention used by `hardware/buttons.py` and the original display driver.
"""

PROFILE__STANDARD = "standard"
PROFILE__MAX = "max"

ACTIVE_PROFILE = PROFILE__MAX


# Default display, one of SettingsConstants.ALL_DISPLAY_CONFIGURATIONS:
#   "ili9486_480x320"  v5: Waveshare 3.5inch RPi LCD (A), plugged onto the header
#   "st7796_480x320"   v4: LCDwiki MSP4021 4.0" on the carrier PCB
DISPLAY_CONFIGURATION = "ili9486_480x320"


# ---------------------------------------------------------------------------------------
# v5 display: Waveshare 3.5inch RPi LCD (A), ILI9486, on SPI0 CE0
# ---------------------------------------------------------------------------------------
# Pins fixed by the board (Waveshare wiki): LCD_RS = pin 18, RST = pin 22, LCD_CS = pin 24
# (CE0), LCD_SI = pin 19, LCD_SCK = pin 23. There is no backlight pin: it is always on.
ILI9486_PIN_DC = 18   # BCM 24
ILI9486_PIN_RST = 22  # BCM 25

# The board has an SPI-to-16-bit-parallel converter in front of the controller, so
# every command and parameter byte goes out as a 16-bit word (0x00, byte); pixels are
# 16-bit RGB565 words anyway (Linux: drivers/gpu/drm/tiny/ili9486.c, "rpi-lcd-35").
#
# 0xE8 = landscape (MY | MX | MV | BGR): rotation 0 of the Linux driver, with the
# board's header at the top edge. Use 0x28 (MV | BGR) if the image is upside down
# because of how the panel sits in your enclosure, then recalibrate the touch screen.
ILI9486_MADCTL = 0xE8

# SPI clock. The converter limits it: 16 MHz is the usual safe value for this board.
# Try 24_000_000 or 32_000_000 for faster screen updates; go back down if the image
# shows noise, wrong colors or shifted lines.
ILI9486_SPI_SPEED_HZ = 16_000_000


# ---------------------------------------------------------------------------------------
# v4 display: LCDwiki MSP4021 4.0" (ST7796S, 480x320 landscape) on SPI0 CE0
# ---------------------------------------------------------------------------------------
# Same control pins as the Waveshare 1.3" hat, so the SPI wiring is unchanged:
#   SPI0 MOSI = pin 19, SCLK = pin 23, CE0 = pin 24 (claimed by the kernel spidev driver)
ST7796_PIN_DC = 22    # BCM 25
ST7796_PIN_RST = 13   # BCM 27
ST7796_PIN_BL = 18    # BCM 24 (the MSP4021 switches the backlight with a transistor)

# 0x28 = landscape (MV | BGR). Use 0xE8 (MY | MX | MV | BGR) if the image is upside down
# because of how the panel is mounted in your enclosure. If you flip it, recalibrate the
# touch screen (Settings > Advanced > Hardware > Touch calibration).
ST7796_MADCTL = 0x28

# Most ST7796S modules expect BGR subpixel order (bit 0x08 of MADCTL, already set above).
# If orange UI elements show up blue, clear that bit: e.g. 0x20 instead of 0x28.

# IPS panels (e.g. Waveshare 3.5" SKU 29318) need display inversion ON to show correct
# colors; TN panels need it OFF. The MSP4021 is sold as "TFT", most likely TN. If the UI
# background is white instead of black, flip this value.
ST7796_INVERSION_ON = False

# SPI clock. 40MHz matches the original SeedSigner driver. Lower it (e.g. 32_000_000) if
# you see noise or tearing.
ST7796_SPI_SPEED_HZ = 40_000_000


# ---------------------------------------------------------------------------------------
# Touch controller (XPT2046, resistive) on SPI0 CE1
# ---------------------------------------------------------------------------------------
# Same wiring on both boards: TP_CS = pin 26 (CE1), TP_IRQ = pin 11, MISO = pin 21,
# MOSI/SCLK shared with the display. Each spidev device keeps its own clock, so the
# display keeps its speed while the touch controller is read at 1 MHz (max 2.5 MHz).
TOUCH_SPI_BUS = 0
TOUCH_SPI_DEVICE = 1                 # /dev/spidev0.1
TOUCH_SPI_SPEED_HZ = 1_000_000

# PENIRQ: low while the panel is pressed. Used only as a cheap "is anyone touching?"
# check, so the bus is idle while nobody touches.
TOUCH_PIN_IRQ = 11                   # BCM 17

# Pressure threshold on the 12-bit pressure estimate: lower = lighter touch accepted,
# but also more noise. Raise it if you see phantom taps.
TOUCH_PRESSURE_THRESHOLD = 400

# Raw ADC -> screen coordinate calibration, as an affine transform:
#   x = A*raw_x + B*raw_y + C
#   y = D*raw_x + E*raw_y + F
# A generic starting point (axes swapped, raw values from ~200 to ~3900). The real
# values depend on the panel: see TOUCH_CALIBRATE_AT_BOOT below.
TOUCH_CALIBRATION = (
    0.0, 480 / 3700, -200 * 480 / 3700,     # A, B, C
    320 / 3700, 0.0, -200 * 320 / 3700,     # D, E, F
)

# With an uncalibrated panel the menus may be impossible to hit, so the calibration
# screen could not be reached. While this is True, the four-cross calibration runs at
# every boot, before the main menu (it only needs presses on the crosses, whatever the
# current calibration). On the first image: calibrate, write down the six values shown,
# copy them into TOUCH_CALIBRATION above, set this to False and rebuild.
TOUCH_CALIBRATE_AT_BOOT = True


# ---------------------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------------------
# v5 has no carrier PCB: the PiSugar 3 Air handles charging and the on/off button by
# itself and SeedSigner reads nothing from it (no I2C driver, no extra code path).
# True only for the v4 carrier PCB, whose pins follow.
HAS_CARRIER = False

# Battery-low comparator (LM393 + TLV431, ~3.44 V on the cell): LOW = battery low
PIN_BATT_LOW = 15                    # BCM 22, 47 kOhm pull-up to 3V3 on the carrier

# MAX16054 CLEAR input: driving it HIGH switches the 5 V boost off, i.e. cuts the power. A 100 kOhm pull-down on the
# carrier keeps the device on while the Pi boots and its pins float.
PIN_PWR_OFF = 16                     # BCM 23

# BQ24072 PGOOD: LOW = USB cable connected (charging or charged)
PIN_CHG_PG = 32                      # BCM 12, 47 kOhm pull-up to 3V3 on the carrier


# ---------------------------------------------------------------------------------------
# UI sizes for touch
# ---------------------------------------------------------------------------------------
# One pixel is ~0.15 mm on the 3.5" panel (73 x 49 mm) and ~0.17 mm on the 4.0" one.
# The stock 32 px button height is too small to hit reliably on a resistive panel;
# 40 px (+4 px gap) gives a 6.7 mm row pitch on the 3.5" panel, 7.7 mm on the 4.0".
TOUCH_BUTTON_HEIGHT = 40


def is_max_profile() -> bool:
    return ACTIVE_PROFILE == PROFILE__MAX


def has_carrier() -> bool:
    """ The v4 carrier PCB (battery-low comparator, charger PGOOD, power switch) """
    return is_max_profile() and HAS_CARRIER
