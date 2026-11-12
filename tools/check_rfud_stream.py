"""Actual DEMO RFUD snapshots, automatic M1 delivery and forced reconnect.

No ISS HTTP is used. Snapshot availability and updates are reported separately.
"""
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowdesk.iss_plus import (ENDPOINT, INTERVALS, IssPlusClient, PacketDecoder,
                              StompParser, encode_frame, subscriptions)
from flowdesk.stream_probe_policy import minute_updates_expected

SYMBOLS = ('MXZ6', 'SIZ6', 'CRZ6', 'EUZ6', 'RIZ6', 'EDZ6',
           'USDRUBF', 'CNYRUBF', 'EURRUBF', 'IMOEXF')


async def matrix(result):
    from websockets.asyncio.client import connect
    parser, ids = StompParser(), {}
    result.update(instruments={}, errors=[], http_market_requests=0)
    async with connect(ENDPOINT, subprotocols=['STOMP'], proxy=None, open_timeout=15,
                       close_timeout=3, ping_interval=15, max_size=4*1024*1024) as ws:
        await ws.send(encode_frame('CONNECT', {'domain':'DEMO','login':'guest','passcode':'guest',
            'accept-version':'1.2,1.1,1.0','host':'iss.moex.com','heart-beat':'10000,10000'}))
        authenticated = False
        async with asyncio.timeout(15):
            while not authenticated:
                for command, headers, body in parser.feed(await ws.recv()):
                    if command == 'ERROR':
                        raise RuntimeError(body)
                    authenticated |= command == 'CONNECTED'
        for symbol in SYMBOLS:
            result['instruments'][symbol] = {'counts':{},'candles':{},'errors':[]}
            decoder = PacketDecoder(symbol, 'RFUD', 1)
            for ident, destination, selector in subscriptions(symbol, 'RFUD'):
                key = f'{symbol}-{ident}'
                ids[key] = (symbol, ident, decoder)
                await ws.send(encode_frame('SUBSCRIBE', {'id':key,'receipt':key,
                    'destination':destination,'selector':selector}))
        deadline, heartbeat = time.monotonic()+35, time.monotonic()
        while time.monotonic() < deadline:
            try:
                data = await asyncio.wait_for(ws.recv(), 1)
            except asyncio.TimeoutError:
                data = ''
            for command, headers, body in parser.feed(data):
                key = headers.get('subscription', '')
                if command == 'ERROR':
                    entry = {'headers':headers,'body':body}
                    result['errors'].append(entry)
                    if key in ids:
                        result['instruments'][ids[key][0]]['errors'].append(entry)
                if command != 'MESSAGE' or key not in ids:
                    continue
                symbol, ident, decoder = ids[key]
                event = decoder.decode({**headers,'subscription':ident},body,datetime.now(timezone.utc))
                if event is None:
                    continue
                target = result['instruments'][symbol]
                target['counts'][ident] = target['counts'].get(ident,0)+1
                if event.kind == 'quote':
                    target['quote'] = {k:event.quote.get(k) for k in
                        ('TICKER','LAST','TIME','TRADINGSTATUS','source_timestamp','published')}
                else:
                    target['candles'][ident] = [c.time.isoformat() for c in event.candles]
            if time.monotonic()-heartbeat >= 5:
                await ws.send('\n')
                heartbeat = time.monotonic()
        for symbol, target in result['instruments'].items():
            target['all_snapshots_received'] = {'quotes',*INTERVALS} <= target['counts'].keys()
            print('RFUD_SNAPSHOTS', symbol, json.dumps(target, ensure_ascii=False), flush=True)
        assert all(v['all_snapshots_received'] and not v['errors']
                   for v in result['instruments'].values()), 'Some RFUD subscriptions did not supply data'


async def automatic(result):
    from websockets.asyncio.client import connect
    sockets, queue = [], asyncio.Queue()
    async def connector(*args, **kwargs):
        ws = await connect(*args, **kwargs)
        sockets.append(ws)
        return ws
    task = asyncio.create_task(IssPlusClient('MXZ6','RFUD',connector=connector).run(queue.put_nowait))
    result.update(symbol='MXZ6',board='RFUD',statuses=[],ready_epochs=[],http_market_requests=0,
                  changed_m1_packets=0,new_m1_keys=0,quote_publish_delays_seconds=[])
    previous, counts = {}, Counter()
    started, dropped = time.monotonic(), False
    try:
        while time.monotonic()-started < 150:
            if not dropped and time.monotonic()-started >= 50 and sockets and result['ready_epochs']:
                sockets[-1].transport.abort()
                dropped = True
            try:
                event = await asyncio.wait_for(queue.get(), 1)
            except asyncio.TimeoutError:
                continue
            if event.kind == 'status':
                result['statuses'].append({'epoch':event.epoch,'state':event.state,'detail':event.detail})
                if event.state == 'ready':
                    result['ready_epochs'].append(event.epoch)
                if event.state == 'unavailable':
                    raise RuntimeError(event.detail)
            else:
                counts[event.timeframe or event.kind] += 1
                if event.kind == 'quote':
                    q = event.quote
                    result['last_quote'] = {k:q.get(k) for k in ('TICKER','LAST','TIME','TRADINGSTATUS')}
                    if isinstance(q.get('published'),(int,float)) and isinstance(q.get('source_timestamp'),(int,float)):
                        result['quote_publish_delays_seconds'].append((q['published']-q['source_timestamp'])/1000)
                if event.timeframe == 'M1':
                    bars = {c.time:c for c in event.candles}
                    old = previous.get(event.epoch)
                    if old is not None:
                        result['new_m1_keys'] += len(bars.keys()-old.keys())
                        result['changed_m1_packets'] += int(any(old.get(k) != v for k,v in bars.items()))
                    previous[event.epoch] = {**(old or {}), **bars}
                    result['last_m1'] = [c.time.isoformat() for c in event.candles]
                    result['last_m1_begin_age_seconds'] = (event.received_at-event.candles[-1].time).total_seconds()
            result['counts'] = dict(counts)
            if (len(set(result['ready_epochs'])) >= 2 and
                    (result['changed_m1_packets'] or not minute_updates_expected(datetime.now(timezone.utc),'RFUD'))):
                break
        result.update(forced_drop=dropped,connections=len(sockets),
                      automatic_m1_changes_observed=result['changed_m1_packets']>0,
                      minute_updates_expected=minute_updates_expected(datetime.now(timezone.utc),'RFUD'))
        assert len(set(result['ready_epochs'])) >= 2, 'RFUD reconnect not verified'
        assert result['automatic_m1_changes_observed'] or not result['minute_updates_expected'], \
            'RFUD M1 updates beyond initial snapshots not observed during delayed session'
        result['validation'] = ('AUTOMATIC_M1_OBSERVED' if result['automatic_m1_changes_observed']
                                else 'SNAPSHOTS_AND_RECONNECT_ONLY_OUTSIDE_SESSION')
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def main():
    result = {'started_at':datetime.now(timezone.utc).isoformat(),'matrix':{},'mxz6':{},'passed':False}
    try:
        await matrix(result['matrix'])
        await automatic(result['mxz6'])
        result['passed'] = True
    finally:
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        Path('issplus-rfud-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print('RFUD_RESULT', json.dumps(result,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    asyncio.run(main())
