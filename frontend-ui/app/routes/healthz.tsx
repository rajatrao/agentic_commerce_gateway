import pg from "pg";

/**
 * Liveness/readiness probe for container health checks and docker-compose.
 * Uses a single raw `pg` connection so the probe does not depend on the Prisma engine.
 */
let pool: pg.Pool | undefined;

function getPool(): pg.Pool {
  if (!pool) {
    const url = new URL(process.env.DATABASE_URL ?? "");
    url.searchParams.delete("schema"); // Prisma-only parameter
    pool = new pg.Pool({ connectionString: url.toString(), max: 1 });
  }
  return pool;
}

/** Returns 200 when the database answers, 503 otherwise. */
export const loader = async () => {
  try {
    await getPool().query("SELECT 1");
    return Response.json({ status: "ok", database: "ok" });
  } catch {
    return Response.json({ status: "degraded", database: "unreachable" }, { status: 503 });
  }
};
