"""
Entrena la IA del healer (F7):
  1. Detector de anomalías con los datos normales de data/healer_trace.csv (modo none).
  2. Agente Q-learning en el simulador.
  3. Muestra la política aprendida y compara reglas vs RL dentro del simulador.

Uso (desde la raíz del repo):
    python healer/train.py
    python healer/train.py --episodes 50000
    python healer/train.py --skip-detector        # solo reentrena el agente
"""
import argparse
import csv
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import greedy, save_q, train_q  # noqa: E402
from simulator import ACTION_NAMES, SCENARIOS, ServiceSim, rules_policy  # noqa: E402
from state import describe  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "data" / "healer_trace.csv"
CURVE = ROOT / "data" / "rl_training.csv"
SIM_EVAL = ROOT / "data" / "sim_eval.csv"

# Estados típicos de cada escenario: la política aprendida debería parecerse a la diapositiva 8.
KEY_STATES = [
    ("1  caída", (0, 0, 0, 0, 1, 0), "restart"),
    ("2a pico de tráfico", (1, 2, 1, 0, 1, 0), "scale"),
    ("2b CPU trabada", (1, 2, 0, 0, 1, 0), "restart"),
    ("3a latencia (recién)", (1, 0, 0, 1, 1, 0), "do_nothing"),
    ("3b latencia (persistente)", (1, 0, 0, 1, 1, 2), "restart"),
    ("   todo normal", (1, 0, 0, 0, 1, 0), "do_nothing"),
]


def evaluate(policy, runs: int = 300, seed: int = 7) -> dict[str, dict]:
    out = {}
    for sc in SCENARIOS[1:]:
        rng = random.Random(seed)
        rewards, steps, recovered, actions, down = [], [], 0, [], []
        for _ in range(runs):
            sim = ServiceSim(sc, rng)
            total, n_act = 0.0, 0
            while not sim.done:
                a = policy(sim.state)
                _, r, _ = sim.step(a)
                total += r
                n_act += a != 0

            rewards.append(total)
            steps.append((sim.t - sim.fault_at) * 5)
            actions.append(n_act)
            down.append(sim.down_ticks * 5)
            recovered += sim.t < 36
        out[sc] = {
            "reward": statistics.mean(rewards),
            "recovery_s": statistics.mean(steps),
            "downtime_s": statistics.mean(down),
            "actions": statistics.mean(actions),
            "recovered_pct": 100 * recovered / runs,
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, default=60000)
    ap.add_argument("--skip-detector", action="store_true")
    ap.add_argument("--mode", default="none", help="modo de las filas normales en healer_trace.csv")
    args = ap.parse_args()

    if not args.skip_detector:
        from detector import train as train_detector
        if not TRACE.exists():
            sys.exit("No existe data/healer_trace.csv. Graba tráfico normal: python healer/healer.py --mode none")
        print("1) Entrenando detector de anomalías con tráfico normal ...")
        model = train_detector(TRACE, args.mode)
        for svc, m in model["services"].items():
            b = m["baseline"]
            print(f"   {svc:<10} {m['n']:>4} muestras · tráfico normal {b['req_mean']:.1f} req/s · "
                  f"/health p95 {b['probe_p95']:.1f} ms")

    print(f"\n2) Entrenando agente Q-learning en el simulador ({args.episodes} episodios) ...")
    Q, history = train_q(args.episodes)
    save_q(Q)
    CURVE.parent.mkdir(parents=True, exist_ok=True)
    with CURVE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(history[0]))
        w.writeheader()
        w.writerows(history)
    last = history[-2000:]
    print(f"   {len(Q)} estados aprendidos · recompensa media últimos 2000 episodios: "
          f"{statistics.mean(h['reward'] for h in last):.1f}")
    print("   Curva de aprendizaje: data/rl_training.csv")

    print("\n3) Política aprendida en estados típicos:")
    ok = 0
    for name, s, expected in KEY_STATES:
        a = greedy(Q, s)
        got = ACTION_NAMES[a] if a is not None else "(no visto)"
        mark = "✓" if got == expected else "✗"
        ok += got == expected
        print(f"   {mark} {name:<26} -> {got:<11} (esperado {expected:<10}) [{describe(s)}]")
    print(f"   {ok}/{len(KEY_STATES)} coinciden con la acción correcta")

    print("\n4) Comparación en el simulador (300 episodios por escenario):")
    rl = evaluate(lambda s: greedy(Q, s) or 0)
    rules = evaluate(rules_policy)
    print(f"   {'escenario':<14} {'estrategia':<7} {'recompensa':>10} {'recovery s':>10} "
          f"{'downtime s':>10} {'acciones':>8} {'recuperado':>10}")
    rows = []
    for sc in SCENARIOS[1:]:
        for name, res in (("reglas", rules[sc]), ("RL", rl[sc])):
            r = res
            print(f"   {sc:<14} {name:<7} {r['reward']:>10.1f} {r['recovery_s']:>10.1f} "
                  f"{r['downtime_s']:>10.1f} {r['actions']:>8.2f} {r['recovered_pct']:>9.0f}%")
            rows.append({"scenario": sc, "strategy": name, **{k: round(v, 2) for k, v in r.items()}})
    with SIM_EVAL.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("   Guardado en data/sim_eval.csv")
    print("\nListo. Ahora prueba el agente real: python healer/healer.py --mode rl")


if __name__ == "__main__":
    main()
