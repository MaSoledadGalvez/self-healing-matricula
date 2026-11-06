"""
Simulador de UN servicio para entrenar al agente Q-learning (F7).

Entrenar con fallos reales tomaría días (cada episodio dura minutos). El simulador
imita, paso a paso (1 paso = 5 s, igual que el healer), cómo cambian las métricas
ante cada fallo y cada acción. Sus números están calibrados con las corridas
reales de F6 (healer_trace.csv / incidents.csv):

  - Restart: el contenedor vuelve en ~1 ciclo; con 1 réplica hay 1 ciclo sin servicio.
  - Pico (2a): 1 réplica se satura (93-100 %); con 2 réplicas cada una queda en ~63 %.
  - CPU trabada (2b): la réplica afectada sigue al 100 % aunque se escale.
  - Latencia de red: /health tarda ~1.2 s; la corta dura ~8 s (1-2 ciclos).
  - Scale sobre un servicio caído también lo levanta (docker compose up lo arranca).
"""
import random

from state import discretize, is_ok, looks_normal

DO_NOTHING, RESTART, SCALE = 0, 1, 2
ACTION_NAMES = ["do_nothing", "restart", "scale"]
SCENARIOS = ["normal", "crash", "spike", "cpu_stress", "latency_short", "latency_long"]
# Frecuencia de cada situación al entrenar: los hipos de red cortos son mucho más
# comunes que una red rota de forma persistente, y la mayor parte del tiempo no pasa nada.
SCENARIO_WEIGHTS = [2, 1, 1, 1, 3, 1]
MAX_STEPS = 36            # 3 minutos por episodio
MAX_REPLICAS = 3

# Recompensa: + recuperar · − downtime · − degradación · − acciones y réplicas extra
R_DOWN = -10.0
R_DEGRADED = -3.0
R_ACTION = -2.0
R_REPLICA = -0.5
R_RECOVERED = 5.0

R_REPLICA_LEFT = -25.0    # al final: cada réplica extra que queda (el healer no desescala solo)
COOLDOWN_STEPS = 4        # igual que el healer real: 20 s sin volver a actuar sobre el servicio
RESTART_DOWN_P = 0.8      # con 1 réplica, el restart casi siempre deja ~5 s sin /health (medido en F6: 3a y 3b)

SPIKE_DEMAND = 130.0      # % de una réplica que pide el pico (1 réplica: 100 %; 2: ~65 %)


