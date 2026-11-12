"""Read-only ISS+ DEMO transport; independent of Qt and indicator engines."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import re
import time

from .market_data import Candle, MOSCOW, candle_close_time, parse_time, period_floor
from .runtime_errors import record_exception
from .asyncio_runtime import prepare_asyncio_runtime

ENDPOINT = 'wss://iss.moex.com/infocx/v3/websocket'
INTERVALS = {'M1': 'M1', 'H1': 'H1', 'D1': 'D1', 'W1': 'W1', 'MN1': 'm1'}
MARKETS = {'TQBR': 'MXSE', 'RFUD': 'FORTS'}


def stream_ticker(symbol, board):
    market = MARKETS[board]
    if board == 'RFUD':
        from .futures_data import iss_symbol
        symbol = iss_symbol(symbol)
    return f'{market}.{board}.{symbol}'


def subscriptions(symbol, board):
    """FORTS uses lowercase selector names and native (Si/Eu) symbol case."""
    market, ticker = MARKETS[board], stream_ticker(symbol, board)
    field, language = ('ticker', 'language') if board == 'RFUD' else ('TICKER', 'LANGUAGE')
    yield 'quotes', f'{market}.securities', f'{field}="{ticker}" and {language}="en"'
    for tf, interval in INTERVALS.items():
        yield tf, f'{market}.candles', f'ticker="{ticker}" and interval="{interval}" and quantity=2'


class FeedError(Exception):
    pass


class GatewayTimeout(ConnectionError):
    pass


def stomp_failure(body):
    # Observed FORTS gateway response: a subscription timed out, not access
    # denial. Unknown resource/authentication errors must still stop retries.
    message = body.strip()[:250]
    # Distinct from asyncio.TimeoutError, which the receive loop uses for its
    # idle heartbeat tick. The gateway failure must reach the reconnect path.
    return (GatewayTimeout(message) if message.casefold() in ('таймаут операции', 'operation timed out')
            else FeedError(message))


@dataclass(frozen=True)
class FeedEvent:
    kind: str
    symbol: str
    board: str
    epoch: int
    received_at: datetime
    timeframe: str = ''
    candles: tuple[Candle, ...] = ()
    quote: dict = field(default_factory=dict)
    state: str = ''
    detail: str = ''


class StompParser:
    def __init__(self):
        self.buffer = b''

    def feed(self, data):
        self.buffer += data.encode('utf-8') if isinstance(data, str) else data
        if len(self.buffer) > 8 * 1024 * 1024:
            raise FeedError('Слишком большой STOMP-фрейм')
        frames = []
        while self.buffer:
            self.buffer = self.buffer.lstrip(b'\r\n')
            separators = [(self.buffer.find(b'\n\n'), 2), (self.buffer.find(b'\r\n\r\n'), 4)]
            separators = [(i, n) for i, n in separators if i >= 0]
            if not separators:
                break
            end, length = min(separators)
            lines = self.buffer[:end].decode('utf-8').splitlines()
            if not lines:
                break
            headers = dict(line.split(':', 1) for line in lines[1:] if ':' in line)
            start = end + length
            if 'content-length' in headers:
                size = int(headers['content-length'])
                if not 0 <= size <= 8 * 1024 * 1024:
                    raise FeedError('Неверная длина STOMP-фрейма')
                finish = start + size
                if len(self.buffer) <= finish:
                    break
                if self.buffer[finish:finish + 1] != b'\0':
                    raise FeedError('Неверный конец STOMP-фрейма')
            else:
                finish = self.buffer.find(b'\0', start)
                if finish < 0:
                    break
            frames.append((lines[0], headers, self.buffer[start:finish].decode('utf-8')))
            self.buffer = self.buffer[finish + 1:]
        return frames


def encode_frame(command, headers=None):
    return command + '\n' + ''.join(f'{k}:{v}\n' for k, v in (headers or {}).items()) + '\n\0'


def fixed(value):
    # ISS fixed values already contain a decimal price, plus its precision.
    return float(value[0] if isinstance(value, (list, tuple)) else value)


class PacketDecoder:
    def __init__(self, symbol, board, epoch):
        self.symbol, self.board, self.epoch = symbol, board, epoch
        self.ticker = stream_ticker(symbol, board)
        self.quote = {}
        self.rows = {}

    def decode(self, headers, body, received_at):
        subscription = headers.get('subscription', '')
        if subscription not in ('quotes', *INTERVALS):
            return None
        payload = json.loads(body)
        props = payload.get('properties', {})
        kind = props.get('type')
        columns = payload.get('columns', [])
        if not all(isinstance(c, str) for c in columns):
            raise FeedError('Неизвестный формат столбцов ISS+')
        rows = [dict(zip(columns, row)) for row in payload.get('data', [])]
        if subscription == 'quotes':
            if kind == 'snapshot':
                self.quote = {}
            for row in rows:
                if row.get('TICKER') != self.ticker:
                    continue
                self.quote.update(row)
            if not self.quote:
                return None
            quote = dict(self.quote)
            for key in ('LAST', 'BID', 'OFFER'):
                if quote.get(key) is not None:
                    quote[key] = fixed(quote[key])
            quote['source_timestamp'] = props.get('timestamp')
            quote['published'] = props.get('published')
            return FeedEvent('quote', self.symbol, self.board, self.epoch, received_at, quote=quote)
        if kind not in ('snapshot', 'updates'):
            return None
        tf = subscription
        state = self.rows.setdefault(tf, {})
        if kind == 'snapshot':
            state.clear()
        candles = []
        for row in rows:
            if not row.get('FROM'):
                raise FeedError('Обновление свечи без FROM')
            key = row['FROM']
            merged = {**state.get(key, {}), **row}
            if not all(merged.get(k) is not None for k in ('OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME')):
                raise FeedError('Неполное обновление свечи без исходного snapshot')
            state[key] = merged
            moment = parse_time(key).astimezone(MOSCOW)
            if tf in ('D1', 'W1', 'MN1'):
                moment = period_floor(moment, tf)
            candles.append(Candle(moment, *(fixed(merged[k]) for k in ('OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME'))))
        # This is only decoder state for partial packets, never history storage.
        for key in sorted(state)[:-8]:
            del state[key]
        if not candles:
            return None
        return FeedEvent('candles', self.symbol, self.board, self.epoch, received_at,
                         timeframe=tf, candles=tuple(candles))


class IssPlusClient:
    """One connection, explicit resubscription and bounded retry backoff."""
    def __init__(self, symbol, board='TQBR', *, connector=None, clock=None,
                 backoffs=(1, 2, 5, 10, 30), stale_seconds=150):
        self.symbol, self.board = symbol.strip().upper(), board.strip().upper()
        if not re.fullmatch(r'[A-Z0-9._-]{1,32}', self.symbol) or not re.fullmatch(r'[A-Z0-9_-]{2,24}', self.board):
            raise ValueError('Неверный инструмент потока')
        if self.board not in MARKETS:
            raise ValueError('Режим торгов не поддерживается потоком ISS+')
        self.connector = connector
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.backoffs, self.stale_seconds = backoffs, stale_seconds

    async def run(self, emit):
        prepare_asyncio_runtime()
        if self.connector is None:
            from websockets.asyncio.client import connect
            connector = connect
        else:
            connector = self.connector
        epoch, failures = 0, 0
        while True:
            epoch += 1
            decoder = PacketDecoder(self.symbol, self.board, epoch)
            def status(state, detail=''):
                emit(FeedEvent('status', self.symbol, self.board, epoch, self.clock(), state=state, detail=detail))
            status('connecting')
            ws = None
            try:
                ws = await connector(ENDPOINT, subprotocols=['STOMP'], proxy=None,
                                     open_timeout=15, close_timeout=3, ping_interval=15,
                                     ping_timeout=10, max_size=4 * 1024 * 1024)
                parser = StompParser()
                await ws.send(encode_frame('CONNECT', {'domain': 'DEMO', 'login': 'guest', 'passcode': 'guest',
                    'accept-version': '1.2,1.1,1.0', 'host': 'iss.moex.com', 'heart-beat': '10000,10000'}))
                authenticated = False
                deadline = time.monotonic() + 12
                while not authenticated:
                    data = await asyncio.wait_for(ws.recv(), max(.1, deadline - time.monotonic()))
                    for command, headers, body in parser.feed(data):
                        if command == 'ERROR':
                            raise stomp_failure(body)
                        if command == 'CONNECTED':
                            authenticated = True
                    if time.monotonic() > deadline and not authenticated:
                        raise TimeoutError('Нет ответа CONNECTED')
                status('resync')
                for ident, destination, selector in subscriptions(self.symbol, self.board):
                    await ws.send(encode_frame('SUBSCRIBE', {'id': ident, 'receipt': ident,
                        'destination': destination, 'selector': selector}))
                received = set()
                ready = False
                heartbeat = last_minute = time.monotonic()
                minute_trade_count = None
                pending_trade_since = None
                connected_at = heartbeat
                while True:
                    try:
                        data = await asyncio.wait_for(ws.recv(), timeout=3)
                        for command, headers, body in parser.feed(data):
                            if command == 'ERROR':
                                raise stomp_failure(body)
                            if command != 'MESSAGE':
                                continue
                            event = decoder.decode(headers, body, self.clock())
                            if event is None:
                                continue
                            received.add(headers.get('subscription'))
                            if event.timeframe == 'M1':
                                last_minute = time.monotonic()
                                minute_trade_count = decoder.quote.get('NUMTRADES')
                                pending_trade_since = None
                            elif event.kind == 'quote':
                                trades = event.quote.get('NUMTRADES')
                                if minute_trade_count is None:
                                    minute_trade_count = trades
                                elif trades is not None and trades != minute_trade_count and pending_trade_since is None:
                                    pending_trade_since = time.monotonic()
                            emit(event)
                            if not ready and {'quotes', *INTERVALS}.issubset(received):
                                ready = True
                                failures = 0
                                status('ready')
                    except asyncio.TimeoutError:
                        pass
                    now = time.monotonic()
                    if not ready and now - connected_at > 45:
                        raise TimeoutError('Не получены обязательные снимки потока')
                    if (decoder.quote.get('TRADINGSTATUS') == 'T' and pending_trade_since is not None and
                            now - pending_trade_since > self.stale_seconds and now - last_minute > self.stale_seconds):
                        raise TimeoutError('Приостановились обновления M1')
                    if now - heartbeat >= 5:
                        await ws.send('\n')
                        heartbeat = now
            except asyncio.CancelledError:
                raise
            except FeedError as exc:
                # An explicit resource/permission error isn't solved by a tight
                # reconnect loop. HTTP is used only with the user's opt-in.
                status('unavailable', str(exc))
                return
            except Exception as exc:
                record_exception('issplus_reconnect', exc)
                status('reconnecting', f'{type(exc).__name__}: {exc}')
                delay = self.backoffs[min(failures, len(self.backoffs) - 1)]
                failures += 1
            finally:
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
            await asyncio.sleep(delay)
