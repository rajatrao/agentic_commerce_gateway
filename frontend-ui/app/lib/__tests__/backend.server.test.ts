import { generateKeyPairSync } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { backend, BackendError } from "../backend.server";
import { resetKeyCache } from "../jwt.server";

const { privateKey } = generateKeyPairSync("ed25519", {
  privateKeyEncoding: { type: "pkcs8", format: "pem" },
  publicKeyEncoding: { type: "spki", format: "pem" },
});

describe("backend client", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    process.env.JWT_PRIVATE_KEY = privateKey;
    process.env.BACKEND_INTERNAL_URL = "http://backend.internal:8000/";
    resetKeyCache();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("sends a bearer service token and JSON body", async () => {
    fetchMock.mockResolvedValue(Response.json({ id: "1", passed: true }));
    await backend.runInstallationCheck("acme.myshopify.com", "acme.example");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "http://backend.internal:8000/api/v1/merchant/installation-checks",
    );
    expect(init.method).toBe("POST");
    expect(init.headers.Authorization).toMatch(/^Bearer ey/);
    expect(JSON.parse(init.body)).toEqual({ store_url: "acme.example" });
  });

  it("maps a 404 on latest check to null", async () => {
    fetchMock.mockResolvedValue(
      Response.json({ detail: "no installation check yet" }, { status: 404 }),
    );
    await expect(
      backend.latestInstallationCheck("acme.myshopify.com"),
    ).resolves.toBeNull();
  });

  it("surfaces FastAPI error details", async () => {
    fetchMock.mockResolvedValue(
      Response.json({ detail: "merchant not installed" }, { status: 403 }),
    );
    const error = await backend
      .runInstallationCheck("acme.myshopify.com")
      .catch((e) => e);
    expect(error).toBeInstanceOf(BackendError);
    expect(error.status).toBe(403);
    expect(error.detail).toBe("merchant not installed");
  });
});
