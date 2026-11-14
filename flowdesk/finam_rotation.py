"""Read-only futures roll exactly one calendar day before verified expiry.

The future and next contract MUST both be present in Finam's active catalog.
The date comes from GetAsset (all documented Date/Timestamp variants), with
MOEX RFUD LASTTRADEDATE as a fallback for Russian contracts only.
Never use turnover/volume to trigger an early switch. A contract with missing
expiry is still selectable if its *exact* symbol is in the active catalog,
but automatic rollover is suspended until the date becomes verifiable.
Previous roll decisions persist; perpetuals never roll.
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
        # Include last year's single-digit contracts near New Year; do not
        # turn an active Z6 into 2036 when started in January 2027.
        base=reference_year-1
        return base+(int(digits)-base%10)%10
    return None


def _date(value, *, mic=""):
    """Parse Finam google.type.Date, RFC3339, and google.protobuf.Timestamp.

    Timestamp denotes an instant: use exchange-local date, not the UTC date,
    to avoid rolling on a wrong day across midnight/DST.
    """
    if isinstance(value,datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(exchange_clock(mic).zone).date()
    if isinstance(value,date):
        return value
    if isinstance(value,dict):
        if all(k in value for k in ("year","month","day")):
            try:
                return date(int(value["year"]),int(value["month"]),int(value["day"]))
            except (ValueError,TypeError,OverflowError):
                return None
        if "seconds" in value:
            try:
                seconds=int(value["seconds"])
                nanos=int(value.get("nanos",0))
                if seconds<=0 or not 0<=nanos<1_000_000_000:
                    return None  # empty protobuf Timestamp, not an expiry
                stamp=datetime.fromtimestamp(seconds+nanos/1e9,tz=timezone.utc)
                return stamp.astimezone(exchange_clock(mic).zone).date()
            except (ValueError,TypeError,OverflowError,OSError):
                return None
        return None
    if isinstance(value,str):
        try:
            stamp=datetime.fromisoformat(value.replace("Z","+00:00"))
            if stamp.tzinfo is not None:
                return stamp.astimezone(exchange_clock(mic).zone).date()
        except (ValueError,TypeError):
            return None
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def expiry_date(asset, *, mic=""):
    """Read both old and current Finam GetAsset response shapes."""
    if not isinstance(asset,dict):
        return None
    details=asset.get("asset_details") or {}
    if not isinstance(details,dict):
        details={}
    # Explicit calendar dates are not instants to shift across time zones.
    futures=(asset,details,asset.get("future_details"),details.get("future_details"))
    for source in futures:
        if isinstance(source,dict):
            found=_date(source.get("expiration_date"),mic=mic)
            if found is not None:
                return found
    return None



def expiry_absence(asset):
    """Safe explanation of missing Finam expiry, without logging API values."""
    if not isinstance(asset,dict):
        return "Finam GetAsset: неверная структура ответа"
    details=asset.get("asset_details")
    sources=[asset,asset.get("future_details"),details]
    if isinstance(details,dict):
        sources.append(details.get("future_details"))
    values=[source["expiration_date"] for source in sources
            if isinstance(source,dict) and "expiration_date" in source]
    if not values:
        return "Finam GetAsset: поле даты отсутствует"
    if all(value is None or value=="" or value=={} or value==0 or
           value=={"seconds":"0","nanos":0} or
           value=={"seconds":0,"nanos":0} for value in values):
        return "Finam GetAsset: поле даты пустое"
    return "Finam GetAsset: дата в неподдерживаемом или недопустимом формате"


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


def resolve_contract(contract, assets, api, *, today, now, saved="", cancelled=lambda:False):
    """Select current series until the last calendar day before expiry.

    Date-based rollover has priority over any quote statistics. Unknown expiry
    permits displaying ONLY the exact active contract; it never licenses a
    speculative move to a different tenor.
    """
    if _root_and_label(contract) is None:
        symbol=match_contract(contract,assets)
        return RollResult(
            Item(contract.display,contract.name,contract.group,symbol,bool(symbol),
                 "вечный контракт, без ролловера"),symbol,"perpetual")

    entries=candidates_for(contract,assets,reference_year=today.year)
    root,month,contract_year=_root_and_label(contract)
    floor=(contract_year,MONTHS.index(month))
    saved_series=SERIES.fullmatch(saved.split("@",1)[0].upper())
    if saved_series is not None and saved_series.group(1) in _roots(contract):
        saved_year=_series_year(saved_series.group(3),today.year)
        if saved_year is not None:
            floor=(saved_year,MONTHS.index(saved_series.group(2)))
    # Only the current tenor and a bounded number of successive series
    # matter for rolling. Avoid exhausting Finam's 200 requests/minute quota.
    entries=dict(list((k,v) for k,v in sorted(entries.items())
                      if k[:2]>=floor)[:6])
    lookup={}
    for row in assets:
        if isinstance(row,dict):
            symbol=str(row.get("symbol") or
                       (str(row.get("ticker",""))+"@"+str(row.get("mic",""))))
            lookup[symbol.upper()]=row

    verified=[]
    unknown=[]
    errors={}
    sources={}
    initial=match_contract(contract,assets)
    preferred=next((s for s in entries.values() if s.upper()==saved.upper()),None)
    if preferred is None:
        preferred=next((s for s in entries.values() if s.upper()==initial.upper()),None)
    ordered=sorted(entries.items(),key=lambda pair:(pair[1]!=preferred,pair[0]))
    for key,symbol in ordered:
        if cancelled():
            raise InterruptedError()
        mic=symbol.rsplit("@",1)[-1]
        # Full GetAsset has the authoritative Finam expiry, while the
        # paginated list often omits its future_details.
        expiry=expiry_date(lookup.get(symbol.upper(),{}),mic=mic)
        if expiry is not None:
            sources[symbol.upper()]="Finam каталог"
        if expiry is None:
            try:
                payload=api.asset_details(symbol)
                expiry=expiry_date(payload,mic=mic)
                if expiry is None:
                    errors[symbol.upper()]=expiry_absence(payload)
                else:
                    sources[symbol.upper()]="Finam GetAsset"
            except InterruptedError:
                raise
            except Exception as exc:
                code=getattr(exc,"http_status",None)
                errors[symbol.upper()]=(
                    "Finam GetAsset: HTTP "+str(code)
                    if isinstance(code,int) else
                    "Finam GetAsset: "+type(exc).__name__)
        if expiry is None:
            unknown.append(symbol)
        elif expiry>=today:
            verified.append((expiry,symbol))
        if symbol==preferred and (expiry is None or today<expiry-timedelta(days=1)):
            # The selected series has not reached its roll day, or its date is
            # unknown and rolling is forbidden. Do not query five distant cards.
            break
    verified.sort(key=lambda x:(x[0],x[1]))

    # A missing date for the currently active ticker must NOT force an
    # unrequested jump to a later contract whose date happens to be known.
    allowed={sym.upper():sym for sym in unknown}
    active_verified={sym.upper() for _,sym in verified}
    present=allowed.get(saved.upper())
    if present is None and initial.upper() not in active_verified:
        present=allowed.get(initial.upper())
    if present is not None:
        detail=errors.get(present.upper(),"нет подтверждённой даты")
        status="экспирация не подтверждена; автопереход приостановлен; "+detail
        return RollResult(
            Item(contract.display,contract.name,contract.group,present,True,status),
            present,status)

    chosen=_preferred_candidate(contract,verified,saved)
    if chosen is None:
        return RollResult(
            Item(contract.display,contract.name,contract.group,"",False,
                 "нет подтверждённого действующего контракта"),
            "","нет подтверждённого действующего контракта")

    expiry,current=chosen
    reason="действует до "+expiry.isoformat()+" · "+sources.get(current.upper(),"данные источника")
    venue=current.rsplit("@",1)[-1].upper()
    next_known=next(((end,sym) for end,sym in verified
                     if end>expiry and sym.rsplit("@",1)[-1].upper()==venue),None)
    if today>=expiry-timedelta(days=1):
        if next_known is not None:
            expiry,current=next_known
            reason=("переход за день до экспирации; новая дата "+expiry.isoformat()+
                    " · "+sources.get(current.upper(),"данные источника"))
        else:
            reason="экспирация "+expiry.isoformat()+"; следующий контракт не подтверждён"

    root,_,_=_root_and_label(contract)
    match=SERIES.fullmatch(current.split("@",1)[0].upper())
    series_year=_series_year(match.group(3),today.year) if match else None
    label=(root+match.group(2)+str(series_year)) if series_year else contract.display
    return RollResult(Item(label,contract.name,contract.group,current,True,reason),
                      current,reason)


def resolve_futures(assets,api,*,today=None,now=None,previous=None,cancelled=lambda:False,
                    progress=lambda msg:None):
    """Resolve 8 US + 10 RU rows using each exchange's local calendar date.

    Only Finam dates authorize rolling. No external expiry or liquidity feed.
    """
    now=now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    previous=previous or {}
    overrides={}
    persisted={}
    diagnostics=[]
    for contract in (*US_CONTRACTS,*RU_CONTRACTS):
        if cancelled():
            raise InterruptedError()
        key=contract.group+":"+contract.display
        local_day=today if today is not None else now.astimezone(
            exchange_clock(contract.mics[0]).zone).date()
        try:
            result=resolve_contract(contract,assets,api,today=local_day,now=now,
                                    saved=previous.get(key,""),cancelled=cancelled)
        except InterruptedError:
            raise
        except Exception:
            result=RollResult(
                Item(contract.display,contract.name,contract.group,"",False,
                     "ошибка проверки Finam"),
                "","ошибка проверки Finam")
        overrides[key]=result.item
        if result.state_symbol:
            persisted[key]=result.state_symbol
        diagnostics.append(contract.display+": "+result.reason)
        progress("Проверка экспирации фьючерсов "+str(len(diagnostics))+"/18")
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
