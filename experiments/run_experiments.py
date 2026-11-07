"""
F8 · Corre los experimentos de forma automática y sin supervisión.

Por cada repetición, escenario y estrategia (intercalados para que ambas estrategias
vivan las mismas condiciones de la PC):
    1. reset del sistema y espera hasta que todo esté sano con 1 réplica
    2. arranca el healer en ese modo (rules o rl)
    3. inyecta el fallo con el chaos-injector
    4. espera a que el healer cierre el incidente (máx. ~5,5 min)
    5. detiene el healer y vuelve al paso 1

Uso (desde la raíz del repo, con docker compose corriendo):
    python experiments/run_experiments.py --reps 5
    python experiments/run_experiments.py --reps 10 --spike-users 150
    python experiments/run_experiments.py --reps 5 --scenarios 2b 3a      # solo algunos
    python experiments/run_experiments.py --reps 5 --strategies rules rl none

Si se interrumpe (Ctrl + C, apagón...), al volver a ejecutarlo continúa donde quedó.
Resultado: data/experiments/runs.csv (+ los logs del healer en data/experiments/logs/).
"""
import argparse
import csv
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "healer"))

from healer import is_healthy  # noqa: E402
from prom import SERVICES, observe  # noqa: E402

DATA = ROOT / "data"
OUT = DATA / "experiments"
LOGS = OUT / "logs"
RUNS = OUT / "runs.csv"
CHAOS_LOG = DATA / "chaos_log.csv"
INCIDENTS = DATA / "incidents.csv"

SCENARIOS = {
    "1": ["crash"],
    "2a": ["traffic-spike"],
    "2b": ["cpu-stress"],
    "3a": ["latency", "--short"],
    "3b": ["latency", "--long"],
}
RUN_FIELDS = ["run_id", "rep", "scenario", "strategy", "event_id", "injected_at",
              "finished_at", "status", "started_iso"]

INCIDENT_WAIT_S = 330      # el healer cierra como not_recovered a los 300 s
HEALER_WARMUP_S = 15       # tiempo para que el healer arranque y haga sus primeros ciclos
SETTLE_S = 20              # calma extra después de que todo está sano


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# Windows: obliga a los procesos hijos de Python a escribir en UTF-8 (si no, usan
# la codificación de Windows y los acentos rompen la lectura de su salida).
CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def py(*args, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", env=CHILD_ENV, **kw)


def reset_and_wait(max_wait: int = 240) -> bool:
    py("chaos/chaos.py", "reset")
    t0, streak = time.time(), 0
    while time.time() - t0 < max_wait:
        time.sleep(5)
        try:
            state = observe()
        except Exception:  # noqa: BLE001
            streak = 0
            continue
        ok = all(is_healthy(state[s]) and round(state[s]["replicas"]) == 1 for s in SERVICES)
        streak = streak + 1 if ok else 0
        if streak >= 3:
            time.sleep(SETTLE_S)
            return True
    return False


def last_chaos_event() -> tuple[str, float]:
    with CHAOS_LOG.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-1]["event_id"], float(rows[-1]["injected_at"])


def wait_incident(event_id: str, timeout: int) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(5)
        if INCIDENTS.exists():
            with INCIDENTS.open(encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if r["event_id"] == event_id:
                        return r["status"]
    return "timeout"


def done_runs() -> set[tuple[str, str, str]]:
    if not RUNS.exists():
        return set()
    with RUNS.open(encoding="utf-8") as f:
        return {(r["rep"], r["scenario"], r["strategy"]) for r in csv.DictReader(f)
                if r["status"] not in ("timeout", "error", "")}


def save_run(row: dict):
    OUT.mkdir(parents=True, exist_ok=True)
    new = not RUNS.exists()
    with RUNS.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RUN_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def main():
    ap = argparse.ArgumentParser(description="Experimentos baseline vs IA")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--scenarios", nargs="+", default=list(SCENARIOS), choices=list(SCENARIOS))
    ap.add_argument("--strategies", nargs="+", default=["rules", "rl"], choices=["rules", "rl", "none"])
    ap.add_argument("--spike-users", type=int, default=150)
    args = ap.parse_args()

    LOGS.mkdir(parents=True, exist_ok=True)
    total = args.reps * len(args.scenarios) * len(args.strategies)
    skip = done_runs()
    log(f"{total} corridas planificadas ({len(skip)} ya hechas). Ctrl + C para pausar.")
    log("Asegurando tráfico normal de fondo (Locust) ...")
    subprocess.run(["docker", "compose", "--profile", "load", "up", "-d"], cwd=ROOT,
                   capture_output=True)

    n = 0
    for rep in range(1, args.reps + 1):
        for sc in args.scenarios:
            for strategy in args.strategies:
                n += 1
                key = (str(rep), sc, strategy)
                if key in skip:
                    continue
                run_id = f"r{rep}-{sc}-{strategy}"
                log(f"--- {n}/{total} · rep {rep} · escenario {sc} · {strategy}")

                if not reset_and_wait():
                    log("  ! el sistema no quedó sano tras el reset; se reintenta una vez")
                    if not reset_and_wait():
                        save_run({"run_id": run_id, "rep": rep, "scenario": sc, "strategy": strategy,
                                  "status": "error", "started_iso": datetime.now().isoformat()})
                        continue

                logf = (LOGS / f"{run_id}.log").open("w", encoding="utf-8")
                healer = subprocess.Popen(
                    [sys.executable, "-u", "healer/healer.py", "--mode", strategy],
                    cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT, env=CHILD_ENV,
                )
                try:
                    time.sleep(HEALER_WARMUP_S)
                    if healer.poll() is not None:
                        log(f"  ! el healer terminó solo; revisa {logf.name}")
                        save_run({"run_id": run_id, "rep": rep, "scenario": sc, "strategy": strategy,
                                  "status": "error", "started_iso": datetime.now().isoformat()})
                        continue
                    cmd = SCENARIOS[sc] + (["--users", str(args.spike_users)] if sc == "2a" else [])
                    r = py("chaos/chaos.py", *cmd)
                    if r.returncode != 0:
                        log(f"  ! error al inyectar: {r.stderr.strip()[:200]}")
                        continue
                    event_id, injected_at = last_chaos_event()
                    log(f"  fallo inyectado (event_id={event_id}); esperando al healer ...")
                    status = wait_incident(event_id, INCIDENT_WAIT_S)
                    log(f"  -> {status}")
                    save_run({"run_id": run_id, "rep": rep, "scenario": sc, "strategy": strategy,
                              "event_id": event_id, "injected_at": f"{injected_at:.3f}",
                              "finished_at": f"{time.time():.3f}", "status": status,
                              "started_iso": datetime.fromtimestamp(injected_at).isoformat()})
                finally:
                    healer.terminate()
                    try:
                        healer.wait(10)
                    except subprocess.TimeoutExpired:
                        healer.kill()
                    logf.close()

    log("Reset final ...")
    reset_and_wait()
    log("Experimentos terminados. Ahora: python experiments/analyze.py")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nPausado. Vuelve a ejecutar el mismo comando para continuar donde quedó.")
        py("chaos/chaos.py", "reset")
