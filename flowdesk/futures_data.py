"""RFUD candles, kept separate from the TQBR physical-session policy."""
from datetime import date, datetime, time, timedelta

from .market_data import (Candle, MOSCOW, EXTRA, NATIVE_HIGHER, CANONICAL_H1_DAYS,
                          candle_close_time, normalize, stock_session_kind)


def iss_symbol(symbol):
    code = symbol.strip().upper()
    for root, native in (('SI', 'Si'), ('EU', 'Eu')):
        if code.startswith(root) and len(code) == 4:
            return native + code[2:]
    return code


def auction(day):
    if day.weekday() >= 5 or stock_session_kind(day) == EXTRA:
        return time(9, 50), time(10)
    if day >= date(2026, 7, 14):
        return time(6, 50), time(7)
    if day >= date(2025, 1, 27):
        return time(8, 50), time(9)
    return time(9, 50), time(10)


def clean(candles, tf):
    result = []
    for c in normalize(candles):
        local = c.time.astimezone(MOSCOW)
        opening, regular = auction(local.date())
        if tf in ('M1', 'M5'):
            if not opening <= local.time().replace(tzinfo=None) < regular:
                result.append(c)
        elif tf == 'H1':
            if local.hour != opening.hour:
                result.append(c)
        else:
            result.append(c)
    return result


def aggregate_h4(candles):
    days = {}
    for c in normalize(candles):
        days.setdefault(c.time.astimezone(MOSCOW).date(), []).append(c)
    result = []
    for day, bars in days.items():
        anchor = min(datetime.combine(day, auction(day)[1], MOSCOW), bars[0].time.astimezone(MOSCOW))
        extra = day.weekday() >= 5 or stock_session_kind(day) == EXTRA
        end = (datetime.combine(day, time(19), MOSCOW) if extra else
               datetime.combine(day + timedelta(days=1), time.min, MOSCOW))
        end = max(end, candle_close_time(bars[-1], 'H1'))
        groups = {}
        for c in bars:
            key = anchor + timedelta(hours=4 * int((c.time - anchor).total_seconds() // 14400))
            groups.setdefault(key, []).append(c)
        for key, chunk in groups.items():
            result.append(Candle(key, chunk[0].open, max(c.high for c in chunk), min(c.low for c in chunk),
                                 chunk[-1].close, sum(c.volume for c in chunk), min(key + timedelta(hours=4), end)))
    return result


def bundle(symbol, display_tf, *, display_limit=3000, history_days=CANONICAL_H1_DAYS,
           higher_history_days=None, **ignored):
    from .market_data import _iss_download, download_native_higher, download_moex
    raw = _iss_download(symbol, 60, days=history_days, limit=220000, timeout=15, board='RFUD', require_full=True)
    hours = clean(raw, 'H1')
    if not hours:
        raise ValueError('Нет часовых свечей фьючерса RFUD')
    families = {tf: download_native_higher(symbol, tf, board='RFUD',
                days=(higher_history_days or {}).get(tf, CANONICAL_H1_DAYS)) for tf in NATIVE_HIGHER}
    families.update(H1=hours, H4=aggregate_h4(hours))
    visible = (download_moex(symbol, display_tf, board='RFUD', limit=display_limit)
               if display_tf in ('M1', 'M5') else families[display_tf][-display_limit:])
    metadata = {'mode': 'MOEX_RFUD_NATIVE_CANDLES', 'market': 'futures', 'board': 'RFUD',
        'raw_h1_bars': len(raw), 'auction_h1_removed': len(raw) - len(hours),
        'observed_legacy_trading_days': [], 'schedule_overrides': [], 'holiday_extra_sessions_removed': [],
        'futures_h4_schedule': 'physical session: 2025-01-27 09:00; 2026-07-14 07:00; extra days 10:00',
        'legacy_session_schedule_verified': False,
        'warning': 'RFUD historical sessions before 2025 and H4/TradingView parity remain unverified',
        'sources': {tf: {'mode': 'MOEX_RFUD_NATIVE_CANDLES', 'native_higher': tf in NATIVE_HIGHER,
            'opening_auction_excluded': tf not in NATIVE_HIGHER, 'higher_from_native_h1': tf == 'H4',
            'market': 'futures', 'board': 'RFUD'} for tf in ('M1', 'M5', 'H1', 'H4', *NATIVE_HIGHER)}}
    return visible, families, metadata
