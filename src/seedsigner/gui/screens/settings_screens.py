import time

from dataclasses import dataclass
from gettext import gettext as _
from PIL.ImageOps import autocontrast
from typing import List

from seedsigner.helpers.l10n import mark_for_translation as _mft
from seedsigner.gui.components import Button, CheckboxButton, CheckedSelectionButton, FontAwesomeIconConstants, Fonts, GUIConstants, Icon, IconButton, IconTextLine, SeedSignerIconConstants, TextArea
from seedsigner.gui.screens.scan_screens import ScanScreen
from seedsigner.gui.screens.screen import BaseScreen, BaseTopNavScreen, ButtonListScreen, ButtonOption
from seedsigner.hardware.buttons import HardwareButtonsConstants
from seedsigner.hardware.camera import Camera
from seedsigner.models.settings import SettingsConstants
from seedsigner.models.threads import BaseThread



@dataclass
class SettingsEntryUpdateSelectionScreen(ButtonListScreen):
    display_name: str = None
    help_text: str = None
    checked_buttons: List[int] = None
    settings_entry_type: str = SettingsConstants.TYPE__ENABLED_DISABLED

    def __post_init__(self):
        self.title = _("Settings")
        self.is_bottom_list = True
        if self.settings_entry_type == SettingsConstants.TYPE__MULTISELECT:
            self.Button_cls = CheckboxButton
        else:
            self.Button_cls = CheckedSelectionButton
        super().__post_init__()

        self.components.append(TextArea(
            text=_(self.display_name),
            font_size=GUIConstants.BODY_FONT_MAX_SIZE,
            is_text_centered=True,
            auto_line_break=True,
            screen_y=self.top_nav.height + GUIConstants.COMPONENT_PADDING
        ))

        if self.help_text:
            prev_component_bottom = self.components[-1].screen_y + self.components[-1].height
            self.components.append(TextArea(
                text=_(self.help_text),
                font_color=GUIConstants.LABEL_FONT_COLOR,
                is_text_centered=True,
                screen_y=prev_component_bottom + GUIConstants.COMPONENT_PADDING,
                auto_line_break=True,
            ))



