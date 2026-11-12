"""Pinned numeric v2.11 functions with per-source persistent state.

Generated blocks are shipped application code. No user expressions are run.
OHLCV transport and source confirmation remain explicit in the Python bridge.
"""
from collections import deque
from copy import copy, deepcopy
from datetime import datetime
from functools import lru_cache
import ast
import json
import math as pymath
import operator
from pathlib import Path
from types import SimpleNamespace as NS

from .market_data import MOSCOW, candle_close_time
from .price_format import price_text


def compare(a,b,operation):
    if a is None or b is None:
        return False
    if operation=='Lt': return a<b
    if operation=='LtE': return a<=b
    if operation=='Gt': return a>b
    if operation=='GtE': return a>=b
    if operation=='Eq': return a==b
    if operation=='NotEq': return a!=b
    raise ValueError(operation)


def inclusive(a,b):
    return range(a,b+1) if a<=b else range(a,b-1,-1)


def binary(a,b,operation):
    if a is None or b is None:return None
    return {'Add':operator.add,'Sub':operator.sub,'Mult':operator.mul,'Div':operator.truediv,
            'Mod':operator.mod,'Pow':operator.pow,'FloorDiv':operator.floordiv}[operation](a,b)


class Record(NS):
    def copy(self):
        return copy(self)


def constructor(fields):
    names=[name for name,_ in fields]
    defaults={name:ast.literal_eval(value) for name,value in fields if value is not None}
    def new(*values):
        if len(values)>len(names):
            raise TypeError('Too many reference record fields')
        row={**defaults,**dict(zip(names,values))}
        if set(row)!=set(names):
            raise TypeError('Missing reference record fields: '+str(set(names)-set(row)))
        return Record(**row)
    return NS(new=new)


@lru_cache(maxsize=1)
def program():
    path=Path(__file__).parent/'data/reference_v211.json'
    data=json.loads(path.read_text(encoding='utf-8'))
    assert data['sha256']=='CC2E1422F6D865C7D4814B7FF7CD4EEA82F87EB89642E8EF5F158A47B85D057F'
    functions={}
    for name,item in data['functions'].items():
        tree=ast.parse(item['code'])
        local_names={n.id for n in ast.walk(tree) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store)}
        past_names={n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call)
                    and isinstance(n.func,ast.Name) and n.func.id=='_past'}
        functions[name]=(item['parameters'],compile(tree,'<v2.11 '+name+'>','exec'),local_names,past_names)
    return data,functions,{name:compile(code,'<v2.11 '+name+'>','exec') for name,code in data['sections'].items()}


