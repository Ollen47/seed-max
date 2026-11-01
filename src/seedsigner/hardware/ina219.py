"""
SeedSigner Max v5: battery level from the INA219 on the Waveshare UPS HAT (C).

The INA219 is a fixed-function current/voltage monitor: an ADC and a handful of
registers, no firmware. On the UPS HAT (C) it measures the LiPo cell through a 0.1 ohm
shunt and sits on the Pi's I2C pins (header pins 3 = SDA, 5 = SCL), reached through the
HAT's pogo pins. The Waveshare 3.5" (A) display doesn't use those two pins.

SeedSigner OS has no I2C driver (no i2c-bcm2835, no /dev/i2c-*), and this project
leaves the OS untouched. So the app talks I2C itself, bit by bit on the two GPIOs
("bit-banged" I2C), in plain Python:
- open-drain emulation: a line is pulled low by driving it as an output LOW and
  released by turning it into an input; the Pi's own 1.8 kOhm pull-ups on GPIO2/3
  bring it back high;
- READ ONLY. The only bytes ever sent to the chip are its address and a register
  pointer. Nothing is written to its configuration or calibration registers: the
  power-on defaults (32 V bus range, +-320 mV shunt range, continuous conversion) are
  all this needs. Bus voltage (register 0x02) and shunt voltage (register 0x01) don't
  depend on the calibration register at all;
- every value is read twice and must agree, and must be physically plausible for one
  LiPo cell; otherwise the reading is dropped. Python threads can stall the bus for a
  few ms; the INA219 resets its interface if SCL stays low ~28 ms. Either way a bad
  transfer is discarded, never shown.

Sign convention (as in Waveshare's own sample code): current < 0 while the cell
discharges into the Pi, > 0 while the charger fills it.
"""
import logging
import time
from dataclasses import dataclass

from seedsigner.hardware import hw_profile

logger = logging.getLogger(__name__)


REG_SHUNT_VOLTAGE = 0x01         # signed, 10 uV per bit (sign-extended for every PGA range)
REG_BUS_VOLTAGE = 0x02           # bits 15..3 = voltage, 4 mV per bit; bit 1 CNVR, bit 0 OVF

# One LiPo cell: anything outside this is a misread, not a battery
CELL_MIN_V = 2.5
CELL_MAX_V = 4.5



# ---------------------------------------------------------------------------------------
# Pure helpers (tested without hardware)
# ---------------------------------------------------------------------------------------
def decode_bus_voltage(raw: int) -> float:
    """ Bus voltage register -> volts (bits 15..3, 4 mV per bit) """
    return ((raw & 0xFFFF) >> 3) * 0.004


def decode_shunt_mv(raw: int) -> float:
    """ Shunt voltage register (two's complement, 10 uV per bit) -> millivolts """
    raw &= 0xFFFF
    if raw & 0x8000:
        raw -= 0x10000
    return raw * 0.01


# Resting (open-circuit) voltage of a LiPo cell -> charge left, coarse but honest
LIPO_CURVE = [
    (3.30, 0), (3.50, 5), (3.60, 12), (3.68, 22), (3.73, 35), (3.77, 45),
    (3.81, 55), (3.86, 65), (3.92, 75), (3.98, 85), (4.06, 93), (4.15, 100),
]


def estimate_percent(voltage: float, current_ma: float, internal_ohm: float = None) -> int:
    """
    Charge left in % from the measured cell voltage. While the cell feeds the Pi its
    voltage sags by I x R_internal, so that drop is added back first; while charging the
    voltage reads high and the estimate is only indicative.
    """
    if internal_ohm is None:
        internal_ohm = hw_profile.BATTERY_INTERNAL_OHM
    resting = voltage - (current_ma / 1000.0) * internal_ohm   # current < 0 when discharging
    if resting <= LIPO_CURVE[0][0]:
        return 0
    if resting >= LIPO_CURVE[-1][0]:
        return 100
    for (v1, p1), (v2, p2) in zip(LIPO_CURVE, LIPO_CURVE[1:]):
        if v1 <= resting <= v2:
            return int(round(p1 + (p2 - p1) * (resting - v1) / (v2 - v1)))
    return 0



