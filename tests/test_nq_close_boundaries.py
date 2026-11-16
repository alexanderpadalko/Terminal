"""Regression for the real October 28 midnight/native-close contamination."""
from contextlib import closing
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
import hashlib,json,tempfile,unittest
from unittest.mock import patch

from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle,serialize_candle
from flowdesk.us_futures_family import build_family,calendar_for_family as calendar_for_pilot,POLICY
from flowdesk.provider_candles import NATIVE_HIGHER
from flowdesk.session_store import record_response
from flowdesk.trading_calendar import calendar_for,CalendarUnavailable

UTC=timezone.utc
SYMBOL='NQZ26@XCME'
CAL=calendar_for_pilot(calendar_for(SYMBOL))
NOW=datetime(2026,10,31,12,tzinfo=UTC)


def archive():
    payload=json.loads((Path(__file__).parent/'fixtures/nq_oct28_session_close.json').read_text())
    result={}
    for tf in ('H1','D1'):
        rows=[]
        for stamp,o,h,l,c,v in payload[tf]:
            start=datetime.fromisoformat(stamp)
            rows.append(Candle(start,o,h,l,c,v,scheduled_close=start+timedelta(hours=1) if tf=='H1' else start+timedelta(days=1)))
        result[tf]=rows
    observations={(tf,b.time):NOW for tf,bars in result.items() for b in bars}
    coverage={'H1':[(result['H1'][0].time,result['H1'][-1].scheduled_close)]}
    return result,observations,coverage


class TestNQCloseBoundaries(unittest.TestCase):
    def test_real_october_28_ohlcv_all_belong_to_same_physical_session(self):
        raw,obs,coverage=archive()
        family,status,meta=build_family(raw,CAL,NOW,coverage=coverage,observations=obs)
        self.assertEqual(status,'ready')
        bar=family['D1'][0]
        self.assertEqual((bar.time,bar.scheduled_close),
            (datetime(2026,10,27,22,tzinfo=UTC),datetime(2026,10,28,21,tzinfo=UTC)))
        self.assertEqual((bar.open,bar.high,bar.low,bar.close),
                         (29814.25,30178,29674.25,29756.25))
        self.assertNotEqual(bar.close,29642)  # Next-session midnight C.
        self.assertNotEqual(bar.close,29979.5)  # TV settlement is a different mode.
        self.assertEqual(meta['close_rule'],'session_last_trade')
        self.assertFalse(bar.settlement_close)
        self.assertEqual(len(family['D1']),3)
        for tf in ('W1','MN1'):
            self.assertEqual(family[tf][-1].close,family['D1'][-1].close)
            self.assertFalse(family[tf][-1].settlement_close)

    def test_changing_next_evening_and_midnight_native_c_cannot_rewrite_prior_day(self):
        raw,obs,coverage=archive()
        before,status,_=build_family(raw,CAL,NOW,coverage=coverage,observations=obs)
        self.assertEqual(status,'ready')
        raw['D1'][0]=replace(raw['D1'][0],close=29650)
        # These H1 prices belong to October 29. They affected native D1's
        # October 28 C; they cannot affect the actual October 28 candle.
        raw['H1']=[replace(b,open=29700,high=29720,low=29680,close=29710)
                   if datetime(2026,10,28,22,tzinfo=UTC)<=b.time<datetime(2026,10,29,5,tzinfo=UTC)
                   else b for b in raw['H1']]
        after,status,_=build_family(raw,CAL,NOW,coverage=coverage,observations=obs)
        self.assertEqual(status,'ready');self.assertEqual(after['D1'][0],before['D1'][0])

    def test_every_daily_and_higher_close_equals_its_own_last_hour_in_real_archive(self):
        raw,obs,coverage=archive()
        family,status,_=build_family(raw,CAL,NOW,coverage=coverage,observations=obs)
        self.assertEqual(status,'ready')
        for tf,bars in family.items():
            for bar in bars:
                hours=[b for b in raw['H1'] if bar.time<=b.time<min(bar.scheduled_close,NOW)]
                self.assertEqual(bar.close,hours[-1].close)
                self.assertLessEqual(bar.low,bar.close);self.assertGreaterEqual(bar.high,bar.close)
                self.assertFalse(bar.settlement_close)

    def test_old_settlement_cache_is_rebuilt_without_raw_data_changes(self):
        raw,obs,coverage=archive()
        api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,
                            market_delay_minutes=lambda mic:0)
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/'cache.sqlite',api=api,clock=lambda:NOW)
            for tf,bars in raw.items():market._upsert(SYMBOL,tf,bars)
            for a,b in coverage['H1']:record_response(market,SYMBOL,'H1',raw['H1'],a,b,NOW)
            original=market._series(SYMBOL)
            fingerprint=lambda series:hashlib.sha256(repr(dict(series)).encode()).hexdigest()
            digest=fingerprint(original)
            bad=replace(raw['D1'][0],time=raw['H1'][0].time,low=29674.25,
                        close=29642,settlement_close=True)
            payload=json.dumps({'derived':{tf:[serialize_candle(bad)] for tf in NATIVE_HIGHER},
                'metadata':{'version':'nq-session-settlement-1','nq_snapshot_at':NOW.isoformat()}})
            with closing(market._connection()) as db,db:
                revision=original.source_state['revision']
                db.execute('INSERT OR REPLACE INTO session_series_cache VALUES(?,?,?,?,?)',
                    (SYMBOL,'old-settlement-policy',revision,NOW.isoformat(),payload))
            prepared=market.prepare_series(SYMBOL,original,NOW)
            self.assertEqual(prepared.session_metadata['version'],POLICY)
            self.assertEqual(prepared['D1'][0].close,29756.25)
            self.assertEqual(fingerprint(market._series(SYMBOL)),digest)
            # A restart reads the corrected cache, never the old bad family.
            restarted=FinamMarketService(market.path,api=api,clock=lambda:NOW)
            self.assertEqual(restarted.prepare_series(SYMBOL,restarted._series(SYMBOL),NOW)['D1'],prepared['D1'])

    def test_missing_or_wrong_calendar_never_reenables_native_nq_daily_close(self):
        raw,obs,coverage=archive()
        api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,
                            market_delay_minutes=lambda mic:0)
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/'cache.sqlite',api=api,clock=lambda:NOW)
            for tf,bars in raw.items():market._upsert(SYMBOL,tf,bars)
            market.calendar_factory=lambda symbol:(_ for _ in ()).throw(CalendarUnavailable('instrument_calendar_unknown'))
            result=market.prepare_series(SYMBOL,market._series(SYMBOL),NOW)
            self.assertTrue(all(not result.get(tf) for tf in NATIVE_HIGHER))
            self.assertEqual(result.session_metadata['version'],POLICY)


if __name__=='__main__':unittest.main()
