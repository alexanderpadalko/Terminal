"""Pine format.mintick rounding, separated from market OHLC values."""
from decimal import Decimal
import math


def price_text(value,tick=.01):
    if value is None:return 'NaN'
    places=max(0,min(10,-Decimal(str(tick)).normalize().as_tuple().exponent))
    rounded=math.floor(value/tick+.5)*tick
    return f'{rounded:.{places}f}'
