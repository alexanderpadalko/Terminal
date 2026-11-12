"""Inspect the display plan over actual SBER HTTP sources without changing calculations."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowdesk.fvg_display import select_fvg_display
from flowdesk.market_cache import MarketService
from check_issplus_live import seed_recent_http


def main():
    with tempfile.TemporaryDirectory() as directory:
        service = MarketService(Path(directory)/'cache.sqlite3')
        seed_recent_http(service)
        analysis, parents, info = service.view('SBER', 'TQBR', 'M1')
        before = [(tf, tuple((z.start,z.impulse,z.created,z.top,z.bottom,z.side,z.broken,z.balanced)
                             for z in a.fvg)) for tf,a in {'M1':analysis,**parents}.items()]
        plan = select_fvg_display(analysis, parents)
        after = [(tf, tuple((z.start,z.impulse,z.created,z.top,z.bottom,z.side,z.broken,z.balanced)
                            for z in a.fvg)) for tf,a in {'M1':analysis,**parents}.items()]
        assert before == after
        assert len(plan.zones) <= 20
        assert len({z.source_tf for z in plan.parents}) == len(plan.parents)
        result = {'at':datetime.now(timezone.utc).isoformat(), 'symbol':'SBER', 'timeframe':'M1',
            'snapshot_asof':info['asof'], 'last_candle':analysis.candles[-1].time.isoformat(),
            'raw': {tf:len(a.fvg) for tf,a in {'M1':analysis,**parents}.items()},
            'display':plan.as_dict(), 'calculation_unchanged':True,
            'limitation':'Display-rule audit on supplied history; not an OHLCV/Pine runtime parity proof.'}
        Path('fvg-display-live-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        shutil.copyfile(service.store.path, 'fvg-display-live-cache.sqlite3')
        print('FVG_DISPLAY_LIVE', json.dumps({'raw':result['raw'],'shown':len(plan.zones),
            'working_parents':len(plan.parents),'rejected':plan.rejected},ensure_ascii=False), flush=True)


if __name__ == '__main__': main()
