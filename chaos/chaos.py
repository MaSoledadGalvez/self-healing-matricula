"""
chaos-injector (F5): provoca los 5 fallos de los experimentos a pedido y registra
la hora EXACTA de cada inyección en data/chaos_log.csv (de ahí sale el recovery time).

Uso (desde la raíz del repo, con docker compose corriendo):
    python chaos/chaos.py crash                         # 1  : courses deja de responder
    python chaos/chaos.py traffic-spike                 # 2a : pico de matrícula (Locust)
    python chaos/chaos.py cpu-stress                    # 2b : proceso trabado satura la CPU de courses
    python chaos/chaos.py latency --short               # 3a : hipo de red de 8 s en billing
    python chaos/chaos.py latency --long                # 3b : billing lento hasta que alguien actúe
    python chaos/chaos.py reset                         # deja todo como al inicio

Opciones útiles: --service courses   --duration 120   --users 150   --delay 600

Solo usa la librería estándar de Python y el comando docker (no hay que instalar nada).
"""
import argparse
import csv
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # raíz del repo
LOG = ROOT / "data" / "chaos_log.csv"
PROJECT = "self-healing-matricula"
APP_SERVICES = ["gateway", "students", "courses", "enrollment", "billing"]
SPIKE_CONTAINER = "chaos-spike"

LOG_FIELDS = [
    "event_id", "scenario", "fault", "service", "container",
    "injected_at", "injected_at_iso", "ends_at", "params", "expected_action",
]


