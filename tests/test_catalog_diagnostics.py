"""Real failure propagation to private logs; no live credentials/network."""
from datetime import datetime,timezone
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from flowdesk import diagnostics_log as diag
from flowdesk.finam_api import FinamAPI,FinamAPIError
from flowdesk.finam_requests import RequestCoordinator
from flowdesk.finam_catalog_worker import FinamCatalogWorker
from flowdesk.moex_reference import refresh_top20


class Response:
    def __init__(self,value):self.value=value
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self):return json.dumps(self.value).encode()


class TestCatalogDiagnostics(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.log=Path(self.temp.name)/"diagnostics.log"
        self.logging=patch.object(diag,"LOG_PATH",self.log)
        self.logging.start();self.addCleanup(self.logging.stop)

    def read(self):return self.log.read_text(encoding="utf-8")

    def api(self,opener):
        return FinamAPI(opener=opener,secret_reader=lambda:"private_secret",
                        coordinator=RequestCoordinator(per_minute=60000))

    def test_second_page_http_error_keeps_page_and_operation_but_not_cursor(self):
        def opened(req,timeout):
            if req.get_method()=="POST":
                return Response({"token":"private_jwt"} if req.full_url.endswith("/sessions") else {"md_permissions":[]})
            if "cursor=" not in req.full_url:
                return Response({"assets":[],"next_cursor":"private_cursor"})
            raise HTTPError(req.full_url,503,"private_message",{},BytesIO(b"private_response"))
        with self.assertRaises(FinamAPIError) as caught:self.api(opened).active_assets()
        self.assertEqual(caught.exception.diagnostic_page,2)
        self.assertEqual(caught.exception.diagnostic_operation,"catalog")
        diag.log_event("catalog_unavailable",error=caught.exception,stage="catalog")
        text=self.read()
        self.assertIn("http=http_503",text)
        self.assertIn("page=2",text)
        self.assertNotIn("private",text)
        self.assertNotIn("https://",text)

    def test_catalog_can_identify_auth_failure_before_first_page(self):
        def opened(req,timeout):
            raise HTTPError(req.full_url,403,"private_message",{},BytesIO(b"private_response"))
        with self.assertRaises(FinamAPIError) as caught:self.api(opened).active_assets()
        diag.log_event("catalog_unavailable",error=caught.exception,stage="catalog")
        text=self.read()
        self.assertIn("stage=catalog",text)
        self.assertIn("operation=auth",text)
        self.assertIn("page=1",text)
        self.assertIn("http=http_403",text)
        self.assertNotIn("private",text)

    def test_invalid_catalog_schema_is_classified_without_body(self):
        api=self.api(Mock())
        api._jwt="private_jwt";api._expiry=float("inf")
        api.opener=Mock(return_value=Response({"assets":"private_invalid_rows"}))
        with self.assertRaises(FinamAPIError) as caught:api.active_assets()
        diag.log_event("catalog_unavailable",error=caught.exception,stage="catalog")
        self.assertIn("kind=invalid_response",self.read())
        self.assertIn("page=1",self.read())
        self.assertNotIn("private",self.read())

    def worker(self,api):
        return FinamCatalogWorker(api,
            roll_state=SimpleNamespace(load=lambda:{},save=Mock()),futures_cache=Mock(),
            clock=lambda:datetime(2026,11,15,tzinfo=timezone.utc))

    def test_worker_logs_original_failure_and_explicit_cache_fallback_outcome(self):
        error=FinamAPIError("private server reply",http_status=503)
        error.diagnostic_page=7;error.diagnostic_operation="catalog"
        worker=self.worker(SimpleNamespace(active_assets=Mock(side_effect=error)))
        with patch("flowdesk.finam_catalog_worker.resolve_futures",return_value=({}, {}, [])), \
             patch("flowdesk.finam_catalog_worker.refresh_top20",return_value=([],"MOEX healthy")):
            worker.run()
        lines=self.read().splitlines()
        failed=next(line for line in lines if "event=catalog_unavailable" in line)
        self.assertIn("http=http_503",failed)
        self.assertIn("page=7",failed)
        self.assertIn("stage=catalog",failed)
        self.assertIn("elapsed_ms=",failed)
        self.assertIn("event=catalog_complete",lines[-1])
        self.assertIn("outcome=fallback",lines[-1])
        self.assertNotIn("private",self.read())

    def test_outer_worker_error_records_the_failing_stage(self):
        worker=self.worker(SimpleNamespace(active_assets=lambda **kwargs:[]))
        worker.roll_state.load=Mock(side_effect=PermissionError("private_path"))
        worker.run()
        text=self.read()
        self.assertIn("event=catalog_worker_failed",text)
        self.assertIn("stage=roll_state_load",text)
        self.assertIn("kind=storage",text)
        self.assertNotIn("private",text)

    def test_swallowed_moex_ranking_error_still_records_its_cause(self):
        source=SimpleNamespace(cached=lambda:{},due=lambda value:True,
            refresh=Mock(side_effect=TimeoutError("private network detail")))
        with patch("flowdesk.moex_reference.cached_top20",return_value=([],"cached")):
            rows,status=refresh_top20(watchlist=source)
        self.assertIn("MOEX недоступна",status)
        text=self.read()
        self.assertIn("event=stock_ranking_unavailable",text)
        self.assertIn("kind=timeout",text)
        self.assertIn("stage=stock_ranking",text)
        self.assertNotIn("private",text)