@dataclass
class IOTestScreen(BaseTopNavScreen):
    def __post_init__(self):
        # TRANSLATOR_NOTE: Short for "Input/Output"; screen to make sure the buttons and camera are working properly
        self.title = _("I/O Test")
        self.show_back_button = False
        self.resolution = (96, 96)
        self.framerate = 10
        self.instructions_text = None
        super().__post_init__()

        # D-pad pictogram
        input_button_width = GUIConstants.BUTTON_HEIGHT + 2
        input_button_height = input_button_width + 2
        dpad_center_x = GUIConstants.EDGE_PADDING + input_button_width + GUIConstants.COMPONENT_PADDING
        dpad_center_y = int((self.canvas_height - input_button_height)/2)

        self.joystick_click_button = IconButton(
            icon_name=FontAwesomeIconConstants.CIRCLE,
            icon_size=GUIConstants.ICON_INLINE_FONT_SIZE - 6,
            width=input_button_width,
            height=input_button_height,
            screen_x=dpad_center_x,
            screen_y=dpad_center_y,
            outline_color=GUIConstants.ACCENT_COLOR,
        )
        self.components.append(self.joystick_click_button)

        self.joystick_up_button = IconButton(
            icon_name=SeedSignerIconConstants.CHEVRON_UP,
            icon_size=GUIConstants.ICON_INLINE_FONT_SIZE,
            width=input_button_width,
            height=input_button_height,
            screen_x=dpad_center_x,
            screen_y=dpad_center_y - input_button_height - GUIConstants.COMPONENT_PADDING,
            outline_color=GUIConstants.ACCENT_COLOR,
        )
        self.components.append(self.joystick_up_button)

        self.joystick_down_button = IconButton(
            icon_name=SeedSignerIconConstants.CHEVRON_DOWN,
            icon_size=GUIConstants.ICON_INLINE_FONT_SIZE,
            width=input_button_width,
            height=input_button_height,
            screen_x=dpad_center_x,
            screen_y=dpad_center_y + input_button_height + GUIConstants.COMPONENT_PADDING,
            outline_color=GUIConstants.ACCENT_COLOR,
        )
        self.components.append(self.joystick_down_button)

        self.joystick_left_button = IconButton(
            icon_name=SeedSignerIconConstants.CHEVRON_LEFT,
            icon_size=GUIConstants.ICON_INLINE_FONT_SIZE,
            width=input_button_width,
            height=input_button_height,
            screen_x=dpad_center_x - input_button_width - GUIConstants.COMPONENT_PADDING,
            screen_y=dpad_center_y,
            outline_color=GUIConstants.ACCENT_COLOR,
        )
        self.components.append(self.joystick_left_button)

        self.joystick_right_button = IconButton(
            icon_name=SeedSignerIconConstants.CHEVRON_RIGHT,
            icon_size=GUIConstants.ICON_INLINE_FONT_SIZE,
            width=input_button_width,
            height=input_button_height,
            screen_x=dpad_center_x + input_button_width + GUIConstants.COMPONENT_PADDING,
            screen_y=dpad_center_y,
            outline_color=GUIConstants.ACCENT_COLOR,
        )
        self.components.append(self.joystick_right_button)

        # Hardware keys UI
        font = Fonts.get_font(GUIConstants.get_button_font_name(), GUIConstants.get_button_font_size())
        (left, top, text_width, bottom) = font.getbbox(text=_("Clear"), anchor="ls")
        icon = Icon(
            icon_name=FontAwesomeIconConstants.CAMERA, 
            icon_size=GUIConstants.ICON_INLINE_FONT_SIZE,
        )
        key_button_width = text_width + 2*GUIConstants.COMPONENT_PADDING + GUIConstants.EDGE_PADDING
        key_button_height = icon.height + int(1.5*GUIConstants.COMPONENT_PADDING)
        key2_y = int(self.canvas_height/2) - int(key_button_height/2)

        self.key2_button = Button(
            # TRANSLATOR_NOTE: Blank the screen
            text=_("Clear"),   # Initialize with text to set vertical centering
            width=key_button_width,
            height=key_button_height,
            screen_x=self.canvas_width - key_button_width + GUIConstants.EDGE_PADDING,
            screen_y=key2_y,
            outline_color=GUIConstants.ACCENT_COLOR,
            is_scrollable_text=False,  # Text has to dynamically update, can't use scrollable Button
        )
        if not self.renderer.is_screenshot_generator:
            # The button text should be empty for its initial state ("Clear" doesn't make
            # any sense until a test image is captured). But the screenshot generator
            # should show the text so its translation can be reviewed.
            self.key2_button.text = " "
        self.components.append(self.key2_button)

        self.key1_button = IconButton(
            icon_name=FontAwesomeIconConstants.CAMERA,
            width=key_button_width,
            height=key_button_height,
            screen_x=self.canvas_width - key_button_width + GUIConstants.EDGE_PADDING,
            screen_y=key2_y - 3*GUIConstants.COMPONENT_PADDING - key_button_height,
            outline_color=GUIConstants.ACCENT_COLOR,
        )
        self.components.append(self.key1_button)

        self.key3_button = Button(
            text=_("Exit"),
            width=key_button_width,
            height=key_button_height,
            screen_x=self.canvas_width - key_button_width + GUIConstants.EDGE_PADDING,
            screen_y=key2_y + 3*GUIConstants.COMPONENT_PADDING + key_button_height,
            outline_color=GUIConstants.ACCENT_COLOR,
            is_scrollable_text=False,  # No help for l10n, but currently ScrollableTextLine interferes with the small button's left edge. (TODO:)
        )
        self.components.append(self.key3_button)


    def _run(self):
        cur_selected_button = self.key1_button
        msg_height = GUIConstants.ICON_LARGE_BUTTON_SIZE + 2*GUIConstants.COMPONENT_PADDING
        camera_message = TextArea(
            text=_("Capturing image..."),
            font_size=GUIConstants.get_top_nav_title_font_size(),
            is_text_centered=True,
            height=msg_height,
            screen_y=int((self.canvas_height - msg_height)/ 2),
        )
        while True:
            input = self.hw_inputs.wait_for(keys=HardwareButtonsConstants.ALL_KEYS)

            if input == HardwareButtonsConstants.KEY1:
                # Note that there are three distinct screen updates that happen at
                # different times, therefore we claim the `Renderer.lock` three separate
                # times.
                cur_selected_button = self.key1_button

                with self.renderer.lock:
                    # Render edges around message box
                    self.image_draw.rectangle(
                        (
                            -1, int((self.canvas_height - msg_height)/ 2) - 1,
                            self.canvas_width + 1, int((self.canvas_height + msg_height)/ 2) + 1
                        ),
                        fill="black",
                        outline=GUIConstants.ACCENT_COLOR,
                        width=1,
                    )
                    cur_selected_button.is_selected = True
                    cur_selected_button.render()
                    camera_message.render()
                    self.renderer.show_image()

                # Snap a pic, render it as the background, re-render all onscreen elements
                camera = Camera.get_instance()
                try:
                    camera.start_single_frame_mode(resolution=(self.canvas_width, self.canvas_height))

                    # Reset the button state
                    with self.renderer.lock:
                        cur_selected_button.is_selected = False
                        cur_selected_button.render()
                        self.renderer.show_image()

                    time.sleep(0.25)
                    background_frame = camera.capture_frame()
                    display_version = autocontrast(
                        background_frame,
                        cutoff=2
                    )
                    with self.renderer.lock:
                        self.canvas.paste(display_version, (0, self.top_nav.height))
                        self.key2_button.text = _("Clear")
                        for component in self.components:
                            component.render()
                        self.renderer.show_image()
                finally:
                    camera.stop_single_frame_mode()

                continue

            elif input == HardwareButtonsConstants.KEY2:
                cur_selected_button = self.key2_button

                # Clear the background
                with self.renderer.lock:
                    cur_selected_button.is_selected = True
                    self._render()
                    self.renderer.show_image()

                    # And then re-render Key2 in its initial state
                    self.key2_button.text = " "
                    cur_selected_button.is_selected = False
                    cur_selected_button.render()
                    self.renderer.show_image()
                
                continue

            elif input == HardwareButtonsConstants.KEY3:
                # Exit
                cur_selected_button = self.key3_button
                cur_selected_button.is_selected = True
                with self.renderer.lock:
                    cur_selected_button.render()
                    self.renderer.show_image()
                    return
            
            elif input == HardwareButtonsConstants.KEY_PRESS:
                cur_selected_button = self.joystick_click_button

            elif input == HardwareButtonsConstants.KEY_UP:
                cur_selected_button = self.joystick_up_button

            elif input == HardwareButtonsConstants.KEY_DOWN:
                cur_selected_button = self.joystick_down_button

            elif input == HardwareButtonsConstants.KEY_LEFT:
                cur_selected_button = self.joystick_left_button

            elif input == HardwareButtonsConstants.KEY_RIGHT:
                cur_selected_button = self.joystick_right_button

            with self.renderer.lock:
                cur_selected_button.is_selected = True
                cur_selected_button.render()
                self.renderer.show_image()

            with self.renderer.lock:
                cur_selected_button.is_selected = False
                cur_selected_button.render()
                self.renderer.show_image()

            time.sleep(0.1)



