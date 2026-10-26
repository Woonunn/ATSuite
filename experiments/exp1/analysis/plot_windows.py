"""Independent zoom plots of the selected low/typical/high original-time windows."""
import argparse
import csv
import json
from pathlib import Path
from datetime import datetime,timezone
import math
import numpy as np
from experiments.exp1.runner.validate_arrivals import load_span
from experiments.exp1.runner.arrival_scheduler import ms_ns
from experiments.exp1.data.prepare_copilot import sha,ROOT
from experiments.exp1.data.extract_copilot_arrivals import timestamp_ns


def plot(traffic,output):
    traffic,output=Path(traffic),Path(output)
    summary=json.loads((traffic/'traffic_summary.json').read_text())
    windows=summary['selected_windows'];roles=('low','typical','high');series=[];table=[]
    durations={w['duration_ms'] for w in windows.values()}
    if len(durations)!=1:raise ValueError('Compare windows of equal duration')
    duration_ms=next(iter(durations));minutes=duration_ms/60000
    duration_ns=ms_ns(duration_ms);bin_ns=10*10**9
    if duration_ns%bin_ns:raise ValueError('Window must contain complete ten-second bins')
    edges=np.arange(0,duration_ns+bin_ns,bin_ns,dtype=np.int64)
    for role in roles:
        arrivals,_=load_span(traffic,role,0,duration_ms)
        offsets=np.array([ms_ns(a.relative_arrival_ms) for a in arrivals],dtype=np.int64)
        counts,_=np.histogram(offsets,bins=edges)
        if len(arrivals)!=windows[role]['arrival_count'] or int(counts.sum())!=len(arrivals):raise ValueError('Plot counts disagree with selected window')
        series.append((role,offsets,counts))
        for start,count in zip(edges[:-1],counts):table.append({'role':role,'bin_start_seconds':int(start)//10**9,'bin_end_seconds':int(start+bin_ns)//10**9,'arrivals':int(count)})
    output.mkdir(parents=True,exist_ok=False)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.ticker import MultipleLocator
    colors={'low':'#0072B2','typical':'#009E73','high':'#D55E00'}
    labels={'low':'(a) Low traffic','typical':'(b) Typical traffic','high':'(c) High traffic'}
    max_bin=max(int(counts.max()) for _,_,counts in series)
    ceiling=max(5,math.ceil(max_bin/5)*5)
    style={'font.family':'serif','font.serif':['STIXGeneral'],'mathtext.fontset':'stix',
        'font.size':10,'axes.labelsize':10,'axes.titlesize':11,'xtick.labelsize':9,'ytick.labelsize':9,
        'axes.linewidth':.7,'xtick.major.width':.7,'ytick.major.width':.7,
        'pdf.fonttype':42,'ps.fonttype':42,'savefig.facecolor':'white'}
    def draw_panel(ax,role,offsets,counts):
        w=windows[role];start_ns=timestamp_ns(w['start_utc'])
        date_edges=mdates.date2num([datetime.fromtimestamp((start_ns+int(edge))/1e9,timezone.utc) for edge in edges])
        ax.bar((date_edges[:-1]+date_edges[1:])/2,counts,width=np.diff(date_edges)*.82,
            color=colors[role],edgecolor='none',alpha=.88,zorder=3)
        ax.set_xlim(date_edges[0],date_edges[-1]);ax.set_ylim(0,ceiling)
        ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=2 if minutes<=15 else 10,tz=timezone.utc))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=timezone.utc))
        ax.yaxis.set_major_locator(MultipleLocator(5))
        ax.set_xlabel('Time (UTC)',labelpad=4);ax.set_ylabel('Requests per 10 s',labelpad=5)
        ax.set_title(labels[role],loc='left',pad=23)
        ax.text(0,1.025,w['start_utc'][:10],transform=ax.transAxes,fontsize=9,color='#555555',ha='left',va='bottom')
        ax.text(1,1.025,f"$n = {len(offsets)}$",transform=ax.transAxes,fontsize=9,color='#333333',ha='right',va='bottom')
        ax.spines[['top','right']].set_visible(False)
        ax.spines[['left','bottom']].set_color('#444444')
        ax.tick_params(direction='out',length=3,pad=3)
        ax.set_axisbelow(True);ax.grid(axis='y',color='#dedede',linewidth=.55,linestyle='--',dashes=(3,3))
    with plt.rc_context(style):
        for role,offsets,counts in series:
            fig,ax=plt.subplots(figsize=(3.8,2.65),layout='constrained')
            draw_panel(ax,role,offsets,counts)
            name=f'traffic_{role}_{minutes:g}min'
            fig.savefig(output/(name+'.png'),dpi=360)
            fig.savefig(output/(name+'.pdf'),metadata={'Creator':'Wabber Copilot traffic plotting','CreationDate':None})
            plt.close(fig)
        fig,axes=plt.subplots(1,3,figsize=(11.4,3.2),sharey=True,layout='constrained')
        for index,(ax,(role,offsets,counts)) in enumerate(zip(axes,series)):
            draw_panel(ax,role,offsets,counts)
            if index:ax.set_ylabel('')
        title=f'Sampled Copilot Arrivals Across {minutes:g}-Minute Traffic Windows'
        fig.suptitle(title,fontsize=14)
        name=f'traffic_windows_{minutes:g}min_combined'
        fig.savefig(output/(name+'.png'),dpi=360)
        fig.savefig(output/(name+'.pdf'),metadata={'Creator':'Wabber Copilot traffic plotting','CreationDate':None})
        plt.close(fig)
    with (output/'window_bins.csv').open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['role','bin_start_seconds','bin_end_seconds','arrivals']);writer.writeheader();writer.writerows(table)
    manifest={'traffic_summary_sha256':sha(traffic/'traffic_summary.json'),'arrivals_sha256':summary['arrivals_sha256'],
        'window_minutes':minutes,'bin_seconds':10,'style':'serif, embedded TrueType PDF, common y-axis, UTC time axis',
        'combined_title':title,'shared_y_limits':[0,ceiling],'selected_windows':windows,'time_scale':1.0,
        'files':{p.name:sha(p) for p in output.iterdir() if p.is_file()}}
    (output/'plot_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');return manifest


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--traffic',required=True,type=Path);p.add_argument('--output',type=Path,default=ROOT/'experiments/exp1/figure')
    a=p.parse_args();r=plot(a.traffic,a.output);print(json.dumps({'window_minutes':r['window_minutes'],'output':str(a.output)}))