@dataclass(frozen=True)
class BatteryReading:
    voltage: float          # cell voltage, V
    current_ma: float       # < 0 discharging, > 0 charging
    percent: int            # estimate, 0-100

    @property
    def on_external_power(self) -> bool:
        """
        A USB cable feeds the HAT: the cell isn't feeding the Pi (it would show
        -300 mA or more) but charging or idle.
        """
        return self.current_ma > hw_profile.BATTERY_EXTERNAL_POWER_MA



# ---------------------------------------------------------------------------------------
# Bit-banged I2C master (open drain on two GPIOs)
# ---------------------------------------------------------------------------------------
class I2CError(Exception):
    pass


class SoftI2C:
    """
    Minimal I2C master on two GPIO pins (BOARD numbering), standard-mode timing or
    slower (the INA219 has no minimum clock rate). `gpio` is RPi.GPIO or a stand-in with
    setup(pin, mode, initial=...), input(pin) and the IN/OUT/LOW constants.
    """
    def __init__(self, gpio, sda: int, scl: int, half_period_s: float = 0.00002):
        self.GPIO = gpio
        self.sda = sda
        self.scl = scl
        self.half = half_period_s
        self._release(self.sda)
        self._release(self.scl)


    # Open drain: pull low as an output, release as an input (external pull-up)
    def _pull_low(self, pin):
        self.GPIO.setup(pin, self.GPIO.OUT, initial=self.GPIO.LOW)

    def _release(self, pin):
        self.GPIO.setup(pin, self.GPIO.IN)

    def _wait(self):
        if self.half:
            time.sleep(self.half)

    def _read(self, pin) -> int:
        return 1 if self.GPIO.input(pin) else 0


    def _scl_high(self):
        self._release(self.scl)
        # Clock stretching: wait (briefly) for a slave holding SCL low
        for _ in range(100):
            if self._read(self.scl):
                break
            time.sleep(0.0001)
        else:
            raise I2CError("SCL held low")
        self._wait()


    def recover(self):
        """ Free a slave stuck mid-byte: up to 9 clocks with SDA released, then STOP """
        self._release(self.sda)
        for _ in range(9):
            if self._read(self.sda):
                break
            self._pull_low(self.scl)
            self._wait()
            self._scl_high()
        self.stop()


    def start(self):
        self._release(self.sda)
        self._scl_high()
        if not self._read(self.sda):
            raise I2CError("SDA held low before START")
        self._pull_low(self.sda)
        self._wait()
        self._pull_low(self.scl)
        self._wait()


    def stop(self):
        self._pull_low(self.sda)
        self._wait()
        self._scl_high()
        self._release(self.sda)
        self._wait()


    def write_byte(self, value: int) -> bool:
        """ Clock out 8 bits, MSB first; True if the slave ACKed """
        for bit in range(7, -1, -1):
            if (value >> bit) & 1:
                self._release(self.sda)
            else:
                self._pull_low(self.sda)
            self._wait()
            self._scl_high()
            self._pull_low(self.scl)
        # ACK clock: release SDA, the slave pulls it low
        self._release(self.sda)
        self._wait()
        self._scl_high()
        ack = self._read(self.sda) == 0
        self._pull_low(self.scl)
        self._wait()
        return ack


    def read_byte(self, ack: bool) -> int:
        """ Clock in 8 bits, MSB first, then ACK (more to come) or NACK (last byte) """
        value = 0
        self._release(self.sda)
        for _ in range(8):
            self._wait()
            self._scl_high()
            value = (value << 1) | self._read(self.sda)
            self._pull_low(self.scl)
        if ack:
            self._pull_low(self.sda)
        else:
            self._release(self.sda)
        self._wait()
        self._scl_high()
        self._pull_low(self.scl)
        self._release(self.sda)
        self._wait()
        return value


    def probe(self, address: int) -> bool:
        """ True if a device ACKs `address` (a START, the address, a STOP; no data) """
        try:
            self.start()
            ack = self.write_byte((address << 1) | 0)
            self.stop()
            return ack
        except I2CError:
            self.recover()
            return False


    def read_register16(self, address: int, register: int) -> int:
        """
        Read a 16-bit big-endian register: write the pointer, STOP, then read two bytes
        (the INA219 keeps the pointer). Raises I2CError on a missing ACK.
        """
        try:
            self.start()
            if not self.write_byte((address << 1) | 0):
                raise I2CError(f"no ACK from 0x{address:02X}")
            if not self.write_byte(register):
                raise I2CError("register pointer not ACKed")
            self.stop()
            self.start()
            if not self.write_byte((address << 1) | 1):
                raise I2CError(f"no ACK from 0x{address:02X} (read)")
            msb = self.read_byte(ack=True)
            lsb = self.read_byte(ack=False)
            self.stop()
            return (msb << 8) | lsb
        except I2CError:
            self.recover()
            raise



