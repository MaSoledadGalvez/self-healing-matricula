"""
F8 · Analiza los experimentos: tablas (promedio ± desviación estándar) y gráficas.

Uso (desde la raíz del repo, con Prometheus corriendo, porque de ahí se calcula
la disponibilidad):
    python experiments/analyze.py
    python experiments/analyze.py --window 180

Lee:   data/experiments/runs.csv · data/incidents.csv · data/rl_training.csv · Prometheus
Crea:  data/results/runs_detailed.csv   una fila por corrida con todas las métricas
       data/results/summary.csv         promedio y desviación por escenario y estrategia
       data/results/summary.md          la misma tabla lista para pegar en el informe
       data/results/*.png               gráficas para el informe
"""
import argparse
import csv
import json
import math
import statistics
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "healer"))
from prom import PROM_URL, QUERIES  # noqa: E402

DATA = ROOT / "data"
RUNS = DATA / "experiments" / "runs.csv"
INCIDENTS = DATA / "incidents.csv"
CURVE = DATA / "rl_training.csv"
OUT = DATA / "results"

SCENARIO_ORDER = ["1", "2a", "2b", "3a", "3b"]
SCENARIO_NAMES = {
    "1": "1 · Caída", "2a": "2a · Pico de tráfico", "2b": "2b · CPU trabada",
    "3a": "3a · Latencia corta", "3b": "3b · Latencia larga",
}
TARGET = {"1": "courses", "2a": "courses", "2b": "courses", "3a": "billing", "3b": "billing"}
EXPECTED = {"1": "restart", "2a": "scale", "2b": "restart", "3a": "do_nothing", "3b": "restart"}
STRATEGY_NAMES = {"rules": "Reglas (baseline)", "rl": "IA (detector + Q-learning)", "none": "Sin healer"}
NOT_RECOVERED_S = 300.0   # tope del incidente: si no se recupera, se cuenta como 300 s

# Paleta categórica validada (slots 1-3) y tinta de texto, del skill de visualización.
COLORS = {"rules": "#eb6834", "rl": "#2a78d6", "none": "#1baf7a"}
INK, INK_2, GRID = "#0b0b0b", "#52514e", "#e4e3df"


# ------------------------------------------------------------------ Prometheus
def prom(path: str, **params):
    url = f"{PROM_URL}/api/v1/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)["data"]["result"]


def service_series(expr: str, service: str, start: float, end: float, step: int = 5):
    for s in prom("query_range", query=expr, start=start, end=end, step=step):
        if s["metric"].get("service") == service:
            return [(float(t) - start, float(v)) for t, v in s["values"]]
    return []


def availability(service: str, t0: float, window: int):
    """Downtime y disponibilidad del servicio afectado según blackbox (/health)."""
    pts = service_series('max by (service) (probe_success{job="blackbox"})', service, t0, t0 + window)
    if not pts:
        return None, None
    down = sum(1 for _, v in pts if v < 1) * 5
    return float(down), 100.0 * (1 - down / (len(pts) * 5))


def request_success(t0: float, window: int):
    """% de peticiones de usuarios que el gateway respondió sin error 5xx."""
    t = t0 + window
    q_err = f'sum(increase(http_requests_total{{service="gateway", status=~"5.."}}[{window}s]))'
    q_all = f'sum(increase(http_requests_total{{service="gateway"}}[{window}s]))'
    err = prom("query", query=q_err, time=t)
    tot = prom("query", query=q_all, time=t)
    e = float(err[0]["value"][1]) if err else 0.0
    a = float(tot[0]["value"][1]) if tot else 0.0
    return 100.0 * (1 - e / a) if a > 0 else None


# ------------------------------------------------------------------ métricas por corrida
def parse_actions(text: str) -> list[tuple[str, str]]:
    out = []
    for part in filter(None, (p.strip() for p in text.split("|"))):
        bits = part.split()
        if len(bits) >= 3:
            out.append((bits[1], bits[2]))   # (acción, servicio)
    return out