@dataclass
class DonateScreen(BaseTopNavScreen):
    def __post_init__(self):
        self.title = _("Donate")
        super().__post_init__()

        self.components.append(TextArea(
            # TRANSLATOR_NOTE: If your language uses the percent sign ("%"), your translation must also use two percent signs ("%%") due to python formatting oddities. "100%%" will be rendered as "100%".
            text=_("SeedSigner is 100%% free & open source, funded solely by the Bitcoin community.\n\nDonate onchain or LN at:").replace("%%", "%"),
            screen_y=self.top_nav.height + 3*GUIConstants.COMPONENT_PADDING,
        ))

        self.components.append(TextArea(
            text="seedsigner.com",
            font_name=GUIConstants.get_body_font_name(),
            font_size=28,
            font_color=GUIConstants.ACCENT_COLOR,
            supersampling_factor=1,
            screen_y=self.components[-1].screen_y + self.components[-1].height + GUIConstants.COMPONENT_PADDING
        ))



@dataclass
class SettingsQRConfirmationScreen(ButtonListScreen):
    config_name: str = None
    title: str = _mft("Settings QR")
    status_message: str = _mft("Settings updated...")
    is_bottom_list: bool = True

    def __post_init__(self):
        # Customize defaults
        self.button_data = [ButtonOption("Home")]
        self.show_back_button = False
        super().__post_init__()

        start_y = self.top_nav.height + 20
        if self.config_name:
            self.config_name_textarea = TextArea(
                text=f'"{self.config_name}"',  # User-supplied string (from SettingsQR); don't wrap to translate
                is_text_centered=True,
                auto_line_break=True,
                screen_y=start_y
            )
            self.components.append(self.config_name_textarea)
            start_y = self.config_name_textarea.screen_y + 50
        
        self.components.append(TextArea(
            text=_(self.status_message),
            is_text_centered=True,
            auto_line_break=True,
            screen_y=start_y
        ))



