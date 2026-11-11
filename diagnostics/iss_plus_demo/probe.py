"""Read-only MOEX ISS+ DEMO probe. No imports from the terminal or indicator.

Requires Python 3.11+ and websockets==15.0.1 for network tests.
python probe.py --mode full --out evidence
python probe.py --mode environment --out local-environment

Every transmitted market-data command and received STOMP frame is timestamped
in frames.ndjson. HTTP controls are performed outside streaming windows.
Only public DEMO/guest credentials are used; no account secrets are read.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import importlib.metadata
import json
from pathlib import Path
import platform
import socket
import statistics
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

UTC = timezone.utc
MSK = ZoneInfo('Europe/Moscow')
URL = 'wss://iss.moex.com/infocx/v3/websocket'
TICKER = 'MXSE.TQBR.SBER'
BASE = 'https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/SBER'


def stamp():
    return datetime.now(UTC).isoformat()


def stats(values):
    if not values:
        return {'count': 0}
    ordered = sorted(values)
    return {'count': len(values), 'min': round(min(values), 4),
            'median': round(statistics.median(values), 4),
            'p95': round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 4),
            'max': round(max(values), 4)}


class Parser:
    """Handles heartbeats, concatenated frames and byte content-length."""
    def __init__(self):
        self.buffer = b''

    def feed(self, data):
        self.buffer += data.encode() if isinstance(data, str) else data
        frames = []
        while self.buffer:
            self.buffer = self.buffer.lstrip(b'\r\n')
            candidates = [(self.buffer.find(b'\n\n'), 2), (self.buffer.find(b'\r\n\r\n'), 4)]
            candidates = [(i, n) for i, n in candidates if i >= 0]
            if not candidates:
                break
            head_end, separator = min(candidates)
            parts = self.buffer[:head_end].decode('utf-8', 'replace').splitlines()
            headers = dict(p.split(':', 1) for p in parts[1:] if ':' in p)
            body_start = head_end + separator
            if 'content-length' in headers:
                body_end = body_start + int(headers['content-length'])
                if len(self.buffer) <= body_end:
                    break
                if self.buffer[body_end:body_end + 1] != b'\0':
                    raise ValueError('Invalid STOMP content-length terminator')
            else:
                body_end = self.buffer.find(b'\0', body_start)
                if body_end < 0:
                    break
            frames.append({'command': parts[0], 'headers': headers,
                           'body': self.buffer[body_start:body_end].decode('utf-8', 'replace')})
            self.buffer = self.buffer[body_end + 1:]
        return frames


def rows_from(body):
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return {}, []
    if not isinstance(payload, dict):
        return {}, []
    columns = payload.get('columns', [])
    names = [c if isinstance(c, str) else c.get('name', '') if isinstance(c, dict) else c[0] for c in columns]
    data = payload.get('data', [])
    if not isinstance(data, list):
        return payload.get('properties', {}), []
    return payload.get('properties', {}), [r if isinstance(r, dict) else dict(zip(names, r)) for r in data]


def exchange_datetime(value, reference):
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=MSK)
    except ValueError:
        try:
            clock = datetime.strptime(value.split('.')[0], '%H:%M:%S').time()
            today = reference.astimezone(MSK).date()
            candidates = [datetime.combine(today + timedelta(days=d), clock, MSK) for d in (-1, 0, 1)]
            return min(candidates, key=lambda x: abs((reference - x).total_seconds()))
        except ValueError:
            return None


class Evidence:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.raw = (self.directory / 'frames.ndjson').open('w', encoding='utf-8')
        self.clock_offset = 0.0
        self.clock_samples = []
        self.http = []
        self.streams = []
        self.recipe = None
        self.connection_attempts = []

    def event(self, label, direction, **payload):
        event = {'received_utc': stamp(), 'monotonic': time.monotonic(),
                 'label': label, 'direction': direction, **payload}
        self.raw.write(json.dumps(event, ensure_ascii=False) + '\n')
        self.raw.flush()
        return event

    def http_control(self, name, url):
        start_wall, start_mono = datetime.now(UTC), time.monotonic()
        record = {'name': name, 'url': url, 'started_at': start_wall.isoformat()}
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'FlowDesk-ISSPlus-Diagnostic/1.0', 'Cache-Control': 'no-cache'})
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
                record.update(status=response.status, server_date=response.headers.get('Date'),
                              age_header=response.headers.get('Age'), payload=payload)
            end = datetime.now(UTC)
            record['rtt_seconds'] = time.monotonic() - start_mono
            if record['server_date']:
                server = parsedate_to_datetime(record['server_date'])
                midpoint = start_wall + (end - start_wall) / 2
                offset = (server - midpoint).total_seconds()
                self.clock_samples.append(offset)
                self.clock_offset = statistics.median(self.clock_samples)
                record['http_date_clock_offset_seconds'] = offset
                record['clock_bound_seconds'] = .5 + record['rtt_seconds'] / 2
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
        record['finished_at'] = stamp()
        self.http.append(record)
        self.event(name, 'http_control', record=record)
        print('HTTP_CONTROL', name, record.get('status', record.get('error')), flush=True)

    def controls(self, suffix):
        params = urllib.parse.urlencode({'iss.meta': 'off', 'iss.only': 'marketdata',
            'marketdata.columns': 'SECID,LAST,TIME,SYSTIME,UPDATETIME,TRADINGSTATUS,NUMTRADES,VOLTODAY'})
        self.http_control('quote_' + suffix, BASE + '.json?' + params)
        start = (datetime.now(MSK) - timedelta(minutes=40)).strftime('%Y-%m-%d %H:%M:%S')
        params = urllib.parse.urlencode({'iss.meta': 'off', 'interval': 1, 'from': start})
        self.http_control('M1_' + suffix, BASE + '/candles.json?' + params)


class Session:
    def __init__(self, evidence, label):
        self.evidence, self.label = evidence, label
        self.parser = Parser()
        self.ws = None
        self.auth = None
        self.message_times = []
        self.packet_types = Counter()
        self.commands = Counter()
        self.ages = {}
        self.samples = []
        self.errors = []
        self.state = {}
        self.received_rows = 0
        self.started = None

    async def send(self, command, headers=None, body=''):
        headers = headers or {}
        frame = command + '\n' + ''.join(f'{k}:{v}\n' for k, v in headers.items()) + '\n' + body + '\0'
        await self.ws.send(frame)
        self.evidence.event(self.label, 'send', command=command,
                             headers={k: '[DEMO guest]' if k == 'passcode' else v for k, v in headers.items()}, body=body)

    async def open(self, recipe):
        from websockets.asyncio.client import connect
        attempt = {'label': self.label, 'recipe': recipe, 'started_at': stamp()}
        self.evidence.connection_attempts.append(attempt)
        try:
            self.ws = await connect(URL, subprotocols=recipe['subprotocols'], proxy=None,
                                    open_timeout=15, close_timeout=3, ping_interval=15,
                                    ping_timeout=10, max_size=4 * 1024 * 1024)
            attempt.update(websocket_connected=True, negotiated_subprotocol=self.ws.subprotocol,
                           response_status=self.ws.response.status_code)
            headers = {'domain': 'DEMO', 'login': 'guest', 'passcode': 'guest'}
            if recipe['versions']:
                headers.update({'accept-version': '1.2,1.1,1.0', 'host': 'iss.moex.com', 'heart-beat': '10000,10000'})
            await self.send('CONNECT', headers)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                message = await asyncio.wait_for(self.ws.recv(), timeout=max(.1, deadline - time.monotonic()))
                for frame in self.parser.feed(message):
                    self.evidence.event(self.label, 'receive', **frame)
                    if frame['command'] == 'CONNECTED':
                        self.auth = frame
                        attempt.update(stomp_connected=True, connected_frame=frame, finished_at=stamp())
                        return True
                    if frame['command'] == 'ERROR':
                        attempt['stomp_error'] = frame
                        self.errors.append(frame)
                        raise RuntimeError('STOMP ERROR: ' + frame['body'][:500])
            raise TimeoutError('No CONNECTED frame')
        except Exception as exc:
            attempt.update(error=f'{type(exc).__name__}: {exc}', finished_at=stamp())
            self.evidence.event(self.label, 'connection_error', error=attempt['error'])
            await self.close()
            return False

    def receive(self, frame):
        event = self.evidence.event(self.label, 'receive', **frame)
        command = frame['command']
        self.commands[command] += 1
        if command == 'ERROR':
            self.errors.append(frame)
            return
        if command != 'MESSAGE':
            return
        properties, rows = rows_from(frame['body'])
        if not rows:
            return
        self.received_rows += len(rows)
        self.message_times.append(event['monotonic'])
        kind = properties.get('type', 'unknown')
        self.packet_types[kind] += 1
        self.state.update(rows[-1])
        if len(self.samples) < 2 or len(self.message_times) % 10 == 0:
            self.samples.append({'received_utc': event['received_utc'], 'properties': properties,
                                 'rows': rows[-2:], 'merged_last_row': dict(self.state)})
        reference = datetime.fromisoformat(event['received_utc']) + timedelta(seconds=self.evidence.clock_offset)
        # Only fields actually present in the received packet are timed. The
        # last historical row isn't confused with first snapshot history.
        for key in ('TIME', 'TILL', 'TRADETIME', 'SYSTIME', 'UPDATETIME'):
            value = rows[-1].get(key)
            exchange = exchange_datetime(value, reference)
            if exchange is not None:
                item = {'received_utc': event['received_utc'], 'exchange_value': value,
                        'exchange_utc': exchange.astimezone(UTC).isoformat(), 'packet_type': kind,
                        'age_seconds': (reference - exchange).total_seconds(),
                        'date_inferred': isinstance(value, str) and len(value.split('.')[0]) <= 8}
                self.ages.setdefault(key, []).append(item)

    async def observe(self, seconds, stop_after_message=False):
        deadline, heartbeat = time.monotonic() + seconds, time.monotonic()
        while time.monotonic() < deadline:
            try:
                message = await asyncio.wait_for(self.ws.recv(), timeout=min(3, max(.1, deadline - time.monotonic())))
                if message in ('\n', b'\n', '\r\n', b'\r\n'):
                    self.evidence.event(self.label, 'receive_heartbeat')
                for frame in self.parser.feed(message):
                    self.receive(frame)
                    if frame['command'] == 'ERROR':
                        return
                if stop_after_message and self.message_times:
                    return
            except asyncio.TimeoutError:
                pass
            except Exception as exc:
                self.errors.append({'transport_error': f'{type(exc).__name__}: {exc}'})
                self.evidence.event(self.label, 'transport_error', error=self.errors[-1])
                return
            if time.monotonic() - heartbeat >= 5:
                try:
                    await self.ws.send('\n')
                    self.evidence.event(self.label, 'send_heartbeat')
                    heartbeat = time.monotonic()
                except Exception as exc:
                    self.errors.append({'heartbeat_error': str(exc)})
                    return

    async def subscribe(self, destination, selector):
        self.started = time.monotonic()
        await self.send('SUBSCRIBE', {'id': self.label, 'receipt': self.label,
                                    'destination': destination, 'selector': selector})

    async def close(self):
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    def summary(self, destination, selector):
        times = self.message_times
        return {'label': self.label, 'destination': destination, 'selector': selector,
                'authenticated': self.auth is not None, 'commands': dict(self.commands),
                'data_messages': len(times), 'received_rows': self.received_rows,
                'packet_types': dict(self.packet_types), 'errors': self.errors,
                'first_message_after_subscribe_seconds': times[0] - self.started if times and self.started else None,
                'data_span_seconds': times[-1] - times[0] if len(times) > 1 else 0,
                'interarrival_seconds': stats([b - a for a, b in zip(times, times[1:])]),
                'ages_seconds': {field: {'all': stats([x['age_seconds'] for x in values]),
                                        'updates': stats([x['age_seconds'] for x in values if x['packet_type'] == 'updates']),
                                        'samples': values[:3] + values[-3:]}
                                 for field, values in self.ages.items()},
                'samples': self.samples, 'final_state': self.state,
                'observed_without_http_requests': True}


async def stream(evidence, label, destination, selector, seconds):
    session = Session(evidence, label)
    if await session.open(evidence.recipe):
        try:
            await session.subscribe(destination, selector)
            await session.observe(seconds)
        except Exception as exc:
            session.errors.append({'subscription_error': f'{type(exc).__name__}: {exc}'})
    result = session.summary(destination, selector)
    await session.close()
    evidence.streams.append(result)
    print('STREAM_RESULT', json.dumps(result, ensure_ascii=False), flush=True)
    return result


async def reconnect_probe(evidence):
    selector = f'TICKER="{TICKER}" and LANGUAGE="en"'
    old = Session(evidence, 'disconnect_seed')
    result = {'drop_type': 'client TCP transport.abort(), not a simulated server failure'}
    if not await old.open(evidence.recipe):
        return {**result, 'status': 'initial_connection_failed'}
    await old.subscribe('MXSE.securities', selector)
    await old.observe(15, stop_after_message=True)
    result['before_drop_messages'] = len(old.message_times)
    result['drop_at'] = stamp()
    then = time.monotonic()
    old.ws.transport.abort()
    try:
        await asyncio.wait_for(old.ws.recv(), timeout=5)
        result['drop_detection'] = 'queued frame received before close; see close code'
    except Exception as exc:
        result['drop_detection'] = f'{type(exc).__name__}: {exc}'
    await old.close()
    result['drop_detected_after_seconds'] = time.monotonic() - then
    result['close_code'] = old.ws.close_code
    await asyncio.sleep(1)
    resumed = Session(evidence, 'reconnected')
    if not await resumed.open(evidence.recipe):
        return {**result, 'status': 'reconnect_failed'}
    await resumed.observe(3)
    result['unsolicited_messages_before_resubscribe'] = len(resumed.message_times)
    await resumed.subscribe('MXSE.securities', selector)
    await resumed.observe(20)
    result.update(status='reconnected_and_resubscribed', recovered_at=stamp(),
                  after_resubscribe=resumed.summary('MXSE.securities', selector),
                  application_resubscription_performed=True,
                  gap_replay_verified=False)
    await resumed.close()
    return result


def environment():
    result = {'utc': stamp(), 'moscow': datetime.now(MSK).isoformat(),
              'platform': platform.platform(), 'python': sys.version,
              'host': 'iss.moex.com', 'terminal_imported': False}
    try:
        result['dns_addresses'] = sorted({r[4][0] for r in socket.getaddrinfo('iss.moex.com', 443, type=socket.SOCK_STREAM)})
    except Exception as exc:
        result['dns_error'] = f'{type(exc).__name__}: {exc}'
    try:
        result['websockets_version'] = importlib.metadata.version('websockets')
    except importlib.metadata.PackageNotFoundError:
        result['websockets_version'] = None
    return result


async def run(args, evidence):
    report = {'started_at': stamp(), 'environment': environment(), 'endpoint': URL,
              'auth_identity': 'DEMO/guest', 'mode': args.mode,
              'official_docs': ['https://moexalgo.github.io/docs/websocket/websocket/',
                                'https://moexalgo.github.io/docs/websocket/docs/websocket-ru/'],
              'measurement_notes': [
                  'HTTP Date is a coarse gateway clock reference, not an independent exchange/NTP clock.',
                  'TIME age includes feed delay, batching, trade inactivity and network latency.',
                  'TILL is the last candle event time; historical snapshot ages do not establish feed delay.',
                  'No HTTP requests are made inside stream observation windows.',
                  'A channel error is reported separately from handshake/authentication failure.',
                  'Success on a hosted runner does not prove reachability from the user PC.']}
    if args.mode == 'environment':
        return report
    await asyncio.to_thread(evidence.controls, 'start')
    recipes = [{'subprotocols': ['STOMP'], 'versions': True},
               {'subprotocols': ['STOMP'], 'versions': False},
               {'subprotocols': ['v12.stomp', 'v11.stomp', 'v10.stomp'], 'versions': True}]
    for recipe in recipes:
        auth = Session(evidence, 'authentication')
        if await auth.open(recipe):
            evidence.recipe = recipe
            report['connected_frame'] = auth.auth
            await auth.close()
            break
    if evidence.recipe is None:
        if args.mode == 'full':
            # Establish whether ordinary delayed HTTP updates still arrive
            # when the independently tested WebSocket path is unavailable.
            for number in range(1, 4):
                await asyncio.sleep(15)
                await asyncio.to_thread(evidence.controls, 'fallback_' + str(number))
        return {**report, 'status': 'websocket_or_stomp_authentication_failed',
                'subscriptions_status': 'not_testable_without_authenticated_connection'}
    report['status'] = 'authenticated'
    core = 125 if args.mode == 'full' else 12
    await stream(evidence, 'securities', 'MXSE.securities', f'TICKER="{TICKER}" and LANGUAGE="en"', core)
    await stream(evidence, 'candles-M1', 'MXSE.candles', f'ticker="{TICKER}" and interval="M1" and quantity=2', core)
    if args.mode == 'full':
        await asyncio.to_thread(evidence.controls, 'after_core')
        for tf in ('M10', 'H1', 'D1', 'W1', 'm1', 'm3', 'Q1', 'M5', 'H4'):
            await stream(evidence, 'candles-' + tf, 'MXSE.candles', f'ticker="{TICKER}" and interval="{tf}" and quantity=2', 12)
        # Official examples use both ticker/TICKER. Probe the documented
        # uppercase spelling independently to diagnose selector compatibility.
        await stream(evidence, 'candles-M1-uppercase', 'MXSE.candles', f'TICKER="{TICKER}" and interval="M1" and quantity=2', 20)
    await stream(evidence, 'orderbooks', 'MXSE.orderbooks', f'TICKER="{TICKER}"', 25 if args.mode == 'full' else 7)
    # Not listed in the current official MXSE stream reference: this is an
    # explicit availability probe, not an assumed supported trades channel.
    await stream(evidence, 'trades-undocumented', 'MXSE.trades', f'TICKER="{TICKER}"', 10 if args.mode == 'full' else 5)
    if args.mode == 'full':
        report['reconnect'] = await reconnect_probe(evidence)
    await asyncio.to_thread(evidence.controls, 'end')
    return report


def self_test():
    parser = Parser()
    assert parser.feed('\nCONNECTED\nversion:1.2\n\n\0MESSAGE\ncontent-length:3\n\na\0b\0') == [
        {'command': 'CONNECTED', 'headers': {'version': '1.2'}, 'body': ''},
        {'command': 'MESSAGE', 'headers': {'content-length': '3'}, 'body': 'a\0b'}]
    assert parser.feed('MESS') == []
    assert parser.feed('AGE\r\nx:y\r\n\r\n{}\0')[0]['body'] == '{}'
    reference = datetime(2026, 11, 12, 0, 2, tzinfo=MSK)
    assert exchange_datetime('23:47:00', reference).day == 11
    assert rows_from('{"columns":["LAST"],"data":[[[300.1,2]]]}')[1][0]['LAST'] == [300.1, 2]
    print('SELF_TEST_OK')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('environment', 'smoke', 'full'), default='full')
    parser.add_argument('--out', default='iss-plus-evidence')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    evidence = Evidence(args.out)
    try:
        report = asyncio.run(run(args, evidence))
    except Exception as exc:
        report = {'status': 'probe_error', 'error': f'{type(exc).__name__}: {exc}', 'environment': environment()}
    report.update(finished_at=stamp(), streams=evidence.streams,
                  connection_attempts=evidence.connection_attempts,
                  clock_calibration={'method': 'HTTP Date versus local request midpoint',
                                     'median_offset_seconds': evidence.clock_offset,
                                     'offset_samples': evidence.clock_samples}, http_controls=evidence.http)
    evidence.raw.close()
    (evidence.directory / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('DIAGNOSTIC_SUMMARY_BEGIN')
    print(json.dumps(report, ensure_ascii=False))
    print('DIAGNOSTIC_SUMMARY_END')


if __name__ == '__main__':
    main()
