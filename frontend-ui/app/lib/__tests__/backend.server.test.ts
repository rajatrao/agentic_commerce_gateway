import { generateKeyPairSync } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  backend,
  BackendError,
  errorMessage,
  toQuery,
} from "../backend.server";
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

  it("previews a mapping rule with a longer timeout and patches active", async () => {
    fetchMock.mockImplementation(async () =>
      Response.json({
        products_scanned: 0,
        matched: 0,
        matches: [],
        unmatched_values: [],
      }),
    );
    const rule = {
      source_type: "tag" as const,
      source_key: "",
      match_pattern: "brand:(.+)",
      target_property: "brand",
      transform: {},
      priority: 100,
      active: true,
    };
    await backend.previewMappingRule("acme.myshopify.com", rule);
    await backend.setMappingRuleActive("acme.myshopify.com", "r 1", false);

    const [previewUrl, previewInit] = fetchMock.mock.calls[0];
    expect(previewUrl).toBe(
      "http://backend.internal:8000/api/v1/merchant/mapping-rules/preview",
    );
    expect(JSON.parse(previewInit.body)).toEqual(rule);
    const [patchUrl, patchInit] = fetchMock.mock.calls[1];
    expect(patchUrl).toBe(
      "http://backend.internal:8000/api/v1/merchant/mapping-rules/r%201",
    );
    expect(patchInit.method).toBe("PATCH");
    expect(JSON.parse(patchInit.body)).toEqual({ active: false });
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

  it("publishes only with explicit confirmation and the chosen ids", async () => {
    fetchMock.mockResolvedValue(
      Response.json({ published: 1, unchanged: 0, failed: 0 }),
    );
    await backend.publish("acme.myshopify.com", ["s1"]);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      "http://backend.internal:8000/api/v1/merchant/catalog/publish",
    );
    expect(JSON.parse(init.body)).toEqual({
      suggestion_ids: ["s1"],
      confirm: true,
    });
  });

  it("maps a missing catalog product to null", async () => {
    fetchMock.mockResolvedValue(
      Response.json({ detail: "product not found" }, { status: 404 }),
    );
    await expect(
      backend.catalogProduct("acme.myshopify.com", "123"),
    ).resolves.toBeNull();
  });
});

describe("helpers", () => {
  it("builds query strings without empty values", () => {
    expect(
      toQuery({
        status: "pending",
        product: "",
        limit: 50,
        writes_only: false,
      }),
    ).toBe("status=pending&limit=50&writes_only=false");
  });

  it("explains errors for merchants", () => {
    expect(errorMessage(new BackendError(409, "already running"))).toBe(
      "already running",
    );
    expect(errorMessage(new TypeError("fetch failed"))).toBe(
      "The gateway engine is unreachable.",
    );
  });
});
