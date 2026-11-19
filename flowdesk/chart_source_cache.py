"""Reuse the selected chart's raw bars across consistent SQLite snapshots."""
from bisect import bisect_left, bisect_right
from collections import deque
from datetime import datetime, timedelta
from itertools import accumulate, chain
from sys import getsizeof

from .cancellation import checkpoint
from .market_data import Candle, normalize
from .terminal_policy import SOURCE_TIMEFRAMES


# Only the selected symbol is retained, with a limit on the estimated
# retained data rather than a count of candle and receipt rows.
MAX_CACHE_BYTES = 256 * 1024 * 1024
_CANDLE_BYTES = 104 + 48 + 5 * 24  # Candle slots, datetime, and OHLCV floats
_STAMP_WIDTH = 32
_CHUNK_ROWS = 4096
_STAMP_PADDING = b'\0' * _STAMP_WIDTH


class _CandleRows(list):
    __slots__ = ('scheduled_count',)

    def __init__(self):
        super().__init__()
        self.scheduled_count = 0

    def replaced(self, left, right, patch):
        result = _CandleRows()
        result.extend(self)
        result[left:right] = patch
        result.scheduled_count = self.scheduled_count + patch.scheduled_count - sum(
            self[index].scheduled_close is not None for index in range(left, right))
        return result

    def retained_bytes(self):
        return getsizeof(self) + len(self) * _CANDLE_BYTES + self.scheduled_count * 48


class _ReceiptChunk:
    __slots__ = ('stamps', 'receipts', 'legacy', 'size')

    def __init__(self, stamps, receipts, legacy):
        self.stamps = bytes(stamps)
        self.receipts = tuple(receipts)
        self.legacy = legacy
        self.size = (getsizeof(self) + getsizeof(self.stamps) + getsizeof(self.receipts) +
                     getsizeof(legacy) + sum(getsizeof(s) for s in set(receipts)) +
                     sum(getsizeof(i) + getsizeof(s) for i, s in legacy.items()))

    def stamp(self, index):
        stamp = self.stamps[index * _STAMP_WIDTH:(index + 1) * _STAMP_WIDTH]
        return stamp.rstrip(b'\0').decode('ascii') if stamp[0] else self.legacy[index]

    def section(self, start, end):
        if start == 0 and end == len(self.receipts):
            return self
        return _ReceiptChunk(self.stamps[start * _STAMP_WIDTH:end * _STAMP_WIDTH],
                             self.receipts[start:end],
                             {i - start: value for i, value in self.legacy.items() if start <= i < end})

    def joined(self, next_chunk):
        legacy = {**self.legacy, **{i + len(self.receipts): value for i, value in next_chunk.legacy.items()}}
        return _ReceiptChunk(self.stamps + next_chunk.stamps, self.receipts + next_chunk.receipts, legacy)


class _ReceiptRows:
    """Exact SQLite receipt triples with shared, fixed-size chunks of stamps."""

    __slots__ = ('frame', 'chunks', 'ends', 'size')

    def __init__(self, frame, chunks):
        self.frame = frame
        self.chunks = tuple(chunks)
        self.ends = tuple(accumulate(len(chunk.receipts) for chunk in self.chunks))
        self.size = (getsizeof(self) + getsizeof(self.chunks) + getsizeof(self.ends) +
                     sum(getsizeof(i) for i in self.ends) + sum(chunk.size for chunk in self.chunks))

    def __len__(self):
        return self.ends[-1] if self.ends else 0

    def __iter__(self):
        for chunk in self.chunks:
            for index, receipt in enumerate(chunk.receipts):
                yield self.frame, chunk.stamp(index), receipt

    def __getitem__(self, index):
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        chunk = bisect_right(self.ends, index)
        local = index - (self.ends[chunk - 1] if chunk else 0)
        data = self.chunks[chunk]
        return self.frame, data.stamp(local), data.receipts[local]

    def _sections(self, start, end):
        position = start
        index = bisect_right(self.ends, start)
        while position < end:
            origin = self.ends[index - 1] if index else 0
            stop = min(end, self.ends[index])
            yield self.chunks[index].section(position - origin, stop - origin)
            position = stop
            index += 1

    def replaced(self, left, right, patch):
        packed = []
        for chunk in chain(self._sections(0, left), patch.chunks, self._sections(right, len(self))):
            if packed and len(packed[-1].receipts) + len(chunk.receipts) <= _CHUNK_ROWS:
                packed[-1] = packed[-1].joined(chunk)
            else:
                packed.append(chunk)
        return _ReceiptRows(self.frame, packed)


class ChartSourceCache:
    """Caller coordinates selection and committed revisions under the service lock."""

    def __init__(self):
        self.symbol = None
        self.snapshot = None
        self.changes = deque(maxlen=32)

    def select(self, symbol):
        if self.symbol != symbol:
            self.symbol = symbol
            self.snapshot = None
            self.changes.clear()

    def record(self, symbol, revision, frame=None, start=None, end=None):
        if symbol == self.symbol:
            self.changes.append((revision, frame, start, end))

    def changes_since(self, symbol, first_revision, revision):
        if symbol != self.symbol or first_revision > revision:
            return None
        changes = tuple(row for row in self.changes if first_revision < row[0] <= revision)
        if any(row[0] != expected for expected, row in enumerate(changes, first_revision + 1)):
            return None
        if len(changes) != revision - first_revision:
            return None
        return changes

    def inputs(self, symbol, revision):
        saved = self.snapshot if symbol == self.symbol else None
        if saved is None:
            return None, ()
        # Unknown revisions, including writes from another process, force an
        # ordinary read. WAL keeps the rows and this revision in one snapshot.
        changes = self.changes_since(symbol, saved[0], revision)
        if changes is None:
            return None, ()
        return saved, changes

    def publish(self, symbol, revision, frames):
        saved = self.snapshot
        if symbol != self.symbol or (saved is not None and saved[0] > revision):
            return
        size = sum(bars.retained_bytes() + observations.size for bars, observations, _ in frames.values())
        self.snapshot = (revision, frames) if size <= MAX_CACHE_BYTES else None


