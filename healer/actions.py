"""
Las 3 acciones de recuperación. Las usan igual las reglas (F6) y el agente RL (F7),
para que la comparación sea justa: solo cambia QUIÉN decide, no CÓMO se actúa.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "self-healing-matricula"
MAX_REPLICAS = 3

DO_NOTHING, RESTART, SCALE = "do_nothing", "restart", "scale"
ACTIONS = [DO_NOTHING, RESTART, SCALE]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


def containers(service: str) -> list[dict]:
    """Contenedores del servicio con su estado: [{'name':..., 'running': bool}]."""
    r = _run([
        "docker", "ps", "-a",
        "--filter", f"label=com.docker.compose.project={PROJECT}",
        "--filter", f"label=com.docker.compose.service={service}",
        "--format", "{{.Names}};{{.State}}",
    ])
    out = []
    for line in r.stdout.split():
        name, state = line.split(";")
        out.append({"name": name, "running": state == "running"})
    return sorted(out, key=lambda c: c["name"])


def _cpu_by_container(names: list[str]) -> dict[str, float]:
    if not names:
        return {}
    r = _run(["docker", "stats", "--no-stream", "--format", "{{.Name}};{{.CPUPerc}}", *names])
    cpu = {}
    for line in r.stdout.split():
        try:
            name, pct = line.split(";")
            cpu[name] = float(pct.rstrip("%"))
        except ValueError:
            pass
    return cpu


def restart(service: str) -> str:
    """Reinicia la réplica con problemas: primero una que esté caída; si todas
    están vivas, la que más CPU consume (la que tiene el proceso trabado)."""
    ctrs = containers(service)
    if not ctrs:
        return "sin contenedores"
    dead = [c for c in ctrs if not c["running"]]
    if dead:
        target = dead[0]["name"]
        _run(["docker", "start", target])
        return f"start {target}"
    cpu = _cpu_by_container([c["name"] for c in ctrs])
    target = max(ctrs, key=lambda c: cpu.get(c["name"], 0.0))["name"]
    _run(["docker", "restart", "-t", "2", target])
    return f"restart {target}"


def scale(service: str) -> str:
    """Agrega una réplica (hasta MAX_REPLICAS)."""
    n = len(containers(service))
    if n >= MAX_REPLICAS:
        return f"ya tiene {n} réplicas (máximo {MAX_REPLICAS})"
    r = _run(["docker", "compose", "up", "-d", "--no-recreate", "--scale", f"{service}={n + 1}", service])
    if r.returncode != 0:
        return f"error al escalar: {r.stderr.strip()[:200]}"
    return f"scale {service} {n} -> {n + 1}"


def execute(service: str, action: str) -> str:
    if action == RESTART:
        return restart(service)
    if action == SCALE:
        return scale(service)
    return ""
