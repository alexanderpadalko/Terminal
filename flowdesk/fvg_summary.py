"""Working-FVG summary predicate shared by Pine v2.13 and v2.14.

Higher columns use confirmed LiquidityView; H1 and the own column may extend
the confirmed episode's bounds. Neither path searches the drawn zone list.
The selected guard now comes from the v2.14 replay (not fallback zones).
"""
from .fvg_display import working_zone

REFERENCE_SHA256 = 'E38B59F0C700EFA1CC32E10FF8B207701980E74450C3877DBD7D5C2490374888'


def summary_fvg_side(source, cutoff, price, *, live=False):
    """v2.13 f_summaryFlowSide / f_summaryHigherSide, inclusive at edges."""
    if source is None or not source.candles or price is None:
        return 0
    zone = working_zone(source, cutoff, cutoff, live=live)
    return zone.side if zone is not None and zone.bottom <= price <= zone.top else 0
