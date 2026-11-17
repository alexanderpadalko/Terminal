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
            evaluate=lambda service,ticker,mic,settings,**kwargs:ScanResult(ticker+"@"+mic,(0,)*4))
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
        from .provider_candles import native_higher_series
        from .finam_api import finam_bar
        observed=datetime(2026,11,15,tzinfo=timezone.utc)
        months=[finam_bar({'timestamp':f'2026-{month:02d}-01T04:00:00Z',
            'open':100,'high':102,'low':99,'close':101,'volume':5},'MN1') for month in range(6,12)]
        native,_=native_higher_series({'MN1':months},observed,
            {('MN1',b.time):observed for b in months})
        result['native_dwm']=len(native.get('MN1',()))==6
        from .us_futures_family import calendar_for_family,build_family,enabled
        nq_calendar=calendar_for_family(calendar_for('NQZ26@XCME'))
        nq_session=nq_calendar.daily_session(date(2026,11,13))
        hours=[Candle(nq_session.start+timedelta(hours=i),100,102,99,101,1,
            scheduled_close=nq_session.start+timedelta(hours=i+1)) for i in range(23)]
        daily=finam_bar({'timestamp':'2026-11-13T05:00:00Z','open':100,
                        'high':102,'low':99,'close':100.5,'volume':23},'D1')
        observations={('H1',b.time):observed for b in hours}
        observations['D1',daily.time]=observed
        family,status,_=build_family({'H1':hours,'D1':[daily]},nq_calendar,observed,
            coverage={'H1':[(nq_session.start,nq_session.end)]},observations=observations)
        result['nq_session_family']=bool(status=='ready' and family and
            family['D1'][0].close==101 and not family['MN1'][0].settlement_close)
        result['nq_session_last_trade']=bool(result['nq_session_family'] and
            family['W1'][0].close==101 and family['MN1'][0].close==101)
        checked=[]
        for symbol in ('NQZ26@XCME','ESZ26@XCME','GCZ26@XCEC','SIZ26@XCEC',
                       'PLF27@XNYM','PAZ26@XNYM','NGZ26@XNYM','BRNF27@IFEU'):
            cal=calendar_for_family(calendar_for(symbol));session=cal.daily_session(date(2026,11,13))
            rows=[]
            for left,right in session.segments:
                while left<right:
                    end=min(right,left+timedelta(hours=1))
                    rows.append(Candle(left,100,102,99,101,1,scheduled_close=end));left=end
            values,status,_=build_family({'H1':rows,'D1':[daily]},cal,observed,
                coverage={'H1':[(session.start,session.end)]},
                observations={('H1',bar.time):observed for bar in rows})
            if enabled(symbol,cal) and status=='ready' and all(values[tf][-1].close==101
                    and not values[tf][-1].settlement_close for tf in ('D1','W1','MN1')):
                checked.append(symbol)
        result['us_futures_session_last_trade']=len(checked)==8
        from .pipeline import analyze_timeframes
        from .terminal_policy import DISPLAY_TIMEFRAMES, FLOW_TIMEFRAMES
        terminal_sources={tf:[Candle(observed-timedelta(hours=i),100,102,99,101,1,
                              scheduled_close=observed-timedelta(hours=i-1))
                              for i in range(12,0,-1)] for tf in (*DISPLAY_TIMEFRAMES,'H1','M1')}
        chart,parents=analyze_timeframes(terminal_sources,'M5',asof=observed,terminal=True)
        result['terminal_scope']=bool(not chart.flows and not chart.pairs and not chart.market
            and set(parents)==set(FLOW_TIMEFRAMES) and set(chart.summary_sources)==set(FLOW_TIMEFRAMES)
            and all(not item.market for item in parents.values()))
        result["ok"] = (result.get("executor_callable") and
                        result.get("thread_result") == 17 and result.get("localhost_resolved")
                        and result.get("scheduler_result")==23 and result.get("scanner_rows")==3
                        and result.get("binance_decoder") and result.get('session_candles')
                        and result.get('native_dwm') and result.get('nq_session_family')
                        and result.get('nq_session_last_trade') and result.get('us_futures_session_last_trade')
                        and result.get('terminal_scope'))
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    Path(output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1
