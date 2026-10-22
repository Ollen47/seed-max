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
from seedsigner.hardware.touch import TouchEvent
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


    def handle_touch(self, event: TouchEvent):
        """
        SeedSigner Max: the on-screen pictograms become buttons (camera / clear / exit on
        the right, the d-pad on the left). A tap anywhere else draws a small cross where
        the touch was read: a quick check of the touch calibration.
        """
        from seedsigner.gui.screens.screen import component_rect
        from seedsigner.hardware.touch import rect_contains
        if not event.is_tap:
            return None
        targets = (
            (self.key1_button, HardwareButtonsConstants.KEY1),
            (self.key2_button, HardwareButtonsConstants.KEY2),
            (self.key3_button, HardwareButtonsConstants.KEY3),
            (self.joystick_click_button, HardwareButtonsConstants.KEY_PRESS),
            (self.joystick_up_button, HardwareButtonsConstants.KEY_UP),
            (self.joystick_down_button, HardwareButtonsConstants.KEY_DOWN),
            (self.joystick_left_button, HardwareButtonsConstants.KEY_LEFT),
            (self.joystick_right_button, HardwareButtonsConstants.KEY_RIGHT),
        )
        for button, code in targets:
            if rect_contains(component_rect(button), event.x, event.y, margin=int(GUIConstants.COMPONENT_PADDING / 2)):
                return code

        with self.renderer.lock:
            size = 6
            self.image_draw.line((event.x - size, event.y, event.x + size, event.y), fill=GUIConstants.ACCENT_COLOR, width=2)
            self.image_draw.line((event.x, event.y - size, event.x, event.y + size), fill=GUIConstants.ACCENT_COLOR, width=2)
            self.renderer.show_image()
        return None


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
    MIN_SAMPLES = 8                # stable readings before a press counts (~80 ms)
    MAX_SAMPLES = 25
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
        clear = 0
        while time.time() < deadline:
            clear = clear + 1 if device.read_raw() is None else 0
            if clear >= 10:
                return True
            time.sleep(0.01)
        return False


    def _read_press(self, device, deadline: float):
        """ Median raw (x, y) of one deliberate press, or None on timeout """
        samples = []
        while time.time() < deadline:
            raw = device.read_raw()
            if raw is None:
                if len(samples) >= self.MIN_SAMPLES:
                    break
                samples = []        # too short: a bounce, not a press
            else:
                samples.append(raw[:2])
                if len(samples) >= self.MAX_SAMPLES:
                    break
            time.sleep(0.01)
        if len(samples) < self.MIN_SAMPLES:
            return None
        xs = sorted(s[0] for s in samples)
        ys = sorted(s[1] for s in samples)
        return (xs[len(xs) // 2], ys[len(ys) // 2])


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
