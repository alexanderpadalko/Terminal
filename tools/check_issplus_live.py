"""Exercise the exact production client and cache on SBER, with a forced drop."""
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowdesk.iss_plus import IssPlusClient
from flowdesk.market_cache import MarketService
from flowdesk.stream_cache import StreamGap


async def main():
    from websockets.asyncio.client import connect
    output = Path('issplus-live-result.json')
    result = {'started': datetime.now(timezone.utc).isoformat(), 'counts': {}, 'statuses': [],
              'errors': [], 'http_repairs': 0, 'stream_batches': 0, 'ready_epochs': [], 'delay_samples': []}
    sockets = []
    async def connector(*args, **kwargs):
        ws = await connect(*args, **kwargs)
        sockets.append(ws)
        return ws
    queue = asyncio.Queue()
    client = IssPlusClient('SBER', connector=connector)
    task = None
    try:
        with tempfile.TemporaryDirectory() as directory:
            service = MarketService(Path(directory) / 'cache.sqlite3')
            await asyncio.to_thread(service.view, 'SBER', 'TQBR', 'M1', with_minutes=True)
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
                    if event.state == 'ready':
                        result['ready_epochs'].append(epoch)
                    if event.state == 'unavailable':
                        raise RuntimeError(event.detail)
                else:
                    counts[event.timeframe or event.kind] += 1
                    if event.kind == 'quote':
                        q = event.quote
                        if q.get('source_timestamp') and q.get('published'):
                            result['delay_samples'].append((q['published'] - q['source_timestamp']) / 1000)
                    if event.kind == 'candles' and repaired:
                        try:
                            await asyncio.to_thread(service.ingest_stream, 'SBER', 'TQBR', [event])
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
            assert result['stream_batches'] > 5, result
            result['success'] = True
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
