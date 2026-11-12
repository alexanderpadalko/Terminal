"""Read-only FVG display rules from the current TradingView v2.11 source.

Detection, flow selection and source OHLCV remain in the existing engines.
Only this display plan decides which already calculated zones reach Qt.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from .market_data import candle_close_time
from .publication import chart_cutoff,source_cutoff,published_state

HIGHER = ('H1', 'H4', 'D1', 'W1', 'MN1')
REFERENCE_SHA256 = 'CC2E1422F6D865C7D4814B7FF7CD4EEA82F87EB89642E8EF5F158A47B85D057F'


@dataclass(frozen=True)
class DisplaySettings:
    enabled: bool = True
    max_zones: int = 20
    only_in_flow: bool = True
    show_lower: bool = True
    higher: tuple[str, ...] = HIGHER

    def __post_init__(self):
        if not 1 <= self.max_zones <= 200:
            raise ValueError('FVG display limit must be between 1 and 200')


@dataclass(frozen=True)
class DisplayZone:
    source_tf: str
    left_time: datetime
    impulse_time: datetime
    formed_at: datetime
    top: float
    bottom: float
    side: int
    right_time: datetime
    broken: bool = False
    parent: bool = False

    @property
    def active(self):
        return not self.broken

    def as_dict(self):
        return {key: value.isoformat() if isinstance(value, datetime) else value
                for key, value in vars(self).items()}


@dataclass(frozen=True)
class FlowView:
    side: int
    lo: float | None
    hi: float | None
    start: datetime | None
    previous: tuple | None = None


@dataclass(frozen=True)
class DisplayPlan:
    zones: tuple[DisplayZone, ...]
    parents: tuple[DisplayZone, ...]
    rejected: dict
    cutoff: datetime | None
    raw_local: int
    settings: DisplaySettings

    def as_dict(self):
        return {'reference': 'TradingView v2.11 H1 + H4', 'reference_sha256': REFERENCE_SHA256,
                'cutoff': self.cutoff.isoformat() if self.cutoff else None,
                'shown': len(self.zones), 'working_parents': len(self.parents),
                'raw_local': self.raw_local, 'rejected': self.rejected,
                'settings': {'enabled': self.settings.enabled, 'max_zones': self.settings.max_zones, 'only_in_flow': self.settings.only_in_flow,
                             'show_lower': self.settings.show_lower, 'higher': list(self.settings.higher)},
                'zones': [z.as_dict() for z in self.zones]}


def in_flow(zone: DisplayZone, flow: FlowView | None):
    """Pine f_mtfInFlow(..., includePrevious=true); direction is not a zone filter."""
    if flow is None:
        return False
    current = (flow.side != 0 and flow.start is not None and flow.lo is not None and flow.hi is not None
               and zone.impulse_time >= flow.start and zone.top >= flow.lo and zone.bottom <= flow.hi)
    previous = False
    if flow.previous is not None:
        start, end, lo, hi = flow.previous
        previous = (start is not None and end is not None and lo is not None and hi is not None
                    and start <= zone.impulse_time < end and zone.top >= lo and zone.bottom <= hi)
    return current or previous


def in_working_parent(child: DisplayZone, parents):
    """Pine f_mtfInH1: strict price overlap, inclusive left-time boundary."""
    return any(p.active and child.bottom < p.top and child.top > p.bottom
               and child.left_time >= p.left_time for p in parents)


def _cutoff(source, cutoff):
    return source_cutoff(source,cutoff)


def _state(source, cutoff):
    """Use the last closed source state; live bounds may only extend its episode."""
    return published_state(source,cutoff,live=True)


def _at(source, index, *, close=False):
    if index is None or not 0 <= index < len(source.candles):
        return None
    bar = source.candles[index]
    return candle_close_time(bar, source.timeframe) if close else bar.time


def _flow_view(source, state):
    if state is None:
        return None
    previous = None
    if state.previous is not None:
        start, end, lo, hi, _ = state.previous
        previous = (state.previous_start_at or _at(source,start),
                    state.previous_end_at or _at(source,end,close=True),lo,hi)
    return FlowView(state.side, state.lo, state.hi,
                    state.range_start_at or _at(source, state.start), previous)


def working_zone(source, cutoff, right_time):
    """Exactly one selected guard; never fall back to an older active FVG."""
    state, index, cutoff = _state(source, cutoff)
    if state is None or not state.side or state.guard_broken or state.guard is None:
        return None
    guard = state.guard
    left, impulse = _at(source, guard.start), _at(source, guard.impulse)
    formed = _at(source, guard.created, close=True)
    if (left is None or impulse is None or formed is None or formed > cutoff
            or state.lo is None or state.hi is None
            or guard.top is None or guard.bottom is None
            or guard.top < state.lo or guard.bottom > state.hi):
        return None
    # The selected guard is stored independently of the retained zone list.
    # Its mutable broken index can refer to a *later* source bar; the closed
    # FlowState.guard_broken is the publication status for this snapshot.
    return DisplayZone(source.timeframe, left, impulse, formed, guard.top, guard.bottom,
                       state.side, right_time, parent=True)


def _local_zones(source, cutoff, right_time, rejected):
    cutoff = _cutoff(source, cutoff)
    published = []
    for zone in source.published_fvg:
        formed = _at(source, zone.created, close=True)
        left, impulse = _at(source, zone.start), _at(source, zone.impulse)
        if formed is None or left is None or impulse is None or formed > cutoff:
            rejected['unconfirmed_or_invalid'] += 1
            continue
        broken_end = _at(source, zone.broken, close=True)
        broken = broken_end is not None and broken_end <= cutoff
        published.append(DisplayZone(source.timeframe, left, impulse, formed,
            zone.top, zone.bottom, zone.side, broken_end if broken else right_time, broken))
    rejected['unconfirmed_or_invalid'] += max(0, len(source.fvg) - len(source.published_fvg))
    return published


def select_fvg_display(analysis, parents=None, settings=None):
    settings = settings or DisplaySettings()
    rejected = Counter()
    if not settings.enabled:
        return DisplayPlan((), (), {'disabled': len(analysis.fvg)}, analysis.asof,
                           len(analysis.fvg), settings)
    if not analysis.candles:
        return DisplayPlan((), (), {}, None, 0, settings)
    right_time = candle_close_time(analysis.candles[-1], analysis.timeframe)
    cutoff = chart_cutoff(analysis)
    lower = analysis.timeframe in ('M1', 'M5')
    drawn, admitted_parents = [], []
    allowed = HIGHER if lower else HIGHER[HIGHER.index(analysis.timeframe)+1:]
    for tf in allowed:
        if tf not in settings.higher or tf not in (parents or {}):
            continue
        source = parents[tf]
        if not source.candles:
            continue
        zone = working_zone(source, cutoff, right_time)
        if zone is None:
            continue
        if len(drawn) >= settings.max_zones:
            rejected['global_limit'] += 1
            continue
        drawn.append(zone)
        if lower:
            admitted_parents.append(zone)
    local = _local_zones(analysis, cutoff, right_time, rejected)
    rejected['source_history_limit'] += max(0, len(local)-settings.max_zones)
    local = local[-settings.max_zones:]
    state, _, _ = _state(analysis, cutoff)
    flow = _flow_view(analysis, state)
    for zone in reversed(local):
        if lower:
            if not settings.show_lower:
                rejected['lower_hidden'] += 1
                continue
            if not in_working_parent(zone, admitted_parents):
                rejected['outside_displayed_working_parent'] += 1
                continue
        elif settings.only_in_flow and not in_flow(zone, flow):
            rejected['outside_current_previous_flow'] += 1
            continue
        if len(drawn) >= settings.max_zones:
            rejected['global_limit'] += 1
            continue
        drawn.append(zone)
    return DisplayPlan(tuple(drawn), tuple(admitted_parents), dict(rejected), cutoff,
                       len(analysis.fvg), settings)
