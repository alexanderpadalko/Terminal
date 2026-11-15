"""CME NQ pilot: regression against observed prices, calendar and atomic migration."""
from contextlib import closing
from dataclasses import replace
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile,unittest
from unittest.mock import patch

from flowdesk.market_data import Candle, serialize_candle, aggregate
from flowdesk.session_store import SourceSeries, record_response, _candle
from flowdesk.nq_session_family import (build_family, calendar_for_pilot, enabled,
                                      next_window, backfill, POLICY)
from flowdesk.trading_calendar import calendar_for
from flowdesk.finam_market import FinamMarketService
from flowdesk.provider_candles import NATIVE_HIGHER, NATIVE_POLICY_VERSION

UTC=timezone.utc
SYMBOL='NQZ26@XCME'
CAL=calendar_for_pilot(calendar_for(SYMBOL))


def source(day=date(2026,11,13)):
    session=CAL.daily_session(day)
    bars=[]
    for a,b in session.segments:
        while a<b:
            end=min(a+timedelta(hours=1),b)
            bars.append(Candle(a,28399.25 if not bars else 28289.25,
                28607.75 if not bars else 28583.75,27695.25,27927.25,1,scheduled_close=end))
            a=end
    label=datetime.combine(day,datetime.min.time(),UTC)+timedelta(hours=5)
    daily=Candle(label,28289.25,28583.75,27695.25,28034.75,99,
                 scheduled_close=label+timedelta(days=1))
    observed=session.end+timedelta(days=1)
    obs={('H1',b.time):observed for b in bars};obs['D1',label]=observed
    coverage={'H1':[(session.start,session.end)]}
    return {'H1':bars,'D1':[daily]},obs,coverage,observed


