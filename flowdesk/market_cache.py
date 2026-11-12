"""Persistent MOEX snapshots, shared by all seven chart timeframes.

Cached bars keep their observation timestamp. Opening a saved snapshot later
must never promote its then-forming candles to confirmed ones.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .market_data import (Candle, MOSCOW, TIMEFRAMES, _iss_download, aggregate,
                          exclude_opening_auction, load_moex_session_bundle,
                          load_session_overrides, normalize, candle_close_time)
from .market_data import (aggregate_moex_h4, NATIVE_HIGHER, MOEX_INTERVALS,
                          CANONICAL_H1_DAYS, bundle_source_info)
from .pipeline import analyze_timeframes
from .moex_clock import with_session_closes

# Bump when cleaning/calendar semantics change; never reuse incompatible OHLC.
# Native D/W/M add new SQLite rows; missing rows trigger a native archive
# fetch while keeping the compatible H1/M1 key and its old full history.
CACHE_POLICY = 'physical-clean-h1-calendar-2026-v1'
PUBLIC_FEED_DELAY = timedelta(minutes=15)


@dataclass
class MarketSnapshot:
    symbol: str
    board: str
    asof: datetime
    hours: list[Candle]
    minutes: list[Candle] | None
    metadata: dict
    native_higher: dict = field(default_factory=dict)
    analyses: dict = field(default_factory=dict)
    series: dict = field(default_factory=dict)
    calculation_key: str = ''

    def sources(self, sessions=None):
        if not self.series:
            if not all(self.native_higher.get(tf) for tf in NATIVE_HIGHER):
                raise ValueError('В снимке нет готовых свечей Д/Н/М MOEX; требуется загрузка')
            hours=with_session_closes(self.hours,'H1',self.board,sessions)
            self.series = {'H1': hours}
            self.series.update({tf:with_session_closes(bars,tf,self.board,sessions)
                                for tf,bars in self.native_higher.items()})
            if self.board == 'RFUD':
                from .futures_data import aggregate_h4
                self.series['H4'] = aggregate_h4(hours)
            else:
                self.series['H4'] = aggregate_moex_h4(hours, auction_overrides=sessions)
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
            for tf in ('H1', 'M1', *NATIVE_HIGHER):
                values = db.execute('SELECT time, open, high, low, close, volume FROM candles '
                                    'WHERE key=? AND tf=? ORDER BY time', (key, tf)).fetchall()
                data[tf] = normalize([Candle(datetime.fromisoformat(t), o, h, l, c, v)
                                      for t, o, h, l, c, v in values])
            if not data['H1']:
                raise ValueError('В сохранённом снимке нет истории H1')
            asof = datetime.fromisoformat(row[2])
            if asof.tzinfo is None:
                raise ValueError('В сохранённом снимке нет часового пояса')
            return MarketSnapshot(row[0], row[1], asof, data['H1'], data['M1'] or None,
                                  json.loads(row[3]), {tf: data[tf] for tf in NATIVE_HIGHER if data[tf]})

    def write(self, key, snapshot):
        with closing(self._connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)',
                       (key, snapshot.symbol, snapshot.board, snapshot.asof.isoformat(),
                        json.dumps(snapshot.metadata, ensure_ascii=False)))
            db.execute('DELETE FROM candles WHERE key=?', (key,))
            db.executemany('INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           ((key, tf, c.time.isoformat(), c.open, c.high, c.low, c.close, c.volume)
                            for tf, bars in [('H1', snapshot.hours), ('M1', snapshot.minutes or []),
                                             *snapshot.native_higher.items()]
                            for c in bars))

    def write_delta(self, key, snapshot, changes):
        # One SQLite transaction publishes metadata and keyed candle upserts.
        # The untouched archive isn't rewritten on each live day/week update.
        with closing(self._connect()) as db, db:
            db.execute('INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)',
                       (key, snapshot.symbol, snapshot.board, snapshot.asof.isoformat(),
                        json.dumps(snapshot.metadata, ensure_ascii=False)))
            db.executemany('INSERT OR REPLACE INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           ((key, tf, c.time.isoformat(), c.open, c.high, c.low, c.close, c.volume)
                            for tf, bars in changes.items() for c in bars))


class MarketService:
    def __init__(self, path: str | Path | None = None, *, clock=None, feed_delay=PUBLIC_FEED_DELAY,load_metadata=False):
        self.store = SnapshotStore(path or Path.home() / '.flowdesk' / 'market_cache_v1.sqlite3')
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if feed_delay < timedelta(0):
            raise ValueError('Задержка источника не может быть отрицательной')
        self.feed_delay = feed_delay
        self.load_metadata=load_metadata
        self.memory = OrderedDict()

    @staticmethod
    def key(symbol, board, overrides):
        policy = CACHE_POLICY + '|' + '|'.join(f'{d.isoformat()}:{v!r}' for d, v in sorted(overrides.items()))
        return symbol.upper() + ':' + board.upper() + ':' + hashlib.sha256(policy.encode()).hexdigest()

    def _fetch(self, symbol, board, previous, need_minutes, overrides, progress):
        # Freeze before network I/O: a request spanning an hour boundary must
        # not confirm a price observed while that hour was still forming.
        requested_at = self.clock()
        asof = requested_at - self.feed_delay
        kwargs = {'board': board, 'auction_overrides': overrides}
        if previous is not None:
            days = max(2, (asof.astimezone(MOSCOW).date() - previous.hours[-1].time.astimezone(MOSCOW).date()).days + 2)
            kwargs['history_days'] = days
        kwargs['higher_history_days'] = {
            tf: (max(2, (asof.astimezone(MOSCOW).date() - previous.native_higher[tf][-1].time.astimezone(MOSCOW).date()).days + 2)
                 if previous and previous.native_higher.get(tf) else CANONICAL_H1_DAYS)
            for tf in NATIVE_HIGHER}
        progress('Обновление последних H1' if previous else 'Первая загрузка архива H1')
        _, families, fetched_meta = load_moex_session_bundle(symbol, 'H1', **kwargs)
        hours = normalize((previous.hours if previous else []) + families['H1'])
        native_higher = {tf: normalize((previous.native_higher.get(tf, []) if previous else []) + families[tf])
                         for tf in NATIVE_HIGHER}
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
            metadata.update({k:v for k,v in previous.metadata.items() if k.startswith('minimum_tick')})
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
                        base_h1_bars=len(hours), snapshot_asof=asof.isoformat(), cache_policy=CACHE_POLICY,
                        request_started_at=requested_at.isoformat(),
                        feed_delay_seconds=int(self.feed_delay.total_seconds()))
        metadata['http_completed_at'] = self.clock().isoformat()
        metadata['sources'] = {tf: {**bundle_source_info(fetched_meta, tf),
                                    'native_higher': True, 'mode': 'MOEX_NATIVE_CANDLES',
                                    'symbol': symbol, 'board': board, 'iss_interval': MOEX_INTERVALS[tf],
                                    'higher_from_clean_h1': False, 'opening_auction_excluded': False,
                                    'first': bars[0].time.isoformat(), 'last': bars[-1].time.isoformat(),
                                    'bars': len(bars), 'snapshot_asof': asof.isoformat()}
                               for tf, bars in native_higher.items()}
        for tf in ('H1', 'H4', 'M1', 'M5'):
            metadata['sources'][tf] = {**fetched_meta.get('sources', {}).get(tf, {}),
                                        'mode': 'MOEX_RFUD_NATIVE_CANDLES' if board == 'RFUD' else 'MOEX_CLEAN_INTRADAY',
                                        'symbol': symbol, 'board': board, 'iss_interval': MOEX_INTERVALS[tf],
                                        'native_higher': False, 'opening_auction_excluded': True,
                                        'higher_from_clean_h1': tf == 'H4' and board != 'RFUD',
                                        'snapshot_asof': asof.isoformat()}
        return MarketSnapshot(symbol, board, asof, hours, minutes, metadata, native_higher)

    def _refresh_tick(self,snapshot):
        """Metadata-only recovery must not advance the OHLCV observation clock."""
        if not self.load_metadata:
            return False
        def valid(value):
            return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>0
        metadata=snapshot.metadata
        now=self.clock()
        day=now.astimezone(MOSCOW).date().isoformat()
        tick=metadata.get('minimum_tick')
        if valid(tick) and metadata.get('minimum_tick_date')==day:
            return False
        attempted=metadata.get('minimum_tick_attempted_at')
        if attempted:
            try:
                if now-datetime.fromisoformat(attempted)<timedelta(seconds=60):
                    return False
            except (ValueError,TypeError):
                pass
        from .market_data import security_tick
        fresh=security_tick(snapshot.symbol,snapshot.board,day)
        metadata['minimum_tick_attempted_at']=now.isoformat()
        if valid(fresh):
            metadata.update(minimum_tick=float(fresh),minimum_tick_date=day,minimum_tick_source='MOEX_MINSTEP')
        else:
            metadata.update(minimum_tick=tick if valid(tick) else None,
                            minimum_tick_source='MOEX_MINSTEP_STALE' if valid(tick) else 'DEFAULT_0.01_UNVERIFIED')
        return True

    def ingest_stream(self, symbol, board, events):
        # The GUI serializes this with HTTP and calculation in MarketWorker.
        # Transport callbacks never mutate shared history or engine objects.
        from .stream_cache import merge_stream
        sessions = load_session_overrides()
        key = self.key(symbol, board, sessions)
        saved = self.memory.get(key)
        if saved is None:
            raise ValueError('Потоку требуется исходный снимок HTTP')
        snapshot, changes = merge_stream(saved, events, sessions)
        if snapshot is saved:
            return
        self.store.write_delta(key, snapshot, changes)
        self.memory[key] = snapshot

    def view(self, symbol, board, tf, *, refresh=False, overrides=None, provenance=None, progress=None,
             with_minutes=False, settings=None, allow_http=True):
        symbol, board = symbol.strip().upper(), board.strip().upper()
        if settings is None:
            from .settings import IndicatorSettings
            settings=IndicatorSettings()
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
        # Old snapshots used request wall time. Tighten their horizon once,
        # without redownloading raw history or promoting stale forming bars.
        if saved is not None:
            recorded_delay = timedelta(seconds=saved.metadata.get('feed_delay_seconds', 0))
            if recorded_delay < self.feed_delay:
                requested = saved.metadata.get('request_started_at', (saved.asof + recorded_delay).isoformat())
                cutoff = saved.asof - (self.feed_delay - recorded_delay)
                saved = MarketSnapshot(saved.symbol, saved.board, cutoff, saved.hours, saved.minutes,
                                        {**saved.metadata, 'request_started_at': requested,
                                         'feed_delay_seconds': int(self.feed_delay.total_seconds()),
                                         'snapshot_asof': cutoff.isoformat()}, saved.native_higher)
        needs_minutes = with_minutes or tf in ('M1', 'M5') and not (overrides or {}).get(tf)
        has_native = saved is not None and all(saved.native_higher.get(t) for t in NATIVE_HIGHER)
        must_fetch = saved is None or refresh or not has_native or needs_minutes and not saved.minutes
        snapshot = saved
        if must_fetch and not allow_http:
            # Never issue an implicit HTTP cold-start, catchup or repair.
            # A short WebSocket snapshot is not enough to rebuild full v2.11 history.
            if saved is None or not has_native or (needs_minutes and not saved.minutes):
                raise RuntimeError('HTTP выключен: нет полной истории в кэше. '
                                   'Включите галочку «HTTP» для первой загрузки.')
            warning = 'HTTP выключен: показана сохранённая история'
        if must_fetch and allow_http:
            try:
                snapshot = self._fetch(symbol, board, saved, needs_minutes, sessions, progress)
                tier = 'network'
                try:
                    self.store.write(key, snapshot)
                except (sqlite3.Error, OSError) as exc:
                    warning = f'Данные загружены; кеш недоступен: {exc}'
            except Exception as exc:
                if saved is None or not has_native or needs_minutes and not saved.minutes:
                    raise
                snapshot, tier = saved, 'offline'
                warning = f'Обновление недоступно; сохранённые данные: {exc}'
        self.memory[key] = snapshot
        if allow_http and self._refresh_tick(snapshot):
            try:
                self.store.write_delta(key,snapshot,{})
            except (sqlite3.Error,OSError) as exc:
                warning=f'MINSTEP cache unavailable: {exc}'
        self.memory.move_to_end(key)
        while len(self.memory) > 4:
            self.memory.popitem(last=False)
        series = snapshot.sources(sessions)
        calculation_key=f'{settings.calculation_key()}|tick:{snapshot.metadata.get("minimum_tick") or .01}'
        if snapshot.calculation_key!=calculation_key:
            snapshot.analyses={}
            snapshot.calculation_key=calculation_key
        source_provenance = {t: {**bundle_source_info(snapshot.metadata, t),
                                  'minimum_tick':snapshot.metadata.get('minimum_tick'),
                                  'first': bars[0].time.isoformat(), 'last': bars[-1].time.isoformat(),
                                  'bars': len(bars), 'snapshot_asof': snapshot.asof.isoformat()}
                             for t, bars in series.items()}
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
                                               provenance={**source_provenance, **(provenance or {})},settings=settings)
        else:
            horizon=min(snapshot.asof,candle_close_time(series[tf][-1],tf))
            if (tf not in snapshot.analyses or snapshot.analyses[tf].asof!=horizon or
                    any(t not in snapshot.analyses for t in series if TIMEFRAMES.index(t)>=TIMEFRAMES.index(tf))):
                progress('Расчёт потоков по загруженной истории')
                main, parents = analyze_timeframes(series, tf, asof=snapshot.asof,
                                                   provenance=source_provenance, cached=snapshot.analyses,settings=settings)
                snapshot.analyses.update({main.timeframe: main, **parents})
            main = snapshot.analyses[tf]
            parents = {t: a for t, a in snapshot.analyses.items()
                       if TIMEFRAMES.index(t) > TIMEFRAMES.index(tf)}
        # Native close publications can stop with the session. Arrange one
        # fresh HTTP observation after an outstanding calendar boundary;
        # the wall clock alone must never confirm a saved partial candle.
        awaiting = [candle_close_time(c, t) for t in ('H1', *NATIVE_HIGHER)
                    for c in series.get(t, [])[-2:] if candle_close_time(c, t) > snapshot.asof]
        confirmation_at = (min(awaiting) + self.feed_delay + timedelta(seconds=30)).isoformat() if awaiting else None
        return main, parents, {'symbol': symbol, 'board': board, 'tf': tf, 'tier': tier,
                               'asof': main.asof.isoformat(), 'warning': warning,
                               'requested_at': snapshot.metadata.get('request_started_at', snapshot.asof.isoformat()),
                               'stream_received_at': snapshot.metadata.get('stream_received_at'),
                               'stream_repair_required': snapshot.metadata.get('stream_repair_required', False),
                               'next_confirmation_at': confirmation_at,
                               'feed_delay_seconds': snapshot.metadata.get('feed_delay_seconds', 0)}

