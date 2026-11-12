from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest

from flowdesk.engine import Analysis, FVG, FlowState
from flowdesk.fvg_display import (DisplaySettings, DisplayZone, FlowView, REFERENCE_SHA256,
    in_flow, in_working_parent, select_fvg_display, working_zone)
from flowdesk.market_data import Candle, MOSCOW, candle_close_time

T0 = datetime(2026, 1, 1, tzinfo=MOSCOW)


def source(tf='H1', *, start=T0, count=8, guard=True, zones=None):
    step = {'M1': timedelta(minutes=1), 'M5': timedelta(minutes=5), 'H1': timedelta(hours=1),
            'H4': timedelta(hours=4), 'D1': timedelta(days=1), 'W1': timedelta(days=7)}.get(tf)
    times = [start+i*step if step else start.replace(month=i+1) for i in range(count)]
    bars = [Candle(t, 100, 500, 0, 100) for t in times]
    selected = FVG(1, 2, 3, 110, 100, 1, source_tf=tf) if guard else None
    states = [FlowState(i, side=1, lo=50, hi=200, start=0, guard=selected) for i in range(count)]
    return Analysis(bars, [], list(zones or []), states, [], tf,
                    asof=candle_close_time(bars[-1], tf))


def lower(zones=None, tf='M1'):
    return source(tf, start=T0+timedelta(days=365), count=120, zones=zones)


def child(*, bottom=104, top=106, start=10, broken=None, side=1):
    return FVG(start, start+1, start+2, top, bottom, side, broken=broken, source_tf='M1')


