"""
Baseline: reglas estáticas (diapositiva 9 de la presentación).

    /health falla k veces seguidas        -> Restart
    CPU > 85 % durante t segundos         -> Scale
    Latencia p95 > umbral                 -> Restart
    en otro caso                          -> Do Nothing

Simple a propósito: no distingue causas (2b se trata como 2a) y reacciona a
picos transitorios (3a). Así se ve dónde la IA puede hacerlo mejor.
"""
from dataclasses import dataclass

from actions import DO_NOTHING, RESTART, SCALE
from prom import latency_signal

HEALTH_FAILS_K = 2          # ciclos seguidos con /health caído
CPU_THRESHOLD = 85.0        # %
CPU_CYCLES = 2              # ciclos seguidos sobre el umbral (2 x 5 s = 10 s)
LATENCY_THRESHOLD_MS = 500.0


@dataclass
class Decision:
    action: str
    reason: str
    anomalous: bool   # ¿el detector de esta estrategia ve algo raro en este ciclo?


class RulesStrategy:
    name = "rules"

    def __init__(self):
        self.health_fails: dict[str, int] = {}
        self.cpu_high: dict[str, int] = {}

    def decide(self, service: str, obs: dict) -> Decision:
        down = obs["health"] < 1
        cpu = obs["cpu_avg_pct"]          # la regla clásica mira el promedio del servicio
        lat = latency_signal(obs)

        self.health_fails[service] = self.health_fails.get(service, 0) + 1 if down else 0
        self.cpu_high[service] = self.cpu_high.get(service, 0) + 1 if cpu > CPU_THRESHOLD else 0
        anomalous = down or cpu > CPU_THRESHOLD or lat > LATENCY_THRESHOLD_MS

        if self.health_fails[service] >= HEALTH_FAILS_K:
            return Decision(RESTART, f"/health falló {self.health_fails[service]} veces", anomalous)
        if self.cpu_high[service] >= CPU_CYCLES:
            return Decision(SCALE, f"CPU {cpu:.0f}% > {CPU_THRESHOLD:.0f}%", anomalous)
        if not down and lat > LATENCY_THRESHOLD_MS:
            return Decision(RESTART, f"latencia {lat:.0f} ms > {LATENCY_THRESHOLD_MS:.0f} ms", anomalous)
        return Decision(DO_NOTHING, "", anomalous)

    def reset(self, service: str):
        """Se llama después de actuar: los contadores empiezan de nuevo."""
        self.health_fails[service] = 0
        self.cpu_high[service] = 0


class NoHealingStrategy:
    """Referencia sin auto-recuperación: detecta pero nunca actúa."""
    name = "none"

    def __init__(self):
        self.rules = RulesStrategy()

    def decide(self, service: str, obs: dict) -> Decision:
        d = self.rules.decide(service, obs)
        return Decision(DO_NOTHING, "", d.anomalous)

    def reset(self, service: str):
        pass