class ServiceSim:
    def __init__(self, scenario: str | None = None, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self.scenario = scenario or self.rng.choices(SCENARIOS, weights=SCENARIO_WEIGHTS)[0]
        self.replicas = 1
        self.t = 0
        self.fault_at = 1                             # el fallo empieza en el primer paso
        self.crashed = self.stressed = self.slow = False
        self.spike_left = self.slow_left = 0
        self.restarting = False
        self.cycles_anomalous = 0
        self.down_ticks = 0
        self.was_bad = False
        self.healthy_streak = 0
        self.done = False
        self.obs = self._observe()
        self.state = discretize(self.obs, None, 0)
        # El episodio empieza cuando el fallo ya ocurrió: así el estado "normal" no
        # arrastra el costo de un fallo futuro (lo que sesgaría el aprendizaje).
        self._tick()

    # ---------------------------------------------------------------- fallos
    def _inject(self):
        s = self.scenario
        if s == "crash":
            self.crashed = True
        elif s == "spike":
            self.spike_left = self.rng.randint(18, 26)      # ~90-130 s
        elif s == "cpu_stress":
            self.stressed = True
        elif s == "latency_short":
            self.slow, self.slow_left = True, self.rng.randint(1, 2)
        elif s == "latency_long":
            self.slow, self.slow_left = True, 10 ** 6

    def fault_active(self) -> bool:
        return self.crashed or self.stressed or self.slow or self.spike_left > 0

    # ---------------------------------------------------------------- métricas
    def _observe(self) -> dict:
        g = self.rng.gauss
        down = self.crashed or (self.restarting and self.replicas == 1
                                and self.rng.random() < RESTART_DOWN_P)
        cpu = max(0.0, g(5, 2))
        req = max(0.0, g(10, 2))
        probe = max(1.0, g(3, 1))
        if self.spike_left > 0:
            req = max(0.0, g(1100, 60))
            cpu = min(100.0, SPIKE_DEMAND / self.replicas + g(0, 4))
            probe = max(1.0, g(40, 15))
        if self.stressed:
            cpu = min(110.0, g(100, 3))
            probe = max(1.0, g(20, 8))
        if self.slow:   # la réplica lenta sigue recibiendo tráfico aunque haya otras
            probe = max(1.0, g(1200, 100))
        if down:
            return {"health": 0, "cpu_max_pct": 0.0, "req_rate": 0.0, "err_rate": 0.0,
                    "probe_ms": 2000.0, "replicas": 0}
        return {"health": 1, "cpu_max_pct": cpu, "req_rate": req, "err_rate": 0.0,
                "probe_ms": probe, "replicas": self.replicas}

    # ---------------------------------------------------------------- paso
    def step(self, action: int):
        """Aplica una acción y avanza el tiempo. Si se actúa, avanza además los
        ciclos de enfriamiento (como el healer real, que no puede volver a actuar
        sobre el servicio durante 20 s): el agente ve directamente cómo quedó el
        servicio después de su acción."""
        reward = 0.0
        if action == RESTART:
            reward += R_ACTION
            self.restarting = True                       # ~2-3 s sin servicio si hay 1 réplica
            self.crashed = self.stressed = False
            self.slow, self.slow_left = False, 0
        elif action == SCALE:
            reward += R_ACTION
            self.replicas = min(MAX_REPLICAS, self.replicas + 1)
            self.crashed = False                         # compose up arranca el contenedor caído
        if action != DO_NOTHING:
            self.cycles_anomalous = 0
        ticks = 1 + (COOLDOWN_STEPS if action != DO_NOTHING else 0)
        for _ in range(ticks):
            reward += self._tick()
            self.restarting = False
            if self.done:
                break
        return self.state, reward, self.done

    def _tick(self) -> float:
        reward = 0.0
        self.t += 1
        if self.t == self.fault_at:
            self._inject()
        if self.spike_left > 0:
            self.spike_left -= 1
        if self.slow and self.slow_left < 10 ** 5:
            self.slow_left -= 1
            if self.slow_left < 0:
                self.slow = False

        self.obs = self._observe()
        tmp = discretize(self.obs, None, self.cycles_anomalous + 1)
        self.cycles_anomalous = 0 if looks_normal(tmp) else self.cycles_anomalous + 1
        self.state = discretize(self.obs, None, self.cycles_anomalous)

        if self.obs["health"] == 0:
            reward += R_DOWN
            self.down_ticks += 1
        elif not is_ok(self.state):
            reward += R_DEGRADED
        reward += R_REPLICA * (self.replicas - 1)

        if not is_ok(self.state):
            self.was_bad, self.healthy_streak = True, 0
        else:
            self.healthy_streak += 1
            if self.was_bad and self.healthy_streak == 1:
                reward += R_RECOVERED

        recovered = self.was_bad and self.healthy_streak >= 3
        self.done = recovered or self.t >= MAX_STEPS or (
            self.scenario == "normal" and self.t >= 12
        )
        if self.done:
            reward += R_REPLICA_LEFT * (self.replicas - 1)
        return reward


def rules_policy(state: tuple) -> int:
    """Las reglas de F6 traducidas al simulador (para comparar en simulación)."""
    health, cpu, traffic, latency, replicas, age = state
    if health == 0:
        return RESTART
    if cpu == 2:
        return SCALE if replicas < MAX_REPLICAS else DO_NOTHING
    if latency == 1:
        return RESTART
    return DO_NOTHING