def wait_touch_release(device, deadline: float, clear_samples: int = 10) -> bool:
    """
    SeedSigner Max: wait until the panel has read empty `clear_samples` times in a row
    (~10 ms apart: ~100 ms without contact). False if `deadline` passes first.
    """
    clear = 0
    while time.time() < deadline:
        clear = clear + 1 if device.read_raw() is None else 0
        if clear >= clear_samples:
            return True
        time.sleep(0.01)
    return False


def read_raw_press(device, deadline: float, min_samples: int, max_samples: int, drop_first: int = None):
    """
    SeedSigner Max: the raw (x, y, pressure) readings of one deliberate press, ~10 ms
    apart, or None if `deadline` passes first. A press counts once it has `min_samples`
    consecutive readings beyond the first `drop_first` (landing noise, dropped later by
    `press_stats()`); a shorter contact is a bounce and is forgotten. Reading stops at
    the release, or once `max_samples` readings were collected (finger still down).
    """
    from seedsigner.hardware.touch import PRESS_DROP_FIRST
    if drop_first is None:
        drop_first = PRESS_DROP_FIRST
    needed = min_samples + drop_first
    samples = []
    while time.time() < deadline:
        raw = device.read_raw()
        if raw is None:
            if len(samples) >= needed:
                break
            samples = []        # too short: a bounce, not a press
        else:
            samples.append(tuple(raw[:3]))
            if len(samples) >= max(max_samples, needed):
                break
        time.sleep(0.01)
    if len(samples) < needed:
        return None
    return samples



