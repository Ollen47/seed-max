import logging
from typing import Callable, List
import RPi.GPIO as GPIO
import time

from seedsigner.hardware.touch import TouchEvent, TouchTracker
from seedsigner.models.singleton import Singleton

logger = logging.getLogger(__name__)


class HardwareButtons(Singleton):
    # SeedSigner Max (v4, touch screen)
    INPUT_GUARD_MS = 400          # touch ignored this long after a screen appears, then until lifted
    HOLD_TO_CONFIRM_MS = 1000     # irreversible actions need the finger held this long
    HOLD_MARGIN_PX = 12           # the finger may wander this far outside the button while holding
    TOUCH_CODE_TTL_MS = 300       # touch codes kept for `check_for_low()` polling loops

    if GPIO.RPI_INFO['P1_REVISION'] == 3: #This indicates that we have revision 3 GPIO
        logger.info("Detected 40pin GPIO (Raspberry Pi 2 and above)")
        KEY_UP_PIN = 31
        KEY_DOWN_PIN = 35
        KEY_LEFT_PIN = 29
        KEY_RIGHT_PIN = 37
        KEY_PRESS_PIN = 33

        KEY1_PIN = 40
        KEY2_PIN = 38
        KEY3_PIN = 36

    else:
        logger.info("Assuming 26 Pin GPIO (Raspberry Pi 1)")
        KEY_UP_PIN = 5
        KEY_DOWN_PIN = 11
        KEY_LEFT_PIN = 3
        KEY_RIGHT_PIN = 15
        KEY_PRESS_PIN = 7

        KEY1_PIN = 16
        KEY2_PIN = 12
        KEY3_PIN = 8


    @classmethod
    def get_instance(cls):
        # This is the only way to access the one and only instance
        if cls._instance is None:
            cls._instance = cls.__new__(cls)

            #init GPIO
            GPIO.setmode(GPIO.BOARD)
            GPIO.setup(HardwareButtons.KEY_UP_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)    # Input with pull-up
            GPIO.setup(HardwareButtons.KEY_DOWN_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # Input with pull-up
            GPIO.setup(HardwareButtons.KEY_LEFT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # Input with pull-up
            GPIO.setup(HardwareButtons.KEY_RIGHT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP) # Input with pull-up
            GPIO.setup(HardwareButtons.KEY_PRESS_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP) # Input with pull-up
            GPIO.setup(HardwareButtons.KEY1_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)      # Input with pull-up
            GPIO.setup(HardwareButtons.KEY2_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)      # Input with pull-up
            GPIO.setup(HardwareButtons.KEY3_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)      # Input with pull-up

            cls._instance.GPIO = GPIO
            cls._instance.override_ind = False

            # SeedSigner Max: no joystick and no KEY1-3, the touch screen is the only input.
            # Header pins 29-40 are left bare (the display covers 1-26) and pin 33 (KEY_PRESS)
            # sits next to GND on pin 34: anything bridging them would "press" with no
            # input guard and no hold-to-confirm. So the key pins are not read at all.
            from seedsigner.hardware import hw_profile
            cls._instance.gpio_keys = not hw_profile.is_max_profile()

            # Track state over time so we can apply input delays/ignores as needed
            cls._instance.cur_input = None           # Track which direction or button was last pressed
            cls._instance.cur_input_started = None   # Track when that input began
            cls._instance.last_input_time = int(time.time() * 1000)  # How long has it been since the last input?
            cls._instance.first_repeat_threshold = 225  # Long-press time required before returning continuous input
            cls._instance.next_repeat_threshold = 250  # Amount of time where we no longer consider input a continuous hold

            # SeedSigner Max: touch screen state (see `begin_screen()` and `poll_touch()`)
            cls._instance.touch_device = None          # XPT2046, created on first use
            cls._instance.touch_device_failed = False
            cls._instance.touch_tracker = TouchTracker()
            cls._instance.touch_handler = None
            cls._instance.touch_screen_started = 0
            cls._instance.touch_require_release = False
            cls._instance.touch_pending_codes = []

        return cls._instance


    # -----------------------------------------------------------------------------------
    # SeedSigner Max: touch screen
    # -----------------------------------------------------------------------------------
    def _get_touch_device(self):
        """
        The XPT2046 driver, created on first use. None on the stock profile, or if the
        controller can't be opened (then nothing can be pressed: fails closed).
        """
        if self.touch_device is None and not self.touch_device_failed:
            from seedsigner.hardware import hw_profile
            if not hw_profile.is_max_profile():
                self.touch_device_failed = True
                return None
            try:
                from seedsigner.hardware.xpt2046 import XPT2046
                self.touch_device = XPT2046()
            except Exception as e:
                logger.exception(f"Touch controller not available: {e}")
                self.touch_device_failed = True
        return self.touch_device


    def begin_screen(self, touch_handler: Callable = None):
        """
        Called by `BaseScreen.display()` every time a screen appears.

        `touch_handler(event)` is the screen's own translation of a `TouchEvent` into
        one of the key codes its `_run()` loop understands (or None to ignore it).

        Starts the input guard: touches are ignored for a short window, and then until
        the finger is lifted, so the press that confirmed the previous screen (or the
        second tap of an accidental double tap) can never act on the new one.
        """
        self.touch_handler = touch_handler
        self.touch_screen_started = int(time.time() * 1000)
        self.touch_require_release = True
        self.touch_pending_codes = []


    def poll_touch(self) -> List[TouchEvent]:
        """ Read the panel once and return the resulting touch events, after the guard """
        device = self._get_touch_device()
        if device is None:
            return []

        point = device.read()
        now = int(time.time() * 1000)
        events = self.touch_tracker.feed(point, now)
        if point is not None:
            # A finger on the panel counts as activity (screensaver)
            self.last_input_time = now

        if self.touch_require_release:
            if now - self.touch_screen_started >= self.INPUT_GUARD_MS and point is None and not self.touch_tracker.is_pressed:
                self.touch_require_release = False
            return []

        return events


    def _touch_codes(self, events: List[TouchEvent]) -> List[int]:
        """ The current screen's key codes for these events """
        codes = []
        if self.touch_handler is None:
            return codes
        for event in events:
            code = self.touch_handler(event)
            if code is not None:
                codes.append(code)
        return codes


    def track_touch_hold(self, rect: tuple, duration_ms: int = None, progress_callback: Callable = None) -> bool:
        """
        The finger just landed on a button that needs a hold (an irreversible action):
        returns True once it has stayed on `rect` for `duration_ms`, False as soon as it
        is lifted or slides away. `progress_callback(progress)` gets 0.0-1.0 while it
        is held, and None when it was let go early, so the screen can draw a filling bar.
        """
        from seedsigner.hardware.touch import rect_contains
        duration_ms = duration_ms or self.HOLD_TO_CONFIRM_MS
        callback = progress_callback or (lambda progress: None)
        device = self._get_touch_device()
        started = int(time.time() * 1000)
        while True:
            now = int(time.time() * 1000)
            point = device.read() if device else None
            self.touch_tracker.feed(point, now)
            position = self.touch_tracker.position
            if position is None or not rect_contains(rect, position[0], position[1], margin=self.HOLD_MARGIN_PX):
                callback(None)
                # Whatever the finger does next (still down and sliding, or the release
                # itself) must not count as a tap somewhere else
                self.touch_require_release = True
                return False

            elapsed = now - started
            callback(min(1.0, elapsed / duration_ms))
            if elapsed >= duration_ms:
                # The next screen's guard waits for this finger to be lifted
                self.touch_require_release = True
                self.last_input_time = now
                return True
            time.sleep(0.02)


    def wait_for(self, keys=[]) -> int:
        """
        Block execution until one of the target keys is pressed.

        Optionally override the wait by calling `trigger_override()`.
        """
        # TODO: Refactor to keep control in the Controller and not here
        from seedsigner.controller import Controller
        controller = Controller.get_instance()
        self.override_ind = False

        while True:
            if self.override_ind:
                # Break out of the wait_for without waiting for user input
                self.override_ind = False
                return HardwareButtonsConstants.OVERRIDE

            cur_time = int(time.time() * 1000)
            if cur_time - self.last_input_time > controller.screensaver_activation_ms and controller.is_screensaver_start_allowed:
                # Start the screensaver. Will block execution until input detected.
                controller.start_screensaver()

                # We're back. Update last_input_time to now.
                self.update_last_input_time()

                # Freeze any further processing for a moment to avoid having the wakeup
                #   input register in the resumed UI.
                time.sleep(self.next_repeat_threshold / 1000.0)

                # SeedSigner Max: the touch that woke the screen must not tap anything
                self.touch_screen_started = int(time.time() * 1000)
                self.touch_require_release = True

                # Resume from a fresh loop
                continue

            # SeedSigner Max: touch screen, translated by the current screen
            for code in self._touch_codes(self.poll_touch()):
                if code in keys:
                    self.cur_input = None
                    return code

            # Check each candidate key to see if it was pressed
            for key in (keys if getattr(self, "gpio_keys", True) else []):
                if self.GPIO.input(key) == GPIO.LOW:
                    if self.cur_input != key:
                        self.cur_input = key
                        self.cur_input_started = int(time.time() * 1000)  # in milliseconds
                        self.last_input_time = self.cur_input_started
                        return key

                    else:
                        # Still pressing the same input
                        if cur_time - self.last_input_time > self.next_repeat_threshold:
                            # Too much time has elapsed to consider this the same
                            #   continuous input. Treat as a new separate press.
                            self.cur_input_started = cur_time
                            self.last_input_time = cur_time
                            return key

                        elif cur_time - self.cur_input_started > self.first_repeat_threshold:
                            # We're good to relay this immediately as continuous
                            #   input.
                            self.last_input_time = cur_time
                            return key

                        else:
                            # We're not yet at the first repeat threshold; triggering
                            #   a key now would be too soon and yields a bad user
                            #   experience when only a single click was intended but
                            #   a second input is processed because of race condition
                            #   against human response time to release the button.
                            # So there has to be a delay before we allow the first
                            #   continuous repeat to register. So we'll ignore this
                            #   round's input and **won't update any of our
                            #   timekeeping vars**. But once we cross the threshold,
                            #   we let the repeats fly.
                            pass

            time.sleep(0.01) # wait 10 ms to give CPU chance to do other things


    def update_last_input_time(self):
        self.last_input_time = int(time.time() * 1000)


    def trigger_override(self) -> bool:
        """ Set the override flag to break out of the current `wait_for` loop """
        self.override_ind = True


    def check_for_low(self, key: int = None, keys: List[int] = None) -> bool:
        """ Returns True if one of the target keys/key is pressed """
        if key:
            keys = [key]
        for key in (keys if getattr(self, "gpio_keys", True) else []):
            if self.GPIO.input(key) == self.GPIO.LOW:
                self.update_last_input_time()
                return True

        # SeedSigner Max: the camera screens poll this instead of calling wait_for().
        # Taps are translated by the screen as usual and kept for a moment, because such
        # a loop asks about different keys in successive calls.
        now = int(time.time() * 1000)
        for code in self._touch_codes(self.poll_touch()):
            self.touch_pending_codes.append((code, now))
        self.touch_pending_codes = [(c, t) for (c, t) in self.touch_pending_codes if now - t < self.TOUCH_CODE_TTL_MS]
        for i, (code, _) in enumerate(self.touch_pending_codes):
            if code in keys:
                del self.touch_pending_codes[i]
                self.update_last_input_time()
                return True
        return False


    def has_any_input(self) -> bool:
        """ Returns True if any of the keys are pressed """
        for key in (HardwareButtonsConstants.ALL_KEYS if getattr(self, "gpio_keys", True) else []):
            if self.GPIO.input(key) == GPIO.LOW:
                return True

        # SeedSigner Max: a finger on the panel. Only the PENIRQ line is read here (no
        # SPI): this is also called from the toast thread.
        device = self.touch_device
        if device is not None and device.is_touched():
            return True
        return False


# class used as short hand for static button/channel lookup values
class HardwareButtonsConstants:
    if GPIO.RPI_INFO['P1_REVISION'] == 3: #This indicates that we have revision 3 GPIO
        KEY_UP = 31
        KEY_DOWN = 35
        KEY_LEFT = 29
        KEY_RIGHT = 37
        KEY_PRESS = 33

        KEY1 = 40
        KEY2 = 38
        KEY3 = 36
    else:
        KEY_UP = 5
        KEY_DOWN = 11
        KEY_LEFT = 3
        KEY_RIGHT = 15
        KEY_PRESS = 7

        KEY1 = 16
        KEY2 = 12
        KEY3 = 8

    OVERRIDE = 1000

    ALL_KEYS = [
        KEY_UP,
        KEY_DOWN,
        KEY_LEFT,
        KEY_RIGHT,
        KEY_PRESS,
        KEY1,
        KEY2,
        KEY3,
    ]

    KEYS__LEFT_RIGHT_UP_DOWN = [KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN]
    KEYS__ANYCLICK = [KEY_PRESS, KEY1, KEY2, KEY3]
