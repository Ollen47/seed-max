"""
SeedSigner Max (v4): battery and power switch on the carrier PCB.

- PIN_BATT_LOW: an LM393 comparator with a TLV431 reference pulls it LOW when the cell
  drops below ~3.44 V (roughly the last 10% of charge). Plain digital input: the
  Pi has no ADC, and no fuel-gauge chip is needed (or wanted) for this.
- PIN_CHG_PG: the BQ24072 charger's PGOOD, LOW while a USB cable is connected.
- PIN_PWR_OFF: the CLEAR input of the MAX16054 on/off controller. HIGH switches the
  5 V boost off and with it the whole device. SeedSigner OS runs entirely from RAM, so
  cutting the power is also what wipes the seed from memory.

All of this is read-only information or a one-way "off" switch: nothing here can send
data anywhere.

v5 has no carrier (hw_profile.HAS_CARRIER = False): every function above is then a
no-op, so these pins are left alone. The v5 battery level comes from the UPS HAT (C)'s
INA219 instead (hardware/ina219.py), read by the same BatteryMonitor thread.
"""
import logging
import time

from seedsigner.hardware import hw_profile
from seedsigner.models.singleton import Singleton
from seedsigner.models.threads import BaseThread

logger = logging.getLogger(__name__)



def _gpio():
    """ RPi.GPIO in BOARD mode, or None where it isn't available (desktop, tests) """
    try:
        import RPi.GPIO as GPIO
    except Exception:
        return None
    GPIO.setmode(GPIO.BOARD)
    GPIO.setwarnings(False)
    return GPIO



def is_battery_low() -> bool:
    GPIO = _gpio()
    if GPIO is None or not hw_profile.has_carrier():
        return False
    # External 47 kOhm pull-up on the carrier: no internal pull needed
    GPIO.setup(hw_profile.PIN_BATT_LOW, GPIO.IN, pull_up_down=GPIO.PUD_OFF)
    return GPIO.input(hw_profile.PIN_BATT_LOW) == GPIO.LOW



def is_usb_power_connected() -> bool:
    GPIO = _gpio()
    if GPIO is None or not hw_profile.has_carrier():
        return False
    GPIO.setup(hw_profile.PIN_CHG_PG, GPIO.IN, pull_up_down=GPIO.PUD_OFF)
    return GPIO.input(hw_profile.PIN_CHG_PG) == GPIO.LOW



def power_off():
    """
    Cut the power through the MAX16054. Returns only if that didn't happen (e.g. a
    bench setup powered from USB without the switch).
    """
    GPIO = _gpio()
    if GPIO is None or not hw_profile.has_carrier():
        return
    logger.info("Cutting power")
    GPIO.setup(hw_profile.PIN_PWR_OFF, GPIO.OUT, initial=GPIO.LOW)
    time.sleep(0.05)
    GPIO.output(hw_profile.PIN_PWR_OFF, GPIO.HIGH)
    time.sleep(1.0)



