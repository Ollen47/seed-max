"""
Pure-logic layer for the "SeedSigner Max" touch screen (v4, v5).

No hardware access here: this module turns a stream of calibrated touch samples into
`TouchEvent`s, converts raw ADC readings to screen coordinates and solves the
calibration. Keeping it hardware-free makes it fully testable on any machine and easy
to audit. The SPI driver lives in `hardware/xpt2046.py`; `hardware/buttons.py` feeds
the events to the screens.

Design choices that matter for a signing device:
- Debounce both ways: a contact counts only after CONTACT_SAMPLES consecutive valid
  samples, a release only after RELEASE_SAMPLES consecutive empty ones spanning at
  least RELEASE_MS. Resistive panels flicker in and out at low pressure, above all
  while a finger or stylus slides; this keeps one press (or one drag) from becoming
  two. The release is timed, not just counted: HardwareButtons.wait_for() reads the
  panel about every 10 ms, and the camera screens' loops read it back to back, so
  three samples could be only a few milliseconds, and any short pressure dip in the
  middle of a drag ended it. Each restarted drag then had to cross TAP_SLOP_PX again before counting
  SWIPE_STEP_PX, and long lists could not be scrolled on the real panel.
- The position is the median of the last three samples, so a single wild sample (they
  happen as pressure drops at release) can't move the finger.
- A tap is a press and release without moving: once the finger travels more than
  TAP_SLOP_PX it becomes a drag and can no longer trigger anything on release. Sliding
  off a button is the way to change your mind, as on a phone.
- Taps are hit-tested at the steady part of the press: the median of its samples
  without the first TAP_DROP_FIRST (landing: the pressure is still building up and the
  reading is pulled off, which is why the calibration drops them too, PRESS_DROP_FIRST)
  and without the last TAP_DROP_LAST (lift-off: the least reliable of all). A tap too
  short to have a steady part is hit-tested where it landed, as before. Measuring taps
  the way the calibration measures its crosses keeps the two in step: on the real panel
  the landing samples read a stylus tap several pixels off (6 Nov 2026).
- Vertical drags are reported as SWIPE steps, one per SWIPE_STEP_PX, which scrolling
  lists translate into one row each.
"""
import math
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class TouchEvent:
    """
    Produced by `TouchTracker`. Coordinates are screen pixels (480x320 landscape).
    """
    TYPE__DOWN = "down"       # finger landed (debounced)
    TYPE__UP = "up"           # finger lifted; `is_tap` if it didn't move in between
    TYPE__SWIPE = "swipe"     # finger dragged vertically by one SWIPE_STEP_PX

    SWIPE__UP = "up"          # finger moved up: reveal what is below (like KEY_DOWN)
    SWIPE__DOWN = "down"      # finger moved down: reveal what is above (like KEY_UP)

    type: str
    x: int
    y: int
    tap: bool = False         # TYPE__UP only: the finger didn't move between DOWN and UP
    direction: str = None     # TYPE__SWIPE only
    duration_ms: int = 0      # TYPE__UP only: how long the finger stayed down

    @property
    def is_down(self) -> bool:
        return self.type == TouchEvent.TYPE__DOWN

    @property
    def is_tap(self) -> bool:
        return self.type == TouchEvent.TYPE__UP and self.tap

    @property
    def is_swipe(self) -> bool:
        return self.type == TouchEvent.TYPE__SWIPE



def rect_contains(rect: tuple[int, int, int, int], x: int, y: int, margin: int = 0) -> bool:
    """ True if (x, y) is inside `rect` = (x1, y1, x2, y2), grown by `margin` pixels """
    x1, y1, x2, y2 = rect
    return (x1 - margin) <= x <= (x2 + margin) and (y1 - margin) <= y <= (y2 + margin)



