-- Se ejecuta solo la primera vez que se crea el volumen de PostgreSQL.
-- Para recargarlo: docker compose down -v

-- Una base por servicio (fallos aislados)
CREATE DATABASE students_db;
CREATE DATABASE courses_db;
CREATE DATABASE enrollment_db;

-- =====================================================================
-- students_db: 500 estudiantes
-- =====================================================================
\connect students_db

CREATE TABLE students (
  id        SERIAL PRIMARY KEY,
  name      VARCHAR(100) NOT NULL,
  email     VARCHAR(120) UNIQUE NOT NULL,
  career    VARCHAR(80)  NOT NULL,
  semester  INT NOT NULL CHECK (semester BETWEEN 1 AND 10)
);

INSERT INTO students (name, email, career, semester)
SELECT
  'Estudiante ' || i,
  'estudiante' || i || '@universidad.edu',
  (ARRAY['Software','Computación','Telecomunicaciones','Industrial','Electrónica'])[1 + (i % 5)],
  1 + (i % 10)
FROM generate_series(1, 500) AS i;

-- =====================================================================
-- courses_db: 40 materias x 3 paralelos (A, B, C) con cupos limitados
-- =====================================================================
\connect courses_db

CREATE TABLE courses (
  id       SERIAL PRIMARY KEY,
  code     VARCHAR(10) UNIQUE NOT NULL,
  name     VARCHAR(100) NOT NULL,
  credits  INT NOT NULL CHECK (credits > 0)
);

CREATE TABLE sections (
  id         SERIAL PRIMARY KEY,
  course_id  INT NOT NULL REFERENCES courses(id),
  parallel   CHAR(1) NOT NULL,
  schedule   VARCHAR(60) NOT NULL,
  capacity   INT NOT NULL CHECK (capacity > 0),
  enrolled   INT NOT NULL DEFAULT 0 CHECK (enrolled >= 0),
  UNIQUE (course_id, parallel),
  CHECK (enrolled <= capacity)
);

-- 10 materias base x 4 niveles = 40 materias (p. ej. "Programación II")
INSERT INTO courses (code, name, credits)
SELECT
  'MAT-' || lpad(i::text, 3, '0'),
  (ARRAY['Cálculo','Física','Programación','Base de Datos','Redes',
         'Sistemas Operativos','Estadística','Álgebra Lineal',
         'Ingeniería de Software','Sistemas en la Nube'])[1 + (i - 1) % 10]
    || ' ' || (ARRAY['I','II','III','IV'])[1 + (i - 1) / 10],
  3 + (i % 3)
FROM generate_series(1, 40) AS i;

INSERT INTO sections (course_id, parallel, schedule, capacity)
SELECT
  c.id,
  p.parallel,
  (ARRAY['Lun-Mié','Mar-Jue','Mié-Vie','Lun-Jue','Mar-Vie'])[1 + (c.id + p.n) % 5]
    || ' ' || (ARRAY['07:00','09:00','11:00','14:00','16:00','18:00'])[1 + (c.id * 3 + p.n) % 6],
  30 + (c.id * 7 + p.n * 5) % 31          -- entre 30 y 60 cupos
FROM courses c
CROSS JOIN (VALUES (1, 'A'), (2, 'B'), (3, 'C')) AS p(n, parallel);

-- =====================================================================
-- enrollment_db: matrículas (vacía al inicio)
-- =====================================================================
\connect enrollment_db

CREATE TABLE enrollments (
  id           SERIAL PRIMARY KEY,
  student_id   INT NOT NULL,
  section_id   INT NOT NULL,
  course_code  VARCHAR(10) NOT NULL,
  invoice_id   UUID NOT NULL,
  amount       NUMERIC(10, 2) NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (student_id, section_id)
);
