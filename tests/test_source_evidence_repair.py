"""A legacy raw row must be re-fetched, not silently accepted or hidden forever."""
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import tempfile,unittest

from flowdesk.finam_market import FinamMarketService
from flowdesk.finam_api import FinamAPIError
from flowdesk.market_data import Candle
from flowdesk.source_repair import repair_source_evidence
from flowdesk.trading_calendar import TradingCalendar

UTC=timezone.utc
START=datetime(2026,10,14,13,tzinfo=UTC)
NOW=datetime(2026,11,16,18,tzinfo=UTC)
SYMBOL='CRZ6@RTSX'


class Source:
    session_candles=True
    native_higher_frames=('D1','W1','MN1')
    def __init__(self):self.calls=[];self.rows=[];self.error=None
    def market_delay_minutes(self,mic):return 0
    def bars(self,symbol,tf,*,start=None,end=None):
        self.calls.append((symbol,tf,start,end))
        if self.error:raise self.error
        return [b for b in self.rows if start<=b.time<end]


class EvidenceRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.api=Source();self.clock=NOW
        self.service=FinamMarketService(Path(self.tmp.name)/'data.sqlite',api=self.api,
            clock=lambda:self.clock,calendar_factory=lambda s:TradingCalendar('CRYPTO_UTC','UTC'))
        self.rows=[Candle(START+timedelta(hours=i),12.6+(i%7)*.01,
                         12.62+(i%7)*.01,12.59+(i%7)*.01,12.61+(i%7)*.01,10)
                   for i in range(50)]
        self.api.rows=self.rows
        self.service._upsert(SYMBOL,'H1',self.rows)

    def unverify(self,left,right):
        with closing(self.service._connection()) as db,db:
            db.execute('DELETE FROM candle_source_observations WHERE symbol=? AND tf=? AND ts>=? AND ts<?',
                (SYMBOL,'H1',self.rows[left].time.isoformat(),self.rows[right].time.isoformat()))
            db.execute('UPDATE candle_source_revisions SET revision=revision+1 WHERE symbol=?',(SYMBOL,))

    def test_exact_interior_window_is_revalidated_without_changing_prices(self):
        self.unverify(3,35)
        before=self.service._series(SYMBOL)
        self.assertTrue(repair_source_evidence(self.service,SYMBOL,before,NOW))
        self.assertEqual(self.api.calls,[(SYMBOL,'H1',self.rows[3].time,self.rows[35].time)])
        after=self.service._series(SYMBOL)
        self.assertEqual(before,after)
        self.assertFalse(repair_source_evidence(self.service,SYMBOL,after,NOW))
        self.assertEqual(len(self.api.calls),1)

    def test_partial_or_empty_response_does_not_certify_missing_rows_or_retry_tightly(self):
        self.unverify(3,35);self.api.rows=self.rows[20:35]
        self.assertTrue(repair_source_evidence(self.service,SYMBOL,self.service._series(SYMBOL),NOW))
        raw=self.service._series(SYMBOL)
        known={ts for tf,ts,stamp in raw.source_state['observations'] if tf=='H1'}
        self.assertNotIn(self.rows[3].time.isoformat(),known)
        self.assertFalse(repair_source_evidence(self.service,SYMBOL,raw,NOW))
        self.assertEqual(len(self.api.calls),1)
        self.clock+=timedelta(minutes=6);self.api.rows=[]
        self.assertFalse(repair_source_evidence(self.service,SYMBOL,raw,self.clock))
        self.assertEqual(len(self.api.calls),2)

    def test_old_legacy_gap_precedes_a_recent_expired_snapshot(self):
        self.unverify(3,35)
        tail=self.rows[-1]
        with closing(self.service._connection()) as db,db:
            db.execute('UPDATE candle_source_observations SET observed_at=? WHERE symbol=? AND tf=? AND ts=?',
                ((tail.time+timedelta(minutes=5)).isoformat(),SYMBOL,'H1',tail.time.isoformat()))
        repair_source_evidence(self.service,SYMBOL,self.service._series(SYMBOL),NOW)
        self.assertEqual(self.api.calls[-1][2:],(self.rows[3].time,self.rows[35].time))

    def test_legacy_request_is_bounded_to_thirty_days(self):
        symbol='LONG@RTSX'
        rows=[Candle(START-timedelta(days=45)+timedelta(hours=i),12.6,12.8,12.5,12.7,1)
              for i in range(24*40)]
        self.service._upsert(symbol,'H1',rows);self.api.rows=rows
        with closing(self.service._connection()) as db,db:
            db.execute('DELETE FROM candle_source_observations WHERE symbol=?',(symbol,))
        repair_source_evidence(self.service,symbol,self.service._series(symbol),NOW)
        _,_,start,end=self.api.calls[-1]
        self.assertLessEqual(end-start,timedelta(days=30))
        known={ts for tf,ts,stamp in self.service._series(symbol).source_state['observations']}
        self.assertNotIn(rows[0].time.isoformat(),known)

    def test_stock_auction_is_not_revalidated_as_a_missing_calculation_bar(self):
        symbol='SBER@MISX';begin=START.replace(hour=15,minute=50)
        rows=[Candle(begin+timedelta(minutes=5*i),270,271,269,270,1) for i in range(3)]
        self.service._upsert(symbol,'M5',rows);self.api.rows=rows
        with closing(self.service._connection()) as db,db:
            db.execute('DELETE FROM candle_source_observations WHERE symbol=? AND ts=?',
                       (symbol,rows[1].time.isoformat()))
        self.assertFalse(repair_source_evidence(self.service,symbol,self.service._series(symbol),NOW))
        self.assertEqual(self.api.calls,[])
        with closing(self.service._connection()) as db,db:
            db.execute('DELETE FROM candle_source_observations WHERE symbol=? AND ts=?',
                       (symbol,rows[0].time.isoformat()))
        self.clock+=timedelta(minutes=2)
        repair_source_evidence(self.service,symbol,self.service._series(symbol),self.clock)
        self.assertEqual(self.api.calls[-1],(symbol,'M5',rows[0].time,rows[1].time))

    def test_missing_evidence_really_breaks_then_repaired_structure_matches_complete_source(self):
        self.unverify(3,35)
        raw=self.service._series(SYMBOL)
        broken=self.service.prepare_series(SYMBOL,raw,NOW)
        self.assertLess(len(broken['H1']),len(self.rows))
        self.assertIn(self.rows[35].time.isoformat(),broken.session_metadata['history_breaks']['H1'])
        repair_source_evidence(self.service,SYMBOL,raw,NOW)
        repaired=self.service.prepare_series(SYMBOL,self.service._series(SYMBOL),NOW)
        self.assertEqual(len(repaired['H1']),len(self.rows))
        self.assertNotIn(self.rows[35].time.isoformat(),repaired.session_metadata['history_breaks'].get('H1',()))
        from flowdesk.engine import analyze
        expected=analyze(self.rows,'H1',asof=NOW)
        actual=analyze(repaired['H1'],'H1',asof=NOW)
        self.assertTrue(expected.segments)
        self.assertEqual(actual.segments,expected.segments)
        self.assertEqual(actual.fvg,expected.fvg)

    def test_stale_closed_snapshot_needs_http_and_forming_snapshot_is_not_sealed(self):
        with closing(self.service._connection()) as db,db:
            db.execute('UPDATE candle_source_observations SET observed_at=? WHERE symbol=? AND tf=? AND ts=?',
                ((self.rows[4].time+timedelta(minutes=5)).isoformat(),SYMBOL,'H1',self.rows[4].time.isoformat()))
        repair_source_evidence(self.service,SYMBOL,self.service._series(SYMBOL),NOW)
        self.assertEqual(self.api.calls[-1][2:],(self.rows[4].time,self.rows[5].time))
        live=Candle(NOW,12.7,12.8,12.6,12.7,1)
        self.service._upsert(SYMBOL,'H1',[live])
        count=len(self.api.calls)
        self.assertFalse(repair_source_evidence(self.service,SYMBOL,self.service._series(SYMBOL),NOW+timedelta(minutes=5)))
        self.assertEqual(len(self.api.calls),count)

    def test_errors_cancellation_and_rate_limit_keep_unverified_source_unconfirmed(self):
        self.unverify(3,35);raw=self.service._series(SYMBOL)
        self.api.error=FinamAPIError('private',http_status=403)
        self.assertFalse(repair_source_evidence(self.service,SYMBOL,raw,NOW))
        self.assertFalse(repair_source_evidence(self.service,SYMBOL,raw,NOW))
        self.assertEqual(len(self.api.calls),1)
        self.clock+=timedelta(minutes=6);self.api.error=FinamAPIError('private',http_status=429)
        with self.assertRaises(FinamAPIError):repair_source_evidence(self.service,SYMBOL,raw,self.clock)
        with self.assertRaises(InterruptedError):
            repair_source_evidence(self.service,SYMBOL,raw,self.clock,cancelled=lambda:True)
        self.assertEqual(self.service._series(SYMBOL),raw)

    def test_selected_view_repairs_before_preparing_and_cache_only_never_uses_http(self):
        self.unverify(3,35)
        with (patch.object(self.service,'refresh_series',return_value={}),
              patch.object(self.service,'analyze_cached',return_value=(SimpleNamespace(asof=NOW),{})) as analyze):
            self.service.view('CRZ6','RTSX','H4',allow_http=False)
            self.assertEqual(self.api.calls,[])
            self.service.view('CRZ6','RTSX','H4',allow_http=True)
            used=analyze.call_args.args[2]
            self.assertEqual(len(used['H1']),len(self.rows))
            self.assertEqual(len(self.api.calls),1)


if __name__=='__main__':unittest.main()
