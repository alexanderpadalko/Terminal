"""Extended no-secret diagnostics for a read-only market analytics terminal.

Distinct from unit tests and the legacy 38-symbol stress audit:
* Finam/Binance persistence isolation with restart, overlap and WAL validation.
* The one user-configurable FVG theme with alpha-composited pixel checks.
* Failure behavior of corrupt saved preferences and corrupt local SQLite.
* Deterministic evidence, with no credentials, HTTP or trading operations.

Run from the repository root with: python -m tools.audit_terminal_extended
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import json
import os
import sqlite3
import tempfile
import time
import tracemalloc


FRAMES = ("M1", "M5", "H1", "H4", "D1", "W1", "MN1")
STEPS = {"M1": 1, "M5": 5, "H1": 60, "H4": 240,
         "D1": 1440, "W1": 10080, "MN1": 43200}


def require(condition, description):
    if not condition:
        raise AssertionError(description)


def provider_isolation(root):
    from flowdesk.binance_api import BinanceAPI
    from flowdesk.finam_market import FinamMarketService
    from flowdesk.market_services import MarketServices
    from flowdesk.market_data import Candle

    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    finam_path = root / "finam_candles_v1.sqlite3"
    crypto_path = root / "binance_candles_v1.sqlite3"
    finam = FinamMarketService(finam_path)
    crypto = FinamMarketService(crypto_path, api=BinanceAPI())
    routed = MarketServices(finam=finam, crypto=crypto, directory=root)
    symbols = ("BTCUSDT@BINANCE", "ETHUSDT@BINANCE")
    per_frame = 80
    t0 = time.perf_counter()

    def write(symbol):
        for tf in FRAMES:
            duration = timedelta(minutes=STEPS[tf])
            bars = [Candle(start + duration*i, 100+i/100, 102+i/100,
                           99+i/100, 101+i/100, 10+i,
                           scheduled_close=start+duration*(i+1))
                    for i in range(per_frame)]
            require(routed.for_symbol(symbol) is crypto, "crypto routed to Finam")
            crypto._upsert(symbol,tf,bars)
            # New provider candles may revise their most recent kline values.
            corrected = bars[5]
            crypto._upsert(symbol,tf,[Candle(
                corrected.time, corrected.open, corrected.high+1,
                corrected.low, corrected.close, corrected.volume,
                scheduled_close=corrected.scheduled_close)])
        return symbol

    with ThreadPoolExecutor(max_workers=2) as pool:
        require(tuple(pool.map(write,symbols))==symbols, "worker lost crypto symbol")
    finam._upsert("SBER@MISX","H1",[Candle(start,10,11,9,10,5)])
    require(routed.for_symbol("SBER@MISX") is finam, "Finam routed to Binance")
    require(not finam._series("BTCUSDT@BINANCE"), "crypto candle polluted Finam DB")
    require(not crypto._series("SBER@MISX"), "Finam candle polluted crypto DB")

    expected_crypto = 2 * len(FRAMES) * per_frame
    for path, count in ((crypto_path,expected_crypto),(finam_path,1)):
        with closing(sqlite3.connect(path)) as db:
            integrity=db.execute("PRAGMA integrity_check").fetchone()[0]
            require(integrity=="ok","SQLite corruption in "+path.name)
            require(db.execute("PRAGMA journal_mode").fetchone()[0].lower()=="wal",
                    "WAL disabled in "+path.name)
            total=db.execute("SELECT COUNT(*) FROM candles").fetchone()[0]
            distinct=db.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT symbol,tf,ts FROM candles)"
            ).fetchone()[0]
            require(total==distinct==count,"missing or duplicate "+path.name)
            if path==crypto_path:
                corrected=db.execute(
                    "SELECT COUNT(*) FROM candles WHERE ROUND(h-o, 2)=3"
                ).fetchone()[0]
                require(corrected==2*len(FRAMES),"overlapping kline correction missing")

    reread=FinamMarketService(crypto_path,api=BinanceAPI())
    for symbol in symbols:
        series=reread._series(symbol)
        require(len(series)==len(FRAMES),"frame lost after crypto restart")
        require(all(len(series[tf])==per_frame for tf in FRAMES),
                "historical candle lost after crypto restart")
    return {"status":"PASS", "providers":["Finam","Binance USD-M"],
            "crypto_symbols":list(symbols), "frames":len(FRAMES),
            "crypto_candles":expected_crypto, "finam_candles":1,
            "kline_corrections":2*len(FRAMES),
            "sqlite_integrity":"ok", "sqlite_mode":"wal",
            "elapsed_seconds":round(time.perf_counter()-t0,3)}


def user_theme_render(root):
    os.environ["QT_QPA_PLATFORM"]="offscreen"
    from PySide6.QtGui import QColor,QImage,QPainter
    from PySide6.QtWidgets import QApplication
    from flowdesk.gui import CandleChart
    from flowdesk.settings import IndicatorSettings,ApplicationSettings,SettingsStore
    from flowdesk.settings_dialog import qcolor

    app=QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    prefs=SettingsStore(root/"user_colors.json")
    chosen={"fvgBullColor":"#A11B00BF",
            "fvgBearColor":"#2244CC80",
            "fvgFilledColor":"#80EC2040"}
    original=IndicatorSettings().changed(**chosen)
    appearance=ApplicationSettings(chart_background="#101820FF",
                                   chart_candle_up="#20C997FF")
    prefs.write(original,appearance)
    loaded,loaded_app=prefs.read()
    require(not prefs.warning,"settings read unexpectedly degraded")
    require(all(getattr(loaded,key)==value for key,value in chosen.items()),
            "FVG settings lost during persistence")
    chart=CandleChart()
    results=[]
    try:
        chart.set_settings(loaded)
        chart.set_chart_colors(loaded_app)
        for alias in ("tv","dark","user"):
            chart.set_theme(alias)
            require(chart.visual_theme=="user","legacy theme override still active")
        for side,broken,key in ((1,False,"fvgBullColor"),
                                (-1,False,"fvgBearColor"),
                                (1,True,"fvgFilledColor")):
            source=qcolor(getattr(loaded,key))
            picture=QImage(220,150,QImage.Format.Format_ARGB32_Premultiplied)
            background=QColor("#101820")
            picture.fill(background)
            painter=QPainter(picture)
            try:
                chart._draw_zone(painter,20,195,20,120,side,broken,"H1",12)
            finally:
                painter.end()
            # Inside FVG rect, beyond the border and away from text/median.
            pixel=picture.pixelColor(31,38)
            opacity=source.alphaF()
            expected=tuple(round(base*(1-opacity)+shade*opacity)
                           for base,shade in zip(
                               (background.red(),background.green(),background.blue()),
                               (source.red(),source.green(),source.blue())))
            actual=(pixel.red(),pixel.green(),pixel.blue())
            require(all(abs(x-y)<=3 for x,y in zip(actual,expected)),
                    "FVG RGB/alpha ignored for "+key+": "+str(actual))
            results.append({"setting":key,"hex":getattr(loaded,key),
                            "measured_rgb":list(actual),
                            "expected_rgb":list(expected)})
        return {"status":"PASS","single_theme":chart.visual_theme,
                "settings_persisted":True,"fvg_rendered":results,
                "canvas_background":loaded_app.chart_background}
    finally:
        chart.close()
        app.processEvents()


def corrupt_state_behavior(root):
    from flowdesk.settings import SettingsStore,IndicatorSettings,ApplicationSettings
    from flowdesk.finam_market import FinamMarketService

    # Fail safe and continue with defaults on malformed preferences.
    path=root/"corrupt-settings.json"
    path.write_text("{incomplete",encoding="utf-8")
    settings=SettingsStore(path)
    indicator,application=settings.read()
    require(isinstance(indicator,IndicatorSettings) and
            isinstance(application,ApplicationSettings) and
            bool(settings.warning),"invalid settings not safely downgraded")

    # A malformed preferences file must survive the next user save.
    original=path.read_bytes()
    settings.write(indicator,application)
    old=list(root.glob(path.name+".invalid-*"))
    require(len(old)==1 and old[0].read_bytes()==original,
            "corrupted preference bytes were lost on save")

    # A valid WAL snapshot is verified and the damaged original is preserved.
    from flowdesk.storage_safety import create_backup, recover_to_new_file, verify_database
    from flowdesk.market_data import Candle
    db=root/"corrupt-candles.sqlite3"
    service=FinamMarketService(db)
    service._upsert("SBER@MISX","H1",[
        Candle(datetime(2026,11,14,tzinfo=timezone.utc),100,101,99,100,10)])
    good=Path(create_backup(db)["path"])
    original_bad=b"invalid SQLite database; do not overwrite"
    db.write_bytes(original_bad)
    try:
        FinamMarketService(db)
    except sqlite3.DatabaseError:
        recovery="manual_recovery_required"
    else:
        recovery="unexpectedly_accepted"
    require(recovery=="manual_recovery_required",
            "corrupt market database accepted without warning")
    output=root/"recovered-new.sqlite3"
    restored=recover_to_new_file(good,output)
    require(restored["candles"]==1 and verify_database(output)==1,
            "non-destructive restoration did not preserve candle")
    require(db.read_bytes()==original_bad,
            "recovery overwrote damaged original")
    return {"status":"PASS","invalid_preferences":"defaults_with_warning",
            "invalid_preferences_original_preserved":True,
            "corrupt_sqlite":"detected_without_deletion",
            "verified_backup_restored_to_new_file":True,
            "recovery_requires_manual_selection":True}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output",default="terminal-extended-audit.json")
    args=parser.parse_args()
    t0=time.perf_counter()
    tracemalloc.start()
    report={"audit_schema":1,"mode":"offline_synthetic",
            "external_requests":False,"secrets_used":False,
            "coverage":"40 instruments / 7 frames; 38 Finam in baseline audit, 2 crypto here"}
    try:
        with tempfile.TemporaryDirectory(prefix="flowdesk-extended-audit-") as folder:
            root=Path(folder)
            report["provider_isolation"]=provider_isolation(root)
            report["theme_fvg"]=user_theme_render(root)
            report["failure_modes"]=corrupt_state_behavior(root)
        report["status"]="PASS"
    except BaseException as exc:
        report["status"]="FAIL"
        report["failure_type"]=type(exc).__name__
        raise
    finally:
        _,peak=tracemalloc.get_traced_memory()
        tracemalloc.stop()
        report["elapsed_seconds"]=round(time.perf_counter()-t0,3)
        report["peak_python_allocations_mb"]=round(peak/1048576,2)
        Path(args.output).write_text(
            json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print("TERMINAL_EXTENDED_AUDIT_"+report["status"]+" "+
              json.dumps(report,ensure_ascii=False))


if __name__=="__main__":
    main()
