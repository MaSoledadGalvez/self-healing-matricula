"""
Healer: el bucle self-healing  Fallo -> Detección -> Decisión -> Acción -> Recuperación.

Uso (desde la raíz del repo, con docker compose corriendo):
    python healer/healer.py --mode rules     # baseline con reglas fijas (F6)
    python healer/healer.py --mode none      # sin auto-recuperación (referencia)
    python healer/healer.py --mode rl        # agente Q-learning (F7)

Cada 5 s: lee Prometheus, decide una acción por servicio, la ejecuta y registra:
    data/healer_trace.csv   una fila por servicio y ciclo (estado + decisión) -> dataset
    data/incidents.csv      una fila por fallo inyectado: detección, acciones y recovery time

Detener con Ctrl + C.
"""
import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import actions  # noqa: E402
from prom import SERVICES, latency_signal, observe  # noqa: E402
from rules import NoHealingStrategy, RulesStrategy  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CHAOS_LOG = DATA / "chaos_log.csv"
TRACE = DATA / "healer_trace.csv"
INCIDENTS = DATA / "incidents.csv"

COOLDOWN_S = 20            # tras actuar sobre un servicio, esperar antes de volver a actuar
PROBE_OK_MS = 300          # /health debe responder en menos de esto para estar "sano"
CPU_OK_PCT = 85            # ninguna réplica por encima de esto
LAT_OK_MS = 500            # latencia que perciben quienes lo llaman
RECOVERY_STREAK = 3        # ciclos sanos seguidos para dar por recuperado (3 x 5 s)
NO_IMPACT_S = 60           # si el fallo nunca se notó en este tiempo, se cierra sin impacto
INCIDENT_TIMEOUT_S = 300   # si no se recupera en 5 min, se cierra como no recuperado

TRACE_FIELDS = [
    "timestamp", "datetime_utc", "mode", "service",
    "health", "probe_ms", "cpu_avg_pct", "cpu_max_pct", "req_rate", "err_rate",
    "p95_ms", "client_p95_ms", "replicas",
    "healthy", "anomalous", "action", "reason", "result", "in_cooldown",
]
INCIDENT_FIELDS = [
    "event_id", "mode", "scenario", "fault", "service", "expected_action",
    "injected_at", "detected_at", "first_action", "first_action_at", "recovered_at",
    "detection_time_s", "recovery_time_s", "downtime_s",
    "n_actions", "actions", "status",
]


def iso(ts: float | None) -> str:
    """Hora local para mostrar en consola."""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else ""


