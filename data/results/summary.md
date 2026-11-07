| Escenario | Estrategia | n | Acción correcta | Recuperado | Recovery time (s) | Downtime (s) | Disponibilidad (%) | Peticiones OK (%) | Acciones innecesarias |
|---|---|---|---|---|---|---|---|---|---|
| 1 · Caída | Reglas (baseline) | 5 | 100 % | 100 % | 29.2 ± 5.6 | 20.0 ± 3.5 | 89.19 ± 1.91 | 93.74 ± 2.25 | 1.0 ± 0.0 |
| 1 · Caída | IA (detector + Q-learning) | 5 | 100 % | 100 % | 25.6 ± 2.9 | 8.0 ± 2.7 | 95.68 ± 1.48 | 99.95 ± 0.01 | 0.0 ± 0.0 |
| 2a · Pico de tráfico | Reglas (baseline) | 5 | 100 % | 100 % | 55.1 ± 13.3 | 1.0 ± 2.2 | 99.46 ± 1.21 | 99.99 ± 0.02 | 0.2 ± 0.4 |
| 2a · Pico de tráfico | IA (detector + Q-learning) | 4 | 100 % | 100 % | 40.3 ± 8.8 | 1.2 ± 2.5 | 99.32 ± 1.35 | 99.96 ± 0.05 | 0.2 ± 0.5 |
| 2b · CPU trabada | Reglas (baseline) | 5 | 0 % | 20 % | 281.0 ± 42.5 | 1.0 ± 2.2 | 99.46 ± 1.21 | 99.99 ± 0.03 | 1.0 ± 0.0 |
| 2b · CPU trabada | IA (detector + Q-learning) | 5 | 100 % | 100 % | 30.4 ± 7.3 | 2.0 ± 2.7 | 98.92 ± 1.48 | 99.76 ± 0.07 | 0.0 ± 0.0 |
| 3a · Latencia corta | Reglas (baseline) | 5 | 40 % | 100 % | 21.9 ± 15.2 | 2.0 ± 2.7 | 98.92 ± 1.48 | 99.97 ± 0.05 | 1.0 ± 1.0 |
| 3a · Latencia corta | IA (detector + Q-learning) | 5 | 100 % | 100 % | 25.3 ± 7.9 | 3.0 ± 2.7 | 98.38 ± 1.48 | 99.92 ± 0.10 | 0.0 ± 0.0 |
| 3b · Latencia larga | Reglas (baseline) | 5 | 100 % | 100 % | 27.8 ± 8.4 | 3.0 ± 4.5 | 98.38 ± 2.42 | 99.90 ± 0.09 | 0.8 ± 0.4 |
| 3b · Latencia larga | IA (detector + Q-learning) | 5 | 100 % | 100 % | 28.4 ± 7.6 | 1.0 ± 2.2 | 99.46 ± 1.21 | 95.80 ± 2.45 | 0.0 ± 0.0 |

Recovery time de corridas no recuperadas = 300 s (tope del experimento).