# ---------------------------------------------------------------------------------------
# The gauge
# ---------------------------------------------------------------------------------------
class INA219Gauge:
    """
    Reads the cell through the INA219. `read()` returns a BatteryReading, or None if
    the chip isn't there or the reading didn't pass the checks.
    """
    def __init__(self, gpio=None, bus: SoftI2C = None):
        if bus is None:
            if gpio is None:
                import RPi.GPIO as gpio
                gpio.setmode(gpio.BOARD)
                gpio.setwarnings(False)
            bus = SoftI2C(gpio, hw_profile.BATTERY_I2C_SDA_PIN, hw_profile.BATTERY_I2C_SCL_PIN)
        self.bus = bus
        self.address = None


    def find(self) -> int | None:
        """ The first address in BATTERY_INA219_ADDRESSES that answers """
        for address in hw_profile.BATTERY_INA219_ADDRESSES:
            if self.bus.probe(address):
                self.address = address
                logger.info(f"INA219 found at 0x{address:02X}")
                return address
        return None


    def _read_twice(self, register: int, tolerance: int) -> int:
        """ Two reads in a row that must agree within `tolerance` raw units """
        a = self.bus.read_register16(self.address, register)
        b = self.bus.read_register16(self.address, register)
        sa = a - 0x10000 if register == REG_SHUNT_VOLTAGE and a & 0x8000 else a
        sb = b - 0x10000 if register == REG_SHUNT_VOLTAGE and b & 0x8000 else b
        if abs(sa - sb) > tolerance:
            raise I2CError(f"unstable register 0x{register:02X}: {a:#06x} vs {b:#06x}")
        return b


    def read(self) -> BatteryReading | None:
        if self.address is None and self.find() is None:
            return None
        try:
            bus_raw = self._read_twice(REG_BUS_VOLTAGE, tolerance=10 << 3)        # 40 mV
            shunt_raw = self._read_twice(REG_SHUNT_VOLTAGE, tolerance=3000)      # 30 mV = 300 mA
        except I2CError as e:
            logger.info(f"INA219 read dropped: {e}")
            return None

        voltage = decode_bus_voltage(bus_raw)
        if not CELL_MIN_V <= voltage <= CELL_MAX_V:
            logger.info(f"INA219 implausible cell voltage dropped: {voltage:.3f} V")
            return None
        current_ma = decode_shunt_mv(shunt_raw) / hw_profile.BATTERY_SHUNT_OHM
        return BatteryReading(
            voltage=round(voltage, 3),
            current_ma=round(current_ma, 1),
            percent=estimate_percent(voltage, current_ma),
        )
