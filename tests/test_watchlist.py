from dataclasses import asdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from flowdesk.watchlist import Instrument, WatchlistService, rank_stocks, roll_futures, slot_date, pages
from flowdesk.market_data import MOSCOW
from flowdesk.preferences import Preferences


def stock_inputs():
    start = date(2026, 9, 1)
    catalog = [{'SECID': f'S{i:02}', 'SECTYPE': '1', 'SHORTNAME': f'Stock {i}', 'BOARDID': 'TQBR'} for i in range(45)]
    catalog += [{'SECID': 'PREF', 'SECTYPE': '2'}, {'SECID': 'ETF', 'SECTYPE': 'E'}]
    rows = [{'TRADEDATE': (start + timedelta(days=d)).isoformat(), 'SECID': c['SECID'],
             'VALUE': 10000 if c['SECID'] in ('PREF', 'ETF') else int(c['SECID'][1:]) + 1}
            for d in range(21) for c in catalog]
    return catalog, rows


def future_inputs():
    rows = [{'SECID': root + series, 'LASTTRADEDATE': expiry, 'SHORTNAME': root + series}
            for root in ('Si', 'CR', 'MX', 'Eu', 'RI', 'ED')
            for series, expiry in [('Z6', '2026-12-17'), ('H7', '2027-03-18')]]
    rows += [{'SECID': code, 'LASTTRADEDATE': '2099-01-01'} for code in ('USDRUBF', 'CNYRUBF', 'EURRUBF', 'IMOEXF')]
    old = [asdict(Instrument(root + 'Z6', 'RFUD', root, 'future')) for root in ('SI', 'CR', 'MX', 'EU', 'RI', 'ED')]
    return old, rows


