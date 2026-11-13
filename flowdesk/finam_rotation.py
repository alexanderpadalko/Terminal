"""Finam-only futures roll: expiry + SAME SESSION volume AND turnover.

No date-only speculative switch. Never conflate SI silver (XCEC) with Si
USD/RUB (RTSX), or choose a different mini/micro product by prefix.
GetAsset expiration dates are authoritative; GetLastQuote supplies volume
and turnover for a same-session comparison. Persist decisions to prevent
roll-back on future starts. Perpetuals do not roll.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from .exchange_time import exchange_clock
from .finam_universe import (
    US_CONTRACTS, RU_CONTRACTS, Contract, Item, match_contract,
)

MONTHS="FGHJKMNQUVXZ"
SERIES=re.compile(rf"^([A-Z]+)([{MONTHS}])(\d{{1,4}})$")
WINDOW_HOURS=96  # weekends; never compare quotes from different sessions


def _root_and_label(contract: Contract):
    match=SERIES.fullmatch(contract.display)
    if match is None:
        return None
    return match.group(1), match.group(2), int(match.group(3))


def _roots(contract: Contract):
    found=set()
    for alias in contract.aliases:
        match=SERIES.fullmatch(alias.upper())
        if match:
            found.add(match.group(1))
    return found


def _series_year(text, reference_year):
    digits=str(text)
    if len(digits)==4:
        year=int(digits)
        return year if 2000<=year<=2100 else None
    if len(digits)==2:
        return 2000+int(digits)
    if len(digits)==1:
        return reference_year+(int(digits)-reference_year%10)%10
    return None


def _date(value):
    if isinstance(value,dict):
        try:
            return date(int(value["year"]),int(value["month"]),int(value["day"]))
        except (ValueError,TypeError,KeyError):
            return None
    if isinstance(value,str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def expiry_date(asset):
    """Only metadata, never guess an exchange settlement day from month code."""
    future=asset.get("future_details") or {}
    return _date(future.get("expiration_date")) or _date(asset.get("expiration_date"))


def _num(value):
    if isinstance(value,dict):
        value=value.get("value")
    try:
        val=Decimal(str(value))
        return val if val.is_finite() and val>=0 else None
    except (TypeError, ValueError, InvalidOperation):
        return None


def _stamp(value):
    if not isinstance(value,str):
        return None
    try:
        d=datetime.fromisoformat(value.replace("Z","+00:00"))
        return d.astimezone(timezone.utc) if d.tzinfo is not None else None
    except ValueError:
        return None


def _quote_stats(payload):
    q=payload.get("quote",payload)
    if not isinstance(q,dict):
        return None
    volume,turnover=_num(q.get("volume")),_num(q.get("turnover"))
    when=_stamp(q.get("timestamp"))
    if volume is None or turnover is None or when is None:
        return None
    return when,volume,turnover


def dominates(current, following, *, mic, symbol, now):
    """Require both indicators larger for quotes of the SAME venue session."""
    old,new=_quote_stats(current),_quote_stats(following)
    if old is None or new is None:
        return False
    old_time,old_vol,old_turn=old
    new_time,new_vol,new_turn=new
    now=now.astimezone(timezone.utc)
    if any(t>now+timedelta(minutes=5) or now-t>timedelta(hours=WINDOW_HOURS)
           for t in (old_time,new_time)):
        return False
    if abs(old_time-new_time)>timedelta(hours=6):
        return False
    clock=exchange_clock(mic,symbol)
    old_session=clock.vwap_session_key(old_time)
    new_session=clock.vwap_session_key(new_time)
    return (old_session is not None and old_session==new_session and
            new_vol>old_vol and new_turn>old_turn and
            new_vol>0 and new_turn>0)


@dataclass(frozen=True)
class RollResult:
    item: Item
    state_symbol: str
    reason: str


def candidates_for(contract, assets, *, reference_year):
    """Full root + exact month code + year; reject MIC/type/ambiguous aliases."""
    roots=_roots(contract)
    detected={}
    for row in assets:
        if not isinstance(row,dict) or row.get("is_archived"):
            continue
        mic=str(row.get("mic") or "").upper()
        if mic not in contract.mics:
            continue
        typ=str(row.get("type") or "")
        if typ and "FUT" not in typ.upper():
            continue
        ticker=str(row.get("ticker") or "").upper()
        match=SERIES.fullmatch(ticker)
        if match is None or match.group(1) not in roots:
            continue
        year=_series_year(match.group(3),reference_year)
        month=match.group(2)
        if year is None:
            continue
        symbol=str(row.get("symbol") or (str(row.get("ticker"))+"@"+mic))
        if symbol.upper()!=ticker+"@"+mic:
            continue
        key=(year, MONTHS.index(month),mic)
        detected.setdefault(key,[]).append(symbol)
    return {k:v[0] for k,v in detected.items() if len(v)==1}


def _preferred_candidate(contract, eligible, saved):
    persisted=saved.upper()
    for expiry,symbol in eligible:
        if symbol.upper()==persisted:
            return expiry,symbol
    seed={alias.upper() for alias in contract.aliases}
    for expiry,symbol in eligible:
        if symbol.split("@",1)[0].upper() in seed:
            return expiry,symbol
    return eligible[0] if eligible else None


def resolve_contract(contract, assets, api, *, today, now, saved="", cancelled=lambda:False,
                     quote_fn=None):
    """Resolve actual tenor by Finam expiry; compare nearest following only.

    No verified next or no comparable two-volume/two-turnover quotes -> keep
    last *unexpired* selection. An expired contract never remains clickable.
    """
    if _root_and_label(contract) is None:
        symbol=match_contract(contract,assets)
        return RollResult(
            Item(contract.display,contract.name,contract.group,symbol,bool(symbol),
                 "без экспирации"),symbol,"perpetual")
    entries=candidates_for(contract,assets,reference_year=today.year)
    lookup={}
    for row in assets:
        if isinstance(row,dict):
            symbol=str(row.get("symbol") or
                       (str(row.get("ticker",""))+"@"+str(row.get("mic",""))))
            lookup[symbol.upper()]=row
    verified=[]
    for key,symbol in sorted(entries.items()):
        if cancelled():
            raise InterruptedError()
        expiry=expiry_date(lookup.get(symbol.upper(),{}))
        if expiry is None:
            try:
                expiry=expiry_date(api.asset_details(symbol))
            except Exception:
                continue
        if expiry is None or expiry<today:
            continue
        verified.append((expiry,symbol))
    verified.sort(key=lambda x:(x[0],x[1]))
    chosen=_preferred_candidate(contract,verified,saved) if verified else None
    if chosen is None:
        return RollResult(Item(contract.display,contract.name,contract.group,
                               "",False,"нет действующей серии с проверенной датой экспирации"),
                          "","нет действующего контракта")
    expiry,current=chosen
    following=next(((end,sym) for end,sym in verified if end>expiry),None)
    reason="действует до "+expiry.isoformat()
    if following is not None:
        follow_expiry,follow=following
        try:
            getter=quote_fn or api.latest_quote
            cur=getter(current)
            nxt=getter(follow)
            if dominates(cur,nxt,mic=current.split("@")[1],symbol=current.split("@")[0],now=now):
                current=follow
                expiry=follow_expiry
                reason="переход по большему объёму и обороту; до "+expiry.isoformat()
        except Exception:
            reason="котировки для сравнения недоступны; действует до "+expiry.isoformat()
    root,_,_=_root_and_label(contract)
    match=SERIES.fullmatch(current.split("@",1)[0].upper())
    label=(root+match.group(2)+str(expiry.year)) if match else contract.display
    return RollResult(Item(label,contract.name,contract.group,current,True,reason),
                      current,reason)


def resolve_futures(assets,api,*,today=None,now=None,previous=None,moex_volume=None,
                    cancelled=lambda:False,progress=lambda msg:None):
    """(overrides, selections, diagnostics) keyed by original display/group."""
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today=today or now.date()
    previous=previous or {}
    overrides={}
    persisted={}
    diagnostics=[]
    for contract in (*US_CONTRACTS,*RU_CONTRACTS):
        if cancelled():
            raise InterruptedError()
        key=contract.group+":"+contract.display
        trading_date=today or now.astimezone(exchange_clock(contract.mics[0]).zone).date()
        try:
            quote_fn=(moex_volume.quote if moex_volume is not None else
                      (lambda _symbol: (_ for _ in ()).throw(
                          ValueError("MOEX RFUD недоступна")))) if contract.group=="ru" else api.latest_quote
            result=resolve_contract(contract,assets,api,today=trading_date,now=now,
                                    saved=previous.get(key,""),cancelled=cancelled,
                                    quote_fn=quote_fn)
        except InterruptedError:
            raise
        except Exception:
            # A failed per-contract metadata lookup never rolls to a guess.
            result=RollResult(Item(contract.display,contract.name,contract.group,
                                   "",False,"ошибка проверки Finam"),"",
                              "ошибка проверки Finam")
        overrides[key]=result.item
        if result.state_symbol:
            persisted[key]=result.state_symbol
        diagnostics.append(contract.display+": "+result.reason)
        progress("Проверка фьючерсов "+str(len(diagnostics))+"/18")
    return overrides,persisted,diagnostics


class RollState:
    def __init__(self,path=None):
        self.path=Path(path or Path.home()/".flowdesk"/"finam_roll_v1.json")

    def load(self):
        try:
            value=json.loads(self.path.read_text(encoding="utf-8"))
            return {str(k):str(v) for k,v in value.get("selections",{}).items()
                    if isinstance(k,str) and isinstance(v,str)}
        except (OSError,ValueError,TypeError,AttributeError):
            return {}

    def save(self,value):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"schema":1,"selections":value},
                                   ensure_ascii=False),encoding="utf-8")
        temp.replace(self.path)
