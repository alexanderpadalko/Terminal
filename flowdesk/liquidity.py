"""Closed-source liquidity captures consumed by Rule 1 on H1/H4/D1/W1.

Sessions and initial history still require comparison with the user's Pine.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .market_data import Candle, candle_close_time

if TYPE_CHECKING:
    from .engine import Analysis

SOURCES = ("MN1", "W1", "D1")  # Most distant -> closest (tie goes to closest).
HIGHER_SOURCES = {"H1": SOURCES, "H4": SOURCES, "D1": ("MN1", "W1"),
                  "W1": ("MN1",), "MN1": ()}


def period_end(candle: Candle, tf: str) -> datetime:
    """Calendar source-bar close, not a guessed close from the next trade."""
    if tf not in (*SOURCES,'H4'):
        raise ValueError("Unknown higher timeframe")
    return candle_close_time(candle, tf)


@dataclass(slots=True, frozen=True)
class Capture:
    source_tf: str
    side: int  # +1=buy-side capture / expects LONG, -1=short-side capture
    captured_at: datetime  # H1 candle *open*, like Pine's time
    kind: str  # MIN/IDM, MAX/IDM, or FVG
    level: float
    from_fvg: bool
    zone_top: float | None = None
    zone_bottom: float | None = None
    detail: str = ''
    audit: str = ''


@dataclass(slots=True)
class CaptureState:
    h1_open: datetime
    h1_closed_at: datetime
    source_status: dict[str, str]
    selected: Capture | None
    available_sources: int
    conflicts: tuple[str, ...] = ()
    # Only NEW events on this H1 bar. Active capture may persist for many bars.
    new_events: tuple[Capture, ...] = ()
    # Historical captures are NOT independently active long/short signals.
    # Per Pine, the final entry of EACH source stack is active; then the newest
    # across sources wins. Older entries can reappear only after FVG cancellation.
    source_active: dict[str, Capture | None] = field(default_factory=dict)
    source_histories: dict[str, tuple[Capture, ...]] = field(default_factory=dict)
    cancelled: tuple[Capture, ...] = ()
    restored: tuple[Capture, ...] = ()
    selection_reason: str = ""
    consumer_tf: str = "H1"
    confirmed: bool = True
    audit_text: str = ''


@dataclass(slots=True)
class _SourceState:
    captured: list[Capture] = field(default_factory=list)
    high_key: int | None = None
    low_key: int | None = None
    high_swept: bool = False
    low_swept: bool = False
    was_touched: bool = False
    touched_zone: tuple[int, float, float] | None = None
    last_validated: int = -1

    def current(self) -> Capture | None:
        return self.captured[-1] if self.captured else None


def select_pine_capture(source_active: dict[str, Capture | None]) -> Capture | None:
    """Pine f_newerCapture(): select ONE newest event; D1 wins equal-time ties.

    A suppressed opposite event must NOT confirm Rule 1, even if the source
    history retains it for restoration after a later FVG cancellation.
    """
    chosen = None
    for tf in SOURCES:
        candidate = source_active.get(tf)
        if candidate is not None and (chosen is None or
                                      candidate.captured_at >= chosen.captured_at):
            chosen = candidate
    return chosen


def audit_higher_captures(h1: "Analysis", sources: dict[str, "Analysis"],
                          *, asof: datetime | None = None, settings=None) -> list[CaptureState]:
    """Project each strictly higher source after its close on the consumer TF.

    Matches Pine's important safeguards: strict sweeps, no ambiguous both-side
    capture, captured structural levels persist, FVG captures are rescinded on
    later *source-bar* closes, and equal-time ties prefer nearest higher TF.

    This preliminary model is intentionally not advertised as a parity-exact
    clone of request.security() / barmerge session behavior.
    """
    if settings is not None:
        from .reference_liquidity import audit_captures
        return audit_captures(h1,sources,asof=asof,settings=settings)
    if h1.timeframe not in HIGHER_SOURCES:
        raise ValueError("Higher liquidity requires H1/H4/D1/W1/MN1 consumer")
    source_tfs = HIGHER_SOURCES[h1.timeframe]
    states = {tf: _SourceState() for tf in source_tfs}
    bars_by_tf: dict[str, list[Candle]] = {}
    ends: dict[str, list[datetime]] = {}
    for tf in source_tfs:
        src = sources.get(tf)
        if src is not None and src.timeframe == tf and len(src.candles) == len(src.pairs) == len(src.flows):
            bars_by_tf[tf] = src.candles
            ends[tf] = [period_end(c, tf) for c in src.candles]

    result: list[CaptureState] = []
    for bar in h1.candles:
        closed_at = candle_close_time(bar, h1.timeframe)
        if asof is not None and closed_at > asof:
            result.append(CaptureState(bar.time, closed_at,
                                       {tf: f"{h1.timeframe}_NOT_CLOSED" for tf in source_tfs},
                                       None, 0, consumer_tf=h1.timeframe, confirmed=False))
            continue
        statuses: dict[str, str] = {}
        current_new: list[Capture] = []
        cancelled: list[Capture] = []
        restored: list[Capture] = []
        conflicts: list[str] = []
        eligible = 0
        for tf in source_tfs:
            src = sources.get(tf)
            if tf not in bars_by_tf or not src or not src.candles:
                statuses[tf] = "NO_DATA"
                continue
            e = ends[tf]
            # Evaluate H1 against an already closed higher candle only.
            src_i = bisect_right(e, bar.time) - 1
            if src_i < 0:
                statuses[tf] = "AWAIT_HIGHER_CLOSE"
                continue
            eligible += 1
            snapshot = src.flows[src_i]
            pair = src.pairs[src_i]
            st = states[tf]
            statuses[tf] = "READY"

            # Higher TF close at this H1 close confirms rejection of an older
            # FVG capture; structural sweeps have no close-based cancellation.
            last_closed = bisect_right(e, closed_at) - 1
            closing_zone_rejected = False
            if last_closed > src_i and snapshot.guard is not None:
                for j in range(src_i + 1, last_closed + 1):
                    newer_close = src.candles[j].close
                    if ((snapshot.side == 1 and newer_close <= snapshot.guard.bottom)
                            or (snapshot.side == -1 and newer_close >= snapshot.guard.top)):
                        closing_zone_rejected = True
            if last_closed > st.last_validated:
                # Pine f_flowLiquidityEvents cancels FVG captures ONLY on the
                # close of their own higher TF, then exposes the newest entry
                # still present in that source's history stack.
                for j in range(st.last_validated + 1, last_closed + 1):
                    latest = src.candles[j]
                    t = e[j]
                    before = st.current()
                    kept = []
                    for old in st.captured:
                        rejected = (old.from_fvg and t > old.captured_at and
                                    (latest.close <= old.zone_bottom if old.side == 1
                                     else latest.close >= old.zone_top))
                        if rejected:
                            cancelled.append(old)
                        else:
                            kept.append(old)
                    st.captured = kept
                    if before != st.current() and st.current() is not None:
                        restored.append(st.current())
                st.last_validated = last_closed

            if pair.high_key != st.high_key:
                st.high_key, st.high_swept = pair.high_key, False
            if pair.low_key != st.low_key:
                st.low_key, st.low_swept = pair.low_key, False
            swept_high = pair.high is not None and not st.high_swept and bar.high > pair.high
            swept_low = pair.low is not None and not st.low_swept and bar.low < pair.low
            if swept_high:
                st.high_swept = True
            if swept_low:
                st.low_swept = True

            guard = snapshot.guard
            active_guard = (guard is not None and snapshot.side != 0
                            and not snapshot.guard_broken and snapshot.lo is not None
                            and snapshot.hi is not None and guard.top >= snapshot.lo
                            and guard.bottom <= snapshot.hi)
            touched = (active_guard and bar.high > guard.bottom and bar.low < guard.top
                       and (bar.low < guard.top if snapshot.side == 1 else bar.high > guard.bottom))
            zone = (snapshot.side, guard.top, guard.bottom) if active_guard else None
            new_touch = (touched and not snapshot.guard_balanced and
                         not closing_zone_rejected and
                         (not st.was_touched or zone != st.touched_zone))
            st.was_touched, st.touched_zone = touched, zone
            buy = swept_low or (new_touch and snapshot.side == 1)
            sell = swept_high or (new_touch and snapshot.side == -1)
            if buy and sell:
                st.captured.clear()
                conflicts.append(tf)
                statuses[tf] = "CONFLICT_BOTH_SIDES"
                continue
            if not buy and not sell:
                continue
            side = 1 if buy else -1
            is_sweep = swept_low if buy else swept_high
            kind = ("MIN/IDM" if buy else "MAX/IDM") if is_sweep else "FVG"
            level = (pair.low if buy else pair.high) if is_sweep else (guard.top if buy else guard.bottom)
            new = Capture(tf, side, bar.time, kind, level, not is_sweep,
                          None if is_sweep else guard.top,
                          None if is_sweep else guard.bottom)
            if is_sweep:
                st.captured.clear()  # MAX/MIN/IDM outranks prior FVG captures
            st.captured.append(new)
            current_new.append(new)

        # Do NOT independently promote the latest long and latest short:
        # Pine chooses one active direction after reducing each source stack.
        active = {tf: states[tf].current() for tf in source_tfs}
        picked = select_pine_capture(active)
        previous = result[-1].selected if result else None
        if picked == previous:
            reason = "UNCHANGED"
        elif picked in restored:
            reason = "RESTORED_AFTER_SOURCE_FVG_CANCEL"
        elif picked is None:
            reason = "NO_ACTIVE_CONFIRMATION"
        elif picked in current_new:
            reason = "NEW_CAPTURE_SELECTED"
        elif cancelled:
            reason = "SOURCE_FVG_CANCEL_FALLBACK"
        else:
            reason = "SOURCE_PRIORITY_CHANGED"
        histories = {tf: tuple(states[tf].captured) for tf in source_tfs}
        result.append(CaptureState(bar.time, closed_at, statuses, picked, eligible,
                                   tuple(conflicts), tuple(current_new), active, histories,
                                   tuple(cancelled), tuple(restored), reason, h1.timeframe))
    return result


def serialize_capture(c: Capture | None) -> dict | None:
    if c is None:
        return None
    return {"source_tf": c.source_tf, "side": c.side, "captured_at": c.captured_at.isoformat(),
            "kind": c.kind, "level": c.level, "from_fvg": c.from_fvg,
            "zone_top": c.zone_top, "zone_bottom": c.zone_bottom}


def serialize_audit(audit: list[CaptureState]) -> dict:
    # An unfinished H1 may be present in ISS. Never display it as the latest
    # *confirmed* higher-liquidity state; use the last closed H1 instead.
    last_closed = next((s for s in reversed(audit)
                        if s.confirmed), None)
    return {
        "status": "RULE_1_CAPTURE_INPUT_PARITY_UNVERIFIED",
        "consumer_tf": audit[0].consumer_tf if audit else None,
        "note": "Подтверждения используются правилом №1. Сессии, история и баровые границы ещё требуют сверки с Pine.",
        "bars": len(audit),
        "events": [{"bar_open": s.h1_open.isoformat(), "bar_closed_at": s.h1_closed_at.isoformat(),
                    "h1_open": s.h1_open.isoformat(), "h1_closed_at": s.h1_closed_at.isoformat(),
                    "capture": serialize_capture(c)}
                   for s in audit for c in s.new_events],
        "conflicts": [{"h1_open": s.h1_open.isoformat(), "source_tf": tf}
                      for s in audit for tf in s.conflicts],
        # Selection transitions and reversions are inspectable without
        # incorrectly claiming that two opposite confirmations are both live.
        "selection_changes": [{
            "h1_open": s.h1_open.isoformat(),
            "selected": serialize_capture(s.selected),
            "selected_long": serialize_capture(s.selected if s.selected and s.selected.side == 1 else None),
            "selected_short": serialize_capture(s.selected if s.selected and s.selected.side == -1 else None),
            "source_active": {tf: serialize_capture(s.source_active.get(tf)) for tf in SOURCES},
            "source_history": {tf: [serialize_capture(c) for c in s.source_histories.get(tf, ())]
                               for tf in SOURCES},
            "cancelled": [serialize_capture(c) for c in s.cancelled],
            "restored": [serialize_capture(c) for c in s.restored],
            "reason": s.selection_reason,
        } for s in audit if s.selection_reason != "UNCHANGED" and
            s.confirmed],
        "latest": (None if last_closed is None else {
            "bar_open": last_closed.h1_open.isoformat(),
            "bar_closed_at": last_closed.h1_closed_at.isoformat(),
            "h1_open": last_closed.h1_open.isoformat(),
            "h1_closed_at": last_closed.h1_closed_at.isoformat(),
            "source_status": last_closed.source_status,
            "available_sources": last_closed.available_sources,
            "selected_capture": serialize_capture(last_closed.selected),
            "selected_long": serialize_capture(last_closed.selected if last_closed.selected and
                                                last_closed.selected.side == 1 else None),
            "selected_short": serialize_capture(last_closed.selected if last_closed.selected and
                                                 last_closed.selected.side == -1 else None),
            "source_active": {tf: serialize_capture(last_closed.source_active.get(tf)) for tf in SOURCES},
            "source_history": {tf: [serialize_capture(c) for c in last_closed.source_histories.get(tf, ())]
                               for tf in SOURCES},
            "reason": last_closed.selection_reason,
        }),
    }

