import asyncio
import json
import unittest
from datetime import datetime

from flowdesk.iss_plus import FeedError, IssPlusClient, PacketDecoder, StompParser, encode_frame, fixed
from flowdesk.market_data import MOSCOW
from flowdesk.chart_time import candle_time_text

NOW = datetime(2026, 11, 11, 12, 16, tzinfo=MOSCOW)


def packet(columns, rows, kind='snapshot'):
    return json.dumps({'properties': {'type': kind, 'timestamp': 1000, 'published': 901000},
                       'columns': columns, 'data': rows})


class TestProtocol(unittest.TestCase):
    def test_candle_label_uses_from_and_never_receipt_or_publication_time(self):
        received = datetime(2026, 11, 12, 11, 38, 43, tzinfo=MOSCOW)
        d = PacketDecoder('SBER', 'TQBR', 1)
        body = packet(['FROM', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME'],
                      [['2026-11-12 11:23:00', 10, 12, 9, 11, 100]])
        event = d.decode({'subscription': 'M1'}, body, received)
        self.assertEqual(event.received_at, received)
        self.assertEqual(event.candles[0].time, received.replace(minute=23, second=0))
        self.assertEqual(candle_time_text(event.candles[0], 'M1'), '1м · 12.11.2026 11:23 МСК')

    def test_fragmented_utf8_bytes_and_heartbeats(self):
        parser = StompParser()
        body = 'Свеча'
        raw = f'\nMESSAGE\r\nsubscription:M1\r\ncontent-length:{len(body.encode())}\r\n\r\n'.encode() + body.encode() + b'\0\n'
        result = []
        for byte in raw:
            result.extend(parser.feed(bytes([byte])))
        self.assertEqual(result, [('MESSAGE', {'subscription': 'M1', 'content-length': '10'}, body)])

    def test_multiple_frames_and_embedded_null(self):
        self.assertEqual(len(StompParser().feed('MESSAGE\ncontent-length:3\n\na\0b\0\nCONNECTED\n\n\0')), 2)

    def test_bad_length_terminator_rejected(self):
        with self.assertRaises(FeedError):
            StompParser().feed('MESSAGE\ncontent-length:1\n\nab\0')

    def test_fixed_decimal_not_scaled_twice(self):
        self.assertEqual(fixed([298.53, 2]), 298.53)

    def test_quotes_partial_merge_and_wrong_symbol(self):
        d = PacketDecoder('SBER', 'TQBR', 1)
        d.decode({'subscription': 'quotes'}, packet(['TICKER', 'LAST', 'TRADINGSTATUS'],
                  [['MXSE.TQBR.SBER', [298.53, 2], 'T']]), NOW)
        result = d.decode({'subscription': 'quotes'}, packet(['TICKER', 'BID'],
                  [['MXSE.TQBR.SBER', [298.5, 2]]], 'updates'), NOW)
        self.assertEqual(result.quote['LAST'], 298.53)
        self.assertEqual(result.quote['BID'], 298.5)
        self.assertEqual(result.quote['TRADINGSTATUS'], 'T')
        other = PacketDecoder('SBER', 'TQBR', 1).decode({'subscription': 'quotes'},
                  packet(['TICKER', 'LAST'], [['MXSE.TQBR.AFKS', [8.5, 3]]]), NOW)
        self.assertIsNone(other)

    def test_partial_candle_and_native_midnight(self):
        d = PacketDecoder('SBER', 'TQBR', 1)
        columns = ['FROM', 'TILL', 'OPEN', 'HIGH', 'LOW', 'CLOSE', 'VOLUME']
        body = packet(columns, [['2026-11-11 07:00:00', '2026-11-11 23:59:59', [10, 2], [12, 2], [9, 2], [11, 2], 100]])
        result = d.decode({'subscription': 'D1'}, body, NOW)
        self.assertEqual(result.candles[0].time.hour, 0)
        result = d.decode({'subscription': 'D1'}, packet(['FROM', 'CLOSE', 'VOLUME'],
                        [['2026-11-11 07:00:00', [11.5, 2], 150]], 'updates'), NOW)
        self.assertEqual((result.candles[0].open, result.candles[0].close, result.candles[0].volume), (10, 11.5, 150))

    def test_missing_snapshot_is_error(self):
        with self.assertRaises(FeedError):
            PacketDecoder('SBER', 'TQBR', 1).decode({'subscription': 'M1'},
                packet(['FROM', 'CLOSE'], [['2026-11-11 12:00:00', 11]], 'updates'), NOW)


class FakeSocket:
    def __init__(self, frames):
        self.frames, self.sent, self.closed = list(frames), [], False

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if self.frames:
            item = self.frames.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        await asyncio.Future()

    async def close(self):
        self.closed = True


class TestClient(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_resubscribes_and_cancel_closes_socket(self):
        sockets = [FakeSocket(['CONNECTED\n\n\0', ConnectionError('forced drop')]),
                   FakeSocket(['CONNECTED\n\n\0'])]
        calls, events = [], []

        async def connector(*args, **kwargs):
            calls.append(kwargs)
            return sockets[len(calls) - 1]

        task = asyncio.create_task(IssPlusClient('SBER', connector=connector, backoffs=(0,)).run(events.append))
        for _ in range(100):
            if len(calls) == 2 and len(sockets[1].sent) == 7:
                break
            await asyncio.sleep(.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(s.closed for s in sockets))
        self.assertTrue(all(len(s.sent) == 7 for s in sockets))
        self.assertIn('interval="m1"', sockets[1].sent[-1])
        self.assertEqual(calls[0]['subprotocols'], ['STOMP'])
        self.assertEqual([e.epoch for e in events if e.state == 'resync'], [1, 2])

    async def test_explicit_resource_error_stops_retry(self):
        socket = FakeSocket(['CONNECTED\n\n\0', 'ERROR\n\nnot authorized\0'])
        events = []
        async def connector(*args, **kwargs):
            return socket
        await asyncio.wait_for(IssPlusClient('SBER', connector=connector).run(events.append), 1)
        self.assertEqual(events[-1].state, 'unavailable')
        self.assertTrue(socket.closed)
