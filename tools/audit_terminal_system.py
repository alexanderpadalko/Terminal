"""Reproducible offline system audit for a read-only analytical terminal.

This does not access production Finam credentials or assert live market parity.
Runs on the GitHub Windows runner and writes redacted machine-readable evidence.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import argparse
import json
import os
import sqlite3
import tempfile
import time

from flowdesk.finam_api import FinamAPI, FinamAPIError
from flowdesk.finam_market import FinamMarketService
from flowdesk.finam_requests import RequestCoordinator
from flowdesk.finam_screener import quote_age_problem, quote_last
from flowdesk.finam_stream import interpret_packet
from flowdesk.market_data import Candle
from flowdesk.settings import MemorySettingsStore


def check(condition, label):
    if not condition:
        raise AssertionError(label)


def storage_stress(folder):
    """Write the configured Finam symbol count across seven frames and verify WAL."""
    path = folder / "stress.sqlite3"
    service = FinamMarketService(path)
    base = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    from flowdesk.finam_universe import US_CONTRACTS, RU_CONTRACTS, STOCK_DISPLAY_LIMIT
    instrument_count=len(US_CONTRACTS)+len(RU_CONTRACTS)+STOCK_DISPLAY_LIMIT
    symbols = [f"T{i:02d}@RTSX" for i in range(instrument_count)]
    frames = ("M1", "M5", "H1", "H4", "D1", "W1", "MN1")
    candles = 40
    start = time.perf_counter()

    def write(symbol):
        for tf in frames:
            step = timedelta(minutes={"M1": 1, "M5": 5, "H1": 60, "H4": 240,
                                      "D1": 1440, "W1": 10080, "MN1": 43200}[tf])
            rows = [Candle(base + step * i, 100 + i, 102 + i, 99 + i,
                           101 + i, 1000 + i) for i in range(candles)]
            service._upsert(symbol, tf, rows)
            # An overlapping historical correction changes one candle, not
            # the unique-key row count; no duplicates may appear.
            changed = list(rows)
            bar = changed[4]
            changed[4] = Candle(bar.time, bar.open, bar.high + 2,
                                bar.low, bar.close, bar.volume)
            service._upsert(symbol, tf, changed)
        return symbol

    with ThreadPoolExecutor(max_workers=3) as pool:
        completed = list(pool.map(write, symbols))
    check(len(completed) == instrument_count, "missing instruments")
    with closing(sqlite3.connect(path)) as db:
        check(db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal",
              "SQLite WAL not enabled")
        check(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
              "SQLite integrity failed")
        total = db.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
        distinct = db.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT symbol,tf,ts FROM candles)"
        ).fetchone()[0]
        check(total == instrument_count * len(frames) * candles == distinct,
              "duplicates or missing candles")
        corrected = db.execute(
            "SELECT COUNT(*) FROM candles WHERE h-o=4").fetchone()[0]
        check(corrected == instrument_count * len(frames), "corrections not persisted")
    # Restart and exercise every symbol/TF to catch cache/database pollution.
    second = FinamMarketService(path)
    for symbol in symbols:
        series = second._series(symbol)
        check(set(series) == set(frames), "cross-frame loss")
        check(all(len(series[tf]) == candles for tf in frames),
              "cross-instrument loss")
    return {"instruments": instrument_count, "timeframes": len(frames), "candles": total,
            "overlap_corrections": corrected,
            "elapsed_seconds": round(time.perf_counter()-start, 3),
            "sqlite_integrity": "ok", "sqlite_journal": "wal"}


def network_and_market_guards():
    # Explicitly ensure the API cannot call trade endpoints even when fed a
    # malformed caller request. Production token is intentionally absent.
    api = FinamAPI(secret_reader=lambda: "")
    denied = 0
    for method, path in (
        ("POST", "/v1/orders"), ("POST", "/v1/instruments/SBER@MISX/orders"),
        ("DELETE", "/v1/orders")):
        try:
            api._json(method, path)
        except (ValueError, FinamAPIError):
            denied += 1
    check(denied == 3, "trade endpoint unexpectedly admitted")

    clock = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
    recent = {"quote": [{"symbol": "SBER@MISX", "last": {"value": "110"},
                         "timestamp": "2026-11-14T11:59:30Z"}]}
    weekend = {"quote": [{"symbol": "SBER@MISX", "last": {"value": "110"},
                          "timestamp": "2026-11-13T11:59:30Z"}]}
    old = {"quote": [{"symbol": "SBER@MISX", "last": {"value": "110"},
                      "timestamp": "2026-11-07T11:59:30Z"}]}
    check(not quote_age_problem(recent, clock, "SBER@MISX"),
          "recent LAST incorrectly rejected")
    check(not quote_age_problem(weekend, clock, "SBER@MISX"),
          "previous-session LAST incorrectly rejected")
    check(quote_age_problem(old, clock, "SBER@MISX"),
          "over-seven-day LAST incorrectly accepted")
    mixed = {"quote": [{"last": 999},
                       {"symbol": "SBER@MISX", "last": {"value": "110"}}]}
    check(quote_last(mixed, "SBER@MISX") == 110,
          "wrong symbol quote selected")

    scope = {"type": "ERROR", "subscription_type": "BARS",
             "subscription_key": "SBER@MISX:TIME_FRAME_H1",
             "error_info": {"message": "password=NEVER_LOG_ME"}}
    event = interpret_packet(scope, "SBER", "MISX", 1, clock)
    check(event.state == "subscription_error",
          "single TF error stopped full WebSocket")
    check("NEVER_LOG_ME" not in event.detail,
          "raw provider credential leaked")

    api.permissions = [{"mic": "MISX", "quote_level": "ACCESS_FORBIDDEN",
                        "delay_minutes": 0}]
    api.permissions_status = "verified"
    check(api.market_delay_minutes("MISX") == 15,
          "forbidden entitlement incorrectly treated as zero-delay")
    rights = api.permission_summary()
    check(rights["status"] == "verified", "rights state lost")
    check("account_id" not in json.dumps(rights), "account ID exposed")
    return {"trade_account_calls_blocked": denied, "stale_quote_rejected": True,
            "market_scope_isolation": True, "safe_permissions": True}


def gui_geometry():
    """Offscreen, no-token GUI: range of supported window dimensions."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QSettings
    from flowdesk.finam_gui import FinamWindow
    from flowdesk.finam_universe import CRYPTO, US_FUTURES, RU_FUTURES, RU_STOCKS, GROUP_LABELS
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    with tempfile.TemporaryDirectory(prefix="flowdesk-gui-audit-") as temp, \
         patch.object(Path,"home",return_value=Path(temp)), \
         patch("flowdesk.finam_gui.QSettings",side_effect=lambda *args:
               QSettings(str(Path(temp)/"prefs.ini"),QSettings.Format.IniFormat)), \
         patch("flowdesk.diagnostics_log.LOG_PATH",Path(temp)/"diagnostics.log"), \
         patch("flowdesk.finam_gui.read_secret", return_value=""):
        window = FinamWindow(settings_store=MemorySettingsStore())
        try:
            observed = []
            for width, height in [(1040, 650), (1280, 720), (1600, 900)]:
                window.resize(width, height)
                window.show()
                app.processEvents()
                headings = [window.examples.item(i).text()
                            for i in range(window.examples.count())
                            if window.examples.item(i).text() in GROUP_LABELS.values()]
                check(headings == [GROUP_LABELS[CRYPTO],
                                   GROUP_LABELS[US_FUTURES],
                                   GROUP_LABELS[RU_FUTURES],
                                   GROUP_LABELS[RU_STOCKS]],
                      "wrong market group order")
                check(not hasattr(window, "search"), "removed search control still present")
                check(window.examples.viewport().width() > 100,
                      "instrument list not visible")
                observed.append({"width": width, "height": height,
                                 "list_viewport": window.examples.viewport().width()})
            check(not hasattr(window, "order_button"),
                  "trade entry control unexpectedly present")
            return {"offscreen_layouts": observed, "read_only_ui": True}
        finally:
            window.close()
            app.processEvents()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="terminal-system-audit.json")
    args = parser.parse_args()
    started = time.perf_counter()
    result = {"audit_schema": 1, "network": "offline_synthetic_only",
              "credentials": "not_loaded", "version": "0.2.26"}
    with tempfile.TemporaryDirectory(prefix="flowdesk-audit-") as temp:
        result["sqlite_stress"] = storage_stress(Path(temp))
    result["safety"] = network_and_market_guards()
    result["gui"] = gui_geometry()
    result["duration_seconds"] = round(time.perf_counter()-started, 3)
    result["status"] = "PASS"
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TERMINAL_SYSTEM_AUDIT_OK " + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
