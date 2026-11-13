"""Paint v2.11 market levels, VWAP, summary and diagnostics in the real chart."""
from PySide6.QtCore import Qt,QPointF,QRectF
from PySide6.QtGui import QFont,QFontMetrics,QPen,QColor,QBrush
from .settings import SIZES
from .settings_dialog import qcolor
from .chart_indicators import summary_rows, period_key


def font(size):
    value=QFont('Segoe UI')
    value.setPixelSize(SIZES[size])
    return value


def draw_market(chart,p,x,y,first,last):
    state=chart.analysis.market
    if not state:return
    cfg=chart.indicator_settings
    def line(points,color,width,label=None,dashed=False):
        pen=QPen(qcolor(color),width)
        if dashed:pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        for a,b in zip(points,points[1:]):
            if b[0]<first or a[0]>last:continue
            p.drawLine(QPointF(x(a[0]),y(a[1])),QPointF(x(b[0]),y(b[1])))
        if label and points:
            right,value=points[-1]
            p.setFont(font('small'))
            p.drawText(QRectF(x(right)+4,y(value)-12,150,24),Qt.AlignmentFlag.AlignLeft,label)
    def horizontal(start,end,value,color,width,label,dashed=False):
        if value is not None and start is not None:
            line([(start,value),(end,value)],color,width,label,dashed)
    def steps(prefix,color,width,label,end):
        start,value=state.get(prefix+'Bar'),state.get(prefix+'Value')
        if start is None or value is None:return
        points=[(start,value)]
        for index,new_value in zip(state.get(prefix+'StepBars',[]),state.get(prefix+'StepValues',[])):
            if index<=end:
                points.extend([(index,points[-1][1]),(index,new_value)])
        points.append((end,points[-1][1]))
        line(points,color,width,label)
    edge=state.get('source_right_index',len(chart.analysis.candles)-.5)
    if state.get('marketRangeOnly'):
        horizontal(state.get('marketChartMaximumBar'),edge,state.get('marketChartMaximum'),cfg.marketMaxColor,cfg.marketMaxWidth,'Максимум графика')
        horizontal(state.get('marketChartMinimumBar'),edge,state.get('marketChartMinimum'),cfg.marketMinColor,cfg.marketMinWidth,'Минимум графика')
    if state.get('marketReady'):
        steps('marketBos',cfg.marketBosColor,cfg.marketBosWidth,'BOS',state.get('marketBosBreakBar') or edge)
        steps('marketChoch',cfg.marketChochColor,cfg.marketChochWidth,'CHOCH',edge)
        up=state.get('marketDirection')==1
        steps('marketExtreme',cfg.marketMaxColor if up else cfg.marketMinColor,
              cfg.marketMaxWidth if up else cfg.marketMinWidth,'MAX' if up else 'MIN',edge)
        if cfg.showMarketIdmInput:
            for prefix,label in [('marketBos','IDM BOS'),('marketChoch','IDM CHOCH'),('marketExtreme','IDM MAX' if up else 'IDM MIN')]:
                end=state.get(prefix+'IdmSweepBar')
                horizontal(state.get(prefix+'IdmBar'),edge if end is None else end,state.get(prefix+'Idm'),cfg.marketIdmColor,cfg.marketIdmWidth,label,True)


def draw_vwap(chart,p,x,y,first,last):
    cfg=chart.indicator_settings
    if not cfg.vwapEnabled or chart.analysis.timeframe not in ('M1','M5'):return
    p.setPen(QPen(qcolor(cfg.vwapColor),cfg.vwapWidth))
    candles=chart.analysis.candles
    for i in range(max(1,first),last):
        before,current=chart.vwap[i-1:i+1]
        if before is None or current is None:
            continue
        # A new VWAP anchor is a new line, not a diagonal connection
        # from the last point of the previous trading session.
        if period_key(candles[i-1].time,cfg.vwapAnchor,exchange=chart.exchange) != \
                period_key(candles[i].time,cfg.vwapAnchor,exchange=chart.exchange):
            continue
        p.drawLine(QPointF(x(i-1),y(before)),QPointF(x(i),y(current)))
    if cfg.vwapShowPrevious:
        p.setPen(QPen(qcolor(cfg.vwapPreviousColor),cfg.vwapPreviousWidth,Qt.PenStyle.DashLine))
        for start,end,value in chart.vwap_previous:
            if end>=first and start<last:
                p.drawLine(QPointF(x(start),y(value)),QPointF(x(end-.5),y(value)))


def draw_overlays(chart,p):
    cfg=chart.indicator_settings
    if cfg.currentFvgTableEnabled:
        rows=summary_rows(chart.analysis)
        p.setFont(font(cfg.currentFvgFontSize))
        width=max(20,(chart.x1-chart.x0)*cfg.currentFvgCellWidth/100)
        heights=([QFontMetrics(font(cfg.currentFvgFontSize)).height()+4,
                  QFontMetrics(font(cfg.currentFlowArrowSize)).height()+4] if cfg.currentFvgCellHeight==0
                 else [max(8,(chart.bottom-chart.top)*cfg.currentFvgCellHeight/100)]*2)
        left=chart.x1-width*5-6
        top=chart.top+4
        for index,row in enumerate(rows):
            for row_index,key in enumerate(('fvg','flow')):
                side=row[key]
                rect=QRectF(left+index*width,top+sum(heights[:row_index]),width,heights[row_index])
                p.fillRect(rect,QColor('#388E3C' if side==1 else '#B22833' if side==-1 else '#636363'))
                p.setPen(QPen(QColor(210,210,210,90),1))
                p.drawRect(rect)
                p.setPen(QColor('white'))
                p.setFont(font(cfg.currentFvgFontSize if row_index==0 else cfg.currentFlowArrowSize))
                text={'D1':'D','W1':'W','MN1':'M'}.get(row['timeframe'],row['timeframe']) if row_index==0 else '↑' if side==1 else '↓' if side==-1 else '—'
                p.drawText(rect,Qt.AlignmentFlag.AlignCenter,text)
    if cfg.flowDiagnosticEnabled and chart.analysis.flows:
        text=chart.flow_plan.diagnostic
        p.setFont(font(cfg.flowDiagnosticFont))
        width=min(chart.x1-chart.x0-12,p.fontMetrics().horizontalAdvance(max(text.splitlines(),key=len))+16)
        height=min(chart.bottom-chart.top-12,p.fontMetrics().lineSpacing()*len(text.splitlines())+16)
        bottom=chart.bottom-(chart.bottom-chart.top)*cfg.flowDiagnosticBottomOffset/100
        rect=QRectF(chart.x0+6,bottom-height,max(50,width),max(25,height))
        p.fillRect(rect,QColor(0,0,0,217))
        p.setPen(QColor('white'))
        p.drawText(rect.adjusted(8,6,-8,-6),Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignTop,text)
    notes=[]
    if cfg.fvgMtfDiagnostics:
        plan=chart.fvg_plan
        notes.append(f'FVG: показано {len(plan.zones)} · рабочих старших {len(plan.parents)} · своих в расчёте {plan.raw_local}')
    if cfg.showMarketStatusInput and chart.analysis.market:
        notes.append(chart.analysis.market.get('marketStatusText',''))
        notes.append(chart.analysis.market.get('marketStartText',''))
    if notes:
        p.setFont(font('small'))
        text='\n'.join(filter(None,notes))
        rect=QRectF(chart.x0+6,chart.bottom-52,chart.x1-chart.x0-12,48)
        p.fillRect(rect,QColor(0,0,0,185))
        p.setPen(QColor('white'))
        p.drawText(rect,Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter,text)