def num(v):
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def build_rows(window: int) -> list[dict]:
    if not RUNS.exists():
        sys.exit("No existe data/experiments/runs.csv. Corre antes: python experiments/run_experiments.py")
    with INCIDENTS.open(encoding="utf-8") as f:
        incidents = {r["event_id"]: r for r in csv.DictReader(f)}
    with RUNS.open(encoding="utf-8") as f:
        runs = [r for r in csv.DictReader(f) if r["event_id"] and r["event_id"] in incidents]

    rows = []
    for r in runs:
        inc = incidents[r["event_id"]]
        sc, target, expected = r["scenario"], TARGET[r["scenario"]], EXPECTED[r["scenario"]]
        t0 = float(r["injected_at"])
        acts = parse_actions(inc["actions"])
        on_target = [a for a, s in acts if s == target]
        first = on_target[0] if on_target else "do_nothing"
        necessary = sum(1 for a, s in acts if s == target and a == expected)
        recovered = inc["status"] in ("recovered", "no_impact")
        rec = num(inc["recovery_time_s"])
        if inc["status"] == "no_impact":
            rec = 0.0
        if not recovered:
            rec = NOT_RECOVERED_S
        down, avail = availability(target, t0, window)
        rows.append({
            "run_id": r["run_id"], "rep": r["rep"], "scenario": sc, "strategy": r["strategy"],
            "status": inc["status"], "recovered": int(recovered),
            "first_action": first, "correct_action": int(first == expected),
            "detection_time_s": num(inc["detection_time_s"]),
            "recovery_time_s": rec,
            "downtime_s": down,
            "availability_pct": avail,
            "request_success_pct": request_success(t0, window),
            "n_actions": len(acts),
            "unnecessary_actions": len(acts) - necessary,
            "injected_at": t0,
        })
    return rows


def mean_sd(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None
    return statistics.mean(vals), (statistics.stdev(vals) if len(vals) > 1 else 0.0)


METRICS = ["recovery_time_s", "detection_time_s", "downtime_s", "availability_pct",
           "request_success_pct", "n_actions", "unnecessary_actions"]


def summarize(rows):
    out = []
    strategies = [s for s in ("rules", "rl", "none") if any(r["strategy"] == s for r in rows)]
    for sc in SCENARIO_ORDER:
        for st in strategies:
            g = [r for r in rows if r["scenario"] == sc and r["strategy"] == st]
            if not g:
                continue
            row = {"scenario": sc, "strategy": st, "n": len(g),
                   "recovered_pct": 100 * sum(r["recovered"] for r in g) / len(g),
                   "correct_action_pct": 100 * sum(r["correct_action"] for r in g) / len(g)}
            for m in METRICS:
                mu, sd = mean_sd([r[m] for r in g])
                row[f"{m}_mean"], row[f"{m}_sd"] = mu, sd
            out.append(row)
    return out, strategies


def fmt(mu, sd, nd=1):
    if mu is None:
        return "–"
    return f"{mu:.{nd}f} ± {sd:.{nd}f}"


def write_tables(rows, summary):
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "runs_detailed.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with (OUT / "summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0]))
        w.writeheader()
        for s in summary:
            w.writerow({k: (round(v, 3) if isinstance(v, float) else v) for k, v in s.items()})

    lines = [
        "| Escenario | Estrategia | n | Acción correcta | Recuperado | Recovery time (s) | "
        "Downtime (s) | Disponibilidad (%) | Peticiones OK (%) | Acciones innecesarias |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summary:
        lines.append(
            f"| {SCENARIO_NAMES[s['scenario']]} | {STRATEGY_NAMES[s['strategy']]} | {s['n']} | "
            f"{s['correct_action_pct']:.0f} % | {s['recovered_pct']:.0f} % | "
            f"{fmt(s['recovery_time_s_mean'], s['recovery_time_s_sd'])} | "
            f"{fmt(s['downtime_s_mean'], s['downtime_s_sd'])} | "
            f"{fmt(s['availability_pct_mean'], s['availability_pct_sd'], 2)} | "
            f"{fmt(s['request_success_pct_mean'], s['request_success_pct_sd'], 2)} | "
            f"{fmt(s['unnecessary_actions_mean'], s['unnecessary_actions_sd'])} |"
        )
    lines.append("")
    lines.append(f"Recovery time de corridas no recuperadas = {NOT_RECOVERED_S:.0f} s (tope del experimento).")
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


# ------------------------------------------------------------------ gráficas
def style(ax, title, ylabel):
    ax.set_title(title, loc="left", fontsize=12, color=INK, pad=12)
    ax.set_ylabel(ylabel, color=INK_2)
    ax.tick_params(colors=INK_2, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)


def grouped_bars(summary, strategies, metric, title, ylabel, fname, ylim=None, note=None):
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=150)
    scen = [s for s in SCENARIO_ORDER if any(r["scenario"] == s for r in summary)]
    width = 0.8 / len(strategies)
    for i, st in enumerate(strategies):
        xs, ys, es = [], [], []
        for j, sc in enumerate(scen):
            row = next((r for r in summary if r["scenario"] == sc and r["strategy"] == st), None)
            if row is None or row[f"{metric}_mean"] is None:
                continue
            xs.append(j - 0.4 + width * (i + 0.5))
            ys.append(row[f"{metric}_mean"])
            es.append(row[f"{metric}_sd"])
        ax.bar(xs, ys, width=width - 0.03, color=COLORS[st], label=STRATEGY_NAMES[st],
               yerr=es, capsize=3, error_kw={"ecolor": INK_2, "elinewidth": 1})
        for x, y in zip(xs, ys):
            ax.annotate(f"{y:.1f}", (x, y), ha="center", va="bottom", fontsize=8,
                        color=INK_2, xytext=(0, 3), textcoords="offset points")
    ax.set_xticks(range(len(scen)))
    ax.set_xticklabels([SCENARIO_NAMES[s] for s in scen], color=INK)
    if ylim:
        ax.set_ylim(*ylim)
    style(ax, title, ylabel)
    ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(0, -0.12), ncol=len(strategies))
    if note:
        fig.text(0.01, 0.005, note, fontsize=8, color=INK_2)
    fig.tight_layout()
    fig.savefig(OUT / fname)
    plt.close(fig)


