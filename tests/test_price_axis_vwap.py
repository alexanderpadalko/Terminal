"""Finam tick-size metadata and session VWAP price markers on the right axis."""
import os
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM","offscreen")

from flowdesk.price_format import price_text, price_places, tick_from_asset
from flowdesk.chart_indicators import vwap_series
from flowdesk.indicator_paint import vwap_axis_levels, place_vwap_axis_tags
from flowdesk.exchange_time import exchange_clock
from flowdesk.market_data import Candle
from flowdesk.settings import IndicatorSettings


class TestFinamPriceTicks(unittest.TestCase):
    def test_finam_decimals_and_min_step_are_scaled_exactly(self):
        self.assertEqual(tick_from_asset({"min_step":1,"decimals":3}),Decimal("0.001"))
        self.assertEqual(tick_from_asset({"min_step":"25","decimals":2}),Decimal("0.25"))
        self.assertEqual(tick_from_asset({"min_step":5,"decimals":3}),Decimal("0.005"))
        self.assertEqual(tick_from_asset({"min_step":1,"decimals":0}),Decimal("1"))
        for data in ({},{ "min_step":0,"decimals":3},
                     {"min_step":1,"decimals":-1},
                     {"min_step":"nan","decimals":2},
                     {"min_step":1,"decimals":22}):
            self.assertIsNone(tick_from_asset(data))

    def test_price_tick_controls_precision_and_decimal_separator(self):
        self.assertEqual(price_places(.001),3)
        self.assertEqual(price_places(.25),2)
        self.assertEqual(price_places(.5),1)
        self.assertEqual(price_places(1),0)
        self.assertEqual(price_text(12.456,.001,decimal_comma=True),"12,456")
        self.assertEqual(price_text(12.449,.001,decimal_comma=True),"12,449")
        self.assertEqual(price_text(12.439,.001,decimal_comma=True),"12,439")
        self.assertEqual(price_text(12.456,.01,decimal_comma=True),"12,46")
        self.assertEqual(price_text(12.625,.25,decimal_comma=True),"12,75")
        self.assertEqual(price_text(12.625,.25),"12.75")
        self.assertEqual(price_text(12.2,1,decimal_comma=True),"12")
        self.assertEqual(price_text(None,.001,decimal_comma=True),"NaN")

    def test_read_only_asset_specification_get_and_cache(self):
        from flowdesk.finam_api import FinamAPI
        calls=[]
        def opener(req, timeout):
            calls.append((req.get_method(),req.full_url))
            if req.full_url.endswith("/v1/sessions"):
                body={"token":"test_access"}
            elif req.full_url.endswith("/v1/sessions/details"):
                body={"md_permissions":[]}
            elif req.full_url.endswith("/v1/assets/CRZ6%40RTSX"):
                body={"ticker":"CRZ6","mic":"RTSX","min_step":1,"decimals":3}
            else:
                raise AssertionError("unexpected endpoint "+req.full_url)
            class Response:
                def __enter__(self):return self
                def __exit__(self,*args):return None
                def read(self):return json.dumps(body).encode()
            return Response()
        api=FinamAPI(opener=opener,secret_reader=lambda:"dummy_secret")
        self.assertEqual(api.asset_tick_size("CRZ6@RTSX"),.001)
        self.assertEqual(api.asset_tick_size("CRZ6@RTSX"),.001)
        urls=[(m,u) for m,u in calls if "/v1/assets/" in u]
        self.assertEqual(len(urls),1)
        self.assertEqual(urls[0][0],"GET")
        self.assertFalse(any("/bars" in url for _,url in calls))


