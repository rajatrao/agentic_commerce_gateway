import { importSPKI, jwtVerify, type CryptoKey, type KeyObject } from "jose";

/**
 * Verifies service JWTs minted by backend-engine for frontend-ui's /internal routes.
 * Mirror image of jwt.server.ts: backend-engine holds the Ed25519 private key,
 * frontend-ui only the public key (BACKEND_JWT_PUBLIC_KEY).
 */

/** A rejected service JWT, with the HTTP status to answer with. */
export class ServiceAuthError extends Error {
  constructor(
    message: string,
    public readonly status: 401 | 403 = 401,
  ) {
    super(message);
    this.name = "ServiceAuthError";
  }
}

/** The verified shop and scopes of a backend-engine service JWT. */
export interface BackendClaims {
  shop: string;
  scopes: string[];
}

/**
 * Records each accepted `jti` until its token expires. The default store is Postgres (Prisma),
 * so a token replayed against another frontend-ui replica is still rejected.
 */
export interface JtiStore {
  /** Returns false when the jti was already used. */
  claim(jti: string, expiresAt: Date): Promise<boolean>;
}

/** Expired rows are swept on roughly one in this many claims. */
const PURGE_ONE_IN = 100;

/** Postgres-backed `JtiStore`, shared by all replicas. */
export const prismaJtiStore: JtiStore = {
  async claim(jti, expiresAt) {
    // Imported lazily so modules (and tests) that never verify a token don't need a client.
    const { default: prisma } = await import("../db.server");
    if (Math.floor(Math.random() * PURGE_ONE_IN) === 0) {
      await prisma.serviceTokenJti.deleteMany({
        where: { expiresAt: { lt: new Date() } },
      });
    }
    try {
      await prisma.serviceTokenJti.create({ data: { jti, expiresAt } });
      return true;
    } catch (error) {
      if ((error as { code?: string }).code === "P2002") return false; // unique violation
      throw error;
    }
  },
};

/** Single-process store, for tests. */
export function memoryJtiStore(): JtiStore {
  const seen = new Map<string, number>();
  return {
    async claim(jti, expiresAt) {
      const now = Date.now();
      for (const [key, exp] of seen) if (exp < now) seen.delete(key);
      if (seen.has(jti)) return false;
      seen.set(jti, expiresAt.getTime());
      return true;
    },
  };
}

let cachedKey: Promise<CryptoKey | KeyObject> | undefined;
let jtiStore: JtiStore = prismaJtiStore;

function loadPublicKey(): Promise<CryptoKey | KeyObject> {
  if (!cachedKey) {
    const pem = process.env.BACKEND_JWT_PUBLIC_KEY?.replace(/\\n/g, "\n");
    if (!pem) {
      throw new ServiceAuthError("BACKEND_JWT_PUBLIC_KEY is not configured");
    }
    cachedKey = importSPKI(pem, "EdDSA");
  }
  return cachedKey;
}

/** Test hook: forget the cached key and use `store` (default: a fresh in-memory store). */
export function resetServiceAuthCache(store: JtiStore = memoryJtiStore()) {
  cachedKey = undefined;
  jtiStore = store;
}

/**
 * Verifies a request's backend-engine service JWT (signature, claims, replay)
 * and checks it grants `requiredScope`. Throws `ServiceAuthError` otherwise.
 */
export async function verifyBackendRequest(
  request: Request,
  requiredScope: string,
): Promise<BackendClaims> {
  const header = request.headers.get("Authorization") ?? "";
  const [scheme, token] = header.split(" ");
  if (scheme?.toLowerCase() !== "bearer" || !token) {
    throw new ServiceAuthError("missing bearer token");
  }

  let payload;
  try {
    ({ payload } = await jwtVerify(token, await loadPublicKey(), {
      algorithms: ["EdDSA"],
      issuer: "backend-engine",
      audience: "frontend-ui",
      clockTolerance: 10,
      requiredClaims: ["exp", "iat", "sub", "jti"],
    }));
  } catch (error) {
    if (error instanceof ServiceAuthError) throw error;
    throw new ServiceAuthError("invalid service token");
  }

  if (!(await jtiStore.claim(payload.jti!, new Date(payload.exp! * 1000)))) {
    throw new ServiceAuthError("token replayed");
  }

  const scopes = Array.isArray(payload.scope)
    ? (payload.scope as string[])
    : [];
  if (!scopes.includes(requiredScope)) {
    throw new ServiceAuthError(`missing scope ${requiredScope}`, 403);
  }
  return { shop: payload.sub!, scopes };
}
