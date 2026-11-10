"""Read-only H1 preview of Pine Rule 1 (first FVG + opposite FVG + HTF capture).

NO order creation and NO mutation of FlowEngine, FlowState or H1->M5 projection.
This is NOT a parity-verified Rule-1 implementation. In particular:
* higher D/W/M capture and bar-merge logic remains preliminary;
* the opposite-FVG origin is approximated using the Python StructureEngine;
* the Python flow candidates are evaluated at FVG formation, while Pine can
  defer attachment until the movement is confirmed.
The output is a per-closed-hour *hypothetical* event log, not trade signals.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .engine import FVG, Movement, StructureEngine
from .liquidity import Capture, CaptureState, SOURCES, serialize_capture

if TYPE_CHECKING:
    from .engine import Analysis, FlowState


@dataclass(slots=True)
class _OppositeCandidate:
    zone: FVG
    start: int
    formed: int
    low: float
    high: float


@dataclass(slots=True)
class ShadowRuleOneRow:
    h1_open: datetime
    h1_closed_at: datetime
    flow_side: int
    actual_rule: int
    outcome: str
    blockers: tuple[str, ...]
    checks: dict[str, bool]
    first_guard: dict | None = None
    first_broken_at: datetime | None = None
    opposite_fvg: dict | None = None
    higher_capture: Capture | None = None
    source_status: dict[str, str] = field(default_factory=dict)
    would_trigger: bool = False
    suppressed_opposite_capture: Capture | None = None
    suppressed_reason: str | None = None
    higher_source_active: dict[str, Capture | None] = field(default_factory=dict)
    higher_selection_reason: str = ""


def check_rule_one(*, side: int, guard_present: bool,
                   first_broken_at: datetime | None,
                   opposite_fvg: dict | None,
                   capture: Capture | None,
                   all_higher_ready: bool,
                   actual_rule: int) -> tuple[dict[str, bool], tuple[str, ...], str, bool]:
    """Evaluate the strict Pine chronology, returning ALL failed conditions.

    An opposite FVG created before the capture can be shown in diagnostics,
    but cannot satisfy Rule 1. A guard break on the capture bar IS allowed.
    Structural Rule 2 still outranks the hypothetical Rule 1.
    """
    valid_capture = capture is not None and capture.side == -side and side in (-1, 1)
    checks = {
        "flow_initialized": side in (-1, 1),
        "first_fvg_present": guard_present,
        "first_fvg_broken": first_broken_at is not None,
        "opposite_fvg_ready": opposite_fvg is not None,
        "higher_sources_ready": all_higher_ready,
        "opposite_liquidity_capture": valid_capture,
        "break_on_or_after_capture": (
            valid_capture and first_broken_at is not None
            and first_broken_at >= capture.captured_at
        ),
        "opposite_fvg_strictly_after_capture": (
            valid_capture and opposite_fvg is not None
            and opposite_fvg["formed_at"] > capture.captured_at
        ),
    }
    # Pine f_mtfBundle() does not require *all three* higher timeframes to
    # be present. Its one selected usable confirmation is sufficient. Keep
    # higher_sources_ready informational, not a false hard blocker.
    blockers = tuple(key for key, passes in checks.items()
                     if not passes and key != "higher_sources_ready")
    eligible = not blockers
    if eligible and actual_rule == 2:
        return checks, (), "OVERRIDDEN_BY_RULE_2", False
    if eligible and actual_rule == 3:
        return checks, (), "WOULD_PREEMPT_RULE_3", True
    if eligible:
        return checks, (), "WOULD_TRIGGER_RULE_1", True
    return checks, blockers, "BLOCKED", False


def _candidate_summary(c: _OppositeCandidate, bars: list) -> dict:
    return {
        "source": "H1",
        "formed_at": bars[c.formed].time,
        "movement_start_at": bars[c.start].time,
        "impulse_at": bars[c.zone.impulse].time,
        "fvg_top": c.zone.top,
        "fvg_bottom": c.zone.bottom,
        "range_lo": c.low,
        "range_hi": c.high,
    }


def audit_rule_one_shadow(h1: "Analysis", captures: list[CaptureState],
                          *, asof: datetime | None = None) -> list[ShadowRuleOneRow]:
    """Read-only replay; source captures are already aligned to H1 closed bars.

    The currently active Rule-2/3 flow remains the baseline; hypothetical
    Rule-1 events do NOT reset the baseline's flow epoch or future candidate
    histories, so subsequent hypothetical events are NOT a simulated Rule-1
    trading history. All time comparisons use H1 candle opens like Pine's time.
    """
    if h1.timeframe != "H1":
        raise ValueError("Rule 1 shadow requires H1")
    if len(captures) != len(h1.candles) or len(h1.flows) != len(h1.candles):
        raise ValueError("H1, capture and flow histories must have equal lengths")
    bars = h1.candles
    by_created: dict[int, list[FVG]] = defaultdict(list)
    for zone in h1.fvg:
        if 0 <= zone.created < len(bars):
            by_created[zone.created].append(zone)
    structure = StructureEngine()
    movement = Movement()
    epoch: tuple | None = None
    guard_key: tuple | None = None
    guard_broken_at: datetime | None = None
    candidates: list[_OppositeCandidate] = []
    eligible_seen = False
    output: list[ShadowRuleOneRow] = []

    for i, bar in enumerate(bars):
        point = structure.step(bar, i)
        movement.feed(point)
        end = bar.time + timedelta(hours=1)
        post = h1.flows[i]
        # Pine checks #1 before performing the real flow update. Use the last
        # bar's stream when this bar actually executes #2 or #3.
        pre = h1.flows[i - 1] if i > 0 else post
        state: FlowState = pre if post.rule in (2, 3) and pre.side else post
        captured = captures[i]
        if captured.h1_open != bar.time:
            raise ValueError("Higher-capture alignment differs from H1 candle time")
        if asof is not None and end > asof:
            output.append(ShadowRuleOneRow(bar.time, end, state.side, post.rule,
                                           "H1_NOT_CLOSED", ("h1_not_closed",),
                                           {}, source_status=captured.source_status))
            continue

        epoch_key = (state.side, state.last_change_at, state.start)
        if epoch_key != epoch:
            epoch = epoch_key
            guard_key = None
            guard_broken_at = None
            candidates.clear()
            eligible_seen = False
        guard = state.guard
        current_guard_key = None if guard is None else (
            guard.created, guard.side, guard.top, guard.bottom, guard.impulse
        )
        if current_guard_key != guard_key:
            guard_key = current_guard_key
            guard_broken_at = None
            candidates.clear()
            eligible_seen = False
        if guard is not None and state.side and (
            bar.close <= guard.bottom if state.side == 1 else bar.close >= guard.top
        ):
            if guard_broken_at is None:
                guard_broken_at = bar.time

        # The shared FVG objects include future status, so the 'broken' field
        # is consulted only with its source index <= the current H1 index.
        live: list[_OppositeCandidate] = []
        for cand in candidates:
            broken_now = cand.zone.broken is not None and cand.zone.broken <= i
            is_same_epoch = (state.start is not None and cand.start >= state.start
                             and guard is not None and cand.zone.impulse > guard.impulse
                             and cand.zone.side == -state.side)
            if broken_now or not is_same_epoch:
                continue
            cand.low = min(cand.low, bar.low)
            cand.high = max(cand.high, bar.high)
            live.append(cand)
        candidates = live

        for zone in by_created.get(i, []):
            if state.side == 0 or guard is None or zone.side != -state.side:
                continue
            if zone.impulse <= guard.impulse or zone.broken is not None and zone.broken <= i:
                continue
            # The Pine origin is more nuanced (raw/outside leg). Here we use
            # the same approximate movement acceptance as Python FlowEngine.
            if (movement.side != zone.side or movement.start is None
                or movement.start > zone.impulse or movement.tip_i is None
                or movement.tip_i <= movement.start or state.start is None
                or movement.start < state.start):
                continue
            origin = movement.start
            subset = bars[origin:i + 1]
            candidates.append(_OppositeCandidate(
                zone, origin, i, min(b.low for b in subset),
                max(b.high for b in subset)
            ))

        capture = captured.selected
        all_ready = all(captured.source_status.get(tf) == "READY" for tf in SOURCES)
        # If capture is present, Pine picks the newest *eligible* FVG after
        # that capture; otherwise it selects the newest eligible movement.
        chosen = None
        if candidates:
            eligible = [c for c in candidates if capture is not None
                        and bars[c.formed].time > capture.captured_at]
            chosen = max(eligible or candidates, key=lambda c: (c.formed, c.start))
        opposite = _candidate_summary(chosen, bars) if chosen else None
        # For investigation, show an opposite capture hidden by the newer
        # selected direction. It is NOT permitted to confirm Rule 1 unless
        # it becomes the selected event after source cancellation/fallback.
        ignored = []
        opposite_direction = -state.side
        for tf in SOURCES:
            stack = captured.source_histories.get(tf, ())
            for earlier in stack:
                if earlier.side == opposite_direction and earlier != capture:
                    is_source_active = earlier == captured.source_active.get(tf)
                    ignored.append((earlier, "SOURCE_ACTIVE_BUT_TF_SUPERSEDED"
                                    if is_source_active else "ARCHIVED_IN_SOURCE_STACK"))
        suppressed, suppressed_reason = (max(
            ignored, key=lambda item: (item[0].captured_at, SOURCES.index(item[0].source_tf)))
            if ignored else (None, None))
        checks, blockers, outcome, would_trigger = check_rule_one(
            side=state.side, guard_present=guard is not None,
            first_broken_at=guard_broken_at, opposite_fvg=opposite,
            capture=capture, all_higher_ready=all_ready, actual_rule=post.rule)
        if would_trigger:
            if eligible_seen:
                outcome, would_trigger = "PERSISTING_AFTER_SHADOW_TRIGGER", False
            else:
                eligible_seen = True
        first_guard = (None if guard is None else {
            "top": guard.top, "bottom": guard.bottom,
            "impulse_at": bars[guard.impulse].time,
            "formed_at": bars[guard.created].time,
        })
        output.append(ShadowRuleOneRow(
            bar.time, end, state.side, post.rule, outcome, blockers,
            checks, first_guard, guard_broken_at, opposite,
            capture, captured.source_status.copy(), would_trigger,
            suppressed, suppressed_reason, captured.source_active.copy(),
            captured.selection_reason
        ))
    return output


def serialize_shadow_rule_one(rows: list[ShadowRuleOneRow]) -> dict:
    """Export per-hour blockers for manual validation against Pine v2.09."""
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    def row_dict(r: ShadowRuleOneRow) -> dict:
        guard = r.first_guard
        opposite = r.opposite_fvg
        if guard is not None:
            guard = {**guard, "impulse_at": iso(guard["impulse_at"]),
                     "formed_at": iso(guard["formed_at"])}
        if opposite is not None:
            opposite = {**opposite, "formed_at": iso(opposite["formed_at"]),
                        "movement_start_at": iso(opposite["movement_start_at"]),
                        "impulse_at": iso(opposite["impulse_at"])}
        return {
            "h1_open": iso(r.h1_open), "h1_closed_at": iso(r.h1_closed_at),
            "flow_side": r.flow_side, "actual_rule": r.actual_rule,
            "outcome": r.outcome, "would_trigger": r.would_trigger,
            "blockers": list(r.blockers), "checks": r.checks,
            "first_fvg": guard, "first_broken_at": iso(r.first_broken_at),
            "opposite_fvg": opposite,
            "higher_capture": serialize_capture(r.higher_capture),
            "higher_source_status": r.source_status,
            "higher_active_by_tf": {tf: serialize_capture(r.higher_source_active.get(tf))
                                    for tf in SOURCES},
            "higher_selection_reason": r.higher_selection_reason,
            "suppressed_opposite_capture": serialize_capture(r.suppressed_opposite_capture),
            "suppressed_reason": r.suppressed_reason,
        }

    confirmed = [r for r in rows if r.outcome != "H1_NOT_CLOSED"]
    counts = Counter(r.outcome for r in rows)
    causes = Counter(reason for r in rows for reason in r.blockers)
    triggers = [row_dict(r) for r in rows if r.would_trigger or r.outcome == "OVERRIDDEN_BY_RULE_2"]
    return {
        "status": "SHADOW_DIAGNOSTIC_ONLY_NOT_APPLIED",
        "note": ("Правило №1 не меняет поток. Встречные FVG и D/W/M ликвидность пока "
                 "не сверены 1:1 с Pine v2.09; найденные срабатывания — только гипотезы."),
        "bars": len(rows), "closed_bars": len(confirmed),
        "outcome_counts": dict(counts), "blocker_counts": dict(causes),
        "hypothetical_events": triggers,
        "latest_closed": row_dict(confirmed[-1]) if confirmed else None,
        "rows": [row_dict(r) for r in rows],
    }
