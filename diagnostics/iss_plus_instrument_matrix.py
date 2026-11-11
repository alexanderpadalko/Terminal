"""ISS+ DEMO guest symbol-availability check; isolated from FlowDesk and its cache.

Tests SEARCH.ticker discovery and 1-shot snapshot subscriptions. It does NOT
claim that a documented market grants access when no MESSAGE has been received.
"""
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diagnostics.iss_plus_demo.probe import Evidence, Parser, Session, rows_from, stamp

PATTERNS = []  # Discovery returned no frames in run 1; probe direct code candidates.

FALLBACK = {
    "SBER": "MXSE.TQBR.SBER",
    "AFKS": "MXSE.TQBR.AFKS",
    "GAZP": "MXSE.TQBR.GAZP",
    "LKOH": "MXSE.TQBR.LKOH",
    "IRAO": "MXSE.TQBR.IRAO",
}
OUT = Path("iss-plus-instruments")
OUT.mkdir(parents=True, exist_ok=True)

def compact_rows(rows, count=4):
    result = []
    for row in rows[:count]:
        if isinstance(row, dict):
            result.append({k: row.get(k) for k in ("ticker", "TICKER", "secid", "boardid", "shortname", "FROM", "TILL", "LAST", "OPEN", "CLOSE") if k in row})
        else:
            result.append(str(row)[:120])
    return result

def frame_data(frame):
    try:
        parsed = json.loads(frame.get("body") or "{}")
    except (TypeError, ValueError):
        parsed = {}
    _, rows = rows_from(frame.get("body", ""))
    return parsed, rows

async def collect(ws, parser, seconds):
    result = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=min(0.85, end-time.monotonic()))
        except asyncio.TimeoutError:
            continue
        except Exception as exc:
            result.append({"command": "TRANSPORT_ERROR", "body": str(exc), "headers": {}})
            break
        result.extend(parser.feed(msg))
    return result