class TestWatchlist(unittest.TestCase):
    def test_refresh_fetches_each_missing_date_and_keeps_twenty_latest_sessions(self):
        catalog, all_rows = stock_inputs()
        # A weekday-only, already verified seed through November 2.
        seed_days = [date(2026, 10, 6) + timedelta(days=i) for i in range(28)]
        seed_days = [d for d in seed_days if d.weekday() < 5][-20:]
        history = [{'TRADEDATE': d.isoformat(), 'SECID': c['SECID'], 'VALUE': 100 + i}
                   for d in seed_days for i, c in enumerate(catalog)]
        old_futures, contracts = future_inputs()
        old_futures = [asdict(r) for r in roll_futures(old_futures, contracts, date(2026, 11, 2))]
        instruments = [asdict(Instrument(c['SECID'], 'TQBR', c.get('SHORTNAME', c['SECID']), 'stock')) for c in catalog[:40]]
        requested = []
        def fetch(path, params):
            if 'history/' in path:
                self.assertIn('date', params)
                requested.append(params['date'])
                d = date.fromisoformat(params['date'])
                rows = ([] if d.weekday() >= 5 or d == date(2026, 11, 4) else
                    [{'TRADEDATE': d.isoformat(), 'SECID': c['SECID'], 'VALUE': 200 + i} for i, c in enumerate(catalog)])
                cols = ['TRADEDATE', 'SECID', 'VALUE']
                return {'history': {'columns': cols, 'data': [[r[k] for k in cols] for r in rows]},
                        'history.cursor': {'columns': ['TOTAL'], 'data': [[len(rows)]]}}
            rows = contracts if 'futures/' in path else catalog
            cols = sorted({k for r in rows for k in r})
            return {'securities': {'columns': cols, 'data': [[r.get(k) for k in cols] for r in rows]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'list.json'
            path.write_text(json.dumps({'slot': '2026-11-03', 'history': history,
                'instruments': instruments + old_futures}), encoding='utf-8')
            service = WatchlistService(path, clock=lambda: datetime(2026, 11, 12, 9, tzinfo=MOSCOW), fetch=fetch)
            value = service.refresh()
            self.assertEqual(len(value['window']), 20)
            self.assertIn('2026-11-03', value['window'])
            self.assertIn('2026-11-10', value['window'])
            self.assertEqual(value['window'][-1], '2026-11-11')
            self.assertTrue({(date(2026, 11, 3) + timedelta(days=i)).isoformat() for i in range(9)} <= set(requested))
            self.assertFalse({d.isoformat() for d in seed_days} & set(requested))

    def test_only_40_equities_and_preferred_separate_last20_completed(self):
        catalog, rows = stock_inputs()
        cutoff = date(2026, 9, 20)
        stocks, kept, days = rank_stocks(rows, catalog, cutoff)
        self.assertEqual(len(stocks), 40)
        self.assertEqual(days, [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(20)])
        self.assertEqual(stocks[0].symbol, 'PREF')
        self.assertNotIn('ETF', [s.symbol for s in stocks])
        self.assertEqual(stocks[1].turnover, 45 * 20)

    def test_new_ipo_enters_ranking_and_old_outside_top40_can_return(self):
        catalog, rows = stock_inputs()
        catalog.append({'SECID': 'NEW', 'SECTYPE': '1'})
        rows.append({'TRADEDATE': '2026-09-20', 'SECID': 'NEW', 'VALUE': 10000000})
        stocks, _, _ = rank_stocks(rows, catalog, date(2026, 9, 20))
        self.assertEqual(stocks[0].symbol, 'NEW')

    def test_different_daily_totals_are_not_double_counted(self):
        catalog, rows = stock_inputs()
        rows.append({**rows[0], 'VALUE': 999})
        with self.assertRaises(ValueError):
            rank_stocks(rows, catalog, date(2026, 9, 20))

    def test_less_than20_sessions_rejected(self):
        catalog, rows = stock_inputs()
        with self.assertRaises(ValueError):
            rank_stocks(rows, catalog, date(2026, 9, 19))

    def test_fixed_futures_keep_contract_until_expiry_then_only_roll_series(self):
        old, rows = future_inputs()
        before = roll_futures(old, rows, date(2026, 12, 16))
        after = roll_futures([asdict(r) for r in before], rows, date(2026, 12, 17))
        self.assertEqual([r.symbol for r in before[:6]], [r + 'Z6' for r in ('SI', 'CR', 'MX', 'EU', 'RI', 'ED')])
        self.assertEqual([r.symbol for r in after[:6]], [r + 'H7' for r in ('SI', 'CR', 'MX', 'EU', 'RI', 'ED')])
        self.assertEqual([r.symbol for r in before[6:]], [r.symbol for r in after[6:]])
        self.assertEqual(before[0].next_symbol, 'SIH7')
        self.assertEqual(after[0].tv_symbol, 'RUS:SIH2027')

    def test_missing_product_never_replaced_with_mini_or_forbidden(self):
        old, rows = future_inputs()
        rows = [r for r in rows if not r['SECID'].upper().startswith('SI')]
        rows.append({'SECID': 'MINISIH7', 'LASTTRADEDATE': '2027-03-18'})
        with self.assertRaises(ValueError):
            roll_futures(old, rows, date(2026, 12, 17))

    def test_6am_moscow_slot_and_missed_expiry_catchup(self):
        self.assertEqual(slot_date(datetime(2026, 12, 17, 5, 59, tzinfo=MOSCOW)), date(2026, 12, 16))
        self.assertEqual(slot_date(datetime(2026, 12, 17, 6, tzinfo=MOSCOW)), date(2026, 12, 17))
        old, rows = future_inputs()
        self.assertTrue(all(r.symbol.endswith('H7') for r in roll_futures(old, rows, date(2027, 1, 1))[:6]))

    def test_incomplete_pagination_and_repeated_pages_rejected(self):
        def broken(path, params):
            return {'history': {'columns': ['SECID'], 'data': [['SBER']]}}
        with self.assertRaises(ValueError):
            pages('path', 'history', fetch=broken)

    def test_bundled_seed_and_corrupt_cache_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.json'
            path.write_text('{bad', encoding='utf-8')
            service = WatchlistService(path)
            value = service.cached()
            self.assertEqual(len(value['instruments']), 50)
            self.assertGreater(len(service.history(value)), 100)
            copy = json.loads(json.dumps(value))
            copy['instruments'][-1]['symbol'] = 'UCNY'
            with self.assertRaises(ValueError):
                service.validate(copy)


class TestPreferences(unittest.TestCase):
    def test_last_instrument_persists_and_bad_input_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            store = Preferences(path)
            self.assertEqual(store.read()['symbol'], 'SBER')
            store.write('AFKS', 'TQBR', 'M5')
            self.assertEqual(store.read(), {'symbol': 'AFKS', 'board': 'TQBR', 'tf': 'M5'})
            path.write_text('[]', encoding='utf-8')
            self.assertEqual(store.read()['symbol'], 'SBER')
