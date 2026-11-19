"""Daily stock ranking and HTTP-only scans, independent of live chart data."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest

from flowdesk.finam_market import FinamMarketService
from flowdesk.finam_universe import compose_universe,RU_FUTURES
from flowdesk.moex_reference import refresh_top10
from flowdesk.watchlist import WatchlistService,HISTORY_POLICY
from flowdesk.market_data import Candle,MOSCOW
from test_finam_optimization import Feed,NOW


class RankingSource:
    def __init__(self,path,clock):
        self.path=path;self.clock=clock;self.calls=0
        self.seed=WatchlistService(path=path.with_name('unused-seed-cache.json')).cached()
        self.seed['window']=[]
    def cached(self):
        return json.loads(self.path.read_text()) if self.path.exists() else deepcopy(self.seed)
    def refresh(self,*,cancelled,stocks_only):
        self.calls+=1
        value=deepcopy(self.seed)
        value.update(history_policy=HISTORY_POLICY,ranking_day=self.clock().astimezone(MOSCOW).date().isoformat(),
                     window=[f'2026-10-{day:02d}' for day in range(1,21)])
        self.path.write_text(json.dumps(value))
        return value


class ResourceBundlePolicyTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)

    def test_daily_ranking_is_shared_by_restarts_until_moscow_midnight(self):
        clock=[datetime(2026,11,17,20,59,tzinfo=timezone.utc)]
        first=RankingSource(self.path/'ranking.json',lambda:clock[0])
        self.assertEqual(len(refresh_top10(watchlist=first)[0]),10)
        second=RankingSource(first.path,lambda:clock[0])
        self.assertEqual(len(refresh_top10(watchlist=second)[0]),10)
        self.assertEqual((first.calls,second.calls),(1,0))
        clock[0]+=timedelta(minutes=2)  # 00:01 MSK; no old 06:00 cutoff.
        refresh_top10(watchlist=second)
        self.assertEqual(second.calls,1)
        refresh_top10(watchlist=second)
        self.assertEqual(second.calls,1)

    def test_same_day_with_old_ranking_policy_is_refreshed(self):
        clock=lambda:datetime(2026,11,17,15,tzinfo=timezone.utc)
        source=RankingSource(self.path/'ranking.json',clock)
        refresh_top10(watchlist=source)
        value=json.loads(source.path.read_text());value['history_policy']='outdated'
        source.path.write_text(json.dumps(value))
        refresh_top10(watchlist=source)
        self.assertEqual(source.calls,2)

    def test_scanner_http_quote_ignores_a_fresh_websocket_quote(self):
        api=Feed();service=FinamMarketService(self.path/'candles.sqlite',api=api,clock=lambda:NOW)
        service._quotes['SBER@MISX']=(NOW,{'last':999})
        with service.http_scan():
            result=service.latest_quote('SBER@MISX')
        self.assertEqual(result['last']['value'],'101')
        self.assertEqual(api.calls[0][1],'quote')
        self.assertIsNone(getattr(service._scan_local,'snapshot',None))

    def test_scanner_uses_http_candle_suffix_even_if_socket_changes_database(self):
        api=Feed();service=FinamMarketService(self.path/'candles.sqlite',api=api,clock=lambda:NOW)
        symbol='EDZ6@RTSX'
        service.refresh_series(symbol,('H1',))
        api.calls.clear()
        with service.http_scan():
            service.refresh_series(symbol,('H1',))
            last=api.rows[-1]
            service._upsert(symbol,'H1',[Candle(last.time,100,1000,99,999,5)],source='stream')
            self.assertEqual(service._series(symbol)['H1'][-1].close,101)
        self.assertEqual(service._series(symbol)['H1'][-1].close,999)
        self.assertEqual(len(api.calls),1)

    def test_universe_and_active_routing_are_finam_only_and_include_ed(self):
        from flowdesk.market_services import MarketServices
        universe,_=compose_universe()
        self.assertEqual(sum(len(rows) for rows in universe.values()),27)
        self.assertNotIn('crypto',universe)
        ed=next(row for row in universe[RU_FUTURES] if row.label=='EDZ2026')
        self.assertEqual(ed.name,'Евро / доллар')
        service=FinamMarketService(self.path/'candles.sqlite',api=Feed())
        routed=MarketServices(finam=service,directory=self.path)
        self.assertIs(routed.for_symbol('EDZ6@RTSX'),service)
        with self.assertRaises(ValueError):routed.for_symbol('BTCUSDT@BINANCE')
        self.assertFalse((self.path/'binance_candles_v1.sqlite3').exists())

    def test_screener_module_is_removed_from_application(self):
        import importlib.util
        self.assertIsNone(importlib.util.find_spec("flowdesk.finam_screener"))
