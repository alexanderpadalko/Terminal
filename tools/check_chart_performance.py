"""Measure the real Qt hover path and verify avoided v2.11 history replay."""
import json
import os
from pathlib import Path
from statistics import median
import sys
import time

os.environ['QT_QPA_PLATFORM']='windows' if os.name=='nt' else 'offscreen'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from flowdesk.gui import CandleChart
from test_chart_performance_gui import large_frame
from test_analysis_reuse import TestAnalysisReuse
from flowdesk.pipeline import analyze_timeframes
from datetime import timedelta


def main():
    app=QApplication([]);chart=CandleChart()
    chart.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    chart.resize(1440,900);chart.set_analysis(large_frame(40000))
    chart.show();app.processEvents();chart.grab()
    result={'history_bars':40000}
    for forced,name in [(True,'full_paint_ms'),(False,'hover_paint_ms')]:
        samples=[]
        for i in range(40):
            chart.cursor_x=chart.x0+100+i*3;chart.cursor_y=chart.top+100+i
            if forced:chart._surface_key=None
            started=time.perf_counter();chart.grab()
            samples.append((time.perf_counter()-started)*1000)
        result[name]=round(median(samples),3)
    result['paint_speedup']=round(result['full_paint_ms']/max(.001,result['hover_paint_ms']),2)
    series=TestAnalysisReuse().series()
    cutoff=series['H1'][-1].time+timedelta(minutes=20,seconds=30)
    first,parents=analyze_timeframes(series,'M1',asof=cutoff)
    started=time.perf_counter()
    analyze_timeframes(series,'M1',asof=cutoff+timedelta(minutes=1))
    result['fresh_replay_ms']=round((time.perf_counter()-started)*1000,3)
    started=time.perf_counter()
    updated,new_parents=analyze_timeframes(series,'M1',asof=cutoff+timedelta(minutes=1),cached={'M1':first,**parents})
    result['cached_replay_ms']=round((time.perf_counter()-started)*1000,3)
    result['unchanged_hour_reused']=new_parents['H1'].flows is parents['H1'].flows
    assert result['unchanged_hour_reused']
    assert result['hover_paint_ms']<result['full_paint_ms'],result
    Path('chart-performance-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print('CHART_PERFORMANCE',json.dumps(result),flush=True)
    chart.close()


if __name__=='__main__':main()
