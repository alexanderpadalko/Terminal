"""Versioned derived families beside untouched, separately stored provider bars."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json

from .cancellation import checkpoint
from .market_data import Candle, candle_close_time, serialize_candle
from .session_candles import AGGREGATION_VERSION, SessionSeries, build_session_series, build_h4_from_finam_h1
from .provider_candles import (NATIVE_HIGHER, NATIVE_POLICY_VERSION, native_higher_enabled,
                               native_higher_series, control_issues)
from .trading_calendar import CalendarUnavailable, calendar_for
from . import us_futures_family as futures
from . import moex_trading_family as moex


class PreparedSeries(dict):
    def __init__(self,series,metadata):
        super().__init__(series)
        self.session_metadata=metadata


class SourceSeries(dict):
    def __init__(self,series,state):
        super().__init__(series)
        self.source_state=state


def source_end(service,symbol,tf,bar):
    ending=candle_close_time(bar,tf)
    if tf not in ('M1','M5','H1'):return ending
    try:
        calendar=service.calendar_factory(symbol)
        day=bar.time.astimezone(calendar.zone).date()
        for candidate in (day,day+timedelta(days=1)):
            session=calendar.session(candidate)
            if session and session.start<=bar.time<session.end:
                return min(ending,session.end)
    except CalendarUnavailable:pass
    return ending


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS candle_source_observations (
        symbol TEXT NOT NULL,tf TEXT NOT NULL,ts TEXT NOT NULL,observed_at TEXT NOT NULL,
        PRIMARY KEY(symbol,tf,ts))''')
    db.execute('''CREATE TABLE IF NOT EXISTS candle_source_revisions (
        symbol TEXT PRIMARY KEY,revision INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS candle_source_coverage (
        symbol TEXT NOT NULL,tf TEXT NOT NULL,start TEXT NOT NULL,ending TEXT NOT NULL,
        PRIMARY KEY(symbol,tf,start))''')
    db.execute('''CREATE TABLE IF NOT EXISTS session_series_cache (
        symbol TEXT PRIMARY KEY,policy TEXT NOT NULL,revision INTEGER NOT NULL,
        observed_at TEXT NOT NULL,payload TEXT NOT NULL)''')


def changed(db,symbol,tf,bars,observed,*,replace=True):
    before=db.total_changes
    if replace:
        db.executemany('INSERT OR REPLACE INTO candle_source_observations VALUES(?,?,?,?)',
            ((symbol,tf,b.time.isoformat(),observed.isoformat()) for b in bars))
    else:
        db.executemany('''INSERT INTO candle_source_observations VALUES(?,?,?,?)
            ON CONFLICT(symbol,tf,ts) DO UPDATE SET observed_at=excluded.observed_at
            WHERE julianday(candle_source_observations.observed_at)<julianday(?)
            AND julianday(excluded.observed_at)>=julianday(?)''',
            ((symbol,tf,b.time.isoformat(),observed.isoformat(),
              candle_close_time(b,tf).isoformat(),candle_close_time(b,tf).isoformat()) for b in bars))
    if not replace and db.total_changes==before:return False
    db.execute('''INSERT INTO candle_source_revisions VALUES(?,1)
        ON CONFLICT(symbol) DO UPDATE SET revision=revision+1''',(symbol,))
    return True


def record_coverage(service,symbol,tf,start,end):
    if start>=end:return
    with service.lock,closing(service._connection()) as db,db:
        rows=db.execute('SELECT start,ending FROM candle_source_coverage WHERE symbol=? AND tf=?',
                        (symbol,tf)).fetchall()
        windows=sorted([(datetime.fromisoformat(a),datetime.fromisoformat(b)) for a,b in rows]+[(start,end)])
        merged=[]
        for left,right in windows:
            if merged and left<=merged[-1][1]:merged[-1]=(merged[-1][0],max(merged[-1][1],right))
            else:merged.append((left,right))
        updated=[(left.isoformat(),right.isoformat()) for left,right in merged]
        if updated==sorted(rows):
            return
        db.execute('DELETE FROM candle_source_coverage WHERE symbol=? AND tf=?',(symbol,tf))
        db.executemany('INSERT INTO candle_source_coverage VALUES(?,?,?,?)',
            ((symbol,tf,left,right) for left,right in updated))
        db.execute('''INSERT INTO candle_source_revisions VALUES(?,1)
            ON CONFLICT(symbol) DO UPDATE SET revision=revision+1''',(symbol,))
        if getattr(service,'_chart_sources',None) is not None and service._chart_sources.symbol==symbol:
            service._commit_source_change(db,symbol,start=start.isoformat(),end=end.isoformat())


def record_response(service,symbol,tf,bars,start,end,observed):
    # An empty/age-truncated response does not certify a whole missing
    # historical session. Bound coverage by actual returned source intervals.
    if not bars:return
    left=max(start,min(b.time for b in bars))
    right=min(end,observed,max(candle_close_time(b,tf) for b in bars))
    record_coverage(service,symbol,tf,left,right)


def _candle(row):
    return Candle(datetime.fromisoformat(row['time']),*(row[k] for k in ('open','high','low','close','volume')),
        scheduled_close=datetime.fromisoformat(row['scheduled_close']) if row.get('scheduled_close') else None,
        settlement_close=row.get('settlement_close',False),
        trading_date=date.fromisoformat(row['trading_date']) if row.get('trading_date') else None)


def prepare(service,symbol,raw,observed,*,bucket_cache=None,bucket_changes=None):
    if not getattr(service.api,'session_candles',False):return raw
    primary_native=native_higher_enabled(service.api)
    direct=primary_native and bool(getattr(service.api,'h1_only_h4_native_dwm',False))
    pilot_requested=primary_native and not direct and bool(futures.product(symbol))
    ru=primary_native and not direct and moex.enabled(symbol)
    state=getattr(raw,'source_state',None)
    def source_observations():
        observations={}
        for index,(tf,ts,stamp) in enumerate(state['observations'] if state else ()):
            if index % 1024 == 0:
                checkpoint()
            observations[(tf,datetime.fromisoformat(ts))]=datetime.fromisoformat(stamp)
        return observations
    factory=getattr(service,'calendar_factory',calendar_for)
    try:
        calendar=factory(symbol)
        if pilot_requested and not futures.enabled(symbol,calendar):
            raise CalendarUnavailable('instrument_calendar_unknown')
    except CalendarUnavailable:
        if bucket_cache is not None:
            bucket_cache.rebase((symbol,None),observed)
        result={tf:bars for tf,bars in raw.items() if tf not in ('H4',*NATIVE_HIGHER)}
        issues={tf:'instrument_calendar_unknown' for tf in ('H4',*NATIVE_HIGHER)}
        if primary_native and not (pilot_requested or ru):
            native,native_issues=native_higher_series(raw,observed,source_observations())
            result.update(native)
            issues={**{'H4':'instrument_calendar_unknown'},**native_issues}
        return PreparedSeries(result,
            {'version':moex.POLICY if ru else futures.POLICY if pilot_requested else NATIVE_POLICY_VERSION if primary_native else AGGREGATION_VERSION,
             'profile':'unknown','issues':issues,
             'origins':{tf:'finam_native' for tf in NATIVE_HIGHER} if primary_native and not (pilot_requested or ru) else {},
             'control_issues':{tf:'instrument_calendar_unknown' for tf in NATIVE_HIGHER} if primary_native else {}})
    pilot=primary_native and not direct and futures.enabled(symbol,calendar)
    if pilot:calendar=futures.calendar_for_family(calendar)
    versions=(AGGREGATION_VERSION,calendar.revision,NATIVE_POLICY_VERSION if primary_native else None)
    full_archive=bool(state and state.get('full_archive'))
    versions+=(full_archive,)
    if direct:versions+=('finam-h1-h4-native-dwm-1',)
    if pilot:versions+=(futures.POLICY,)
    ru_progress=state.get('moex_progress') if state else None
    if ru:versions+=(moex.POLICY,moex.REVISION,ru_progress)
    policy=hashlib.sha256(repr(versions).encode()).hexdigest()
    if bucket_cache is not None:
        bucket_cache.rebase((symbol,policy),observed,bucket_changes)
    # History retention changes H1/H4 and the archive-mode flag, but must not
    # discard an already coherent Last/Open family during a source outage.
    # Accept only precisely matching calendar/family policies, including the
    # pre-archive version; never a different session or closing-price rule.
    family_cache_policies={policy}
    if pilot or ru:
        sibling=list(versions);sibling[3]=not full_archive
        family_cache_policies.add(hashlib.sha256(repr(tuple(sibling)).encode()).hexdigest())
        legacy=('session-ohlcv-1',calendar.revision,NATIVE_POLICY_VERSION)
        legacy+=(futures.POLICY,) if pilot else (moex.POLICY,moex.REVISION,ru_progress)
        family_cache_policies.add(hashlib.sha256(repr(legacy).encode()).hexdigest())
    with closing(service._connection()) as db:
        revision=state['revision'] if state else -1
        saved=db.execute('SELECT policy,revision,observed_at,payload FROM session_series_cache WHERE symbol=?',(symbol,)).fetchone()
        if saved and saved[0]==policy and saved[1]==revision:
            prior=datetime.fromisoformat(saved[2])
            if prior<=observed and prior.replace(second=0,microsecond=0)==observed.replace(second=0,microsecond=0):
                try:
                    checkpoint()
                    data=json.loads(saved[3])
                    if not isinstance(data['metadata'],dict) or data['metadata'].get('policy')!=policy:
                        raise ValueError('Invalid derived metadata')
                    result={tf:bars for tf,bars in raw.items() if tf not in ('H1','H4','D1','W1','MN1')}
                    result.update({tf:[_candle(b) for b in bars] for tf,bars in data['derived'].items()
                                   if tf in ('H1','H4','D1','W1','MN1')})
                    checkpoint()
                    return PreparedSeries(result,data['metadata'])
                except (ValueError,TypeError,KeyError):
                    pass  # Damaged derived data is rebuilt from untouched raw bars.
        windows=state['coverage'] if state else []
    observed_at=source_observations()
    native,native_issues=native_higher_series(raw,observed,observed_at) if primary_native else ({},{})
    coverage={}
    for tf,start,end in windows:
        coverage.setdefault(tf,[]).append((datetime.fromisoformat(start),datetime.fromisoformat(end)))
    # Legacy raw bars have no delivery/finalization evidence. Do not let a
    # previously saved developing candle become confirmed just because the
    # wall clock advanced. The first bounded HTTP refresh revalidates them.
    if state is not None:
        unknown=datetime.min.replace(tzinfo=timezone.utc)
        for tf,bars in raw.items():
            for index,bar in enumerate(bars):
                if index % 1024 == 0:
                    checkpoint()
                observed_at.setdefault((tf,bar.time),unknown)
    try:
        builder=build_h4_from_finam_h1 if direct else build_session_series
        built=builder(raw,calendar,observed,coverage=coverage,observed_at=observed_at,
                      bucket_cache=bucket_cache)
    except (ValueError,OverflowError):
        if not primary_native:raise
        built=SessionSeries({tf:bars for tf,bars in raw.items() if tf in ('M1','M5')},
            {tf:'control_source_invalid' for tf in ('H1','H4',*NATIVE_HIGHER)})
    checkpoint()
    metadata={'version':NATIVE_POLICY_VERSION if primary_native else built.version,
              'profile':calendar.profile,'policy':policy,
              'history_breaks':built.breaks,
              'issues':built.issues,'origins':built.origins,'calendar_parity':'unverified_tradingview'}
    if primary_native:
        metadata['control_issues']={} if direct else control_issues(built,native,calendar,observed)
        metadata['control_counts']={tf:len(built.series.get(tf,())) for tf in NATIVE_HIGHER}
        metadata['issues']={**{tf:issue for tf,issue in built.issues.items() if tf not in NATIVE_HIGHER},**native_issues}
        metadata['origins']={**built.origins,**{tf:'finam_native' for tf in NATIVE_HIGHER}}
        built.series={tf:bars for tf,bars in built.series.items() if tf not in NATIVE_HIGHER}
        built.series.update(native)
        metadata['history_breaks']={tf:rows for tf,rows in built.breaks.items() if tf not in NATIVE_HIGHER}
        if pilot or ru:
            # A selected trading-day family never falls back to differently
            # grouped native D/W/M while its source history is unavailable.
            for tf in NATIVE_HIGHER:built.series.pop(tf,None)
            family_policy=moex.POLICY if ru else futures.POLICY
            origin='finam_moex_trading_day' if ru else 'us_futures_session_last_trade'
            snapshot_key='moex_snapshot_at' if ru else 'futures_snapshot_at'
            metadata['version']=family_policy
            metadata['issues']={tf:value for tf,value in metadata['issues'].items() if tf not in NATIVE_HIGHER}
            metadata['origins']={tf:value for tf,value in metadata['origins'].items() if tf not in NATIVE_HIGHER}
            metadata['control_issues']={}
            try:
                checkpoint()
                if ru:
                    candidate,status,detail=moex.build_family(raw,symbol,observed,
                        observations=observed_at,archive_verified=moex.archive_ready(raw,symbol),coverage=coverage)
                else:
                    candidate,status,detail=futures.build_family(raw,calendar,observed,
                        coverage=coverage,observations=observed_at)
                    if candidate is None and full_archive:
                        candidate,status,detail=futures.build_verified_archive(raw,calendar,observed,
                            coverage=coverage,observations=observed_at)
            except (ValueError,OverflowError):
                candidate,status,detail=None,'control_source_invalid',{'policy':family_policy}
            metadata['moex_staging' if ru else 'futures_staging']={**detail,'status':status}
            if candidate is not None:
                # Publish all three frames from one source snapshot. The RU
                # candidate explicitly preserves its unaffected native prefix.
                built.series.update(candidate)
                if not ru and detail.get('history_breaks'):
                    metadata['history_breaks'].update({tf:rows for tf,rows in detail['history_breaks'].items()
                                                       if tf in NATIVE_HIGHER})
                metadata['version']=family_policy
                metadata['origins']={**metadata['origins'],
                    **{tf:origin for tf in NATIVE_HIGHER}}
                metadata['control_issues']={}
                metadata[snapshot_key]=observed.isoformat()
            else:
                metadata['issues'].update({tf:status for tf in NATIVE_HIGHER})
                # Once promoted, a transient missing source must not switch
                # the chart back to a differently defined native family.
                # Keep the last coherent snapshot, never sealing its old
                # drafts merely because the wall clock has advanced.
                try:
                    previous=json.loads(saved[3]) if saved and saved[0] in family_cache_policies else {}
                    prior_meta=previous.get('metadata',{})
                    stamp=datetime.fromisoformat(prior_meta[snapshot_key])
                    if prior_meta.get('version')==family_policy and stamp<=observed:
                        retained={tf:[bar for row in previous['derived'].get(tf,())
                            if (bar:=_candle(row)).scheduled_close<=stamp or bar.scheduled_close>observed]
                            for tf in NATIVE_HIGHER}
                        built.series.update(retained)
                        metadata['history_breaks'].update({tf:rows for tf,rows in prior_meta.get('history_breaks',{}).items()
                                                           if tf in NATIVE_HIGHER})
                        metadata['version']=family_policy
                        metadata[snapshot_key]=stamp.isoformat()
                        metadata['origins']={**metadata['origins'],
                            **{tf:origin for tf in NATIVE_HIGHER}}
                        metadata['issues']={**metadata['issues'],**{tf:status for tf in NATIVE_HIGHER}}
                except (ValueError,KeyError,TypeError,AttributeError):pass
    checkpoint()
    from .diagnostics_log import log_candle_policy
    signature=(policy,tuple(sorted(metadata['issues'].items())),tuple(sorted(metadata.get('control_issues',{}).items())))
    with service.lock:
        prior=getattr(service,'_last_candle_diagnostics',{})
        should_log=prior.get(symbol)!=signature
        prior[symbol]=signature;service._last_candle_diagnostics=prior
    if should_log:
        log_candle_policy(symbol=symbol,profile=calendar.profile,issues=metadata['issues'],
                          policy=metadata['version'],control_issues=metadata.get('control_issues'),
                          counts={tf:len(built.series.get(tf,[])) for tf in ('H4','D1','W1','MN1')})
    payload=json.dumps({'derived':{tf:[serialize_candle(b) for b in bars]
        for tf,bars in built.series.items() if tf in ('H1','H4','D1','W1','MN1')},'metadata':metadata})
    with service.lock,closing(service._connection()) as db,db:
        checkpoint()
        # Do not publish a cache whose source changed during computation.
        current=db.execute('SELECT revision FROM candle_source_revisions WHERE symbol=?',(symbol,)).fetchone()
        if revision>=0 and (current[0] if current else 0)==revision:
            db.execute('INSERT OR REPLACE INTO session_series_cache VALUES(?,?,?,?,?)',
                       (symbol,policy,revision,observed.isoformat(),payload))
    return PreparedSeries(built.series,metadata)


def backfill(service,symbol,now,*,cancelled=lambda:False):
    """One bounded source window per background pass, persistent across restarts."""
    if getattr(service.api,'provider_name','Finam')=='Binance':return {}
    try:calendar=service.calendar_factory(symbol)
    except CalendarUnavailable:return {'H4':'нет подтверждённого календаря инструмента'}
    direct=bool(getattr(service.api,'h1_only_h4_native_dwm',False))
    if not direct and native_higher_enabled(service.api) and futures.enabled(symbol,calendar):
        return futures.backfill(service,symbol,now,cancelled=cancelled)
    if not direct and native_higher_enabled(service.api) and moex.enabled(symbol):
        result=moex.backfill(service,symbol,now,cancelled=cancelled)
        if result:return result
    tf='H1' if direct else ('M5' if calendar.profile in ('US_EQUITY_RTH','MOEX_STOCK_FULL') else 'H1')
    raw=service._series(symbol)
    bars=raw.get(tf,[])
    cursor=min((b.time for b in bars),default=now)
    progress=service.load_history_progress().get((symbol,'SESSION_'+tf),{})
    if progress.get('cursor') is not None:cursor=min(cursor,progress['cursor'])
    if progress.get('exhausted'):return {}
    earliest=(datetime(2026,3,23,tzinfo=timezone.utc) if calendar.profile=='MOEX_FUTURE_FULL'
              else datetime(2026,1,1,tzinfo=timezone.utc))
    # Do not request history before a contract's first known native daily bar.
    if raw.get('D1'):earliest=max(earliest,min(b.time for b in raw['D1']))
    if cursor<=earliest:return {}
    if cancelled():raise InterruptedError()
    start=max(earliest,cursor-timedelta(days=30))
    older=service.api.bars(symbol,tf,start=start,end=cursor)
    if cancelled():raise InterruptedError()
    if older:service._upsert(symbol,tf,older)
    record_response(service,symbol,tf,older,start,cursor,now)
    service.save_history_progress(symbol,'SESSION_'+tf,
        {'cursor':start,'exhausted':start<=earliest or not older})
    return {'H4':'дозагрузка исходной истории сессий'}
