"""
SeedSigner Max (v4): battery and power switch on the carrier PCB.

- PIN_BATT_LOW: an LM393 comparator with a TLV431 reference pulls it LOW when the cell
  drops below ~3.41 V (roughly the last 10% of charge). Plain digital input: the
  Pi has no ADC, and no fuel-gauge chip is needed (or wanted) for this.
- PIN_CHG_PG: the BQ24072 charger's PGOOD, LOW while a USB cable is connected.
- PIN_PWR_OFF: the CLEAR input of the MAX16054 on/off controller. HIGH switches the
  5 V boost off and with it the whole device. SeedSigner OS runs entirely from RAM, so
  cutting the power is also what wipes the seed from memory.

All of this is read-only information or a one-way "off" switch: nothing here can send
data anywhere.
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
    if GPIO is None or not hw_profile.is_max_profile():
        return False
    # External 47 kOhm pull-up on the carrier: no internal pull needed
    GPIO.setup(hw_profile.PIN_BATT_LOW, GPIO.IN, pull_up_down=GPIO.PUD_OFF)
    return GPIO.input(hw_profile.PIN_BATT_LOW) == GPIO.LOW



def is_usb_power_connected() -> bool:
    GPIO = _gpio()
    if GPIO is None or not hw_profile.is_max_profile():
        return False
    GPIO.setup(hw_profile.PIN_CHG_PG, GPIO.IN, pull_up_down=GPIO.PUD_OFF)
    return GPIO.input(hw_profile.PIN_CHG_PG) == GPIO.LOW



def power_off():
    """
    Cut the power through the MAX16054. Returns only if that didn't happen (e.g. a
    bench setup powered from USB without the switch).
    """
    GPIO = _gpio()
    if GPIO is None or not hw_profile.is_max_profile():
        return
    logger.info("Cutting power")
    GPIO.setup(hw_profile.PIN_PWR_OFF, GPIO.OUT, initial=GPIO.LOW)
    time.sleep(0.05)
    GPIO.output(hw_profile.PIN_PWR_OFF, GPIO.HIGH)
    time.sleep(1.0)



class BatteryMonitor(Singleton, BaseThread):
    """
    Background thread: shows a "Battery low" warning toast when the comparator trips,
    and again every REMIND_EVERY_S while it stays low. A reading only counts after
    CONFIRM_READINGS identical ones in a row, so the voltage dip of a camera start-up
    doesn't raise a false alarm.
    """
    CHECK_EVERY_S = 5
    CONFIRM_READINGS = 3
    REMIND_EVERY_S = 5 * 60

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            monitor = cls.__new__(cls)
            cls._instance = monitor
            BaseThread.__init__(monitor)
            monitor.is_low = False
        return cls._instance


    def start_monitoring(self):
        if hw_profile.is_max_profile() and _gpio() is not None:
            self.start()


    def run(self):
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
                    if last_warning is None or now - last_warning >= self.REMIND_EVERY_S:
                        last_warning = now
                        Controller.get_instance().activate_toast(WarningToast(_("Battery low"), duration=5))
            except Exception as e:
                logger.exception(f"Battery monitor: {e}")

            time.sleep(self.CHECK_EVERY_S)
