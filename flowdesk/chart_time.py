"""Chart labels show source candle start, in the selected exchange's time zone."""
from .exchange_time import ExchangeClock, LEGACY_MOSCOW_CLOCK

LABELS = {'M1': '1м', 'M5': '5м', 'H1': '1ч', 'H4': '4ч',
          'D1': 'Д', 'W1': 'Н', 'MN1': 'М'}


def candle_time_text(candle, timeframe, exchange: ExchangeClock | None = None):
    clock = exchange if exchange is not None else LEGACY_MOSCOW_CLOCK
    return f'{LABELS[timeframe]} · {clock.format(candle.time)}'
