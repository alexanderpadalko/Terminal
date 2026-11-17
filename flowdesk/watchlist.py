"""Daily MOEX lists: 40 shares by 20-session RUB turnover, 10 fixed futures."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import base64
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request
import zlib

from .market_data import MOSCOW, stock_session_kind, REGULAR

ROOT = 'https://iss.moex.com/iss/'
TERMS = ('SI', 'CR', 'MX', 'EU', 'RI', 'ED')
PERPETUALS = (('USDRUBF', 'USDRUB.P'), ('CNYRUBF', 'CNYRUB.P'),
              ('EURRUBF', 'EURRUB.P'), ('IMOEXF', 'IRUS.P'))
HISTORY_POLICY = 'TQBR_EXPLICIT_DAILY_HISTORY_V2'


@dataclass(frozen=True)
class Instrument:
    symbol: str
    board: str
    name: str
    kind: str
    tv_symbol: str = ''
    expiry: str = ''
    turnover: float = 0
    next_symbol: str = ''
    next_expiry: str = ''


def slot_date(now):
    local = now.astimezone(MOSCOW)
    return local.date() - timedelta(days=local.hour < 6)


def get_json(path, params=None):
    query = urllib.parse.urlencode({'iss.meta': 'off', **(params or {})})
    request = urllib.request.Request(ROOT + path + '?' + query, headers={'User-Agent': 'FlowDesk/0.2.20'})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def pages(path, block, params=None, *, fetch=get_json, cancelled=lambda: False):
    result, start, previous = [], 0, None
    for _ in range(500):
        if cancelled():
            raise InterruptedError('Загрузка списка остановлена')
        payload = fetch(path, {**(params or {}), 'start': start, 'iss.only': f'{block},{block}.cursor'})
        table = payload.get(block)
        if not table or 'columns' not in table or 'data' not in table:
            raise ValueError(f'MOEX: нет таблицы {block}')
        chunk = [dict(zip(table['columns'], row)) for row in table['data']]
        if not chunk:
            return result
        signature = json.dumps(chunk, sort_keys=True)
        if signature == previous:
            raise ValueError('MOEX повторяет страницу; неполный список не принят')
        previous = signature
        result.extend(chunk)
        start += len(chunk)
        cursor = payload.get(block + '.cursor', {})
        if cursor.get('data'):
            position = dict(zip(cursor['columns'], cursor['data'][0]))
            if start >= int(position['TOTAL']):
                return result
        elif block == 'securities':
            # Board reference tables are returned in full, without pagination.
            return result
    raise ValueError('Превышен лимит страниц списка MOEX')


def compact_history(rows):
    values = {}
    for row in rows:
        if row.get('BOARDID', 'TQBR') != 'TQBR':
            continue
        key = (row['TRADEDATE'], row['SECID'])
        value = str(row.get('VALUE') or 0)
        if Decimal(value) < 0:
            raise ValueError('Отрицательный оборот MOEX')
        # Never add a repeated total/session row twice.
        if key in values and Decimal(values[key]['VALUE']) != Decimal(value):
            raise ValueError(f'Неоднозначный дневной оборот: {key}')
        values[key] = {'TRADEDATE': key[0], 'SECID': key[1], 'VALUE': value}
    return sorted(values.values(), key=lambda r: (r['TRADEDATE'], r['SECID']))


def rank_stocks(history, catalog, cutoff):
    rows = compact_history([r for r in history if r['TRADEDATE'] <= cutoff.isoformat()])
    days = sorted({r['TRADEDATE'] for r in rows})[-20:]
    if len(days) != 20:
        raise ValueError('Для рейтинга нужны 20 завершённых торговых дней')
    eligible = {r['SECID']: r for r in catalog if str(r.get('SECTYPE')) in ('1', '2')
                and r.get('BOARDID', 'TQBR') == 'TQBR'}
    totals = {}
    for row in rows:
        if row['TRADEDATE'] in days and row['SECID'] in eligible:
            code = row['SECID']
            totals[code] = totals.get(code, Decimal(0)) + Decimal(row['VALUE'])
    ranked = sorted((code for code in totals if totals[code] > 0), key=lambda c: (-totals[c], c))[:40]
    if len(ranked) != 40:
        raise ValueError('MOEX не подтвердила 40 обыкновенных/привилегированных акций')
    stocks = [Instrument(code, 'TQBR', eligible[code].get('SHORTNAME') or code, 'stock',
                         'RUS:' + code, turnover=float(totals[code])) for code in ranked]
    return stocks, [r for r in rows if r['TRADEDATE'] in days], days


def roll_futures(previous, catalog, day):
    table = {r['SECID'].upper(): r for r in catalog}
    by_root = {}
    for row in catalog:
        code = row['SECID'].upper()
        match = re.fullmatch(r'([A-Z]+)[FGHJKMNQUVXZ]\d', code)
        if match and row.get('LASTTRADEDATE'):
            by_root.setdefault(match[1], []).append(row)
    result = []
    for root in TERMS:
        # Keep the configured product and series until its last trading day.
        configured = next((r for r in previous if r['kind'] == 'future' and
                           re.fullmatch(root + r'[FGHJKMNQUVXZ]\d', r['symbol'].upper())), None)
        current = table.get(configured['symbol'].upper()) if configured else None
        if current and current.get('LASTTRADEDATE') and current['LASTTRADEDATE'] > day.isoformat():
            selected = current
        else:
            choices = [r for r in by_root.get(root, []) if r['LASTTRADEDATE'] > day.isoformat()]
            if not choices:
                raise ValueError(f'Не найдена действующая серия {root}; состав не изменён')
            selected = min(choices, key=lambda r: (r['LASTTRADEDATE'], r['SECID']))
        code = selected['SECID'].upper()
        expiry = selected['LASTTRADEDATE']
        year = date.fromisoformat(expiry).year
        following = sorted((r for r in by_root.get(root, []) if r['LASTTRADEDATE'] > expiry),
                           key=lambda r: (r['LASTTRADEDATE'], r['SECID']))
        next_row = following[0] if following else {}
        result.append(Instrument(code, 'RFUD', selected.get('SHORTNAME') or code, 'future',
                                 f'RUS:{code[:-1]}{year}', expiry,
                                 next_symbol=next_row.get('SECID', '').upper(), next_expiry=next_row.get('LASTTRADEDATE', '')))
    for code, alias in PERPETUALS:
        if code not in table:
            raise ValueError(f'Биржа не подтвердила {code}; фиксированный актив не удалён')
        result.append(Instrument(code, 'RFUD', table[code].get('SHORTNAME') or code, 'perpetual', 'RUS:' + alias))
    return result


class WatchlistService:
    def __init__(self, path=None, *, clock=None, fetch=get_json, seed_path=None):
        self.path = Path(path or Path.home() / '.flowdesk' / 'watchlist_v1.json')
        self.seed_path = Path(seed_path or Path(__file__).parent / 'data' / 'watchlist_seed.json')
        self.clock = clock or (lambda: datetime.now(MOSCOW))
        self.fetch = fetch

    @staticmethod
    def validate(value):
        items = value['instruments']
        stocks = [r for r in items if r['kind'] == 'stock']
        futures = [r for r in items if r['kind'] != 'stock']
        if len(stocks) != 40 or len(futures) != 10 or len({(r['symbol'], r['board']) for r in items}) != 50:
            raise ValueError('Ожидаются 40 уникальных акций и 10 фиксированных фьючерсов')
        if any(r['board'] != ('TQBR' if r['kind'] == 'stock' else 'RFUD') for r in items):
            raise ValueError('Неверный режим торгов в списке')
        matches = [re.fullmatch(r'([A-Z]+)[FGHJKMNQUVXZ]\d', r['symbol'])
                   for r in futures if r['kind'] == 'future']
        if not all(matches):
            raise ValueError('Неверная серия фьючерса')
        roots = {m.group(1) for m in matches}
        perpetuals = {r['symbol'] for r in futures if r['kind'] == 'perpetual'}
        if roots != set(TERMS) or perpetuals != {c for c, _ in PERPETUALS}:
            raise ValueError('Изменён фиксированный состав фьючерсов')
        return value

    def cached(self):
        for path in (self.path, self.seed_path):
            try:
                return self.validate(json.loads(path.read_text(encoding='utf-8-sig')))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return None

    def due(self, value):
        return (not value or value.get('slot') != slot_date(self.clock()).isoformat()
                or value.get('history_policy') != HISTORY_POLICY)

    @staticmethod
    def history(value):
        if 'history' in value:
            return value['history']
        if value.get('history_zlib'):
            return json.loads(zlib.decompress(base64.b64decode(value['history_zlib'])))
        return []

    def refresh(self, cancelled=lambda: False, *, stocks_only=False):
        """When stocks_only, refresh exact share turnover without touching futures.

        The Finam terminal has its own expiry + liquidity rollover; legacy
        RFUD roll_futures must never block updating the stock ranking.
        """
        now = self.clock()
        slot = now.astimezone(MOSCOW).date() if stocks_only else slot_date(now)
        cutoff = slot - timedelta(days=1)
        old = self.cached() or {}
        history = self.history(old)
        start = (date.fromisoformat(min(r['TRADEDATE'] for r in history))
                 if history else cutoff - timedelta(days=60))
        start = max(start, cutoff - timedelta(days=60))
        known_days = {r['TRADEDATE'] for r in history}
        additions = []
        day = start
        while day <= cutoff:
            if day.isoformat() in known_days:
                day += timedelta(days=1)
                continue
            daily = pages('history/engines/stock/markets/shares/boards/TQBR/securities.json', 'history',
                          {'date': day.isoformat(), 'tradingsession': 3,
                           'history.columns': 'BOARDID,TRADEDATE,SECID,VALUE'}, fetch=self.fetch, cancelled=cancelled)
            if any(r['TRADEDATE'] != day.isoformat() for r in daily):
                raise ValueError(f'MOEX вернула другую дату вместо {day}')
            if not daily and stock_session_kind(day) == REGULAR:
                raise ValueError(f'Пропущен завершённый торговый день {day}')
            additions.extend(daily)
            day += timedelta(days=1)
        catalog = pages('engines/stock/markets/shares/boards/TQBR/securities.json', 'securities',
                        {'securities.columns': 'SECID,BOARDID,SHORTNAME,SECTYPE'}, fetch=self.fetch, cancelled=cancelled)
        stocks, history, days = rank_stocks(history + additions, catalog, cutoff)
        expected = cutoff
        while stock_session_kind(expected) != REGULAR:
            expected -= timedelta(days=1)
        if days[-1] < expected.isoformat():
            raise ValueError(f'Не получены завершённые торги {expected}; сохранён прежний список')
        if stocks_only:
            # Reuse the 10 validated old rows only to preserve the on-disk
            # cache schema. They are NEVER used for the Finam futures UI.
            futures=[Instrument(**row) for row in old['instruments']
                     if row['kind']!='stock']
        else:
            contracts = pages('engines/futures/markets/forts/boards/RFUD/securities.json', 'securities',
                              {'securities.columns': 'SECID,SHORTNAME,LASTTRADEDATE,ASSETCODE'},
                              fetch=self.fetch, cancelled=cancelled)
            futures = roll_futures(old.get('instruments', []), contracts, slot)
        value = self.validate({'schema': 1, 'history_policy': HISTORY_POLICY, 'slot': slot.isoformat(), 'generated_at': now.isoformat(),
            'window': days, 'history': history, 'instruments': [asdict(r) for r in stocks + futures],
            'rule': ('TQBR shares: 20 completed sessions, exact VALUE; futures preserved for Finam' if stocks_only
                     else 'TQBR ordinary/preferred, 20 completed sessions; fixed 6 term + 4 perpetual; roll at expiry 06 MSK')})
        if stocks_only:value['ranking_day']=now.astimezone(MOSCOW).date().isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        temp.replace(self.path)
        return value
