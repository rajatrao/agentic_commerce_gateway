import { importSPKI, jwtVerify, type CryptoKey, type KeyObject } from "jose";

/**
 * Verifies service JWTs minted by backend-engine for frontend-ui's /internal routes.
 * Mirror image of jwt.server.ts: backend-engine holds the Ed25519 private key,
 * frontend-ui only the public key (BACKEND_JWT_PUBLIC_KEY).
 */

export class ServiceAuthError extends Error {
  constructor(
    message: string,
    public readonly status: 401 | 403 = 401,
  ) {
    super(message);
    this.name = "ServiceAuthError";
  }
}

export interface BackendClaims {
  shop: string;
  scopes: string[];
}

let cachedKey: Promise<CryptoKey | KeyObject> | undefined;
const seenJti = new Map<string, number>();

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

/** Test hook. */
export function resetServiceAuthCache() {
  cachedKey = undefined;
  seenJti.clear();
}

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

  const now = Date.now();
  for (const [jti, exp] of seenJti) if (exp < now) seenJti.delete(jti);
  if (seenJti.has(payload.jti!)) {
    throw new ServiceAuthError("token replayed");
  }
  seenJti.set(payload.jti!, payload.exp! * 1000);

  const scopes = Array.isArray(payload.scope)
    ? (payload.scope as string[])
    : [];
  if (!scopes.includes(requiredScope)) {
    throw new ServiceAuthError(`missing scope ${requiredScope}`, 403);
  }
  return { shop: payload.sub!, scopes };
}
