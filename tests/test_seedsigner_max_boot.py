"""
SeedSigner Max v5: the boot-time touch calibration comes before any other screen, is
repeated until a calibration is kept, and hands over to the "remove the microSD" warning
when that is set to stay on screen. The diagnostic touch test, when enabled, comes first
of all, then the calibration.
"""
from unittest.mock import MagicMock, patch

from base import BaseTest

from seedsigner.controller import Controller
from seedsigner.hardware import hw_profile
from seedsigner.models.settings_definition import SettingsConstants
from seedsigner.views.view import Destination, MainMenuView, RemoveMicroSDWarningView


class Stop(BaseException):
    """ Not an Exception: the Controller's main loop must not catch it """


class TestBootCalibration(BaseTest):
    def _start(self, microsd_toast_timer):
        """ (first Destination, activate_toast mock) when the Controller starts """
        seen = []

        def first_view_only(destination, *args, **kwargs):
            seen.append(destination)
            raise Stop()

        # The Controller keeps its own reference to the Settings instance
        Controller.get_instance().settings.set_value(SettingsConstants.SETTING__MICROSD_TOAST_TIMER, microsd_toast_timer)
        with patch.object(Destination, "_run_view", first_view_only), \
             patch("seedsigner.views.screensaver.OpeningSplashView.run"), \
             patch.object(Controller, "activate_toast") as activate_toast:
            try:
                Controller.get_instance().start()
            except Stop:
                pass
        return seen[0], activate_toast


    def test_calibration_first_when_enabled(self):
        from seedsigner.views.settings_views import TouchCalibrationView
        hw_profile.TOUCH_CALIBRATE_AT_BOOT = True
        try:
            first, toast = self._start(SettingsConstants.MICROSD_TOAST_TIMER_FIVE_SECONDS)
            assert first.View_cls == TouchCalibrationView
            assert first.view_args == {"at_boot": True, "then_microsd_warning": False}
            assert not toast.called, "the 5-second microSD toast would cover the crosses"

            first, toast = self._start(SettingsConstants.MICROSD_TOAST_TIMER_FOREVER)
            assert first.View_cls == TouchCalibrationView
            assert first.view_args == {"at_boot": True, "then_microsd_warning": True}
        finally:
            hw_profile.TOUCH_CALIBRATE_AT_BOOT = False


    def test_touch_test_before_calibration(self):
        from seedsigner.views.settings_views import TouchRawTestView
        hw_profile.TOUCH_CALIBRATE_AT_BOOT = True
        hw_profile.TOUCH_RAW_TEST_AT_BOOT = True
        try:
            first, toast = self._start(SettingsConstants.MICROSD_TOAST_TIMER_FIVE_SECONDS)
            assert first.View_cls == TouchRawTestView
            assert first.view_args == {"then_calibration": True, "then_microsd_warning": False}
            assert not toast.called, "the 5-second microSD toast would cover the touch test"

            first, toast = self._start(SettingsConstants.MICROSD_TOAST_TIMER_FOREVER)
            assert first.View_cls == TouchRawTestView
            assert first.view_args == {"then_calibration": True, "then_microsd_warning": True}

            # Touch test only, calibration already baked into the image
            hw_profile.TOUCH_CALIBRATE_AT_BOOT = False
            first, toast = self._start(SettingsConstants.MICROSD_TOAST_TIMER_FIVE_SECONDS)
            assert first.View_cls == TouchRawTestView
            assert first.view_args == {"then_calibration": False, "then_microsd_warning": False}
            assert not toast.called
        finally:
            hw_profile.TOUCH_CALIBRATE_AT_BOOT = False
            hw_profile.TOUCH_RAW_TEST_AT_BOOT = False


    def test_stock_behavior_when_disabled(self):
        hw_profile.TOUCH_CALIBRATE_AT_BOOT = False
        hw_profile.TOUCH_RAW_TEST_AT_BOOT = False
        first, toast = self._start(SettingsConstants.MICROSD_TOAST_TIMER_FIVE_SECONDS)
        assert first.View_cls == MainMenuView and toast.called
        first, toast = self._start(SettingsConstants.MICROSD_TOAST_TIMER_FOREVER)
        assert first.View_cls == RemoveMicroSDWarningView



class TestBootCalibrationView(BaseTest):
    def _run(self, screen_results, **view_args):
        """ Run TouchCalibrationView with scripted screen results; (destination, screens shown) """
        from seedsigner.hardware.buttons import HardwareButtons
        from seedsigner.hardware.touch import TouchCalibration
        from seedsigner.views.settings_views import TouchCalibrationView

        device = MagicMock()
        device.calibration = TouchCalibration(1, 0, 0, 0, 1, 0)
        results = list(screen_results)
        shown = []

        def run_screen(view, screen_cls, **kwargs):
            shown.append(screen_cls.__name__)
            return results.pop(0)

        with patch.object(HardwareButtons, "get_instance", return_value=MagicMock(_get_touch_device=lambda: device)), \
             patch.object(TouchCalibrationView, "run_screen", run_screen):
            destination = TouchCalibrationView(**view_args).run()
        return destination, shown, device


    def test_boot_repeats_until_kept(self):
        from seedsigner.hardware.touch import TouchCalibration
        new = TouchCalibration(0.1, 0, 0, 0, 0.1, 0)
        destination, shown, device = self._run(
            [None,               # nobody touched the crosses
             (new, 1.0), 1,      # calibrated, then "Discard"
             (new, 1.0), "timeout",
             (new, 1.0), 0],     # "Keep"
            at_boot=True)
        assert shown.count("TouchCalibrationScreen") == 4
        assert device.calibration == new
        assert destination.View_cls == MainMenuView


    def test_boot_then_microsd_warning(self):
        from seedsigner.hardware.touch import TouchCalibration
        destination, _, _ = self._run([(TouchCalibration(0.1, 0, 0, 0, 0.1, 0), 1.0), 0], at_boot=True, then_microsd_warning=True)
        assert destination.View_cls == RemoveMicroSDWarningView


    def test_from_settings_runs_once(self):
        from seedsigner.views.settings_views import SettingsMenuView
        destination, shown, device = self._run([None])
        assert shown == ["TouchCalibrationScreen"]
        assert destination.View_cls == SettingsMenuView



class TestBootTouchTestView(BaseTest):
    def _run(self, **view_args):
        from seedsigner.views.settings_views import TouchRawTestView
        shown = []

        def run_screen(view, screen_cls, **kwargs):
            shown.append(screen_cls.__name__)
            return []

        with patch.object(TouchRawTestView, "run_screen", run_screen):
            destination = TouchRawTestView(**view_args).run()
        return destination, shown


    def test_then_calibration(self):
        from seedsigner.views.settings_views import TouchCalibrationView
        for then_microsd_warning in (False, True):
            destination, shown = self._run(then_calibration=True, then_microsd_warning=then_microsd_warning)
            assert shown == ["TouchRawTestScreen"], "the touch test runs once"
            assert destination.View_cls == TouchCalibrationView
            assert destination.view_args == {"at_boot": True, "then_microsd_warning": then_microsd_warning}


    def test_without_calibration(self):
        destination, shown = self._run(then_calibration=False)
        assert shown == ["TouchRawTestScreen"] and destination.View_cls == MainMenuView
        destination, _ = self._run(then_calibration=False, then_microsd_warning=True)
        assert destination.View_cls == RemoveMicroSDWarningView
