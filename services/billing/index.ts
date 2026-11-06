// billing: calcula el valor de la matrícula y simula el cobro. Sin base de datos.
import { randomUUID } from "node:crypto";
import { createService } from "../shared/observability";

const COST_PER_CREDIT = Number(process.env.COST_PER_CREDIT || 45.5);
// Latencia simulada del "banco": base + variación aleatoria (configurable).
const BASE_LATENCY_MS = Number(process.env.BASE_LATENCY_MS || 50);
const JITTER_MS = Number(process.env.JITTER_MS || 30);

const { app, start } = createService({ name: "billing" });

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

app.post("/invoices", async (req, res) => {
  const { studentId, credits } = req.body ?? {};
  if (!Number.isInteger(studentId) || !Number.isInteger(credits) || credits <= 0) {
    res.status(400).json({ error: "studentId y credits (enteros) requeridos" });
    return;
  }
  await sleep(BASE_LATENCY_MS + Math.random() * JITTER_MS);
  res.status(201).json({
    invoiceId: randomUUID(),
    studentId,
    credits,
    amount: Number((credits * COST_PER_CREDIT).toFixed(2)),
    status: "paid",
  });
});

start();
