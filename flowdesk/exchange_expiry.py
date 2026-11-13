"""Read-only official exchange expiry calendars; never derive dates from a symbol.

ICE Brent: publicly published *LTD (Last Trade Date)* on its product expiry page.
CME/COMEX/NYMEX: public CME ProductCalendar/Future JSON when the product
identifier is known. This is an optional best-effort source: CME's supported
Reference Data API requires separate access. Failed/changed schemas must NOT
produce a rollover date. No trading API or third-party quotes are involved.
"""
from __future__ import annotations

from datetime import date, datetime
from html.parser import HTMLParser
import json
import re
from urllib.request import Request, urlopen

from .finam_universe import US_CONTRACTS

CME_ROOTS = {"NQ": 209, "ES": 133, "GC": 437, "SI": 458}
# Product IDs are for CME Group *Future* calendars, never options.
CME_ROOT_VENUES = {
    "NQ": ("XCME",), "ES": ("XCME",),
    "GC": ("XCEC","XCME"), "SI": ("XCEC","XCME"),
}
ICE_URL="https://www.ice.com/products/219/brent-crude-futures/expiry"
MONTHS="FGHJKMNQUVXZ"
MONTH_INDEX={letter:i for i,letter in enumerate(MONTHS,1)}
MONTH_NAMES=("Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep",
             "Oct","Nov","Dec")
SYMBOL=re.compile(r"^([A-Z]+)([FGHJKMNQUVXZ])(\d{1,4})@([A-Z0-9]{4})$")


def _year(digits,refyear):
    if len(digits)==4:
        return int(digits)
    if len(digits)==2:
        return 2000+int(digits)
    base=refyear-1
    return base+(int(digits)-base%10)%10


def _date(value):
    if isinstance(value,date):
        return value
    if not isinstance(value,str):
        return None
    text=value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.*)?",text):
            return date.fromisoformat(text[:10])
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}",text):
            return datetime.strptime(text,"%m/%d/%Y").date()
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2}",text):
            return datetime.strptime(text,"%m/%d/%y").date()
    except ValueError:
        return None
    return None


def _calendar_contract_month(value,root,refyear):
    """Match *exact* contract identity; don't confuse unrelated futures/options."""
    if not isinstance(value,str):
        return None
    normalized=value.strip().upper().replace(" ","")
    if normalized.startswith(root):
        match=re.fullmatch(re.escape(root)+r"([FGHJKMNQUVXZ])(\d{1,4})",normalized)
        if match:
            return _year(match.group(2),refyear),MONTH_INDEX[match.group(1)]
    match=re.fullmatch(r"([FGHJKMNQUVXZ])(\d{1,4})",normalized)
    if match:
        return _year(match.group(2),refyear),MONTH_INDEX[match.group(1)]
    match=re.fullmatch(r"(\d{4})(0[1-9]|1[0-2])",normalized)
    if match:
        return int(match.group(1)),int(match.group(2))
    match=re.fullmatch(r"([A-Z]{3})(\d{2,4})",normalized)
    if match and match.group(1).title() in MONTH_NAMES:
        return _year(match.group(2),refyear),MONTH_NAMES.index(match.group(1).title())+1
    return None


def cme_calendar_dates(payload, *, root, reference_year):
    """Parse verified contract-level last trading days from CME official JSON.

    Only accepts rows with a recognizable maturity AND explicit last-trade
    field or Last Trade Date event. Never infer from settlement/delivery/
    first-notice/option-expiration dates.
    """
    result={}
    if not isinstance(payload,(list,dict)):
        return result
    pending=[payload]
    visited=0
    while pending and visited<15000:
        obj=pending.pop()
        visited+=1
        if isinstance(obj,list):
            pending.extend(obj[:15000])
            continue
        if not isinstance(obj,dict):
            continue
        pending.extend(v for v in obj.values() if isinstance(v,(dict,list)))
        label=" ".join(str(obj.get(k,"")) for k in
                       ("eventType","event","description","dateType","type","name")).lower()
        if "option" in label or "first notice" in label or "settlement" in label:
            continue
        contract=None
        for field in ("contractSymbol","contract","symbol","contractMonth",
                      "contractCode","maturity","expiration","month"):
            contract=_calendar_contract_month(obj.get(field),root,reference_year)
            if contract:
                break
        if contract is None:
            continue
        day=None
        for key in ("lastTradeDate","lastTradingDate","lastTradingDay",
                    "lastTrade","lastTradeDay","lastTrading"):
            day=_date(obj.get(key))
            if day:
                break
        if day is None and ("last trade" in label or "lasttrade" in label):
            for key in ("date","eventDate","tradeDate"):
                day=_date(obj.get(key))
                if day:
                    break
        if day is None or abs(day.year-contract[0])>1:
            continue
        prior=result.get(contract)
        if prior is not None and prior!=day:
            # Conflicting official records are NOT a usable date.
            result[contract]=None
        elif contract not in result:
            result[contract]=day
    return {key:val for key,val in result.items() if val is not None}