class TouchTracker:
    CONTACT_SAMPLES = 2       # ~20 ms of stable contact before a press counts
    RELEASE_SAMPLES = 3       # at least this many empty samples in a row ...
    RELEASE_MS = 60           # ... spanning at least this long before a release counts
    TAP_SLOP_PX = 20          # ~3 mm on the 3.5" panel, ~3.5 mm on the 4.0"
    SWIPE_STEP_PX = 44        # one list row (40 px button + 4 px gap)
    TAP_DROP_FIRST = 3        # landing samples left out of a tap's position (= PRESS_DROP_FIRST)
    TAP_DROP_LAST = 2         # lift-off samples left out of a tap's position
    TAP_MIN_STEADY = 3        # fewer steady samples than this: the landing point is used
    TAP_MAX_SAMPLES = 50      # samples kept per press (~0.5 s), enough for any tap

    def __init__(self):
        self.reset()


    def reset(self):
        self._contact = False
        self._pending_down = 0
        self._pending_up = 0
        self._empty_since_ms = None
        self._recent = deque(maxlen=3)
        self._down_pos = None
        self._press_samples = []
        self._last_pos = None
        self._down_ms = 0
        self._dragging = False
        self._swipe_anchor_y = None


    @property
    def is_pressed(self) -> bool:
        """ True between a DOWN and its UP """
        return self._contact


    @property
    def position(self) -> tuple[int, int] | None:
        """ Current (filtered) finger position, None if not pressed """
        return self._last_pos if self._contact else None


    @property
    def is_dragging(self) -> bool:
        return self._contact and self._dragging


    def _median_position(self) -> tuple[int, int]:
        xs = sorted(p[0] for p in self._recent)
        ys = sorted(p[1] for p in self._recent)
        return (xs[len(xs) // 2], ys[len(ys) // 2])


    def _tap_position(self) -> tuple[int, int]:
        """
        Where a tap is hit-tested: the median of the steady samples of the press, i.e.
        without the first TAP_DROP_FIRST and the last TAP_DROP_LAST. The landing point
        if fewer than TAP_MIN_STEADY are left (a very short tap).
        """
        steady = self._press_samples[self.TAP_DROP_FIRST:len(self._press_samples) - self.TAP_DROP_LAST]
        if len(steady) < self.TAP_MIN_STEADY:
            return self._down_pos
        xs = sorted(p[0] for p in steady)
        ys = sorted(p[1] for p in steady)
        return (xs[len(xs) // 2], ys[len(ys) // 2])


    def feed(self, point: tuple[int, int] | None, now_ms: int) -> list[TouchEvent]:
        """
        Feed one sample: the calibrated (x, y) of the finger, or None if nobody is
        touching (or the pressure is too low to trust). Returns the events it causes.
        """
        events = []

        if point is not None:
            self._pending_up = 0
            self._empty_since_ms = None
            self._recent.append((int(point[0]), int(point[1])))

            if not self._contact:
                self._pending_down += 1
                if self._pending_down >= self.CONTACT_SAMPLES:
                    self._contact = True
                    self._pending_down = 0
                    pos = self._median_position()
                    self._down_pos = pos
                    # The samples that made the contact are the first of the press
                    self._press_samples = list(self._recent)[-self.CONTACT_SAMPLES:]
                    self._last_pos = pos
                    self._down_ms = now_ms
                    self._dragging = False
                    self._swipe_anchor_y = pos[1]
                    events.append(TouchEvent(TouchEvent.TYPE__DOWN, pos[0], pos[1]))
            else:
                if len(self._press_samples) < self.TAP_MAX_SAMPLES:
                    self._press_samples.append((int(point[0]), int(point[1])))
                pos = self._median_position()
                self._last_pos = pos
                if not self._dragging:
                    dx = pos[0] - self._down_pos[0]
                    dy = pos[1] - self._down_pos[1]
                    if dx * dx + dy * dy > self.TAP_SLOP_PX * self.TAP_SLOP_PX:
                        self._dragging = True

                if self._dragging:
                    dy = pos[1] - self._swipe_anchor_y
                    while abs(dy) >= self.SWIPE_STEP_PX:
                        if dy < 0:
                            direction = TouchEvent.SWIPE__UP
                            self._swipe_anchor_y -= self.SWIPE_STEP_PX
                        else:
                            direction = TouchEvent.SWIPE__DOWN
                            self._swipe_anchor_y += self.SWIPE_STEP_PX
                        events.append(TouchEvent(TouchEvent.TYPE__SWIPE, pos[0], pos[1], direction=direction))
                        dy = pos[1] - self._swipe_anchor_y

        else:
            self._pending_down = 0
            if self._contact:
                self._pending_up += 1
                if self._empty_since_ms is None:
                    self._empty_since_ms = now_ms
                if self._pending_up >= self.RELEASE_SAMPLES and now_ms - self._empty_since_ms >= self.RELEASE_MS:
                    # The steady part of the press (see module docstring); a drag ends
                    # with tap=False, its position doesn't matter to anyone
                    up_pos = self._tap_position() if not self._dragging else self._down_pos
                    events.append(TouchEvent(
                        TouchEvent.TYPE__UP,
                        up_pos[0],
                        up_pos[1],
                        tap=not self._dragging,
                        duration_ms=now_ms - self._down_ms,
                    ))
                    self.reset()
            else:
                # A blip too short to count as a contact: forget it entirely
                self._recent.clear()

        return events



@dataclass(frozen=True)
class TouchCalibration:
    """
    Affine map from raw ADC readings to screen pixels:
        x = a*raw_x + b*raw_y + c
        y = d*raw_x + e*raw_y + f
    Six coefficients cover offset, scale, swapped axes, mirrored axes and the small
    rotation of a panel glued slightly crooked.
    """
    a: float
    b: float
    c: float
    d: float
    e: float
    f: float

    @classmethod
    def from_tuple(cls, values) -> "TouchCalibration":
        return cls(*[float(v) for v in values])


    def as_tuple(self) -> tuple:
        return (self.a, self.b, self.c, self.d, self.e, self.f)


    def to_screen(self, raw_x: float, raw_y: float, width: int, height: int) -> tuple[int, int]:
        """ Screen position of a raw reading, clamped to the visible area """
        x = self.a * raw_x + self.b * raw_y + self.c
        y = self.d * raw_x + self.e * raw_y + self.f
        x = min(max(int(round(x)), 0), width - 1)
        y = min(max(int(round(y)), 0), height - 1)
        return (x, y)


    @classmethod
    def solve(cls, raw_points: list[tuple[float, float]], screen_points: list[tuple[float, float]]) -> "TouchCalibration":
        """
        Least-squares fit from at least three (non-collinear) raw/screen point pairs.
        Raises ValueError if the points can't define a transform.
        """
        if len(raw_points) != len(screen_points) or len(raw_points) < 3:
            raise ValueError("Need at least three matching point pairs")

        # Normal equations M * [a, b, c] = v for x, and M * [d, e, f] = w for y
        sxx = sxy = sx = syy = sy = 0.0
        vx = [0.0, 0.0, 0.0]
        vy = [0.0, 0.0, 0.0]
        for (rx, ry), (px, py) in zip(raw_points, screen_points):
            sxx += rx * rx
            sxy += rx * ry
            sx += rx
            syy += ry * ry
            sy += ry
            vx[0] += rx * px
            vx[1] += ry * px
            vx[2] += px
            vy[0] += rx * py
            vy[1] += ry * py
            vy[2] += py
        n = float(len(raw_points))
        m = [
            [sxx, sxy, sx],
            [sxy, syy, sy],
            [sx, sy, n],
        ]
        a, b, c = _solve_3x3(m, vx)
        d, e, f = _solve_3x3(m, vy)
        return cls(a, b, c, d, e, f)


    def max_error(self, raw_points, screen_points) -> float:
        """ Largest distance in pixels between where the points are and where we'd put them """
        worst = 0.0
        for (rx, ry), (px, py) in zip(raw_points, screen_points):
            x = self.a * rx + self.b * ry + self.c
            y = self.d * rx + self.e * ry + self.f
            worst = max(worst, ((x - px) ** 2 + (y - py) ** 2) ** 0.5)
        return worst



def _solve_3x3(m: list[list[float]], v: list[float]) -> tuple[float, float, float]:
    """ Gaussian elimination with partial pivoting; raises ValueError if singular """
    a = [row[:] + [v[i]] for i, row in enumerate(m)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
        scale = max(abs(x) for x in a[pivot][:3]) or 1.0
        if abs(a[pivot][col]) / scale < 1e-9:
            raise ValueError("Calibration points are collinear or identical")
        a[col], a[pivot] = a[pivot], a[col]
        for r in range(col + 1, 3):
            factor = a[r][col] / a[col][col]
            for k in range(col, 4):
                a[r][k] -= factor * a[col][k]
    x = [0.0, 0.0, 0.0]
    for r in range(2, -1, -1):
        x[r] = (a[r][3] - sum(a[r][k] * x[k] for k in range(r + 1, 3))) / a[r][r]
    return tuple(x)



# ---------------------------------------------------------------------------------------
# Statistics of one press (raw readings), for the calibration and the touch test
# ---------------------------------------------------------------------------------------
PRESS_DROP_FIRST = 3        # the first readings of a press are landing noise: dropped


@dataclass(frozen=True)
class PressStats:
    """
    Summary of the raw readings of one press: median position, half the range of each
    axis (how much the readings moved around), median pressure, number of readings.
    """
    x: int
    y: int
    x_half_range: int
    y_half_range: int
    pressure: int
    samples: int

    def log_line(self, number: int) -> str:
        """ One line of the touch test log (fits in ~40 fixed-width characters) """
        return (f"{number}  x={self.x}(±{self.x_half_range})  y={self.y}(±{self.y_half_range})"
                f"  z={self.pressure}  k={self.samples}")


def _median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def press_stats(samples, drop_first: int = PRESS_DROP_FIRST) -> PressStats | None:
    """
    `samples`: the (raw_x, raw_y, pressure) readings of one press, in order. The first
    `drop_first` are dropped; None if nothing is left. The median of an even count is
    the upper one, as everywhere else here. Half ranges are rounded up, so any spread at
    all shows as at least 1.
    """
    kept = list(samples)[drop_first:]
    if not kept:
        return None
    xs = [s[0] for s in kept]
    ys = [s[1] for s in kept]
    zs = [s[2] for s in kept]
    return PressStats(
        x=int(round(_median(xs))),
        y=int(round(_median(ys))),
        x_half_range=int(math.ceil((max(xs) - min(xs)) / 2)),
        y_half_range=int(math.ceil((max(ys) - min(ys)) / 2)),
        pressure=int(round(_median(zs))),
        samples=len(kept),
    )
