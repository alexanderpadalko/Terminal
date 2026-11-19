"""Reuse the selected chart's raw bars across consistent SQLite snapshots."""
from bisect import bisect_left
from collections import deque
from datetime import datetime, timedelta

from .cancellation import checkpoint
from .market_data import Candle, normalize
from .terminal_policy import SOURCE_TIMEFRAMES


# Measured in retained source rows (candles plus delivery observations).
# The selected chart alone gets a slot. Oversized histories use normal reads.
MAX_ROWS = 1_000_000


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

    def record(self, symbol, revision, frame=None, start=None):
        if symbol == self.symbol:
            self.changes.append((revision, frame, start))

    def inputs(self, symbol, revision):
        saved = self.snapshot if symbol == self.symbol else None
        if saved is None:
            return None, ()
        seen = saved[0]
        changes = tuple(row for row in self.changes if seen < row[0] <= revision)
        # Unknown revisions, including writes from another process, force an
        # ordinary read. WAL keeps the rows and this revision in one snapshot.
        if seen > revision or len(changes) != revision - seen:
            return None, ()
        return saved, changes

    def publish(self, symbol, revision, frames):
        saved = self.snapshot
        if symbol != self.symbol or (saved is not None and saved[0] > revision):
            return
        count = sum(len(bars) + len(observations) for bars, observations, _ in frames.values())
        self.snapshot = (revision, frames) if count <= MAX_ROWS else None


def read_frames(db, symbol, saved, changes):
    dirty = {}
    for _, frame, start in changes:
        if frame is None:
            continue  # Coverage changed, no bar or delivery rows changed.
        old = dirty.get(frame, start)
        dirty[frame] = min(old, start) if old is not None and start is not None else None

    frames = {} if saved is None else dict(saved[1])
    for frame in SOURCE_TIMEFRAMES if saved is None else dirty:
        checkpoint()
        previous = frames.get(frame)
        start = dirty.get(frame)
        if not previous or not previous[2]:
            start = None
        bars, observations, canonical = _read_frame(db, symbol, frame, start)
        if start is not None:
            prior_bars, prior_observations, _ = previous
            boundary = datetime.fromisoformat(start)
            bar_index = bisect_left(prior_bars, boundary, key=lambda bar: bar.time)
            obs_index = bisect_left(prior_observations, start, key=lambda row: row[1])
            bars = prior_bars[:bar_index] + bars
            observations = prior_observations[:obs_index] + observations
        frames[frame] = bars, observations, canonical
    return frames


def _canonical(value, parsed):
    return parsed.utcoffset() == timedelta(0) and value == parsed.isoformat()


def _read_frame(db, symbol, frame, start):
    where = 'WHERE symbol=? AND tf=?'
    params = (symbol, frame)
    if start is not None:
        where += ' AND ts>=?'
        params += (start,)
    bars = []
    canonical = True
    rows = db.execute(f'SELECT ts,o,h,l,c,v,closes_at FROM candles {where} ORDER BY ts', params)
    for index, (stamp, o, h, low, c, volume, end) in enumerate(rows):
        if index % 1024 == 0:
            checkpoint()
        opened = datetime.fromisoformat(stamp)
        canonical = canonical and _canonical(stamp, opened)
        bars.append(Candle(opened, o, h, low, c, volume, datetime.fromisoformat(end) if end else None))
    observations = []
    receipts = {}
    rows = db.execute(f'SELECT ts,observed_at FROM candle_source_observations {where} ORDER BY ts', params)
    for index, (stamp, received) in enumerate(rows):
        if index % 1024 == 0:
            checkpoint()
        # An archive reply provides one receipt time for an entire batch.
        observations.append((frame, stamp, receipts.setdefault(received, received)))
        # Check orphan or legacy rows too before relying on UTC text order.
        canonical = canonical and (len(stamp) in (25, 32) and stamp[10]=='T' and stamp.endswith('+00:00'))
    return (bars if canonical else normalize(bars)), observations, canonical
