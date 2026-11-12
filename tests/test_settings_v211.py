import json
import random
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch

from flowdesk.settings import IndicatorSettings,ApplicationSettings,SettingsStore,specifications
from flowdesk.chart_indicators import vwap_series,summary_rows
from flowdesk.engine import Analysis,FlowState,PairSnapshot,FVG,analyze
from flowdesk.pipeline import analyze_timeframes
from flowdesk.market_data import Candle,MOSCOW,aggregate
from flowdesk.reference_runtime import ReferenceContext,compare,binary
from flowdesk.reference_liquidity import audit_captures

BASE=datetime(2026,6,1,10,tzinfo=MOSCOW)


def bars(count=100,step=timedelta(hours=1)):
    rng=random.Random(211)
    result=[]
    price=100
    for i in range(count):
        end=price+rng.uniform(-4,4)
        result.append(Candle(BASE+i*step,price,max(price,end)+rng.random()*3,min(price,end)-rng.random()*3,end,100))
        price=end
    return result


class TestSettingsV211(unittest.TestCase):
    def test_all_74_source_inputs_have_typed_defaults_and_original_groups(self):
        settings=IndicatorSettings()
        self.assertEqual(len(specifications()),74)
        self.assertEqual(len(settings.as_dict()),74)
        self.assertEqual(len({x['group'] for x in specifications()}),6)
        self.assertEqual(settings.fvgMaxZones,20)
        self.assertEqual(settings.liquidityHistoryBars,1000)
        self.assertFalse(settings.flowShowData)
        self.assertEqual(settings.fvgBullColor,'#00332A26')
        self.assertEqual(settings.fvgFontM,'huge')

    def test_invalid_ranges_types_and_options_are_rejected(self):
        for change in ({'fvgMaxZones':0},{'fvgMaxZones':201},{'fvgMaxZones':True},
                       {'liquidityHistoryBars':99},{'flowShowData':'false'},{'flowFontSize':'20'},
                       {'upColor':'#FF0000'},{'vwapSource':'__import__'}):
            with self.subTest(change=change),self.assertRaises(ValueError):IndicatorSettings(change)

    def test_settings_survive_new_store_instance_and_defaults_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'settings.json'
            store=SettingsStore(path)
            indicator=IndicatorSettings().changed(fvgMaxZones=7,showH4OnM5=False,flowShowData=True)
            application=ApplicationSettings(False,120,True)
            store.write(indicator,application)
            new_i,new_a=SettingsStore(path).read()
            self.assertEqual(new_i.as_dict(),indicator.as_dict())
            self.assertEqual(new_a,application)
            store.write(IndicatorSettings(),ApplicationSettings())
            self.assertEqual(SettingsStore(path).read()[0].as_dict(),IndicatorSettings().as_dict())

    def test_corrupted_settings_recover_without_overwriting_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'settings.json'
            path.write_text('{broken',encoding='utf-8')
            store=SettingsStore(path)
            settings,_=store.read()
            self.assertEqual(settings.fvgMaxZones,20)
            self.assertTrue(store.warning)
            self.assertEqual(path.read_text(encoding='utf-8'),'{broken')

    def test_visual_changes_keep_calculation_key_but_filters_change_it(self):
        settings=IndicatorSettings()
        self.assertEqual(settings.calculation_key(),settings.changed(flowShowData=True,fvgFontM='small').calculation_key())
        for change in ({'fvgMaxZones':7},{'fvgExcludeGaps':False},{'liquidityHistoryBars':100}):
            self.assertNotEqual(settings.calculation_key(),settings.changed(**change).calculation_key())

    def test_legacy_flow_deepcopy_does_not_alias_reference_state(self):
        context=ReferenceContext('H1',IndicatorSettings())
        data=bars(5)
        for i in range(4):
            context.observe(data,i)
            context.call('f_flowSnapshot',False,'','','',None,None,True)
        saved=deepcopy(context)
        context.observe(data,4)
        context.call('f_flowSnapshot',False,'','','',None,None,True)
        self.assertEqual(saved.index,3)
        self.assertEqual(saved.states['f_flowSnapshot']['time'] if 'time' in saved.states['f_flowSnapshot'] else saved.index,3)