def append_csv(path: Path, fields: list[str], row: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


def is_healthy(obs: dict) -> bool:
    """Criterio objetivo de 'sano', igual para todas las estrategias."""
    return (
        obs["health"] >= 1
        and (obs["probe_ms"] or 0) < PROBE_OK_MS
        and obs["cpu_max_pct"] < CPU_OK_PCT
        and latency_signal(obs) < LAT_OK_MS
    )


def fmt(v, nd=1):
    return "" if v is None else round(v, nd)


# ------------------------------------------------------------------ incidentes
class IncidentTracker:
    """Une cada fallo del chaos-injector con lo que hizo el healer después."""

    def __init__(self, mode: str, started_at: float):
        self.mode = mode
        self.started_at = started_at
        self.seen: set[str] = set()
        self.open: dict[str, dict] = {}   # event_id -> incidente abierto

    def poll_chaos_log(self):
        if not CHAOS_LOG.exists():
            return
        with CHAOS_LOG.open(encoding="utf-8") as f:
            for ev in csv.DictReader(f):
                eid = ev["event_id"]
                if eid in self.seen:
                    continue
                self.seen.add(eid)
                if float(ev["injected_at"]) < self.started_at:
                    continue  # fallos de antes de arrancar el healer: se ignoran
                self.open[eid] = {
                    **{k: ev[k] for k in ("event_id", "scenario", "fault", "service", "expected_action")},
                    "injected_at": float(ev["injected_at"]),
                    "detected_at": None, "first_action": "", "first_action_at": None,
                    "seen_unhealthy": False, "healthy_streak": 0, "streak_start": None,
                    "down_cycles": 0, "actions": [],
                }
                print(f"\n>>> Fallo detectado en chaos_log: escenario {ev['scenario']} "
                      f"({ev['fault']}) en {ev['service']}")

    def update(self, now: float, interval: float, state: dict, decisions: dict, done: dict):
        for eid, inc in list(self.open.items()):
            svc = inc["service"]
            obs, dec = state[svc], decisions[svc]
            if dec.anomalous and inc["detected_at"] is None:
                inc["detected_at"] = now
            if obs["health"] < 1:
                inc["down_cycles"] += 1
            # Todas las acciones durante el incidente (también sobre otros servicios).
            for s, result in done.items():
                inc["actions"].append(f"+{now - inc['injected_at']:.0f}s {decisions[s].action} {s}")
                if s == svc and not inc["first_action"]:
                    inc["first_action"], inc["first_action_at"] = decisions[s].action, now

            if not is_healthy(obs):
                inc["seen_unhealthy"] = True
                inc["healthy_streak"], inc["streak_start"] = 0, None
            elif inc["seen_unhealthy"]:
                if inc["healthy_streak"] == 0:
                    inc["streak_start"] = now
                inc["healthy_streak"] += 1

            age = now - inc["injected_at"]
            if inc["seen_unhealthy"] and inc["healthy_streak"] >= RECOVERY_STREAK:
                self.close(eid, "recovered", inc["streak_start"], interval)
            elif not inc["seen_unhealthy"] and age > NO_IMPACT_S:
                self.close(eid, "no_impact", None, interval)
            elif age > INCIDENT_TIMEOUT_S:
                self.close(eid, "not_recovered", None, interval)

    def close(self, eid: str, status: str, recovered_at: float | None, interval: float):
        inc = self.open.pop(eid)
        t0 = inc["injected_at"]
        row = {
            "event_id": eid, "mode": self.mode,
            **{k: inc[k] for k in ("scenario", "fault", "service", "expected_action", "first_action")},
            "injected_at": f"{t0:.3f}",
            "detected_at": f"{inc['detected_at']:.3f}" if inc["detected_at"] else "",
            "first_action_at": f"{inc['first_action_at']:.3f}" if inc["first_action_at"] else "",
            "recovered_at": f"{recovered_at:.3f}" if recovered_at else "",
            "detection_time_s": f"{inc['detected_at'] - t0:.1f}" if inc["detected_at"] else "",
            "recovery_time_s": f"{recovered_at - t0:.1f}" if recovered_at else "",
            "downtime_s": f"{inc['down_cycles'] * interval:.0f}",
            "n_actions": len(inc["actions"]),
            "actions": " | ".join(inc["actions"]),
            "status": status,
        }
        append_csv(INCIDENTS, INCIDENT_FIELDS, row)
        rt = row["recovery_time_s"] or "-"
        print(f"<<< Incidente {inc['scenario']} en {inc['service']}: {status} | "
              f"detección {row['detection_time_s'] or '-'} s | recovery {rt} s | "
              f"downtime {row['downtime_s']} s | acciones: {row['n_actions']}\n")


# ------------------------------------------------------------------ bucle principal
def get_strategy(mode: str):
    if mode == "rules":
        return RulesStrategy()
    if mode == "none":
        return NoHealingStrategy()
    if mode == "rl":
        from agent import RLStrategy  # se agrega en F7
        return RLStrategy()
    raise ValueError(mode)


def main():
    ap = argparse.ArgumentParser(description="Healer self-healing del sistema de matrícula")
    ap.add_argument("--mode", choices=["rules", "none", "rl"], default="rules")
    ap.add_argument("--interval", type=float, default=5.0, help="segundos entre ciclos")
    args = ap.parse_args()

    strategy = get_strategy(args.mode)
    started = time.time()
    tracker = IncidentTracker(args.mode, started)
    tracker.poll_chaos_log()  # marca como vistos los fallos viejos
    last_action_at: dict[str, float] = {}
    print(f"Healer en modo '{args.mode}' cada {args.interval:.0f} s. Ctrl + C para detener.")

    cycle = 0
    while True:
        t_cycle = time.time()
        try:
            state = observe()
        except Exception as e:  # noqa: BLE001
            print(f"[{iso(t_cycle)}] No se pudo leer Prometheus: {e}")
            time.sleep(args.interval)
            continue

        tracker.poll_chaos_log()
        now = time.time()
        decisions, done = {}, {}
        for svc in SERVICES:
            obs = state[svc]
            dec = strategy.decide(svc, obs)
            in_cooldown = now - last_action_at.get(svc, 0) < COOLDOWN_S
            result = ""
            if dec.action != actions.DO_NOTHING and not in_cooldown:
                result = actions.execute(svc, dec.action)
                last_action_at[svc] = time.time()
                strategy.reset(svc)
                done[svc] = result
                print(f"[{iso(now)}] {svc:<10} {dec.action.upper():<8} ({dec.reason}) -> {result}")
            decisions[svc] = dec
            append_csv(TRACE, TRACE_FIELDS, {
                "timestamp": f"{now:.3f}",
                "datetime_utc": datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "mode": args.mode, "service": svc,
                "health": fmt(obs["health"], 0), "probe_ms": fmt(obs["probe_ms"]),
                "cpu_avg_pct": fmt(obs["cpu_avg_pct"]), "cpu_max_pct": fmt(obs["cpu_max_pct"]),
                "req_rate": fmt(obs["req_rate"], 2), "err_rate": fmt(obs["err_rate"], 2),
                "p95_ms": fmt(obs["p95_ms"]), "client_p95_ms": fmt(obs["client_p95_ms"]),
                "replicas": fmt(obs["replicas"], 0),
                "healthy": int(is_healthy(obs)), "anomalous": int(dec.anomalous),
                "action": dec.action if not in_cooldown else actions.DO_NOTHING,
                "reason": dec.reason, "result": result, "in_cooldown": int(in_cooldown),
            })

        tracker.update(now, args.interval, state, decisions, done)

        cycle += 1
        if cycle % 6 == 0:  # resumen cada ~30 s
            resumen = "  ".join(
                f"{s}:{'OK' if is_healthy(state[s]) else '!!'}"
                f"(cpu {state[s]['cpu_max_pct']:.0f}% lat {latency_signal(state[s]):.0f}ms x{state[s]['replicas']:.0f})"
                for s in SERVICES
            )
            print(f"[{iso(now)}] {resumen}")

        time.sleep(max(0.0, args.interval - (time.time() - t_cycle)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nHealer detenido.")
