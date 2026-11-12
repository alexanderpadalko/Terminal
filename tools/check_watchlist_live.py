"""Build a real 20-session list and check RFUD source candles for all 10 contracts."""
import base64
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowdesk.watchlist import WatchlistService
from flowdesk.futures_data import aggregate_h4, clean
from flowdesk.market_data import _iss_download, download_native_higher, MOSCOW


def main():
    result = {'started': datetime.now(MOSCOW).isoformat(), 'errors': [], 'futures': []}
    try:
        with tempfile.TemporaryDirectory() as directory:
            service = WatchlistService(Path(directory) / 'list.json')
            snapshot = service.refresh()
            result.update(slot=snapshot['slot'], window=snapshot['window'],
                          stocks=[r['symbol'] for r in snapshot['instruments'] if r['kind'] == 'stock'])
            for row in snapshot['instruments']:
                if row['kind'] == 'stock':
                    continue
                bars = _iss_download(row['symbol'], 60, days=3, limit=200000, timeout=15, board='RFUD', require_full=True)
                hours = clean(bars, 'H1')
                h4 = aggregate_h4(hours)
                assert hours and h4, row
                result['futures'].append({**row, 'h1_bars': len(hours), 'h4_bars': len(h4),
                                         'first': hours[0].time.isoformat(), 'last': hours[-1].time.isoformat()})
                print('RFUD_OK', row['symbol'], len(hours), flush=True)
            daily = download_native_higher('SIZ6', 'D1', days=30, board='RFUD')
            assert daily and daily[-1].time.hour == 0
            minute = _iss_download('SIZ6', 1, days=2, limit=200000, timeout=15, board='RFUD', require_full=True)
            assert clean(minute, 'M1')
            result['minute_bars'] = len(minute)
            snapshot['history_zlib'] = base64.b64encode(zlib.compress(
                json.dumps(snapshot.pop('history'), separators=(',', ':')).encode(), 9)).decode()
            destination = Path('flowdesk/data/watchlist_seed.json')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
            result['success'] = True
            print('LIST_SUMMARY_BEGIN', flush=True)
            print(json.dumps({k: v for k, v in result.items() if k != 'futures'}, ensure_ascii=False), flush=True)
            print('LIST_SUMMARY_END', flush=True)
    except Exception as exc:
        result['errors'].append(f'{type(exc).__name__}: {exc}')
        raise
    finally:
        Path('watchlist-live-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
