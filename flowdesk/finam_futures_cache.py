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
        if record.get("verified") is not True:
            return None
        ticker = symbol.split("@", 1)[0].upper()
        # A local file is not authoritative: prohibit a different root,
        # another instrument type, or a different contract in the snapshot.
        term = re.fullmatch(r"([A-Z][A-Z0-9]*)[FGHJKMNQUVXZ][0-9]{1,4}", ticker)
        valid_roots = {
            match.group(1) for alias in contract.aliases
            if (match := re.fullmatch(
                r"([A-Z][A-Z0-9]*)[FGHJKMNQUVXZ][0-9]{1,4}", alias.upper()))
        }
        if valid_roots:
            if term is None or term.group(1) not in valid_roots:
                return None
        elif ticker not in {alias.upper() for alias in contract.aliases}:
            return None
        if valid_roots:
            dated = DATE.search(source)
            if dated is None or not source.endswith((" · Finam GetAsset", " · Finam каталог")):
                return None
            try:
                expiry = date.fromisoformat(dated.group(1))
            except ValueError:
                return None
            # A rollover is required on the calendar day before expiry.
            # Never show yesterday's active contract as verified on roll day.
            if today >= expiry - timedelta(days=1):
                return None
        elif source != "вечный контракт, без ролловера":
            # A term contract without a verified expiry must be checked
            # live; a saved ticker is insufficient evidence.
            return None
        return Item(label, contract.name, contract.group, symbol, True,
                    source + " · сохранённый список; сверка Finam")

    @classmethod
    def _safe_item_at(cls, contract, record, now):
        symbol=record.get("symbol","") if isinstance(record,dict) else ""
        mic=symbol.rsplit("@",1)[-1].upper() if isinstance(symbol,str) else ""
        if mic not in contract.mics:
            mic=contract.mics[0]
        today=now.astimezone(exchange_clock(mic).zone).date()
        return cls._safe_item(contract,record,today)

    @classmethod
    def complete(cls, futures, *, now=None):
        """Only fresh, fully confirmed metadata completes the daily check."""
        if not isinstance(futures,dict):
            return False
        now=now or datetime.now(timezone.utc)
        for group,contracts in GROUP_CONTRACTS.items():
            for contract in contracts:
                item=futures.get(group+":"+contract.display)
                if not isinstance(item,Item) or cls._safe_item_at(contract,asdict(item),now) is None:
                    return False
        return True

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
                    item = self._safe_item_at(contract, stored.get(key), now)
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
                    if self._safe_item_at(contract, candidate, now):
                        records[key] = candidate
            document = {"schema": 1, "slot": slot_date(now).isoformat(),
                        "updated_at": now.isoformat(), "contracts": records}
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._path(group)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
