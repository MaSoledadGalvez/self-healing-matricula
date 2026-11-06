"""
Agente Q-learning tabular (F7).

  Estado      (health, cpu, traffic, latency, replicas, age)   ver state.py
  Acciones    Do Nothing · Restart · Scale
  Recompensa  + recuperar · − downtime · − degradación · − acciones y réplicas extra

Se entrena en el simulador (train.py) y se usa en el healer real con --mode rl:
el detector decide SI hay un problema; el agente decide QUÉ hacer.
"""
import json
import random
from pathlib import Path

from actions import ACTIONS, DO_NOTHING
from detector import Detector
from rules import Decision
from simulator import ServiceSim
from state import describe, discretize, state_key

Q_PATH = Path(__file__).resolve().parent / "models" / "q_table.json"


# ------------------------------------------------------------------ entrenamiento
def train_q(episodes: int = 60000, alpha: float = 0.1, gamma: float = 0.97,
            eps_start: float = 1.0, eps_end: float = 0.05, seed: int = 42):
    rng = random.Random(seed)
    Q: dict[str, list[float]] = {}
    history = []
    for ep in range(episodes):
        eps = max(eps_end, eps_start - (eps_start - eps_end) * ep / (0.7 * episodes))
        sim = ServiceSim(rng=rng)
        s, total = sim.state, 0.0
        while not sim.done:
            k = state_key(s)
            q = Q.setdefault(k, [0.0, 0.0, 0.0])
            a = rng.randrange(3) if rng.random() < eps else max(range(3), key=lambda i: q[i])
            s2, r, done = sim.step(a)
            q2 = Q.setdefault(state_key(s2), [0.0, 0.0, 0.0])
            target = r if done else r + gamma * max(q2)
            q[a] += alpha * (target - q[a])
            s, total = s2, total + r
        history.append({"episode": ep, "scenario": sim.scenario, "reward": round(total, 2),
                        "steps": sim.t, "epsilon": round(eps, 3)})
    return Q, history


def save_q(Q: dict):
    Q_PATH.parent.mkdir(parents=True, exist_ok=True)
    Q_PATH.write_text(json.dumps(Q, indent=1), encoding="utf-8")


def load_q() -> dict:
    if not Q_PATH.exists():
        raise SystemExit("No hay tabla Q. Ejecuta: python healer/train.py")
    return json.loads(Q_PATH.read_text(encoding="utf-8"))


def greedy(Q: dict, s: tuple) -> int | None:
    q = Q.get(state_key(s))
    if q is None or q == [0.0, 0.0, 0.0]:
        return None  # estado nunca visto en el entrenamiento
    return max(range(3), key=lambda i: q[i])


# ------------------------------------------------------------------ uso en el healer real
class RLStrategy:
    name = "rl"

    def __init__(self, detector_kind: str = "iforest"):
        self.detector = Detector(detector_kind)
        self.Q = load_q()
        self.cycles_anomalous: dict[str, int] = {}

    def decide(self, service: str, obs: dict) -> Decision:
        anomalous, why = self.detector.check(service, obs)
        n = self.cycles_anomalous.get(service, 0) + 1 if anomalous else 0
        self.cycles_anomalous[service] = n
        if not anomalous:
            return Decision(DO_NOTHING, "", False)
        s = discretize(obs, self.detector.baseline(service), n)
        a = greedy(self.Q, s)
        if a is None:
            return Decision(DO_NOTHING, f"{why}; estado no visto [{describe(s)}]", True)
        return Decision(ACTIONS[a], f"{why}; [{describe(s)}]", True)

    def reset(self, service: str):
        # Tras actuar, la "edad" de la anomalía vuelve a empezar.
        self.cycles_anomalous[service] = 0
