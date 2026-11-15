"""Offline, reproducible provider-to-TradingView OHLCV comparison.

Requires timezone-aware/Unix timestamps. Never downloads TradingView data,
loads a key or guesses the reference bar's closing time/session/contract.
"""
import argparse
import csv
from dataclasses import replace
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flowdesk.market_data import read_csv,parse_time
from flowdesk.session_candles import build_session_series
from flowdesk.trading_calendar import calendar_for


def strict_csv(path):
    text=Path(path).read_text(encoding='utf-8-sig')
    dialect=csv.Sniffer().sniff(text[:8192],delimiters=',;\t')
    reader=csv.DictReader(text.splitlines(),dialect=dialect)
    columns={k.strip().lower():k for k in reader.fieldnames}
    key=next((columns[k] for k in ('time','timestamp','date','datetime') if k in columns),None)
    if key is None:raise ValueError('CSV requires a timestamp')
    endings={}
    for row in reader:
        value=row[key].strip()
        if not re.fullmatch(r'\d{10}(?:\.\d+)?|\d{13}',value):
            parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
            if parsed.tzinfo is None:raise ValueError('CSV timestamps require timezone/Unix UTC')
        closed=next((row[columns[k]] for k in ('scheduled_close','time_close') if columns.get(k) and row[columns[k]]),None)
        if closed:
            if not re.fullmatch(r'\d{10}(?:\.\d+)?|\d{13}',closed):
                parsed=datetime.fromisoformat(closed.replace('Z','+00:00'))
                if parsed.tzinfo is None:raise ValueError('CSV closing timestamps require timezone')
            endings[parse_time(value)]=parse_time(closed)
    bars=read_csv(path)
    return [replace(bar,scheduled_close=endings.get(bar.time)) for bar in bars]


def compare(actual,reference,*,tolerance=1e-8,volume=False):
    if not math.isfinite(tolerance) or tolerance<0:raise ValueError('Invalid tolerance')
    left={b.time:b for b in actual};right={b.time:b for b in reference}
    differences=[];matched=0;endings=0
    for stamp in sorted(set(left)|set(right)):
        a,b=left.get(stamp),right.get(stamp)
        if a is None or b is None:
            differences.append({'time':stamp.isoformat(),'field':'missing_actual' if a is None else 'missing_reference'})
            continue
        matched+=1
        for field in ('open','high','low','close')+(('volume',) if volume else ()):
            x,y=getattr(a,field),getattr(b,field)
            if not math.isclose(x,y,abs_tol=tolerance,rel_tol=0):
                differences.append({'time':stamp.isoformat(),'field':field,'actual':x,'reference':y})
        if b.scheduled_close is not None:
            endings+=1
            if a.scheduled_close!=b.scheduled_close:
                differences.append({'time':stamp.isoformat(),'field':'scheduled_close',
                    'actual':a.scheduled_close.isoformat() if a.scheduled_close else None,
                    'reference':b.scheduled_close.isoformat()})
    return {'actual_bars':len(left),'reference_bars':len(right),'matched_timestamps':matched,
        'reference_closes_available':endings,'volume_compared':volume,
        'ohlc_timestamps_match':bool(matched) and not differences,'differences':differences,
        'full_parity_confirmed':bool(matched) and endings==len(right) and not differences and volume}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--symbol',required=True)
    parser.add_argument('--timeframe',choices=('H4','D1','W1','MN1'),required=True)
    parser.add_argument('--source',action='append',required=True,help='M1/M5/H1/D1/H4=provider.csv')
    parser.add_argument('--asof',required=True,help='Timezone-aware ISO observation')
    parser.add_argument('--reference',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--tolerance',type=float,default=1e-8)
    parser.add_argument('--compare-volume',action='store_true')
    args=parser.parse_args();sources={}
    for item in args.source:
        tf,path=item.split('=',1)
        if tf not in ('M1','M5','H1','H4','D1'):raise ValueError('Unsupported source timeframe')
        sources[tf]=strict_csv(path)
    observed=datetime.fromisoformat(args.asof.replace('Z','+00:00'))
    if observed.tzinfo is None:raise ValueError('Observation requires a timezone')
    calendar=calendar_for(args.symbol)
    built=build_session_series(sources,calendar,observed)
    result=compare(built.series.get(args.timeframe,[]),strict_csv(args.reference),
                   tolerance=args.tolerance,volume=args.compare_volume)
    result.update({'symbol':args.symbol,'timeframe':args.timeframe,'session_profile':calendar.profile,
        'algorithm':built.version,'issues':built.issues,
        'required_reference_identity':'same contract, data vendor, adjustments and session mode'})
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='differences'},ensure_ascii=False))


if __name__=='__main__':main()
