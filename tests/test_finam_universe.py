"""The visible FlowDesk universe is the screenshot shortlist plus ten ranked stocks."""
import json
import unittest
from pathlib import Path

from flowdesk.finam_universe import (
    US_CONTRACTS, RU_CONTRACTS, US_FUTURES, RU_FUTURES, RU_STOCKS, CRYPTO,
    compose_universe, cached_top10, match_contract, is_in_universe, Item
)


class FakeWatchlist:
    def __init__(self, value):
        self.value=value
    def cached(self):
        return self.value


class TestCuratedFinamUniverse(unittest.TestCase):
    def test_exact_screenshot_shortlist_and_unchanged_stock_ranking(self):
        universe,status=compose_universe()
        self.assertEqual(tuple(universe),(US_FUTURES,RU_STOCKS,RU_FUTURES,CRYPTO))
        self.assertEqual([x.label for x in universe[US_FUTURES]],
                         ["GCZ2026","SIZ2026","NGZ2026","BRNF2027"])
        self.assertEqual([x.label for x in universe[RU_FUTURES]],
                         ["SIZ2026","CRZ2026","EUZ2026","USDRUB.P",
                          "CNYRUB.P","EURRUB.P","MXZ2026"])
        self.assertEqual([x.label for x in universe[CRYPTO]],["BTCUSDT.P"])
        self.assertEqual(len(universe[RU_STOCKS]),10)
        self.assertEqual(sum(map(len,universe.values())),22)
        self.assertFalse(is_in_universe("ETHUSDT@BINANCE",universe))
        self.assertTrue(status)

    def test_futures_do_not_invent_venue_or_tenor(self):
        gold=US_CONTRACTS[0]
        rows=[
            {"ticker":"GCZ6","mic":"RTSX","symbol":"GCZ6@RTSX"},
            {"ticker":"GCH27","mic":"XCEC","symbol":"GCH27@XCEC"},
            {"ticker":"GCZ26","mic":"XCEC","symbol":"GCZ26@XCEC","is_archived":True},
        ]
        self.assertEqual(match_contract(gold,rows),"")
        self.assertEqual(match_contract(gold,rows+[{
            "ticker":"GCZ6","mic":"XCEC","symbol":"GCZ6@XCEC","type":"TYPE_FUTURE",
        }]),"GCZ6@XCEC")
        self.assertEqual(match_contract(gold,rows+[
            {"ticker":"GCZ6","mic":"XCEC","symbol":"GCZ6@XCEC"},
            {"ticker":"GCZ26","mic":"XCEC","symbol":"GCZ26@XCEC"}
        ]),"")  # ambiguous: never guess which Finam instrument

    def test_distinguish_silver_SI_on_comex_and_ruble_SI_on_rtsx(self):
        us=[x for x in US_CONTRACTS if x.display=="SIZ2026"][0]
        ru=[x for x in RU_CONTRACTS if x.display=="SIZ2026"][0]
        row_us={"ticker":"SIZ6","mic":"XCEC","symbol":"SIZ6@XCEC"}
        row_ru={"ticker":"SiZ6","mic":"RTSX","symbol":"SiZ6@RTSX"}
        self.assertEqual(match_contract(us,[row_us,row_ru]),"SIZ6@XCEC")
        self.assertEqual(match_contract(ru,[row_us,row_ru]),"SIZ6@RTSX")

    def test_russian_futures_aliases_include_mx_not_only_mix(self):
        mx=[x for x in RU_CONTRACTS if x.display=="MXZ2026"][0]
        result=match_contract(mx,[{"ticker":"MXZ6","mic":"RTSX","symbol":"MXZ6@RTSX"}])
        self.assertEqual(result,"MXZ6@RTSX")

    def test_perpetuals_resolve_by_real_underlying_not_tv_label(self):
        contracts=[c for c in RU_CONTRACTS if c.display.endswith(".P")]
        matches=[{"ticker":"USDRUBF","mic":"RTSX","symbol":"USDRUBF@RTSX"},
                 {"ticker":"CNYRUBF","mic":"RTSX","symbol":"CNYRUBF@RTSX"},
                 {"ticker":"EURRUBF","mic":"RTSX","symbol":"EURRUBF@RTSX"}]
        self.assertEqual([match_contract(c,matches) for c in contracts],
                         [r["symbol"] for r in matches])

    def test_previous_confirmed_twenty_session_rank_is_trimmed_to_top_ten(self):
        stock_rows=[{"symbol":f"S{i:02d}","board":"TQBR","kind":"stock",
                     "name":f"Stock {i}","turnover":1000-i}
                    for i in range(40)]
        value={"slot":"2026-11-10",
               "window":[f"2026-11-{i:02d}" for i in range(1,21)],
               "instruments":stock_rows}
        rows,source=cached_top10(watchlist=FakeWatchlist(value))
        self.assertEqual(len(rows),10)
        self.assertEqual(rows[0].symbol,"S00@MISX")
        self.assertEqual(rows[-1].symbol,"S09@MISX")
        self.assertIn("2026-11-20",source)

    def test_seed_is_labelled_unconfirmed_and_never_rated_as_fresh(self):
        rows,status=cached_top10()
        self.assertEqual(len(rows),10)
        self.assertTrue(all(row.symbol.endswith("@MISX") for row in rows))
        self.assertNotIn("сейчас",status)

    def test_top_ten_uses_saved_rank_not_alphabetical_tickers(self):
        # Keep the complete 20-session archive intact. The curated list
        # must take the *ranked first ten*, without re-sorting by name.
        rows=[{"symbol":f"Z{i:02d}","board":"TQBR","kind":"stock",
               "name":f"Stock {i}","turnover":1000-i}
              for i in range(40)]
        saved={"slot":"2026-11-10",
               "window":["2026-11-10"],"instruments":rows}
        selected,status=cached_top10(watchlist=FakeWatchlist(saved))
        self.assertEqual([r.label for r in selected],
                         [f"Z{i:02d}" for i in range(10)])
        self.assertNotIn("Z10", [r.label for r in selected])
        self.assertIn("2026-11-10", status)
        # The full offline saved ranking is untouched.
        self.assertEqual(len(saved["instruments"]),40)

    def test_catalog_only_enables_verified_future(self):
        assets=[{"ticker":"GCZ6","mic":"XCEC","symbol":"GCZ6@XCEC"}]
        groups,_=compose_universe(assets)
        self.assertTrue(is_in_universe("GCZ6@XCEC",groups))
        self.assertFalse(is_in_universe("GCH27@XCEC",groups))
        self.assertFalse(is_in_universe("AAPL@XNGS",groups))

    def test_normalized_currency_symbols_keep_exact_contract_and_venue(self):
        rows=[Item("SIZ2026","USD/RUB",RU_FUTURES,"SiZ6@RTSX",True),
              Item("EUZ2026","EUR/RUB",RU_FUTURES,"EuZ6@RTSX",True),
              Item("CRZ2026","CNY/RUB",RU_FUTURES,"CRZ6@RTSX",False)]
        groups={RU_FUTURES:rows}
        for row in rows[:2]:
            for symbol in (row.symbol,row.symbol.upper(),row.symbol.lower()):
                with self.subTest(symbol=symbol):
                    self.assertTrue(is_in_universe(symbol,groups))
        for symbol in ("SIH7@RTSX","EUH7@RTSX","SIZ26@RTSX",
                       "SIZ6@XCEC","EUZ6@XCME","CRZ6@RTSX",""):
            with self.subTest(rejected=symbol):
                self.assertFalse(is_in_universe(symbol,groups))
        self.assertEqual([row.symbol for row in rows[:2]],
                         ["SiZ6@RTSX","EuZ6@RTSX"])


if __name__=="__main__":
    unittest.main()
