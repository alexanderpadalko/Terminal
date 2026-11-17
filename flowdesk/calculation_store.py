"""Bounded, disposable calculation snapshots. JSON only: never executable pickle.

Source candles stay in their existing database. A bad, stale, incompatible or
missing snapshot is a cache miss and always falls back to the original replay.
"""
from collections import deque
from contextlib import closing
from dataclasses import fields, is_dataclass
from datetime import date,datetime
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3
import sys
import threading
import time
import zlib

SCHEMA=2
MAX_ENTRY=32*1024*1024
MAX_RAW=128*1024*1024
MAX_TOTAL=256*1024*1024
MAX_ENTRIES=128
MODEL_FILES=('engine.py','reference_engine.py','reference_runtime.py','v214_working_fvg.py',
             'replay_checkpoint.py','liquidity.py','pipeline.py','history_analysis.py',
             'market_data.py','settings.py','terminal_policy.py','finam_market.py','calculation_store.py',
             'data/reference_v211.json','data/settings_v211.json')


@lru_cache(maxsize=1)
def model_version():
    folder=Path(__file__).parent
    if getattr(sys,'frozen',False) or not (folder/'engine.py').is_file():
        data=json.loads((folder/'data/calculation_model.json').read_text(encoding='utf-8'))
        if data.get('schema')!=SCHEMA or len(data.get('sha256',''))!=64:raise ValueError('Calculation model')
        return data['sha256']
    digest=hashlib.sha256(f'{SCHEMA}:{sys.version_info[:2]}'.encode())
    for name in MODEL_FILES:digest.update((folder/name).read_text(encoding='utf-8').encode())
    return digest.hexdigest()


@lru_cache(maxsize=1)
def registry():
    from .engine import Analysis,Candle,Segment,FVG,PairSnapshot,FlowState,StructureEngine,FVGEngine,PairEngine
    from .liquidity import Capture,CaptureState
    from .reference_engine import PinnedFlowEngine
    from .reference_runtime import Record
    from .replay_checkpoint import ReplayCheckpoint
    return {cls.__name__:cls for cls in (Analysis,Candle,Segment,FVG,PairSnapshot,FlowState,
        Capture,CaptureState,ReplayCheckpoint,StructureEngine,FVGEngine,PairEngine,PinnedFlowEngine,Record)}


@lru_cache(maxsize=None)
def field_names(cls):return tuple(f.name for f in fields(cls))


def encode(value):
    from .reference_runtime import ReferenceContext
    nodes=[];memo={}
    def put(item):
        if item is None or type(item) in (bool,int,float,str):return item
        if isinstance(item,datetime):
            stamp={'time':item.isoformat()}
            zone=getattr(item.tzinfo,'key',None)
            if zone:stamp['zone']=zone
            if item.fold:stamp['fold']=item.fold
            return stamp
        if isinstance(item,date):return {'day':item.isoformat()}
        if id(item) in memo:return {'ref':memo[id(item)][0]}
        index=len(nodes);memo[id(item)]=(index,item);nodes.append(None)
        if type(item) in (list,tuple,deque):
            node=[type(item).__name__,[put(v) for v in item]]
            if isinstance(item,deque):node.append(item.maxlen)
        elif type(item) is dict:
            node=['dict',[[put(k),put(v)] for k,v in item.items()]]
        elif isinstance(item,ReferenceContext):
            # observe() reconstructs transient OHLC/timeframe helpers before
            # the next step; only numeric state/history must survive restart.
            node=['context',put({'tf':item.tf,'settings':item.settings.as_dict(),
                'states':item.states,'history':item.history,'index':item.index,'tick':item.tick})]
        else:
            name=type(item).__name__
            if registry().get(name) is not type(item):raise ValueError('Unsupported calculation state')
            if is_dataclass(item):node=[name,[put(getattr(item,key)) for key in field_names(type(item))]]
            else:node=[name,put(vars(item))]
        nodes[index]=node
        return {'ref':index}
    root=put(value)
    return json.dumps({'schema':SCHEMA,'root':root,'nodes':nodes},ensure_ascii=False,
                      allow_nan=False,separators=(',',':')).encode()


