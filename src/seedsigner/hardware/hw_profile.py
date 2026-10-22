"""
Build-time hardware profile for the "SeedSigner Max" modification (v4).

SeedSigner is stateless: unless Persistent Settings are enabled on a microSD card,
every boot starts from the default settings. A board that does not use the original
Waveshare 1.3" hat would therefore boot into an unusable screen every time.

The fix is to bake the hardware defaults into the image itself. Everything that is
specific to the custom hardware lives in this one file so that it is easy to audit
and easy to change before building your own image.

    ACTIVE_PROFILE = PROFILE__STANDARD  -> stock SeedSigner behavior (Waveshare 1.3")
    ACTIVE_PROFILE = PROFILE__MAX       -> 4.0" ST7796S 480x320 + XPT2046 resistive touch

SeedSigner Max v4 hardware: Raspberry Pi Zero v1.3, LCDwiki MSP4021 (4.0" 480x320
ST7796S panel with an XPT2046 resistive touch controller), LiPo cell with a BQ24072
charger, a MAX16054 pushbutton on/off controller and a TPS61023 5 V boost. No joystick, no KEY1-3:
the touch screen is the only input.

All pin numbers below use the physical header numbering (GPIO.BOARD), the same
convention used by `hardware/buttons.py` and the original display driver. The full
carrier schematic is documented in `seedsigner-max-v4-schema.md`.
"""

PROFILE__STANDARD = "standard"
PROFILE__MAX = "max"

ACTIVE_PROFILE = PROFILE__MAX


# ---------------------------------------------------------------------------------------
# Display (ST7796S, 480x320 landscape) on SPI0 CE0
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
# Shares MOSI/SCLK with the display. MISO (pin 21) belongs to the touch controller only:
# the display's SDO pin is not connected. Each spidev device keeps its own clock, so the
# display stays at 40 MHz while the touch controller is read at 1 MHz (max 2.5 MHz).
TOUCH_SPI_BUS = 0
TOUCH_SPI_DEVICE = 1                 # /dev/spidev0.1
TOUCH_SPI_SPEED_HZ = 1_000_000

# PENIRQ: low while the panel is pressed (10 kOhm pull-up on the MSP4021). Used only as
# a cheap "is anyone touching?" check, so the bus is idle while nobody touches.
TOUCH_PIN_IRQ = 11                   # BCM 17

# Pressure threshold on the 12-bit pressure estimate: lower = lighter touch accepted,
# but also more noise. Raise it if you see phantom taps.
TOUCH_PRESSURE_THRESHOLD = 400

# Raw ADC -> screen coordinate calibration, as an affine transform:
#   x = A*raw_x + B*raw_y + C
#   y = D*raw_x + E*raw_y + F
# These defaults assume the usual mounting of the MSP4021 (panel axes swapped by the
# landscape MADCTL, raw values from ~200 to ~3900) and are only a starting point:
# run Settings > Advanced > Hardware > Touch calibration once on your device and copy the six
# values it shows here, then rebuild the image.
TOUCH_CALIBRATION = (
    0.0, 480 / 3700, -200 * 480 / 3700,     # A, B, C
    320 / 3700, 0.0, -200 * 320 / 3700,     # D, E, F
)


# ---------------------------------------------------------------------------------------
# Power (carrier PCB)
# ---------------------------------------------------------------------------------------
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
# On the 4.0" panel one pixel is ~0.174 mm. The stock 32 px button height (5.6 mm) is
# too small to hit reliably on a resistive panel; 40 px (+4 px gap = 7.7 mm pitch) is.
TOUCH_BUTTON_HEIGHT = 40


def is_max_profile() -> bool:
    return ACTIVE_PROFILE == PROFILE__MAX
