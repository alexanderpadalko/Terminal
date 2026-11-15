"""Finam D/W/M are primary data; session reconstruction is an audit only."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .market_data import candle_close_time, normalize

NATIVE_HIGHER = ('D1', 'W1', 'MN1')
NATIVE_POLICY_VERSION = 'finam-native-dwm-1'
UTC = timezone.utc


def native_higher_enabled(api):
    return (bool(getattr(api, 'session_candles', False)) and
            tuple(getattr(api, 'native_higher_frames', ())) == NATIVE_HIGHER)


def native_higher_series(raw, observed, observations):
    """Keep each provider TF whole, without filling gaps from derived bars.

    A known saved draft needs a post-close snapshot before publication as
    closed. Legacy archive bars may be retained when a later period is already
    present at the observation time. An unobserved legacy right edge needs the
    normal bounded HTTP refresh; lower bars and holiday calendars are irrelevant.
    """
    result = {}; issues = {}
    unknown = datetime.min.replace(tzinfo=UTC)
    for tf in NATIVE_HIGHER:
        rows = normalize(raw.get(tf, ()))
        accepted = []
        pending_gap = False
        for index, bar in enumerate(rows):
            if bar.time > observed:
                continue
            ending = candle_close_time(bar, tf)
            known = observations.get((tf, bar.time), unknown)
            legacy = known == unknown
            if ending <= observed:
                successor = index + 1 < len(rows) and rows[index + 1].time <= observed
                valid = (legacy and successor) or (not legacy and known >= ending)
            else:
                valid = not legacy and bar.time <= known <= observed
            if not valid:
                issues[tf] = 'native_snapshot_unconfirmed'
                if ending <= observed:
                    pending_gap = True
                continue
            if bar.volume < 0:
                raise ValueError('Invalid native volume')
            if pending_gap:
                accepted.clear()  # Do not create a false adjacency across a known draft.
                pending_gap = False
            accepted.append(bar if bar.scheduled_close is not None else
                            replace(bar, scheduled_close=ending))
        if accepted:
            result[tf] = accepted
    return result, issues


def control_issues(built, primary, calendar, observed):
    """Compare only available complete control periods; never replace native OHLC.

    Finam uses date labels. Session candles carry actual physical opening times,
    which may fall in the previous month/week. Compare by their trading period.
    A mismatch is diagnostic, not proof that either session definition is wrong.
    """
    from .session_candles import _same_prices
    issues = {tf: value for tf, value in built.issues.items() if tf in NATIVE_HIGHER}
    for tf in ('W1', 'MN1'):
        def key(day):
            return day.replace(day=1) if tf == 'MN1' else day - timedelta(days=day.weekday())
        native = {key(b.time.astimezone(UTC).date()): b for b in primary.get(tf, ())}
        for bar in built.series.get(tf, ()):
            # Forming native/control snapshots may have different delivery
            # times. Only compare finalized control candles here.
            if bar.scheduled_close > observed:
                continue
            period = key((bar.scheduled_close - timedelta(microseconds=1)).astimezone(calendar.zone).date())
            candidate = native.get(period)
            if candidate is not None and candle_close_time(candidate, tf) <= observed and not _same_prices(candidate, bar):
                issues[tf] = 'native_period_session_mismatch'
    return issues
