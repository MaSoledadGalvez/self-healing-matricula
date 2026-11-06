"""
Detector de anomalías (F7).

Se entrena SOLO con tráfico normal (filas de data/healer_trace.csv grabadas con
--mode none y sin fallos). Aprende cómo se ve cada servicio cuando está sano y
marca como anomalía lo que se aleja de eso.

  iforest  Isolation Forest multivariable (uno por servicio)       <- el principal
  zscore   referencia simple: alguna métrica a más de 3 desviaciones de su media

Ambos se combinan con dos comprobaciones duras que un modelo entrenado solo con
datos sanos no puede aprender (en los datos normales nunca hay caídas ni 5xx):
/health caído o errores 5xx por encima de 1 req/s.

Isolation Forest es bueno para combinaciones raras de varias métricas, pero le
cuesta ver una sola métrica extrema cuando las demás están normales (p. ej. la
CPU al 100 % con tráfico normal, escenario 2b). Por eso el modo iforest también
marca anomalía si alguna métrica está a más de 6 desviaciones de lo normal.
"""
import csv
import math
import pickle
from pathlib import Path

import numpy as np
from sklearn.ensemble import IsolationForest

from prom import SERVICES

MODEL_PATH = Path(__file__).resolve().parent / "models" / "detector.pkl"
FEATURES = ["cpu_max_pct", "req_rate", "err_rate", "probe_ms", "client_p95_ms"]
Z_THRESHOLD = 3.0          # modo zscore
Z_EXTREME = 6.0            # red de seguridad del modo iforest
ERR_MAX = 1.0


def _num(v) -> float | None:
    try:
        x = float(v)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def vectorize(obs: dict) -> list[float]:
    """Mismas transformaciones al entrenar y al usar: log para tasas y latencias."""
    cpu = _num(obs.get("cpu_max_pct")) or 0.0
    req = _num(obs.get("req_rate")) or 0.0
    err = _num(obs.get("err_rate")) or 0.0
    probe = _num(obs.get("probe_ms")) or 0.0
    client = _num(obs.get("client_p95_ms"))
    if client is None:
        client = probe
    return [cpu, math.log1p(req), err, math.log1p(probe), math.log1p(client)]


def load_normal_rows(trace: Path, mode: str = "none") -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {s: [] for s in SERVICES}
    with trace.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["mode"] != mode or r["service"] not in rows:
                continue
            if _num(r["health"]) != 1:
                continue  # por si hubo algún hipo: solo filas sanas
            rows[r["service"]].append(r)
    return rows


def train(trace: Path, mode: str = "none", seed: int = 42) -> dict:
    data = load_normal_rows(trace, mode)
    model = {"services": {}}
    for svc, rows in data.items():
        if len(rows) < 60:
            raise SystemExit(
                f"Muy pocos datos normales de {svc} ({len(rows)} filas). Graba al menos "
                f"10 minutos con: python healer/healer.py --mode none"
            )
        X = np.array([vectorize(r) for r in rows])
        iforest = IsolationForest(n_estimators=200, contamination=0.005, random_state=seed).fit(X)
        mean, std = X.mean(axis=0), X.std(axis=0)
        std = np.maximum(std, np.array([2.0, 0.2, 0.2, 0.3, 0.3]))  # piso: evita z enormes por ruido mínimo
        req = [_num(r["req_rate"]) or 0.0 for r in rows]
        probe = [_num(r["probe_ms"]) or 0.0 for r in rows]
        model["services"][svc] = {
            "iforest": iforest,
            "mean": mean, "std": std,
            "baseline": {
                "req_mean": float(np.mean(req)),
                "probe_p95": float(np.percentile(probe, 95)),
            },
            "n": len(rows),
        }
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MODEL_PATH.open("wb") as f:
        pickle.dump(model, f)
    return model


class Detector:
    def __init__(self, kind: str = "iforest"):
        if not MODEL_PATH.exists():
            raise SystemExit("No hay detector entrenado. Ejecuta: python healer/train.py")
        with MODEL_PATH.open("rb") as f:
            self.model = pickle.load(f)
        self.kind = kind

    def baseline(self, service: str) -> dict:
        return self.model["services"][service]["baseline"]

    def check(self, service: str, obs: dict) -> tuple[bool, str]:
        """(¿anomalía?, motivo)"""
        if (obs.get("health") or 0) < 1:
            return True, "/health caído"
        if (obs.get("err_rate") or 0) > ERR_MAX:
            return True, f"errores {obs['err_rate']:.1f}/s"
        m = self.model["services"][service]
        x = np.array([vectorize(obs)])
        if self.kind == "zscore":
            z = np.abs((x[0] - m["mean"]) / m["std"])
            i = int(np.argmax(z))
            if z[i] > Z_THRESHOLD:
                return True, f"z({FEATURES[i]})={z[i]:.1f}"
            return False, ""
        score = float(m["iforest"].decision_function(x)[0])
        if score < 0:
            return True, f"iforest {score:.3f}"
        z = np.abs((x[0] - m["mean"]) / m["std"])
        i = int(np.argmax(z))
        if z[i] > Z_EXTREME:
            return True, f"{FEATURES[i]} extremo (z={z[i]:.0f})"
        return False, ""
