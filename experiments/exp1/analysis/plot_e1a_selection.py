"""Publication figures separating observed latency choices from budget forecasts."""
import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

from experiments.exp1.analysis.compare_strategies import compare, PROVIDERS
from experiments.exp1.analysis.plot_e1a import ROLES, STYLE
from experiments.exp1.data.prepare_e1a import ROOT, sha

LABELS = {'aws_lambda': 'AWS Lambda', 'aws_agentcore': 'AWS AgentCore',
          'ali_fc': 'Alibaba FC', 'ali_agentrun': 'Alibaba AgentRun'}
SHORT = {'aws_lambda': 'Lambda', 'aws_agentcore': 'AgentCore',
         'ali_fc': 'FC', 'ali_agentrun': 'AgentRun'}
STRATEGY = {p: 'FaaS' if p in ('aws_lambda', 'ali_fc') else 'Session MCP' for p in PROVIDERS}


def selection_data(reports: list, admission: dict, fx: float) -> tuple:
    """Keep the latency oracle and the pre-run budget ranking as distinct objectives."""
    checked = compare(reports)
    if (len(reports) != 12 or len(checked['comparisons']) != 3
            or any(c['missing_providers'] for c in checked['comparisons'])):
        raise ValueError('All twelve paired formal windows are required')
    if admission['basis'] != 'planning_admission_not_actual_billing' or not math.isfinite(fx) or fx <= 0:
        raise ValueError('An explicit planning admission and positive FX reference are required')
    rows = []
    for r in reports:
        if (r['pilot'] or r['sequence_seed'] != 11 or r['window_role'] not in ROLES
                or r['request_table_sha256'] != admission['request_table_sha256']
                or float(r['window_duration_ms']) != 600000 or r['slo_ms'] != 120000):
            raise ValueError('Use the unchanged seed-11 ten-minute formal matrix')
        proposal = admission['groups'][f"11:{r['window_role']}:{r['provider']}"]
        bound = float(proposal['upper_cost_reservation'])
        currency = proposal['currency']
        if currency not in ('USD', 'CNY') or not math.isfinite(bound) or bound <= 0 or not proposal['approved']:
            raise ValueError('Missing or invalid budget reservation')
        latency = r['latency_ms_success_only']['p95']
        if latency is not None and (not math.isfinite(latency) or latency < 0):
            raise ValueError('Invalid measured latency')
        rows.append({'provider': r['provider'], 'role': r['window_role'], 'requests': r['requests'],
                     'successes': r['successes'], 'success_rate': r['success_rate'],
                     'slo_attainment': r['slo_attainment'], 'rate': r['arrival_rate_per_s'],
                     'p95_s': latency / 1000 if latency is not None else None,
                     'budget_cny': bound * (fx if currency == 'USD' else 1),
                     'budget_cny_per_1000_successes': bound * (fx if currency == 'USD' else 1)
                         * 1000 / r['successes'] if r['successes'] else None,
                     'eligible': r['success_rate'] >= .99 and r['slo_attainment'] >= .95})
    choices = {}
    for role in ROLES:
        eligible = [r for r in rows if r['role'] == role and r['eligible']]
        choices[role] = {
            'observed_p95_minimum': min(eligible, key=lambda r: r['p95_s'])['provider'] if eligible else None,
            'minimum_budget_forecast': min(eligible, key=lambda r: r['budget_cny_per_1000_successes'])['provider'] if eligible else None,
            'actual_cost_minimum': None}
    return rows, choices


def frontier(rows: list) -> list:
    """Return only feasible, discrete nondominated latency/budget points."""
    feasible = [r for r in rows if r['eligible']]
    return sorted([r for r in feasible if not any(
        other['p95_s'] <= r['p95_s']
        and other['budget_cny_per_1000_successes'] <= r['budget_cny_per_1000_successes']
        and (other['p95_s'] < r['p95_s']
             or other['budget_cny_per_1000_successes'] < r['budget_cny_per_1000_successes'])
        for other in feasible)], key=lambda r: r['budget_cny_per_1000_successes'])


