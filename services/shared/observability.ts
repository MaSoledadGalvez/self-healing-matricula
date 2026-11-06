// Código común a los 5 servicios: app Express, métricas Prometheus,
// /health, /ready, /metrics y apagado limpio.
import express, { Express, Request, Response, NextFunction } from "express";
import client from "prom-client";

export interface ServiceOptions {
  name: string;
  // Chequeo de "listo" propio de cada servicio (p. ej. conexión a su BD).
  readyCheck?: () => Promise<void>;
}

export interface Service {
  app: Express;
  register: client.Registry;
  start: (onShutdown?: () => Promise<void>) => void;
}

// Un registro de métricas por proceso (cada contenedor corre un solo servicio).
// Se exporta para que shared/http.ts registre ahí la latencia de las llamadas salientes.
export const register = new client.Registry();

export function createService(opts: ServiceOptions): Service {
  const { name } = opts;
  register.setDefaultLabels({ service: name });
  client.collectDefaultMetrics({ register }); // CPU, RAM y event loop del proceso

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

  const app = express();
  app.use(express.json());

  // Mide latencia y cuenta peticiones de negocio (incluye errores 5xx).
  app.use((req: Request, res: Response, next: NextFunction) => {
    if (req.path === "/metrics" || req.path === "/health" || req.path === "/ready") {
      return next();
    }
    const end = httpDuration.startTimer();
    res.on("finish", () => {
      const route = req.route?.path ?? "unmatched";
      const labels = { method: req.method, route, status: String(res.statusCode) };
      end(labels);
      httpTotal.inc(labels);
    });
    next();
  });

  // Vivo: el proceso responde.
  app.get("/health", (_req, res) => {
    res.json({ status: "ok", service: name });
  });

  // Listo: el proceso responde y sus dependencias propias funcionan.
  app.get("/ready", async (_req, res) => {
    try {
      if (opts.readyCheck) await opts.readyCheck();
      res.json({ status: "ready", service: name });
    } catch {
      res.status(503).json({ status: "not ready", service: name });
    }
  });

  app.get("/metrics", async (_req, res) => {
    res.set("Content-Type", register.contentType);
    res.send(await register.metrics());
  });

  const start = (onShutdown?: () => Promise<void>) => {
    const port = Number(process.env.PORT || 3000);
    const server = app.listen(port, () => {
      console.log(`[${name}] escuchando en puerto ${port}`);
    });
    const shutdown = () => {
      console.log(`[${name}] apagando...`);
      server.close(async () => {
        if (onShutdown) await onShutdown();
        process.exit(0);
      });
      setTimeout(() => process.exit(0), 5000).unref();
    };
    process.on("SIGTERM", shutdown);
    process.on("SIGINT", shutdown);
  };

  return { app, register, start };
}
