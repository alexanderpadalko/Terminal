"""Simultaneous DEMO quotes/M1/D1 control; no terminal imports or changes."""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
import time
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diagnostics.iss_plus_demo.probe import (Evidence, Session, BASE, TICKER,
                                             MSK, stamp, environment)


def controls(evidence, suffix):
    evidence.controls(suffix)
    params = urllib.parse.urlencode({'iss.meta': 'off', 'interval': 24,
                                     'from': datetime.now(MSK).date().isoformat()})
    evidence.http_control('D1_' + suffix, BASE + '/candles.json?' + params)


async def run(evidence):
    report = {'started_at': stamp(), 'environment': environment()}
    await asyncio.to_thread(controls, evidence, 'before')
    connection = Session(evidence, 'parallel-control')
    recipe = {'subprotocols': ['STOMP'], 'versions': True}
    if not await connection.open(recipe):
        return {**report, 'status': 'connection_failed'}
    specs = [('securities', 'MXSE.securities', f'TICKER="{TICKER}" and LANGUAGE="en"'),
             ('M1', 'MXSE.candles', f'ticker="{TICKER}" and interval="M1" and quantity=2'),
             ('D1', 'MXSE.candles', f'ticker="{TICKER}" and interval="D1" and quantity=2')]
    streams = {}
    for label, destination, selector in specs:
        streams[label] = Session(evidence, label)
        streams[label].auth = connection.auth
        streams[label].started = time.monotonic()
        await connection.send('SUBSCRIBE', {'id': label, 'receipt': label,
                                           'destination': destination, 'selector': selector})
    deadline, heartbeat = time.monotonic() + 95, time.monotonic()
    while time.monotonic() < deadline:
        try:
            data = await asyncio.wait_for(connection.ws.recv(), timeout=3)
            for frame in connection.parser.feed(data):
                label = frame['headers'].get('subscription', frame['headers'].get('receipt-id'))
                if label in streams:
                    streams[label].receive(frame)
                else:
                    evidence.event('unrouted', 'receive', **frame)
        except asyncio.TimeoutError:
            pass
        if time.monotonic() - heartbeat >= 5:
            await connection.ws.send('\n')
            evidence.event('parallel-control', 'send_heartbeat')
            heartbeat = time.monotonic()
    await connection.close()
    report.update(status='completed', streams=[streams[label].summary(destination, selector)
                                              for label, destination, selector in specs])
    await asyncio.to_thread(controls, evidence, 'after')
    return report


def main():
    evidence = Evidence('iss-plus-consistency')
    try:
        report = asyncio.run(run(evidence))
    except Exception as exc:
        report = {'status': 'error', 'error': f'{type(exc).__name__}: {exc}'}
    report.update(finished_at=stamp(), http_controls=evidence.http,
                  clock_offset_seconds=evidence.clock_offset)
    evidence.raw.close()
    (evidence.directory / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('DIAGNOSTIC_SUMMARY_BEGIN')
    print(json.dumps(report, ensure_ascii=False))
    print('DIAGNOSTIC_SUMMARY_END')


if __name__ == '__main__':
    main()