class BatteryMonitor(Singleton, BaseThread):
    """
    Background thread for the battery.

    v4 carrier: shows a "Battery low" warning toast when the comparator trips, and again
    every REMIND_EVERY_S while it stays low. A reading only counts after
    CONFIRM_READINGS identical ones in a row, so the voltage dip of a camera start-up
    doesn't raise a false alarm.

    v5 (UPS HAT (C)): reads the INA219 every CHECK_EVERY_S and keeps `reading`, the
    median of the last READINGS_KEPT good readings (a BatteryReading, or None while
    there is none), for the battery icon in the top nav. "Battery low" below
    hw_profile.BATTERY_LOW_PERCENT every REMIND_EVERY_S, "Battery almost empty" below
    BATTERY_CRITICAL_PERCENT every CRITICAL_REMIND_S; never while a USB cable powers it.
    """
    CHECK_EVERY_S = 5
    FAST_CHECK_EVERY_S = 1          # while the Battery screen is open
    CONFIRM_READINGS = 3
    REMIND_EVERY_S = 5 * 60
    CRITICAL_REMIND_S = 60
    WARNING_HYSTERESIS_PERCENT = 3     # a warning level is left only this far above its threshold
    READINGS_KEPT = 3
    STALE_AFTER_S = 60              # no good reading for this long: hide the icon
    GIVE_UP_AFTER = 6               # INA219 not found this many times in a row: stop

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            monitor = cls.__new__(cls)
            cls._instance = monitor
            BaseThread.__init__(monitor)
            monitor.is_low = False
            monitor.recent = []
            monitor.reading = None
            monitor.reading_time = 0
            monitor.gauge = None
            monitor.fast = False
            monitor.latest = None          # last good reading, unfiltered
            monitor.last_battery_percent = None   # last estimate made on battery (off the cable)
            monitor.warnings_paused = False      # True while a QR code is on screen (pause_warnings)
        return cls._instance


    def start_monitoring(self):
        if getattr(self, "_monitoring", False):
            # Once per boot (a thread can only be started once)
            return
        if (hw_profile.has_carrier() or hw_profile.has_battery_gauge()) and _gpio() is not None:
            self._monitoring = True
            self.start()


    def pause_warnings(self, paused: bool):
        """
        No battery toast while a QR code is on screen (QRDisplayScreen). Every toast breaks
        the screen's wait for input (`override_ind`, upstream), and the QR screen used to
        take that as "done": the signed PSBT vanished and had to be signed again. A warning
        that comes due meanwhile is shown at the first check after the QR screen closes.
        """
        self.warnings_paused = paused


    @property
    def current(self):
        """ The latest (filtered) BatteryReading, or None if there is no fresh one """
        if self.reading is None or time.time() - self.reading_time > self.STALE_AFTER_S:
            return None
        return self.reading


    def add_reading(self, reading, now: float = None):
        """
        Keep the median (by voltage) of the last READINGS_KEPT good readings. When the
        cable is plugged in or pulled out, the readings before it describe another state:
        they are dropped, so the change shows at once instead of after the median catches up.
        """
        if reading is None:
            return
        self.latest = reading
        if self.recent and self.recent[-1].on_external_power != reading.on_external_power:
            self.recent = []
        self.recent = (self.recent + [reading])[-self.READINGS_KEPT:]
        ordered = sorted(self.recent, key=lambda r: r.voltage)
        self.reading = ordered[len(ordered) // 2]
        self.reading_time = time.time() if now is None else now
        if not self.reading.on_external_power:
            self.last_battery_percent = self.reading.percent


    def shown_percent(self) -> int | None:
        """
        The percentage to show: the estimate on battery; nothing while the charger fills
        the cell (its voltage reads high whatever the charge); 100 once charged.
        """
        reading = self.current
        if reading is None:
            return None
        if not reading.on_external_power:
            return reading.percent
        return None if reading.charging else 100


    def fill_percent(self) -> int | None:
        """ How full to draw the icon: as shown, or while charging the last on-battery estimate """
        reading = self.current
        if reading is None:
            return None
        shown = self.shown_percent()
        if shown is not None:
            return shown
        last = getattr(self, "last_battery_percent", None)
        return last if last is not None else reading.percent


    def warning_due(self, now: float, last_warning: float | None, last_level: str | None):
        """
        (toast text or None, level) for the current reading: "critical", "low" or None.
        A level change warns at once; the same level again after its reminder interval.
        """
        from gettext import gettext as _
        reading = self.current
        if reading is None or reading.on_external_power:
            return None, None
        # Hysteresis: a level, once reached, is kept until the estimate is clearly above its
        # threshold. Near 15% the estimate wobbles by a point from one check to the next
        # (4 mV steps, load changes), and every re-entry used to warn again at once.
        percent, margin = reading.percent, self.WARNING_HYSTERESIS_PERCENT
        if percent <= hw_profile.BATTERY_CRITICAL_PERCENT or (
                last_level == "critical" and percent <= hw_profile.BATTERY_CRITICAL_PERCENT + margin):
            level, text, every = "critical", _("Battery almost empty ({}%)").format(percent), self.CRITICAL_REMIND_S
        elif percent <= hw_profile.BATTERY_LOW_PERCENT or (
                last_level in ("low", "critical") and percent <= hw_profile.BATTERY_LOW_PERCENT + margin):
            level, text, every = "low", _("Battery low ({}%)").format(percent), self.REMIND_EVERY_S
        else:
            return None, None
        # At once only when it gets worse (or the first time); otherwise on the reminder schedule
        severity = {None: 0, "low": 1, "critical": 2}
        if severity[level] > severity.get(last_level, 0) or last_warning is None or now - last_warning >= every:
            return text, level
        return None, level


    def next_warning(self, now: float, last_warning: float | None, last_level: str | None):
        """
        One check of the gauge loop: (toast text or None, last_warning, last_level) to
        carry to the next check. While warnings are paused (a QR code on screen) a warning
        that is due is held back and the state is left as it was, so it is due again at
        the first check after the pause.
        """
        text, level = self.warning_due(now, last_warning, last_level)
        self.is_low = level is not None
        if text and getattr(self, "warnings_paused", False):
            return None, last_warning, last_level
        if text:
            last_warning = now
        return text, last_warning, level


    def _run_carrier(self):
        from seedsigner.controller import Controller
        from seedsigner.gui.toast import WarningToast
        from gettext import gettext as _

        low_readings = 0
        last_warning = None
        while self.keep_running:
            try:
                if is_battery_low():
                    low_readings += 1
                else:
                    low_readings = 0
                    self.is_low = False
                    last_warning = None

                if low_readings >= self.CONFIRM_READINGS:
                    self.is_low = True
                    now = time.time()
                    if (last_warning is None or now - last_warning >= self.REMIND_EVERY_S) and not getattr(self, "warnings_paused", False):
                        last_warning = now
                        Controller.get_instance().activate_toast(WarningToast(_("Battery low"), duration=5))
            except Exception as e:
                logger.exception(f"Battery monitor: {e}")

            time.sleep(self.CHECK_EVERY_S)


    def _run_gauge(self):
        from seedsigner.controller import Controller
        from seedsigner.gui.toast import WarningToast
        from seedsigner.hardware.ina219 import INA219Gauge

        if self.gauge is None:
            try:
                self.gauge = INA219Gauge()
            except Exception as e:
                logger.exception(f"Battery monitor: no gauge: {e}")
                return
        not_found = 0
        last_warning = None
        last_level = None
        while self.keep_running:
            try:
                if self.gauge.address is None and self.gauge.find() is None:
                    not_found += 1
                    if not_found >= self.GIVE_UP_AFTER:
                        logger.info("INA219 not found: no battery level")
                        return
                else:
                    self.add_reading(self.gauge.read())
                    now = time.time()
                    text, last_warning, last_level = self.next_warning(now, last_warning, last_level)
                    if text:
                        Controller.get_instance().activate_toast(WarningToast(text, duration=5))
            except Exception as e:
                logger.exception(f"Battery monitor: {e}")

            # Short sleeps, so opening the Battery screen speeds readings up at once
            started = time.time()
            while self.keep_running:
                interval = self.FAST_CHECK_EVERY_S if self.fast else self.CHECK_EVERY_S
                if time.time() - started >= interval:
                    break
                time.sleep(0.2)


    def run(self):
        if hw_profile.has_carrier():
            self._run_carrier()
        elif hw_profile.has_battery_gauge():
            self._run_gauge()
