"""Small, Qt-independent viewport operations for an anchored chart."""
from dataclasses import dataclass


@dataclass
class ChartView:
    count: int = 125
    offset: int = 0

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

    def latest(self):
        self.offset = 0
