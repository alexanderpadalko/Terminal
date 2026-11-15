"""Local diagnostics are durable, rotating and contain no private API data."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest
from unittest.mock import patch
import json
import socket
import ssl
from urllib.error import URLError
from flowdesk.finam_api import FinamAPIError

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

    def test_structured_http_catalog_failure_retains_only_safe_context(self):
        error=FinamAPIError("Bearer secret_token https://private.example?account_id=private_account",
                            http_status=429,retry_after=60)
        error.diagnostic_operation="catalog"
        error.diagnostic_page=17
        diag.log_event("catalog_unavailable",error=error,stage="catalog",elapsed_ms=1234,path=self.log)
        text=self.read()
        for field in ("http=http_429","kind=http","error_type=FinamAPIError",
                      "stage=catalog","operation=catalog","page=17","retry_after_s=60","elapsed_ms=1234"):
            self.assertIn(field,text)
        for private in ("secret_token","private.example","private_account","Bearer","account_id"):
            self.assertNotIn(private,text)

    def test_wrapped_network_causes_are_preserved_without_exception_messages(self):
        cases=((TimeoutError("private"),"timeout","TimeoutError"),
               (socket.gaierror(-2,"private"),"dns","gaierror"),
               (ssl.SSLError("private"),"tls","SSLError"),
               (ConnectionResetError("private"),"connection","ConnectionResetError"))
        for cause,kind,name in cases:
            with self.subTest(kind=kind):
                try:
                    try:raise URLError(cause)
                    except URLError:raise FinamAPIError("private wrapper") from None
                except FinamAPIError as error:
                    diag.log_event("catalog_unavailable",error=error,path=self.log)
                line=self.read().splitlines()[-1]
                self.assertIn("kind="+kind,line)
                self.assertIn("error_type="+name,line)
                self.assertIn("http=none",line)
                self.assertNotIn("private",line)

    def test_untrusted_error_names_context_and_string_conversion_are_not_logged(self):
        class PrivateExceptionName(Exception):
            def __str__(self):raise AssertionError("Do not stringify exceptions")
        error=PrivateExceptionName()
        error.diagnostic_operation="private_operation"
        error.diagnostic_page="private_page"
        error.retry_after=float("inf")
        error.__cause__=error
        diag.log_event("catalog_unavailable",error=error,stage="private_stage",
                       elapsed_ms=float("nan"),path=self.log)
        text=self.read()
        self.assertIn("kind=unknown",text)
        self.assertIn("error_type=other",text)
        self.assertNotIn("private",text.lower())
        self.assertNotIn("PrivateExceptionName",text)
        self.assertNotIn("nan",text)
        self.assertNotIn("inf",text.lower().replace("info",""))

    def test_json_errors_and_fallback_outcomes_do_not_copy_payloads(self):
        error=json.JSONDecodeError("private_message","private_payload",0)
        diag.log_event("catalog_unavailable",error=error,stage="catalog",path=self.log)
        diag.log_event("catalog_complete",stage="catalog_refresh",outcome="fallback",
                       elapsed_ms=15000,path=self.log)
        text=self.read()
        self.assertIn("kind=invalid_response",text)
        self.assertIn("error_type=JSONDecodeError",text)
        self.assertIn("outcome=fallback",text)
        self.assertNotIn("private",text)


if __name__=="__main__":
    unittest.main()
