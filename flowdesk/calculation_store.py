"""Bounded, disposable calculation snapshots. JSON only: never executable pickle.

Source candles stay in their existing database. A bad, stale, incompatible or
missing snapshot is a cache miss and always falls back to the original replay.
"""
from array import array
from collections import deque
from contextlib import closing
from dataclasses import fields, is_dataclass
from datetime import date,datetime
from functools import lru_cache
import hashlib
from itertools import islice
import json
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3
import sys
import threading
import time
import zlib

SCHEMA=3
MAX_ENTRY=32*1024*1024
MAX_RAW=128*1024*1024
MAX_TOTAL=256*1024*1024
MAX_ENTRIES=128
MAX_NODES=1_500_000
JSON_BATCH=1024
COMPRESSION_BATCH=256*1024
MODEL_FILES=('engine.py','reference_engine.py','reference_runtime.py','v214_working_fvg.py',
             'replay_checkpoint.py','liquidity.py','pipeline.py','history_analysis.py',
             'market_data.py','settings.py','terminal_policy.py','finam_market.py','chart_source_cache.py',
             'session_store.py','calculation_store.py',
             'data/reference_v211.json','data/settings_v211.json')


class SnapshotTooLarge(ValueError):
    """The derived graph exceeded this store's limits during encoding."""


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError()


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


def _encoded_records(value):
    """Write one JSON record per object, retaining references instead of a second graph."""
    from .reference_runtime import ReferenceContext
    dumps=json.JSONEncoder(ensure_ascii=False,allow_nan=False,separators=(',',':')).encode
    items=[]
    indexes={}

    def reference(item):
        if item is None or type(item) in (bool,int,float,str):return item
        if isinstance(item,datetime):
            stamp={'time':item.isoformat()}
            zone=getattr(item.tzinfo,'key',None)
            if zone:stamp['zone']=zone
            if item.fold:stamp['fold']=item.fold
            return stamp
        if isinstance(item,date):return {'day':item.isoformat()}
        identity=id(item)
        index=indexes.get(identity)
        if index is None:
            index=len(items)
            if index>=MAX_NODES:raise SnapshotTooLarge('Calculation snapshot contains too many objects')
            indexes[identity]=index
            # Keep identities alive for shared references, including generated
            # context dictionaries that were not in the caller's graph.
            items.append(item)
        return {'ref':index}

    def record(kind,values,length,extra=()):
        if length<=JSON_BATCH:
            yield (dumps([kind,list(values),*extra])+'\n').encode()
            return
        yield ('['+dumps(kind)+',[').encode()
        first=True
        while batch:=list(islice(values,JSON_BATCH)):
            if not first:
                yield b','
            yield dumps(batch)[1:-1].encode()
            first=False
        yield (']'+''.join(','+dumps(v) for v in extra)+']\n').encode()

    yield (dumps({'schema':SCHEMA,'root':reference(value)})+'\n').encode()
    index=0
    while index<len(items):
        item=items[index]
        index+=1
        if type(item) in (list,tuple,deque):
            extra=(item.maxlen,) if isinstance(item,deque) else ()
            yield from record(type(item).__name__,(reference(v) for v in item),len(item),extra)
            continue
        elif type(item) is dict:
            yield from record('dict',([reference(k),reference(v)] for k,v in item.items()),len(item))
            continue
        elif isinstance(item,ReferenceContext):
            # observe() reconstructs transient OHLC/timeframe helpers before
            # the next step; only numeric state/history must survive restart.
            node=['context',reference({'tf':item.tf,'settings':item.settings.as_dict(),
                'states':item.states,'history':item.history,'index':item.index,'tick':item.tick})]
        else:
            name=type(item).__name__
            if registry().get(name) is not type(item):raise ValueError('Unsupported calculation state')
            if is_dataclass(item):node=[name,[reference(getattr(item,key)) for key in field_names(type(item))]]
            else:node=[name,reference(vars(item))]
        yield (dumps(node)+'\n').encode()


def encode(value, *, compressed=False, cancelled=None):
    """Enforce size limits while writing, before allocating oversized snapshots."""
    output=bytearray()
    buffer=bytearray()
    zipper=zlib.compressobj(level=1) if compressed else None
    raw_size=0
    for index, chunk in enumerate(_encoded_records(value)):
        if index % JSON_BATCH == 0:
            _check_cancelled(cancelled)
        raw_size+=len(chunk)
        if raw_size>MAX_RAW:raise SnapshotTooLarge('Calculation snapshot exceeds the raw limit')
        if zipper is None:
            output.extend(chunk)
            continue
        buffer.extend(chunk)
        if len(buffer)>=COMPRESSION_BATCH:
            output.extend(zipper.compress(buffer))
            buffer.clear()
            if len(output)>MAX_ENTRY:raise SnapshotTooLarge('Calculation snapshot exceeds the disk limit')
    _check_cancelled(cancelled)
    if zipper is not None:
        output.extend(zipper.compress(buffer))
        output.extend(zipper.flush())
        if len(output)>MAX_ENTRY:raise SnapshotTooLarge('Calculation snapshot exceeds the disk limit')
    return bytes(output)


