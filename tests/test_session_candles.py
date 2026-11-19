"""Session OHLCV, source integrity, no-lookahead and restart/migration contracts."""
from contextlib import closing
from dataclasses import replace
from datetime import date,datetime,time,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from flowdesk.diagnostics_log import log_candle_policy
from flowdesk.finam_api import FinamAPI,parse_instant,finam_bar
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle,candle_close_time,normalize
from flowdesk.session_candles import ClosedBucketCache, _LowerSource, build_session_series
from flowdesk.session_store import record_coverage
from tools.compare_session_csv import compare,strict_csv
from flowdesk.trading_calendar import CalendarUnavailable,TradingCalendar,TradingSession,calendar_for

UTC=timezone.utc


def lower(session,minutes=5):
    result=[]
    for left,right in session.segments:
        cursor=left
        while cursor<right:
            end=min(right,cursor+timedelta(minutes=minutes));i=len(result)
            result.append(Candle(cursor,100+i,102+i,99+i,101+i,10,scheduled_close=end))
            cursor=end
    return result


def prices(bars):
    return (bars[0].open,max(b.high for b in bars),min(b.low for b in bars),bars[-1].close,sum(b.volume for b in bars))


class TestTradingSessions(unittest.TestCase):
    def test_moex_weekend_daily_belongs_to_monday_but_h4_resets_each_session(self):
        for symbol in ('SBER@MISX','SIZ6@RTSX'):
            cal=calendar_for(symbol);monday=date(2026,11,16)
            physical=[cal.session(date(2026,11,d)) for d in (14,15,16)]
            rows=[bar for session in physical for bar in lower(session)]
            daily=cal.daily_session(monday)
            self.assertEqual(cal.trading_date(date(2026,11,14)),monday)
            self.assertEqual(daily.start,physical[0].start)
            self.assertIsNone(cal.daily_session(date(2026,11,14)))
            built=build_session_series({'M5':rows},cal,physical[-1].end)
            self.assertEqual(len(built.series['D1']),1)
            self.assertEqual(prices([built.series['D1'][0]]),prices(rows))
            self.assertEqual(len(built.series['H4']),11)
            for bar in built.series['H4']:
                self.assertEqual(bar.time.astimezone(cal.zone).date(),
                    (bar.scheduled_close-timedelta(seconds=1)).astimezone(cal.zone).date())

    def test_moex_month_and_week_start_on_prior_month_weekend_trades(self):
        cal=calendar_for('SIZ6@RTSX')
        sessions=[cal.session(date(2026,10,31)),cal.session(date(2026,11,1)),cal.session(date(2026,11,2))]
        rows=[b for s in sessions for b in lower(s)]
        # Future Sunday/Monday prices cannot enter the Saturday snapshot.
        saturday=build_session_series({'M5':rows},cal,sessions[0].end)
        day=saturday.series['D1'][0]
        self.assertEqual(day.time,sessions[0].start)
        self.assertEqual(day.scheduled_close,sessions[-1].end)
        self.assertEqual(prices([day]),prices(lower(sessions[0])))
        self.assertEqual(saturday.series['W1'][0].time,sessions[0].start)
        self.assertEqual(saturday.series['MN1'][0].time,sessions[0].start)
        full=build_session_series({'M5':rows},cal,sessions[-1].end)
        self.assertEqual(full.series['MN1'][0].scheduled_close,cal.daily_session(date(2026,11,30)).end)
        self.assertEqual(full.series['D1'][0].volume,sum(b.volume for b in rows))

    def test_moex_daily_can_clip_each_verified_extra_session_close(self):
        cal=calendar_for('MXZ6@RTSX')
        physical=[cal.session(date(2026,5,d)) for d in (1,2,3,4)]
        rows=[replace(b,scheduled_close=b.time+timedelta(hours=1))
              for session in physical for b in lower(session,60)]
        built=build_session_series({'H1':rows},cal,physical[-1].end)
        self.assertEqual(len(built.series['D1']),1)
        self.assertEqual(built.series['D1'][0].time,physical[0].start)
        self.assertEqual(built.series['D1'][0].volume,sum(b.volume for b in rows))

    def test_us_rth_four_hours_and_short_final_bucket(self):
        cal=calendar_for('AAPL@XNGS');day=date(2026,11,12);s=cal.session(day)
        slots=list(s.buckets(240));local=lambda d:d.astimezone(cal.zone).strftime('%H:%M')
        self.assertEqual([(local(p[0][0]),local(p[-1][1])) for p in slots],[('09:30','13:30'),('13:30','16:00')])
        built=build_session_series({'M5':lower(s)},cal,s.end)
        self.assertEqual([b.time for b in built.series['H4']],[p[0][0] for p in slots])
        self.assertEqual(built.series['D1'][0].scheduled_close,s.end)

    def test_us_holidays_early_close_and_dst(self):
        cal=calendar_for('AAPL@XNGS')
        self.assertIsNone(cal.session(date(2026,11,26)))
        self.assertEqual(cal.session(date(2026,11,27)).end.astimezone(cal.zone).hour,13)
        self.assertEqual(len(list(cal.session(date(2026,11,27)).buckets(240))),1)
        self.assertEqual(cal.session(date(2026,3,6)).start.hour,14)
        self.assertEqual(cal.session(date(2026,3,9)).start.hour,13)

    def test_cme_sunday_belongs_to_monday_and_dst_is_local(self):
        cal=calendar_for('NQZ26@XCME')
        summer=cal.session(date(2026,3,9));winter=cal.session(date(2026,11,9))
        self.assertEqual((summer.start.weekday(),summer.start.hour),(6,22))
        self.assertEqual((winter.start.weekday(),winter.start.hour),(6,23))
        self.assertEqual(summer.end-summer.start,timedelta(hours=23))
        self.assertEqual(list(summer.buckets(240))[-1][-1][1],summer.end)
        with self.assertRaises(CalendarUnavailable):cal.session(date(2026,11,26))

    def test_ice_product_and_october_temporary_dst_schedule(self):
        cal=calendar_for('BRNF27@IFEU')
        regular=cal.session(date(2026,11,9));temporary=cal.session(date(2026,10,26))
        self.assertEqual((regular.start.weekday(),regular.start.hour,regular.end.hour),(6,23,23))
        self.assertEqual((temporary.start.weekday(),temporary.start.hour,temporary.end.hour),(6,22,22))
        self.assertEqual(cal.session(date(2026,11,27)).end.hour,20)
        with self.assertRaises(CalendarUnavailable):calendar_for('BRNF27@XNYM')

    def test_moex_history_change_and_amended_weekends(self):
        cal=calendar_for('SiZ6@RTSX')
        local=lambda s:s.start.astimezone(cal.zone).hour
        self.assertEqual(local(cal.session(date(2026,7,13))),9)
        self.assertEqual(local(cal.session(date(2026,7,14))),7)
        self.assertEqual(local(cal.session(date(2026,11,15))),10)
        self.assertIsNone(cal.session(date(2026,11,28)))
        self.assertIsNotNone(cal.session(date(2026,12,5)))
        with self.assertRaises(CalendarUnavailable):cal.session(date(2026,3,20))
        with self.assertRaises(CalendarUnavailable):cal.session(date(2025,11,14))

    def test_breaks_consume_four_trading_hours_without_crossing_days(self):
        d=date(2026,11,12);dt=lambda h:datetime.combine(d,time(h),UTC)
        s=TradingSession(d,((dt(9),dt(11)),(dt(12),dt(16))))
        self.assertEqual(list(s.buckets(240)),[((dt(9),dt(11)),(dt(12),dt(14))),((dt(14),dt(16)),)])
        cal=TradingCalendar('TEST','UTC',overrides=((d,s.segments),))
        built=build_session_series({'M5':lower(s)},cal,s.end)
        self.assertEqual([(b.time.hour,b.scheduled_close.hour) for b in built.series['H4']],[(9,14),(14,16)])

    def test_unknown_profiles_and_invalid_session_intervals_fail_explicitly(self):
        with self.assertRaises(CalendarUnavailable):calendar_for('UNKNOWN@XZZZ')
        with self.assertRaises(ValueError):TradingSession(date(2026,1,1),((datetime(2026,1,1),datetime(2026,1,2)),))
        s=calendar_for('BTCUSDT@BINANCE').session(date(2026,11,12))
        with self.assertRaises(ValueError):list(s.buckets(0))


