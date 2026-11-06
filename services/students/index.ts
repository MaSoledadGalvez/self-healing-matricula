// students: login, carrera y semestre del estudiante. Base: students_db.
import { createService } from "../shared/observability";
import { createPool } from "../shared/db";

const pool = createPool();
const { app, start } = createService({
  name: "students",
  readyCheck: async () => {
    await pool.query("SELECT 1");
  },
});

// Login simplificado: solo por email (el foco del proyecto es la recuperación).
app.post("/login", async (req, res) => {
  const { email } = req.body ?? {};
  if (!email) { res.status(400).json({ error: "email requerido" }); return; }
  try {
    const r = await pool.query(
      "SELECT id, name, email, career, semester FROM students WHERE email = $1",
      [email]
    );
    if (r.rowCount === 0) { res.status(404).json({ error: "estudiante no encontrado" }); return; }
    res.json(r.rows[0]);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "error interno" });
  }
});

app.get("/students/:id", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id)) { res.status(400).json({ error: "id inválido" }); return; }
  try {
    const r = await pool.query(
      "SELECT id, name, email, career, semester FROM students WHERE id = $1",
      [id]
    );
    if (r.rowCount === 0) { res.status(404).json({ error: "estudiante no encontrado" }); return; }
    res.json(r.rows[0]);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "error interno" });
  }
});

start(() => pool.end());
