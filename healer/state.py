"""
Estado discreto del agente Q-learning (uno por servicio).

    (health, cpu, traffic, latency, replicas, age)

    health    0 caído · 1 arriba
    cpu       0 < 60 % · 1 60-85 % · 2 > 85 %       (réplica más cargada)
    traffic   0 normal · 1 muy por encima de lo normal   -> distingue 2a de 2b
    latency   0 normal · 1 /health lento                  -> 3a / 3b
    replicas  1 · 2 · 3
    age       cuánto lleva la anomalía: 0 recién (1-2 ciclos) · 1 un rato (3-4) · 2 persistente (5+)
              -> distingue latencia transitoria (3a) de persistente (3b)

Lo usan IGUAL el simulador (para entrenar) y el healer real (para decidir).
Para latencia se usa el tiempo de /health de cada servicio (blackbox), no la
latencia vista por quien llama: así el agente culpa al servicio lento y no a
quien lo está esperando (el error que cometían las reglas con enrollment).
"""

CPU_MID, CPU_HIGH = 60.0, 85.0
LAT_MIN_MS = 300.0         # /health por encima de esto es lento ...
LAT_FACTOR = 5.0           # ... o 5 veces su valor normal (lo que sea mayor)
TRAFFIC_FACTOR = 3.0       # tráfico "alto": 3 veces lo normal ...
TRAFFIC_MIN_EXTRA = 20.0   # ... y al menos 20 req/s por encima (evita ruido en servicios con poco tráfico)

DEFAULT_BASELINE = {"req_mean": 10.0, "probe_p95": 5.0}


def age_bin(cycles_anomalous: int) -> int:
    if cycles_anomalous <= 2:
        return 0
    if cycles_anomalous <= 4:
        return 1
    return 2


def discretize(obs: dict, baseline: dict | None = None, cycles_anomalous: int = 0) -> tuple:
    b = baseline or DEFAULT_BASELINE
    health = 1 if obs.get("health", 0) >= 1 else 0
    cpu_v = obs.get("cpu_max_pct") or 0.0
    cpu = 2 if cpu_v > CPU_HIGH else (1 if cpu_v > CPU_MID else 0)
    req = obs.get("req_rate") or 0.0
    traffic = 1 if (req > TRAFFIC_FACTOR * b["req_mean"] and req > b["req_mean"] + TRAFFIC_MIN_EXTRA) else 0
    probe = obs.get("probe_ms") or 0.0
    latency = 1 if probe > max(LAT_MIN_MS, LAT_FACTOR * b["probe_p95"]) else 0
    replicas = int(max(1, min(3, round(obs.get("replicas") or 1))))
    if health == 0:
        cpu = traffic = latency = 0   # si está caído, lo demás no importa
    return (health, cpu, traffic, latency, replicas, age_bin(cycles_anomalous))


def looks_normal(s: tuple) -> bool:
    health, cpu, traffic, latency, _, _ = s
    return health == 1 and cpu < 2 and traffic == 0 and latency == 0


def is_ok(s: tuple) -> bool:
    """Servicio funcionando bien para el usuario (aunque tenga tráfico alto)."""
    health, cpu, _, latency, _, _ = s
    return health == 1 and cpu < 2 and latency == 0


def state_key(s: tuple) -> str:
    return ",".join(map(str, s))


def describe(s: tuple) -> str:
    health, cpu, traffic, latency, replicas, age = s
    return (f"{'arriba' if health else 'CAÍDO'}, CPU {['baja', 'media', 'ALTA'][cpu]}, "
            f"tráfico {'ALTO' if traffic else 'normal'}, latencia {'ALTA' if latency else 'normal'}, "
            f"x{replicas}, anomalía {['recién', 'un rato', 'persistente'][age]}")
