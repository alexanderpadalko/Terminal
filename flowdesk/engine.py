"""Candle-by-candle analysis core; no broker/orders.

The *structure* state machine follows the v2.04 H1 Pine structure/mother-bar
rules. FVG creation and invalidation follow Pine's three-candle conditions.
Flow approximates Rules 2 & 3 and tracks eligible candidates. Rule 1's MTF
liquidity confirmation is not yet production-parity validated, and cannot
silently issue a signal. This is a visualization/diagnostic port, NOT a trading
strategy or a verified Pine reimplementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from .market_data import Candle


@dataclass(slots=True)
class Segment:
    a: int
    pa: float
    b: int
    pb: float
    side: int
    confirmed: bool = True


@dataclass(slots=True)
class StructurePoint:
    i: int
    direction: int
    start_i: Optional[int]
    base: Optional[float]
    tip_i: Optional[int]
    tip: Optional[float]
    outside_dir: int
    outside_start_i: Optional[int]
    outside_base: Optional[float]
    turn_up: bool = False
    turn_down: bool = False


class StructureEngine:
    def __init__(self):
        self.direction = 0
        self.outside_origin = 0
        self.outside_start: Optional[int] = None
        self.outside_base: Optional[float] = None
        self.extreme: Optional[float] = None
        self.extreme_i: Optional[int] = None
        self.mother_hi: Optional[float] = None
        self.mother_lo: Optional[float] = None
        self.pivot: Optional[float] = None
        self.pivot_i: Optional[int] = None
        self.initial_hi: Optional[float] = None
        self.initial_lo: Optional[float] = None
        self.first_i: Optional[int] = None
        self.segments: list[Segment] = []
        self.outside_base_segment: Optional[Segment] = None

    def step(self, bar: Candle, i: int) -> StructurePoint:
        up = down = False
        hi, lo = bar.high, bar.low
        if self.initial_hi is None:
            self.initial_hi, self.initial_lo, self.first_i = hi, lo, i
        elif self.direction == 0:
            bh, bl = hi > self.initial_hi, lo < self.initial_lo
            chosen = 0
            if bh and not bl:
                chosen = 1
            elif bl and not bh:
                chosen = -1
            elif bh and bl:
                long = hi - self.initial_hi >= self.initial_lo - lo
                self.outside_start = self.first_i
                self.outside_base = self.initial_lo if long else self.initial_hi
                self.outside_origin = 1 if long else -1
                self.outside_base_segment = Segment(self.first_i, self.outside_base, i, hi if long else lo, self.outside_origin, False)
                chosen = -1 if long else 1
                self.direction = chosen
                self.pivot, self.pivot_i = (hi if long else lo), i
                self.extreme, self.extreme_i = (lo if long else hi), i
                self.mother_hi, self.mother_lo = hi, lo
                up, down = not long, long
            if chosen == 1 and not bl:
                self.direction = 1
                self.pivot, self.pivot_i = self.initial_lo, self.first_i
                self.extreme, self.extreme_i = hi, i
                self.mother_hi, self.mother_lo = hi, lo
            elif chosen == -1 and not bh:
                self.direction = -1
                self.pivot, self.pivot_i = self.initial_hi, self.first_i
                self.extreme, self.extreme_i = lo, i
                self.mother_hi, self.mother_lo = hi, lo
        else:
            bh, bl = hi > self.mother_hi, lo < self.mother_lo
            if self.outside_origin != 0 and bh != bl and self.outside_base_segment:
                self.outside_base_segment.confirmed = True
                self.segments.append(self.outside_base_segment)
            if bh and bl:
                continuing = self.outside_origin != 0
                path_dir = self.outside_origin if continuing else self.direction
                if not continuing:
                    self.outside_origin = self.direction
                    self.outside_start = self.pivot_i
                    self.outside_base = self.pivot
                    self.outside_base_segment = Segment(self.pivot_i, self.pivot, i, hi if path_dir == 1 else lo, path_dir, False)
                    up, down = (path_dir == -1), (path_dir == 1)
                elif self.outside_base_segment is not None:
                    self.outside_base_segment.b = i
                    self.outside_base_segment.pb = hi if path_dir == 1 else lo
                if path_dir == 1:
                    self.pivot, self.pivot_i = hi, i
                    self.direction, self.extreme, self.extreme_i = -1, lo, i
                else:
                    self.pivot, self.pivot_i = lo, i
                    self.direction, self.extreme, self.extreme_i = 1, hi, i
                self.mother_hi, self.mother_lo = hi, lo
            elif self.direction == 1:
                if bl:
                    self.segments.append(Segment(self.pivot_i, self.pivot, self.extreme_i, self.extreme, 1))
                    self.pivot, self.pivot_i = self.extreme, self.extreme_i
                    self.direction, self.extreme, self.extreme_i = -1, lo, i
                    self.mother_hi, self.mother_lo = hi, lo
                    down = True
                    self.outside_origin, self.outside_start, self.outside_base = 0, None, None
                    self.outside_base_segment = None
                elif bh:
                    self.extreme, self.extreme_i = hi, i
                    self.mother_hi, self.mother_lo = hi, lo
                    self.outside_origin, self.outside_start, self.outside_base = 0, None, None
                    self.outside_base_segment = None
            else:
                if bh:
                    self.segments.append(Segment(self.pivot_i, self.pivot, self.extreme_i, self.extreme, -1))
                    self.pivot, self.pivot_i = self.extreme, self.extreme_i
                    self.direction, self.extreme, self.extreme_i = 1, hi, i
                    self.mother_hi, self.mother_lo = hi, lo
                    up = True
                    self.outside_origin, self.outside_start, self.outside_base = 0, None, None
                    self.outside_base_segment = None
                elif bl:
                    self.extreme, self.extreme_i = lo, i
                    self.mother_hi, self.mother_lo = hi, lo
                    self.outside_origin, self.outside_start, self.outside_base = 0, None, None
                    self.outside_base_segment = None
        return StructurePoint(i, self.direction, self.pivot_i, self.pivot, self.extreme_i, self.extreme,
                              self.outside_origin, self.outside_start, self.outside_base, up, down)

    def current_segments(self) -> list[Segment]:
        result = self.segments[:]
        if self.outside_base_segment is not None and not self.outside_base_segment.confirmed:
            result.append(self.outside_base_segment)
        if self.direction and self.pivot_i is not None and self.extreme_i is not None:
            result.append(Segment(self.pivot_i, self.pivot, self.extreme_i, self.extreme, self.direction, False))
        return result


@dataclass(slots=True)
class FVG:
    start: int
    impulse: int
    created: int
    top: float
    bottom: float
    side: int
    broken: Optional[int] = None
    balanced: bool = False
    source_tf: str = ""

    @property
    def active(self) -> bool:
        return self.broken is None

    def update(self, price: float, i: int):
        if self.broken is not None:
            return
        midpoint = (self.top + self.bottom) / 2
        if (self.side == 1 and price <= midpoint) or (self.side == -1 and price >= midpoint):
            self.balanced = True
        if (self.side == 1 and price <= self.bottom) or (self.side == -1 and price >= self.top):
            self.broken = i


class FVGEngine:
    def __init__(self, tf: str, exclude_gaps: bool = True):
        self.tf = tf
        self.exclude_gaps = exclude_gaps
        self.zones: list[FVG] = []
        self._bars: list[Candle] = []
        self._inside: list[bool] = []
        self._mother_hi: Optional[float] = None
        self._mother_lo: Optional[float] = None

    def step(self, bar: Candle) -> Optional[FVG]:
        i = len(self._bars)
        inside = self._mother_hi is not None and bar.high <= self._mother_hi and bar.low >= self._mother_lo
        if not inside:
            self._mother_hi, self._mother_lo = bar.high, bar.low
        for zone in self.zones:
            zone.update(bar.close, i)
        result = None
        if i >= 2:
            prev, prev2 = self._bars[-1], self._bars[-2]
            ignore_inside = self.tf in ("M1", "M5", "H1")
            eligible = not ignore_inside or (not inside and not self._inside[-1] and not self._inside[-2])
            gap = (bar.low > prev.high or bar.high < prev.low or prev.low > prev2.high or prev.high < prev2.low)
            if eligible and (not self.exclude_gaps or not gap):
                if bar.low > prev2.high:
                    result = FVG(i - 2, i - 1, i, bar.low, prev2.high, 1, source_tf=self.tf)
                elif bar.high < prev2.low:
                    result = FVG(i - 2, i - 1, i, prev2.low, bar.high, -1, source_tf=self.tf)
                if result:
                    self.zones.append(result)
        self._bars.append(bar)
        self._inside.append(inside)
        return result


@dataclass(slots=True)
class Movement:
    side: int = 0
    start: Optional[int] = None
    base: Optional[float] = None
    tip: Optional[float] = None
    tip_i: Optional[int] = None

    def feed(self, raw: StructurePoint):
        if raw.direction == 0 or raw.start_i is None or raw.tip_i is None or raw.tip_i <= raw.start_i:
            return
        if self.side == raw.direction:
            if (raw.direction == 1 and raw.tip > self.tip) or (raw.direction == -1 and raw.tip < self.tip):
                self.tip_i = raw.tip_i
            self.tip = (max(self.tip, raw.tip) if self.side == 1 else min(self.tip, raw.tip))
        else:
            self.side, self.start, self.base, self.tip, self.tip_i = raw.direction, raw.start_i, raw.base, raw.tip, raw.tip_i


@dataclass(slots=True)
class PairSnapshot:
    high: Optional[float] = None
    low: Optional[float] = None
    high_key: Optional[int] = None
    low_key: Optional[int] = None
    direction: int = 0


class PairEngine:
    """The v2.04 structural MAX/MIN + IDM pair used for higher liquidity."""
    def __init__(self):
        self.pair = PairSnapshot()
        self.candidate_high = self.candidate_low = None
        self.candidate_high_key = self.candidate_low_key = None
        self.seen_pivot = None
        self.seen_dir = 0

    def step(self, bar: Candle, raw: StructurePoint) -> PairSnapshot:
        s = self.pair
        d, pivot, pi, i = raw.direction, raw.base, raw.start_i, raw.i
        new_pivot = pi is not None and (pi != self.seen_pivot or d != self.seen_dir)
        current_outside = raw.outside_dir != 0 and pi == i and raw.outside_base is not None and raw.outside_start_i is not None
        if s.direction == 0 and d != 0 and pivot is not None:
            s.direction = d
            s.high = pivot if d == -1 else bar.high
            s.low = pivot if d == 1 else bar.low
            s.high_key = pi if d == -1 else i
            s.low_key = pi if d == 1 else i
            self.candidate_high, self.candidate_low = s.high, s.low
            self.candidate_high_key, self.candidate_low_key = s.high_key, s.low_key
        elif s.direction != 0:
            if current_outside:
                if raw.outside_dir == 1:
                    if bar.high > s.high:
                        s.high, s.high_key = bar.high, i
                        s.low, s.low_key, s.direction = raw.outside_base, raw.outside_start_i, 1
                        self.candidate_low, self.candidate_low_key = s.low, s.low_key
                    self.candidate_high, self.candidate_high_key = bar.high, i
                    if bar.low < s.low:
                        s.low, s.low_key = bar.low, i
                        s.high, s.high_key = self.candidate_high, self.candidate_high_key
                        s.direction = -1
                        self.candidate_low, self.candidate_low_key = s.low, s.low_key
                else:
                    if bar.low < s.low:
                        s.low, s.low_key = bar.low, i
                        s.high, s.high_key, s.direction = raw.outside_base, raw.outside_start_i, -1
                        self.candidate_high, self.candidate_high_key = s.high, s.high_key
                    self.candidate_low, self.candidate_low_key = bar.low, i
                    if bar.high > s.high:
                        s.high, s.high_key = bar.high, i
                        s.low, s.low_key = self.candidate_low, self.candidate_low_key
                        s.direction = 1
                        self.candidate_high, self.candidate_high_key = s.high, s.high_key
            else:
                if new_pivot:
                    if d == -1 and (self.candidate_high_key is None or pi >= self.candidate_high_key):
                        self.candidate_high, self.candidate_high_key = pivot, pi
                    elif d == 1 and (self.candidate_low_key is None or pi >= self.candidate_low_key):
                        self.candidate_low, self.candidate_low_key = pivot, pi
                if bar.high > s.high:
                    s.high, s.high_key = bar.high, i
                    s.low, s.low_key = self.candidate_low, self.candidate_low_key
                    s.direction = 1
                    self.candidate_high, self.candidate_high_key = s.high, s.high_key
                elif bar.low < s.low:
                    s.low, s.low_key = bar.low, i
                    s.high, s.high_key = self.candidate_high, self.candidate_high_key
                    s.direction = -1
                    self.candidate_low, self.candidate_low_key = s.low, s.low_key
        if new_pivot:
            self.seen_pivot, self.seen_dir = pi, d
        # Return immutable-in-practice copy so history does not alias.
        return PairSnapshot(s.high, s.low, s.high_key, s.low_key, s.direction)


@dataclass(slots=True)
class Candidate:
    zone: FVG
    start: int
    low: float
    high: float
    low_i: int
    high_i: int
    created: int


@dataclass(slots=True)
class FlowState:
    i: int
    side: int = 0
    lo: Optional[float] = None
    hi: Optional[float] = None
    start: Optional[int] = None
    previous: Optional[tuple] = None
    rule: int = 0
    diagnosis: str = "Ожидание структурного плеча"
    guard: Optional[FVG] = None
    guard_broken: bool = False
    liquidity: Optional[PairSnapshot] = None
    source_tf: str = ""
    source_bar_time: Optional[datetime] = None
    source_confirmed_at: Optional[datetime] = None
    # Provenance of the displayed H1 interval, even when mapped to M1/M5.
    range_start_at: Optional[datetime] = None
    range_low_at: Optional[datetime] = None
    range_high_at: Optional[datetime] = None
    last_change_at: Optional[datetime] = None
    last_change_rule: int = 0
    range_origin: str = ""


class FlowEngine:
    """Conservative visual Rule 2/3 engine; Rule 1 is a parity-gated TODO.

    This core DOES NOT label Rule 1 as confirmed without a verified MTF
    liquidity stream. No real-time trade or order execution is implemented.
    """
    def __init__(self):
        self.movement = Movement()
        self.side = 0
        self.lo = self.hi = None
        self.lo_i = self.hi_i = None
        self.start = None
        self.previous = None
        self.guard: Optional[FVG] = None
        self.guard_broken = False
        self.guard_broken_at = None
        self.candidate_cont: Optional[Candidate] = None
        self.candidate_rev: Optional[Candidate] = None
        self.extension_i: Optional[int] = None
        self.extension_leg_start: Optional[int] = None
        self.structural_reverse_lo = self.structural_reverse_hi = None
        self.structural_reverse_lo_i = self.structural_reverse_hi_i = None
        self.origin_locked = False
        self.seed_impulse = None
        self.flow_has_fvg = False
        self.last_change_i: Optional[int] = None
        self.last_change_rule: int = 0
        self.range_origin = ""
        self.history: list[FlowState] = []

    def _zone_candidate(self, zone: FVG, movement: Movement, candles: list[Candle], i: int) -> Optional[Candidate]:
        if movement.side != zone.side or movement.start is None or movement.start > zone.impulse:
            return None
        start = movement.start
        if movement.tip_i is None or movement.tip_i <= start:
            return None
        # Data from impulse base through formation of the zone.
        part = candles[start:i+1]
        low = min([movement.base] + [b.low for b in part])
        high = max([movement.base] + [b.high for b in part])
        low_i = min(range(start, i+1), key=lambda idx: candles[idx].low)
        high_i = max(range(start, i+1), key=lambda idx: candles[idx].high)
        return Candidate(zone, start, low, high, low_i, high_i, i)

    def step(self, bar: Candle, i: int, structure: StructurePoint, zone: Optional[FVG], candles: list[Candle], pair: PairSnapshot) -> FlowState:
        self.movement.feed(structure)
        movement = self.movement
        rule = 0
        message = ""
        if self.side == 0 and movement.side != 0 and movement.start is not None:
            self.side = movement.side
            self.start = movement.start
            self.lo = min(movement.base, movement.tip, bar.low)
            self.hi = max(movement.base, movement.tip, bar.high)
            self.lo_i = movement.start if movement.base <= movement.tip and movement.base <= bar.low else (movement.tip_i if movement.tip <= bar.low else i)
            self.hi_i = movement.start if movement.base >= movement.tip and movement.base >= bar.high else (movement.tip_i if movement.tip >= bar.high else i)
            self.origin_locked = True
            self.structural_reverse_lo = bar.low
            self.structural_reverse_hi = bar.high
            self.structural_reverse_lo_i = self.structural_reverse_hi_i = i
            rule, message = 0, "Первое подтверждённое структурное плечо"
            self.last_change_i = i
            self.last_change_rule = 0
            self.range_origin = "Старт по структурному плечу"

        if self.side and self.guard is not None and self.guard.broken is not None:
            self.guard_broken = True
            self.guard_broken_at = self.guard.broken
        broken = self.side != 0 and ((self.side == 1 and bar.close < self.lo) or (self.side == -1 and bar.close > self.hi))
        old_range = (self.start, self.lo, self.hi)
        before_hi, before_lo = self.hi, self.lo
        extending_start = movement.start if movement.side == self.side else None
        if self.side == 1 and bar.high > self.hi:
            if self.extension_leg_start != extending_start:
                self.extension_i = i
                self.extension_leg_start = extending_start
            self.hi, self.hi_i = bar.high, i
            self.structural_reverse_lo, self.structural_reverse_hi = bar.low, bar.high
            self.structural_reverse_lo_i = self.structural_reverse_hi_i = i
        elif self.side == -1 and bar.low < self.lo:
            if self.extension_leg_start != extending_start:
                self.extension_i = i
                self.extension_leg_start = extending_start
            self.lo, self.lo_i = bar.low, i
            self.structural_reverse_lo, self.structural_reverse_hi = bar.low, bar.high
            self.structural_reverse_lo_i = self.structural_reverse_hi_i = i
        if self.side:
            if self.structural_reverse_lo is None or bar.low < self.structural_reverse_lo:
                self.structural_reverse_lo, self.structural_reverse_lo_i = bar.low, i
            if self.structural_reverse_hi is None or bar.high > self.structural_reverse_hi:
                self.structural_reverse_hi, self.structural_reverse_hi_i = bar.high, i

        for name in ("candidate_cont", "candidate_rev"):
            candidate = getattr(self, name)
            if candidate is None:
                continue
            still_alive = (bar.close >= candidate.low if self.side == 1 else bar.close <= candidate.high) if name == "candidate_cont" else (bar.close <= candidate.high if self.side == 1 else bar.close >= candidate.low)
            if not still_alive or (candidate.zone.broken is not None and candidate.zone.broken <= i):
                setattr(self, name, None)
            else:
                if bar.low < candidate.low:
                    candidate.low, candidate.low_i = bar.low, i
                if bar.high > candidate.high:
                    candidate.high, candidate.high_i = bar.high, i

        if zone:
            cand = self._zone_candidate(zone, movement, candles, i)
            if cand:
                if self.side == zone.side and self.start is not None and zone.impulse >= self.start:
                    closer = self.guard is None or (zone.top > self.guard.top if self.side == 1 else zone.bottom < self.guard.bottom)
                    if closer:
                        self.guard = zone
                        self.guard_broken = False
                        self.guard_broken_at = None
                        self.flow_has_fvg = True
                if self.start is not None and cand.start > self.start:
                    if self.side == zone.side and not (self.seed_impulse is not None and cand.start <= self.seed_impulse):
                        if self.candidate_cont is None or cand.start > self.candidate_cont.start:
                            self.candidate_cont = cand
                        elif cand.start == self.candidate_cont.start:
                            z = self.candidate_cont.zone
                            if (zone.top > z.top if zone.side == 1 else zone.bottom < z.bottom):
                                self.candidate_cont.zone = zone
                    elif self.side == -zone.side:
                        if self.candidate_rev is None or cand.start >= self.candidate_rev.start:
                            self.candidate_rev = cand
        # Priority 2, then 3.  Rule 1 relies on multi-timeframe liquidity and
        # is intentionally withheld until parity validation is available.
        new_dir = 0
        new_start = new_lo = new_hi = new_lo_i = new_hi_i = None
        selected_zone = None
        if self.side and broken:
            rule = 2
            new_dir = -self.side
            if self.candidate_rev is not None:
                cand = self.candidate_rev
                new_start, new_lo, new_hi = cand.start, cand.low, cand.high
                new_lo_i, new_hi_i = cand.low_i, cand.high_i
                selected_zone = cand.zone
                message = "№2: закрытие за границей потока; передан встречный FVG"
            else:
                new_start = self.lo_i if new_dir == 1 else self.hi_i
                new_lo = self.lo if new_dir == 1 else self.structural_reverse_lo
                new_hi = self.hi if new_dir == -1 else self.structural_reverse_hi
                new_lo_i = self.lo_i if new_dir == 1 else self.structural_reverse_lo_i
                new_hi_i = self.hi_i if new_dir == -1 else self.structural_reverse_hi_i
                message = "№2: закрытие за границей; встречного FVG нет"
        elif (self.candidate_cont is not None and self.extension_i is not None and
              self.extension_i >= self.candidate_cont.start):
            rule = 3
            new_dir = self.side
            cand = self.candidate_cont
            new_start, new_lo, new_hi = cand.start, cand.low, cand.high
            new_lo_i, new_hi_i = cand.low_i, cand.high_i
            selected_zone = cand.zone
            message = "№3: FVG подходящего плеча и расширение тенью"
        if new_dir:
            if self.side:
                self.previous = (old_range[0], i - 1, old_range[1], old_range[2], self.side)
            self.origin_locked = new_dir != self.side or selected_zone is None
            self.side, self.start, self.lo, self.hi = new_dir, new_start, new_lo, new_hi
            self.lo_i, self.hi_i = new_lo_i, new_hi_i
            self.guard = selected_zone
            self.guard_broken = bool(selected_zone and selected_zone.broken is not None)
            self.guard_broken_at = selected_zone.broken if selected_zone else None
            self.flow_has_fvg = selected_zone is not None
            # Match Pine flowSeedImpulse: an unguarded Rule 2 has no
            # reversalImpulse (na). 0.2.1 incorrectly substituted i - 1,
            # rejecting the first eligible continuation FVG as "old".
            self.seed_impulse = (selected_zone.impulse if selected_zone is not None
                                 else None if rule == 2 else i - 1)
            self.structural_reverse_lo, self.structural_reverse_hi = bar.low, bar.high
            self.structural_reverse_lo_i = self.structural_reverse_hi_i = i
            self.candidate_cont = self.candidate_rev = None
            self.extension_i = self.extension_leg_start = None
            self.last_change_i = i
            self.last_change_rule = rule
            self.range_origin = message

        def at(index: Optional[int]) -> Optional[datetime]:
            return candles[index].time if index is not None and 0 <= index < len(candles) else None

        state = FlowState(i, self.side, self.lo, self.hi, self.start, self.previous, rule,
                          message or ("Лонг" if self.side == 1 else "Шорт" if self.side == -1 else "Инициализация"),
                          self.guard, self.guard_broken, pair,
                          range_start_at=at(self.start), range_low_at=at(self.lo_i),
                          range_high_at=at(self.hi_i), last_change_at=at(self.last_change_i),
                          last_change_rule=self.last_change_rule, range_origin=self.range_origin)
        self.history.append(state)
        return state


@dataclass(slots=True)
class Analysis:
    candles: list[Candle]
    segments: list[Segment]
    fvg: list[FVG]
    flows: list[FlowState]
    pairs: list[PairSnapshot]
    timeframe: str
    flow_source_tf: str = ""
    flow_status: str = "LOCAL_PRELIMINARY"
    flow_source_note: str = ""


def _project_h1_flows(
    lower: list[Candle], lower_tf: str, h1: Analysis | None,
    *, asof: datetime | None = None,
) -> list[FlowState]:
    """Project *closed* H1 snapshots onto M1/M5 without future data.

    Source H1 candles are stamped at the START of the hour. Their analysis is
    visible on the lower chart only at/after the end of that hour. The rule
    event is emitted once, on the lower candle whose closing time crosses the
    H1 close. An absent H1 source leaves the low-TF flow unknown; never replace
    it with the local lower-timeframe FlowEngine.

    NOTE: 'closed H1 candle' is not 'parity-verified Pine calculation'.
    """
    if lower_tf not in ("M1", "M5"):
        raise ValueError("Проекция H1 поддерживается только на M1/M5")
    if h1 is not None and h1.timeframe != "H1":
        raise ValueError("Для M1/M5 необходим именно ряд H1")
    if h1 is not None and len(h1.candles) != len(h1.flows):
        raise ValueError("История H1 повреждена: число свечей и потоков отличается")
    step = timedelta(minutes=1 if lower_tf == "M1" else 5)
    flows: list[FlowState] = []
    if h1 is None or not h1.candles:
        return [FlowState(i, diagnosis="НЕТ H1: поток не рассчитывается на M1/M5",
                          source_tf="H1") for i in range(len(lower))]
    h1_ends = [b.time + timedelta(hours=1) for b in h1.candles]
    h1_starts = [b.time for b in h1.candles]
    lower_starts = [b.time for b in lower]
    prior_close = None
    for i, candle in enumerate(lower):
        chart_close = candle.time + step
        # The current unfinished lower candle may exist in ISS: it must not
        # unlock a future H1 snapshot merely because its nominal close is later.
        eligible_until = min(chart_close, asof) if asof is not None else chart_close
        source_idx = bisect_right(h1_ends, eligible_until) - 1
        if source_idx < 0:
            flows.append(FlowState(i, diagnosis="Ожидание закрытой свечи H1",
                                   source_tf="H1"))
            prior_close = chart_close
            continue
        h = h1.flows[source_idx]
        confirmed_at = h1_ends[source_idx]
        # Preserve old event timing: a Rule 2/3 event occurs on one lower
        # candle, not on every one of the 12/60 candles within an hour.
        event_now = (
            h.rule in (1, 2, 3) and confirmed_at <= chart_close and
            (prior_close is not None and prior_close < confirmed_at or
             prior_close is None and candle.time < confirmed_at)
        )
        rule = h.rule if event_now else 0
        status = (h.diagnosis if event_now else
                  "H1: " + ("Лонг" if h.side == 1 else "Шорт" if h.side == -1 else "Ожидание"))
        # Original H1 index numbers cannot be plotted as M5/M1 x-coordinates.
        # Translate both current and previous flow indices via timestamps.
        start = None
        if h.start is not None and 0 <= h.start < len(h1_starts):
            start = bisect_left(lower_starts, h1_starts[h.start])
        previous = None
        if h.previous is not None:
            prev_start, prev_end, lo, hi, side = h.previous
            if (isinstance(prev_start, int) and isinstance(prev_end, int)
                    and 0 <= prev_start < len(h1_starts)
                    and 0 <= prev_end < len(h1_starts)):
                previous = (
                    bisect_left(lower_starts, h1_starts[prev_start]),
                    max(0, bisect_left(lower_starts, h1_ends[prev_end]) - 1),
                    lo, hi, side,
                )
        flows.append(FlowState(
            i=i, side=h.side, lo=h.lo, hi=h.hi, start=start,
            previous=previous, rule=rule, diagnosis=status,
            guard=h.guard, guard_broken=h.guard_broken, liquidity=h.liquidity,
            source_tf="H1", source_bar_time=h1_starts[source_idx],
            source_confirmed_at=confirmed_at,
            range_start_at=h.range_start_at, range_low_at=h.range_low_at,
            range_high_at=h.range_high_at, last_change_at=h.last_change_at,
            last_change_rule=h.last_change_rule, range_origin=h.range_origin,
        ))
        prior_close = chart_close
    return flows


def analyze(
    candles: list[Candle], timeframe: str = "H1", exclude_gaps: bool = True,
    *, higher_h1: Analysis | None = None, asof: datetime | None = None,
) -> Analysis:
    """Local structure/FVG; a separate H1 source is mandatory for low-TF flow."""
    if timeframe not in ("M1", "M5", "H1", "D1", "W1", "MN1"):
        raise ValueError("Неизвестный таймфрейм")
    structure = StructureEngine()
    fvg = FVGEngine(timeframe, exclude_gaps)
    pair = PairEngine()
    low_tf = timeframe in ("M1", "M5")
    flow = None if low_tf else FlowEngine()
    pairs = []
    for i, bar in enumerate(candles):
        sp = structure.step(bar, i)
        ps = pair.step(bar, sp)
        zone = fvg.step(bar)
        if flow is not None:
            flow.step(bar, i, sp, zone, candles, ps)
        pairs.append(ps)
    if low_tf:
        projected = _project_h1_flows(candles, timeframe, higher_h1, asof=asof)
        origin = "H1_CONFIRMED_CANDLES" if higher_h1 is not None and higher_h1.candles else "H1_UNAVAILABLE"
        note = "Поток H1; только закрытые H1 свечи" if higher_h1 is not None and higher_h1.candles else "Ряд H1 отсутствует; направление потока не рассчитывается"
        return Analysis(candles, structure.current_segments(), fvg.zones, projected, pairs,
                        timeframe, "H1", origin, note)
    return Analysis(candles, structure.current_segments(), fvg.zones, flow.history, pairs,
                    timeframe, timeframe, "LOCAL_PRELIMINARY", "Собственный поток, алгоритм ещё не сверен с Pine")
