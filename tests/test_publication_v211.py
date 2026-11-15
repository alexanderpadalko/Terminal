"""Integration contracts taken from the v2.11 right-edge/own-column rules."""
from dataclasses import replace
from datetime import datetime,timedelta
import unittest

from flowdesk.chart_indicators import summary_rows
from flowdesk.engine import Analysis,FlowState,FVG
from flowdesk.fvg_display import select_fvg_display
from flowdesk.market_data import Candle,MOSCOW,candle_close_time
from flowdesk.pipeline import analyze_timeframes
from flowdesk.settings import IndicatorSettings

BASE=datetime(2026,1,1,8,tzinfo=MOSCOW)


def frame(tf,times,states,asof,price=105):
    candles=[Candle(t,price,max(115,price),min(90,price),price,100) for t in times]
    return Analysis(candles,[],[],states,[],tf,asof=asof)


class TestPublicationV211(unittest.TestCase):
    def test_switch_from_short_minute_archive_restores_hourly_observation_horizon(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from flowdesk.market_cache import MarketService,MarketSnapshot
        from flowdesk.market_data import aggregate
        hours=[Candle(BASE+timedelta(hours=i),100,110,90,105,100) for i in range(5)]
        minutes=[Candle(BASE+timedelta(hours=1,minutes=i),100,110,90,105,100) for i in range(2)]
        observed=BASE+timedelta(hours=4,minutes=30)
        with TemporaryDirectory() as directory,patch('flowdesk.market_cache.load_session_overrides',return_value={}):
            service=MarketService(Path(directory)/'cache.sqlite3')
            key=service.key('SBER','TQBR',{})
            service.memory[key]=MarketSnapshot('SBER','TQBR',observed,hours,minutes,{'feed_delay_seconds':900},
                                    {tf:aggregate(hours,tf) for tf in ('D1','W1','MN1')})
            lower,_,_=service.view('SBER','TQBR','M1')
            self.assertLess(lower.asof,observed)
            main,_,_=service.view('SBER','TQBR','H1')
            self.assertEqual(main.asof,observed)
            self.assertEqual(main.confirmed_bars,4)

    def test_own_summary_column_uses_full_local_context_not_liquidity_window(self):
        times=[BASE+timedelta(days=i) for i in range(2)]
        guard=FVG(0,0,0,110,100,1)
        own=frame('D1',times,[FlowState(i,1,90,120,0,guard=guard) for i in range(2)],BASE+timedelta(days=3))
        limited=frame('D1',times,[FlowState(i,-1,90,120,0,guard=guard) for i in range(2)],own.asof)
        own.summary_sources={'D1':limited}
        row=next(row for row in summary_rows(own) if row['timeframe']=='D1')
        self.assertEqual((row['flow'],row['fvg']),(1,1))

    def test_short_chart_summary_cannot_read_a_later_parent_state(self):
        times=[BASE+timedelta(hours=i) for i in range(8)]
        parent=frame('H1',times,[FlowState(i,1 if i<2 else -1,90,120,0) for i in range(8)],BASE+timedelta(hours=12))
        chart=frame('M1',[BASE+timedelta(hours=2,minutes=i) for i in range(2)],
                    [FlowState(i) for i in range(2)],parent.asof)
        chart.summary_sources={'H1':parent}
        self.assertEqual(summary_rows(chart)[0]['flow'],1)

    def test_short_chart_fvg_uses_parent_at_its_own_right_edge(self):
        times=[BASE+timedelta(hours=i) for i in range(8)]
        old=FVG(0,0,0,110,100,1)
        future=FVG(3,4,5,210,200,1)
        parent=frame('H1',times,[FlowState(i,1,50,250,0,guard=old if i<2 else future) for i in range(8)],BASE+timedelta(hours=12))
        chart=frame('M1',[BASE+timedelta(hours=2,minutes=i) for i in range(2)],
                    [FlowState(i) for i in range(2)],parent.asof)
        plan=select_fvg_display(chart,{'H1':parent})
        self.assertEqual([zone.top for zone in plan.parents],[110])
        self.assertEqual(plan.cutoff,candle_close_time(chart.candles[-1],'M1'))


    def test_h4_chart_provides_its_own_hourly_summary_source(self):
        # On H4/D/W/M the original pipeline skipped H1 entirely: summary
        # showed a dash even though independent hourly candles existed.
        hours=[Candle(BASE+timedelta(hours=i),100,110+(i%4),90,105,100)
               for i in range(36)]
        higher=[Candle(BASE+timedelta(hours=4*i),100,110,90,105,100)
                for i in range(9)]
        asof=BASE+timedelta(hours=17,minutes=30)
        a,_=analyze_timeframes({'H1':hours,'H4':higher},'H4',asof=asof)
        self.assertIn('H1',a.summary_sources)
        hourly=a.summary_sources['H1']
        self.assertEqual(hourly.timeframe,'H1')
        self.assertEqual(hourly.asof,asof)
        self.assertEqual(hourly.candles[-1].time,BASE+timedelta(hours=17))
        self.assertTrue(summary_rows(a)[0]['available'])
        self.assertEqual(summary_rows(a)[0]['flow'],hourly.flows[16].side)

    def test_higher_chart_does_not_invent_h1_when_hourly_candles_are_absent(self):
        higher=[Candle(BASE+timedelta(hours=i*4),100,110,90,105,100)
                for i in range(7)]
        a,_=analyze_timeframes({'H4':higher},'H4',asof=BASE+timedelta(hours=24))
        self.assertNotIn('H1',a.summary_sources)
        self.assertEqual((summary_rows(a)[0]['flow'],summary_rows(a)[0]['available']),(0,False))

    def test_h1_summary_live_range_retains_the_confirmed_guard(self):
        guard=FVG(0,0,0,105,102,1)
        confirmed=FlowState(0,1,90,100,0,guard=guard)
        developing=replace(confirmed,i=1,hi=110,live_range=True,guard=FVG(1,1,1,125,120,1))
        parent=frame('H1',[BASE,BASE+timedelta(hours=1)],[confirmed,developing],BASE+timedelta(hours=1,minutes=30))
        chart=frame('M1',[BASE+timedelta(hours=1,minutes=29)],[FlowState(0)],parent.asof,price=104)
        chart.summary_sources={'H1':parent}
        self.assertEqual((summary_rows(chart)[0]['flow'],summary_rows(chart)[0]['fvg']),(1,1))

    def test_h4_summary_matches_drawn_working_guard_in_extended_live_range(self):
        # A developing bar can extend the confirmed episode's bounds, but
        # never replace its guard/direction with a forming guard.
        guard=FVG(0,0,0,100,95,-1)
        confirmed=FlowState(0,-1,105,120,0,guard=guard)
        developing=replace(confirmed,i=1,lo=90,live_range=True,
                           guard=FVG(1,1,1,210,200,1))
        source=frame('H4',[BASE,BASE+timedelta(hours=4)],
                     [confirmed,developing],BASE+timedelta(hours=6))
        chart=frame('H1',[BASE+timedelta(hours=5)],[FlowState(0)],source.asof,price=98)
        chart.summary_sources={'H4':source}
        zones=[zone for zone in select_fvg_display(chart,{'H4':source}).zones
               if zone.source_tf=='H4']
        self.assertEqual([(zone.bottom,zone.top) for zone in zones],[(95,100)])
        row=next(row for row in summary_rows(chart) if row['timeframe']=='H4')
        self.assertEqual((row['flow'],row['fvg']),(-1,-1))
        from flowdesk.finam_screener import select_working_fvg_signals
        self.assertEqual(select_working_fvg_signals(chart,98).as_map()['H4'],row['fvg'])

    def test_summary_does_not_highlight_a_guard_before_its_formation_close(self):
        guard=FVG(0,0,1,110,100,1)
        source=frame('H4',[BASE,BASE+timedelta(hours=4)],
                     [FlowState(i,1,90,120,0,guard=guard) for i in range(2)],
                     BASE+timedelta(hours=6))
        chart=frame('H1',[BASE+timedelta(hours=5)],[FlowState(0)],source.asof,price=105)
        chart.summary_sources={'H4':source}
        self.assertFalse(select_fvg_display(chart,{'H4':source}).zones)
        row=next(row for row in summary_rows(chart) if row['timeframe']=='H4')
        self.assertEqual((row['flow'],row['fvg']),(1,0))

    def test_future_parent_tail_does_not_push_history_window_past_cutoff(self):
        parents=[Candle(BASE+timedelta(hours=i*4),100,110,90,105,100) for i in range(150)]
        cutoff=parents[100].time+timedelta(hours=1)
        main=[Candle(cutoff-timedelta(hours=1),100,110,90,105,100)]
        analysis,_=analyze_timeframes({'H1':main,'H4':parents},'H1',asof=cutoff,
                                      settings=IndicatorSettings().changed(liquidityHistoryBars=100))
        window=analysis.summary_sources['H4'].candles
        self.assertEqual((len(window),window[0].time,window[-1].time),(100,parents[1].time,parents[100].time))

    def test_calculation_horizon_stops_at_main_chart_end_even_with_later_asof(self):
        parents=[Candle(BASE+timedelta(hours=i*4),100,110,90,105,100) for i in range(150)]
        right=parents[100].time+timedelta(hours=1)
        main=[Candle(right-timedelta(hours=1),100,110,90,105,100)]
        analysis,_=analyze_timeframes({'H1':main,'H4':parents},'H1',asof=parents[-1].time,
                                      settings=IndicatorSettings().changed(liquidityHistoryBars=100))
        self.assertEqual(analysis.asof,right)
        self.assertEqual(analysis.summary_sources['H4'].candles[-1].time,parents[100].time)
