import unittest
from datetime import datetime, timedelta, timezone, date, time

from flowdesk.engine import analyze
from flowdesk.market_data import Candle
from flowdesk.history_analysis import analyze_history
from flowdesk.session_candles import build_session_series
from flowdesk.trading_calendar import TradingCalendar, CalendarUnavailable
from flowdesk.us_futures_family import build_verified_archive, calendar_for_family

UTC=timezone.utc


def hours(start,count):
    return [Candle(start+timedelta(hours=i),100+i%7,102+i%7,99+i%7,101+i%7,10)
            for i in range(count)]


class RetentionTests(unittest.TestCase):
    def test_missing_hour_retains_both_verified_sides_and_marks_replay_boundary(self):
        start=datetime(2026,10,1,tzinfo=UTC)
        rows=hours(start,24*5)
        del rows[24*2+3]
        built=build_session_series({'H1':rows},TradingCalendar('CRYPTO_UTC','UTC'),
                                   start+timedelta(days=5))
        self.assertEqual(len(built.series['H1']),119)
        self.assertEqual(len(built.series['H4']),29)
        self.assertEqual(len(built.series['D1']),4)
        self.assertEqual(built.series['D1'][0].time,start)
        self.assertEqual(built.series['D1'][-1].time,start+timedelta(days=4))
        self.assertIn((start+timedelta(hours=52)).isoformat(),built.breaks['H1'])
        self.assertIn((start+timedelta(days=3)).isoformat(),built.breaks['D1'])

    def test_unknown_calendar_retains_real_native_hourly_prefix_without_native_h4_fallback(self):
        class Calendar(TradingCalendar):
            def session(self,day):
                if day<date(2026,10,3):raise CalendarUnavailable('historical_hours_unknown')
                return super().session(day)
        start=datetime(2026,10,1,tzinfo=UTC)
        rows=hours(start,24*4)
        native=Candle(start,1,2,.5,1,20)
        built=build_session_series({'H1':rows,'H4':[native]},Calendar('CRYPTO_UTC','UTC'),
                                   start+timedelta(days=4))
        self.assertEqual(len(built.series['H1']),96)
        self.assertEqual(built.series['H1'][0],rows[0])
        self.assertNotIn(native,built.series['H4'])
        self.assertIn((start+timedelta(days=2)).isoformat(),built.breaks['H1'])

    def test_stale_unknown_hourly_draft_is_not_confirmed_by_elapsed_wall_clock(self):
        class Calendar(TradingCalendar):
            def session(self,day):raise CalendarUnavailable('historical_hours_unknown')
        start=datetime(2026,10,1,tzinfo=UTC)
        rows=hours(start,2)
        built=build_session_series({'H1':rows},Calendar('CRYPTO_UTC','UTC'),start+timedelta(days=1),
            observed_at={('H1',rows[0].time):start+timedelta(minutes=5),
                         ('H1',rows[1].time):start+timedelta(hours=2)})
        self.assertEqual(built.series['H1'],[rows[1]])

    def test_previous_evening_session_does_not_duplicate_native_transition_boundaries(self):
        class Calendar(TradingCalendar):
            def session(self,day):
                if day==date(2026,10,2):raise CalendarUnavailable('historical_hours_unknown')
                return self._session(day,time(17),time(16),previous=True)
        start=datetime(2026,10,1,17,tzinfo=UTC)
        rows=hours(start,47)
        built=build_session_series({'H1':rows},Calendar('CRYPTO_UTC','UTC'),
                                   datetime(2026,10,3,16,tzinfo=UTC))
        boundary=datetime(2026,10,2,17,tzinfo=UTC).isoformat()
        self.assertIn(boundary,built.breaks.get('H1',[]))
        self.assertLessEqual(len(built.breaks.get('H1',[])),2)

    def test_unconfirmed_interior_native_hour_splits_retained_prefix(self):
        class Calendar(TradingCalendar):
            def session(self,day):raise CalendarUnavailable('historical_hours_unknown')
        start=datetime(2026,10,1,tzinfo=UTC)
        rows=hours(start,3)
        observed=start+timedelta(days=1)
        built=build_session_series({'H1':rows},Calendar('CRYPTO_UTC','UTC'),observed,
            observed_at={('H1',b.time):b.time+timedelta(minutes=5) if i==1 else observed
                         for i,b in enumerate(rows)})
        self.assertEqual(built.series['H1'],[rows[0],rows[2]])
        self.assertIn(rows[2].time.isoformat(),built.breaks['H1'])

    def test_gap_replay_matches_fresh_engine_without_cross_gap_segments_or_fvg(self):
        start=datetime(2026,10,1,tzinfo=UTC)
        rows=hours(start,50)+hours(start+timedelta(days=5),70)
        asof=start+timedelta(days=10)
        expected=analyze(rows[50:],'H1',asof=asof)
        got=analyze_history(rows,'H1',asof=asof,breaks=[rows[50].time.isoformat()])
        self.assertEqual(got.candles,rows)
        self.assertEqual(len(got.flows),len(rows))
        self.assertEqual([s.side for s in got.flows[50:]],[s.side for s in expected.flows])
        self.assertEqual([s.start for s in got.flows[50:]],
                         [s.start+50 if s.start is not None else None for s in expected.flows])
        self.assertTrue(all(s.b<50 or s.a>=50 for s in got.segments))
        self.assertTrue(all(z.broken is not None and z.broken<=50 for z in got.fvg if z.created<50))
        self.assertEqual(got.market.get('source_right_index'),len(rows)-.5)

    def test_us_archive_extension_keeps_verified_last_family_when_older_hours_unknown(self):
        from flowdesk.trading_calendar import calendar_for
        cal=calendar_for_family(calendar_for('NQZ26@XCME'))
        begin=date(2026,10,1)
        observed=datetime(2026,11,16,19,tzinfo=UTC)
        rows=[]
        cursor=begin
        while cursor<=date(2026,11,16):
            session=cal.daily_session(cursor)
            if session:
                for pieces in session.buckets(60):
                    if pieces[-1][1]<=observed:
                        rows.append(Candle(pieces[0][0],10,11,9,10,1,
                                           scheduled_close=pieces[-1][1]))
            cursor+=timedelta(days=1)
        unknown=hours(datetime(2025,12,24,tzinfo=UTC),5)
        raw={'H1':unknown+rows,'D1':[Candle(datetime(2025,1,1,tzinfo=UTC),999,999,999,999,999)]}
        family,status,detail=build_verified_archive(raw,cal,observed,
            coverage={},observations={('H1',b.time):observed for b in raw['H1']})
        self.assertEqual(status,'ready')
        self.assertTrue(family['D1'])
        self.assertTrue(family['W1'])
        self.assertTrue(family['MN1'])
        self.assertTrue(all(b.close==10 for tf in family for b in family[tf]))
        self.assertTrue(detail['verified_archive_blocks'])

    def test_higher_family_boundaries_do_not_replace_independent_hourly_replay(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch
        from flowdesk.finam_market import FinamMarketService
        from flowdesk.provider_candles import NATIVE_HIGHER
        from flowdesk.history_archive import VERSION
        from test_nq_session_family import source,SYMBOL
        raw,obs,coverage,now=source()
        api=SimpleNamespace(session_candles=True,native_higher_frames=NATIVE_HIGHER,
                            market_delay_minutes=lambda mic:0)
        with tempfile.TemporaryDirectory() as folder:
            service=FinamMarketService(Path(folder)/'data.sqlite',api=api,clock=lambda:now)
            for tf,rows in raw.items():service._upsert(SYMBOL,tf,rows)
            service.save_history_progress(SYMBOL,VERSION+'H1',{'cursor':now,'exhausted':False})
            marker=raw['H1'][4].time.isoformat()
            family={tf:[raw['D1'][0]] for tf in NATIVE_HIGHER}
            with (patch('flowdesk.session_store.futures.build_family',return_value=(None,'incomplete_lower_coverage',{})),
                  patch('flowdesk.session_store.futures.build_verified_archive',return_value=(family,'ready',{'history_breaks':{'H1':[marker],'H4':[marker],'D1':[marker]}}))):
                result=service.prepare_series(SYMBOL,service._series(SYMBOL),now)
            self.assertNotIn(marker,result.session_metadata['history_breaks'].get('H1',[]))
            self.assertNotIn(marker,result.session_metadata['history_breaks'].get('H4',[]))
            self.assertIn(marker,result.session_metadata['history_breaks']['D1'])


if __name__=='__main__':unittest.main()
