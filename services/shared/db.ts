// Pool de conexión a PostgreSQL. Cada servicio usa su propia base (DB_NAME).
import { Pool } from "pg";

export function createPool(): Pool {
  return new Pool({
    host: process.env.DB_HOST || "postgres",
    port: Number(process.env.DB_PORT || 5432),
    user: process.env.POSTGRES_USER,
    password: process.env.POSTGRES_PASSWORD,
    database: process.env.DB_NAME,
    max: 10,
    connectionTimeoutMillis: 2000,
  });
}
