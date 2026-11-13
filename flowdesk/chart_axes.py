"""Scale-aware price and exchange-local time-axis ticks.

Pure presentation logic: changes neither OHLC, bar timestamps, indicators nor
exchange sessions. Both rulers select a new interval from the visible zoom;
drawing happens in the existing Qt chart paint.
"""
from __future__ import annotations

from math import ceil, floor, isclose, isfinite, log10
from statistics import median


_NICE = (1.0, 2.0, 2.5, 5.0, 10.0)
_TIME_INTERVALS = (
    ("minute", 1, 60),
    ("minute", 5, 300),
    ("minute", 10, 600),
    ("minute", 15, 900),
    ("minute", 30, 1800),
    ("hour", 1, 3600),
    ("hour", 2, 7200),
    ("hour", 3, 10800),
    ("hour", 4, 14400),
    ("hour", 6, 21600),
    ("hour", 12, 43200),
    ("day", 1, 86400),
    ("day", 2, 172800),
    ("day", 3, 259200),
    ("week", 1, 604800),
    ("week", 2, 1209600),
    ("month", 1, 2629800),
    ("month", 3, 7889400),
    ("month", 6, 15778800),
    ("year", 1, 31557600),
    ("year", 2, 63115200),
    ("year", 5, 157788000),
    ("year", 10, 315576000),
)


def nice_price_step(low, high, height_px, tick_size=None, target_px=36):
    """Find an aesthetically rounded axis step, never finer than one tick.

    Values are anchored to zero instead of dividing the visible extent into
    a fixed number of arbitrary fractions. Decimal tick sizes are honoured
    even when a canonical 1/2/2.5/5 step is not a legal multiple.
    """
    if (not all(isfinite(v) for v in (low, high, height_px))
            or high <= low or height_px <= 0):
        return None
    tick = (float(tick_size) if tick_size is not None
            and isinstance(tick_size, (int, float))
            and isfinite(tick_size) and tick_size > 0 else None)
    raw = (high - low) * max(20, target_px) / height_px
    if tick is not None:
        raw = max(raw, tick)
    if not isfinite(raw) or raw <= 0:
        return None
    base = 10.0 ** floor(log10(raw))
    for multiplier in (1.0, 10.0, 100.0, 1000.0):
        for factor in _NICE:
            step = base * multiplier * factor
            if step < raw * (1 - 1e-12):
                continue
            if tick is None:
                return step
            units = step / tick
            if isfinite(units) and isclose(units, round(units),
                                            rel_tol=1e-9, abs_tol=1e-9):
                return step
    # Unusual increments (e.g. 0.03 or 3.0) still need a legal grid.
    if tick is None:
        return base * 10000.0
    units_needed = raw / tick
    power = 10.0 ** floor(log10(units_needed))
    for multiplier in (1.0, 10.0, 100.0):
        for factor in _NICE:
            units = factor * power * multiplier
            if units >= units_needed and isclose(units, round(units),
                                                  rel_tol=1e-10, abs_tol=1e-8):
                return round(units) * tick
    return ceil(units_needed) * tick


def price_axis_ticks(low, high, height_px, tick_size=None, target_px=36):
    """Return value/step pairs for visible, non-overlapping price labels."""
    step = nice_price_step(low, high, height_px, tick_size, target_px)
    if step is None or not isfinite(step) or step <= 0:
        return []
    first = ceil(low / step - 1e-10)
    last = floor(high / step + 1e-10)
    if last < first or last - first > 128:
        return []
    result = []
    for n in range(first, last + 1):
        value = n * step
        if abs(value) < step * 1e-10:
            value = 0.0
        result.append(value)
    return result


def _calendar_bucket(instant, kind, increment):
    """Exchange-wall-clock boundary, not a fabricated continuous UTC grid."""
    ordinal = instant.date().toordinal()
    if kind == "minute":
        bucket = (ordinal * 1440 + instant.hour * 60 + instant.minute) // increment
    elif kind == "hour":
        bucket = (ordinal * 24 + instant.hour) // increment
    elif kind == "day":
        bucket = ordinal // increment
    elif kind == "week":
        bucket = (ordinal - instant.weekday()) // (7 * increment)
    elif kind == "month":
        bucket = (instant.year * 12 + instant.month - 1) // increment
    else:
        bucket = instant.year // increment
    # A repeated hour across a DST autumn transition is a separate interval.
    return (bucket, instant.utcoffset()) if kind in ("minute", "hour") else bucket


def _calendar_label(instant, kind, previous):
    if kind in ("minute", "hour"):
        if previous is not None and instant.date() != previous.date():
            return instant.strftime("%d.%m")
        return instant.strftime("%H:%M")
    if kind in ("day", "week"):
        return instant.strftime("%d.%m.%Y") if previous is not None and (
            instant.year != previous.year) else instant.strftime("%d.%m")
    if kind == "month":
        return instant.strftime("%m.%Y")
    return instant.strftime("%Y")


def _median_bar_seconds(candles, first, last):
    """Use the trading-bar cadence, not long weekend/session gaps."""
    if last - first < 2:
        return 60.0
    stride = max(1, (last - first) // 400)
    seconds = []
    # Near neighbours preserve nominal cadence even across market holidays.
    for i in range(first + 1, last, stride):
        seconds.append((candles[i].time - candles[i-1].time).total_seconds())
    positive = sorted(value for value in seconds if 0 < value < 366 * 86400)
    return median(positive) if positive else 60.0


def time_axis_ticks(candles, first, last, left_edge, pixels_per_bar,
                    localize, *, target_px=110):
    """Return visible (bar index, caption) on exchange-local calendar bounds.

    The bar grid remains compressed across nights/weekends, as in the existing
    chart. Tick positions always refer to *actual* Finam candles. Use a
    calendar boundary at the requested zoom and suppress close captions.
    """
    if not candles or last <= first or pixels_per_bar <= 0:
        return []
    first = max(0, first)
    last = min(len(candles), last)
    if last <= first:
        return []
    nominal = _median_bar_seconds(candles, first, last)
    desired_seconds = nominal * max(70, target_px) / pixels_per_bar
    kind, step, _ = next(
        (candidate for candidate in _TIME_INTERVALS
         if candidate[2] >= desired_seconds),
        _TIME_INTERVALS[-1],
    )
    prev = (localize(candles[first - 1].time) if first > 0 else None)
    previous_bucket = _calendar_bucket(prev, kind, step) if prev else None
    ticks = []
    previous_x = float("-inf")
    previous_label_time = prev
    for index in range(first, last):
        current = localize(candles[index].time)
        bucket = _calendar_bucket(current, kind, step)
        if bucket != previous_bucket:
            position = (index - left_edge + 0.5) * pixels_per_bar
            if position >= 0 and position - previous_x >= target_px * 0.85:
                caption = _calendar_label(current, kind, previous_label_time)
                ticks.append((index, caption))
                previous_x = position
                previous_label_time = current
        previous_bucket = bucket
    if not ticks:
        # Very small or heavily gapped windows still deserve one readable
        # date/time label; it must correspond to a real observed candle.
        midpoint = (first + last - 1) // 2
        middle = localize(candles[midpoint].time)
        ticks.append((midpoint, _calendar_label(middle, kind, prev)))
    return ticks
