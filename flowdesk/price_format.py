"""Price formatting and Finam asset tick precision (display only).

The quote price step is min_step / 10**decimals, per Finam GetAsset.
Use Decimal to avoid binary-float rounding on fractional ticks. Legacy
Pine/reference formatting still uses a period unless decimal_comma=True.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def tick_from_asset(asset):
    """Return exact Finam tick size as Decimal or None for absent/bad metadata."""
    if not isinstance(asset, dict) or "min_step" not in asset or "decimals" not in asset:
        return None
    try:
        step = Decimal(str(asset["min_step"]))
        raw_places = asset["decimals"]
        if isinstance(raw_places, bool) or str(raw_places).strip() != str(int(raw_places)):
            return None
        places = int(raw_places)
        if not step.is_finite() or step <= 0 or not 0 <= places <= 12:
            return None
        result = step.scaleb(-places)
        if not result.is_finite() or result <= 0:
            return None
        return result
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def price_places(tick=.01):
    step = Decimal(str(tick))
    if not step.is_finite() or step <= 0:
        raise ValueError("Price tick must be finite and positive")
    places = max(0, -step.normalize().as_tuple().exponent)
    return min(12, places)


def price_text(value, tick=.01, *, decimal_comma=False):
    if value is None:
        return "NaN"
    step = Decimal(str(tick))
    places = price_places(step)
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "NaN"
        # Round to the nearest market tick, then preserve the tick's decimal places.
        ticks = (amount / step).to_integral_value(rounding=ROUND_HALF_UP)
        rounded = ticks * step
        text = f"{rounded:.{places}f}"
        if Decimal(text) == 0 and text.startswith("-"):
            text = text[1:]
    except (InvalidOperation, OverflowError, ZeroDivisionError, TypeError):
        return "NaN"
    return text.replace(".", ",") if decimal_comma else text
