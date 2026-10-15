import { generateKeyPairSync } from "node:crypto";
import { decodeProtectedHeader, importSPKI, jwtVerify } from "jose";
import { beforeEach, describe, expect, it } from "vitest";

import {
  JWT_AUDIENCE,
  JWT_ISSUER,
  mintServiceToken,
  resetKeyCache,
} from "../jwt.server";

const { privateKey, publicKey } = generateKeyPairSync("ed25519", {
  privateKeyEncoding: { type: "pkcs8", format: "pem" },
  publicKeyEncoding: { type: "spki", format: "pem" },
});

describe("mintServiceToken", () => {
  beforeEach(() => {
    process.env.JWT_PRIVATE_KEY = privateKey.replace(/\n/g, "\\n"); // as stored in env vars
    resetKeyCache();
  });

  it("produces an EdDSA token the backend's public key verifies", async () => {
    const token = await mintServiceToken("acme.myshopify.com");
    expect(decodeProtectedHeader(token).alg).toBe("EdDSA");

    const { payload } = await jwtVerify(
      token,
      await importSPKI(publicKey, "EdDSA"),
      {
        issuer: JWT_ISSUER,
        audience: JWT_AUDIENCE,
      },
    );
    expect(payload.sub).toBe("acme.myshopify.com");
    expect(payload.scope).toEqual(["merchant:read", "merchant:write"]);
    expect(payload.exp! - payload.iat!).toBe(60);
    expect(payload.jti).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("issues a unique jti per call", async () => {
    const a = await mintServiceToken("a.myshopify.com", ["internal:sync"]);
    const b = await mintServiceToken("a.myshopify.com", ["internal:sync"]);
    expect(a).not.toEqual(b);
  });

  it("fails loudly when the key is missing", async () => {
    delete process.env.JWT_PRIVATE_KEY;
    resetKeyCache();
    await expect(mintServiceToken("a.myshopify.com")).rejects.toThrow(
      /JWT_PRIVATE_KEY/,
    );
  });
});
