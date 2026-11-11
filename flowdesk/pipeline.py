"""One calculation timestamp and the full supplied history for every timeframe."""
from __future__ import annotations

from datetime import datetime, timezone

from .engine import Analysis, analyze
from .market_data import Candle, TIMEFRAMES, normalize


def analyze_timeframes(series: dict[str, list[Candle]], display_tf: str,
                       *, asof: datetime | None = None,
                       provenance: dict[str, dict] | None = None
                       ) -> tuple[Analysis, dict[str, Analysis]]:
    """Analyze longest TF first, without shortening the calculation history.

    An explicit asof is also used for CSV, which can contain an unfinished
    source candle. Each source retains its own provenance when CSV replaces
    a higher timeframe from an MOEX bundle.
    """
    if display_tf not in TIMEFRAMES or not series.get(display_tf):
        raise ValueError("Нет свечей выбранного таймфрейма")
    asof = asof if asof is not None else datetime.now(timezone.utc)
    analyses: dict[str, Analysis] = {}
    for tf in reversed(TIMEFRAMES[TIMEFRAMES.index(display_tf):]):
        if not series.get(tf):
            continue
        result = analyze(normalize(series[tf]), tf, asof=asof,
                         higher_h1=analyses.get("H1"),
                         higher_liquidity=analyses if tf in ("H1", "D1", "W1", "MN1") else None)
        result.session_info = dict((provenance or {}).get(tf, {}))
        analyses[tf] = result
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