class TestNQFamily(unittest.TestCase):
    def test_nov13_uses_session_last_consistently_across_family(self):
        raw,obs,coverage,now=source()
        built,status,_=build_family(raw,CAL,now,coverage=coverage,observations=obs)
        self.assertEqual(status,'ready')
        bar=built['D1'][0]
        self.assertEqual((bar.open,bar.high,bar.low,bar.close),(28399.25,28607.75,27695.25,27927.25))
        self.assertEqual((bar.time,bar.scheduled_close),(raw['H1'][0].time,raw['H1'][-1].scheduled_close))
        self.assertEqual(bar.volume,23)
        self.assertFalse(bar.settlement_close)
        self.assertEqual(built['W1'][0].close,bar.close)
        self.assertEqual(built['MN1'][0].close,bar.close)

    def test_native_daily_close_outside_session_range_is_never_used(self):
        raw,obs,coverage,now=source()
        raw['D1']=[replace(raw['D1'][0],low=25000,close=25001)]
        family,status,_=build_family(raw,CAL,now,coverage=coverage,observations=obs)
        self.assertEqual(status,'ready')
        for tf in NATIVE_HIGHER:
            bar=family[tf][0]
            self.assertEqual((bar.low,bar.close),(27695.25,27927.25))
            self.assertFalse(bar.settlement_close)
            self.assertEqual(_candle(serialize_candle(bar)),bar)
        self.assertEqual(aggregate(family['D1'],'MN1')[0].low,27695.25)
        with self.assertRaises(ValueError):replace(family['D1'][0],close=25001)

    def test_holidays_merge_to_following_business_date_with_verified_short_segment(self):
        for holiday,trade_date in ((date(2026,6,19),date(2026,6,22)),
             (date(2026,7,3),date(2026,7,6)),(date(2026,9,7),date(2026,9,8)),
             (date(2026,11,26),date(2026,11,27))):
            self.assertIsNone(CAL.daily_session(holiday))
            session=CAL.daily_session(trade_date)
            self.assertEqual(len(session.segments),2)
            self.assertEqual(session.segments[0][1].astimezone(CAL.zone).hour,12)
            self.assertEqual(CAL.trading_date(holiday),trade_date)
        thanksgiving=CAL.daily_session(date(2026,11,27))
        self.assertEqual(thanksgiving.end.astimezone(CAL.zone).strftime('%H:%M'),'12:15')
        self.assertEqual(len(list(CAL.session(date(2026,11,27)).buckets(240))),5)

    def test_holiday_family_uses_last_trade_of_final_segment(self):
        raw,obs,coverage,now=source(date(2026,9,8))
        now=CAL.daily_session(date(2026,9,8)).end+timedelta(minutes=1)
        obs={key:now for key in obs}
        built,status,_=build_family(raw,CAL,now,coverage=coverage,observations=obs)
        self.assertEqual(status,'ready')
        self.assertEqual(len(built['D1']),1)
        self.assertEqual(built['D1'][0].time,CAL.session(date(2026,9,7)).start)
        self.assertEqual(built['D1'][0].close,raw['H1'][-1].close)

    def test_sparse_no_trade_hours_need_bounded_response_not_inferred_oldest_bar(self):
        raw,obs,coverage,now=source();del raw['H1'][9]
        self.assertIsNone(build_family(raw,CAL,now,coverage={},observations=obs)[0])
        self.assertEqual(build_family(raw,CAL,now,coverage=coverage,observations=obs)[1],'ready')

    def test_only_hourly_finalization_is_required_native_daily_stamps_are_irrelevant(self):
        raw,obs,coverage,now=source()
        for stamp in (raw['D1'][0].time,now+timedelta(days=1)):
            altered={**obs,('D1',raw['D1'][0].time):stamp}
            family,status,_=build_family(raw,CAL,now,coverage=coverage,observations=altered)
            self.assertEqual(status,'ready');self.assertEqual(family['D1'][0].close,27927.25)
        old={**obs,('H1',raw['H1'][-1].time):raw['H1'][-1].time}
        self.assertIsNone(build_family(raw,CAL,now,coverage=coverage,observations=old)[0])
        future={**obs,('H1',raw['H1'][-1].time):now+timedelta(days=1)}
        self.assertIsNone(build_family(raw,CAL,now,coverage=coverage,observations=future)[0])

    def test_forming_session_keeps_same_last_trade_rule(self):
        raw,obs,coverage,now=source()
        now=raw['H1'][5].time+timedelta(minutes=30)
        raw['H1']=raw['H1'][:6]
        obs={key:now for key in obs}
        family,status,_=build_family(raw,CAL,now,coverage=coverage,observations=obs)
        self.assertEqual(status,'ready')
        self.assertEqual(family['D1'][0].close,27927.25)
        self.assertFalse(family['D1'][0].settlement_close)
        self.assertGreater(family['D1'][0].scheduled_close,now)

    def test_old_native_month_cannot_be_spliced_to_recent_rebuilt_suffix(self):
        raw,obs,coverage,now=source()
        raw['MN1']=[Candle(datetime(2026,6,1,tzinfo=UTC),100,110,90,105,1)]
        self.assertIsNone(build_family(raw,CAL,now,coverage=coverage,observations=obs)[0])

    def test_invalid_source_volume_blocks_candidate(self):
        raw,obs,coverage,now=source();raw['H1'][3]=replace(raw['H1'][3],volume=-1)
        family,status,_=build_family(raw,CAL,now,coverage=coverage,observations=obs)
        self.assertIsNone(family);self.assertEqual(status,'control_source_invalid')

    def test_first_partial_available_session_is_retained_and_identified(self):
        raw,obs,coverage,now=source();raw['H1']=raw['H1'][4:]
        family,status,detail=build_family(raw,CAL,now,coverage=coverage,observations=obs)
        self.assertEqual(status,'ready');self.assertTrue(detail['left_partial'])
        self.assertEqual(family['MN1'][0].time,raw['H1'][0].time)

    def test_pilot_does_not_change_es_other_contracts_or_crypto(self):
        self.assertTrue(enabled(SYMBOL,calendar_for(SYMBOL)))
        for symbol in ('ESZ26@XCME','NQH27@XCME','BTCUSDT@BINANCE'):
            self.assertFalse(enabled(symbol,calendar_for(symbol)))

    def test_dst_sunday_and_month_end_scheduled_close(self):
        self.assertEqual(CAL.session(date(2026,10,30)).start.hour,22)
        self.assertEqual(CAL.session(date(2026,11,2)).start.hour,23)
        raw,obs,coverage,now=source()
        family,_,_=build_family(raw,CAL,now,coverage=coverage,observations=obs)
        self.assertEqual(family['MN1'][0].scheduled_close,datetime(2026,11,30,22,tzinfo=UTC))

    def test_future_unknown_month_end_keeps_known_trades_provisional(self):
        raw,obs,coverage,now=source(date(2026,12,11))
        raw['MN1']=[Candle(datetime(2026,12,1,5,tzinfo=UTC),100,110,90,105,1,
                           scheduled_close=datetime(2027,1,1,5,tzinfo=UTC))]
        family,status,detail=build_family(raw,CAL,now,coverage=coverage,observations=obs)
        self.assertEqual(status,'ready')
        self.assertEqual(detail['provisional_period_closes'],['MN1'])
        self.assertGreater(family['MN1'][0].scheduled_close,now)
        self.assertEqual(family['MN1'][0].close,raw['H1'][-1].close)

    def test_backfill_revalidates_middle_legacy_archive_and_resumes_after_restart(self):
        raw,obs,coverage,now=source()
        rows=SourceSeries(raw,{'coverage':[]})
        first=next_window(rows,now,{},CAL)
        self.assertEqual(first[1],CAL.daily_session(date(2026,11,13)).end)
        # A much older raw bar does not move the first request before the
        # still-unverified interior of the native archive.
        rows['H1'].insert(0,Candle(datetime(2026,1,5,tzinfo=UTC),1,2,0,1,1))
        self.assertEqual(next_window(rows,now,{},CAL),first)
        cursor=first[0]+timedelta(hours=2)
        self.assertEqual(next_window(rows,now,{'cursor':cursor},CAL)[1],cursor)

    def test_completed_backfill_ignores_closed_weekends_and_targets_real_gap(self):
        raw,obs,coverage,now=source()
        session=CAL.daily_session(date(2026,11,13))
        rows=SourceSeries(raw,{'coverage':[('H1',session.start.isoformat(),session.end.isoformat())]})
        self.assertIsNone(next_window(rows,now,{'exhausted':True},CAL))
        rows.source_state['coverage'][0]=('H1',session.start.isoformat(),(session.end-timedelta(hours=1)).isoformat())
        self.assertEqual(next_window(rows,now,{'exhausted':True},CAL)[1],session.end)

    def test_prepared_family_promotion_cache_and_raw_archive_preserved(self):
        raw,obs,coverage,now=source()
        api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,
                            market_delay_minutes=lambda mic:0)
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/'data.sqlite',api=api,clock=lambda:now)
            for tf,bars in raw.items():market._upsert(SYMBOL,tf,bars)
            for a,b in coverage['H1']:record_response(market,SYMBOL,'H1',raw['H1'],a,b,now)
            before=market._series(SYMBOL)
            first=market.prepare_series(SYMBOL,before,now)
            self.assertEqual(first.session_metadata['version'],POLICY)
            second=market.prepare_series(SYMBOL,market._series(SYMBOL),now)
            self.assertEqual(first,second)
            self.assertFalse(second['MN1'][0].settlement_close)
            self.assertEqual(market._series(SYMBOL),before)
            # An incomplete Last-price candidate cannot fall back to native
            # midnight D/W/M, which caused the original error.
            with patch('flowdesk.session_store.nq.build_family',return_value=(None,'incomplete_lower_coverage',{})):
                with closing(market._connection()) as db,db:db.execute('DELETE FROM session_series_cache')
                pending=market.prepare_series(SYMBOL,before,now)
            self.assertTrue(all(not pending.get(tf) for tf in NATIVE_HIGHER))
            self.assertEqual(pending.session_metadata['version'],POLICY)

    def test_promoted_snapshot_survives_failure_without_confirming_old_draft(self):
        raw,obs,coverage,now=source()
        api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,
                            market_delay_minutes=lambda mic:0)
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/'data.sqlite',api=api,clock=lambda:now)
            for tf,bars in raw.items():market._upsert(SYMBOL,tf,bars)
            for a,b in coverage['H1']:record_response(market,SYMBOL,'H1',raw['H1'],a,b,now)
            first=market.prepare_series(SYMBOL,market._series(SYMBOL),now)
            with patch('flowdesk.session_store.nq.build_family',return_value=(None,'incomplete_lower_coverage',{})):
                failed=market.prepare_series(SYMBOL,market._series(SYMBOL),now+timedelta(hours=1))
                self.assertEqual(failed['D1'],first['D1'])
                self.assertEqual(failed.session_metadata['version'],POLICY)
                after=market.prepare_series(SYMBOL,market._series(SYMBOL),datetime(2026,12,1,tzinfo=UTC))
            self.assertFalse(after.get('MN1'))  # November draft never became closed.
            self.assertEqual(after['D1'],first['D1'])

    def test_bootstrap_budget_persisted_cursor_empty_response_and_cancellation(self):
        calls=[]
        def request(symbol,tf,*,start,end):
            calls.append((start,end))
            return [Candle(start+timedelta(hours=1),100,102,99,101,1)]
        now=datetime(2026,11,15,tzinfo=UTC)
        api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,
                            market_delay_minutes=lambda mic:0,bars=request)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'data.sqlite'
            market=FinamMarketService(path,api=api,clock=lambda:now)
            market._upsert(SYMBOL,'D1',[Candle(datetime(2026,6,17,4,tzinfo=UTC),100,102,99,101,1),
                Candle(datetime(2026,11,13,5,tzinfo=UTC),100,102,99,101,1)])
            with self.assertRaises(InterruptedError):backfill(market,SYMBOL,now,cancelled=lambda:True)
            self.assertEqual(calls,[])
            backfill(market,SYMBOL,now,cancelled=lambda:False)
            self.assertEqual(len(calls),6)
            progress=market.load_history_progress()[SYMBOL,'NQ_SESSION_H1_v1']
            self.assertTrue(progress['exhausted'])
            restarted=FinamMarketService(path,api=api,clock=lambda:now)
            self.assertEqual(restarted.load_history_progress()[SYMBOL,'NQ_SESSION_H1_v1'],progress)
            api.bars=lambda *args,**kwargs:[]
            backfill(restarted,SYMBOL,now,cancelled=lambda:False)
            self.assertEqual(restarted.load_history_progress()[SYMBOL,'NQ_SESSION_H1_v1'],progress)


if __name__=='__main__':unittest.main()
