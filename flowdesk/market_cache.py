"""Persistent MOEX snapshots, shared by all seven chart timeframes.

Cached bars keep their observation timestamp. Opening a saved snapshot later
must never promote its then-forming candles to confirmed ones.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .market_data import (Candle, MOSCOW, TIMEFRAMES, _iss_download, aggregate,
                          exclude_opening_auction, load_moex_session_bundle,
                          load_session_overrides, normalize, candle_close_time)
from .market_data import aggregate_moex_h4
from .pipeline import analyze_timeframes

# Bump when cleaning/calendar semantics change; never reuse incompatible OHLC.
CACHE_POLICY = 'physical-clean-h1-calendar-2026-v1'


@dataclass
class MarketSnapshot:
    symbol: str
    board: str
    asof: datetime
    hours: list[Candle]
    minutes: list[Candle] | None
    metadata: dict
    analyses: dict = field(default_factory=dict)
    series: dict = field(default_factory=dict)

    def sources(self, sessions=None):
        if not self.series:
            self.series = {'H1': self.hours}
            self.series.update({tf: aggregate(self.hours, tf) for tf in ('D1', 'W1', 'MN1')})
            self.series['H4'] = aggregate_moex_h4(self.hours, auction_overrides=sessions)
            if self.minutes:
                self.series['M1'] = self.minutes
                self.series['M5'] = aggregate(self.minutes, 'M5')
        return self.series


class SnapshotStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=20)
        db.execute('CREATE TABLE IF NOT EXISTS snapshots '
                   '(key TEXT PRIMARY KEY, symbol TEXT, board TEXT, asof TEXT, metadata TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS candles '
                   '(key TEXT, tf TEXT, time TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, '
                   'PRIMARY KEY (key, tf, time))')
        return db

    def read(self, key):
        if not self.path.exists():
            return None
        with closing(self._connect()) as db, db:
            row = db.execute('SELECT symbol, board, asof, metadata FROM snapshots WHERE key=?', (key,)).fetchone()
            if row is None:
                return None
            data = {}
            for tf in ('H1', 'M1'):
                values = db.execute('SELECT time, open, high, low, close, volume FROM candles '
                                    'WHERE key=? AND tf=? ORDER BY time', (key, tf)).fetchall()
                data[tf] = normalize([Candle(datetime.fromisoformat(t), o, h, l, c, v)
                                      for t, o, h, l, c, v in values])
            if not data['H1']:
                raise ValueError('В сохранённом снимке нет истории H1')
            asof = datetime.fromisoformat(row[2])
            if asof.tzinfo is None:
                raise ValueError('В сохранённом снимке нет часового пояса')
            return MarketSnapshot(row[0], row[1], asof, data['H1'], data['M1'] or None, json.loads(row[3]))

    def write(self, key, snapshot):
        with closing(self._connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)',
                       (key, snapshot.symbol, snapshot.board, snapshot.asof.isoformat(),
                        json.dumps(snapshot.metadata, ensure_ascii=False)))
            db.execute('DELETE FROM candles WHERE key=?', (key,))
            db.executemany('INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           ((key, tf, c.time.isoformat(), c.open, c.high, c.low, c.close, c.volume)
                            for tf, bars in [('H1', snapshot.hours), ('M1', snapshot.minutes or [])]
                            for c in bars))


class MarketService:
    def __init__(self, path: str | Path | None = None, *, clock=None):
        self.store = SnapshotStore(path or Path.home() / '.flowdesk' / 'market_cache_v1.sqlite3')
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.memory = OrderedDict()

    @staticmethod
    def key(symbol, board, overrides):
        policy = CACHE_POLICY + '|' + '|'.join(f'{d.isoformat()}:{v!r}' for d, v in sorted(overrides.items()))
        return symbol.upper() + ':' + board.upper() + ':' + hashlib.sha256(policy.encode()).hexdigest()

    def _fetch(self, symbol, board, previous, need_minutes, overrides, progress):
        # Freeze before network I/O: a request spanning an hour boundary must
        # not confirm a price observed while that hour was still forming.
        asof = self.clock()
        kwargs = {'board': board, 'auction_overrides': overrides}
        if previous is not None:
            days = max(2, (asof.astimezone(MOSCOW).date() - previous.hours[-1].time.astimezone(MOSCOW).date()).days + 2)
            kwargs['history_days'] = days
        progress('Обновление последних H1' if previous else 'Первая загрузка архива H1')
        _, families, fetched_meta = load_moex_session_bundle(symbol, 'H1', **kwargs)
        hours = normalize((previous.hours if previous else []) + families['H1'])
        minutes = previous.minutes if previous else None
        if need_minutes or minutes:
            progress('Загрузка минутных свечей для M1 и M5')
            days = (max(2, (asof.astimezone(MOSCOW).date() - minutes[-1].time.astimezone(MOSCOW).date()).days + 2)
                    if minutes else 14)
            raw = _iss_download(symbol, 1, days=days, limit=200000, timeout=15,
                                board=board, require_full=True)
            clean = exclude_opening_auction(raw, 'M1', board=board, overrides=overrides)
            if not clean:
                raise ValueError('После исключения аукциона нет минутных свечей')
            minutes = normalize((minutes or []) + clean)
        metadata = dict(fetched_meta)
        if previous:
            metadata['latest_fetch'] = fetched_meta
            # Overlapping pages cannot be added to raw/auction totals without
            # double counting. Preserve the original fetch as a separate audit.
            metadata['initial_archive_fetch'] = previous.metadata.get('initial_archive_fetch', previous.metadata)
            metadata['raw_h1_bars'] = metadata['auction_h1_removed'] = None
            metadata['history_request'] = 'CACHED_ARCHIVE_WITH_INCREMENTAL_REFRESH'
            for field_name in ('observed_legacy_trading_days', 'schedule_overrides'):
                metadata[field_name] = sorted(set(previous.metadata.get(field_name, [])) |
                                              set(fetched_meta.get(field_name, [])))
        metadata.update(base_h1_first=hours[0].time.isoformat(), base_h1_last=hours[-1].time.isoformat(),
                        base_h1_bars=len(hours), snapshot_asof=asof.isoformat(), cache_policy=CACHE_POLICY)
        return MarketSnapshot(symbol, board, asof, hours, minutes, metadata)

    def view(self, symbol, board, tf, *, refresh=False, overrides=None, provenance=None, progress=None):
        symbol, board = symbol.strip().upper(), board.strip().upper()
        if tf not in TIMEFRAMES:
            raise ValueError('Неизвестный таймфрейм')
        progress = progress or (lambda text: None)
        sessions = load_session_overrides()
        key = self.key(symbol, board, sessions)
        saved = self.memory.get(key)
        tier, warning = 'memory', ''
        if saved is None:
            tier = 'disk'
            try:
                saved = self.store.read(key)
            except (sqlite3.Error, OSError, ValueError) as exc:
                warning = f'Кеш недоступен: {exc}'
        needs_minutes = tf in ('M1', 'M5') and not (overrides or {}).get(tf)
        must_fetch = saved is None or refresh or needs_minutes and not saved.minutes
        snapshot = saved
        if must_fetch:
            try:
                snapshot = self._fetch(symbol, board, saved, needs_minutes, sessions, progress)
                tier = 'network'
                try:
                    self.store.write(key, snapshot)
                except (sqlite3.Error, OSError) as exc:
                    warning = f'Данные загружены; кеш недоступен: {exc}'
            except Exception as exc:
                if saved is None or needs_minutes and not saved.minutes:
                    raise
                snapshot, tier = saved, 'offline'
                warning = f'Обновление недоступно; сохранённые данные: {exc}'
        self.memory[key] = snapshot
        self.memory.move_to_end(key)
        while len(self.memory) > 4:
            self.memory.popitem(last=False)
        series = snapshot.sources(sessions)
        if overrides:
            cutoff = snapshot.asof
            for source_tf, bars in overrides.items():
                observed = (provenance or {}).get(source_tf, {}).get('observed_asof')
                if observed and bars:
                    observed = datetime.fromisoformat(observed)
                    if candle_close_time(bars[-1], source_tf) > observed and observed < cutoff:
                        cutoff = observed
                        warning = (warning + ' · ' if warning else '') + f'Расчёт ограничен снимком незакрытого CSV {source_tf}; обновите импорт для продолжения'
            main, parents = analyze_timeframes({**series, **overrides}, tf, asof=cutoff,
                                               provenance={**{t: snapshot.metadata for t in series}, **(provenance or {})})
        else:
            if not snapshot.analyses:
                progress('Расчёт потоков по загруженной истории')
                first_tf = 'M1' if snapshot.minutes else 'H1'
                main, parents = analyze_timeframes(series, first_tf, asof=snapshot.asof,
                                                   provenance={t: snapshot.metadata for t in series})
                snapshot.analyses = {main.timeframe: main, **parents}
            main = snapshot.analyses[tf]
            parents = {t: a for t, a in snapshot.analyses.items()
                       if TIMEFRAMES.index(t) > TIMEFRAMES.index(tf)}
        return main, parents, {'symbol': symbol, 'board': board, 'tf': tf, 'tier': tier,
                               'asof': main.asof.isoformat(), 'warning': warning}
