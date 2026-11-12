"""Chart timestamps always come from the candle's source period start."""
from .market_data import MOSCOW

LABELS = {'M1': '1м', 'M5': '5м', 'H1': '1ч', 'H4': '4ч',
          'D1': 'Д', 'W1': 'Н', 'MN1': 'М'}


def candle_time_text(candle, timeframe):
    stamp = candle.time.astimezone(MOSCOW)
    return f'{LABELS[timeframe]} · {stamp:%d.%m.%Y %H:%M} МСК'
