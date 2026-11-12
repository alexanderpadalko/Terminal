"""Small, Qt-independent viewport operations for an anchored chart."""
from dataclasses import dataclass
import math


def time_axis_zoom_factor(start_x, dx, width):
    """Stretch the ruler about its right edge, as in the reference video.

    Moving the ruler to the right reduces candle spacing. Use the distance
    to the fixed edge so the gesture scales with the chart, not a fixed
    exponential multiplier per pixel. Bound a drag beyond the edge.
    """
    distance = max(1.0, float(width) - float(start_x))
    return distance / max(distance / 50.0, distance - float(dx))


def inside_mother_bars(candles) -> list[bool]:
    """Flag inside candles using the *last non-inside mother*.

    A candle is inside when its entire high-low range lies within the current
    mother bar (inclusive on both edges). Nested inside candles do NOT replace
    the mother. The first candle is always a mother. This mirrors the
    high/low containment used by the indicator's FVG engine; it is used here
    only for presentation, never for signal calculations.
    """
    inside_flags: list[bool] = []
    mother_high = mother_low = None
    for candle in candles:
        inside = (mother_high is not None
                  and candle.high <= mother_high
                  and candle.low >= mother_low)
        inside_flags.append(inside)
        if not inside:
            mother_high, mother_low = candle.high, candle.low
    return inside_flags


@dataclass
class ChartView:
    count: float = 125
    offset: float = 0
    right_padding: float = 16

    @property
    def span(self):
        # The total number of slots is independent of future whitespace.
        # Thus moving a candle left/right never changes its pixel width.
        return max(1.0, float(self.count)) + 16.0

    def bounds(self, length):
        right = length - self.offset + self.right_padding
        if length:
            right = min(length + self.span - 1, max(min(length, self.span), right))
        return right - self.span, right

    def window(self, length):
        if not length:
            return 0, 0
        left, right = self.bounds(length)
        return max(0, min(length, math.floor(left))), max(0, min(length, math.ceil(right)))

    def _set_right(self, right, length):
        # Keep at least one real candle visible; no artificial OHLCV needed.
        right = min(length + self.span - 1, max(min(length, self.span), right))
        self.offset = max(0.0, length - right)
        self.right_padding = max(0.0, right - length)

    def pan(self, bars, length):
        if length:
            self._set_right(self.bounds(length)[1] - bars, length)

    def zoom(self, factor, fraction, length):
        if not length:
            return
        fraction = min(1.0, max(0.0, fraction))
        left, _ = self.bounds(length)
        anchor = left + fraction * self.span
        new_span = min(10016.0, max(26.0, self.span * factor))
        self.count = new_span - 16.0
        self._set_right(anchor + (1 - fraction) * new_span, length)

    def drag_chart(self, bars, length, *, start_offset, start_padding):
        if length:
            self._set_right(length - start_offset + start_padding - float(bars), length)

    def latest(self):
        self.offset, self.right_padding = 0.0, 16.0

