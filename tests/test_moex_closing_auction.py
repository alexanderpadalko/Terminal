"""Closing-auction M1/M5 equities bars cannot influence Finam signals."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from flowdesk.finam_auction import (
    exclude_stock_closing_auction, is_stock_closing_auction_bar,
)
from flowdesk.finam_market import FinamMarketService
from flowdesk.market_data import Candle, MOSCOW


def bar(day, hour, minute, *, price=100, volume=1):
    stamp=datetime.fromisoformat(day).replace(
        hour=hour, minute=minute, tzinfo=MOSCOW)
    return Candle(stamp, price, price+1, price-1, price, volume)


class OfflineAPI:
    def market_delay_minutes(self, mic):
        return 0
    def bars(self, *_args):
        raise AssertionError("REST must not be called in this test")


class TestMoexStockClosingAuction(unittest.TestCase):
    def test_exact_moscow_window_and_effective_date_for_stocks_only(self):
        date="2026-11-13"
        for minute in range(50,60):
            self.assertEqual(is_stock_closing_auction_bar(
                bar(date,18,minute),"M1","MISX"),minute>=55)
        self.assertFalse(is_stock_closing_auction_bar(bar(date,18,54),"M5","MISX"))
        self.assertTrue(is_stock_closing_auction_bar(bar(date,18,55),"M5","MISX"))
        self.assertFalse(is_stock_closing_auction_bar(bar(date,19,0),"M5","MISX"))
        self.assertFalse(is_stock_closing_auction_bar(bar("2026-03-20",18,55),"M5","MISX"))
        self.assertTrue(is_stock_closing_auction_bar(bar("2026-03-23",18,55),"M5","MISX"))
        self.assertFalse(is_stock_closing_auction_bar(bar("2026-11-14",18,55),"M5","MISX"))
        for mic in ("RTSX","XCME","IFEU","XNYM","XNGS"):
            self.assertFalse(is_stock_closing_auction_bar(bar(date,18,55),"M5",mic))
        self.assertFalse(is_stock_closing_auction_bar(bar(date,18,55),"H1","MISX"))

    def test_only_m1_m5_filtered_no_mutation_of_native_higher(self):
        rows={
            "M1":[bar("2026-11-13",18,m,price=m) for m in range(52,60)]
                  +[bar("2026-11-13",19,0)],
            "M5":[bar("2026-11-13",18,50,price=100),
                  bar("2026-11-13",18,55,price=999),
                  bar("2026-11-13",19,0,price=102)],
            "H1":[bar("2026-11-13",18,0,price=101)],
            "D1":[bar("2026-11-13",0,0,price=102)],
        }
        cleaned=exclude_stock_closing_auction(rows,"MISX")
        self.assertEqual([b.time.minute for b in cleaned["M1"]],[52,53,54,0])
        self.assertEqual([b.time.hour for b in cleaned["M1"]],[18,18,18,19])
        self.assertEqual([b.open for b in cleaned["M5"]],[100,102])
        self.assertEqual(len(rows["M5"]),3)
        self.assertIs(cleaned["H1"],rows["H1"])
        self.assertIs(cleaned["D1"],rows["D1"])
        self.assertIs(exclude_stock_closing_auction(rows,"RTSX"),rows)

    def test_filtered_m5_reaches_analysis_and_raw_cache_survives(self):
        with tempfile.TemporaryDirectory() as folder:
            now=lambda:datetime(2026,11,13,20,tzinfo=timezone.utc)
            service=FinamMarketService(Path(folder)/"finam.sqlite3",
                                       api=OfflineAPI(),clock=now)
            # 40 valid 5m bars plus one extreme auction outlier at 18:55.
            source=[bar("2026-11-13",16+i//12,(i%12)*5,
                        price=110+i/10) for i in range(40)]
            suspect=bar("2026-11-13",18,55,price=999999,volume=90000)
            source=[c for c in source if c.time!=suspect.time]+[suspect]
            service._upsert("LKOH@MISX","M5",source)
            main,parents,info=service.view("LKOH","MISX","M5",
                                           allow_http=False)
            self.assertTrue(main.candles)
            self.assertFalse(any(c.time==suspect.time for c in main.candles))
            self.assertLess(max(c.high for c in main.candles),200)
            self.assertEqual(info["symbol"],"LKOH")
            self.assertTrue(any(c.time==suspect.time for c in
                                service._series("LKOH@MISX")["M5"]))
            again,_,_=service.view("LKOH","MISX","M5",allow_http=False)
            self.assertEqual(main.candles,again.candles)
            # Futures must retain the same timestamp and value.
            service._upsert("SIZ26@RTSX","M5",source)
            future,_,_=service.view("SIZ26","RTSX","M5",allow_http=False)
            self.assertTrue(any(c.time==suspect.time for c in future.candles))


if __name__=="__main__":
    unittest.main()