class _IceTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows=[]
        self.cells=[]
        self.words=[]
        self.in_row=False
        self.in_cell=False

    def handle_starttag(self, tag, attrs):
        if tag=="tr":
            self.in_row=True
            self.cells=[]
        elif tag in ("td","th") and self.in_row:
            self.in_cell=True
            self.words=[]

    def handle_data(self, text):
        if self.in_cell:
            self.words.append(text)

    def handle_endtag(self,tag):
        if tag in ("td","th") and self.in_cell:
            self.cells.append(" ".join(self.words).strip())
            self.in_cell=False
        if tag=="tr" and self.in_row:
            if self.cells:
                self.rows.append(self.cells)
            self.in_row=False


def ice_brent_dates(html):
    """Read ICE product 219 table: month, first trade, *LTD* at column 3."""
    parser=_IceTable()
    parser.feed(html)
    result={}
    header_found=False
    for cells in parser.rows:
        if len(cells)>=3 and "Last Trade Date" in cells[2]:
            header_found=True
            continue
        if not header_found or len(cells)<3:
            continue
        month=re.fullmatch(r"([A-Za-z]{3})(\d{2})",cells[0].strip())
        if not month or month.group(1).title() not in MONTH_NAMES:
            continue
        year=2000+int(month.group(2))
        m=MONTH_NAMES.index(month.group(1).title())+1
        day=_date(cells[2])
        if day and abs(day.year-year)<=1:
            result[(year,m)]=day
    return result


def _fetch(url, *, opener=urlopen):
    request=Request(url,headers={
        "Accept":"application/json,text/html;q=0.8",
        "User-Agent":"FlowDesk/0.2.26 (official exchange expiry metadata)"})
    with opener(request,timeout=12) as response:
        return response.read(2_000_001)


class OfficialExpiry:
    """Caches each exchange product calendar once per startup worker."""
    def __init__(self, *, fetch=None, reference_year=None):
        self.fetch=fetch or _fetch
        self.reference_year=reference_year or datetime.utcnow().year
        self._cache={}
        self.errors={}

    def get(self,symbol):
        match=SYMBOL.fullmatch(symbol.upper())
        if not match:
            return None,"неверный формат символа для календаря"
        root,month,year_raw,mic=match.groups()
        year=_year(year_raw,self.reference_year)
        if root=="BRN" and mic=="IFEU":
            source="ICE Brent"
            if source not in self._cache:
                try:
                    raw=self.fetch(ICE_URL)
                    html=raw.decode("utf-8") if isinstance(raw,bytes) else str(raw)
                    self._cache[source]=ice_brent_dates(html)
                    if not self._cache[source]:
                        self.errors[source]="таблица LTD не найдена"
                except Exception as exc:
                    self._cache[source]={}
                    self.errors[source]="недоступен официальный календарь ("+type(exc).__name__+")"
        elif root in CME_ROOTS and mic in CME_ROOT_VENUES[root]:
            source="CME "+root
            if source not in self._cache:
                try:
                    url="https://www.cmegroup.com/CmeWS/mvc/ProductCalendar/Future/"+str(CME_ROOTS[root])+".json"
                    raw=self.fetch(url)
                    obj=json.loads(raw) if isinstance(raw,(bytes,str)) else raw
                    self._cache[source]=cme_calendar_dates(
                        obj,root=root,reference_year=self.reference_year)
                    if not self._cache[source]:
                        self.errors[source]="нет точной даты Last Trade Date в публичном ответе CME"
                except Exception as exc:
                    self._cache[source]={}
                    self.errors[source]="публичный календарь CME недоступен ("+type(exc).__name__+")"
        else:
            return None,"для "+root+"@"+mic+" не настроен проверенный публичный календарь"
        found=self._cache[source].get((year,MONTH_INDEX[month]))
        if found is not None:
            return found,source+" (Last Trade Date)"
        return None,self.errors.get(source,"нет точной даты серии "+month+str(year)+" в "+source)
