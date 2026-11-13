"""Bounded read-only check with the local key; never print credentials/accounts."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from flowdesk.asyncio_runtime import prepare_asyncio_runtime
from flowdesk.finam_api import FinamAPI, FinamAPIError, read_secret, symbol_name
from flowdesk.finam_stream import FinamStreamWorker, interpret_packet


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="TSLA@XNAS")
    parser.add_argument("--http", action="store_true", help="Explicitly allow market REST checks")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--catalog", action="store_true", help="Include optional symbol-catalog diagnostics")
    parser.add_argument("--all-frames", action="store_true", help="Observe up to 20 seconds for all seven candle frames")
    args = parser.parse_args()
    ticker, mic = args.symbol.split("@")
    symbol = symbol_name(ticker, mic)
    report = {"symbol": symbol, "secret_configured": bool(read_secret()), "read_only": True}
    if report["secret_configured"]:
        prepare_asyncio_runtime()
        failures=[]
        def error_terms(message):
            allowed={"symbol","instrument","account","not","found","permission","denied",
                     "unavailable","market","data","unsupported","cache","provider","mic",
                     "assets","forbidden","invalid","internal","server","error","unknown"}
            allowed.update({"attributeerror","typeerror","valueerror","list","object","has","no",
                            "attribute","get","subscriptable","mapping","timeout","timed","out",
                            "datetime","invalidstatus","keyerror","indexerror","float","str","dict"})
            return sorted(set(re.findall(r"[a-z]+",str(message).lower())) & allowed)
        def open_checked(request,timeout):
            try:
                return urlopen(request,timeout=timeout)
            except HTTPError as exc:
                # Extract numeric code and an allowlist of generic words, never
                # raw messages, bodies, account IDs, request headers or keys.
                try:
                    problem=json.loads(exc.read(8192))
                except Exception:
                    problem={}
                failures.append({"http_status":exc.code,
                    "api_code":problem.get("code") if type(problem.get("code")) is int else None,
                    "message_terms":error_terms(problem.get("message",""))})
                raise
        api = FinamAPI(opener=open_checked)
        try:
            api.access_key()
            report["authentication"] = "OK"
            report["matching_permissions"] = [
                {k: p[k] for k in ("mic", "country", "continent", "worldwide", "quote_level", "delay_minutes") if k in p}
                for p in api.permissions if isinstance(p, dict) and
                (p.get("mic") == mic or p.get("worldwide"))]
            if args.http:
                try:
                    bars = api.bars(symbol, "H1")
                    report["history"] = {"status": "OK", "bars": len(bars),
                        "last_bar": bars[-1].time.isoformat() if bars else None}
                except FinamAPIError as exc:
                    report["history"] = {"status": "ERROR", "http_status": exc.http_status}
            if args.http and args.catalog:
                cursor,seen="",set()
                matches=[]
                for page in range(25):
                    query={"only_active":"true"}
                    if cursor:query["cursor"]=cursor
                    try:
                        catalog=api._json("GET","/v1/assets/all?"+urlencode(query),token=api.access_key())
                    except FinamAPIError as exc:
                        report["all_catalog_error"]={"http_status":exc.http_status}
                        break
                    matches.extend({k:a[k] for k in ("symbol","ticker","mic") if k in a}
                        for a in catalog.get("assets",[]) if isinstance(a,dict) and a.get("ticker")==ticker)
                    report["all_catalog_pages"]=page+1
                    report["catalog_examples"]=[{k:a[k] for k in ("symbol","ticker","mic") if k in a}
                        for a in catalog.get("assets",[]) if isinstance(a,dict) and
                        a.get("ticker") in ("SBER","GAZP","AAPL","MSFT","NVDA","TSLA","IBM")]
                    cursor=catalog.get("next_cursor")
                    if matches or not cursor or cursor=="0" or cursor in seen:
                        report["all_catalog_complete"]=not cursor or cursor=="0"
                        break
                    seen.add(cursor)
                report["all_catalog_matches"]=matches
            worker = FinamStreamWorker(ticker, mic, api=api)
            def packet_shape(value,depth=0):
                if depth>4:return type(value).__name__
                if isinstance(value,dict):
                    return {k:packet_shape(v,depth+1) for k,v in value.items()
                            if k not in ("token","secret","account_ids","account_id")}
                if isinstance(value,list):return [packet_shape(value[0],depth+1)] if value else []
                return type(value).__name__
            def inspect_packet(packet,*args):
                report.setdefault("packet_shapes",[])
                if len(report["packet_shapes"])<3:
                    expanded=dict(packet)
                    if isinstance(expanded.get("payload"),str):
                        try:expanded["payload"]=json.loads(expanded["payload"])
                        except ValueError:pass
                    report["packet_shapes"].append(packet_shape(expanded))
                    key=str(packet.get("subscription_key") or "")
                    report.setdefault("subscription_keys",[]).append(re.sub(r"[A-Za-z0-9_-]{25,}","<opaque>",key)[:150])
                return interpret_packet(packet,*args)
            counts, states, frames = {}, [], set()
            def record(event):
                counts[event.kind] = counts.get(event.kind, 0) + 1
                if event.kind == "status":
                    states.append(event.state)
                    if event.detail:
                        report["websocket_error_terms"]=error_terms(event.detail)
                if event.kind == "candles":
                    frames.add(event.timeframe)
                if event.kind == "quote":
                    report["quote_snapshot"]=event.quote.get("isDataSnapshot",event.quote.get("is_data_snapshot"))
                    report["quote_timestamp"]=event.quote.get("timestamp")
                if (not args.all_frames and event.kind in ("quote", "candles")) or len(frames)==7:
                    worker.stop()
            worker.event.connect(record, Qt.ConnectionType.DirectConnection)
            async def observe():
                try:
                    await asyncio.wait_for(worker._connect_loop(), timeout=20)
                except TimeoutError:
                    pass
                finally:
                    worker.stop()
            with patch("flowdesk.finam_stream.interpret_packet",side_effect=inspect_packet):
                asyncio.run(observe())
            report["websocket"] = {"states": states, "events": counts,
                "candle_timeframes":sorted(frames),
                "received_market_data": bool(counts.get("quote") or counts.get("candles"))}
            report["http_errors"]=failures
        except FinamAPIError as exc:
            report["authentication_error"] = {"http_status": exc.http_status}
        except Exception as exc:
            report["error_type"] = type(exc).__name__
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
