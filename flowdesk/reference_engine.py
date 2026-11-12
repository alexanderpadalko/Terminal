"""Bridge pinned v2.11 numeric state to the existing chart data model."""
from bisect import bisect_left,bisect_right
from copy import deepcopy
from datetime import datetime
from .reference_runtime import ReferenceContext
from .market_data import MOSCOW


def stamp(value):
    return datetime.fromtimestamp(value/1000,MOSCOW) if value is not None else None


class PinnedFlowEngine:
    def __init__(self,timeframe,settings):
        self.context=ReferenceContext(timeframe,settings)
        self.history=[]
        self.times=[]
        self.last_change_i=None
        self.last_change_rule=0
        self.last_pair=None

    def step(self,bar,i,structure,zone,candles,pair,*,capture=None,higher_audit=''):
        from .engine import FVG,FlowState,PairSnapshot
        context=self.context
        old_state=context.states.get('f_flowSnapshot',{})
        old_guard=(old_state.get('guardBottom'),old_state.get('guardTop'))
        context.observe(candles,i)
        self.times.append(int(bar.time.timestamp()*1000))
        def index(time):
            return bisect_left(self.times,time) if time is not None else None
        event=((capture.detail or f'{capture.source_tf} {capture.kind} {capture.level:g} от {capture.captured_at:%Y-%m-%d %H:%M}') if capture else '')
        event_time=int(capture.captured_at.timestamp()*1000) if capture else None
        previous,current,level=context.call('f_flowSnapshot',context.tf!='MN1',
            event if capture and capture.side==1 else '',event if capture and capture.side==-1 else '',
            higher_audit or (capture.audit if capture else ''),event_time if capture and capture.side==1 else None,event_time if capture and capture.side==-1 else None,True)
        state=context.states['f_flowSnapshot']
        rule=state.get('changeRule',0)
        from .liquidity import serialize_capture
        chosen=state.get('chosenRuleOne')
        selected=state.get('selectedZone') if rule==1 else chosen.zone if chosen else None
        evidence={'outcome':'APPLIED_RULE_1' if rule==1 else 'PINNED_V211','applied':rule==1,
                  'eligible':rule==1,'source':'v2.11','higher_capture':serialize_capture(capture),
                  'first_fvg':({'bottom':old_guard[0],'top':old_guard[1]} if old_guard[0] is not None else None),
                  'opposite_fvg':({'bottom':selected.bottom,'top':selected.top,
                                   'formed_at':stamp(chosen.formedAt).isoformat() if chosen else None,
                                   'range_start_at':stamp(chosen.originTime).isoformat() if chosen else None} if selected else None)}
        if rule or not self.history or self.history[-1].side!=current.direction:
            self.last_change_i=i
            self.last_change_rule=rule
        guard=None
        if current.firstTop is not None and current.firstBottom is not None and current.workingImpulseTime is not None:
            impulse=index(current.workingImpulseTime)
            guard=FVG(index(current.workingLeftTime),impulse,min(i,impulse+1),current.firstTop,current.firstBottom,
                      current.direction,balanced=state.get('guardBalanced',False),
                      broken=index(state.get('firstBrokenAt')) if current.firstBroken else None,source_tf=context.tf)
        old=None
        if current.prevStart is not None and current.prevEnd is not None:
            old=(index(current.prevStart),max(0,bisect_left(self.times,current.prevEnd)-1),current.prevLo,current.prevHi,
                 self.history[-1].side if self.history else 0)
        self.last_pair=PairSnapshot(level.structureHigh,level.structureLow,index(level.idmHighKey),index(level.idmLowKey),level.structureDirection)
        diagnostic=current.diagnostic
        if self.history and self.history[-1].diagnosis==diagnostic:diagnostic=self.history[-1].diagnosis
        result=FlowState(i,current.direction,current.lo,current.hi,index(current.startTime),old,rule,diagnostic,
            guard,current.firstBroken,self.last_pair,source_tf=context.tf,
            range_start_at=stamp(current.startTime),range_low_at=stamp(state.get('flowLoTime')),
            range_high_at=stamp(state.get('flowHiTime')),last_change_at=candles[self.last_change_i].time if self.last_change_i is not None else None,
            last_change_rule=self.last_change_rule,range_origin=state.get('flowDiagnostic',''),
            guard_balanced=state.get('guardBalanced',False),
            rule1_check=evidence,change_evidence=deepcopy(evidence) if rule==1 else {})
        self.history.append(result)
        return result


def market_state(analysis,settings):
    """Run the actual v2.11 replay and live-shadow blocks on chart pivots."""
    from .engine import StructureEngine
    candles=analysis.candles
    if analysis.asof is not None:
        candles=candles[:bisect_right(candles,analysis.asof,key=lambda bar:bar.time)]
    if (not candles or not settings.enableMarketStructureInput or
            settings.showMarketOnlyOnH1 and analysis.timeframe not in ('H1','H4')):
        return {}
    if len(candles)>100000:
        return {'limit_reached':True,'marketStatusText':'BOS/CHOCH/MAX/MIN отключены: история превышает 100 000 свечей'}
    context=ReferenceContext(analysis.timeframe,settings)
    fields={'marketBarHigh':[b.high for b in candles],'marketBarLow':[b.low for b in candles],
            'marketBarClose':[b.close for b in candles], 'marketBarTime':[int(b.time.timestamp()*1000) for b in candles],
            'marketPivotHigh':[None]*len(candles),'marketPivotLow':[None]*len(candles)}
    for name in ('marketRecordHigh','marketRecordLow','marketHighRecordBars','marketLowRecordBars',
                 'marketPreviousHighBar','marketPreviousLowBar','marketRangeLayers','marketBodyHighNext','marketBodyLowNext'):
        fields[name]=[]
    structure=StructureEngine()
    prior=None
    for i,bar in enumerate(candles):
        point=structure.step(bar,i)
        if prior and prior.outside_dir and point.outside_dir and point.start_i!=prior.start_i:
            arr=fields['marketPivotHigh' if point.outside_dir==1 else 'marketPivotLow']
            if prior.start_i is not None:arr[prior.start_i]=None
            if point.start_i is not None:arr[point.start_i]=point.base
        if point.turn_down and point.start_i is not None:
            fields['marketPivotHigh'][point.start_i]=point.base
        if point.turn_up and point.start_i is not None:
            fields['marketPivotLow'][point.start_i]=point.base
        prior=point
    context.observe(candles,len(candles)-1)
    context.env.update(fields)
    confirmed=sum(analysis.is_confirmed(i) for i in range(len(candles)))
    if confirmed==0:return {}
    context.env['barstate'].isconfirmed=confirmed==len(candles)
    context.env['enableMarketStructure']=True
    context.section('market_init')
    context.section('market_replay')
    context.section('market_live')
    display_names={'marketReady','marketHasExtreme','marketRangeOnly','marketDirection','marketStatusText','marketStartText',
                   'marketChartMaximum','marketChartMaximumBar','marketChartMinimum','marketChartMinimumBar'}
    display_names.update('market'+prefix+suffix for prefix in ('Bos','Choch','Extreme')
                         for suffix in ('Value','TriggerValue','Bar','BreakBar','Idm','IdmBar','IdmSweepBar','StepBars','StepValues'))
    result={k:deepcopy(v) for k,v in context.env.items() if k in display_names}
    result['source_right_index']=len(candles)-.5
    return result