class TestVwapAxisLevels(unittest.TestCase):
    @staticmethod
    def make_chart(previous_enabled=True):
        start=datetime(2026,11,12,19,0,tzinfo=timezone.utc)
        day2=datetime(2026,11,13,7,0,tzinfo=timezone.utc)
        times=[start,start+timedelta(minutes=5),day2,day2+timedelta(minutes=5)]
        prices=[12.456,12.458,12.463,12.465]
        bars=[Candle(t,p,p,p,p,1) for t,p in zip(times,prices)]
        cfg=IndicatorSettings().changed(vwapEnabled=True,vwapShowPrevious=previous_enabled,
                                        vwapAnchor="Session",vwapSource="close")
        clock=exchange_clock("RTSX","CRZ6")
        current,previous=vwap_series(bars,"Session","close",exchange=clock)
        return SimpleNamespace(indicator_settings=cfg,
            analysis=SimpleNamespace(timeframe="M5",candles=bars),
            exchange=clock,vwap=current,vwap_previous=previous)

    def test_current_and_previous_vwap_values_are_from_same_series(self):
        chart=self.make_chart()
        levels=vwap_axis_levels(chart)
        self.assertEqual([label for label,_,_ in levels],["VWAP","VWAP-1"])
        self.assertAlmostEqual(levels[0][1],12.464)
        self.assertAlmostEqual(levels[1][1],12.457)
        self.assertEqual([price_text(value,.001,decimal_comma=True) for _,value,_ in levels],
                         ["12,464","12,457"])

    def test_visibility_follows_settings_and_timeframe(self):
        chart=self.make_chart(previous_enabled=False)
        self.assertEqual(len(vwap_axis_levels(chart)),1)
        chart.indicator_settings=chart.indicator_settings.changed(vwapEnabled=False)
        self.assertEqual(vwap_axis_levels(chart),[])
        chart.indicator_settings=chart.indicator_settings.changed(vwapEnabled=True)
        chart.analysis.timeframe="H1"
        self.assertEqual(vwap_axis_levels(chart),[])

    def test_previous_label_needs_a_completed_session(self):
        chart=self.make_chart()
        chart.vwap_previous=[]
        self.assertEqual([tag[0] for tag in vwap_axis_levels(chart)],["VWAP"])

    def test_price_tags_stay_inside_axis_and_avoid_current_price(self):
        chart=self.make_chart()
        tags=place_vwap_axis_tags(vwap_axis_levels(chart),
            lambda price:120+(price-12.457)*200,
            50,350,[(122,20)])
        self.assertEqual(len(tags),2)
        self.assertTrue(all(64<=row[3]<=336 for row in tags))
        self.assertGreaterEqual(abs(tags[0][3]-tags[1][3]),31)
        self.assertTrue(all(abs(row[3]-122)>=27 for row in tags))
        self.assertEqual(place_vwap_axis_tags(vwap_axis_levels(chart),
            lambda price:900,50,350),[])


class TestChartPriceRuler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app=QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_chart_price_format_and_vwap_are_coupled_to_instrument_tick(self):
        from flowdesk.gui import CandleChart
        from flowdesk.engine import analyze
        chart=CandleChart()
        try:
            fixture=TestVwapAxisLevels.make_chart()
            chart.set_settings(fixture.indicator_settings)
            chart.set_exchange("RTSX","CRZ6")
            chart.set_analysis(analyze(fixture.analysis.candles,"M5"))
            self.assertEqual(chart.format_price(12.456),"12,46")
            chart.set_price_tick(.001)
            self.assertEqual(chart.format_price(12.456),"12,456")
            self.assertEqual(chart.format_price(12.449),"12,449")
            self.assertEqual([name for name,_,_ in vwap_axis_levels(chart)],["VWAP","VWAP-1"])
            chart.resize(1120,720)
            chart.grab()  # Real Qt rendering, including right-hand VWAP tags.
            chart.set_price_tick(None)
            self.assertEqual(chart.format_price(12.456),"12,46")
        finally:
            chart.close()

    def test_finam_worker_passes_metadata_to_gui_without_candle_http(self):
        from flowdesk.finam_gui import FinamLoadWorker
        from flowdesk.engine import analyze
        fixture=TestVwapAxisLevels.make_chart()
        class API:
            def asset_tick_size(self,symbol):
                self_symbol.symbol=symbol
                return .001
        class Service:
            api=API()
            def view(self,ticker,mic,timeframe,**kwargs):
                self_symbol.allow_http=kwargs["allow_http"]
                return (analyze(fixture.analysis.candles,"M5"),{},
                        {"symbol":ticker,"board":mic,"tf":timeframe,
                         "warning":"","asof":fixture.analysis.candles[-1].time.isoformat()})
        self_symbol=SimpleNamespace()
        worker=FinamLoadWorker(Service(),"CRZ6","RTSX","M5",
                               fixture.indicator_settings,allow_http=False)
        results=[]
        worker.loaded.connect(lambda analysis,parents,info:results.append(info))
        worker.run()
        self.assertEqual(self_symbol.symbol,"CRZ6@RTSX")
        self.assertFalse(self_symbol.allow_http)
        self.assertEqual(len(results),1)
        self.assertEqual(results[0]["price_tick_size"],.001)


if __name__=="__main__":
    unittest.main()