def _changed_ranges(ranges):
    if any(start is None for start, _ in ranges):
        return [(None, None)]
    merged = []
    for start, end in sorted(ranges, key=lambda bounds: bounds[0]):
        if not merged:
            merged.append((start, end))
            continue
        prior_start, prior_end = merged[-1]
        if prior_end is None:
            break  # An unbounded suffix already covers every later range.
        if start <= prior_end:
            merged[-1] = (prior_start, max(prior_end, end) if end is not None else None)
        else:
            merged.append((start, end))
    return merged


def read_frames(db, symbol, saved, changes, *, source_frames=SOURCE_TIMEFRAMES):
    dirty = {}
    for _, frame, start, end in changes:
        if frame is not None and frame in source_frames:
            dirty.setdefault(frame, []).append((start, end))
    frames = {} if saved is None else {k:v for k,v in saved[1].items() if k in source_frames}
    for frame in source_frames if saved is None else dirty:
        checkpoint()
        previous = frames.get(frame)
        if not previous or not previous[2]:
            frames[frame] = _read_frame(db, symbol, frame, None)
            continue
        for start, end in _changed_ranges(dirty[frame]):
            if start is None:
                previous = _read_frame(db, symbol, frame, None)
                break
            bars, observations, canonical = _read_frame(db, symbol, frame, start, end)
            if not canonical:
                previous = _read_frame(db, symbol, frame, None)
                break
            old_bars, old_observations, _ = previous
            bar_left = bisect_left(old_bars, datetime.fromisoformat(start), key=lambda bar: bar.time)
            obs_left = bisect_left(old_observations, start, key=lambda row: row[1])
            bar_right, obs_right = len(old_bars), len(old_observations)
            if end is not None:
                bar_right = bisect_right(old_bars, datetime.fromisoformat(end), key=lambda bar: bar.time)
                obs_right = bisect_right(old_observations, end, key=lambda row: row[1])
            previous = (old_bars.replaced(bar_left, bar_right, bars),
                        old_observations.replaced(obs_left, obs_right, observations), True)
        frames[frame] = previous
    return frames


def _canonical(value, parsed):
    return parsed.utcoffset() == timedelta(0) and value == parsed.isoformat()


def _read_frame(db, symbol, frame, start, end=None):
    where = 'WHERE symbol=? AND tf=?'
    params = (symbol, frame)
    if start is not None:
        where += ' AND ts>=?'
        params += (start,)
        if end is not None:
            where += ' AND ts<=?'
            params += (end,)
    bars = _CandleRows()
    canonical = True
    previous_end = None
    previous_close = None
    rows = db.execute(f'SELECT ts,o,h,l,c,v,closes_at FROM candles {where} ORDER BY ts', params)
    for index, (stamp, o, h, low, c, volume, end) in enumerate(rows):
        if index % 1024 == 0:
            checkpoint()
        # Consecutive Finam candles commonly share this immutable timestamp.
        opened = previous_close if previous_end == stamp else datetime.fromisoformat(stamp)
        closed = datetime.fromisoformat(end) if end else None
        canonical = canonical and _canonical(stamp, opened)
        bars.append(Candle(opened, o, h, low, c, volume, closed))
        bars.scheduled_count += closed is not None
        previous_end, previous_close = end, closed
    chunks = []
    stamps, observed, legacy = bytearray(), [], {}
    receipts = {}
    rows = db.execute(f'SELECT ts,observed_at FROM candle_source_observations {where} ORDER BY ts', params)
    for index, (stamp, received) in enumerate(rows):
        if index % 1024 == 0:
            checkpoint()
        # An archive reply provides one receipt time for an entire batch.
        if len(stamp) <= _STAMP_WIDTH and stamp.isascii() and stamp and '\0' not in stamp:
            stamps.extend(stamp.encode('ascii').ljust(_STAMP_WIDTH, b'\0'))
        else:
            legacy[len(observed)] = stamp
            stamps.extend(_STAMP_PADDING)
        observed.append(receipts.setdefault(received, received))
        # Check orphan or legacy rows too before relying on UTC text order.
        canonical = canonical and (len(stamp) in (25, 32) and stamp[10]=='T' and stamp.endswith('+00:00'))
        if len(observed) == _CHUNK_ROWS:
            chunks.append(_ReceiptChunk(stamps, observed, legacy))
            stamps, observed, legacy = bytearray(), [], {}
    if observed:
        chunks.append(_ReceiptChunk(stamps, observed, legacy))
    if not canonical:
        bars[:] = normalize(bars)
        bars.scheduled_count = sum(bar.scheduled_close is not None for bar in bars)
    return bars, _ReceiptRows(frame, chunks), canonical
