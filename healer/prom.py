"""
Lee de Prometheus el estado actual de cada servicio (la "observación" del healer).

Devuelve, por servicio, un diccionario con:
  health        1 si /health responde (blackbox), 0 si no
  probe_ms      cuánto tardó /health (detecta latencia de red al instante)
  cpu_avg_pct   CPU de todas las réplicas / suma de sus límites (%)
  cpu_max_pct   CPU de la réplica más cargada (%)  -> no se esconde tras escalar
  req_rate      peticiones por segundo
  err_rate      errores 5xx por segundo
  p95_ms        latencia p95 medida por el propio servicio
  client_p95_ms latencia p95 vista por quien lo llama (incluye red)
  replicas      réplicas vivas (0 si todas están caídas)
"""
import json
import math
import urllib.parse
import urllib.request

PROM_URL = "http://localhost:9090"
SERVICES = ["gateway", "students", "courses", "enrollment", "billing"]

_RE = "self-healing-matricula-(gateway|students|courses|enrollment|billing|postgres)-[0-9]+"
_CAP = "self-healing-matricula-(.*)-[0-9]+"
_SEL = '{name=~"' + _RE + '"}'


def _by_service(expr: str) -> str:
    return f'label_replace({expr}, "service", "$1", "name", "{_CAP}")'


# Ventanas de 20 s: reaccionan rápido y siempre tienen al menos 3 muestras (scrape cada 5 s).
_CPU_CTR = _by_service(f"sum by (name) (rate(container_cpu_usage_seconds_total{_SEL}[20s]))")
_LIM_CTR = _by_service(
    f"(sum by (name) (container_spec_cpu_quota{_SEL}) / sum by (name) (container_spec_cpu_period{_SEL}))"
)

QUERIES = {
    "health": 'max by (service) (probe_success{job="blackbox"})',
    "probe_ms": '1000 * max by (service) (probe_duration_seconds{job="blackbox"})',
    "cpu_avg_pct": f"100 * sum by (service) ({_CPU_CTR}) / sum by (service) ({_LIM_CTR})",
    "cpu_max_pct": f"100 * max by (service) ({_CPU_CTR} / {_LIM_CTR})",
    "req_rate": 'sum by (service) (rate(http_requests_total{job="services"}[20s]))',
    "err_rate": 'sum by (service) (rate(http_requests_total{job="services", status=~"5.."}[20s]))',
    "p95_ms": '1000 * histogram_quantile(0.95, sum by (service, le) '
              '(rate(http_request_duration_seconds_bucket{job="services"}[30s])))',
    "client_p95_ms": '1000 * histogram_quantile(0.95, sum by (service, le) (label_replace('
                     'rate(upstream_request_duration_seconds_bucket{job="services"}[20s]), '
                     '"service", "$1", "target", "(.*)")))',
    "replicas": 'count by (service) (up{job="services"} == 1)',
}

# Valor por defecto si Prometheus no devuelve la serie (p. ej. servicio sin tráfico o caído).
DEFAULTS = {
    "health": 0.0, "probe_ms": None, "cpu_avg_pct": None, "cpu_max_pct": None,
    "req_rate": 0.0, "err_rate": 0.0, "p95_ms": None, "client_p95_ms": None, "replicas": 0.0,
}


def _query(expr: str) -> dict[str, float]:
    url = f"{PROM_URL}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=5) as r:
        body = json.load(r)
    out = {}
    for item in body["data"]["result"]:
        v = float(item["value"][1])
        if not math.isnan(v) and not math.isinf(v):
            out[item["metric"].get("service", "?")] = v
    return out


# Si en una lectura falta la CPU de un servicio vivo (cAdvisor a veces se atrasa),
# usamos el último valor conocido en vez de suponer 0 %: un 0 % falso haría creer
# que un servicio saturado ya se recuperó.
HOLD_S = 30
_last_cpu: dict[str, tuple[float, dict]] = {}


def observe() -> dict[str, dict]:
    """Una foto del sistema: {servicio: {métrica: valor}}."""
    import time
    now = time.time()
    results = {name: _query(expr) for name, expr in QUERIES.items()}
    state = {}
    for svc in SERVICES:
        obs = {k: results[k].get(svc, DEFAULTS[k]) for k in QUERIES}
        cpu_keys = ("cpu_avg_pct", "cpu_max_pct")
        if all(obs[k] is not None for k in cpu_keys):
            _last_cpu[svc] = (now, {k: obs[k] for k in cpu_keys})
        else:
            prev = _last_cpu.get(svc)
            alive = obs["health"] >= 1 or obs["replicas"] > 0
            for k in cpu_keys:
                if obs[k] is None:
                    obs[k] = prev[1][k] if (alive and prev and now - prev[0] < HOLD_S) else 0.0
        state[svc] = obs
    return state


def latency_signal(obs: dict) -> float:
    """Latencia que perciben los usuarios del servicio: la de quien lo llama si existe,
    si no (gateway no tiene quien lo llame) el tiempo de /health."""
    v = obs.get("client_p95_ms")
    if v is None:
        v = obs.get("probe_ms")
    return v or 0.0