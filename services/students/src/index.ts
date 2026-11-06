import express, { Request, Response, NextFunction } from "express";
import { Pool } from "pg";
import client from "prom-client";

const SERVICE = process.env.SERVICE_NAME || "students";
const PORT = Number(process.env.PORT || 3000);

// ---------- Base de datos ----------
const pool = new Pool({
  host: process.env.DB_HOST || "postgres",
  port: Number(process.env.DB_PORT || 5432),
  user: process.env.POSTGRES_USER,
  password: process.env.POSTGRES_PASSWORD,
  database: process.env.DB_NAME || "students_db",
  max: 10,
  connectionTimeoutMillis: 2000,
});

// ---------- Métricas Prometheus ----------
const register = new client.Registry();
register.setDefaultLabels({ service: SERVICE });
client.collectDefaultMetrics({ register }); // CPU, RAM, event loop del proceso

const httpDuration = new client.Histogram({
  name: "http_request_duration_seconds",
  help: "Duración de las peticiones HTTP en segundos",
  labelNames: ["method", "route", "status"],
  buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
  registers: [register],
});

const httpTotal = new client.Counter({
  name: "http_requests_total",
  help: "Total de peticiones HTTP",
  labelNames: ["method", "route", "status"],
  registers: [register],
});

// ---------- App ----------
const app = express();
app.use(express.json());

// Mide latencia y cuenta peticiones (incluye errores 5xx)
app.use((req: Request, res: Response, next: NextFunction) => {
  const end = httpDuration.startTimer();
  res.on("finish", () => {
    const route = req.route?.path ?? "unmatched";
    const labels = { method: req.method, route, status: String(res.statusCode) };
    end(labels);
    httpTotal.inc(labels);
  });
  next();
});

// Vivo: el proceso responde
app.get("/health", (_req, res) => {
  res.json({ status: "ok", service: SERVICE });
});

// Listo: el proceso responde Y llega a su base de datos
app.get("/ready", async (_req, res) => {
  try {
    await pool.query("SELECT 1");
    res.json({ status: "ready", service: SERVICE });
  } catch {
    res.status(503).json({ status: "not ready", service: SERVICE });
  }
});

app.get("/metrics", async (_req, res) => {
  res.set("Content-Type", register.contentType);
  res.send(await register.metrics());
});

// ---------- Endpoints de negocio ----------
// Login simplificado: solo por email (el foco del proyecto es la recuperación)
app.post("/login", async (req, res) => {
  const { email } = req.body ?? {};
  if (!email) return res.status(400).json({ error: "email requerido" });
  try {
    const r = await pool.query(
      "SELECT id, name, email, career, semester FROM students WHERE email = $1",
      [email]
    );
    if (r.rowCount === 0) return res.status(404).json({ error: "estudiante no encontrado" });
    res.json(r.rows[0]);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "error interno" });
  }
});

app.get("/students/:id", async (req, res) => {
  const id = Number(req.params.id);
  if (!Number.isInteger(id)) return res.status(400).json({ error: "id inválido" });
  try {
    const r = await pool.query(
      "SELECT id, name, email, career, semester FROM students WHERE id = $1",
      [id]
    );
    if (r.rowCount === 0) return res.status(404).json({ error: "estudiante no encontrado" });
    res.json(r.rows[0]);
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "error interno" });
  }
});

// ---------- Arranque y apagado limpio ----------
const server = app.listen(PORT, () => {
  console.log(`[${SERVICE}] escuchando en puerto ${PORT}`);
});

process.on("SIGTERM", () => {
  console.log(`[${SERVICE}] apagando...`);
  server.close(() => pool.end().then(() => process.exit(0)));
});