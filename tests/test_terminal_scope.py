"""Active terminal removals must eliminate work, including with legacy caches."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from flowdesk.cancellation import Cancelled, scope
from flowdesk.engine import StructureEngine, analyze
from flowdesk.market_data import Candle
from flowdesk.pipeline import analyze_timeframes
from flowdesk.settings import IndicatorSettings, ApplicationSettings
from flowdesk.terminal_policy import DISPLAY_TIMEFRAMES, SOURCE_TIMEFRAMES, FLOW_TIMEFRAMES, retired_setting

NOW=datetime(2026,11,17,12,tzinfo=timezone.utc)


def bars(minutes, count=12):
    step=timedelta(minutes=minutes)
    return [Candle(NOW-step*(count-i),100+i,103+i,98+i,101+i,10,
                   scheduled_close=NOW-step*(count-i-1)) for i in range(count)]


class TerminalScopeTests(unittest.TestCase):
    def test_cancellation_stops_m5_replay_before_the_full_history(self):
        rows = bars(5, count=600)
        step = StructureEngine.step
        completed = 0

        def track(engine, candle, index):
            nonlocal completed
            completed += 1
            return step(engine, candle, index)

        with patch.object(StructureEngine, 'step', autospec=True, side_effect=track):
            with self.assertRaises(Cancelled), scope(lambda: completed >= 10):
                analyze_timeframes({'M5': rows}, 'M5', asof=NOW, terminal=True)
        self.assertGreaterEqual(completed, 10)
        self.assertLess(completed, len(rows))
        main, _ = analyze_timeframes({'M5': rows}, 'M5', asof=NOW, terminal=True)
        self.assertEqual(main.candles, rows)

    def test_pipeline_never_analyzes_h1_m1_or_market_block(self):
        series={tf:bars(step) for tf,step in
                [('M1',1),('M5',5),('H1',60),('H4',240),('D1',1440),('W1',10080),('MN1',43200)]}
        calls=[]
        def tracked(source,tf,**kwargs):
            calls.append(tf)
            return analyze(source,tf,**kwargs)
        with patch('flowdesk.pipeline.analyze',side_effect=tracked), \
             patch('flowdesk.reference_engine.market_state',side_effect=AssertionError('retired market block')), \
             patch('flowdesk.engine._project_h1_flows',side_effect=AssertionError('retired projection')):
            main,parents=analyze_timeframes(series,'M5',asof=NOW,terminal=True)
            self.assertEqual(set(calls),set(DISPLAY_TIMEFRAMES))
            self.assertEqual(set(parents),set(FLOW_TIMEFRAMES))
            self.assertEqual(set(main.summary_sources),set(FLOW_TIMEFRAMES))
            for source in (main,*parents.values(),*main.summary_sources.values()):
                self.assertFalse(source.market)
            self.assertFalse(main.flows)
            self.assertFalse(main.pairs)
            self.assertTrue(main.segments)
            again,_=analyze_timeframes(series,'M5',asof=NOW,
                                      cached={'M5':main,**parents},terminal=True)
            self.assertFalse(again.flows)
        for retired in ('M1','H1'):
            with self.assertRaises(ValueError):
                analyze_timeframes(series,retired,asof=NOW,terminal=True)

    def test_m5_skips_pair_engine_and_keeps_candles_zigzag_and_fvg(self):
        source=bars(5)
        expected=analyze(source,'M5',asof=NOW)
        with patch('flowdesk.engine.PairEngine',side_effect=AssertionError('unused pairs')):
            result=analyze(source,'M5',asof=NOW,low_tf_flow=False,market_structure=False)
        self.assertEqual(result.candles,expected.candles)
        self.assertEqual(result.segments,expected.segments)
        self.assertEqual(result.fvg,expected.fvg)
        self.assertEqual(result.flow_status,'DISABLED')
        self.assertEqual((result.flows,result.pairs,result.liquidity_audit),([],[],[]))

    def test_remaining_flows_and_zigzag_match_reference_calculations(self):
        series={tf:bars(step) for tf,step in
                [('H1',60),('H4',240),('D1',1440),('W1',10080),('MN1',43200)]}
        active,parents=analyze_timeframes(series,'H4',asof=NOW,terminal=True)
        reference,original=analyze_timeframes(series,'H4',asof=NOW)
        for frame,current in {'H4':active,**parents}.items():
            before={'H4':reference,**original}[frame]
            self.assertEqual(current.reference_version,before.reference_version)
            for field in ('candles','segments','flows','pairs','fvg','liquidity_audit'):
                self.assertEqual(getattr(current,field),getattr(before,field),(frame,field))

    def test_m5_never_draws_any_flow_even_with_legacy_analysis(self):
        from flowdesk.flow_display import select_flow_display
        result=select_flow_display(analyze(bars(5),'M5'),{'H1':analyze(bars(60),'H1')},IndicatorSettings())
        self.assertEqual((result.state,result.levels,result.diagnostic),(None,(),''))

    def test_m5_empty_diagnostic_is_safe_with_legacy_flow_objects(self):
        from types import SimpleNamespace
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QImage, QPainter
        from flowdesk.indicator_paint import draw_overlays
        from flowdesk.flow_display import select_flow_display
        app=QApplication.instance() or QApplication([])
        settings=IndicatorSettings().changed(flowDiagnosticEnabled=True,currentFvgTableEnabled=False)
        legacy=analyze(bars(5),'M5')
        self.assertTrue(legacy.flows)
        chart=SimpleNamespace(indicator_settings=settings,analysis=legacy,
                              flow_plan=select_flow_display(legacy,{},settings),terminal=True)
        surface=QImage(100,100,QImage.Format.Format_ARGB32)
        painter=QPainter(surface)
        try:draw_overlays(chart,painter)
        finally:painter.end()

    def test_live_m5_checkpoint_has_no_pair_state_and_matches_full_replay(self):
        from dataclasses import replace
        source=bars(5)
        observed=source[-1].time+timedelta(minutes=1)
        options=dict(asof=observed,low_tf_flow=False,market_structure=False)
        first=analyze(source,'M5',**options)
        self.assertIsNone(first.replay_checkpoint.engines[2])
        changed=source[:-1]+[replace(source[-1],high=source[-1].high+1)]
        resumed=analyze(changed,'M5',cached=first,**options)
        replay=analyze(changed,'M5',**options)
        for field in ('candles','segments','fvg','flows','pairs'):
            self.assertEqual(getattr(resumed,field),getattr(replay,field),field)

    def test_active_service_does_not_reuse_legacy_flow_cache(self):
        from flowdesk.finam_market import FinamMarketService
        from test_finam_only import FakeAPI
        settings=IndicatorSettings()
        with tempfile.TemporaryDirectory() as folder:
            service=FinamMarketService(Path(folder)/'cache.sqlite',api=FakeAPI(),clock=lambda:NOW)
            series={'M5':bars(5),'H1':bars(60),'H4':bars(240)}
            old,_=service.analyze_cached('SBER@MISX','M5',series,settings,NOW,terminal=False)
            active,_=service.analyze_cached('SBER@MISX','M5',series,settings,NOW)
            self.assertTrue(old.flows)
            self.assertFalse(active.flows)
            self.assertFalse(active.market)

    def test_m1_not_loaded_requested_subscribed_or_archived_old_rows_preserved(self):
        from contextlib import closing
        from flowdesk.finam_market import FinamMarketService
        from flowdesk.finam_api import FinamAPI
        from flowdesk.binance_api import BinanceAPI
        from flowdesk.history_archive import FRAMES
        from test_finam_only import FakeAPI
        api=FakeAPI()
        with tempfile.TemporaryDirectory() as folder:
            service=FinamMarketService(Path(folder)/'cache.sqlite',api=api,clock=lambda:NOW)
            service._upsert('SBER@MISX','M1',bars(1))
            service.refresh_series('SBER@MISX',('M1',*SOURCE_TIMEFRAMES))
            self.assertNotIn('M1',service._series('SBER@MISX'))
            self.assertIn('H1',service._series('SBER@MISX'))
            self.assertNotIn('M1',{tf for _,tf in api.calls})
            with closing(service._connection()) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM candles WHERE tf='M1'").fetchone()[0],12)
            for tf in ('M1','H1'):
                with self.assertRaises(ValueError):service.view('SBER','MISX',tf)
        self.assertNotIn('M1',FRAMES)
        for client in (FinamAPI,BinanceAPI):
            self.assertNotIn('M1',client.stream_frames)
            self.assertIn('H1',client.stream_frames)

    def test_terminal_settings_remove_market_and_retired_frame_controls(self):
        from PySide6.QtWidgets import QApplication
        from flowdesk.settings_dialog import SettingsDialog
        app=QApplication.instance() or QApplication([])
        dialog=SettingsDialog(IndicatorSettings(),ApplicationSettings(),finam_only=True)
        try:
            self.assertFalse(any(retired_setting(key) for key in dialog.controls))
            self.assertIn('showStructure',dialog.controls)
            self.assertIn('fvgShowM5',dialog.controls)
            self.assertIn('vwapEnabled',dialog.controls)
            saved,_=dialog.values()
            self.assertFalse(saved.enableMarketStructureInput)
            self.assertFalse(saved.showMarketIdmInput)
            self.assertFalse(saved.flowH1OnM5)
            dialog.restore_defaults()
            restored,_=dialog.values()
            self.assertFalse(restored.enableMarketStructureInput)
        finally:dialog.close()

    def test_visible_intervals_and_summary_have_no_h1_m1(self):
        from PySide6.QtWidgets import QApplication
        from flowdesk.gui import TimeframeBar
        from flowdesk.chart_indicators import summary_rows
        app=QApplication.instance() or QApplication([])
        bar=TimeframeBar(frames=DISPLAY_TIMEFRAMES)
        self.assertEqual(tuple(bar.buttons),DISPLAY_TIMEFRAMES)
        self.assertEqual(bar.currentText(),'H4')
        main,_=analyze_timeframes({'H4':bars(240)},'H4',asof=NOW,terminal=True)
        self.assertEqual(tuple(row['timeframe'] for row in summary_rows(main,FLOW_TIMEFRAMES)),FLOW_TIMEFRAMES)
        bar.close()
