"""Rule 1's independent FVG attachments and chronological reversal gate.

The attachment rules follow v2.09 PendingFlowFvg/RuleOneFvg. No ticker,
capture date or screenshot value is special-cased.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .engine import Candidate, FVG, Movement, StructurePoint
from .liquidity import Capture, serialize_capture

if TYPE_CHECKING:
    from .market_data import Candle


@dataclass(slots=True)
class PendingAttachment:
    zone: FVG
    origin: int
    origin_bar: int | None
    origin_price: float
    low: float
    high: float
    low_i: int
    high_i: int
    ready: bool
    chain: int | None
    outside_first: bool


class RuleOneTracker:
    def __init__(self):
        self.pending: list[PendingAttachment] = []
        self.candidates: list[Candidate] = []
        self.previous_raw: StructurePoint | None = None
        self.previous_leg: Movement | None = None
        self.guard_key = None

    def reset(self):
        # Pine retains unresolved attachments across flow changes.
        self.candidates.clear()
        self.guard_key = None

    def _detect(self, zone, raw, leg, bars, i):
        prev, prior = self.previous_raw, self.previous_leg
        if prev is None or prior is None or i < 2:
            return
        side = zone.side
        normal = prior.side == side and prior.start is not None and prior.base is not None
        current = leg.side == side and leg.start is not None and leg.start <= zone.impulse
        outside = prev.outside_dir != 0
        first = outside and prev.outside_dir == side
        raw_impulse = prev.direction == side
        if first:
            origin, price = prev.outside_start_i, prev.outside_base
        elif outside and raw_impulse:
            origin, price = prev.start_i, prev.base
        elif normal:
            origin, price = prior.start, prior.base
        elif current:
            origin, price = leg.start, leg.base
        elif raw_impulse:
            origin, price = prev.start_i, prev.base
        else:
            origin = (i - 2 if (bars[i - 2].low <= bars[i - 1].low if side == 1
                                else bars[i - 2].high >= bars[i - 1].high) else i - 1)
            price = bars[origin].low if side == 1 else bars[origin].high
        if origin is None or price is None:
            return
        origin_bar = (prev.outside_start_i if first else
                      prev.start_i if raw_impulse and prev.start_i == origin else
                      raw.start_i if raw.direction == side and raw.start_i == origin else None)
        tip_bar = (prev.start_i if first else prev.tip_i if raw_impulse else
                   raw.tip_i if raw.direction == side else None)
        distinct = origin_bar is not None and tip_bar is not None and tip_bar > origin_bar
        ready = distinct if outside else normal or current
        tip, tip_i = ((prev.base, prev.start_i) if first else
                      (prior.tip, prior.tip_i) if normal else
                      (leg.tip, leg.tip_i) if current else
                      (bars[i - 1].high if side == 1 else bars[i - 1].low, i - 1))
        lo, hi = min(price, tip, bars[i].low), max(price, tip, bars[i].high)
        lo_i = origin if price <= tip and price <= bars[i].low else tip_i if tip <= bars[i].low else i
        hi_i = origin if price >= tip and price >= bars[i].high else tip_i if tip >= bars[i].high else i
        self.pending.append(PendingAttachment(
            zone, origin, origin_bar, price, lo, hi, lo_i, hi_i,
            ready, prev.outside_start_i if outside else None, first))

    def step(self, *, bars: list[Candle], i: int, raw: StructurePoint,
             leg: Movement, zone: FVG | None, side: int, start: int | None,
             guard: FVG | None, broken_at: int | None,
             capture: Capture | None, require_higher: bool):
        key = None if guard is None else (guard.created, guard.impulse, guard.side,
                                           guard.top, guard.bottom)
        if key != self.guard_key:
            self.candidates.clear()
            self.guard_key = key
        if zone is not None:
            self._detect(zone, raw, leg, bars, i)
        waiting = []
        for saved in self.pending:
            if i > saved.zone.created:
                if bars[i].low < saved.low:
                    saved.low, saved.low_i = bars[i].low, i
                if bars[i].high > saved.high:
                    saved.high, saved.high_i = bars[i].high, i
            same_chain = (saved.chain is not None and raw.outside_dir != 0
                          and raw.outside_start_i == saved.chain)
            if not saved.ready and same_chain:
                if saved.outside_first:
                    saved.ready = (raw.outside_start_i is not None and raw.start_i is not None
                                   and raw.start_i > raw.outside_start_i)
                elif raw.direction == saved.zone.side:
                    saved.origin, saved.origin_bar, saved.origin_price = raw.start_i, raw.start_i, raw.base
                    saved.ready = raw.tip_i is not None and raw.start_i is not None and raw.tip_i > raw.start_i
            same_raw = raw.direction == saved.zone.side and raw.start_i == saved.origin
            if not saved.ready and same_raw:
                saved.origin_bar, saved.origin_price = raw.start_i, raw.base
                saved.ready = raw.tip_i is not None and raw.start_i is not None and raw.tip_i > raw.start_i
            if not saved.ready and saved.origin_bar is None and saved.chain is None:
                if (raw.direction == saved.zone.side and raw.start_i is not None
                        and raw.tip_i is not None and raw.start_i <= saved.zone.impulse <= raw.tip_i
                        and raw.tip_i > raw.start_i):
                    saved.origin, saved.origin_bar, saved.origin_price = raw.start_i, raw.start_i, raw.base
                    saved.ready = True
            stale = ((start is not None and saved.zone.impulse < start)
                     or saved.origin > saved.zone.impulse)
            dead = saved.zone.broken is not None and saved.zone.broken <= i
            if (saved.ready and not stale and not dead and side and start is not None
                    and saved.zone.side == -side and saved.origin >= start
                    and guard is not None and saved.zone.impulse > guard.impulse):
                lo, hi = min(saved.origin_price, saved.low), max(saved.origin_price, saved.high)
                self.candidates.append(Candidate(
                    saved.zone, saved.origin, lo, hi,
                    saved.origin if saved.origin_price < saved.low else saved.low_i,
                    saved.origin if saved.origin_price > saved.high else saved.high_i,
                    saved.zone.created))
            detached = (not same_chain and not same_raw and raw.start_i is not None
                        and raw.start_i > saved.zone.impulse)
            if not (saved.ready or stale or dead or detached):
                waiting.append(saved)
        self.pending = waiting
        live = []
        for cand in self.candidates:
            invalid = (guard is None or start is None or cand.start < start
                       or cand.zone.side != -side or cand.zone.impulse <= guard.impulse
                       or cand.zone.broken is not None and cand.zone.broken <= i)
            if invalid:
                continue
            if bars[i].low < cand.low:
                cand.low, cand.low_i = bars[i].low, i
            if bars[i].high > cand.high:
                cand.high, cand.high_i = bars[i].high, i
            live.append(cand)
        self.candidates = live
        self.previous_raw, self.previous_leg = raw, replace(leg)
        eligible = [c for c in live if not require_higher or
                    capture is not None and bars[c.created].time > capture.captured_at]
        pool = eligible or live
        chosen = max(enumerate(pool), key=lambda item: (item[1].created, item[0]))[1] if pool else None
        working = None
        if chosen is not None:
            for cand in live:
                if cand.start == chosen.start and (working is None or
                        (cand.zone.top > working.top if cand.zone.side == 1
                         else cand.zone.bottom < working.bottom)):
                    working = cand.zone
        capture_ok = capture is not None and capture.side == -side
        checks = {
            "flow_initialized": side in (-1, 1),
            "first_fvg_present": guard is not None,
            "first_fvg_broken": broken_at is not None,
            "opposite_fvg_ready": chosen is not None,
            "opposite_liquidity_capture": not require_higher or capture_ok,
            "break_on_or_after_capture": not require_higher or
                capture_ok and broken_at is not None and bars[broken_at].time >= capture.captured_at,
            "opposite_fvg_strictly_after_capture": not require_higher or
                capture_ok and chosen is not None and bars[chosen.created].time > capture.captured_at,
        }
        report = {
            "requires_higher": require_higher, "checks": checks,
            "blockers": [name for name, passed in checks.items() if not passed],
            "higher_capture": serialize_capture(capture),
            "first_fvg": None if guard is None else {"bottom": guard.bottom, "top": guard.top},
            "first_broken_at": bars[broken_at].time.isoformat() if broken_at is not None else None,
            "opposite_fvg": None if chosen is None else {
                "bottom": chosen.zone.bottom, "top": chosen.zone.top,
                "formed_at": bars[chosen.created].time.isoformat(),
                "range_start_at": bars[chosen.start].time.isoformat()},
            "eligible": all(checks.values()), "outcome": "BLOCKED", "applied": False,
        }
        return chosen, working, report