@dataclass
class TouchCalibrationScreen(BaseScreen):
    """
    SeedSigner Max: four-point touch calibration.

    Shows a cross near each corner in turn; the user presses its center. The raw ADC
    readings are collected straight from the XPT2046 (no calibration applied), the
    median of each press is kept, and an affine transform is fitted to the four points
    by least squares. Returns (TouchCalibration, max_error_px), or None if the user
    stops touching the screen for TIMEOUT_S (e.g. opened it by mistake).
    """
    TARGET_INSET = 40              # px from the screen edges
    MIN_SAMPLES = 8                # stable readings kept (after the first 3) before a press counts
    MAX_SAMPLES = 25               # readings collected per press, the dropped ones included
    TIMEOUT_S = 30

    def __post_init__(self):
        super().__post_init__()
        inset = self.TARGET_INSET
        self.targets = [
            (inset, inset),
            (self.canvas_width - inset, inset),
            (self.canvas_width - inset, self.canvas_height - inset),
            (inset, self.canvas_height - inset),
        ]
        self.instructions_font = Fonts.get_font(GUIConstants.get_body_font_name(), GUIConstants.get_body_font_size())


    def _render_target(self, index: int):
        x, y = self.targets[index]
        self.clear_screen()
        size = 14
        self.image_draw.ellipse((x - size, y - size, x + size, y + size), outline=GUIConstants.ACCENT_COLOR, width=2)
        self.image_draw.line((x - 2 * size, y, x + 2 * size, y), fill=GUIConstants.ACCENT_COLOR, width=2)
        self.image_draw.line((x, y - 2 * size, x, y + 2 * size), fill=GUIConstants.ACCENT_COLOR, width=2)
        self.image_draw.text(
            (int(self.canvas_width / 2), int(self.canvas_height / 2) - GUIConstants.COMPONENT_PADDING),
            _("Touch calibration") + f"  {index + 1}/{len(self.targets)}",
            fill=GUIConstants.BODY_FONT_COLOR, font=self.instructions_font, anchor="ms")
        self.image_draw.text(
            (int(self.canvas_width / 2), int(self.canvas_height / 2) + 2 * GUIConstants.COMPONENT_PADDING + 4),
            _("Press the center of the cross"),
            fill=GUIConstants.LABEL_FONT_COLOR, font=self.instructions_font, anchor="ms")


    def _render(self):
        self._render_target(0)


    def _wait_release(self, device, deadline: float) -> bool:
        """ Wait for ~100 ms without contact """
        return wait_touch_release(device, deadline)


    def _read_press(self, device, deadline: float):
        """
        Median raw (x, y) of one deliberate press, or None on timeout. The first
        PRESS_DROP_FIRST readings of the press (landing noise) are dropped; MIN_SAMPLES
        counts the readings kept after that.
        """
        from seedsigner.hardware.touch import press_stats
        samples = read_raw_press(device, deadline, self.MIN_SAMPLES, self.MAX_SAMPLES)
        if samples is None:
            return None
        stats = press_stats(samples)
        return (stats.x, stats.y)


    def _run(self):
        from seedsigner.hardware.touch import TouchCalibration
        device = self.hw_inputs._get_touch_device()
        if device is None:
            return None

        raw_points = []
        for index in range(len(self.targets)):
            if index > 0:
                with self.renderer.lock:
                    self._render_target(index)
                    self.renderer.show_image()

            deadline = time.time() + self.TIMEOUT_S
            # The finger that opened this screen, or pressed the previous cross, must go first
            if not self._wait_release(device, deadline):
                return None
            point = self._read_press(device, deadline)
            if point is None:
                return None
            raw_points.append(point)

        # Don't let the last press leak into the next screen as a tap
        self.hw_inputs.touch_require_release = True

        try:
            calibration = TouchCalibration.solve(raw_points, self.targets)
        except ValueError:
            return None
        return (calibration, calibration.max_error(raw_points, self.targets))



@dataclass
class TouchCalibrationResultScreen(ButtonListScreen):
    """
    SeedSigner Max: the new calibration is already active while this screen is shown,
    so reaching "Keep" proves it works. If nothing is chosen within CONFIRM_TIMEOUT_S
    (e.g. the new calibration is so wrong the buttons can't be hit) it returns "timeout"
    and the view restores the previous calibration.
    """
    CONFIRM_TIMEOUT_S = 15
    calibration_values: tuple = None
    max_error_px: float = 0.0

    def __post_init__(self):
        self.title = _("Touch calibration")
        self.show_back_button = False
        self.is_bottom_list = True
        self.button_data = [ButtonOption("Keep"), ButtonOption("Discard")]
        super().__post_init__()

        a, b, c, d, e, f = self.calibration_values
        self.components.append(TextArea(
            text=_("Max error: {} px").format(round(self.max_error_px, 1)),
            screen_y=self.top_nav.height,
        ))
        self.components.append(TextArea(
            # The six values to copy into hw_profile.TOUCH_CALIBRATION
            text=f"{a:.5f}, {b:.5f}, {c:.2f},\n{d:.5f}, {e:.5f}, {f:.2f}",
            font_name=GUIConstants.FIXED_WIDTH_FONT_NAME,
            font_color=GUIConstants.LABEL_FONT_COLOR,
            screen_y=self.components[-1].screen_y + self.components[-1].height + GUIConstants.COMPONENT_PADDING,
        ))
        self.deadline = time.time() + self.CONFIRM_TIMEOUT_S
        self.threads.append(TouchCalibrationResultScreen.TimeoutThread(self))


    class TimeoutThread(BaseThread):
        def __init__(self, screen):
            super().__init__()
            self.screen = screen

        def run(self):
            while self.keep_running:
                if time.time() >= self.screen.deadline:
                    self.screen.hw_inputs.trigger_override()
                    return
                time.sleep(0.1)


    def _run_callback(self):
        if time.time() >= self.deadline:
            return "timeout"
        return None



