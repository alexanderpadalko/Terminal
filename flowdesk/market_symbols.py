"""Provider identities: Finam MICs and two explicitly allowed Binance perps."""
from .finam_api import symbol_name as finam_symbol_name

BINANCE="BINANCE"  # Provider identifier, not an invented exchange MIC.
CRYPTO_TICKERS=("BTCUSDT","ETHUSDT")


def symbol_name(ticker,venue):
    if str(venue).strip().upper()==BINANCE:
        ticker=str(ticker).strip().upper().removesuffix(".P")
        if ticker not in CRYPTO_TICKERS:
            raise ValueError("Доступны только BTCUSDT.P и ETHUSDT.P Binance")
        return ticker+"@"+BINANCE
    return finam_symbol_name(ticker,venue)
