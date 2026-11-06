// enrollment: la matrícula. Llama a students, courses y billing. Base: enrollment_db.
// Aquí están las cadenas de llamadas por donde se propagan los fallos.
import { createService } from "../shared/observability";
import { createPool } from "../shared/db";
import { callService, UpstreamError } from "../shared/http";

const pool = createPool();
const { app, start } = createService({
  name: "enrollment",
  readyCheck: async () => {
    await pool.query("SELECT 1");
  },
});

const TIMEOUT_MS = Number(process.env.UPSTREAM_TIMEOUT_MS || 2000);
const RETRIES = Number(process.env.UPSTREAM_RETRIES || 2);

// Traduce un fallo de otro servicio a la respuesta que ve el usuario.
function upstreamFailure(res: any, err: unknown): void {
  if (err instanceof UpstreamError) {
    console.error(`[enrollment] ${err.message}`);
    res.status(err.kind === "timeout" ? 504 : 503).json({ error: err.message });
    return;
  }
  console.error(err);
  res.status(500).json({ error: "error interno" });
}

app.post("/enrollments", async (req, res) => {
  const { studentId, sectionId } = req.body ?? {};
  if (!Number.isInteger(studentId) || !Number.isInteger(sectionId)) {
    res.status(400).json({ error: "studentId y sectionId (enteros) requeridos" });
    return;
  }

  // 0. ¿Ya está matriculado en ese paralelo?
  try {
    const dup = await pool.query(
      "SELECT 1 FROM enrollments WHERE student_id = $1 AND section_id = $2",
      [studentId, sectionId]
    );
    if ((dup.rowCount ?? 0) > 0) { res.status(409).json({ error: "ya matriculado" }); return; }
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "error interno" });
    return;
  }

  // 1. Validar estudiante (GET: es seguro reintentar).
  try {
    const s = await callService("students", `/students/${studentId}`, {
      timeoutMs: TIMEOUT_MS,
      retries: RETRIES,
    });
    if (s.status === 404) { res.status(404).json({ error: "estudiante no encontrado" }); return; }
    if (s.status !== 200) { res.status(502).json({ error: "students respondió mal" }); return; }
  } catch (err) {
    upstreamFailure(res, err);
    return;
  }

  // 2. Reservar cupo (POST que modifica datos: SIN reintentos para no reservar dos veces).
  let section: any;
  try {
    const r = await callService("courses", `/sections/${sectionId}/reserve`, {
      method: "POST",
      timeoutMs: TIMEOUT_MS,
      retries: 0,
    });
    if (r.status === 404 || r.status === 409) { res.status(r.status).json(r.data); return; }
    if (r.status !== 200) { res.status(502).json({ error: "courses respondió mal" }); return; }
    section = r.data;
  } catch (err) {
    upstreamFailure(res, err);
    return;
  }

  // 3. Cobrar. Si falla, se libera el cupo reservado (compensación).
  let invoice: any;
  try {
    const b = await callService("billing", "/invoices", {
      method: "POST",
      body: { studentId, credits: section.credits },
      timeoutMs: TIMEOUT_MS,
      retries: RETRIES,
    });
    if (b.status !== 201) throw new UpstreamError(`billing respondió ${b.status}`, "unavailable");
    invoice = b.data;
  } catch (err) {
    await callService("courses", `/sections/${sectionId}/release`, {
      method: "POST",
      timeoutMs: TIMEOUT_MS,
      retries: RETRIES,
    }).catch(() => console.error("[enrollment] no se pudo liberar el cupo", sectionId));
    upstreamFailure(res, err);
    return;
  }

  // 4. Registrar la matrícula.
  try {
    const ins = await pool.query(
      `INSERT INTO enrollments (student_id, section_id, course_code, invoice_id, amount)
       VALUES ($1, $2, $3, $4, $5)
       RETURNING id, student_id, section_id, course_code, invoice_id, amount, created_at`,
      [studentId, sectionId, section.code, invoice.invoiceId, invoice.amount]
    );
    res.status(201).json({ ...ins.rows[0], course: section.name, parallel: section.parallel });
  } catch (err: any) {
    if (err?.code === "23505") { res.status(409).json({ error: "ya matriculado" }); return; }
    console.error(err);
    res.status(500).json({ error: "error interno" });
  }
});

app.get("/enrollments/:studentId", async (req, res) => {
  const studentId = Number(req.params.studentId);
  if (!Number.isInteger(studentId)) { res.status(400).json({ error: "id inválido" }); return; }
  try {
    const r = await pool.query(
      `SELECT id, student_id, section_id, course_code, invoice_id, amount, created_at
         FROM enrollments WHERE student_id = $1 ORDER BY created_at`,
      [studentId]
    );
    res.json(r.rows);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "error interno" });
  }
});

start(() => pool.end());