class TestV211Calculations(unittest.TestCase):
    def test_future_http_tail_cannot_enter_market_replay_before_observation(self):
        data=bars(40)
        cutoff=BASE+timedelta(hours=10,minutes=30)
        early=analyze(data[:11],'H1',asof=cutoff)
        future=analyze(data,'H1',asof=cutoff)
        self.assertEqual(future.market,early.market)
        self.assertEqual(future.market['source_right_index'],10.5)

    def test_rule_two_uses_confirmed_close_and_live_keeps_previous_direction(self):
        data=[Candle(BASE,9,10,8,9,100),Candle(BASE+timedelta(hours=1),10,11,9.3,10.8,100),
              Candle(BASE+timedelta(hours=2),9,10,7.5,7.8,100)]
        before=analyze(data,'H1',asof=BASE+timedelta(hours=2,minutes=30))
        after=analyze(data,'H1',asof=BASE+timedelta(hours=3))
        self.assertEqual((before.flows[-1].side,before.flows[-1].rule),(1,0))
        self.assertEqual((after.flows[-1].side,after.flows[-1].rule),(-1,2))
        self.assertEqual(after.reference_version,'2.11')

    def test_bounded_source_fvg_archive_does_not_bound_structural_history(self):
        data=bars(500)
        result=analyze(data,'H4',settings=IndicatorSettings().changed(fvgMaxZones=3))
        self.assertLessEqual(len(result.fvg),3)
        self.assertEqual(len(result.flows),500)
        self.assertGreater(len(result.segments),3)

    def test_higher_history_window_is_distinct_from_full_chart_history(self):
        data=bars(400)
        series={tf:data for tf in ('H1','H4','D1','W1','MN1')}
        result,parents=analyze_timeframes(series,'H1',asof=BASE+timedelta(days=10000),
                                         settings=IndicatorSettings().changed(liquidityHistoryBars=100))
        self.assertEqual(len(result.candles),400)
        self.assertEqual(len(parents['H4'].candles),400)
        self.assertEqual(len(result.summary_sources['H4'].candles),100)
        self.assertEqual(len(result.summary_sources['MN1'].candles),100)

    def test_h4_capture_is_consumed_by_h1_and_wins_equal_time_tie(self):
        from flowdesk.liquidity import select_pine_capture,Capture
        h4=Capture('H4',1,BASE,'MIN/IDM',99,False)
        daily=Capture('D1',-1,BASE,'MAX/IDM',110,False)
        self.assertIs(select_pine_capture({'H4':h4,'D1':daily}),h4)
        higher_bar=Candle(BASE-timedelta(hours=4),105,110,100,105,100)
        source=Analysis([higher_bar],[],[],[FlowState(0,side=1,lo=100,hi=110)],
                        [PairSnapshot(110,100,0,0,1)],'H4')
        consumer=Analysis([Candle(BASE,105,108,99,105,100)],[],[],[],[],'H1')
        rows=audit_captures(consumer,{'H4':source},asof=None,settings=IndicatorSettings())
        self.assertEqual((rows[0].selected.source_tf,rows[0].selected.side),('H4',1))

    def test_first_bar_missing_history_uses_pine_na_semantics(self):
        self.assertFalse(compare(None,10,'Lt'))
        self.assertIsNone(binary(None,1,'Sub'))
        a=analyze(bars(1),'H1')
        self.assertEqual(a.flows[0].side,0)
        self.assertEqual(a.fvg,[])

    def test_market_scope_toggle_calculates_other_frames_and_can_disable_block(self):
        data=bars(100)
        self.assertFalse(analyze(data,'D1').market)
        self.assertTrue(analyze(data,'D1',settings=IndicatorSettings().changed(showMarketOnlyOnH1=False)).market)
        self.assertFalse(analyze(data,'H1',settings=IndicatorSettings().changed(enableMarketStructureInput=False)).market)


class TestVwapAndSummary(unittest.TestCase):
    def test_vwap_is_volume_weighted_and_session_reset_preserves_prior_final(self):
        data=[Candle(BASE,10,10,10,10,1),Candle(BASE+timedelta(hours=1),20,20,20,20,3),
              Candle(BASE+timedelta(days=1),30,30,30,30,2)]
        values,previous=vwap_series(data)
        self.assertEqual(values,[10,17.5,30])
        self.assertEqual(previous,[[2,3,17.5]])

    def test_zero_volume_period_does_not_resurrect_older_vwap(self):
        data=[Candle(BASE,10,10,10,10,1),Candle(BASE+timedelta(days=1),20,20,20,20,0),
              Candle(BASE+timedelta(days=2),30,30,30,30,1)]
        values,previous=vwap_series(data)
        self.assertEqual(values,[10,None,30])
        self.assertEqual(previous,[[1,2,10]])

    def test_summary_distinguishes_flow_direction_and_price_in_fvg(self):
        data=bars(3)
        price=data[-1].close
        guard=FVG(0,1,2,price+1,price-1,1)
        source=Analysis(data,[],[],[FlowState(i,1,price-3,price+3,guard=guard) for i in range(3)],[], 'H1')
        main=Analysis(data,[],[],source.flows,[],'M1')
        main.summary_sources={'H1':source}
        row=summary_rows(main)[0]
        self.assertEqual((row['flow'],row['fvg']),(1,1))
        guard.broken=2
        row=summary_rows(main)[0]
        # Explicit guard status is the published source state, not mutable raw-zone status.
        source.flows[-1].guard_broken=True
        self.assertEqual(summary_rows(main)[0]['fvg'],0)
        self.assertEqual(summary_rows(main)[0]['flow'],1)