class TestSessionAggregation(unittest.TestCase):
    def setUp(self):
        self.cal=calendar_for('SBER@MISX');self.s=self.cal.session(date(2026,11,12));self.rows=lower(self.s)

    def test_closed_buckets_reuse_unchanged_history_and_replace_a_correction(self):
        cache = ClosedBucketCache()
        cache.rebase(self.cal.revision, self.s.end)
        current = build_session_series({'M5': self.rows}, self.cal, self.s.end, bucket_cache=cache)
        later = self.s.end + timedelta(minutes=1)
        cache.rebase(self.cal.revision, later, ())
        with patch.object(_LowerSource, '_bucket', side_effect=AssertionError('history replayed')):
            self.assertEqual(build_session_series({'M5': self.rows}, self.cal, later, bucket_cache=cache), current)

        corrected = replace(self.rows[0], high=999)
        rows = {'M5': [corrected, *self.rows[1:]]}
        cache.rebase(self.cal.revision, later, [('M5', corrected.time, corrected.time)])
        updated = build_session_series(rows, self.cal, later, bucket_cache=cache)
        self.assertEqual(updated, build_session_series(rows, self.cal, later))
        self.assertEqual(updated.series['H4'][0].high, 999)
        self.assertIs(updated.series['H4'][-1], current.series['H4'][-1])

    def test_forming_bucket_cannot_be_reused_after_close(self):
        start = self.s.start
        end = start + timedelta(hours=2)
        observed = end - timedelta(minutes=30)
        rows = {'H1': lower(self.s, 60)[:2]}
        observations = {('H1', rows['H1'][1].time): observed}
        pieces = ((start, end),)
        cache = ClosedBucketCache()
        cache.rebase(self.cal.revision, observed)
        forming = _LowerSource(rows, {}, observations, observed, self.cal, bucket_cache=cache)
        self.assertIsNotNone(forming.bucket(pieces, self.s.end)[0])
        cache.rebase(self.cal.revision, end, ())
        closed = _LowerSource(rows, {}, observations, end, self.cal, bucket_cache=cache)
        self.assertEqual(closed.bucket(pieces, self.s.end), (None, None))

    def test_coverage_change_can_recompute_cached_missing_and_empty_buckets(self):
        start = self.s.start
        end = start + timedelta(minutes=5)
        pieces = ((start, end),)
        rows = {'M5': self.rows[1:2]}
        cache = ClosedBucketCache()
        cache.rebase(self.cal.revision, end)
        raw = _LowerSource(rows, {}, {}, end, self.cal, bucket_cache=cache)
        self.assertEqual(raw.bucket(pieces, self.s.end), (None, None))
        with patch.object(_LowerSource, '_bucket', side_effect=AssertionError('missing result replayed')):
            self.assertEqual(raw.bucket(pieces, self.s.end), (None, None))

        cache.rebase(self.cal.revision, end, [(None, start, end)])
        covered = _LowerSource(rows, {'M5': [(start, end)]}, {}, end, self.cal, bucket_cache=cache)
        self.assertEqual(covered.bucket(pieces, self.s.end), (None, 'no_trades'))
        with patch.object(_LowerSource, '_bucket', side_effect=AssertionError('empty result replayed')):
            self.assertEqual(covered.bucket(pieces, self.s.end), (None, 'no_trades'))

    def test_rebase_includes_the_source_bar_before_a_session_open(self):
        start = self.s.start
        end = start + timedelta(hours=1)
        pieces = ((start, end),)
        rows = {'H1': lower(self.s, 60)[:1]}
        cache = ClosedBucketCache()
        cache.rebase(self.cal.revision, end)
        raw = _LowerSource(rows, {}, {}, end, self.cal, bucket_cache=cache)
        self.assertIsNotNone(raw.bucket(pieces, self.s.end)[0])

        overlap = replace(rows['H1'][0], time=start - timedelta(minutes=30),
                          scheduled_close=start + timedelta(minutes=30))
        cache.rebase(self.cal.revision, end, [('H1', overlap.time, overlap.time)])
        corrected = _LowerSource({'H1': [overlap, *rows['H1']]}, {}, {}, end, self.cal, bucket_cache=cache)
        self.assertEqual(corrected.bucket(pieces, self.s.end), (None, None))

    def test_short_session_tail_and_native_daily_priority_when_verified(self):
        native=Candle(datetime(2026,11,12,5,tzinfo=UTC),*prices(self.rows),scheduled_close=datetime(2026,11,13,5,tzinfo=UTC))
        result=build_session_series({'M5':self.rows,'D1':[native]},self.cal,self.s.end)
        self.assertEqual(result.series['D1'][0].time,self.s.start)
        self.assertEqual(prices(result.series['D1']),prices(self.rows))
        self.assertEqual(result.series['H4'][-1].scheduled_close,self.s.end)
        self.assertEqual(result.series['H4'][-1].scheduled_close-result.series['H4'][-1].time,timedelta(minutes=50))
        self.assertIn('verified',result.origins['D1'])

    def test_wrong_native_daily_is_not_relabelled_as_correct_session(self):
        native=Candle(datetime(2026,11,12,5,tzinfo=UTC),1,2,0,1,99,scheduled_close=self.s.end)
        result=build_session_series({'M5':self.rows,'D1':[native]},self.cal,self.s.end)
        self.assertEqual(prices(result.series['D1']),prices(self.rows))
        self.assertEqual(result.issues['D1'],'native_daily_session_mismatch')

    def test_hole_cannot_be_hidden_by_old_native_higher_candle(self):
        missing=self.rows[:10]+self.rows[11:]
        native=Candle(self.s.start,1,2,0,1,99,scheduled_close=self.s.end)
        result=build_session_series({'M5':missing,'H4':[native],'D1':[native]},self.cal,self.s.end)
        self.assertNotIn('D1',result.series)
        self.assertEqual(result.issues['D1'],'incomplete_lower_coverage')
        # A verified successful HTTP coverage interval can certify sparse
        # no-trade slots. It still never creates a filled-in source candle.
        sparse=build_session_series({'M5':missing},self.cal,self.s.end,
            coverage={'M5':[(self.s.start,self.s.end)]})
        self.assertEqual(sparse.series['D1'][0].volume,sum(b.volume for b in missing))

    def test_unfinished_source_cannot_leak_its_future_final_high(self):
        asof=self.s.start+timedelta(hours=2,minutes=2)
        rows=self.rows[:25]
        result=build_session_series({'M5':rows},self.cal,asof)
        self.assertNotIn('H4',result.series)
        known={('M5',rows[-1].time):asof}
        current=build_session_series({'M5':rows},self.cal,asof,observed_at=known)
        self.assertEqual(current.series['H4'][0].scheduled_close,self.s.start+timedelta(hours=4))
        self.assertGreater(candle_close_time(current.series['H4'][0],'H4'),asof)
        self.assertEqual(current.series['H4'][0].high,max(b.high for b in rows))

    def test_duplicates_last_value_wins_and_shuffled_load_is_deterministic(self):
        corrected=replace(self.rows[0],high=999)
        data=list(reversed(self.rows))+[corrected]
        result=build_session_series({'M5':data},self.cal,self.s.end)
        self.assertEqual(result.series['D1'][0].high,999)
        self.assertEqual(result.series['D1'][0].volume,len(self.rows)*10)

    def test_h1_crossing_rth_open_cannot_be_cut_into_a_new_h4(self):
        cal=calendar_for('AAPL@XNGS');s=cal.session(date(2026,11,12))
        hourly=lower(TradingSession(s.trading_date,((s.start-timedelta(minutes=30),s.end),)),60)
        result=build_session_series({'H1':hourly},cal,s.end)
        self.assertNotIn('D1',result.series)
        self.assertNotIn('H4',result.series)

    def test_week_month_are_built_from_every_session_daily(self):
        cal=calendar_for('BTCUSDT@BINANCE')
        rows=[]
        for n in range(1,29):
            s=cal.session(date(2026,2,n));rows.append(Candle(s.start,n,n+2,n-1,n+1,10,scheduled_close=s.end))
        end=rows[-1].scheduled_close
        result=build_session_series({'D1':rows},cal,end)
        self.assertEqual(prices(result.series['MN1']),(1,30,0,29,280))
        self.assertEqual(result.series['MN1'][0].time,rows[0].time)
        broken=build_session_series({'D1':rows[:12]+rows[13:]},cal,end)
        self.assertNotIn('MN1',broken.series)

    def test_cme_week_begins_with_sunday_evening_from_the_monday_trade_date(self):
        cal=calendar_for('NQZ26@XCME');raw=[]
        for n in range(9,14):raw.extend(lower(cal.session(date(2026,11,n)),60))
        end=cal.session(date(2026,11,13)).end
        result=build_session_series({'H1':raw},cal,end)
        self.assertEqual(result.series['W1'][0].time,cal.session(date(2026,11,9)).start)
        self.assertEqual(result.series['W1'][0].scheduled_close,end)

    def test_future_interior_holiday_does_not_remove_a_forming_month(self):
        cal=calendar_for('NQZ26@XCME');raw=[]
        for n in range(2,14):
            s=cal.session(date(2026,11,n))
            if s:raw.extend(lower(s,60))
        observed=datetime(2026,11,15,12,tzinfo=UTC)
        result=build_session_series({'H1':raw},cal,observed)
        self.assertEqual(result.series['MN1'][0].time,cal.session(date(2026,11,2)).start)
        self.assertEqual(result.series['MN1'][0].scheduled_close,cal.session(date(2026,11,30)).end)
        self.assertGreater(result.series['MN1'][0].scheduled_close,observed)

    def test_offline_comparison_does_not_claim_unknown_reference_closing_times(self):
        bar=lower(self.s)[0]
        result=compare([bar],[replace(bar,scheduled_close=None)],volume=True)
        self.assertTrue(result['ohlc_timestamps_match'])
        self.assertFalse(result['full_parity_confirmed'])
        self.assertTrue(compare([bar],[bar],volume=True)['full_parity_confirmed'])
        self.assertFalse(compare([bar],[replace(bar,close=bar.open)],volume=True)['ohlc_timestamps_match'])

    def test_csv_reference_requires_timezone_and_accepts_explicit_close(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bars.csv'
            path.write_text('time,open,high,low,close,volume,time_close\n2026-11-12T07:00:00Z,1,2,0,1,10,2026-11-12T11:00:00Z\n',encoding='utf-8')
            self.assertEqual(strict_csv(path)[0].scheduled_close,datetime(2026,11,12,11,tzinfo=UTC))
            path.write_text('time,open,high,low,close\n2026-11-12T07:00:00,1,2,0,1\n',encoding='utf-8')
            with self.assertRaises(ValueError):strict_csv(path)

    def test_timestamp_and_ohlcv_errors_are_rejected_at_ingestion(self):
        with self.assertRaises(ValueError):parse_instant(True)
        with self.assertRaises(ValueError):parse_instant('2026-11-12T00:00:00')
        row={'timestamp':'2026-11-12T00:00:00Z','open':1,'high':2,'low':0,'close':1,'volume':-1}
        with self.assertRaises(ValueError):finam_bar(row,'H4')
        with self.assertRaises(ValueError):finam_bar(row,'unknown')

    def test_provider_adjacent_dates_are_filtered_without_using_their_prices(self):
        api=FinamAPI(secret_reader=lambda:'')
        api.access_key=lambda **kw:'test-only'
        row={'timestamp':'2026-11-12T05:00:00Z','open':1,'high':2,'low':0,'close':1,'volume':1}
        with patch.object(api,'_json',return_value={'bars':[row,{**row,'timestamp':'2026-11-14T05:00:00Z'}]}):
            result=api.bars('SBER@MISX','D1',start=datetime(2026,11,11,tzinfo=UTC),end=datetime(2026,11,14,tzinfo=UTC))
        self.assertEqual(len(result),1)

    def test_incomplete_or_mismatched_http_payload_cannot_certify_coverage(self):
        api=FinamAPI(secret_reader=lambda:'');api.access_key=lambda **kw:'test-only'
        for payload in ({},{'bars':[],'next_cursor':'more'},{'bars':[{'symbol':'OTHER@MISX'}]}):
            with self.subTest(payload=payload),patch.object(api,'_json',return_value=payload):
                with self.assertRaises(ValueError):api.bars('SBER@MISX','H1')

    def test_dst_fold_timestamps_sort_by_instant_not_repeated_wall_time(self):
        zone=ZoneInfo('America/New_York')
        later=Candle(datetime(2026,11,1,1,10,tzinfo=zone,fold=1),1,2,0,1)
        earlier=Candle(datetime(2026,11,1,1,50,tzinfo=zone,fold=0),1,2,0,1)
        self.assertEqual(normalize([later,earlier]),[earlier,later])

    def test_a_draft_never_becomes_final_just_by_advancing_the_clock(self):
        end=self.s.start+timedelta(hours=4)
        rows=self.rows[:48]
        observed_at={('M5',bar.time):bar.scheduled_close for bar in rows}
        observed_at[('M5',rows[-1].time)]=end-timedelta(seconds=1)
        result=build_session_series({'M5':rows},self.cal,end,observed_at=observed_at)
        self.assertNotIn('H4',result.series)
        observed_at[('M5',rows[-1].time)]=end
        confirmed=build_session_series({'M5':rows},self.cal,end,observed_at=observed_at)
        self.assertEqual(len(confirmed.series['H4']),1)


class SessionFeed:
    session_candles=True
    provider_name='Finam'
    def market_delay_minutes(self,mic):return 0


class TestSessionStore(unittest.TestCase):
    def test_restart_version_invalidation_and_raw_history_are_independent(self):
        cal=calendar_for('SBER@MISX');s=cal.session(date(2026,11,12));rows=lower(s)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'candles.sqlite'
            def create():return FinamMarketService(path,api=SessionFeed(),clock=lambda:s.end)
            market=create();market._upsert('SBER@MISX','M5',rows)
            first=market.prepare_series('SBER@MISX',market._series('SBER@MISX'),s.end)
            restarted=create()
            with patch('flowdesk.session_store.build_session_series',side_effect=AssertionError('cache not reused')):
                cached=restarted.prepare_series('SBER@MISX',restarted._series('SBER@MISX'),s.end)
            self.assertEqual(first,cached)
            with closing(sqlite3.connect(path)) as db,db:
                db.execute("UPDATE session_series_cache SET policy='old-algorithm',payload='invalid'")
            rebuilt=restarted.prepare_series('SBER@MISX',restarted._series('SBER@MISX'),s.end)
            self.assertEqual(first,rebuilt)
            self.assertEqual(restarted._series('SBER@MISX')['M5'],rows)

    def test_corrupt_current_derived_cache_is_rebuilt(self):
        s=calendar_for('SBER@MISX').session(date(2026,11,12))
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/'data.sqlite',api=SessionFeed(),clock=lambda:s.end)
            market._upsert('SBER@MISX','M5',lower(s))
            first=market.prepare_series('SBER@MISX',market._series('SBER@MISX'),s.end)
            with closing(market._connection()) as db,db:
                db.execute("UPDATE session_series_cache SET payload='invalid'")
            self.assertEqual(market.prepare_series('SBER@MISX',market._series('SBER@MISX'),s.end),first)

    def test_closed_stream_cannot_overwrite_confirmed_history_but_http_can_correct(self):
        s=calendar_for('SBER@MISX').session(date(2026,11,12));bar=lower(s)[0]
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/'data.sqlite',api=SessionFeed(),clock=lambda:s.end)
            market._upsert('SBER@MISX','M5',[bar])
            correction=replace(bar,high=999)
            self.assertFalse(market._upsert('SBER@MISX','M5',[correction],source='stream'))
            self.assertEqual(market._series('SBER@MISX')['M5'],[bar])
            self.assertIn(('SBER@MISX','M5'),market._repair)
            market._upsert('SBER@MISX','M5',[correction],source='http')
            self.assertEqual(market._series('SBER@MISX')['M5'],[correction])

    def test_identical_http_snapshot_confirms_a_previously_saved_draft(self):
        s=calendar_for('SBER@MISX').session(date(2026,11,12));rows=lower(s)[:48]
        current=[rows[-1].scheduled_close-timedelta(seconds=1)]
        with tempfile.TemporaryDirectory() as folder:
            market=FinamMarketService(Path(folder)/'data.sqlite',api=SessionFeed(),clock=lambda:current[0])
            market._upsert('SBER@MISX','M5',rows,source='stream')
            current[0]=rows[-1].scheduled_close
            before=market.prepare_series('SBER@MISX',market._series('SBER@MISX'),current[0])
            self.assertNotIn('H4',before)
            market._upsert('SBER@MISX','M5',rows,source='http')
            after=market.prepare_series('SBER@MISX',market._series('SBER@MISX'),current[0])
            self.assertEqual(len(after['H4']),1)

    def test_full_and_incremental_inputs_produce_identical_session_candles(self):
        s=calendar_for('SBER@MISX').session(date(2026,11,12));rows=lower(s)
        with tempfile.TemporaryDirectory() as folder:
            a=FinamMarketService(Path(folder)/'a.sqlite',api=SessionFeed(),clock=lambda:s.end)
            b=FinamMarketService(Path(folder)/'b.sqlite',api=SessionFeed(),clock=lambda:s.end)
            a._upsert('SBER@MISX','M5',rows)
            for chunk in (rows[:50],rows[45:100],rows[95:]):b._upsert('SBER@MISX','M5',chunk)
            self.assertEqual(a.prepare_series('SBER@MISX',a._series('SBER@MISX'),s.end),
                             b.prepare_series('SBER@MISX',b._series('SBER@MISX'),s.end))

    def test_calendar_diagnostic_never_records_untrusted_text(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'diag.log'
            log_candle_policy(symbol='SBER@MISX',profile='SECRET',
                issues={'H4':'SECRET'},counts={'H4':3},path=path)
            text=path.read_text(encoding='utf-8')
            self.assertNotIn('SECRET',text);self.assertIn('H4_count=3',text)
