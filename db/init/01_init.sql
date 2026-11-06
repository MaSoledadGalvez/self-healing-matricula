-- Una base por servicio (fallos aislados)
CREATE DATABASE students_db;
CREATE DATABASE courses_db;
CREATE DATABASE enrollment_db;

-- ===== students_db =====
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
