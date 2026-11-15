"""Safe daily snapshots of the two independently resolved Finam futures lists.

The cache is a UI startup fallback, never authority for rollover or market
data. A dated contract can be shown only before its rollover day; expired
and undated term contracts are NOT made selectable from disk.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re

from .finam_api import SAFE_SYMBOL
from .finam_universe import (
    US_FUTURES, RU_FUTURES, US_CONTRACTS, RU_CONTRACTS, Item,
)
from .watchlist import slot_date
from .exchange_time import exchange_clock

GROUP_CONTRACTS = {US_FUTURES: US_CONTRACTS, RU_FUTURES: RU_CONTRACTS}
DATE = re.compile(r"(?:действует до |новая дата )(\d{4}-\d{2}-\d{2})")


class FuturesListCache:
    """Atomic, separate US/RU snapshots without credentials or raw API rows."""

    def __init__(self, directory=None):
        self.directory = Path(directory or Path.home() / ".flowdesk")

    def _path(self, group):
        if group not in GROUP_CONTRACTS:
            raise ValueError("Unknown futures list")
        return self.directory / ("finam_futures_us_v1.json" if group == US_FUTURES
                                 else "finam_futures_ru_v1.json")

    @staticmethod
    def _safe_item(contract, record, today):
        if not isinstance(record, dict):
            return None
        symbol = record.get("symbol", "")
        label = record.get("label", "")
        source = record.get("source", "")
        if not all(isinstance(x, str) for x in (symbol, label, source)):
            return None
        if not SAFE_SYMBOL.fullmatch(symbol.upper()):
            return None
        mic = symbol.rsplit("@", 1)[1].upper()
        if mic not in contract.mics or not label or len(label) > 45:
            return None
        if not record.get("verified"):
            return None
        dated = DATE.search(source)
        if dated:
            try:
                expiry = date.fromisoformat(dated.group(1))
            except ValueError:
                return None
            # A rollover is required on the calendar day before expiry.
            # Never show yesterday's active contract as verified on roll day.
            if today >= expiry - timedelta(days=1):
                return None
        elif "вечный контракт, без ролловера" not in source:
            # A term contract without a verified expiry must be checked
            # live; a saved ticker is insufficient evidence.
            return None
        return Item(label, contract.name, contract.group, symbol, True,
                    source + " · сохранённый список; сверка Finam")

    def load(self, *, now=None):
        now = now or datetime.now(timezone.utc)
        result = {}
        for group, contracts in GROUP_CONTRACTS.items():
            try:
                value = json.loads(self._path(group).read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("schema") != 1:
                    continue
                stored = value.get("contracts", {})
                if not isinstance(stored, dict):
                    continue
                for contract in contracts:
                    key = group + ":" + contract.display
                    exchange_today = now.astimezone(exchange_clock(contract.mics[0]).zone).date()
                    item = self._safe_item(contract, stored.get(key), exchange_today)
                    if item is not None:
                        result[key] = item
            except (OSError, ValueError, TypeError):
                continue
        return result

    def save(self, futures, *, now=None):
        now = now or datetime.now(timezone.utc)
        for group, contracts in GROUP_CONTRACTS.items():
            # Only publish rows whose contract metadata was actually
            # confirmed this pass. Retain prior snapshot for missing rows,
            # so an outage does not replace it with unverified guesses.
            prior = {}
            try:
                existing = json.loads(self._path(group).read_text(encoding="utf-8"))
                if isinstance(existing, dict) and isinstance(existing.get("contracts"), dict):
                    prior = existing["contracts"]
            except (OSError, ValueError, TypeError):
                pass
            records = dict(prior)
            for contract in contracts:
                key = group + ":" + contract.display
                item = futures.get(key)
                if isinstance(item, Item) and item.verified:
                    candidate = asdict(item)
                    exchange_today = now.astimezone(exchange_clock(contract.mics[0]).zone).date()
                    if self._safe_item(contract, candidate, exchange_today):
                        records[key] = candidate
            document = {"schema": 1, "slot": slot_date(now).isoformat(),
                        "updated_at": now.isoformat(), "contracts": records}
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._path(group)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
