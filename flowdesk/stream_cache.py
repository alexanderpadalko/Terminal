"""Atomic stream upserts and conservative confirmation, outside indicator code."""
from datetime import datetime, timedelta
import math

from .market_data import (MOSCOW, TIMEFRAMES, Candle, aggregate, candle_close_time,
                          exclude_opening_auction, normalize, period_floor)


class StreamGap(ValueError):
    """A discontinuity needs an HTTP overlap repair before streaming resumes."""


DEPENDENTS = {
    'MN1': {'W1', 'D1', 'H4', 'H1', 'M5', 'M1'},
    'W1': {'D1', 'H4', 'H1', 'M5', 'M1'},
    'D1': {'H4', 'H1', 'M5', 'M1'},
    'H1': {'H4', 'M5', 'M1'}, 'H4': {'H1','M5','M1'}, 'M1': {'M5'}, 'M5': set(),
}


def merge_stream(saved, events, sessions):
    from .market_cache import MarketSnapshot, PUBLIC_FEED_DELAY
    raw = {'H1': saved.hours, 'M1': saved.minutes or [], **saved.native_higher}
    updates = {tf: {} for tf in raw}
    metadata = dict(saved.metadata)
    baseline = datetime.fromisoformat(metadata.get('http_completed_at', saved.asof.isoformat()))
    proofs = {tf: datetime.fromisoformat(value) for tf, value in metadata.get('stream_final_horizons', {}).items()}
    final_keys = {tf: set(values) for tf, values in metadata.get('stream_final_keys', {}).items()}
    desired = datetime.fromisoformat(metadata.get('stream_desired_asof', saved.asof.isoformat()))
    developing = set(metadata.get('stream_developing_h1', []))
    received = None
    for event in events:
        if (event.kind != 'candles' or (event.symbol, event.board) != (saved.symbol, saved.board)
                or event.timeframe not in updates or event.received_at <= baseline):
            continue
        tf = event.timeframe
        bars = list(event.candles)
        if tf in ('M1', 'H1'):
            bars = exclude_opening_auction(bars, tf, board=saved.board, overrides=sessions)
        for bar in bars:
            if (bar.time.tzinfo is None or not all(math.isfinite(x) for x in
                    (bar.open, bar.high, bar.low, bar.close, bar.volume)) or bar.volume < 0 or
                    bar.high < max(bar.open, bar.low, bar.close) or bar.low > min(bar.open, bar.close)):
                raise ValueError('Некорректная свеча ISS+')
            if raw[tf] and bar.time < raw[tf][0].time:
                continue
            updates[tf][bar.time] = bar
            end = candle_close_time(bar, tf)
            if end <= event.received_at - PUBLIC_FEED_DELAY:
                proofs[tf] = max(proofs.get(tf, saved.asof), end)
                final_keys.setdefault(tf, set()).add(bar.time.isoformat())
                if tf in ('M1', 'H1'):
                    desired = max(desired, end)
                if tf == 'H1':
                    developing.discard(bar.time.isoformat())
        received = max(received or event.received_at, event.received_at)
    if received is None:
        return saved, {}
    for tf, additions in updates.items():
        tail = raw[tf][-1] if raw[tf] else None
        for bar in sorted(additions.values(), key=lambda c: c.time):
            if tail and bar.time > candle_close_time(tail, tf):
                raise StreamGap(f'Разрыв {tf}: {tail.time.isoformat()} → {bar.time.isoformat()}')
            if tail is None or bar.time > tail.time:
                tail = bar
    merged = {tf: normalize(bars + list(updates[tf].values())) for tf, bars in raw.items()}
    # Forming H1 is built from complete minute coverage, never from adding
    # minute volumes to an HTTP partial-hour prefix. Closed native H1 wins.
    if updates['M1'] and merged['M1'] and desired > saved.asof:
        hours = {c.time: c for c in merged['H1']}
        groups = aggregate([c for c in merged['M1'] if candle_close_time(c, 'M1') <= desired], 'H1')
        for bar in groups:
            end = candle_close_time(bar, 'H1')
            if end <= saved.asof or bar.time.isoformat() in final_keys.get('H1', set()):
                continue
            if bar.time < period_floor(saved.asof.astimezone(MOSCOW), 'H1'):
                continue
            hours[bar.time] = bar
            updates['H1'][bar.time] = bar
            developing.add(bar.time.isoformat())
        merged['H1'] = sorted(hours.values(), key=lambda c: c.time)
    cutoff = desired
    # A faster D/W/M publication or computer clock cannot seal a partial H1.
    # If a boundary crosses before all raw sources are verified, hold just
    # before it and ask the coordinator for an HTTP overlap repair.
    for tf, bars in merged.items():
        for bar in bars:
            end = candle_close_time(bar, tf)
            if saved.asof < end <= desired and (bar.time.isoformat() not in final_keys.get(tf, set()) or
                    tf == 'H1' and bar.time.isoformat() in developing):
                cutoff = min(cutoff, end - timedelta(microseconds=1))
    cutoff = max(saved.asof, cutoff)
    metadata.update(stream_final_horizons={tf: t.isoformat() for tf, t in proofs.items()},
                    stream_final_keys={tf: sorted(key for key in keys if
                        candle_close_time(Candle(datetime.fromisoformat(key), 0, 0, 0, 0), tf) > cutoff)
                        for tf, keys in final_keys.items()},
                    stream_desired_asof=desired.isoformat(), stream_received_at=received.isoformat(),
                    stream_developing_h1=sorted(developing), snapshot_asof=cutoff.isoformat(),
                    stream_repair_required=cutoff < desired)
    snapshot = MarketSnapshot(saved.symbol, saved.board, cutoff, merged['H1'], merged['M1'] or None,
                              metadata, {tf: merged[tf] for tf in ('D1', 'W1', 'MN1')})
    old_sources, new_sources = saved.sources(sessions), snapshot.sources(sessions)
    invalid = set()
    for tf, bars in new_sources.items():
        old = old_sources.get(tf, [])
        if old != bars or sum(candle_close_time(c, tf) <= saved.asof for c in old) != sum(
                candle_close_time(c, tf) <= cutoff for c in bars):
            invalid.add(tf)
        # Forming parent updates don't change confirmed liquidity downstream.
        if [c for c in old if candle_close_time(c, tf) <= saved.asof] != [
                c for c in bars if candle_close_time(c, tf) <= cutoff]:
            invalid.update(DEPENDENTS[tf])
    snapshot.analyses = {tf: a for tf, a in saved.analyses.items() if tf not in invalid}
    snapshot.calculation_key=saved.calculation_key
    changes = {tf: [c for c in values.values() if c != next((b for b in raw[tf] if b.time == c.time), None)]
               for tf, values in updates.items()}
    return snapshot, changes
