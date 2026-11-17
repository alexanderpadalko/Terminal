"""Compare summary signals with the predicates in the actual user v2.13 file."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from itertools import product
from pathlib import Path
import re
from types import SimpleNamespace
import unittest

from flowdesk.chart_indicators import summary_rows
from flowdesk.engine import Analysis, FVG, FlowState
from flowdesk.finam_screener import select_working_fvg_signals
from flowdesk.fvg_display import working_zone
from flowdesk.fvg_summary import REFERENCE_SHA256, summary_fvg_side
from flowdesk.market_data import Candle

BASE=datetime(2026,11,12,tzinfo=timezone.utc)
SOURCE=Path(__file__).resolve().parents[1]/'reference/TradingView_v2.13.pine'


def source(tf='H4',*,guard=None,broken=False,price=100):
    bars=[Candle(BASE+timedelta(hours=i*4),price,max(120,price),min(80,price),price,
                 scheduled_close=BASE+timedelta(hours=(i+1)*4)) for i in range(6)]
    guard=guard or FVG(0,1,2,110,90,1)
    states=[FlowState(i,guard.side,80,120,0,guard=guard,guard_broken=broken)
            for i in range(6)]
    return Analysis(bars,[],[],states,[],tf,asof=bars[-1].scheduled_close)


def pine_predicate(name):
    # Development-only syntax conversion of the pinned Pine function itself.
    # No app implementation/predicates are copied into this oracle.
    raw=SOURCE.read_bytes()
    if hashlib.sha256(raw).hexdigest().upper()!=REFERENCE_SHA256:
        raise AssertionError('v2.13 source changed')
    text=raw.decode('utf-8-sig').replace('\r\n','\n')
    match=re.search(r'^'+name+r'\([^\n]*\) =>\n((?:    [^\n]*\n|\n)+)',text,re.M)
    lines=[]
    for line in match[1].rstrip().splitlines():
        line=re.sub(r'^(\s*)(?:int|bool) ',r'\1',line).replace(':=','=')
        if line.lstrip().startswith('if '):line+=':'
        lines.append(line)
    lines[-1]='    return '+lines[-1].strip()
    namespace={'na':lambda value:value is None}
    exec('def predicate('+('snapshot' if name=='f_summaryFlowSide' else 'level')+', price):\n'+'\n'.join(lines),namespace)
    return namespace['predicate']


class TestSummaryV213(unittest.TestCase):
    def test_flow_and_higher_rules_match_actual_pine_inclusive_predicates(self):
        flow=pine_predicate('f_summaryFlowSide')
        higher=pine_predicate('f_summaryHigherSide')
        # Broken/neutral states, guard outside flow, both directions, and
        # exact/later price boundaries; evaluate the original Pine bodies.
        for side,broken,lo,hi,price in product((-1,0,1),(False,True),
                (80,115),(85,120),(89.99,90,100,110,110.01)):
            item=source()
            for state in item.flows:
                state.side,state.guard_broken,state.lo,state.hi=side,broken,lo,hi
            snapshot=SimpleNamespace(direction=side,firstBroken=broken,
                firstTop=110,firstBottom=90,lo=lo,hi=hi)
            level=SimpleNamespace(side=side,top=110,bottom=90,
                working=side!=0 and not broken and 110>=lo and 90<=hi)
            with self.subTest(side=side,broken=broken,lo=lo,hi=hi,price=price):
                actual=summary_fvg_side(item,item.asof,price)
                self.assertEqual(actual,flow(snapshot,price))
                self.assertEqual(actual,higher(level,price))

    def test_all_five_columns_and_screener_share_the_rule(self):
        main=source('H1')
        main.summary_sources={tf:source(tf) for tf in ('H4','D1','W1','MN1')}
        self.assertEqual([row['fvg'] for row in summary_rows(main)],[1]*5)
        self.assertEqual(select_working_fvg_signals(main,100).directions,(1,)*4)

    def test_si_visible_older_zone_does_not_replace_the_broken_selected_guard(self):
        guard=FVG(2,3,4,82947,82930,-1,broken=5)
        h4=source(guard=guard,broken=True,price=83029)
        h4.fvg=[FVG(0,1,2,83464,83021,-1),guard]
        for state in h4.flows:
            state.lo,state.hi=82538,83811
        self.assertTrue(h4.fvg[0].bottom<=83029<=h4.fvg[0].top)
        main=source('H1',price=83029)
        main.summary_sources={'H4':h4}
        for chart in (main,h4):
            row=next(row for row in summary_rows(chart) if row['timeframe']=='H4')
            self.assertEqual((row['flow'],row['fvg']),(-1,0))
            self.assertEqual(select_working_fvg_signals(chart,83029).as_map()['H4'],0)

    def test_higher_source_publishes_replacement_and_break_only_on_close(self):
        h4=source()
        h4.flows[-1]=replace(h4.flows[-1],guard_broken=True,
                            guard=FVG(3,4,5,105,95,-1),side=-1)
        before=h4.candles[-1].scheduled_close-timedelta(seconds=1)
        self.assertEqual(summary_fvg_side(h4,before,100),1)
        self.assertEqual(summary_fvg_side(h4,h4.asof,100),0)
        # If the new guard is still active it first becomes a short at close.
        h4.flows[-1].guard_broken=False
        self.assertEqual(summary_fvg_side(h4,before,100),1)
        self.assertEqual(summary_fvg_side(h4,h4.asof,100),-1)

    def test_guard_retained_outside_display_array_still_signals(self):
        h4=source()
        h4.fvg=[]
        self.assertEqual(summary_fvg_side(h4,h4.asof,100),1)
        self.assertEqual(summary_fvg_side(None,h4.asof,100),0)
        self.assertEqual(summary_fvg_side(h4,h4.asof,None),0)

    def test_plot_working_guard_keeps_its_default_live_behavior(self):
        h4=source(guard=FVG(0,1,2,110,90,-1))
        for state in h4.flows:
            state.lo,state.hi=115,120
        h4.flows[-1]=replace(h4.flows[-1],lo=80,live_range=True)
        cutoff=h4.asof-timedelta(hours=2)
        self.assertIsNotNone(working_zone(h4,cutoff,cutoff))
        self.assertEqual(summary_fvg_side(h4,cutoff,100),0)
        self.assertEqual(summary_fvg_side(h4,cutoff,100,live=True),-1)
