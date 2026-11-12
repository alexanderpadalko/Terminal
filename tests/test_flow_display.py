"""The v2.11 confirmed flow, previous interval and diagnostic are one snapshot."""
from dataclasses import replace
from datetime import datetime,timedelta
import unittest

from flowdesk.engine import Analysis,FlowState,_project_h1_flows,analyze
from flowdesk.market_data import Candle,MOSCOW
from flowdesk.settings import IndicatorSettings

BASE=datetime(2026,11,12,20,tzinfo=MOSCOW)


def frame(tf,times,states,asof):
    return Analysis([Candle(t,100,110,90,105) for t in times],[],[],states,[],tf,asof=asof)


class TestFlowProjection(unittest.TestCase):
    def test_final_session_hour_is_published_on_2349_minute_close(self):
        source=frame('H1',[BASE+timedelta(hours=2),BASE+timedelta(hours=3)],
                     [FlowState(0,1,90,110,0),FlowState(1,-1,90,110,0,rule=2)],BASE+timedelta(hours=3,minutes=50))
        source.candles[-1]=replace(source.candles[-1],scheduled_close=source.asof)
        minute=Candle(BASE+timedelta(hours=3,minutes=49),100,110,90,105)
        projected=_project_h1_flows([minute],'M1',source,asof=source.asof)
        self.assertEqual((projected[-1].side,projected[-1].rule),(-1,2))
        self.assertEqual(projected[-1].source_confirmed_at,source.asof)


class TestFlowDisplay(unittest.TestCase):
    def plan(self,analysis,parents=None,settings=None):
        from flowdesk.flow_display import select_flow_display
        return select_flow_display(analysis,parents or {},settings or IndicatorSettings())

    def fixture(self):
        state=FlowState(1,1,90,110,0,diagnosis='№3: попутное плечо с FVG',
                        previous=(0,0,70,80,-1))
        parent=frame('H1',[BASE,BASE+timedelta(hours=1)],[state,state],BASE+timedelta(hours=2))
        chart=frame('M1',[BASE+timedelta(hours=2,minutes=i) for i in range(5)],
                    [FlowState(i,diagnosis='H1: Лонг') for i in range(5)],parent.asof+timedelta(minutes=5))
        return chart,parent

    def test_lower_diagnostic_retains_full_h1_text_and_confirmation_time(self):
        chart,parent=self.fixture()
        plan=self.plan(chart,{'H1':parent})
        self.assertIn('№3: попутное плечо с FVG',plan.diagnostic)
        self.assertIn('Снимок на закрытие: 2026-11-12 22:00',plan.diagnostic)
        self.assertEqual(len(plan.levels),10)

    def test_older_previous_interval_keeps_exact_times_and_never_collapses_to_first_bar(self):
        chart,parent=self.fixture()
        start=BASE-timedelta(days=2)
        end=BASE-timedelta(days=1,minutes=10)
        for state in parent.flows:
            state.previous_start_at=start
            state.previous_end_at=end
        plan=self.plan(chart,{'H1':parent})
        old=[line for line in plan.levels if line.previous]
        self.assertEqual([(line.start,line.end) for line in old],[(start,end)]*5)
        self.assertTrue(all(line.end<chart.candles[0].time for line in old))

    def test_hidden_levels_keep_diagnostic_and_h1_transfer_switch_is_independent(self):
        chart,parent=self.fixture()
        for change in ({'flowShowLevels':False},{'flowH1OnM5':False}):
            plan=self.plan(chart,{'H1':parent},IndicatorSettings().changed(**change))
            self.assertEqual(plan.levels,())
            self.assertIn('№3:',plan.diagnostic)

    def test_native_diagnostic_uses_closed_snapshot_and_reports_pending_reversal(self):
        bars=[Candle(BASE,9.5,10,9,9.5),Candle(BASE+timedelta(hours=1),9.8,11,9.3,10.8),
              Candle(BASE+timedelta(hours=2),9.5,10,8.6,8.8)]
        chart=analyze(bars,'H1',asof=BASE+timedelta(hours=2,minutes=15))
        plan=self.plan(chart)
        self.assertIn(chart.flows[1].diagnosis,plan.diagnostic)
        self.assertIn('На формирующейся свече: ШОРТ; подтверждение после её закрытия',plan.diagnostic)
        self.assertEqual(plan.state.side,1)

    def test_future_parent_state_cannot_replace_short_chart_levels(self):
        chart,parent=self.fixture()
        parent.candles.append(Candle(BASE+timedelta(hours=2),100,130,80,120))
        parent.flows.append(FlowState(2,-1,80,130,2))
        parent.asof=BASE+timedelta(hours=3)
        self.assertEqual(self.plan(chart,{'H1':parent}).state.side,1)

    def test_previous_end_is_copied_from_pinned_snapshot_without_index_rounding(self):
        from flowdesk.reference_engine import PinnedFlowEngine
        from flowdesk.engine import StructureEngine,PairEngine
        engine=PinnedFlowEngine('H1',IndicatorSettings())
        structure=StructureEngine();pairs=PairEngine()
        bars=[Candle(BASE,9.5,10,9,9.5),Candle(BASE+timedelta(hours=1),9.8,11,9.3,10.8),
              Candle(BASE+timedelta(hours=2),9.5,10,8.6,8.8)]
        for i,bar in enumerate(bars):
            sp=structure.step(bar,i)
            state=engine.step(bar,i,sp,None,bars,pairs.step(bar,sp))
        raw=engine.context.states['f_flowSnapshot']['current']
        self.assertEqual(state.previous_end_at.timestamp()*1000,raw.prevEnd)
