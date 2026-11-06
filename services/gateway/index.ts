// gateway: entrada única del sistema. Reenvía cada ruta a su servicio y
// balancea entre réplicas (round-robin, ver shared/http.ts). Sin base de datos.
import { Request, Response } from "express";
import { createService } from "../shared/observability";
import { callService, UpstreamError } from "../shared/http";

const { app, start } = createService({ name: "gateway" });

const TIMEOUT_MS = Number(process.env.UPSTREAM_TIMEOUT_MS || 5000);
const UPSTREAMS = ["students", "courses", "enrollment", "billing"];

// Estado de todos los servicios de un vistazo (útil para probar desde el navegador).
app.get("/status", async (_req, res) => {
  const result: Record<string, string> = {};
  await Promise.all(
    UPSTREAMS.map(async (svc) => {
      try {
        const r = await callService(svc, "/ready", { timeoutMs: 1000 });
        result[svc] = r.status === 200 ? "up" : `down (${r.status})`;
      } catch (err: any) {
        result[svc] = `down (${err?.kind ?? "error"})`;
      }
    })
  );
  res.json(result);
});

// Reenvía la petición tal cual (mismo método, ruta y body) al servicio indicado.
function forwardTo(service: string) {
  return async (req: Request, res: Response) => {
    try {
      const hasBody = req.method !== "GET" && req.method !== "HEAD";
      const r = await callService(service, req.originalUrl, {
        method: req.method,
        body: hasBody ? req.body ?? {} : undefined,
        timeoutMs: TIMEOUT_MS,
        retries: req.method === "GET" ? 1 : 0, // solo se reintentan lecturas
      });
      res.status(r.status).json(r.data);
    } catch (err) {
      if (err instanceof UpstreamError) {
        res.status(err.kind === "timeout" ? 504 : 502).json({ error: err.message });
      } else {
        console.error(err);
        res.status(500).json({ error: "error interno" });
      }
    }
  };
}

// Rutas públicas -> servicio. Se registran con nombre para que la métrica
// http_request_duration_seconds tenga la etiqueta route legible.
app.post("/login", forwardTo("students"));
app.get("/students/:id", forwardTo("students"));

app.get("/courses", forwardTo("courses"));
app.get("/courses/:id/sections", forwardTo("courses"));

app.post("/enrollments", forwardTo("enrollment"));
app.get("/enrollments/:studentId", forwardTo("enrollment"));

start();