def decode(raw, *, cancelled=None):
    from .reference_runtime import ReferenceContext
    from .settings import IndicatorSettings
    _check_cancelled(cancelled)
    if isinstance(raw,str):raw=raw.encode()
    if len(raw)>MAX_RAW:raise ValueError('Snapshot exceeds the raw limit')
    header_end=raw.find(b'\n')
    if header_end<0:raise ValueError('Snapshot header')
    payload=json.loads(raw[:header_end])
    if payload['schema']!=SCHEMA:raise ValueError('Snapshot schema')
    # Small offsets let the reader decode only the requested node, so it
    # never holds an entire parsed JSON tree beside the restored graph.
    offsets=array('I',[header_end+1])
    while offsets[-1]<len(raw):
        if len(offsets) % JSON_BATCH == 0:
            _check_cancelled(cancelled)
        if len(offsets)>MAX_NODES:raise ValueError('Snapshot has too many objects')
        end=raw.find(b'\n',offsets[-1])
        if end<0:raise ValueError('Truncated snapshot')
        offsets.append(end+1)
    memo={};pending=set();strings={};times={}
    records_read = 0
    def get(item):
        nonlocal records_read
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
        if not 0<=index<len(offsets)-1:raise ValueError('Snapshot index')
        if index in memo:return memo[index]
        if index in pending:raise ValueError('Immutable snapshot cycle')
        records_read += 1
        if records_read % JSON_BATCH == 0:
            _check_cancelled(cancelled)
        pending.add(index)
        kind,data,*extra=json.loads(raw[offsets[index]:offsets[index+1]])
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
    try:
        result = get(payload['root'])
        _check_cancelled(cancelled)
        return result
    finally:get=None  # Do not retain the decoded graph until cyclic GC runs.


class CalculationStore:
    def __init__(self,path):
        self.path=Path(path);self._lock=threading.Lock();self._handled={};self._attempted={}

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

    def load(self,symbol,tf,*,cancelled=None):
        try:
            _check_cancelled(cancelled)
            with self._lock,closing(self._connect()) as db:
                row=db.execute('SELECT model,digest,payload FROM snapshots WHERE symbol=? AND tf=?',(symbol,tf)).fetchone()
            _check_cancelled(cancelled)
            if not row or row[0]!=model_version() or len(row[2])>MAX_ENTRY:return None
            if hashlib.sha256(row[2]).hexdigest()!=row[1]:return None
            stream=zlib.decompressobj();raw=stream.decompress(row[2],MAX_RAW+1)
            if len(raw)>MAX_RAW or stream.unconsumed_tail or not stream.eof:return None
            value=decode(raw,cancelled=cancelled)
            from .engine import Analysis
            key,result=value
            if (type(key) is not tuple or len(key)!=4 or type(result) is not tuple or
                    len(result)!=2 or not isinstance(result[0],Analysis) or
                    result[0].timeframe!=tf or type(result[1]) is not dict):return None
            return value
        except InterruptedError:
            raise
        except (OSError,sqlite3.Error,ValueError,TypeError,KeyError,IndexError,AttributeError,OverflowError,RecursionError,zlib.error):
            return None

    def save(self,symbol,tf,key,result,*,cancelled=None):
        from .engine import Analysis
        _check_cancelled(cancelled)
        if not isinstance(result[0],Analysis):return
        identity=(symbol,tf);now=time.monotonic()
        with self._lock:
            if now-self._attempted.get(identity,-float('inf'))<30:return
            self._attempted[identity]=now
        from .market_data import candle_close_time
        try:
            # Live-only changes do not rewrite a large snapshot every second.
            sources={result[0].timeframe:result[0],**result[1]}
            digest=hashlib.sha256(repr((key[0],key[-1],result[0].session_info)).encode())
            for frame,item in sorted(sources.items()):
                digest.update(frame.encode()+b'\0')
                for index,bar in enumerate(item.candles):
                    if index % JSON_BATCH == 0:
                        _check_cancelled(cancelled)
                    if item.asof is None or candle_close_time(bar,frame)<=item.asof:
                        digest.update(repr(bar).encode()+b'\0')
            signature=digest.hexdigest()
            if self._handled.get(identity)==signature:return
            payload=encode((key,result),compressed=True,cancelled=cancelled)
            with self._lock,closing(self._connect()) as db,db:
                _check_cancelled(cancelled)
                db.execute('INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?,?,?)',
                           (symbol,tf,model_version(),hashlib.sha256(payload).hexdigest(),payload,time.time()))
                rows=db.execute('SELECT symbol,tf,LENGTH(payload) FROM snapshots ORDER BY touched DESC').fetchall()
                total=0
                for i,(name,frame,size) in enumerate(rows):
                    total+=size
                    if i>=MAX_ENTRIES or total>MAX_TOTAL:
                        db.execute('DELETE FROM snapshots WHERE symbol=? AND tf=?',(name,frame))
                _check_cancelled(cancelled)
            self._handled[identity]=signature
        except InterruptedError:
            # A later request for the same chart can retry immediately.
            with self._lock:
                if self._attempted.get(identity) == now:
                    del self._attempted[identity]
            raise
        except SnapshotTooLarge:
            # A changing draft alone should not repeatedly serialize a
            # closed history that is already too large to persist.
            self._handled[identity]=signature
        except (OSError,sqlite3.Error,ValueError,TypeError,OverflowError,RecursionError):
            pass  # A derived cache can never make source data unavailable.
