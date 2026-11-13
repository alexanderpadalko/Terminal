"""Local diagnostics are durable, rotating and contain no private API data."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest.mock import patch

from flowdesk import diagnostics_log as diag


class TestPrivateDiagnostics(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log=Path(self.temp.name)/"logs"/"diagnostics.log"

    def read(self):
        return self.log.read_text(encoding="utf-8")

    def test_expiry_records_only_known_fields_not_tokens_or_payload(self):
        contract=SimpleNamespace(display="NQZ2026")
        item=SimpleNamespace(
            symbol="NQZ26@XCME",verified=True,
            source="экспирация не подтверждена; автопереход приостановлен; "
                   "Finam GetAsset: HTTP 403; CME NQ недоступен; "
                   "Authorization Bearer secret_private_jwt; "
                   "account_id=private_account; https://private.example/key")
        self.assertTrue(diag.log_contract(contract,item,path=self.log))
        text=self.read()
        for token in ("secret_private_jwt","private_account",
                      "private.example","Bearer","Authorization",
                      "https://"):
            self.assertNotIn(token,text)
        self.assertIn("contract=NQZ2026",text)
        self.assertIn("symbol=NQZ26@XCME",text)
        self.assertIn("status=unknown_expiry",text)
        self.assertIn("http=http_403",text)
        self.assertIn("calendar=unavailable",text)
        self.assertIn("source=cme",text)

    def test_known_date_and_valid_source_are_recorded_not_raw_payload(self):
        item=SimpleNamespace(symbol="NQZ26@XCME",verified=True,
                             source="действует до 2026-12-18 · Finam GetAsset")
        self.assertTrue(diag.log_contract(SimpleNamespace(display="NQZ2026"),
                                          item,path=self.log))
        self.assertIn("expiry=2026-12-18",self.read())
        self.assertIn("source=finam_asset",self.read())

    def test_chart_observation_and_error_are_safe_allowlisted(self):
        self.assertTrue(diag.log_snapshot(symbol="NQZ26@XCME",timeframe="H1",
                                         asof="2026-11-13T17:48:00+00:00",
                                         delay_seconds=0,path=self.log))
        self.assertTrue(diag.log_event(
            "stream_unavailable",symbol="NQZ26@XCME",
            error="HTTP 429 Bearer secret_account=private",path=self.log))
        self.assertFalse(diag.log_event("arbitrary private=secret",path=self.log))
        self.assertTrue(diag.log_snapshot(
            symbol="Bearer secret",timeframe="invalid",
            asof="password never serialize",delay_seconds="secret",path=self.log))
        text=self.read()
        self.assertIn("asof_utc=2026-11-13T17:48:00+00:00",text)
        self.assertIn("permission_delay_min=0",text)
        self.assertIn("http=http_429",text)
        self.assertIn("symbol=-",text)
        self.assertNotIn("private",text)
        self.assertNotIn("Bearer",text)
        self.assertNotIn("password",text)

    def test_rotates_and_never_leaks_source_messages(self):
        with patch.object(diag,"_MAX_BYTES",210),patch.object(diag,"_BACKUPS",2):
            for _ in range(70):
                self.assertTrue(diag.log_event("catalog_complete",
                                               error="password_xyz",path=self.log))
        files=list(self.log.parent.glob("diagnostics.log*"))
        self.assertGreaterEqual(len(files),2)
        self.assertLessEqual(len(files),3)
        self.assertTrue(all(f.stat().st_size<=210 for f in files))
        self.assertTrue(all("password_xyz" not in f.read_text(encoding="utf-8")
                            for f in files))
        if os.name != "nt":
            self.assertEqual(self.log.stat().st_mode & 0o077,0)


if __name__=="__main__":
    unittest.main()