class ReferenceContext:
    def __init__(self,tf,settings=None):
        self.tf,self.settings=tf,settings
        self.states={}
        self.history={}
        self.env={}
        self.candles=[]
        self.index=-1
        self.tick=.01
        self._base={
            '__builtins__':{},'na':lambda x:x is None,
            'float':lambda x:None if x is None else float(x),
            'int':lambda x:None if x is None else int(x),
            '_inclusive':inclusive,'_compare':compare,'_binary':binary,
            'math':NS(min=min,max=max,floor=pymath.floor,ceil=pymath.ceil,
                      abs=abs,round_to_mintick=lambda v:None if v is None else pymath.floor(v/self.tick+.5)*self.tick),
            'str':NS(tostring=self._text,format_time=self._time),
            'format':NS(mintick='mintick'),
            'array':NS(new=lambda *args:[] if not args else [args[1] if len(args)>1 else None]*args[0],
                size=len,get=lambda xs,i:xs[i],set=self._set,last=lambda xs:xs[-1],
                copy=lambda xs:list(xs),push=lambda xs,v:xs.append(v),pop=lambda xs:xs.pop(),
                shift=lambda xs:xs.pop(0),remove=lambda xs,i:xs.pop(i),clear=lambda xs:xs.clear()),
        }
        self._base.update({name:constructor(fields) for name,fields in program()[0]['records'].items()})
        self._base.update({name:self._helper(name) for name in program()[1]})

    @staticmethod
    def _set(xs,i,value):
        xs[i]=value

    def _text(self,value,format_=None):
        if value is None:
            return 'NaN'
        if type(value) is bool:return 'true' if value else 'false'
        if format_=='mintick':return price_text(value,self.tick)
        return f'{value:.8f}'.rstrip('0').rstrip('.') if isinstance(value,float) else str(value)

    @staticmethod
    def _time(value,format_=None,tz=None):
        return datetime.fromtimestamp(value/1000,MOSCOW).strftime('%Y-%m-%d %H:%M') if value is not None else 'NaN'

    def _helper(self,name):
        def invoke(*args):
            return self.call(name,*args)
        return invoke

    def __deepcopy__(self,memo):
        result=ReferenceContext(self.tf,self.settings)
        memo[id(self)]=result
        result.states=deepcopy(self.states,memo)
        result.history=deepcopy(self.history,memo)
        result.env=deepcopy({k:v for k,v in self.env.items() if k not in self._base},memo)
        result.candles=self.candles
        result.index=self.index
        result.tick=self.tick
        return result

    def observe(self,candles,index):
        self.candles,self.index=candles,index
        bar=candles[index]
        seconds={'M1':60,'M5':300,'H1':3600,'H4':14400,'D1':86400,'W1':604800,'MN1':2592000}[self.tf]
        self.env.update(high=bar.high,low=bar.low,close=bar.close,open=bar.open,volume=bar.volume,
            time=int(bar.time.timestamp()*1000),time_close=int(candle_close_time(bar,self.tf).timestamp()*1000),
            bar_index=index,barstate=NS(isfirst=index==0,islast=True,isconfirmed=True,islastconfirmedhistory=True),
            timeframe=NS(in_seconds=lambda tf=None: {'60':3600,'240':14400,'D':86400,'W':604800,'M':2592000}.get(tf,seconds),
                period=self.tf,ismonthly=self.tf=='MN1',isweekly=self.tf=='W1',isdaily=self.tf=='D1'),
            fvgMaxZones=self.settings.fvgMaxZones if self.settings else 20,
            fvgExcludeGaps=self.settings.fvgExcludeGaps if self.settings else True,
            flowDiagnosticEnabled=True,syminfo=NS(mintick=self.tick))

    def _past(self,name,ago,function=None):
        if name in ('open','high','low','close','volume','time','time_close'):
            index=self.index-ago
            if index<0:
                return None
            candle=self.candles[index]
            if name=='time':return int(candle.time.timestamp()*1000)
            if name=='time_close':return int(candle_close_time(candle,self.tf).timestamp()*1000)
            return getattr(candle,name)
        values=self.history.get(function,{}).get(name,())
        return values[-ago] if len(values)>=ago else ('' if name=='higherAudit' else None)

    def call(self,name,*args):
        parameters,code,names,past=program()[1][name]
        if len(args)!=len(parameters):
            raise TypeError(name+' reference argument count')
        state=self.states.setdefault(name,{})
        namespace={**self._base,**self.env,**state,**dict(zip(parameters,args)),
                   '_has':lambda key:key in state,'_past':lambda key,ago:self._past(key,ago,name)}
        exec(code,namespace,namespace)
        state.update({key:namespace[key] for key in names if key in namespace})
        if past:
            history=self.history.setdefault(name,{})
            for key in past:
                if key not in ('open','high','low','close','volume','time','time_close'):
                    history.setdefault(key,deque(maxlen=3)).append(namespace.get(key))
        return namespace.get('_return')

    def section(self,name):
        self.env.update(self._base)
        self.env.update(_has=lambda key:key in self.env,_past=lambda key,ago:self._past(key,ago))
        exec(program()[2][name],self.env,self.env)
        return self.env