@dataclass
class TouchRawTestScreen(BaseScreen):
    """
    SeedSigner Max: diagnostic touch test (hw_profile.TOUCH_RAW_TEST_AT_BOOT).

    Five numbered targets (four corners and the center). For each deliberate press the
    raw XPT2046 readings are summarized in one log line: median position with half the
    range of each axis, median pressure, number of readings. After the fifth press, the
    least-squares fit of the five raw points to the five targets and its worst error.
    Uses raw readings only, so it works with any calibration, even a wildly wrong one.

    A strip of color bars (red, green, blue, white, the UI orange, black) under the
    title checks the display's colors at the same time.

    Ends on a press after the fifth one, or when nobody touches the screen for
    TIMEOUT_S at any stage. Returns the list of PressStats (possibly incomplete).
    """
    TIMEOUT_S = 120
    MIN_SAMPLES = 8                # stable readings kept (after the first 3) before a press counts
    MAX_SAMPLES = 50               # readings collected per press, the dropped ones included
    TARGETS = [(40, 40), (440, 40), (440, 280), (40, 280), (240, 160)]

    # Layout (480x320 landscape)
    TITLE_Y = 13                   # centers of the title and instruction lines
    INSTRUCTION_Y = 35
    COLOR_BARS_Y = 60              # top of the color strip (below the top corner targets)
    COLOR_BAR_SIZE = (70, 28)
    COLOR_BAR_GAP = 8
    FIT_Y = 94                     # fit result: three lines above the center target
    LOG_Y = 180                    # press log: five lines below the center target
    LINE_HEIGHT = 18
    TEXT_LEFT = 64                 # log and fit text stay clear of the corner targets (x 64..416)
    RAW_Y = 298                    # raw bytes of the last read: under the bottom targets
    FIXED_FONT_SIZE = 16

    def __post_init__(self):
        super().__post_init__()
        self.title_font = Fonts.get_font(GUIConstants.get_top_nav_title_font_name(), GUIConstants.get_body_font_size())
        self.small_font = Fonts.get_font(GUIConstants.get_body_font_name(), GUIConstants.LABEL_FONT_SIZE)
        self.fixed_font = Fonts.get_font(GUIConstants.FIXED_WIDTH_FONT_NAME, self.FIXED_FONT_SIZE)
        self.results = []
        self.fit = None             # (TouchCalibration or None, max error px or None)
        self.raw_response = None    # bytes of the XPT2046's last read during the last press


    def raw_line(self) -> str | None:
        """
        The last read's response bytes 1-20 in hex: ten 4-digit words, one per conversion
        (Z1 Z2 X0 X1 X2 X3 Y0 Y1 Y2 Y3). A sound word is 0xxx..7FF8 with the low 3 bits
        zero (its last hex digit 0 or 8): a quick check of the byte alignment on the board.
        """
        if not self.raw_response or len(self.raw_response) < 21:
            return None
        return "r " + "".join(f"{b:02X}" for b in self.raw_response[1:21])


    @classmethod
    def color_bars(cls):
        """ (label, fill, text color, outline) of each box of the color strip, in order """
        return [
            ("RED", (255, 0, 0), "white", None),
            ("GREEN", (0, 255, 0), "black", None),
            ("BLUE", (0, 0, 255), "white", None),
            ("WHITE", (255, 255, 255), "black", None),
            ("ORANGE", GUIConstants.ACCENT_COLOR, "black", None),
            ("BLACK", (0, 0, 0), "white", (255, 255, 255)),
        ]


    @classmethod
    def color_bar_rects(cls):
        width, height = cls.COLOR_BAR_SIZE
        count = len(cls.color_bars())
        total = count * width + (count - 1) * cls.COLOR_BAR_GAP
        x = int((480 - total) / 2)
        rects = []
        for _ in range(count):
            rects.append((x, cls.COLOR_BARS_Y, x + width - 1, cls.COLOR_BARS_Y + height - 1))
            x += width + cls.COLOR_BAR_GAP
        return rects


    def fit_lines(self) -> list[str]:
        if self.fit is None:
            return []
        calibration, max_error = self.fit
        if calibration is None:
            return ["fit failed: points collinear"]
        a, b, c, d, e, f = calibration.as_tuple()
        return [
            f"fit err {max_error:.0f} px",
            f"a={a:.5f} b={b:.5f} c={c:.2f}",
            f"d={d:.5f} e={e:.5f} f={f:.2f}",
        ]


    def _render_target(self, index: int):
        x, y = self.TARGETS[index]
        if index < len(self.results):
            color = GUIConstants.LABEL_FONT_COLOR            # done
        elif index == len(self.results):
            color = GUIConstants.ACCENT_COLOR                # press this one next
        else:
            color = GUIConstants.BODY_FONT_COLOR
        size = 8
        self.image_draw.ellipse((x - size, y - size, x + size, y + size), outline=color, width=2)
        self.image_draw.line((x - 12, y, x + 12, y), fill=color, width=1)
        self.image_draw.line((x, y - 12, x, y + 12), fill=color, width=1)
        # Number beside the target, on the side facing the middle of the screen
        label_x = x + 22 if x <= 240 else x - 22
        self.image_draw.text((label_x, y), str(index + 1), fill=color, font=self.fixed_font, anchor="mm")


    def _render(self):
        self.clear_screen()
        draw = self.image_draw
        center_x = int(self.canvas_width / 2)
        draw.text((center_x, self.TITLE_Y), "Touch test", fill=GUIConstants.BODY_FONT_COLOR, font=self.title_font, anchor="mm")

        if len(self.results) < len(self.TARGETS):
            instruction, color = "Hold each target ~1 s, in order 1-5", GUIConstants.LABEL_FONT_COLOR
        else:
            instruction, color = "Touch anywhere to continue", GUIConstants.BODY_FONT_COLOR
        draw.text((center_x, self.INSTRUCTION_Y), instruction, fill=color, font=self.small_font, anchor="mm")

        # Color strip: redrawn with every refresh of this screen
        for (label, fill, text_color, outline), rect in zip(self.color_bars(), self.color_bar_rects()):
            draw.rectangle(rect, fill=fill, outline=outline, width=1)
            draw.text((int((rect[0] + rect[2]) / 2), int((rect[1] + rect[3]) / 2)), label, fill=text_color, font=self.small_font, anchor="mm")

        for i, line in enumerate(self.fit_lines()):
            draw.text((self.TEXT_LEFT, self.FIT_Y + i * self.LINE_HEIGHT), line, fill=GUIConstants.BODY_FONT_COLOR, font=self.fixed_font)

        for index in range(len(self.TARGETS)):
            self._render_target(index)

        for i, stats in enumerate(self.results):
            draw.text((self.TEXT_LEFT, self.LOG_Y + i * self.LINE_HEIGHT), stats.log_line(i + 1), fill=GUIConstants.BODY_FONT_COLOR, font=self.fixed_font)

        raw = self.raw_line()
        if raw:
            draw.text((self.TEXT_LEFT, self.RAW_Y), raw, fill=GUIConstants.LABEL_FONT_COLOR, font=self.fixed_font)


    def _refresh(self):
        with self.renderer.lock:
            self._render()
            self.renderer.show_image()


    def _run(self):
        import logging
        from seedsigner.hardware.touch import TouchCalibration, press_stats
        logger = logging.getLogger(__name__)

        device = self.hw_inputs._get_touch_device()
        if device is None:
            return self.results

        try:
            for index in range(len(self.TARGETS)):
                deadline = time.time() + self.TIMEOUT_S
                # The previous press (or a finger left on the panel) must go first
                if not wait_touch_release(device, deadline):
                    return self.results
                samples = read_raw_press(device, deadline, self.MIN_SAMPLES, self.MAX_SAMPLES)
                if samples is None:
                    return self.results
                stats = press_stats(samples)
                self.results.append(stats)
                raw = getattr(device, "last_response", None)
                self.raw_response = list(raw) if isinstance(raw, (list, tuple, bytes)) else None
                logger.info(f"Touch test target {self.TARGETS[index]}: {stats.log_line(index + 1)}")

                if len(self.results) == len(self.TARGETS):
                    raw_points = [(r.x, r.y) for r in self.results]
                    try:
                        calibration = TouchCalibration.solve(raw_points, self.TARGETS)
                        self.fit = (calibration, calibration.max_error(raw_points, self.TARGETS))
                    except ValueError:
                        self.fit = (None, None)
                    logger.info(f"Touch test fit: {' | '.join(self.fit_lines())}")
                self._refresh()

            # "Touch anywhere to continue": one more deliberate press, or the timeout
            deadline = time.time() + self.TIMEOUT_S
            if wait_touch_release(device, deadline):
                read_raw_press(device, deadline, self.MIN_SAMPLES, self.MIN_SAMPLES)
            return self.results

        finally:
            # Let go of the panel before the next screen: the press that ended the test
            # must not count there (the calibration also waits for a release first)
            wait_touch_release(device, time.time() + 5)
            self.hw_inputs.touch_require_release = True



