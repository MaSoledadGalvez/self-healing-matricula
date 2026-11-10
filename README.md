# self-healing-matricula

Sistema de matrícula universitaria con microservicios que **se repara solo** ante fallos.
Compara dos estrategias de recuperación:

- **Reglas** (baseline): umbrales fijos.
- **IA**: detector de anomalías (Isolation Forest) + agente Q-learning.

Ciclo del healer: **Fallo → Detección → Decisión → Acción (Do Nothing / Restart / Scale) → Recuperación**.

## Requisitos

- Docker Desktop (con Docker Compose)
- Python 3.10 o superior
- Puertos libres: `8080` (gateway), `3000` (Grafana), `9090` (Prometheus), `8081` (cAdvisor)

> Todos los comandos se ejecutan **desde la raíz del repo** (`self-healing-matricula/`).

## 1. Configuración inicial (solo la primera vez)

```powershell
# Variables de la base de datos
copy .env.example .env

# Dependencias de Python (healer con IA y análisis de resultados)
pip install -r healer/requirements.txt -r experiments/requirements.txt
```

## 2. Levantar el sistema

```powershell
docker compose up --build -d
```

La primera vez tarda unos minutos (construye la imagen y carga la base de datos).
Comprueba que todo esté arriba:

| Qué | Dónde |
|---|---|
| Estado de los servicios | http://localhost:8080/status |
| Grafana (dashboard) | http://localhost:3000 (admin / admin) |
| Prometheus | http://localhost:9090 |
| cAdvisor | http://localhost:8081 |

Para probar la API a mano usa [docs/pruebas.http](docs/pruebas.http) (extensión REST Client de VS Code).

## 3. Generar tráfico de estudiantes

```powershell
docker compose --profile load up -d
```

Lanza 20 estudiantes simulados con Locust. Para usar más: `$env:LOCUST_USERS=40; docker compose --profile load up -d`.

## 4. Entrenar la IA (opcional)

Los modelos ya entrenados están en [healer/models/](healer/models/), así que este paso se puede saltar.
Para reentrenarlos:

```powershell
# a) Grabar al menos 10 min de datos normales (sin fallos). Detener con Ctrl + C
python healer/healer.py --mode none

# b) Entrenar el detector y el agente Q-learning (en el simulador)
python healer/train.py
```

`python healer/train.py --skip-detector` reentrena solo el agente.

## 5. Ejecutar el healer e inyectar fallos

Usa **dos terminales**.

**Terminal 1: el healer** (elige una estrategia)

```powershell
python healer/healer.py --mode rules    # reglas fijas (baseline)
python healer/healer.py --mode rl       # IA: detector + Q-learning
python healer/healer.py --mode none     # sin auto-recuperación (referencia)
```

**Terminal 2: el chaos-injector** (espera ~20 s después de arrancar el healer)

| Escenario | Comando | Acción correcta |
|---|---|---|
| 1 · Caída de `courses` | `python chaos/chaos.py crash` | Restart |
| 2a · Pico de tráfico | `python chaos/chaos.py traffic-spike` | Scale |
| 2b · CPU trabada | `python chaos/chaos.py cpu-stress` | Restart |
| 3a · Latencia corta | `python chaos/chaos.py latency --short` | Do Nothing |
| 3b · Latencia larga | `python chaos/chaos.py latency --long` | Restart |

En la consola del healer verás la acción que toma y, al cerrarse el incidente, el tiempo de
detección, el recovery time y el downtime. En Grafana se ve el fallo y la recuperación.

**Entre cada prueba** vuelve al estado inicial (sin fallos y 1 réplica por servicio):

```powershell
python chaos/chaos.py reset
```

Resultados de cada corrida:

- `data/chaos_log.csv`: cuándo se inyectó cada fallo
- `data/incidents.csv`: detección, acciones y recovery time de cada incidente
- `data/healer_trace.csv`: estado y decisión por servicio en cada ciclo

## 6. Experimentos automáticos (opcional)

Corre todos los escenarios con ambas estrategias, sin supervisión. Con `--reps 5` tarda varias horas.
Si se interrumpe, al volver a ejecutarlo continúa donde quedó.

```powershell
python experiments/run_experiments.py --reps 5
python experiments/run_experiments.py --reps 5 --scenarios 2b 3a   # solo algunos
```

Después genera las tablas y gráficas (con Prometheus corriendo):

```powershell
python experiments/analyze.py
```

Salida en [data/results/](data/results/): `summary.md`, `summary.csv` y gráficas `.png`.

## 7. Apagar

```powershell
python chaos/chaos.py reset
docker compose down          # agrega -v para borrar los datos y recargar la BD
```

## Estructura

```
services/        microservicios (gateway, students, courses, enrollment, billing)
db/init/         esquema y datos iniciales de PostgreSQL
observability/   Prometheus, Grafana, blackbox
load/            tráfico de estudiantes (Locust)
chaos/           chaos-injector (los 5 escenarios de fallo)
healer/          healer: reglas, detector, agente Q-learning y simulador
experiments/     experimentos automáticos y análisis
data/            logs, datasets y resultados
```