def plot(source: Path, admission_path: Path, output: Path) -> None:
    """Export four figures and the exact values, captions, and provenance behind them."""
    source = Path(source); admission_path = Path(admission_path); output = Path(output)
    data = json.loads(source.read_text()); admission = json.loads(admission_path.read_text())
    proofs = {e['path']: e for g in admission['groups'].values() for e in g['evidence']}
    for proof in proofs.values():
        if sha(ROOT / proof['path']) != proof['sha256']:
            raise ValueError('Sealed admission evidence changed: ' + proof['path'])
    manifest_path = next(ROOT / e['path'] for e in proofs.values()
                         if e['sha256'] == admission['workload_manifest_sha256'])
    manifest = json.loads(manifest_path.read_text()); fx_ref = manifest['config']['budget']['fx_reference']
    fx_path = ROOT / fx_ref['path']; fx = json.loads(fx_path.read_text())
    if sha(fx_path) != fx_ref['sha256'] or sha(ROOT / fx['raw_path']) != fx['raw_sha256']:
        raise ValueError('Frozen FX evidence changed')
    rows, choices = selection_data(data['reports'], admission, float(fx['usd_to_cny']))
    output.mkdir(parents=True, exist_ok=False)
    plt.rcParams.update({'font.family': 'serif', 'font.serif': ['STIXGeneral'], 'mathtext.fontset': 'stix',
                         'font.size': 10, 'axes.labelsize': 10, 'axes.titlesize': 11,
                         'xtick.labelsize': 9, 'ytick.labelsize': 9, 'pdf.fonttype': 42, 'ps.fonttype': 42,
                         'svg.fonttype': 'none', 'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.linewidth': .7, 'savefig.dpi': 400})
    rates = [next(r['rate'] for r in rows if r['role'] == role) for role in ROLES]
    high = {r['provider']: r for r in rows if r['role'] == 'high'}
    fastest = high[choices['high']['observed_p95_minimum']]
    cheapest_forecast = high[choices['high']['minimum_budget_forecast']]
    session = high['ali_agentrun']
    legend = [Line2D([], [], color=STYLE[p][1], marker=STYLE[p][2], lw=1.8, markersize=5,
                     label=LABELS[p] + (' (FaaS)' if STRATEGY[p] == 'FaaS' else '')) for p in PROVIDERS]
    captions = []

    def decorate(ax, ylabel):
        ax.set_xticks(rates, [f'{role.title()}\n{rate:.3f}' for role, rate in zip(ROLES, rates)])
        ax.set_xlabel('Offered load (sampled requests/s)'); ax.set_ylabel(ylabel)
        ax.set_xlim(0.045, .85); ax.grid(axis='y', color='#D8DEE6', linewidth=.6, alpha=.7); ax.set_axisbelow(True)

    def curves(ax, key, providers=PROVIDERS):
        for provider in providers:
            values = sorted([r for r in rows if r['provider'] == provider], key=lambda r: r['rate'])
            ax.plot([r['rate'] for r in values], [r[key] for r in values], color=STYLE[provider][1],
                    marker=STYLE[provider][2], lw=1.8, ms=5.5, markeredgecolor='white', markeredgewidth=.5)

    def winners(ax, key, objective):
        selected = [next(r for r in rows if r['role'] == role and r['provider'] == choices[role][objective])
                    for role in ROLES if choices[role][objective] is not None]
        ax.plot([r['rate'] for r in selected], [r[key] for r in selected], color='#283442',
                ls=':', lw=1, marker='o', ms=11, markerfacecolor='none', markeredgewidth=1.1, zorder=5)

    with PdfPages(output / 'paper_figures.pdf') as pdf:
        def save(fig, name, caption):
            for extension in ('pdf', 'png', 'svg'):
                fig.savefig(output / f'{name}.{extension}', facecolor='white')
            pdf.savefig(fig); plt.close(fig); captions.append(name + '\n' + caption)

        fig, ax = plt.subplots(figsize=(5.2, 3.7))
        fig.subplots_adjust(left=.14, right=.97, bottom=.32, top=.86)
        curves(ax, 'p95_s'); winners(ax, 'p95_s', 'observed_p95_minimum')
        decorate(ax, 'P95 successful-task latency (s)'); ax.set_ylim(12, 31)
        ax.annotate(f"AgentCore: {high['aws_agentcore']['p95_s']:.2f} s", xy=(rates[-1], high['aws_agentcore']['p95_s']), xytext=(.42, 29.4),
                         fontsize=9, color=STYLE['aws_agentcore'][1], arrowprops={'arrowstyle': '-', 'color': '#9AA4B2', 'lw': .7})
        ax.annotate(f"Best P95: {SHORT[fastest['provider']]}\n{fastest['p95_s']:.2f} s", xy=(rates[-1], fastest['p95_s']), xytext=(.46, 12.7),
                         fontsize=9, arrowprops={'arrowstyle': '-', 'color': '#7B8794', 'lw': .7})
        fig.legend(handles=legend, loc='lower center', bbox_to_anchor=(.5, .035), ncol=2, frameon=False, fontsize=8.5)
        fig.text(.14, .955, 'Measured latency across traffic windows', fontsize=13, fontweight='bold')
        save(fig, '01_measured_performance', 'Measured successful-task P95 only. The per-window P95 oracle is post hoc, not an executed adaptive policy. Feasibility: success >=99% and SLO attainment >=95% for a 120-second SLO. All 429 responses remain failures in the source data. Lines connect three observed windows; no continuous crossover threshold or confidence interval is inferred.')

        fig, ax = plt.subplots(figsize=(7.4, 3.6)); fig.subplots_adjust(left=.03, right=.99, top=.84, bottom=.17)
        ax.set_xlim(-1.13, 3.02); ax.set_ylim(-.18, 2.03); ax.axis('off')
        for j, role in enumerate(ROLES):
            n = next(r['requests'] for r in rows if r['role'] == role)
            ax.text(j+.46, 1.83, f'{role.title()} load\n{rates[j]:.3f} requests/s; n={n}', ha='center', va='center', fontsize=9)
        for y, objective, label, detail in [(0.96, 'observed_p95_minimum', 'Latency objective', 'Measured P95 minimum'),
                                           (0.03, 'minimum_budget_forecast', 'Budget objective', 'Minimum planning forecast')]:
            ax.text(-.16, y+.43, label, ha='right', fontsize=10, fontweight='bold')
            ax.text(-.16, y+.22, detail, ha='right', fontsize=8, color='#667085')
            for j, role in enumerate(ROLES):
                provider = choices[role][objective]; color = STYLE[provider][1]
                selected = next(r for r in rows if r['role'] == role and r['provider'] == provider)
                ax.add_patch(Rectangle((j, y), .92, .68, facecolor=color+'18', edgecolor=color, lw=1.25))
                ax.text(j+.46, y+.45, SHORT[provider], ha='center', fontsize=11, fontweight='bold', color='#192B3A')
                ax.text(j+.46, y+.26, STRATEGY[provider], ha='center', fontsize=9, color='#435264')
                value = f"P95 = {selected['p95_s']:.2f} s" if objective == 'observed_p95_minimum' else f"{selected['budget_cny_per_1000_successes']:.2f} CNY / 1k successes"
                ax.text(j+.46, y+.08, value, ha='center', fontsize=8, color='#435264')
        fig.text(.055, .95, 'Traffic-dependent deployment choices', fontsize=13, fontweight='bold')
        fig.text(.055, .075, 'Budget row uses pre-run reservations / observed successes. It is not an actual-cost optimum.', fontsize=8.5, color='#986418')
        save(fig, '02_deployment_choices', 'Discrete deployment choices at the three measured loads. The first row minimizes observed P95 among feasible options. The second minimizes the pre-run conservative budget reservation per observed success. These are different objectives. No actual-billing cost optimum or implemented switching policy is claimed.')

        fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.8)); fig.subplots_adjust(left=.09, right=.985, bottom=.32, top=.79, wspace=.32)
        for ax in axes:decorate(ax, 'Reserved CNY / 1,000 successes')
        curves(axes[0], 'budget_cny_per_1000_successes'); winners(axes[0], 'budget_cny_per_1000_successes', 'minimum_budget_forecast')
        curves(axes[1], 'budget_cny_per_1000_successes', ('aws_lambda', 'ali_fc'))
        winners(axes[1], 'budget_cny_per_1000_successes', 'minimum_budget_forecast')
        axes[0].set_ylim(0, 25); axes[1].set_ylim(3.8, 7.6)
        axes[0].set_title('(a) All four deployments', loc='left', fontweight='bold')
        axes[1].set_title('(b) FaaS detail', loc='left', fontweight='bold')
        axes[0].plot(rates[-1], next(r['budget_cny_per_1000_successes'] for r in rows if r['role']=='high' and r['provider']=='ali_agentrun'),
                     marker='x', color='#8B2F20', ms=11, mew=1.6)
        axes[0].annotate(f"{session['success_rate']:.2%} success", xy=(rates[-1], session['budget_cny_per_1000_successes']),
                         xytext=(.38, 13.4), fontsize=8, color=STYLE['ali_agentrun'][1],
                         arrowprops={'arrowstyle': '-', 'color': '#9AA4B2', 'lw': .7})
        axes[1].annotate(f"Lowest forecast:\n{LABELS[cheapest_forecast['provider']]}, {cheapest_forecast['budget_cny_per_1000_successes']:.2f}", xy=(rates[-1], cheapest_forecast['budget_cny_per_1000_successes']), xytext=(.43, 6.0),
                         fontsize=8.5, arrowprops={'arrowstyle': '-', 'color': '#9AA4B2', 'lw': .7})
        fig.text(.09, .96, 'Planning-cost sensitivity', fontsize=13, fontweight='bold')
        fig.legend(handles=legend, loc='lower center', bbox_to_anchor=(.5, .035), ncol=2, frameon=False, fontsize=8.5)
        save(fig, '03_planning_cost', 'Pre-run group reservations divided by successful formal requests, in CNY per 1,000 successes. Conversion uses the sealed ECB reference. This is a planning metric, not actual or metered expenditure; the original cold/idle/ancillary assumptions and margins are retained. The cross marks the high-load Session configuration failing the reliability constraint. The forecast ranking changes from Lambda to Alibaba FC, but actual cost ranking remains unknown.')

        fig, axes = plt.subplots(1, 3, figsize=(8.4, 3.6), sharex=True, sharey=True)
        fig.subplots_adjust(left=.075, right=.985, bottom=.29, top=.79, wspace=.12)
        offsets = {'aws_lambda':(8,-13),'aws_agentcore':(-7,8),'ali_fc':(8,7),'ali_agentrun':(8,5)}
        for ax, role in zip(axes, ROLES):
            values = [r for r in rows if r['role'] == role]; front = frontier(values)
            ax.plot([r['budget_cny_per_1000_successes'] for r in front], [r['p95_s'] for r in front], ls='--', lw=.9, color='#526171', zorder=0)
            for r in values:
                p = r['provider']; x = r['budget_cny_per_1000_successes']; y = r['p95_s']
                ax.scatter(x, y, color=STYLE[p][1], marker=STYLE[p][2] if r['eligible'] else 'x', s=43,
                           linewidth=1.2, zorder=3)
                label = SHORT[p]
                if not r['eligible']:label += f"\n{r['success_rate']:.2%} success"
                ax.annotate(label, (x,y), xytext=offsets[p], textcoords='offset points',
                            ha='right' if offsets[p][0]<0 else 'left', fontsize=7.5, color=STYLE[p][1])
            ax.set_title(role.title()+' traffic', loc='left', fontweight='bold'); ax.set_xlim(3.5,26); ax.set_ylim(12.5,31)
            ax.set_xlabel('Reserved CNY / 1k successes'); ax.grid(color='#D8DEE6', lw=.55, alpha=.65); ax.set_axisbelow(True)
        axes[0].set_ylabel('P95 successful-task latency (s)')
        fig.text(.075, .96, 'Latency versus planning budget', fontsize=13, fontweight='bold')
        fig.text(.075, .895, 'Measured latency; forecast cost. Dashed segments connect feasible nondominated options.', fontsize=8.5, color='#566273')
        fig.legend(handles=legend, loc='lower center', bbox_to_anchor=(.5,.03), ncol=2, frameon=False, fontsize=8.5)
        save(fig, '04_latency_budget_frontier', 'Discrete latency/budget trade-offs for each traffic window. Latency is measured; the horizontal axis is the planning reservation per successful request, not billed expense. Nondominance requires success >=99% and SLO attainment >=95%. The high-load Session MCP point is excluded from the frontier, retaining all 138 initialization failures. Lines are guides between tested deployments, not measurements of intermediate configurations.')

    with (output/'figure_data.csv').open('w', newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    (output/'choices.json').write_text(json.dumps({'choices':choices,'actual_cost_optimum':'unidentified; complete allocated billing unavailable'},indent=2)+'\n')
    (output/'captions.txt').write_text('\n\n'.join(captions)+'\n\nAll figures: csv_excel_34, task seed 0, sequence seed 11, original ten-minute sampled windows; no full-product QPS claim or multi-seed confidence interval. Alibaba AgentRun is the plot label for the existing FC Session-exclusive MCP adapter used in this experiment; a separate AgentRun API was not tested.\n')
    provenance={'comparison':{'path':str(source),'sha256':sha(source)},'admission':{'path':str(admission_path),'sha256':sha(admission_path)},
                'fx':{'path':str(fx_path),'sha256':sha(fx_path)},'script_sha256':sha(Path(__file__)),
                'files':{p.name:sha(p) for p in output.iterdir() if p.is_file()},
                'cost_basis':'pre-run conservative budget reservations / observed successes; not actual billing'}
    (output/'figure_manifest.json').write_text(json.dumps(provenance,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('comparison',type=Path);parser.add_argument('--admission',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    plot(args.comparison,args.admission,args.output)
