"""Reusing an unchanged interval must equal a fresh v2.11 replay."""
from datetime import datetime,timedelta
from dataclasses import replace
import random
import unittest

from flowdesk.market_data import Candle,MOSCOW,aggregate
from flowdesk.pipeline import analyze_timeframes


class TestAnalysisReuse(unittest.TestCase):
    def series(self):
        base=datetime(2026,10,1,7,tzinfo=MOSCOW)
        rng=random.Random(211);price=100;hours=[]
        for i in range(120):
            end=price+rng.uniform(-2,2)
            hours.append(Candle(base+timedelta(hours=i),price,max(price,end)+1,min(price,end)-1,end,100))
            price=end
        minutes=[Candle(hours[-1].time+timedelta(minutes=i),price,price+1,price-1,price,10) for i in range(60)]
        return {'H1':hours,'M1':minutes,**{tf:aggregate(hours,tf) for tf in ('H4','D1','W1','MN1')}}

    def test_new_minute_does_not_replay_unchanged_hour_and_higher_contexts(self):
        series=self.series();at=series['H1'][-1].time+timedelta(minutes=20,seconds=30)
        old,parents=analyze_timeframes(series,'M1',asof=at)
        cached={'M1':old,**parents}
        new,new_parents=analyze_timeframes(series,'M1',asof=at+timedelta(minutes=1),cached=cached)
        self.assertTrue(new_parents['H1'].flows is parents['H1'].flows,'Unchanged H1 was replayed')
        self.assertTrue(new.summary_sources['H4'].flows is old.summary_sources['H4'].flows,'Unchanged context was replayed')
        fresh,fresh_parents=analyze_timeframes(series,'M1',asof=new.asof)
        for tf,a in {'M1':new,**new_parents}.items():
            b={'M1':fresh,**fresh_parents}[tf]
            self.assertEqual((a.flows,a.pairs,a.fvg,a.confirmed_bars),(b.flows,b.pairs,b.fvg,b.confirmed_bars),tf)

    def test_hour_close_and_changed_higher_confirmation_force_replay(self):
        series=self.series();at=series['H1'][-1].time+timedelta(minutes=59)
        main,parents=analyze_timeframes(series,'M1',asof=at)
        _,new=analyze_timeframes(series,'M1',asof=at+timedelta(minutes=1),cached={'M1':main,**parents})
        self.assertTrue(new['H1'].flows is not parents['H1'].flows,'Hour close reused stale state')

    def test_higher_confirmed_correction_invalidates_hour_even_when_hour_bars_match(self):
        series=self.series();at=series['H1'][-1].time+timedelta(minutes=20)
        main,parents=analyze_timeframes(series,'M1',asof=at)
        day=series['D1'][0]
        changed={**series,'D1':[replace(day,high=day.high+20),*series['D1'][1:]]}
        _,new=analyze_timeframes(changed,'M1',asof=at,cached={'M1':main,**parents})
        self.assertTrue(new['H1'].flows is not parents['H1'].flows,'Higher correction reused stale state')
        _,fresh=analyze_timeframes(changed,'M1',asof=at)
        self.assertEqual(new['H1'].flows,fresh['H1'].flows)