async def main():
    evidence = Evidence(str(OUT))
    report = {
        "tested_at_utc": stamp(),
        "scope": "ISS+ STOMP DEMO/guest, discovery and snapshot access only",
        "disclaimer": "No response means UNKNOWN, not UNAVAILABLE",
        "connection": {},
        "search": {},
        "subscriptions": [],
    }
    session = Session(evidence, "symbols-probe")
    if not await session.open({"subprotocols":["STOMP"], "versions":True}):
        report["connection"] = evidence.connection_attempts[-1]
        (OUT/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print("INSTRUMENT_MATRIX_BEGIN")
        print(json.dumps(report,ensure_ascii=False))
        print("INSTRUMENT_MATRIX_END")
        evidence.raw.close()
        return
    report["connection"] = {
        "websocket_connected": True,
        "stomp_connected": True,
        "catalog_markets": sorted([
            str(k) for k in ((json.loads(session.auth.get("body") or "{}")).keys())
        ]) if (session.auth.get("body") or "").lstrip().startswith("{") else [],
    }
    print("Connected DEMO/guest; searching symbols.",flush=True)
    try:
        # A REQUEST reply is handled before subscriptions; no ambiguous interleaving.
        for label, pattern in PATTERNS:
            reqid = "lookup-" + label
            await session.send("REQUEST", {"id": reqid, "destination":"SEARCH.ticker",
                                           "selector": f'pattern="{pattern}"'})
            frames = await collect(session.ws, session.parser, 1.65)
            entries, errors, commands = [], [], []
            for f in frames:
                commands.append(f["command"])
                if f["command"] == "ERROR":
                    errors.append({"headers": f["headers"],"message":f.get("body","")[:250]})
                if f["command"] in ("REPLY","MESSAGE"):
                    parsed, rows = frame_data(f)
                    entries.extend(rows)
                    if not rows and isinstance(parsed, list):
                        entries.extend(x for x in parsed if isinstance(x, dict))
                    elif not rows and isinstance(parsed, dict):
                        for value in parsed.values():
                            if isinstance(value, list) and value and isinstance(value[0], dict):
                                entries.extend(value)
            report["search"][label] = {
                "pattern":pattern,"commands":commands,"count":len(entries),
                "items":entries[:20],"errors":errors,
            }
            print("SEARCH",label,len(entries),commands[:3],flush=True)

        choices = []
        for label, _ in PATTERNS:
            found = report["search"][label]["items"]
            full = [(r.get("ticker") or r.get("TICKER") or "") for r in found if isinstance(r,dict)]
            full = [v for v in full if isinstance(v,str) and "." in v]
            expected = {
                "SBER":"MXSE.TQBR.","AFKS":"MXSE.TQBR.","GAZP":"MXSE.TQBR.",
                "LKOH":"MXSE.TQBR.","IRAO":"MXSE.TQBR.",
                "SBMX":"MXSE.","SU262":"MXSE.","CNYRUB":"MXCX.",
                "USD000":"MXCX.","IMOEX":"INDICES.",
                "Si":"FORTS.","RI":"FORTS.","BR":"FORTS."
            }.get(label,"")
            preferred = [v for v in full if v.startswith(expected)]
            picked = (preferred or full or [FALLBACK.get(label,"")])[0]
            if not picked:
                continue
            source = "SEARCH.ticker" if full else "fallback_not_discovered"
            choices.append((label,picked,source))
        choices.extend([
            ("ROSN", "MXSE.TQBR.ROSN", "candidate_code"),
            ("VTBR", "MXSE.TQBR.VTBR", "candidate_code"),
            ("YDEX", "MXSE.TQBR.YDEX", "candidate_code"),
            ("OZON", "MXSE.TQBR.OZON", "candidate_code"),
            ("NVTK", "MXSE.TQBR.NVTK", "candidate_code"),
            ("TATN", "MXSE.TQBR.TATN", "candidate_code"),
            ("SBMX", "MXSE.TQTF.SBMX", "candidate_code"),
            ("SU26238", "MXSE.TQOB.SU26238RMFS4", "candidate_code"),
            ("SU26238BB", "MXSEBB.TQOB.SU26238RMFS4", "candidate_code"),
            ("CNYRUB_TOM", "MXCX.CETS.CNYRUB_TOM", "candidate_code"),
            ("USD000UTSTOM", "MXCX.CETS.USD000UTSTOM", "candidate_code"),
            ("SiZ6_RFUD", "FORTS.RFUD.SiZ6", "candidate_code"),
            ("SiZ6_FORTS", "FORTS.FORTS.SiZ6", "candidate_code"),
            ("IMOEX_INDEX", "INDICES.MICEXINDEXCF.IMOEX", "candidate_code"),
            ("IMOEX_ALT", "INDICES.INDICES.IMOEX", "candidate_code"),
        ])
        seen = set()
        specs = []
        for label,ticker,source in choices:
            if ticker in seen:
                continue
            seen.add(ticker)
            family = ticker.split(".")[0]
            if family not in {"MXSE","MXSEBB","MXCX","FORTS","OPTIONS","INDICES","MONEY","CBRF","FIXING","SRATES"}:
                continue
            select_field = "TICKER" if family in ("MXSE","MXSEBB","MXCX","MONEY") else "ticker"
            specs.append({
                "label":label,"ticker":ticker,"discovered_by":source,
                "destination":f"{family}.securities",
                "selector":f'{select_field}="{ticker}"',
            })
            if family not in {"MXSEBB","MONEY"}:
                specs.append({
                    "label":label+"-M1","ticker":ticker,"discovered_by":source,
                    "destination":f"{family}.candles",
                    "selector":f'ticker="{ticker}" and interval="M1" and quantity=2',
                })
        # Explicit SBER+AFKS reference in case SEARCH has selective results.
        for label in ("SBER","AFKS"):
            ticker=FALLBACK[label]
            if not any(i["ticker"]==ticker and i["destination"]=="MXSE.securities" for i in specs):
                specs.append({"label":label+"-reference","ticker":ticker,
                              "discovered_by":"fallback_not_discovered",
                              "destination":"MXSE.securities","selector":f'TICKER="{ticker}"'})
        specs=specs[:36]
        for i,spec in enumerate(specs):
            spec["id"]=f"inst-{i}"
            await session.send("SUBSCRIBE", {
                "id":spec["id"], "receipt":spec["id"],
                "destination":spec["destination"],"selector":spec["selector"]
            })
            if (i+1)%6==0:
                await asyncio.sleep(.25)
        print("SUBSCRIBE count",len(specs),flush=True)
        frames=await collect(session.ws,session.parser,24)
        by_id=defaultdict(lambda:{"commands":[], "rows":0, "types":{}, "errors":[], "examples":[]})
        unassigned=[]
        for f in frames:
            h=f.get("headers",{})
            ident=h.get("subscription") or h.get("receipt-id") or h.get("id")
            if ident not in {s["id"] for s in specs}:
                unassigned.append({"command":f["command"],"headers":h,"message":f.get("body","")[:300]})
                continue
            ent=by_id[ident]
            ent["commands"].append(f["command"])
            if f["command"]=="ERROR":
                ent["errors"].append({"headers":h,"message":f.get("body","")[:400]})
            if f["command"]=="MESSAGE":
                props, rows=rows_from(f.get("body",""))
                ent["rows"]+=len(rows)
                typ=props.get("type","unknown")
                ent["types"][typ]=ent["types"].get(typ,0)+1
                if rows and not ent["examples"]:
                    ent["examples"]=compact_rows(rows)
        for spec in specs:
            result={**spec,**by_id[spec["id"]]}
            result["status"] = (
                "DATA_RECEIVED" if result["rows"]>0 else
                "ERROR" if result["errors"] else
                "RECEIPT_ONLY_NO_DATA" if "RECEIPT" in result["commands"] else
                "NO_RESPONSE"
            )
            report["subscriptions"].append(result)
            print("RESULT",result["label"],result["ticker"],result["destination"],result["status"],
                  result["rows"],flush=True)
        report["unassigned_frames"]=unassigned[:15]
    except Exception as exc:
        report["exception"]=f"{type(exc).__name__}: {exc}"
    finally:
        await session.close()
        evidence.raw.close()
        report["finished_at_utc"]=stamp()
        (OUT/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print("INSTRUMENT_MATRIX_BEGIN",flush=True)
        print(json.dumps({"connection":report.get("connection"),"search":{k:{"count":v.get("count"),"commands":v.get("commands"),"error_count":len(v.get("errors",[])), "examples":compact_rows(v.get("items",[]),3)} for k,v in report["search"].items()},"subscriptions":report["subscriptions"],"exception":report.get("exception")},ensure_ascii=False),flush=True)
        print("INSTRUMENT_MATRIX_END",flush=True)

if __name__ == "__main__":
    asyncio.run(main())