# ---------------------------------------------------------------- utilidades
def run(cmd: list[str], check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
    """Ejecuta un comando docker desde la raíz del repo."""
    if not quiet:
        print("  $", " ".join(cmd))
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        print(r.stdout, r.stderr, sep="\n", file=sys.stderr)
        sys.exit(f"Error ejecutando: {' '.join(cmd)}")
    return r


def container_name(service: str) -> str:
    """Nombre de la primera réplica VIVA del servicio. No se asume que sea la -1:
    al escalar y desescalar, Docker Compose puede dejar viva otra (p. ej. la -2)."""
    r = run(["docker", "ps",
             "--filter", f"label=com.docker.compose.project={PROJECT}",
             "--filter", f"label=com.docker.compose.service={service}",
             "--format", "{{.Names}}"], check=False, quiet=True)
    names = sorted(r.stdout.split())
    if not names:
        sys.exit(f"El servicio {service} no tiene réplicas corriendo. Ejecuta: python chaos/chaos.py reset")
    return names[0]


def ensure_running(container: str) -> None:
    r = run(["docker", "inspect", "-f", "{{.State.Running}}", container], check=False, quiet=True)
    if r.stdout.strip() != "true":
        sys.exit(f"El contenedor {container} no está corriendo. Ejecuta: python chaos/chaos.py reset")


def log_event(scenario, fault, service, container, injected_at, ends_at, params, expected_action):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LOG.exists()
    event_id = uuid.uuid4().hex[:8]
    with LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow({
            "event_id": event_id,
            "scenario": scenario,
            "fault": fault,
            "service": service,
            "container": container,
            "injected_at": f"{injected_at:.3f}",
            "injected_at_iso": datetime.fromtimestamp(injected_at, timezone.utc).isoformat(timespec="milliseconds"),
            "ends_at": f"{ends_at:.3f}" if ends_at else "",
            "params": params,
            "expected_action": expected_action,
        })
    print(f"  -> registrado en data/chaos_log.csv (event_id={event_id})")
    return event_id


# ---------------------------------------------------------------- escenarios
def crash(args):
    """Escenario 1: el servicio muere de golpe (SIGKILL). Acción correcta: Restart."""
    ctr = container_name(args.service)
    ensure_running(ctr)
    print(f"[1] Matando {ctr} ...")
    t0 = time.time()
    run(["docker", "kill", ctr])
    log_event("1", "crash", args.service, ctr, t0, None, "signal=SIGKILL", "restart")


def traffic_spike(args):
    """Escenario 2a: apertura de matrícula, muchos usuarios a la vez. Acción correcta: Scale."""
    run(["docker", "rm", "-f", SPIKE_CONTAINER], check=False, quiet=True)
    print(f"[2a] Lanzando {args.users} estudiantes durante {args.duration} s contra el gateway ...")
    t0 = time.time()
    run([
        "docker", "compose", "run", "-d", "--rm", "--no-deps", "--name", SPIKE_CONTAINER,
        "locust",
        "-f", "/mnt/locust/locustfile.py", "--headless",
        "-u", str(args.users), "-r", str(max(1, args.users // 3)),
        "-t", f"{args.duration}s",
        "--host", "http://gateway:3000", "--only-summary",
        "EstudiantePico",
    ])
    log_event("2a", "traffic_spike", "courses", SPIKE_CONTAINER, t0, t0 + args.duration,
              f"users={args.users};duration={args.duration}", "scale")


def cpu_stress(args):
    """Escenario 2b: un proceso trabado consume la CPU sin más tráfico. Acción correcta: Restart."""
    ctr = container_name(args.service)
    ensure_running(ctr)
    print(f"[2b] stress-ng con {args.workers} workers en {ctr} (dura hasta {args.duration} s o hasta un Restart) ...")
    t0 = time.time()
    run(["docker", "exec", "-d", "-u", "root", ctr,
         "stress-ng", "--cpu", str(args.workers), "--timeout", f"{args.duration}s"])
    log_event("2b", "cpu_stress", args.service, ctr, t0, None,
              f"workers={args.workers};max_duration={args.duration}", "restart")


def latency(args):
    """Escenarios 3a (corta, se va sola: Do Nothing) y 3b (persistente: Restart)."""
    ctr = container_name(args.service)
    ensure_running(ctr)
    run(["docker", "exec", "-u", "root", ctr, "tc", "qdisc", "del", "dev", "eth0", "root"],
        check=False, quiet=True)  # limpia una latencia anterior si existía
    netem = ["docker", "exec", "-u", "root", ctr, "tc", "qdisc", "add", "dev", "eth0", "root",
             "netem", "delay", f"{args.delay}ms", f"{args.jitter}ms"]
    if args.long:
        print(f"[3b] Latencia persistente de {args.delay} ms en {ctr} (se quita con Restart o reset) ...")
        t0 = time.time()
        run(netem)
        log_event("3b", "latency_long", args.service, ctr, t0, None,
                  f"delay_ms={args.delay};jitter_ms={args.jitter}", "restart")
    else:
        print(f"[3a] Hipo de red de {args.delay} ms durante {args.short_duration} s en {ctr} ...")
        t0 = time.time()
        run(netem)
        log_event("3a", "latency_short", args.service, ctr, t0, t0 + args.short_duration,
                  f"delay_ms={args.delay};jitter_ms={args.jitter};duration={args.short_duration}",
                  "do_nothing")
        time.sleep(args.short_duration)
        # Si el healer reinició el contenedor, la latencia ya no existe: no es error.
        run(["docker", "exec", "-u", "root", ctr, "tc", "qdisc", "del", "dev", "eth0", "root"],
            check=False)
        print("  -> latencia retirada")


def reset(_args):
    """Deja el sistema como al inicio: sin fallos activos y con 1 réplica por servicio."""
    print("[reset] Deteniendo pico de tráfico ...")
    run(["docker", "rm", "-f", SPIKE_CONTAINER], check=False)
    print("[reset] Quitando latencia y stress-ng de los contenedores vivos ...")
    r = run(["docker", "ps", "--filter", f"label=com.docker.compose.project={PROJECT}",
             "--format", "{{.Names}}"], quiet=True)
    for ctr in r.stdout.split():
        if any(ctr.startswith(f"{PROJECT}-{s}-") for s in APP_SERVICES):
            run(["docker", "exec", "-u", "root", ctr, "tc", "qdisc", "del", "dev", "eth0", "root"],
                check=False, quiet=True)
            run(["docker", "exec", "-u", "root", ctr, "pkill", "-f", "stress-ng"],
                check=False, quiet=True)
    print("[reset] Levantando servicios caídos y volviendo a 1 réplica ...")
    scale = [arg for s in APP_SERVICES for arg in ("--scale", f"{s}=1")]
    run(["docker", "compose", "up", "-d", "--no-recreate", *scale, "postgres", *APP_SERVICES])
    print("[reset] Listo.")


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="chaos-injector del sistema de matrícula")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("crash", help="1: mata un servicio")
    p.add_argument("--service", default="courses", choices=APP_SERVICES)
    p.set_defaults(func=crash)

    p = sub.add_parser("traffic-spike", help="2a: pico de carga con Locust")
    p.add_argument("--users", type=int, default=150)
    p.add_argument("--duration", type=int, default=120, help="segundos")
    p.set_defaults(func=traffic_spike)

    p = sub.add_parser("cpu-stress", help="2b: stress-ng dentro del contenedor")
    p.add_argument("--service", default="courses", choices=APP_SERVICES)
    p.add_argument("--workers", type=int, default=2)
    p.add_argument("--duration", type=int, default=600, help="máximo en segundos")
    p.set_defaults(func=cpu_stress)

    p = sub.add_parser("latency", help="3a/3b: latencia de red con tc netem")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--short", action="store_true", help="3a: transitoria")
    g.add_argument("--long", action="store_true", help="3b: persistente")
    p.add_argument("--service", default="billing", choices=APP_SERVICES)
    p.add_argument("--delay", type=int, default=600, help="ms agregados a cada paquete")
    p.add_argument("--jitter", type=int, default=50, help="variación en ms")
    p.add_argument("--short-duration", type=int, default=8, help="segundos (solo --short)")
    p.set_defaults(func=latency)

    p = sub.add_parser("reset", help="quita todos los fallos y vuelve a 1 réplica")
    p.set_defaults(func=reset)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