def timeline(rows, scenario, expr, service, title, ylabel, fname, transform=lambda v: v):
    fig, ax = plt.subplots(figsize=(9, 3.8), dpi=150)
    drawn = 0
    for st in ("rules", "rl"):
        run = next((r for r in rows if r["scenario"] == scenario and r["strategy"] == st), None)
        if run is None:
            continue
        t0 = run["injected_at"]
        pts = service_series(expr, service, t0 - 30, t0 + 240)
        if not pts:
            continue
        xs = [t - 30 for t, _ in pts]
        ax.plot(xs, [transform(v) for _, v in pts], color=COLORS[st], linewidth=2,
                label=STRATEGY_NAMES[st])
        drawn += 1
    if not drawn:
        plt.close(fig)
        return
    ax.axvline(0, color=INK_2, linewidth=1, linestyle="--")
    ax.annotate("fallo inyectado", (0, 0), xycoords=("data", "axes fraction"), xytext=(4, 4),
                textcoords="offset points", fontsize=8, color=INK_2)
    ax.set_xlabel("segundos desde la inyección del fallo", color=INK_2)
    style(ax, title, ylabel)
    ax.legend(frameon=False, loc="upper right")
    fig.tight_layout()
    fig.savefig(OUT / fname)
    plt.close(fig)


def learning_curve():
    if not CURVE.exists():
        return
    with CURVE.open(encoding="utf-8") as f:
        rewards = [float(r["reward"]) for r in csv.DictReader(f)]
    k = 1000
    ma = [statistics.mean(rewards[max(0, i - k):i + 1]) for i in range(0, len(rewards), 100)]
    fig, ax = plt.subplots(figsize=(9, 3.6), dpi=150)
    ax.plot([i * 100 for i in range(len(ma))], ma, color=COLORS["rl"], linewidth=2)
    ax.set_xlabel("episodio de entrenamiento (simulador)", color=INK_2)
    style(ax, "Curva de aprendizaje del agente Q-learning", f"recompensa media (últimos {k} episodios)")
    fig.tight_layout()
    fig.savefig(OUT / "rl_learning_curve.png")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=180,
                    help="segundos después de cada fallo usados para downtime y disponibilidad")
    args = ap.parse_args()

    rows = build_rows(args.window)
    if not rows:
        sys.exit("No hay corridas completas para analizar.")
    summary, strategies = summarize(rows)
    write_tables(rows, summary)

    note = f"Promedio ± desviación estándar. Ventana de {args.window} s tras cada fallo."
    grouped_bars(summary, strategies, "recovery_time_s", "Recovery time por escenario",
                 "segundos (menos es mejor)", "recovery_time.png",
                 note=note + f" No recuperado = {NOT_RECOVERED_S:.0f} s.")
    grouped_bars(summary, strategies, "downtime_s", "Downtime del servicio afectado",
                 "segundos sin responder /health", "downtime.png", note=note)
    grouped_bars(summary, strategies, "availability_pct", "Disponibilidad del servicio afectado",
                 "% del tiempo arriba", "availability.png", ylim=(80, 100.5), note=note)
    grouped_bars(summary, strategies, "request_success_pct", "Peticiones de usuarios respondidas sin error",
                 "% de peticiones sin 5xx en el gateway", "request_success.png", ylim=(80, 100.5), note=note)
    grouped_bars(summary, strategies, "unnecessary_actions", "Acciones innecesarias por incidente",
                 "acciones (menos es mejor)", "unnecessary_actions.png", note=note)

    timeline(rows, "2b", QUERIES["cpu_max_pct"], "courses",
             "Escenario 2b · CPU de courses (réplica más cargada)", "% del límite de CPU",
             "timeline_2b_cpu.png")
    timeline(rows, "3a", QUERIES["health"], "billing",
             "Escenario 3a · ¿billing responde /health? (1 = sí)", "/health", "timeline_3a_health.png")
    timeline(rows, "1", QUERIES["health"], "courses",
             "Escenario 1 · ¿courses responde /health? (1 = sí)", "/health", "timeline_1_health.png")
    learning_curve()
    print(f"\nTablas y gráficas en {OUT}")


if __name__ == "__main__":
    main()
