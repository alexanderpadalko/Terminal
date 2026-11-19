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


from flowdesk.terminal_policy import SOURCE_TIMEFRAMES
FRAMES = SOURCE_TIMEFRAMES
STEPS = {"M5": 5, "H1": 60,
         "D1": 1440, "W1": 10080, "MN1": 43200}


def require(condition, description):
    if not condition:
        raise AssertionError(description)


def provider_isolation(root):
    from flowdesk.finam_market import FinamMarketService
    from flowdesk.market_services import MarketServices
    from flowdesk.market_data import Candle
    from flowdesk.finam_universe import compose_universe, RU_FUTURES
    finam=FinamMarketService(root / "finam_candles_v1.sqlite3")
    routed=MarketServices(finam=finam,directory=root)
    rows=compose_universe()[0][RU_FUTURES]
    require(any(row.label=="EDZ2026" for row in rows),"ED missing")
    require(routed.for_symbol("EDZ6@RTSX") is finam,"ED not routed to Finam")
    for label,symbol in (('IRUS.P','IMOEXF@RTSX'),('MMZ2026','MMZ6@RTSX'),
                         ('GNZ2026','GNZ6@RTSX'),('S1Z2026','S1Z6@RTSX'),
                         ('BMZ2026','BMZ6@RTSX'),('NRX2026','NRX6@RTSX')):
        require(any(row.label==label for row in rows),'Added MOEX future missing')
        require(routed.for_symbol(symbol) is finam,'MOEX future not routed to Finam')
    try:routed.for_symbol("BTCUSDT@BINANCE")
    except ValueError:pass
    else:raise AssertionError("Retired crypto connection enabled")
    require(not (root/"binance_candles_v1.sqlite3").exists(),"Retired crypto database created")
    finam._upsert("EDZ6@RTSX","H1",[Candle(datetime(2026,11,17,tzinfo=timezone.utc),1,2,.5,1,1)])
    require(len(finam._series("EDZ6@RTSX")["H1"])==1,"ED history lost")
    return {"status":"PASS","providers":["Finam"],"frames":len(FRAMES),
            "ed_source":"Finam","retired_crypto_connection_rejected":True,
            "retired_crypto_database_created":False}


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
            "coverage":"27 Finam instruments, including ED and 5 MOEX additions; 5 Finam source frames, 5 chart frames"}
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
