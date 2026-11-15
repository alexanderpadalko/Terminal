"""v2.14 original Pine guard selection versus the executing FlowDesk replay."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

from flowdesk.chart_indicators import summary_rows
from flowdesk.engine import Analysis, PairEngine, StructureEngine
from flowdesk.finam_screener import select_working_fvg_signals
from flowdesk.market_data import Candle, candle_close_time
from flowdesk.reference_engine import PinnedFlowEngine
from flowdesk.reference_runtime import program
from flowdesk.settings import IndicatorSettings
from flowdesk.v214_working_fvg import ACTIVE_VERSION

SOURCE = Path(__file__).resolve().parents[1] / "reference/TradingView_v2.14.pine"
BASE = datetime(2026, 11, 12, 10, tzinfo=timezone.utc)


def v214_replay(*, side=1, has_guard=True, ready=False, dead=False,
                nearer=True, origin_in_flow=True):
    candles = [Candle(BASE + timedelta(hours=i), 100, 105, 95, 100, 10)
               for i in range(3)]
    eng = PinnedFlowEngine("H1", IndicatorSettings())
    structure, pairs = StructureEngine(), PairEngine()
    def step(i):
        point = structure.step(candles[i], i)
        pair = pairs.step(candles[i], point)
        return eng.step(candles[i], i, point, None, candles, pair)
    step(0)
    state = eng.context.states["f_flowSnapshot"]
    t0, t1 = [int(candles[i].time.timestamp() * 1000) for i in (0, 1)]
    state.update(
        flowDir=side, flowStart=t0, flowLo=80, flowHi=130,
        flowLoTime=t0, flowHiTime=t0, flowOriginLocked=True,
        guardTop=110 if has_guard else None,
        guardBottom=90 if has_guard else None,
        guardLeftTime=t0 if has_guard else None,
        guardImpulse=t0 if has_guard else None,
        guardBalanced=False, flowHasFvg=has_guard,
        firstBroken=False, firstBrokenAt=None,
        structuralReverseLo=95, structuralReverseHi=105,
        structuralReverseLoTime=t0, structuralReverseHiTime=t0,
    )
    # For SHORT, a new bottom < 90 is nearer the flow's lower extreme.
    # For LONG, a new top > 110 is nearer its upper extreme.
    top = (115 if nearer else 109) if side == 1 else 86
    bottom = 108 if side == 1 else (85 if nearer else 91)
    if side == -1 and not nearer:
        top = 109
    # No live market requests, no fabricated historical signal:
    # these are deliberately synthetic in-memory Pine record states.
    zone = eng.context._base["FlowFvg"].new(
        top, bottom, side, t1, False, t1 if dead else None, t0
    )
    origin = t1 if origin_in_flow else t0 - 3600000
    saved = eng.context._base["PendingFlowFvg"].new(
        zone, t1, origin, 1, 100, 95, 115, ready, None, False,
        False, t0, t1
    )
    state["pendingFvgs"].append(saved)
    out=step(1)
    return eng, out, candles, saved


class TestWorkingFvgV214(unittest.TestCase):
    def test_pine_source_is_the_actual_new_rule_not_just_a_version_label(self):
        pine=SOURCE.read_text(encoding="utf-8-sig")
        self.assertIn('v2.14 H1 + H4',pine[:300])
        self.assertIn(
            'bool workingCandidate = sameFlowZone and (saved.ready or (not na(guardTop) and saved.originTime >= flowStart))',
            pine
        )
        self.assertIn(
            'bool nearerToExtreme = na(guardTop) or (saved.zone.side == 1 ? saved.zone.top > guardTop : saved.zone.bottom < guardBottom)',
            pine
        )
        self.assertIn('firstBroken := dead',pine)
        self.assertIn('bool selectedWorking = sameFlowZone and not na(guardTop)',pine)

    def test_runtime_uses_patched_numeric_pine_not_legacy_guard_rule(self):
        data,functions,_=program()
        self.assertEqual(data["version"], ACTIVE_VERSION)
        snapshot=functions["f_flowSnapshot"]
        self.assertIn("workingCandidate", snapshot[1].co_names)
        self.assertIn("selectedWorking", snapshot[1].co_names)

    def test_first_working_fvg_still_requires_its_own_ready_leg(self):
        _, state, _, saved = v214_replay(has_guard=False, ready=False)
        self.assertFalse(saved.ready)
        self.assertIsNone(state.guard)
        self.assertFalse(state.guard_broken)

    def test_first_ready_guard_can_be_selected(self):
        _, state, _, _ = v214_replay(has_guard=False, ready=True)
        self.assertIsNotNone(state.guard)
        self.assertEqual((state.guard.bottom, state.guard.top), (108, 115))
        self.assertFalse(state.guard_broken)

    def test_unready_closer_bull_fvg_replaces_existing_guard(self):
        _, state, _, saved = v214_replay()
        self.assertFalse(saved.ready)
        self.assertEqual((state.guard.bottom, state.guard.top), (108, 115))
        self.assertFalse(state.guard_broken)

    def test_unready_closer_bear_fvg_replaces_existing_guard(self):
        _, state, _, saved = v214_replay(side=-1)
        self.assertFalse(saved.ready)
        self.assertEqual((state.guard.bottom, state.guard.top), (85, 86))
        self.assertFalse(state.guard_broken)

    def test_unready_worse_or_stale_candidate_cannot_replace_guard(self):
        for nearer, within in ((False,True),(True,False)):
            with self.subTest(nearer=nearer,within=within):
                _, state, _, _=v214_replay(nearer=nearer,origin_in_flow=within)
                self.assertEqual((state.guard.bottom, state.guard.top),(90,110))

    def test_broken_new_guard_is_sticky_and_old_zone_cannot_signal(self):
        eng, state, bars, _=v214_replay(dead=True)
        self.assertEqual((state.guard.bottom,state.guard.top),(108,115))
        self.assertTrue(state.guard_broken)
        state.source_bar_time = bars[1].time
        state.source_confirmed_at = candle_close_time(bars[1], "H1")
        chart=Analysis(bars[:2], [], [], eng.history, [], "H1",
                       asof=candle_close_time(bars[1], "H1"))
        chart.summary_sources={}
        self.assertEqual(summary_rows(chart)[0]["flow"], 1)
        self.assertEqual(summary_rows(chart)[0]["fvg"], 0)
        self.assertEqual(
            select_working_fvg_signals(chart, 100).as_map()["H1"],0
        )
        self.assertTrue("рабочий FVG: пробит" in state.diagnosis)

if __name__=="__main__":
    unittest.main()
