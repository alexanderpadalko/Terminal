"""Recover official MINSTEP without changing a saved market observation."""
from datetime import datetime,timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from flowdesk.market_cache import MarketService,MarketSnapshot
from flowdesk.market_data import Candle,MOSCOW,aggregate,security_tick


class TestTickRecovery(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.now=datetime(2026,11,12,12,20,tzinfo=MOSCOW)
        self.asof=self.now-timedelta(minutes=15)
        self.path=Path(self.folder.name)/'cache.sqlite3'
        self.service=MarketService(self.path,clock=lambda:self.now,load_metadata=True)
        bars=[Candle(self.now.replace(hour=10,minute=0),8,9,7,8.5,123)]
        self.saved=MarketSnapshot('AFKS','TQBR',self.asof,bars,None,{'feed_delay_seconds':900},
                                  {tf:aggregate(bars,tf) for tf in ('D1','W1','MN1')})
        self.key=self.service.key('AFKS','TQBR',{})
        self.service.store.write(self.key,self.saved)
        sessions=patch('flowdesk.market_cache.load_session_overrides',return_value={})
        sessions.start()
        self.addCleanup(sessions.stop)

    def test_cached_snapshot_recovers_tick_without_ohlcv_request_or_confirmation(self):
        with patch('flowdesk.market_data.security_tick',return_value=.001) as metadata, \
             patch('flowdesk.market_cache.load_moex_session_bundle') as archive:
            analysis,_,_=self.service.view('AFKS','TQBR','H1')
        archive.assert_not_called()
        metadata.assert_called_once()
        self.assertEqual(analysis.minimum_tick,.001)
        restored=self.service.store.read(self.key)
        self.assertEqual((restored.asof,restored.hours),(self.asof,self.saved.hours))
        self.assertEqual(restored.metadata['minimum_tick'],.001)

    def test_failed_tick_lookup_retries_after_backoff_and_invalidates_analysis(self):
        with patch('flowdesk.market_data.security_tick',side_effect=[None,.001]) as metadata:
            first,_,_=self.service.view('AFKS','TQBR','H1')
            self.now+=timedelta(seconds=30)
            same,_,_=self.service.view('AFKS','TQBR','H1')
            self.assertIs(first,same)
            self.assertEqual(metadata.call_count,1)
            self.now+=timedelta(seconds=31)
            recovered,_,_=self.service.view('AFKS','TQBR','H1')
        self.assertEqual(metadata.call_count,2)
        self.assertIsNot(first,recovered)
        self.assertEqual((recovered.minimum_tick,recovered.asof),(.001,first.asof))
        self.assertEqual(self.service.store.read(self.key).asof,self.asof)

    def test_unavailable_tick_response_is_not_cached_for_the_entire_day(self):
        from io import StringIO
        empty='{"securities":{"columns":["SECID","MINSTEP"],"data":[]}}'
        valid='{"securities":{"columns":["SECID","MINSTEP"],"data":[["AFKS",0.001]]}}'
        with patch('flowdesk.market_data.urllib.request.urlopen',side_effect=[StringIO(empty),StringIO(valid)]) as request:
            self.assertIsNone(security_tick('AFKS','TQBR','2099-01-01'))
            self.assertEqual(security_tick('AFKS','TQBR','2099-01-01'),.001)
        self.assertEqual(request.call_count,2)
