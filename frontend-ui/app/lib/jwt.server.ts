import { randomUUID } from "node:crypto";
import { SignJWT, importPKCS8, type CryptoKey, type KeyObject } from "jose";

/**
 * Mints short-lived service JWTs that authenticate frontend-ui to backend-engine.
 *
 * - Ed25519 (EdDSA): frontend-ui holds the private key, backend-engine only the public key,
 *   so a compromised backend cannot forge UI calls.
 * - `sub` is the merchant's shop domain, which backend-engine uses for tenant scoping.
 * - 60 second lifetime plus a unique `jti` that the backend rejects if replayed.
 */

export type ServiceScope = "merchant:read" | "merchant:write" | "internal:sync";

export const JWT_ISSUER = "frontend-ui";
export const JWT_AUDIENCE = "backend-engine";
const TTL_SECONDS = 60;

let cachedKey: Promise<CryptoKey | KeyObject> | undefined;

function loadPrivateKey(): Promise<CryptoKey | KeyObject> {
  if (!cachedKey) {
    const pem = process.env.JWT_PRIVATE_KEY?.replace(/\\n/g, "\n");
    if (!pem) {
      throw new Error("JWT_PRIVATE_KEY is not configured");
    }
    cachedKey = importPKCS8(pem, "EdDSA");
  }
  return cachedKey;
}

/** Test hook: forget the cached key so a new JWT_PRIVATE_KEY is picked up. */
export function resetKeyCache() {
  cachedKey = undefined;
}

export async function mintServiceToken(
  shop: string,
  scope: ServiceScope[] = ["merchant:read", "merchant:write"],
): Promise<string> {
  const key = await loadPrivateKey();
  return new SignJWT({ scope })
    .setProtectedHeader({ alg: "EdDSA", typ: "JWT" })
    .setIssuer(JWT_ISSUER)
    .setAudience(JWT_AUDIENCE)
    .setSubject(shop)
    .setIssuedAt()
    .setExpirationTime(`${TTL_SECONDS}s`)
    .setJti(randomUUID())
    .sign(key);
}