class TestFvgDisplay(unittest.TestCase):
    def test_global_switch_disables_entire_plan(self):
        plan = select_fvg_display(lower([child()]), {'H1': source()}, DisplaySettings(enabled=False))
        self.assertEqual(plan.zones, ())
        self.assertEqual(plan.parents, ())

    def test_actual_v211_pine_predicate_results(self):
        value = json.loads((Path(__file__).parent/'fixtures/fvg_display_v211.json').read_text())
        self.assertEqual(value['reference_sha256'], REFERENCE_SHA256)
        def dt(v): return None if v is None else T0+timedelta(seconds=v)
        def display(z):
            return DisplayZone('M1', dt(z.get('leftTime', 0)), dt(z.get('impulseTime', 1)), T0,
                z['top'], z['bottom'], z.get('side', 1), T0, not z.get('active', True))
        for i, case in enumerate(value['cases']):
            with self.subTest(case=i, kind=case['kind']):
                if case['kind'] == 'child':
                    result = in_working_parent(display(case['child']), [display(z) for z in case['parents']])
                elif case['kind'] == 'flow':
                    s = case['snapshot']
                    flow = FlowView(s['direction'], s['lo'], s['hi'], dt(s['startTime']),
                        (dt(s['prevStart']), dt(s['prevEnd']), s['prevLo'], s['prevHi']))
                    result = in_flow(display(case['zone']), flow)
                else:
                    s = case['snapshot']; a = source(count=4)
                    z = FVG(s['workingLeftTime'], s['workingImpulseTime'], 2,
                            s['firstTop'], s['firstBottom'], 1, source_tf='H1')
                    a.flows = [FlowState(j, side=s['direction'], lo=s['lo'], hi=s['hi'],
                        start=0, guard=z, guard_broken=s['firstBroken']) for j in range(4)]
                    result = working_zone(a, a.asof, a.asof) is not None
                self.assertEqual(result, case['expected'])

    def test_lower_requires_displayed_working_parent_and_strict_price_overlap(self):
        zones = [child(), child(bottom=108, top=112, start=20),
                 child(bottom=90, top=100, start=30), child(bottom=110, top=120, start=40)]
        a = lower(zones); parent = source()
        plan = select_fvg_display(a, {'H1': parent})
        self.assertEqual([z.source_tf for z in plan.zones], ['H1', 'M1', 'M1'])
        self.assertEqual([z.bottom for z in plan.zones[1:]], [108, 104])
        self.assertEqual(len(plan.parents), 1)
        self.assertEqual(select_fvg_display(a, {}).zones, ())
        self.assertEqual(select_fvg_display(a, {'H1': parent}, DisplaySettings(higher=())).zones, ())

    def test_children_of_either_direction_must_not_start_before_parent(self):
        parent = source()
        for tf in ('M1', 'M5'):
            a = source(tf, count=120, guard=False, start=T0)
            a.asof = T0+timedelta(hours=8)
            # Parent starts at 01:00; a three-candle child starting at 00:00
            # cannot be admitted even when price and direction overlap.
            a.fvg = [child(start=0, side=-1), child(start=60 if tf=='M1' else 12, side=-1)]
            plan = select_fvg_display(a, {'H1': parent})
            self.assertEqual(len([z for z in plan.zones if not z.parent]), 1)
            self.assertEqual(plan.zones[-1].side, -1)

    def test_working_guard_survives_retention_and_rebalance_without_fallback(self):
        a = lower([child()]); parent = source(zones=[FVG(0, 1, 2, 210, 200, 1)])
        parent.flows[-1].guard.balanced = True
        self.assertEqual(len(select_fvg_display(a, {'H1': parent}).zones), 2)
        parent.flows[-1].guard_broken = True
        self.assertEqual(select_fvg_display(a, {'H1': parent}).zones, ())

    def test_global_limit_prioritizes_higher_and_only_drawn_parents_admit_children(self):
        parents = {tf: source(tf) for tf in ('H1', 'H4', 'D1', 'W1', 'MN1')}
        for i, p in enumerate(parents.values()):
            guard = p.flows[-1].guard
            guard.bottom, guard.top = 100+i*10, 110+i*10
        a = lower([child(bottom=144, top=146)])
        plan = select_fvg_display(a, parents, DisplaySettings(max_zones=2))
        self.assertEqual([z.source_tf for z in plan.zones], ['H1', 'H4'])
        self.assertEqual(len(plan.parents), 2)
        plan = select_fvg_display(a, parents)
        self.assertEqual([z.source_tf for z in plan.zones], ['H1', 'H4', 'D1', 'W1', 'MN1', 'M1'])

    def test_native_flow_filter_includes_previous_and_uses_impulse_not_left_time(self):
        zones = [FVG(0, 1, 2, 110, 100, 1), FVG(2, 3, 4, 150, 140, -1),
                 FVG(3, 4, 5, 90, 80, -1), FVG(4, 5, 6, 310, 300, 1)]
        a = source(zones=zones)
        a.flows[-1] = FlowState(7, side=1, lo=130, hi=160, start=3,
                                previous=(0, 1, 95, 115, -1))
        plan = select_fvg_display(a)
        self.assertEqual([z.bottom for z in plan.zones], [140, 100])
        all_plan = select_fvg_display(a, settings=DisplaySettings(only_in_flow=False))
        self.assertEqual(len(all_plan.zones), 4)

    def test_broken_child_is_kept_and_ends_at_break_close_not_break_open(self):
        a = lower([child(broken=20)])
        plan = select_fvg_display(a, {'H1': source()})
        zone = plan.zones[-1]
        self.assertTrue(zone.broken)
        self.assertEqual(zone.right_time, candle_close_time(a.candles[20], 'M1'))
        a.candles.append(Candle(a.candles[-1].time+timedelta(minutes=1), 100, 500, 0, 100))
        a.flows.append(replace(a.flows[-1], i=120)); a.asof += timedelta(minutes=1)
        self.assertEqual(select_fvg_display(a, {'H1': source()}).zones[-1].right_time, zone.right_time)

    def test_no_future_formation_or_break_and_no_open_parent_replacement(self):
        parent = source()
        old = parent.flows[0].guard
        new = FVG(4, 5, 6, 210, 200, 1, source_tf='H1')
        for i in range(6, 8): parent.flows[i] = replace(parent.flows[i], guard=new)
        before = T0+timedelta(hours=6, minutes=59)
        self.assertEqual(working_zone(parent, before, before).bottom, 100)
        after = T0+timedelta(hours=7)
        self.assertEqual(working_zone(parent, after, after).bottom, 200)
        a = lower([child(broken=30), child(start=70)])
        a.asof = candle_close_time(a.candles[25], 'M1')
        plan = select_fvg_display(a, {'H1': source()})
        self.assertEqual(len(plan.zones), 2)
        self.assertFalse(plan.zones[-1].broken)

    def test_settings_limit_and_source_history_are_independent_of_calculation(self):
        zones = [child(start=i) for i in range(40)]
        a = lower(zones); original = [(z.start,z.broken,z.top,z.bottom) for z in a.fvg]
        plan = select_fvg_display(a, {'H1': source()}, DisplaySettings(max_zones=5))
        self.assertEqual(len(plan.zones), 5)
        self.assertEqual([z.left_time for z in plan.zones[1:]], [a.candles[i].time for i in (39,38,37,36)])
        self.assertEqual([(z.start,z.broken,z.top,z.bottom) for z in a.fvg], original)
        self.assertEqual(plan.rejected['source_history_limit'], 35)

    def test_hiding_lower_keeps_working_parents_and_no_m5_parent_on_m1(self):
        a = lower([child()])
        plan = select_fvg_display(a, {'H1': source(), 'M5': source('M5')}, DisplaySettings(show_lower=False))
        self.assertEqual([z.source_tf for z in plan.zones], ['H1'])
