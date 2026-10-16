import { generateKeyPairSync, randomUUID } from "node:crypto";
import { SignJWT, importPKCS8 } from "jose";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  prismaJtiStore,
  resetServiceAuthCache,
  ServiceAuthError,
  verifyBackendRequest,
} from "../service-auth.server";

// A stand-in for the shared Postgres table: one Set seen by every "replica".
const sharedRows = new Set<string>();
vi.mock("../../db.server", () => ({
  default: {
    serviceTokenJti: {
      deleteMany: vi.fn(async () => ({ count: 0 })),
      create: vi.fn(async ({ data }: { data: { jti: string } }) => {
        if (sharedRows.has(data.jti)) {
          throw Object.assign(new Error("Unique constraint failed"), {
            code: "P2002",
          });
        }
        sharedRows.add(data.jti);
        return data;
      }),
    },
  },
}));

const keys = () =>
  generateKeyPairSync("ed25519", {
    privateKeyEncoding: { type: "pkcs8", format: "pem" },
    publicKeyEncoding: { type: "spki", format: "pem" },
  });
const backend = keys();

async function token(
  opts: { key?: string; scope?: string[]; aud?: string; jti?: string } = {},
) {
  return new SignJWT({ scope: opts.scope ?? ["admin-token:read"] })
    .setProtectedHeader({ alg: "EdDSA" })
    .setIssuer("backend-engine")
    .setAudience(opts.aud ?? "frontend-ui")
    .setSubject("acme.myshopify.com")
    .setIssuedAt()
    .setExpirationTime("60s")
    .setJti(opts.jti ?? randomUUID())
    .sign(await importPKCS8(opts.key ?? backend.privateKey, "EdDSA"));
}

const req = (jwt: string) =>
  new Request("http://x/internal/admin-token", {
    headers: { Authorization: `Bearer ${jwt}` },
  });

describe("verifyBackendRequest", () => {
  beforeEach(() => {
    process.env.BACKEND_JWT_PUBLIC_KEY = backend.publicKey;
    resetServiceAuthCache();
  });

  it("accepts a valid backend token", async () => {
    const claims = await verifyBackendRequest(
      req(await token()),
      "admin-token:read",
    );
    expect(claims.shop).toBe("acme.myshopify.com");
  });

  it("rejects replays", async () => {
    const jwt = await token();
    await verifyBackendRequest(req(jwt), "admin-token:read");
    await expect(
      verifyBackendRequest(req(jwt), "admin-token:read"),
    ).rejects.toThrow(/replayed/);
  });

  it("rejects wrong signer, audience and missing scope", async () => {
    await expect(
      verifyBackendRequest(
        req(await token({ key: keys().privateKey })),
        "admin-token:read",
      ),
    ).rejects.toBeInstanceOf(ServiceAuthError);
    await expect(
      verifyBackendRequest(
        req(await token({ aud: "backend-engine" })),
        "admin-token:read",
      ),
    ).rejects.toThrow(/invalid/);
    await expect(
      verifyBackendRequest(
        req(await token({ scope: ["other"] })),
        "admin-token:read",
      ),
    ).rejects.toMatchObject({ status: 403 });
  });

  it("rejects a missing header", async () => {
    await expect(
      verifyBackendRequest(new Request("http://x"), "admin-token:read"),
    ).rejects.toThrow(/missing bearer/);
  });
});

describe("jti stores", () => {
  beforeEach(() => {
    process.env.BACKEND_JWT_PUBLIC_KEY = backend.publicKey;
  });

  it("rejects a replay sent to another replica (shared Postgres store)", async () => {
    const jwt = await token();
    resetServiceAuthCache(prismaJtiStore); // replica A
    await verifyBackendRequest(req(jwt), "admin-token:read");
    resetServiceAuthCache(prismaJtiStore); // replica B: fresh process state, same table
    await expect(
      verifyBackendRequest(req(jwt), "admin-token:read"),
    ).rejects.toThrow(/replayed/);
  });
});
