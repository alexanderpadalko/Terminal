"""Small, Qt-independent viewport operations for an anchored chart."""
from dataclasses import dataclass


@dataclass
class ChartView:
    count: int = 125
    offset: int = 0
    # Empty calendar space to the right of the most recent candle.
    # It changes viewport drawing only; no synthetic OHLCV bars are created.
    right_padding: int = 16

    def window(self, length):
        if not length:
            return 0, 0
        end = length - max(0, min(self.offset, max(0, length - min(self.count, length))))
        return max(0, end - self.count), end

    def pan(self, bars, length):
        self.offset = max(0, min(max(0, length - min(self.count, length)), self.offset + bars))

    def zoom(self, factor, fraction, length):
        if not length:
            return
        first, last = self.window(length)
        fraction = min(1.0, max(0.0, fraction))
        anchor = first + fraction * (last - first)
        self.count = min(length, max(10, min(10000, round((last - first) * factor))))
        end = round(anchor + (1 - fraction) * self.count)
        end = min(length, max(self.count, end))
        self.offset = length - end

    def drag_chart(self, bars, length, *, start_offset, start_padding):
        """Pan historical bars; beyond the latest bar create/remove empty space.

        Positive bars = drag price to the right (older history), negative =
        drag price left (later history, then future whitespace).
        Drag arguments are measured from mouse press, not cumulatively.
        """
        bars = int(bars)
        start_offset = max(0, int(start_offset))
        start_padding = max(0, int(start_padding))
        max_offset = max(0, length - min(self.count, length))
        if bars >= 0:
            # Preserve ordinary drag-right historical panning, while
            # shrinking any existing future whitespace in parallel.
            self.right_padding = max(0, start_padding - bars)
            self.offset = min(max_offset, start_offset + bars)
        else:
            consumed = min(start_offset, -bars)
            self.offset = start_offset - consumed
            self.right_padding = min(250, start_padding - bars - consumed)

    def latest(self):
        self.offset = 0
