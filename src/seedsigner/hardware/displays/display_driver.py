from dataclasses import dataclass


DISPLAY_TYPE__ST7789 = "st7789"
DISPLAY_TYPE__ILI9341 = "ili9341"
DISPLAY_TYPE__ILI9486 = "ili9486"
DISPLAY_TYPE__ST7796 = "st7796"   # SeedSigner Max: 3.5"/4.0" 480x320 SPI panels

ALL_DISPLAY_TYPES = [DISPLAY_TYPE__ST7789, DISPLAY_TYPE__ILI9341, DISPLAY_TYPE__ILI9486, DISPLAY_TYPE__ST7796]

    

@dataclass
class BaseDisplayDriver:
    _width: int
    _height: int

    def __str__(self):
        return f"DisplayDriver(display_type={getattr(self, 'display_type', None)}, width={self.width}, height={self.height})"


    @property
    def width(self):
        return self._width


    @property
    def height(self):
        return self._height


    def invert(self, enabled: bool = True):
        """
        Invert how the display interprets colors.
        Implementation in child classes is optional.
        """
        pass


    def show_image(self, image, x_start: int = 0, y_start: int = 0):
        """
        The main rendering call to the display driver.
        Must be implemented in child classes.
        """
        raise Exception("show_image() must be implemented in child classes")


    def cleanup(self):
        """
        Cleanup resources used by the display driver.
        Implementation in child classes is optional.
        """
        pass



class DisplayDriverFactory:
    """
    Manages all logic related to instantiating display drivers based on type and resolution.

    Imports for specific display drivers are done within this class to avoid circular imports.
    """

    @classmethod
    def instantiate_display_driver(cls, display_type: str = DISPLAY_TYPE__ST7789, width: int = None, height: int = None) -> BaseDisplayDriver:
        if display_type not in ALL_DISPLAY_TYPES:
            raise ValueError(f"Invalid display type: {display_type}")

        if display_type == DISPLAY_TYPE__ST7789:
            if width not in [240, 320] or height != 240:
                raise ValueError("ST7789 display only supports 240x240 or 320x240 resolutions")

            if width == 240:
                # TODO: For now the original ST7789 driver has to be used for 240x240.
                # The mpy version below renders incorrectly (almost like each row of pixels
                # is one pixel short, so the entire screen exhibits a diagonal skew).
                from seedsigner.hardware.displays.ST7789 import ST7789 as original_ST7789
                return original_ST7789(_width=width, _height=height)

            elif width == 320:
                from seedsigner.hardware.displays.st7789_mpy import ST7789 as mpy_ST7789
                # Have to swap width and height; screen is natively 240x320
                return mpy_ST7789(_width=height, _height=width)

        elif display_type == DISPLAY_TYPE__ILI9341:
            from seedsigner.hardware.displays.ili9341 import ILI9341
            display = ILI9341(_width=width, _height=height)
            display.begin()
            return display
        
        elif display_type == DISPLAY_TYPE__ST7796:
            if (width, height) != (480, 320):
                raise ValueError("ST7796 display only supports 480x320 resolution")
            from seedsigner.hardware.displays.st7796 import ST7796
            # The driver configures the natively portrait panel as landscape itself
            return ST7796(_width=width, _height=height)

        elif display_type == DISPLAY_TYPE__ILI9486:
            # SeedSigner Max v5: Waveshare 3.5inch RPi LCD (A), see ili9486.py
            if (width, height) != (480, 320):
                raise ValueError("ILI9486 display only supports 480x320 resolution")
            from seedsigner.hardware.displays.ili9486 import ILI9486
            # The driver configures the natively portrait panel as landscape itself
            return ILI9486(_width=width, _height=height)
