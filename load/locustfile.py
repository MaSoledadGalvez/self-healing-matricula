"""
Estudiantes simulados para el sistema de matrícula (F4).

Dos perfiles:
  EstudianteNormal -> un día cualquiera: la mayoría consulta materias y horarios,
                      una minoría se matricula. Es el tráfico de fondo en todos
                      los experimentos.
  EstudiantePico   -> apertura de matrícula: miles entran a la vez y recargan la
                      oferta sin parar. Lo lanza el chaos-injector (escenario 2a).

Se ejecuta dentro de Docker (no hay que instalar Locust en Windows):
  docker compose --profile load up -d                  # tráfico normal (20 usuarios)
  $env:LOCUST_USERS=40; docker compose --profile load up -d   # más usuarios
"""
import random

from locust import HttpUser, between, task

N_STUDENTS = 500
N_COURSES = 40
N_SECTIONS = 120  # 40 materias x 3 paralelos


def ok_codes(response, *codes):
    """Marca como éxito los códigos esperados del negocio (p. ej. 409 'sin cupos')."""
    if response.status_code in codes:
        response.success()
    else:
        response.failure(f"HTTP {response.status_code}")


class EstudianteNormal(HttpUser):
    wait_time = between(1, 3)  # cada estudiante "piensa" 1-3 s entre clics

    def on_start(self):
        self.student_id = random.randint(1, N_STUDENTS)
        with self.client.post(
            "/login",
            json={"email": f"estudiante{self.student_id}@universidad.edu"},
            name="/login",
            catch_response=True,
        ) as r:
            ok_codes(r, 200)

    @task(5)
    def ver_materias(self):
        self.client.get("/courses", name="/courses")

    @task(4)
    def ver_paralelos(self):
        course = random.randint(1, N_COURSES)
        self.client.get(f"/courses/{course}/sections", name="/courses/:id/sections")

    @task(2)
    def ver_perfil(self):
        self.client.get(f"/students/{self.student_id}", name="/students/:id")

    @task(1)
    def ver_mis_matriculas(self):
        self.client.get(f"/enrollments/{self.student_id}", name="/enrollments/:studentId")

    @task(1)
    def matricularse(self):
        section = random.randint(1, N_SECTIONS)
        with self.client.post(
            "/enrollments",
            json={"studentId": self.student_id, "sectionId": section},
            name="/enrollments",
            catch_response=True,
        ) as r:
            # 201 matriculado, 409 ya matriculado o sin cupos: ambos son respuestas válidas.
            ok_codes(r, 201, 409)


class EstudiantePico(HttpUser):
    """Apertura de matrícula: recargan la oferta casi sin pausa (satura courses)."""

    wait_time = between(0.05, 0.2)

    @task(3)
    def ver_materias(self):
        self.client.get("/courses", name="/courses")

    @task(2)
    def ver_paralelos(self):
        course = random.randint(1, N_COURSES)
        self.client.get(f"/courses/{course}/sections", name="/courses/:id/sections")
