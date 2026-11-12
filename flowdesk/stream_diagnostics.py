"""Bounded diagnostics using the exact bundled ISS+ client, without ISS HTTP."""
import asyncio
from collections import Counter
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
import traceback

from .asyncio_runtime import prepare_asyncio_runtime
from .iss_plus import IssPlusClient


async def probe(result,timeout=100):
    loop=asyncio.get_running_loop()
    try:
        result['executor_before']=await loop.run_in_executor(None,lambda:'ok')
    except Exception as exc:
        result['executor_before_error']=f'{type(exc).__name__}: {exc}'
    prepare_asyncio_runtime()
    result['executor_after']=await loop.run_in_executor(None,lambda:'ok')
    # Exercise asyncio's real DNS path as well as the executor constructor.
    addresses=await asyncio.wait_for(loop.getaddrinfo('iss.moex.com',443),20)
    result['dns_addresses']=len(addresses)
    queue=asyncio.Queue()
    task=asyncio.create_task(IssPlusClient('SBER','TQBR').run(queue.put_nowait))
    counts=Counter()
    result.update(statuses=[],counts={},http_market_requests=0)
    deadline=loop.time()+timeout
    ready=False
    try:
        while loop.time()<deadline:
            event=await asyncio.wait_for(queue.get(),max(.1,deadline-loop.time()))
            if event.kind=='status':
                result['statuses'].append({'state':event.state,'detail':event.detail,'epoch':event.epoch})
                ready=ready or event.state=='ready'
                if event.state=='unavailable' or 'lazy_import' in event.detail:
                    raise RuntimeError(event.detail)
            else:
                counts[event.timeframe or event.kind]+=1
                result['counts']=dict(counts)
                if event.timeframe=='M1':
                    result['last_m1']=[c.time.isoformat() for c in event.candles]
                if event.kind=='quote':
                    result['quote_time']=event.quote.get('TIME')
            if ready and counts['M1']>=2 and counts['quote']>=2:
                result['automatic_updates']=True
                return
        raise TimeoutError('ISS+ snapshots/automatic M1 update not received')
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def main(argv):
    if len(argv)!=1:
        return 2
    destination=Path(argv[0])
    result={'started_at':datetime.now(timezone.utc).isoformat(),
            'frozen':bool(getattr(sys,'frozen',False)),'python':sys.version,'passed':False}
    try:
        asyncio.run(probe(result))
        result['passed']=True
    except Exception as exc:
        result.update(error=f'{type(exc).__name__}: {exc}',traceback=''.join(traceback.format_exception(exc)))
    result['finished_at']=datetime.now(timezone.utc).isoformat()
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if result['passed'] else 1

