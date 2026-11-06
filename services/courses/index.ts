// courses: oferta de materias, paralelos, horarios y cupos. Base: courses_db.
import { createService } from "../shared/observability";
import { createPool } from "../shared/db";

const pool = createPool();
const { app, start } = createService({
  name: "courses",
  readyCheck: async () => {
    await pool.query("SELECT 1");
  },
});

// Lista de materias (la consulta más frecuente durante la matrícula).
app.get("/courses", async (_req, res) => {
  try {
    const r = await pool.query("SELECT id, code, name, credits FROM courses ORDER BY id");
    res.json(r.rows);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "error interno" });
  }
});

// Paralelos de una materia con horario y cupos disponibles.
app.get("/courses/:id/sections", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id)) { res.status(400).json({ error: "id inválido" }); return; }
  try {
    const r = await pool.query(
      `SELECT id, course_id, parallel, schedule, capacity, enrolled,
              capacity - enrolled AS available
         FROM sections WHERE course_id = $1 ORDER BY parallel`,
      [id]
    );
    if (r.rowCount === 0) { res.status(404).json({ error: "materia no encontrada" }); return; }
    res.json(r.rows);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "error interno" });
  }
});

// Reserva un cupo de forma atómica: solo suma si aún hay cupo.
app.post("/sections/:id/reserve", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id)) { res.status(400).json({ error: "id inválido" }); return; }
  try {
    const r = await pool.query(
      `UPDATE sections s SET enrolled = s.enrolled + 1
         FROM courses c
        WHERE s.id = $1 AND s.course_id = c.id AND s.enrolled < s.capacity
    RETURNING s.id, s.course_id, s.parallel, s.schedule, c.code, c.name, c.credits`,
      [id]
    );
    if (r.rowCount === 0) {
      const exists = await pool.query("SELECT 1 FROM sections WHERE id = $1", [id]);
      if (exists.rowCount === 0) { res.status(404).json({ error: "paralelo no encontrado" }); return; }
      res.status(409).json({ error: "sin cupos" });
      return;
    }
    res.json(r.rows[0]);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "error interno" });
  }
});

// Libera un cupo (lo usa enrollment si el cobro falla después de reservar).
app.post("/sections/:id/release", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id)) { res.status(400).json({ error: "id inválido" }); return; }
  try {
    await pool.query(
      "UPDATE sections SET enrolled = enrolled - 1 WHERE id = $1 AND enrolled > 0",
      [id]
    );
    res.json({ released: true });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "error interno" });
  }
});

start(() => pool.end());
