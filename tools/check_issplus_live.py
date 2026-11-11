"""Exercise the exact production client and cache on SBER, with a forced drop."""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import time
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowdesk.iss_plus import IssPlusClient, fixed
from flowdesk.market_cache import MarketService, MarketSnapshot, CACHE_POLICY, PUBLIC_FEED_DELAY
from flowdesk.market_data import (_iss_download, exclude_opening_auction, load_moex_session_bundle,
                                 load_session_overrides)
from flowdesk.pipeline import analyze_timeframes
from flowdesk.stream_cache import StreamGap


def seed_recent_http(service):
    # Test data only: full archive coverage is checked by archive-check.
    # Keep this network probe short enough to observe subscription updates.
    requested = service.clock()
    sessions = load_session_overrides()
    _, families, metadata = load_moex_session_bundle('SBER', 'H1', history_days=7,
        higher_history_days={tf: 120 for tf in ('D1', 'W1', 'MN1')}, auction_overrides=sessions)
    minutes = exclude_opening_auction(_iss_download('SBER', 1, days=3, limit=200000,
        timeout=15, board='TQBR', require_full=True), 'M1', board='TQBR', overrides=sessions)
    asof = requested - PUBLIC_FEED_DELAY
    metadata.update(request_started_at=requested.isoformat(), http_completed_at=service.clock().isoformat(),
                    snapshot_asof=asof.isoformat(), feed_delay_seconds=900, cache_policy=CACHE_POLICY)
    saved = MarketSnapshot('SBER', 'TQBR', asof, families['H1'], minutes, metadata,
                           {tf: families[tf] for tf in ('D1', 'W1', 'MN1')})
    key = service.key('SBER', 'TQBR', sessions)
    service.store.write(key, saved)
    service.memory[key] = saved
    service.view('SBER', 'TQBR', 'M1')