def decode(raw):
    from .reference_runtime import ReferenceContext
    from .settings import IndicatorSettings
    payload=json.loads(raw)
    if payload['schema']!=SCHEMA or len(payload['nodes'])>1500000:raise ValueError('Snapshot schema')
    nodes=payload['nodes'];memo={};pending=set();strings={};times={}
    def get(item):
        if type(item) is str:return strings.setdefault(item,item)
        if item is None or type(item) in (bool,int,float):return item
        if type(item) is not dict:raise ValueError('Snapshot value')
        if 'time' in item and set(item)<={'time','zone','fold'}:
            value=item['time']
            key=(value,item.get('zone'),item.get('fold',0))
            if key not in times:
                stamp=datetime.fromisoformat(value)
                if item.get('zone'):stamp=stamp.astimezone(ZoneInfo(item['zone']))
                if item.get('fold'):stamp=stamp.replace(fold=item['fold'])
                times[key]=stamp
            return times[key]
        if set(item)=={'day'}:return date.fromisoformat(item['day'])
        if set(item)!={'ref'} or type(item['ref']) is not int:raise ValueError('Snapshot reference')
        index=item['ref']
        if not 0<=index<len(nodes):raise ValueError('Snapshot index')
        if index in memo:return memo[index]
        if index in pending:raise ValueError('Immutable snapshot cycle')
        pending.add(index)
        kind,data,*extra=nodes[index]
        if kind in ('list','dict','deque'):
            result=[] if kind=='list' else {} if kind=='dict' else deque(maxlen=extra[0])
            memo[index]=result
            if kind=='dict':result.update((get(k),get(v)) for k,v in data)
            else:result.extend(get(v) for v in data)
        elif kind=='tuple':result=tuple(get(v) for v in data)
        elif kind=='context':
            state=get(data)
            result=ReferenceContext(state['tf'],IndicatorSettings(state['settings']))
            for key in ('states','history','index','tick'):setattr(result,key,state[key])
        else:
            cls=registry().get(kind)
            if cls is None:raise ValueError('Snapshot type')
            result=cls.__new__(cls);memo[index]=result
            if is_dataclass(cls):
                names=field_names(cls)
                if type(data) is not list or len(data)!=len(names):raise ValueError('Snapshot fields')
                values={name:get(value) for name,value in zip(names,data)}
            else:values=get(data)
            for key,value in values.items():
                if not isinstance(key,str) or key.startswith('__'):raise ValueError('Snapshot attribute')
                object.__setattr__(result,key,value)
        memo[index]=result;pending.remove(index)
        return result
    return get(payload['root'])


class CalculationStore:
    def __init__(self,path):
        self.path=Path(path);self._lock=threading.Lock();self._saved={};self._attempted={}

    def _connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=.25)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS snapshots
                (symbol TEXT,tf TEXT,model TEXT,digest TEXT,payload BLOB,touched REAL,
                 PRIMARY KEY(symbol,tf))''')
        except Exception:
            db.close()
            raise
        return db

    def load(self,symbol,tf):
        try:
            with self._lock,closing(self._connect()) as db:
                row=db.execute('SELECT model,digest,payload FROM snapshots WHERE symbol=? AND tf=?',(symbol,tf)).fetchone()
            if not row or row[0]!=model_version() or len(row[2])>MAX_ENTRY:return None
            if hashlib.sha256(row[2]).hexdigest()!=row[1]:return None
            stream=zlib.decompressobj();raw=stream.decompress(row[2],MAX_RAW+1)
            if len(raw)>MAX_RAW or stream.unconsumed_tail or not stream.eof:return None
            value=decode(raw)
            from .engine import Analysis
            key,result=value
            if (type(key) is not tuple or len(key)!=4 or type(result) is not tuple or
                    len(result)!=2 or not isinstance(result[0],Analysis) or
                    result[0].timeframe!=tf or type(result[1]) is not dict):return None
            return value
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,IndexError,AttributeError,OverflowError,RecursionError,zlib.error):
            return None

    def save(self,symbol,tf,key,result):
        from .engine import Analysis
        if not isinstance(result[0],Analysis):return
        identity=(symbol,tf);now=time.monotonic()
        with self._lock:
            if now-self._attempted.get(identity,-float('inf'))<30:return
            self._attempted[identity]=now
        # Live-only changes do not rewrite a large snapshot every second.
        from .market_data import candle_close_time
        sources={result[0].timeframe:result[0],**result[1]}
        closed=tuple((frame,tuple(b for b in item.candles if item.asof is None or
                      candle_close_time(b,frame)<=item.asof)) for frame,item in sorted(sources.items()))
        signature=hashlib.sha256(repr((key[0],key[-1],closed,result[0].session_info)).encode()).hexdigest()
        if self._saved.get(identity)==signature:return
        try:
            raw=encode((key,result))
            if len(raw)>MAX_RAW:return
            payload=zlib.compress(raw,level=1)
            if len(payload)>MAX_ENTRY:return
            with self._lock,closing(self._connect()) as db,db:
                db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?)',
                           (symbol,tf,model_version(),hashlib.sha256(payload).hexdigest(),payload,time.time()))
                rows=db.execute('SELECT symbol,tf,LENGTH(payload) FROM snapshots ORDER BY touched DESC').fetchall()
                total=0
                for i,(name,frame,size) in enumerate(rows):
                    total+=size
                    if i>=MAX_ENTRIES or total>MAX_TOTAL:
                        db.execute('DELETE FROM snapshots WHERE symbol=? AND tf=?',(name,frame))
            self._saved[identity]=signature
        except (OSError,sqlite3.Error,ValueError,TypeError,OverflowError,RecursionError):
            pass  # A derived cache can never make source data unavailable.
