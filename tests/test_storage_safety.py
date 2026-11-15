"""WAL safety, atomic snapshots, non-destructive recovery and user settings."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import time
import unittest

from flowdesk.finam_market import FinamMarketService
from flowdesk.market_services import MarketServices
from flowdesk.market_data import Candle
from flowdesk.settings import SettingsStore, IndicatorSettings, ApplicationSettings
from flowdesk.storage_safety import (
    backup_if_due, create_backup, recover_to_new_file, snapshots,
    snapshot_directory, verify_database,
)


NOW=datetime(2026,11,15,13,0,tzinfo=timezone.utc)


class TestSQLiteSafety(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=self.root/"finam_candles_v1.sqlite3"
        self.service=FinamMarketService(self.path)
        self.symbol="SBER@MISX"
        self.candle=Candle(NOW,100,105,95,103,1000)
        self.service._upsert(self.symbol,"H1",[self.candle])

    def test_verified_backup_includes_committed_uncheckpointed_wal(self):
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)",
                           (self.symbol,"D1",(NOW+timedelta(days=1)).isoformat(),
                            100,105,95,103,1000,None))
            writer.commit()
            # Hold source open: the WAL may contain committed data that was
            # not copied back to the main database file.
            data=create_backup(self.path,now=NOW)
            snap=Path(data["path"])
            self.assertEqual(data["candles"],2)
            self.assertEqual(verify_database(snap),2)
            self.assertEqual(len(snapshots(self.path)),1)
            self.assertTrue(snap.parent==snapshot_directory(self.path))
            with closing(sqlite3.connect(snap)) as read:
                self.assertEqual(read.execute("SELECT COUNT(*) FROM candles").fetchone()[0],2)

    def test_daily_rotation_keeps_only_three_completed_snapshots(self):
        days=[NOW+timedelta(days=n) for n in range(5)]
        for day in days:
            result=backup_if_due(self.path,now=day)
            self.assertTrue(result["created"])
            self.assertFalse(backup_if_due(self.path,now=day)["created"])
        old=snapshots(self.path)
        self.assertEqual(len(old),3)
        self.assertTrue(all(day.strftime("%Y%m%d") in path.name
                            for day,path in zip(reversed(days[-3:]),old)))
        self.assertEqual(verify_database(old[0]),1)

    def test_failed_backup_keeps_previous_snapshots_and_bad_bytes(self):
        good=Path(create_backup(self.path,now=NOW)["path"])
        self.path.write_bytes(b"not a SQLite database")
        before=self.path.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            create_backup(self.path,now=NOW+timedelta(days=1))
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(tuple(snapshots(self.path)),(good,))
        self.assertEqual(verify_database(good),1)

    def test_recovery_creates_new_file_never_overwrites_original(self):
        snap=Path(create_backup(self.path,now=NOW)["path"])
        self.service._upsert(self.symbol,"H1",[
            Candle(NOW+timedelta(hours=1),100,106,95,104,1002)])
        self.assertEqual(verify_database(self.path),2)
        output=self.root/"restore-to-new.sqlite3"
        recovered=recover_to_new_file(snap,output)
        self.assertEqual(recovered["candles"],1)
        self.assertEqual(verify_database(output),1)
        self.assertEqual(verify_database(self.path),2)
        with self.assertRaises(FileExistsError):
            recover_to_new_file(snap,output)
        with self.assertRaises(FileExistsError):
            recover_to_new_file(snap,self.path)
        self.assertEqual(verify_database(output),1)
        self.assertEqual(verify_database(self.path),2)

    def test_invalid_snapshot_and_missing_source_are_rejected(self):
        invalid=self.root/"fake.sqlite3"
        invalid.write_bytes(b"broken")
        with self.assertRaises(sqlite3.DatabaseError):
            verify_database(invalid)
        with self.assertRaises(sqlite3.DatabaseError):
            recover_to_new_file(invalid,self.root/"new.sqlite3")
        self.assertFalse((self.root/"new.sqlite3").exists())
        with self.assertRaises(FileNotFoundError):
            create_backup(self.root/"absent.sqlite3")

    def test_async_snapshot_does_not_open_trading_api_or_touch_crypto(self):
        services=MarketServices(finam=self.service,directory=self.root)
        self.assertTrue(services.backup_if_due_async())
        deadline=time.monotonic()+15
        while services._backup_active and time.monotonic()<deadline:
            time.sleep(.02)
        self.assertFalse(services._backup_active)
        self.assertEqual(len(snapshots(self.path)),1)
        self.assertFalse((self.root/"binance_candles_v1.sqlite3").exists())

    def test_corrupted_preferences_are_preserved_before_next_save(self):
        cfg=self.root/"settings_v211.json"
        original=b'{"private custom preferences":broken'
        cfg.write_bytes(original)
        store=SettingsStore(cfg)
        indicator,application=store.read()
        self.assertTrue(store.warning)
        store.write(indicator,application)
        self.assertFalse(store.warning)
        saved=list(self.root.glob("settings_v211.json.invalid-*"))
        self.assertEqual(len(saved),1)
        self.assertEqual(saved[0].read_bytes(),original)
        loaded,appearance=SettingsStore(cfg).read()
        self.assertEqual(loaded.as_dict(),IndicatorSettings().as_dict())
        self.assertIsInstance(appearance,ApplicationSettings)
        self.assertEqual(json.loads(cfg.read_text(encoding="utf-8"))["schema"],1)


if __name__=="__main__":
    unittest.main()
