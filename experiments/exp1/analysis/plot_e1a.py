"""Paper-style traffic sensitivity and cost/latency trade-offs from measured reports."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

STYLE={'aws_lambda':('AWS Lambda','#0072B2','o'),'aws_agentcore':('AWS AgentCore','#56B4E9','s'),
       'ali_fc':('Alibaba FC','#009E73','^'),'ali_agentrun':('Alibaba AgentRun','#D55E00','D')}
ROLES=('low','typical','high')


def plot(source,output):
    source=Path(source);data=json.loads(source.read_text());reports=data['reports']
    if not reports:raise ValueError('No measured reports')
    if len({r['pilot'] for r in reports})!=1:raise ValueError('Plot Pilot and formal experiments separately')
    if len({r['request_table_sha256'] for r in reports})!=1:raise ValueError('Different request tables cannot form one paired figure')
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    plt.rcParams.update({'font.family':'serif','font.serif':['STIXGeneral'],'mathtext.fontset':'stix','font.size':10,
        'axes.titlesize':11,'axes.labelsize':10,'xtick.labelsize':9,'ytick.labelsize':9,'pdf.fonttype':42,
        'ps.fonttype':42,'axes.spines.top':False,'axes.spines.right':False,'savefig.dpi':360})
    pilot=reports[0]['pilot'];title='Pilot: '+ 'Fixed-Workload Traffic Sensitivity' if pilot else 'E1-A: Fixed-Workload Traffic Sensitivity'
    fig,axes=plt.subplots(1,3,figsize=(11.4,3.25));fig.subplots_adjust(left=.07,right=.99,bottom=.26,top=.78,wspace=.32)
    measures=[('latency_ms_success_only','p95','P95 successful-task latency (s)',.001),('total_cost_cny',None,'Actual cost per successful task (CNY)',1),('slo_attainment',None,'Requests meeting the SLO (%)',100)]
    handles=[]
    for provider,(label,color,marker) in STYLE.items():
        rows=sorted([r for r in reports if r['provider']==provider],key=lambda r:r['arrival_rate_per_s'])
        for ax,(field,stat,ylabel,scale) in zip(axes,measures):
            points=[]
            for r in rows:
                value=r[field].get(stat) if stat else r.get(field)
                if field=='total_cost_cny':
                    value=value/r['successes'] if value is not None and r['successes'] and r['cost_status']=='complete_actual_billing' else None
                if value is not None:points.append((r['arrival_rate_per_s'],value*scale))
            if points:
                line,=ax.plot(*zip(*points),color=color,marker=marker,markersize=5,linewidth=1.6,label=label)
                if ax is axes[0]:handles.append(line)
    for i,(ax,(_,_,ylabel,_)) in enumerate(zip(axes,measures)):
        ax.set_title(f'({chr(97+i)}) '+('Latency','Complete cost','SLO attainment')[i],loc='left',fontweight='bold',pad=10)
        ax.set_xlabel('Sampled Session arrivals / s');ax.set_ylabel(ylabel);ax.grid(axis='y',color='#D8DDE4',linewidth=.55,alpha=.75);ax.set_axisbelow(True);ax.xaxis.set_major_locator(MaxNLocator(4))
        if not ax.lines:
            message='Awaiting complete\nactual billing evidence' if i==1 else 'No successful\nmeasured requests'
            ax.text(.5,.5,message,ha='center',va='center',transform=ax.transAxes,color='#667085',fontsize=10)
    axes[2].set_ylim(0,105)
    fig.suptitle(title,fontsize=14,fontweight='bold',y=.98)
    if handles:fig.legend(handles=handles,loc='lower center',bbox_to_anchor=(.5,.005),ncol=4,frameon=False,fontsize=9)
    for extension in ('png','pdf'):fig.savefig(output/('e1a_traffic_sensitivity.'+extension))
    plt.close(fig)
    fig,axes=plt.subplots(1,3,figsize=(11.4,3.25),sharey=True);fig.subplots_adjust(left=.07,right=.99,bottom=.23,top=.78,wspace=.25)
    for ax,role in zip(axes,ROLES):
        ax.set_title(role.title()+' traffic',loc='left',fontweight='bold');count=0
        for r in reports:
            if r['window_role']!=role or r.get('total_cost_cny') is None or r['cost_status']!='complete_actual_billing' or not r['successes']:continue
            latency=r['latency_ms_success_only']['p95']
            if latency is None:continue
            label,color,marker=STYLE[r['provider']];ax.scatter(r['total_cost_cny']/r['successes'],latency/1000,c=color,marker=marker,s=48,edgecolor='white',linewidth=.6);count+=1
        if not count:ax.text(.5,.5,'Awaiting paired latency\nand complete billing',transform=ax.transAxes,ha='center',va='center',color='#667085')
        ax.set_xlabel('Actual cost / successful task (CNY)');ax.grid(color='#D8DDE4',linewidth=.55);ax.set_axisbelow(True)
    axes[0].set_ylabel('P95 successful-task latency (s)');fig.suptitle(('Pilot: ' if pilot else 'E1-A: ')+'Cost–Latency Trade-offs Across Traffic Windows',fontsize=14,fontweight='bold',y=.98)
    fig.text(.5,.015,'Optimality requires success ≥ 99% and SLO attainment ≥ 95%; absent cost evidence is never plotted as zero.',ha='center',fontsize=9,color='#667085')
    for extension in ('png','pdf'):fig.savefig(output/('e1a_cost_latency.'+extension))
    plt.close(fig)
    import hashlib
    manifest={'source':str(source),'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
        'pilot':pilot,'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}}
    (output/'plot_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('comparison',type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();plot(a.comparison,a.output)
