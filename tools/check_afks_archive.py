"""Optional network integration check; never supplies constants to the engine."""
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flowdesk.market_data import load_moex_session_bundle
from flowdesk.pipeline import analyze_timeframes, source_coverage


def main():
    destination = Path('archive-check')
    destination.mkdir(exist_ok=True)
    report = {'parity': 'UNVERIFIED', 'data_source': 'MOEX AFKS TQBR cleaned H1 archive'}
    try:
        _, series, provenance = load_moex_session_bundle('AFKS', 'H1')
        asof = datetime.now(timezone.utc)
        # Save the fetched market inputs so a later calculation can reproduce
        # this check without making any requests or depending on current time.
        (destination / 'inputs.json').write_text(json.dumps({
            'asof': asof.isoformat(), 'session_info': provenance,
            'series': {tf: [{'time': c.time.isoformat(), 'open': c.open, 'high': c.high,
                            'low': c.low, 'close': c.close, 'volume': c.volume} for c in bars]
                       for tf, bars in series.items()}}), encoding='utf-8')
        main_analysis, parents = analyze_timeframes(series, 'H1', asof=asof)
        report['sources'] = {}
        for tf, result in {'H1': main_analysis, **parents}.items():
            latest = result.flows[-1]
            last_capture = next((s for s in reversed(result.liquidity_audit)
                                 if not any(v.endswith('_NOT_CLOSED') for v in s.source_status.values())), None)
            report['sources'][tf] = {
                'coverage': source_coverage(result), 'side': latest.side,
                'lo': latest.lo, 'hi': latest.hi,
                'pair': {'high': result.pairs[-1].high, 'low': result.pairs[-1].low},
                'last_rule': latest.last_change_rule,
                'last_change_at': latest.last_change_at.isoformat() if latest.last_change_at else None,
                'latest_rule1_check': result.flows[result.confirmed_bars - 1].rule1_check,
                'recent_events': [{'time': result.candles[f.i].time.isoformat(), 'side': f.side,
                                   'rule': f.rule, 'lo': f.lo, 'hi': f.hi,
                                   'evidence': f.change_evidence} for f in result.flows
                                  if f.rule and result.candles[f.i].time.year == 2026][-12:]}
            print(tf, json.dumps(report['sources'][tf], ensure_ascii=False), flush=True)
        report['network_status'] = 'SUCCESS'
    except Exception as exc:
        report['network_status'] = 'FAILED'
        report['error'] = str(exc)
        raise
    finally:
        (destination / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
