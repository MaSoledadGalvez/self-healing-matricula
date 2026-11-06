// Llamadas entre servicios con balanceo entre réplicas, timeout y reintentos.
//
// Por qué resolvemos el DNS a mano: cuando se escala un servicio
// (docker compose up --scale courses=2, o el healer hace Scale), el DNS de
// Docker devuelve una IP por réplica. Si dejáramos que fetch reutilice la
// conexión, todas las peticiones irían a la misma réplica. Aquí repartimos
// en round-robin y refrescamos la lista cada pocos segundos, así las
// réplicas nuevas reciben tráfico enseguida.
import { Resolver } from "node:dns/promises";
import client from "prom-client";
import { register } from "./observability";

// Latencia vista por QUIEN LLAMA (incluye red). Necesaria para detectar latencia
// de red (tc netem): el servicio lento no la ve en sus propias métricas.
const upstreamDuration = new client.Histogram({
  name: "upstream_request_duration_seconds",
  help: "Duración de las llamadas a otros servicios, medida por el que llama",
  labelNames: ["target", "outcome"],
  buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
  registers: [register],
});

const DNS_TTL_MS = 2000;
const DNS_FAIL_TTL_MS = 1000;
const cache = new Map<string, { ips: string[]; expires: number; next: number }>();

// Resolver asíncrono (c-ares) con timeout corto. NO usamos dns.lookup porque usa
// un grupo de solo 4 hilos: si un servicio cae y su nombre tarda en resolverse,
// esos hilos se bloquean y las llamadas a los servicios SANOS también se atrasan
// (así un fallo en courses hacía parecer lento a students).
const resolver = new Resolver({ timeout: 500, tries: 1 });

async function pickAddress(host: string): Promise<string> {
  const now = Date.now();
  let entry = cache.get(host);
  if (!entry || entry.expires < now) {
    let ips: string[] = [];
    try {
      ips = (await resolver.resolve4(host)).sort();
    } catch {
      ips = []; // nombre sin réplicas vivas
    }
    entry = {
      ips,
      expires: now + (ips.length ? DNS_TTL_MS : DNS_FAIL_TTL_MS),
      next: entry?.next ?? 0,
    };
    cache.set(host, entry);
  }
  if (entry.ips.length === 0) throw new Error(`sin direcciones para ${host}`);
  const ip = entry.ips[entry.next % entry.ips.length];
  entry.next++;
  return ip;
}

export interface CallOptions {
  method?: string;
  body?: unknown;
  timeoutMs?: number; // tiempo máximo por intento
  retries?: number;   // reintentos extra (0 = un solo intento)
}

export interface CallResult {
  status: number;
  data: any;
}

export class UpstreamError extends Error {
  constructor(message: string, public readonly kind: "timeout" | "unavailable") {
    super(message);
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// host = nombre del servicio en Docker (p. ej. "courses"), path = "/courses/1"
export async function callService(
  host: string,
  path: string,
  opts: CallOptions = {}
): Promise<CallResult> {
  const { method = "GET", body, timeoutMs = 2000, retries = 0 } = opts;
  const port = Number(process.env.UPSTREAM_PORT || 3000);
  let lastError: UpstreamError | undefined;

  for (let attempt = 0; attempt <= retries; attempt++) {
    const end = upstreamDuration.startTimer({ target: host });
    try {
      const ip = await pickAddress(host);
      const res = await fetch(`http://${ip}:${port}${path}`, {
        method,
        headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: body !== undefined ? JSON.stringify(body) : undefined,
        signal: AbortSignal.timeout(timeoutMs),
      });
      const text = await res.text();
      end({ outcome: `${Math.floor(res.status / 100)}xx` });
      let data: any = text;
      try {
        data = text ? JSON.parse(text) : null;
      } catch {
        /* respuesta no JSON: se deja como texto */
      }
      // Un 5xx del otro servicio cuenta como fallo y se reintenta.
      if (res.status >= 500 && attempt < retries) {
        lastError = new UpstreamError(`${host} respondió ${res.status}`, "unavailable");
      } else {
        return { status: res.status, data };
      }
    } catch (err: any) {
      const isTimeout = err?.name === "TimeoutError" || err?.name === "AbortError";
      end({ outcome: isTimeout ? "timeout" : "error" });
      lastError = new UpstreamError(
        `${host}${path}: ${isTimeout ? "timeout" : err?.message ?? "error"}`,
        isTimeout ? "timeout" : "unavailable"
      );
    }
    if (attempt < retries) await sleep(100 * 2 ** attempt); // backoff: 100, 200, 400 ms
  }
  throw lastError ?? new UpstreamError(`${host} no disponible`, "unavailable");
}