@dataclass
class BatteryInfoScreen(BaseTopNavScreen):
    """
    SeedSigner Max v5: what the UPS HAT (C)'s INA219 reads, live (one reading a second
    while the screen is open): cell voltage, current (< 0 = the cell feeds the Pi),
    estimated charge, cable detected or not, the chip's address and the raw registers.
    """
    REFRESH_S = 1.0
    TEXT_LEFT = 24
    LINE_HEIGHT = 30

    def __post_init__(self):
        self.title = _("Battery")
        self.show_back_button = True
        super().__post_init__()
        self.font = Fonts.get_font(GUIConstants.FIXED_WIDTH_FONT_NAME, 18)
        self.threads.append(BatteryInfoScreen.RefreshThread(self))


    def lines(self) -> list[str]:
        from seedsigner.hardware.power import BatteryMonitor
        monitor = BatteryMonitor.get_instance()
        gauge = getattr(monitor, "gauge", None)
        latest = getattr(monitor, "latest", None)
        shown = monitor.current
        if gauge is None or gauge.address is None:
            return ["INA219: not found (yet)"]
        out = [f"INA219 at 0x{gauge.address:02X}"]
        if latest is None:
            return out + ["no reading yet"]
        out += [
            f"voltage   {latest.voltage:.3f} V",
            f"current   {latest.current_ma:+.1f} mA",
            f"cable     {'yes' if latest.on_external_power else 'no'}"
            + (f" ({'charging' if latest.charging else 'charged'})" if latest.on_external_power else ""),
            f"estimate  {latest.percent}%",
        ]
        if shown is not None:
            percent = monitor.shown_percent()
            out.append(f"shown     {'--' if percent is None else f'{percent}%'}")
        if gauge.last_raw:
            bus_raw, shunt_raw = gauge.last_raw
            out.append(f"raw  bus {bus_raw:04X}  shunt {shunt_raw:04X}")
        return out


    def _render(self):
        super()._render()
        self.render_lines()


    def render_lines(self):
        top = self.top_nav.height + GUIConstants.COMPONENT_PADDING
        self.image_draw.rectangle((0, top, self.canvas_width, self.canvas_height), fill=GUIConstants.BACKGROUND_COLOR)
        for i, line in enumerate(self.lines()):
            self.image_draw.text((self.TEXT_LEFT, top + i * self.LINE_HEIGHT), line, fill=GUIConstants.BODY_FONT_COLOR, font=self.font)


    class RefreshThread(BaseThread):
        def __init__(self, screen):
            super().__init__()
            self.screen = screen

        def run(self):
            from seedsigner.hardware.power import BatteryMonitor
            monitor = BatteryMonitor.get_instance()
            monitor.fast = True
            try:
                while self.keep_running:
                    time.sleep(self.screen.REFRESH_S)
                    with self.screen.renderer.lock:
                        self.screen.render_lines()
                        self.screen.top_nav.render()
                        self.screen.renderer.show_image()
            finally:
                monitor.fast = False
