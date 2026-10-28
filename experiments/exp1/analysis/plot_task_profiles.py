"""Plot existing offline reference profiles; run with python -m experiments.exp1.analysis.plot_task_profiles."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[3]
TASKS = {"human_15", "csv_excel_34", "csv_excel_29", "human_142", "human_12", "human_7"}


def plot(manifest, output):
    manifest, output = Path(manifest), Path(output)
    source = manifest.read_bytes()
    data = json.loads(source)
    tasks = data["tasks"]
    if len(tasks) != 6 or {t["task_id"] for t in tasks} != TASKS:
        raise ValueError("Expected the six measured DataSciBench candidates")
    environment = data["environment"]
    if environment["repetitions"] != 3:
        raise ValueError("Figure expects three independent runs per task")
    for task in tasks:
        p = task["profiling"]
        runs = p["compute_wall_seconds_runs"]
        values = [*runs, p["compute_wall_seconds_median"], p["peak_rss_bytes_max"], p["shared_state_bytes_max"]]
        if len(runs) != 3 or any(not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError(f"Invalid or missing measurements: {task['task_id']}")
        if not math.isclose(statistics.median(runs), p["compute_wall_seconds_median"], rel_tol=1e-12):
            raise ValueError("Recorded median disagrees with individual runs")
    tasks = sorted(tasks, key=lambda t: t["profiling"]["compute_wall_seconds_median"])
    paths = [output.with_suffix(suffix) for suffix in (".png", ".pdf")]
    if any(path.exists() for path in paths):
        raise FileExistsError("Choose a new output stem to preserve existing figures")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

    colors = dict(zip(
        ("human_15", "csv_excel_34", "csv_excel_29", "human_142", "human_12", "human_7"),
        ("#0072B2", "#009E73", "#CC79A7", "#E69F00", "#D55E00", "#56B4E9"),
    ))
    style = {
        "font.family": "serif", "font.serif": ["STIXGeneral"], "mathtext.fontset": "stix",
        "font.size": 10, "axes.labelsize": 10, "axes.titlesize": 11,
        "xtick.labelsize": 9, "ytick.labelsize": 10, "axes.linewidth": .7,
        "pdf.fonttype": 42, "ps.fonttype": 42, "savefig.facecolor": "white",
    }
    columns = (
        ("(a) Compute time", "Compute time (s, log scale)", "compute_wall_seconds_median", 1),
        ("(b) Peak process RSS", "Peak process RSS (MiB)", "peak_rss_bytes_max", 2**20),
        ("(c) Shared state size", "Serialized shared state (MiB, log scale)", "shared_state_bytes_max", 2**20),
    )
    with plt.rc_context(style):
        fig, axes = plt.subplots(1, 3, figsize=(12, 4), sharey=True)
        fig.subplots_adjust(left=.125, right=.965, top=.78, bottom=.18, wspace=.30)
        for column, (ax, (title, label, key, divisor)) in enumerate(zip(axes, columns)):
            if column != 1:
                ax.set_xscale("log")
            for row, task in enumerate(tasks):
                p = task["profiling"]
                value = p[key] / divisor
                color = colors[task["task_id"]]
                if column == 0:
                    for offset, run in zip((-.10, 0, .10), p["compute_wall_seconds_runs"]):
                        ax.scatter(run, row + offset, s=15, facecolors="white", edgecolors=color, linewidths=.8, zorder=3)
                ax.scatter(value, row, s=57, c=color, edgecolors="white", linewidths=.7, zorder=4)
                text = f"{value:.3f}" if column == 0 else (f"{value:.1f}" if column == 1 else f"{value:.3g}")
                ax.annotate(text, (value, row), xytext=(8, 0), textcoords="offset points",
                            ha="left", va="center", fontsize=9, color="#333333")
            ax.set_title(title, loc="left", pad=24)
            ax.text(0, 1.035, "Median and three runs" if column == 0 else "Maximum over three runs",
                    transform=ax.transAxes, fontsize=9, color="#666666", va="bottom")
            ax.set_xlabel(label, labelpad=8)
            ax.set_yticks(range(6), [task["task_id"] for task in tasks])
            ax.set_ylim(5.6, -.65)
            ax.set_axisbelow(True)
            ax.grid(axis="x", color="#dddddd", linewidth=.55, linestyle="--")
            ax.grid(axis="y", color="#eeeeee", linewidth=.55)
            ax.spines[["top", "right", "left"]].set_visible(False)
            ax.spines["bottom"].set_color("#555555")
            ax.tick_params(axis="y", length=0, pad=8)
            ax.tick_params(axis="x", direction="out", length=3)
            ax.xaxis.set_minor_locator(NullLocator())
        axes[0].set_xlim(.25, 180)
        axes[0].xaxis.set_major_locator(FixedLocator([.5, 1, 5, 10, 50, 100]))
        axes[1].set_xlim(0, 1350)
        axes[1].xaxis.set_major_locator(FixedLocator([0, 400, 800, 1200]))
        axes[2].set_xlim(.00045, 2000)
        axes[2].xaxis.set_major_locator(FixedLocator([.001, .01, .1, 1, 10, 100, 1000]))
        for ax in (axes[0], axes[2]):
            ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:g}"))
        fig.suptitle("Resource and State Characteristics of Six DataSciBench Tasks", fontsize=15, y=.97)
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(paths[0], dpi=360)
        fig.savefig(paths[1], metadata={
            "Title": "Resource and State Characteristics of Six DataSciBench Tasks",
            "Creator": "Wabber task profile plotting",
            "Subject": f"Source manifest SHA256: {hashlib.sha256(source).hexdigest()}",
            "CreationDate": None,
        })
        plt.close(fig)
    return paths


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "benchmarks/DataSciBench/tasks/manifest.json")
    parser.add_argument("--output", type=Path, default=ROOT / "experiments/exp1/figure/task_profiles/datascibench_task_profiles")
    args = parser.parse_args()
    print("\n".join(str(path) for path in plot(args.manifest, args.output)))