async def main():
    from websockets.asyncio.client import connect
    output = Path('issplus-live-result.json')
    result = {'started': datetime.now(timezone.utc).isoformat(), 'counts': {}, 'statuses': [],
              'errors': [], 'http_repairs': 0, 'stream_batches': 0, 'accepted_batches': 0,
              'minute_updates': 0, 'ready_epochs': [], 'delay_samples': [],
              'trade_changes_after_repair': 0, 'seed_history_days': {'H1': 7, 'M1': 3, 'higher': 120}}
    sockets = []
    async def connector(*args, **kwargs):
        ws = await connect(*args, **kwargs)
        sockets.append(ws)
        return ws
    queue = asyncio.Queue()
    client = IssPlusClient('SBER', connector=connector)
    task = None
    segments, segment, previous_trade_count = [], None, None
    try:
        with tempfile.TemporaryDirectory() as directory:
            service = MarketService(Path(directory) / 'cache.sqlite3')
            await asyncio.to_thread(seed_recent_http, service)
            task = asyncio.create_task(client.run(queue.put_nowait))
            counts, epoch, repaired, dropped = Counter(), 0, False, False
            deadline, started = time.monotonic() + 145, time.monotonic()
            while time.monotonic() < deadline:
                if not dropped and time.monotonic() - started > 72 and sockets:
                    sockets[-1].transport.abort()
                    dropped = True
                try:
                    event = await asyncio.wait_for(queue.get(), 1)
                except asyncio.TimeoutError:
                    continue
                if event.epoch != epoch:
                    epoch, repaired = event.epoch, False
                if event.kind == 'status':
                    result['statuses'].append({'epoch': epoch, 'state': event.state, 'detail': event.detail})
                    print('STATUS', epoch, event.state, event.detail, flush=True)
                    if event.state == 'resync':
                        _, _, info = await asyncio.to_thread(service.view, 'SBER', 'TQBR', 'M1',
                                                            refresh=True, with_minutes=True)
                        repaired = info['tier'] == 'network'
                        result['http_repairs'] += 1
                        segment = {'started': service.clock(), 'ended': None, 'first_trade': None}
                        segments.append(segment)
                        previous_trade_count = None
                    if event.state == 'reconnecting' and segment:
                        segment['ended'] = event.received_at
                    if event.state == 'ready':
                        result['ready_epochs'].append(epoch)
                    if event.state == 'unavailable':
                        raise RuntimeError(event.detail)
                else:
                    counts[event.timeframe or event.kind] += 1
                    if event.kind == 'quote':
                        q = event.quote
                        result['last_quote'] = {key: q.get(key) for key in ('LAST', 'TIME', 'TRADINGSTATUS', 'STATUS', 'NUMTRADES')}
                        if q.get('source_timestamp') and q.get('published'):
                            result['delay_samples'].append((q['published'] - q['source_timestamp']) / 1000)
                        if segment and event.received_at > segment['started'] and q.get('NUMTRADES') is not None:
                            count = fixed(q['NUMTRADES'])
                            if previous_trade_count is not None and count > previous_trade_count:
                                result['trade_changes_after_repair'] += 1
                                segment['first_trade'] = segment['first_trade'] or event.received_at
                            previous_trade_count = count
                    if event.kind == 'candles' and repaired:
                        try:
                            key = next(iter(service.memory))
                            before = service.memory[key]
                            await asyncio.to_thread(service.ingest_stream, 'SBER', 'TQBR', [event])
                            after = service.memory[key]
                            if after is not before:
                                result['accepted_batches'] += 1
                                if event.timeframe == 'M1' and before.minutes != after.minutes:
                                    result['minute_updates'] += 1
                                    print('M1_APPLIED', after.minutes[-1].time.isoformat(), after.asof.isoformat(), flush=True)
                            a, _, info = await asyncio.to_thread(service.view, 'SBER', 'TQBR', 'M1')
                            result['stream_batches'] += 1
                            if info.get('stream_repair_required'):
                                await asyncio.to_thread(service.view, 'SBER', 'TQBR', 'M1', refresh=True, with_minutes=True)
                                result['http_repairs'] += 1
                        except StreamGap:
                            await asyncio.to_thread(service.view, 'SBER', 'TQBR', 'M1', refresh=True, with_minutes=True)
                            result['http_repairs'] += 1
            result['counts'] = dict(counts)
            result['forced_drop'] = dropped
            result['connections'] = len(sockets)
            result['finished'] = datetime.now(timezone.utc).isoformat()
            assert {'M1', 'H1', 'D1', 'W1', 'MN1', 'quote'} <= counts.keys(), result
            assert len(set(result['ready_epochs'])) >= 2, result
            assert result['stream_batches'] >= 10, result
            expected = any(s['first_trade'] and (s['ended'] or service.clock()) - s['first_trade'] >= timedelta(seconds=75)
                           for s in segments)
            if expected:
                assert result['minute_updates'] > 0, 'Trades continued, but no M1 was applied after HTTP repair'
            result['live_m1_updates'] = 'OBSERVED' if result['minute_updates'] else 'NOT_OBSERVED'
            result['status'] = 'PASSED' if result['minute_updates'] else 'PARTIAL_NO_NEW_M1'
            if not result['minute_updates']:
                result['limitation'] = 'No new M1 observed after repairs; transport/reconnect checked, live candle updates remain unverified in this run.'
            cached, parents, _ = await asyncio.to_thread(service.view, 'SBER', 'TQBR', 'M1')
            saved = service.memory[next(iter(service.memory))]
            fresh, fresh_parents = await asyncio.to_thread(analyze_timeframes, saved.sources(), 'M1', asof=saved.asof)
            result['replay_timeframes'] = []
            for tf, a in {'M1': cached, **parents}.items():
                b = {'M1': fresh, **fresh_parents}[tf]
                assert (a.flows, a.fvg, a.pairs, a.confirmed_bars) == (b.flows, b.fvg, b.pairs, b.confirmed_bars), tf
                result['replay_timeframes'].append(tf)
            result['success'] = True
            shutil.copyfile(service.store.path, 'issplus-live-cache.sqlite3')
    except Exception as exc:
        result['errors'].append(f'{type(exc).__name__}: {exc}')
        raise
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
