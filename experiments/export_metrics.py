"""
Exporta las métricas de Prometheus a un CSV (una fila por servicio cada STEP segundos).
Este CSV es tu DATASET: con él se entrena el detector de anomalías y se hacen las gráficas.

Uso (desde la raíz del repo, con docker compose corriendo):
    python experiments/export_metrics.py --minutes 30 --label normal
    python experiments/export_metrics.py --minutes 10 --label prueba --step 2

Solo usa la librería estándar de Python (no hay que instalar nada).
"""
import argparse
import csv
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROM = "http://localhost:9090"

# Contenedores de la app y cómo sacar el nombre del servicio desde el nombre del contenedor.
RE = "self-healing-matricula-(gateway|students|courses|enrollment|billing|postgres)-[0-9]+"
CAP = "self-healing-matricula-(.*)-[0-9]+"


def by_service(expr: str) -> str:
    """Agrega una métrica de cAdvisor (por contenedor) a nivel de servicio."""
    return f'label_replace({expr}, "service", "$1", "name", "{CAP}")'


# Selectores de cAdvisor (se arman aparte para que funcione en Python 3.11).
SEL = '{name=~"' + RE + '"}'
CPU_RATE = by_service(f"rate(container_cpu_usage_seconds_total{SEL}[30s])")
CPU_LIMIT = by_service(f"container_spec_cpu_quota{SEL} / container_spec_cpu_period{SEL}")
RAM = by_service(f"container_memory_working_set_bytes{SEL}")
CPU_RATE_CTR = by_service(f"sum by (name) (rate(container_cpu_usage_seconds_total{SEL}[30s]))")
CPU_LIMIT_CTR = by_service(f"(sum by (name) (container_spec_cpu_quota{SEL}) / sum by (name) (container_spec_cpu_period{SEL}))")

# Columna del CSV -> consulta PromQL (cada una devuelve una serie por servicio).
QUERIES = {
    # CPU del contenedor en % de su límite (suma de réplicas / suma de límites).
    "cpu_pct": f"100 * sum by (service) ({CPU_RATE}) / sum by (service) ({CPU_LIMIT})",
    # CPU de la réplica MÁS cargada: si una sola réplica está trabada (2b),
    # el promedio la esconde después de escalar; el máximo no.
    "cpu_max_pct": f"100 * max by (service) ({CPU_RATE_CTR} / {CPU_LIMIT_CTR})",
    "ram_mb": f"sum by (service) ({RAM}) / 1024 / 1024",
    # Respaldo si cAdvisor no funciona en tu PC (solo ve el proceso Node).
    "cpu_proc_pct": '100 * sum by (service) (rate(process_cpu_seconds_total{job="services"}[30s])) '
                    '/ (0.5 * count by (service) (up{job="services"} == 1))',
    "p95_ms": '1000 * histogram_quantile(0.95, sum by (service, le) '
              '(rate(http_request_duration_seconds_bucket{job="services"}[30s])))',
    # Latencia vista por quien llama (incluye red; detecta tc netem).
    "client_p95_ms": '1000 * histogram_quantile(0.95, sum by (service, le) (label_replace('
                     'rate(upstream_request_duration_seconds_bucket{job="services"}[30s]), '
                     '"service", "$1", "target", "(.*)")))',
    # Tiempo de respuesta de /health medido desde afuera (blackbox).
    "probe_ms": '1000 * max by (service) (probe_duration_seconds{job="blackbox"})',
    "req_rate": 'sum by (service) (rate(http_requests_total{job="services"}[30s]))',
    "err_rate": 'sum by (service) (rate(http_requests_total{job="services", status=~"5.."}[30s]))',
    "health": "max by (service) (probe_success)",
    "replicas": 'count by (service) (up{job="services"} == 1)',
}


def query_range(expr: str, start: float, end: float, step: int):
    params = urllib.parse.urlencode({"query": expr, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(f"{PROM}/api/v1/query_range?{params}", timeout=30) as r:
        body = json.load(r)
    if body.get("status") != "success":
        raise RuntimeError(body)
    return body["data"]["result"]


def main():
    ap = argparse.ArgumentParser(description="Exporta métricas de Prometheus a CSV")
    ap.add_argument("--minutes", type=float, default=30, help="minutos hacia atrás desde ahora")
    ap.add_argument("--step", type=int, default=5, help="segundos entre muestras")
    ap.add_argument("--label", default="sin_etiqueta", help="etiqueta del experimento (p. ej. normal)")
    ap.add_argument("--out", default=None, help="ruta del CSV (por defecto data/metrics_<label>_<fecha>.csv)")
    args = ap.parse_args()

    end = time.time()
    start = end - args.minutes * 60

    rows: dict[tuple[float, str], dict] = {}
    for col, expr in QUERIES.items():
        try:
            series = query_range(expr, start, end, args.step)
        except Exception as e:  # noqa: BLE001
            print(f"[aviso] no se pudo obtener {col}: {e}")
            continue
        if not series:
            print(f"[aviso] {col}: sin datos")
        for s in series:
            svc = s["metric"].get("service", "?")
            for ts, val in s["values"]:
                row = rows.setdefault((float(ts), svc), {})
                v = float(val)
                row[col] = "" if v != v else round(v, 4)  # NaN -> vacío

    if not rows:
        print("No hay datos. ¿Está corriendo docker compose y Prometheus en :9090?")
        return

    out = Path(args.out) if args.out else Path("data") / (
        f"metrics_{args.label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["timestamp", "datetime_utc", "service", "label", *QUERIES.keys()]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for (ts, svc) in sorted(rows):
            w.writerow({
                "timestamp": int(ts),
                "datetime_utc": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "service": svc,
                "label": args.label,
                **rows[(ts, svc)],
            })
    services = sorted({s for _, s in rows})
    print(f"Listo: {out}  ({len(rows)} filas, servicios: {', '.join(services)})")


if __name__ == "__main__":
    main()
