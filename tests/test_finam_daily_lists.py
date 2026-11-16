"""Daily cached US/RU Finam futures lists; read-only, no live credentials."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import tempfile
import unittest

from flowdesk.finam_futures_cache import FuturesListCache
from flowdesk.finam_universe import US_FUTURES, RU_FUTURES, US_CONTRACTS, RU_CONTRACTS, Item
from flowdesk.watchlist import slot_date
from flowdesk.finam_gui import FinamWindow


class DailyFuturesListsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache=FuturesListCache(Path(self.tmp.name))
        self.now=datetime(2026,11,16,9,0,tzinfo=timezone.utc)
        self.us=Item(US_CONTRACTS[0].display,US_CONTRACTS[0].name,
                     US_FUTURES,"GCZ6@XCEC",True,
                     "действует до 2026-12-18 · Finam GetAsset")
        self.ru=Item(RU_CONTRACTS[0].display,RU_CONTRACTS[0].name,
                     RU_FUTURES,"SiZ6@RTSX",True,
                     "действует до 2026-12-17 · Finam GetAsset")
        self.rows={US_FUTURES+":"+US_CONTRACTS[0].display:self.us,
                   RU_FUTURES+":"+RU_CONTRACTS[0].display:self.ru}

    def test_two_separate_files_and_independent_cached_lists(self):
        self.cache.save(self.rows,now=self.now)
        folder=Path(self.tmp.name)
        us=folder/"finam_futures_us_v1.json"
        ru=folder/"finam_futures_ru_v1.json"
        self.assertTrue(us.is_file())
        self.assertTrue(ru.is_file())
        us_rows=json.loads(us.read_text(encoding="utf-8"))["contracts"]
        ru_rows=json.loads(ru.read_text(encoding="utf-8"))["contracts"]
        self.assertEqual(list(us_rows),[US_FUTURES+":"+US_CONTRACTS[0].display])
        self.assertEqual(list(ru_rows),[RU_FUTURES+":"+RU_CONTRACTS[0].display])
        rows=self.cache.load(now=self.now)
        self.assertEqual(rows[US_FUTURES+":"+US_CONTRACTS[0].display].symbol,"GCZ6@XCEC")
        self.assertEqual(rows[RU_FUTURES+":"+RU_CONTRACTS[0].display].symbol,"SiZ6@RTSX")
        self.assertIn("сохранённый список",rows[US_FUTURES+":"+US_CONTRACTS[0].display].source)

    def test_outage_does_not_erase_existing_verified_entries(self):
        self.cache.save(self.rows,now=self.now)
        self.cache.save({},now=self.now+timedelta(hours=2))
        self.assertEqual(len(self.cache.load(now=self.now)),2)

    def test_cache_never_reactivates_contract_on_rollover_day(self):
        self.cache.save(self.rows,now=self.now)
        # US market roll day Dec 17, Russian market Dec 16.
        after=datetime(2026,12,17,12,tzinfo=timezone.utc)
        self.assertEqual(self.cache.load(now=after),{})

    def test_unknown_expiry_and_wrong_market_are_not_restored(self):
        unknown=Item(US_CONTRACTS[1].display,US_CONTRACTS[1].name,
                     US_FUTURES,"SIZ6@XCEC",True,
                     "экспирация не подтверждена; автопереход приостановлен")
        wrong=Item(US_CONTRACTS[0].display,US_CONTRACTS[0].name,
                   US_FUTURES,"GCZ6@RTSX",True,self.us.source)
        self.cache.save({US_FUTURES+":"+US_CONTRACTS[1].display:unknown,
                         US_FUTURES+":"+US_CONTRACTS[0].display:wrong},
                        now=self.now)
        self.assertEqual(self.cache.load(now=self.now),{})

    def test_truncated_cache_does_not_take_down_other_market(self):
        self.cache.save(self.rows,now=self.now)
        (Path(self.tmp.name)/"finam_futures_us_v1.json").write_text("{broken",encoding="utf-8")
        restored=self.cache.load(now=self.now)
        self.assertEqual(list(restored),[RU_FUTURES+":"+RU_CONTRACTS[0].display])

    def test_catalog_daily_refresh_only_on_new_moscow_slot(self):
        day=slot_date(self.now)
        stub=SimpleNamespace(closing=False,_catalog_success_slot=day,
                             _refresh_catalog=Mock())
        with patch("flowdesk.finam_gui.read_secret",return_value="token"), \
             patch("flowdesk.finam_gui.datetime") as utc:
            utc.now.return_value=self.now
            FinamWindow._daily_catalog_refresh(stub)
            stub._refresh_catalog.assert_not_called()
            stub._catalog_success_slot=day-timedelta(days=1)
            FinamWindow._daily_catalog_refresh(stub)
            stub._refresh_catalog.assert_called_once_with()

    def test_cache_verified_flag_must_be_a_boolean(self):
        from dataclasses import asdict
        for value in (False,"false","true",1,[],{}):
            with self.subTest(value=value):
                record={**asdict(self.us),"verified":value}
                self.assertIsNone(self.cache._safe_item(US_CONTRACTS[0],record,
                                                       self.now.date()))

    def test_term_contract_cannot_become_perpetual_via_source_text(self):
        from dataclasses import asdict
        record={**asdict(self.us),"source":"вечный контракт, без ролловера"}
        self.assertIsNone(self.cache._safe_item(US_CONTRACTS[0],record,
                                               self.now.date()))

    def test_cache_expiry_requires_a_finam_source(self):
        from dataclasses import asdict
        for source in ("MOEX LASTTRADEDATE","CME calendar","ICE Brent","данные источника"):
            with self.subTest(source=source):
                record={**asdict(self.us),"source":"действует до 2026-12-18 · "+source}
                self.assertIsNone(self.cache._safe_item(US_CONTRACTS[0],record,
                                                       self.now.date()))
        for source in ("Finam GetAsset","Finam каталог"):
            record={**asdict(self.us),"source":"действует до 2026-12-18 · "+source}
            self.assertIsNotNone(self.cache._safe_item(US_CONTRACTS[0],record,
                                                      self.now.date()))

    def test_perpetual_cache_uses_contract_definition(self):
        contract=RU_CONTRACTS[3]
        record={"symbol":"USDRUBF@RTSX","label":contract.display,"verified":True,
                "source":"вечный контракт, без ролловера"}
        self.assertIsNotNone(self.cache._safe_item(contract,record,self.now.date()))
        record["source"]="действует до 2026-12-18 · Finam GetAsset"
        self.assertIsNone(self.cache._safe_item(contract,record,self.now.date()))

    def test_rollover_cache_uses_actual_catalog_mic_local_date(self):
        from dataclasses import replace
        contract=US_CONTRACTS[0]  # GC may be returned on XCEC or XCME.
        row=Item(contract.display,contract.name,contract.group,"GCZ6@XCME",True,
                 "действует до 2026-12-18 · Finam GetAsset")
        key=contract.group+":"+contract.display
        boundary=datetime(2026,12,17,5,30,tzinfo=timezone.utc)
        self.cache.save({key:row},now=boundary)
        # Chicago is still Dec 16; New York is already Dec 17 (roll day).
        self.assertIn(key,self.cache.load(now=boundary))
        from dataclasses import asdict
        self.assertIsNone(self.cache._safe_item_at(contract,
            asdict(replace(row,symbol="GCZ6@XCEC")),boundary))


if __name__=="__main__":
    unittest.main()
