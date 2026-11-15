"""Credential-free packaged regression for the Finam worker's asyncio startup."""
import asyncio
import concurrent.futures
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

from .finam_stream import FinamStreamWorker
from .finam_requests import RequestCoordinator
from .finam_screener import FinamScreenerWorker, ScanResult
from .binance_stream import interpret_packet as binance_packet
from datetime import date,datetime,timedelta,timezone


def check_finam_runtime(output):
    result = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
    worker = FinamStreamWorker("TEST", "MISX")
    async def exercise_executor():
        result["executor_callable"] = callable(concurrent.futures.ThreadPoolExecutor)
        result["thread_result"] = await asyncio.to_thread(lambda: 17)
        addresses = await asyncio.get_running_loop().getaddrinfo(
            "localhost", 443, type=socket.SOCK_STREAM)
        result["localhost_resolved"] = bool(addresses)
        scheduler=RequestCoordinator()
        result["scheduler_result"]=await asyncio.to_thread(
            lambda:scheduler.run("local",lambda:23))
    worker._connect_loop = exercise_executor
    try:
        worker.run()
        entries=[SimpleNamespace(symbol=f"TEST{i}@MISX",verified=True) for i in range(3)]
        scanner=FinamScreenerWorker(object(),entries,object(),backfill=False,
            evaluate=lambda service,ticker,mic,settings,**kwargs:ScanResult(ticker+"@"+mic,(0,)*5))
        rows=[]
        scanner.row.connect(rows.append)
        scanner.run()
        result["scanner_rows"]=len(rows)
        crypto=binance_packet({"data":{"e":"aggTrade","s":"BTCUSDT",
            "p":"100","T":1794736800000}},"BTCUSDT",1,datetime.now(timezone.utc))
        result["binance_decoder"]=crypto is not None and crypto.board=="BINANCE"
        from .trading_calendar import calendar_for
        from .market_data import Candle
        from .session_candles import build_session_series
        calendar=calendar_for('SBER@MISX');session=calendar.session(date(2026,11,12))
        beginning=list(session.buckets(240))[-1][0][0]
        source=[Candle(beginning+timedelta(minutes=5*i),100,102,99,101,5,
                       scheduled_close=beginning+timedelta(minutes=5*(i+1))) for i in range(10)]
        built=build_session_series({'M5':source},calendar,session.end)
        result['session_candles']=bool(built.series.get('H4') and
            built.series['H4'][-1].scheduled_close==session.end)
        result["ok"] = (result.get("executor_callable") and
                        result.get("thread_result") == 17 and result.get("localhost_resolved")
                        and result.get("scheduler_result")==23 and result.get("scanner_rows")==3
                        and result.get("binance_decoder") and result.get('session_candles'))
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    Path(output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
