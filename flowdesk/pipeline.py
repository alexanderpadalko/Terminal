"""One calculation timestamp and the full supplied history for every timeframe."""
from __future__ import annotations

from datetime import datetime, timezone
from bisect import bisect_right
from dataclasses import replace

from .engine import Analysis, analyze
from .market_data import Candle, TIMEFRAMES, normalize,candle_close_time
from .liquidity import HIGHER_SOURCES
from .history_analysis import analyze_history
from .terminal_policy import DISPLAY_TIMEFRAMES
from .cancellation import checkpoint


def _closed(source):
    return (len(source.candles) if source.asof is None else
            bisect_right(source.candles,source.asof,key=lambda bar:candle_close_time(bar,source.timeframe)))


def _reuse(source,bars,tf,asof):
    """A later clock alone cannot change output inside the same source interval."""
    if source is None or source.timeframe!=tf or source.asof is None or asof<source.asof or source.candles!=bars:
        return None
    if (_closed(source)!=bisect_right(bars,asof,key=lambda bar:candle_close_time(bar,tf)) or
            bisect_right(bars,source.asof,key=lambda bar:bar.time)!=bisect_right(bars,asof,key=lambda bar:bar.time)):
        return None
    return source if source.asof==asof else replace(source,asof=asof)


def _same_closed(before,after):
    if before is None:return False
    old_count,new_count=_closed(before),_closed(after)
    return old_count==new_count and before.candles[:old_count]==after.candles[:new_count]


def analyze_timeframes(series: dict[str, list[Candle]], display_tf: str,
                       *, asof: datetime | None = None,
                       provenance: dict[str, dict] | None = None,
                       cached: dict[str, Analysis] | None = None,
                       settings=None, terminal=False
                       ) -> tuple[Analysis, dict[str, Analysis]]:
    """Analyze longest TF first, without shortening the calculation history.

    An explicit asof is also used for CSV, which can contain an unfinished
    source candle. Each source retains its own provenance when CSV replaces
    a higher timeframe from an MOEX bundle.
    """
    frames=DISPLAY_TIMEFRAMES if terminal else TIMEFRAMES
    if display_tf not in frames or not series.get(display_tf):
        raise ValueError("Нет свечей выбранного таймфрейма")
    asof = asof if asof is not None else datetime.now(timezone.utc)
    main_candles=normalize(series[display_tf])
    asof=min(asof,candle_close_time(main_candles[-1],display_tf))
    if settings is None:
        from .settings import IndicatorSettings
        settings=IndicatorSettings()
    analyses: dict[str, Analysis] = {}
    minimum_tick=next((data['minimum_tick'] for data in (provenance or {}).values() if data.get('minimum_tick')), .01)
    def replay(bars,tf,*,previous=None,**kwargs):
        if terminal:
            kwargs.update(market_structure=False, low_tf_flow=False, incremental=True)
        boundaries=(provenance or {}).get(tf,{}).get('history_breaks',{}).get(tf,())
        return analyze_history(bars,tf,breaks=boundaries,cached=previous,**kwargs) if boundaries else analyze(bars,tf,cached=previous,**kwargs)
    limited={}
    previous_summary={}
    for saved in (cached or {}).values():
        previous_summary.update(getattr(saved,'summary_sources',{}))
    changed_contexts=set()
    if settings is not None:
        for tf in ('MN1','W1','D1','H4'):
            checkpoint()
            if series.get(tf):
                bars=normalize(series[tf])
                known=bars[:bisect_right(bars,asof,key=lambda bar:bar.time)]
                if not known:
                    continue
                bars=known[-settings.liquidityHistoryBars:]
                old=previous_summary.get(tf)
                dependency_changed=bool(set(HIGHER_SOURCES.get(tf,()))&changed_contexts)
                result=None if dependency_changed else _reuse(old,bars,tf,asof)
                limited[tf]=result or replay(bars,tf,asof=asof,higher_liquidity=limited,
                                             previous=old if terminal else None if dependency_changed else old,
                                             settings=settings,minimum_tick=minimum_tick)
                if dependency_changed or not _same_closed(old,limited[tf]):changed_contexts.add(tf)
    reused=set()
    for tf in reversed(frames[frames.index(display_tf):]):
        checkpoint()
        if not series.get(tf):
            continue
        bars=normalize(series[tf])
        dependency_changed=bool(set(HIGHER_SOURCES.get(tf,()))&changed_contexts)
        if tf in ('M1','M5') and 'H1' in analyses and 'H1' not in reused:dependency_changed=True
        saved=(cached or {}).get(tf)
        result=None if dependency_changed else _reuse(saved,bars,tf,asof)
        if result is not None:
            analyses[tf] = result
            reused.add(tf)
            continue
        # The limited context is already the exact full calculation when
        # its complete input fits the configured window. Clone the Analysis
        # container so summary links cannot create cycles through contexts.
        exact=limited.get(tf) if terminal else None
        result = replace(exact) if exact is not None and exact.candles==bars else replay(bars, tf, asof=asof,
                         previous=saved if terminal else None if dependency_changed else saved,
                         higher_h1=analyses.get("H1"),
                         higher_liquidity=(limited if settings is not None else analyses) if tf in ("H1", "H4", "D1", "W1", "MN1") else None,
                         settings=settings,minimum_tick=minimum_tick)
        result.session_info = dict((provenance or {}).get(tf, {}))
        analyses[tf] = result
    if settings is not None:
        summary={**limited}
        if analyses.get('H1'):
            summary['H1']=analyses['H1']
        elif not terminal and series.get('H1'):
            # H1 is LOWER than H4/D/W/M: the main analysis loop stops at the
            # displayed frame and therefore never visits it. The five-column
            # summary must still receive its independent confirmed H1 flow.
            # Do not substitute H4 direction for missing H1 data.
            hourly=normalize(series['H1'])
            eligible=hourly[:bisect_right(hourly,asof,key=lambda bar:bar.time)]
            if eligible:
                changed=bool(set(HIGHER_SOURCES['H1'])&changed_contexts)
                old=(cached or {}).get('H1') or previous_summary.get('H1')
                kept=None if changed else _reuse(old,eligible,'H1',asof)
                summary['H1']=kept or replay(eligible,'H1',asof=asof,
                    previous=None if changed else old,
                    higher_liquidity=limited,settings=settings,
                    minimum_tick=minimum_tick)
                summary['H1'].session_info=dict((provenance or {}).get('H1',{}))
        checkpoint()
        for result in analyses.values():
            # Do not retain an Analysis -> summary -> same Analysis cycle.
            result.summary_sources={tf:source for tf,source in summary.items() if source is not result}
    main = analyses.pop(display_tf)
    return main, analyses


def source_coverage(analysis: Analysis) -> dict:
    """Report actual coverage; a bar count is never proof of Pine parity."""
    candles = analysis.candles
    return {
        "bars": len(candles),
        "confirmed_bars": analysis.confirmed_bars,
        "forming_bars": len(candles) - analysis.confirmed_bars,
        "first": candles[0].time.isoformat() if candles else None,
        "last": candles[-1].time.isoformat() if candles else None,
        "asof": analysis.asof.isoformat() if analysis.asof else None,
        "history_complete": False,
        "note": "Показан доступный диапазон; полнота истории и паритет с Pine не подтверждены",
    }

